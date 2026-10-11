"""A scheduled check must not edit a profile whose contract already matches."""

from collections.abc import Iterator
from contextlib import contextmanager
from copy import deepcopy
from datetime import UTC, datetime, timedelta

import pytest
from bot.services.account_roles import grant_role
from db.dal import subscription_dal, tariff_dal
from db.dal import user_panel_squad_override_dal as squad_dal
from minishop_corp.integration.access import AccessAdapter
from minishop_corp.integration.access_types import PeriodAccess, panel_time
from minishop_corp.storage.schema import Membership, Operation
from sqlalchemy import event

from .conftest import CORP_SQUAD, EXTERNAL_SQUAD, USER_ID, Host
from .test_memberships import execute, joined
from .test_reconciliation import due, revise

pytestmark = pytest.mark.asyncio(loop_scope="session")


@contextmanager
def profile_writes(host: Host) -> Iterator[list[str]]:
    writes: list[str] = []

    def record(
        connection: object,
        cursor: object,
        statement: str,
        parameters: object,
        context: object,
        executemany: bool,
    ) -> None:
        words = statement.lstrip().split()
        if not words or words[0] not in {"INSERT", "UPDATE", "DELETE"}:
            return
        table = words[1] if words[0] == "UPDATE" else words[2]
        if not table.startswith("ext_minishop_corp_"):
            writes.append(table)

    bind = host.session.get_bind()
    event.listen(bind, "before_cursor_execute", record)
    try:
        yield writes
    finally:
        event.remove(bind, "before_cursor_execute", record)


@pytest.mark.parametrize("expired", [False, True])
@pytest.mark.parametrize(
    ("external", "default"), [(None, None), (None, EXTERNAL_SQUAD), (EXTERNAL_SQUAD, None)]
)
async def test_repeated_sweeps_only_read_matching_profiles(
    host: Host, expired: bool, external: str | None, default: str | None
) -> None:
    accepted, member_id = await joined(host)
    host.settings.USER_EXTERNAL_SQUAD_UUID = default
    await grant_role(host.session, USER_ID, "admin", source="corp_test")
    changes: dict[str, object] = {"external_squad_uuid": external}
    if expired:
        changes["ends_at"] = panel_time(datetime.now(UTC) - timedelta(days=1))
    await revise(host, accepted.offer.contract_id, **changes)
    # Establish the conditions first; the following sweeps must only verify them.
    assert await execute(host, await due(host, member_id))
    for _ in range(2):
        operation_id = await due(host, member_id)
        before = len(host.panel.requests)
        with profile_writes(host) as writes:
            assert await execute(host, operation_id)
        assert writes == []
        requests = host.panel.requests[before:]
        assert requests and all(method == "GET" for method, _ in requests)
        operation = await host.session.get(Operation, operation_id)
        member = await host.session.get(Membership, member_id, populate_existing=True)
        assert operation is not None and operation.state == "succeeded"
        assert member is not None and member.last_reconciled_at is not None


@pytest.mark.parametrize("side", ["minishop", "panel"])
@pytest.mark.parametrize(
    "delta", [timedelta(days=-5), timedelta(days=5), timedelta(milliseconds=1)]
)
async def test_sweep_repairs_expiry_drift_on_either_side_once(
    host: Host, side: str, delta: timedelta
) -> None:
    _, member_id = await joined(host)
    adapter = AccessAdapter(host.service)
    state = await adapter.read(host.session, USER_ID)
    assert state is not None
    drifted = state.end_date + delta
    if side == "minishop":
        await subscription_dal.update_subscription(
            host.session, state.subscription_id, {"end_date": drifted}
        )
    else:
        # A warm native cache must not hide panel-only drift.
        host.service.panel_service._users_cache.ttl_seconds = 300
        cached = await host.service.panel_service.get_user_by_uuid(state.panel_user_uuid)
        host.panel.users[state.panel_user_uuid]["expireAt"] = drifted.isoformat()
        before = len(host.panel.requests)
        assert await host.service.panel_service.get_user_by_uuid(state.panel_user_uuid) == cached
        assert len(host.panel.requests) == before
    before = len(host.panel.requests)
    assert await execute(host, await due(host, member_id))
    assert any(method == "PATCH" for method, _ in host.panel.requests[before:])
    restored = await adapter.read(host.session, USER_ID)
    assert restored is not None and restored.end_date == state.end_date
    assert (
        datetime.fromisoformat(str(host.panel.users[state.panel_user_uuid]["expireAt"]))
        == state.end_date
    )
    before = len(host.panel.requests)
    assert await execute(host, await due(host, member_id))
    assert all(method == "GET" for method, _ in host.panel.requests[before:])


@pytest.mark.parametrize(
    ("side", "field", "value"),
    [
        ("minishop", "tariff_key", "personal"),
        ("minishop", "hwid_device_limit", 1),
        ("minishop", "traffic_limit_bytes", 1),
        ("minishop", "is_active", False),
        ("minishop", "tier_baseline_bytes", 1),
        ("minishop", "premium_is_limited", True),
        ("panel", "tag", "PERSONAL"),
        ("panel", "hwidDeviceLimit", 1),
        ("panel", "trafficLimitBytes", 1),
        ("panel", "trafficLimitStrategy", "DAY"),
        ("panel", "status", "DISABLED"),
        ("panel", "activeInternalSquads", []),
        ("panel", "externalSquadUuid", None),
    ],
)
async def test_sweep_restores_contract_fields_then_stops_editing(
    host: Host, side: str, field: str, value: object
) -> None:
    _, member_id = await joined(host)
    adapter = AccessAdapter(host.service)
    state = await adapter.read(host.session, USER_ID)
    assert state is not None
    expected_panel = deepcopy(host.panel.users[state.panel_user_uuid])
    if side == "minishop":
        await subscription_dal.update_subscription(
            host.session, state.subscription_id, {field: value}
        )
    else:
        assert expected_panel[field] != value
        host.panel.users[state.panel_user_uuid][field] = value
    before = len(host.panel.requests)
    assert await execute(host, await due(host, member_id))
    assert any(method == "PATCH" for method, _ in host.panel.requests[before:])
    assert await adapter.read(host.session, USER_ID) == state
    assert host.panel.users[state.panel_user_uuid] == expected_panel
    before = len(host.panel.requests)
    with profile_writes(host) as writes:
        assert await execute(host, await due(host, member_id))
    assert writes == []
    assert all(method == "GET" for method, _ in host.panel.requests[before:])


async def test_sweep_restores_native_external_override_even_when_panel_matches(host: Host) -> None:
    _, member_id = await joined(host)
    state = await AccessAdapter(host.service).read(host.session, USER_ID)
    assert state is not None
    await squad_dal.deactivate_external_override(
        host.session, user_id=USER_ID, panel_user_uuid=state.panel_user_uuid
    )
    assert await execute(host, await due(host, member_id))
    override = await squad_dal.get_active_external_override(
        host.session, user_id=USER_ID, panel_user_uuid=state.panel_user_uuid
    )
    assert override is not None and override.squad_uuid == EXTERNAL_SQUAD
    before = len(host.panel.requests)
    with profile_writes(host) as writes:
        assert await execute(host, await due(host, member_id))
    assert writes == []
    assert all(method == "GET" for method, _ in host.panel.requests[before:])


async def test_manual_tag_and_squads_do_not_trigger_profile_edits(host: Host) -> None:
    _, member_id = await joined(host)
    state = await AccessAdapter(host.service).read(host.session, USER_ID)
    assert state is not None
    panel = host.panel.users[state.panel_user_uuid]
    panel["tag"] = "MANUAL"
    # The panel may return squad objects in another order, not just a list of UUIDs.
    panel["activeInternalSquads"] = [
        {"uuid": "10000000-0000-4000-8000-000000000099"},
        {"uuid": CORP_SQUAD},
    ]
    for _ in range(2):
        before = len(host.panel.requests)
        with profile_writes(host) as writes:
            assert await execute(host, await due(host, member_id))
        assert writes == []
        assert all(method == "GET" for method, _ in host.panel.requests[before:])


async def test_native_bonuses_and_extra_devices_are_included_in_comparison(host: Host) -> None:
    _, member_id = await joined(host)
    adapter = AccessAdapter(host.service)
    state = await adapter.read(host.session, USER_ID)
    assert state is not None
    await tariff_dal.create_hwid_device_purchase(
        host.session,
        subscription_id=state.subscription_id,
        payment_id=None,
        purchased_devices=2,
        valid_until=datetime.now(UTC) + timedelta(days=30),
    )
    await subscription_dal.update_subscription(
        host.session, state.subscription_id, {"regular_bonus_bytes": 3 * 1024**3}
    )
    await adapter.assign_period(
        host.session, USER_ID, PeriodAccess("corp", state.end_date, EXTERNAL_SQUAD)
    )
    panel = host.panel.users[state.panel_user_uuid]
    assert panel["hwidDeviceLimit"] == 7 and panel["trafficLimitBytes"] == 23 * 1024**3
    before = len(host.panel.requests)
    with profile_writes(host) as writes:
        assert await execute(host, await due(host, member_id))
    assert writes == []
    assert all(method == "GET" for method, _ in host.panel.requests[before:])


async def test_fractional_tariff_price_does_not_create_permanent_drift(host: Host) -> None:
    tariff = host.settings.tariffs_config.require_configured("corp")
    tariff.prices["rub"]["1"] = 100.1
    _, member_id = await joined(host)
    before = len(host.panel.requests)
    with profile_writes(host) as writes:
        assert await execute(host, await due(host, member_id))
    assert writes == []
    assert all(method == "GET" for method, _ in host.panel.requests[before:])


async def test_unavailable_panel_retries_without_editing_matching_local_profile(host: Host) -> None:
    _, member_id = await joined(host)
    member = await host.session.get(Membership, member_id)
    assert member is not None
    checked_at = member.last_reconciled_at
    operation_id = await due(host, member_id)
    before = len(host.panel.requests)
    host.panel.fail_reads = True
    with profile_writes(host) as writes:
        assert not await execute(host, operation_id)
    assert writes == []
    assert all(method == "GET" for method, _ in host.panel.requests[before:])
    operation = await host.session.get(Operation, operation_id)
    assert operation is not None and operation.state == "retry"
    assert operation.error_code == "minishop_corp_panel_unconfirmed"
    await host.session.refresh(member)
    assert member.last_reconciled_at == checked_at
    host.panel.fail_reads = False
    before = len(host.panel.requests)
    with profile_writes(host) as writes:
        assert await execute(host, operation_id, due=True)
    assert writes == []
    assert all(method == "GET" for method, _ in host.panel.requests[before:])


async def test_retry_rechecks_drift_that_was_already_repaired(host: Host) -> None:
    _, member_id = await joined(host)
    state = await AccessAdapter(host.service).read(host.session, USER_ID)
    assert state is not None
    panel = host.panel.users[state.panel_user_uuid]
    expiry = panel["expireAt"]
    panel["expireAt"] = (state.end_date + timedelta(days=1)).isoformat()
    operation_id = await due(host, member_id)
    host.panel.fail_updates = True
    assert not await execute(host, operation_id)
    host.panel.fail_updates = False
    panel["expireAt"] = expiry
    before = len(host.panel.requests)
    with profile_writes(host) as writes:
        assert await execute(host, operation_id, due=True)
    assert writes == []
    assert all(method == "GET" for method, _ in host.panel.requests[before:])


async def test_contract_metadata_revision_does_not_edit_member_profile(host: Host) -> None:
    accepted, member_id = await joined(host)
    await grant_role(host.session, USER_ID, "admin", source="corp_test")
    await revise(host, accepted.offer.contract_id, name="Renamed contract")
    before = len(host.panel.requests)
    with profile_writes(host) as writes:
        assert await execute(host, await due(host, member_id))
    assert writes == []
    assert all(method == "GET" for method, _ in host.panel.requests[before:])
    member = await host.session.get(Membership, member_id, populate_existing=True)
    assert member is not None and member.applied_version == member.scheduled_version == 2
