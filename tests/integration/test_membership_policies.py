from datetime import UTC, datetime
from uuid import uuid4

import pytest
from db.dal import payment_dal, rollypay_dal, subscription_dal, wata_subscription_dal
from minishop_corp.integration.access import AccessAdapter
from minishop_corp.integration.access_types import AccessError, DisabledAccess, TrialAccess

from .conftest import CORP_SQUAD, USER_ID, Host
from .test_access import seed_personal, target

pytestmark = pytest.mark.asyncio(loop_scope="session")


@pytest.mark.parametrize("setting", ["disabled", "zero"])
async def test_departure_without_trial_disables_access_and_keeps_account(
    host: Host, setting: str
) -> None:
    adapter = AccessAdapter(host.service)
    corporate = await adapter.assign_period(host.session, USER_ID, target())
    if setting == "disabled":
        host.settings.TRIAL_ENABLED = False
    else:
        host.settings.TRIAL_DURATION_DAYS = 0
    departure = adapter.prepare_departure(starts_at=datetime.now(UTC))
    assert isinstance(departure, DisabledAccess)
    state = await adapter.disable_access(host.session, USER_ID, departure)
    assert state.subscription_id == corporate.subscription_id and not state.is_active
    assert state.tariff_key is None and state.provider != "trial"
    assert state.end_date == departure.ends_at
    panel = host.panel.users[state.panel_user_uuid]
    assert panel["status"] == "EXPIRED"
    assert CORP_SQUAD not in panel["activeInternalSquads"]
    assert panel["externalSquadUuid"] is None and panel["tag"] != "CORP"
    repeated = await adapter.disable_access(host.session, USER_ID, departure)
    assert repeated.end_date == departure.ends_at and not repeated.is_active
    assert len(host.panel.users) == 1
    assert await subscription_dal.get_active_subscription_by_user_id(host.session, USER_ID) is None


async def test_enabled_trial_prepares_fixed_standard_window(host: Host) -> None:
    departure = AccessAdapter(host.service).prepare_departure(starts_at=datetime.now(UTC))
    assert isinstance(departure, TrialAccess)
    assert (departure.ends_at - departure.starts_at).days == host.settings.TRIAL_DURATION_DAYS


@pytest.mark.parametrize(
    "status",
    [
        "pending",
        "PENDING_YOOKASSA",
        "creation_unknown",
        "succeeded_pending_finalization",
        "succeeded_pending_review",
        "unknown-future-status",
    ],
)
async def test_pending_payments_block_join_without_mutation(host: Host, status: str) -> None:
    payment = await payment_dal.create_payment_record(
        host.session,
        {"user_id": USER_ID, "amount": 100, "currency": "RUB", "status": status},
    )
    with pytest.raises(AccessError, match="payment_pending"):
        await AccessAdapter(host.service).validate_join(host.session, USER_ID)
    await host.session.refresh(payment)
    assert payment.status == status
    assert host.panel.requests == []


@pytest.mark.parametrize(
    "status",
    ["succeeded", "failed", "canceled", "cancelled", "failed_creation", "refunded", "reversed"],
)
async def test_terminal_payment_does_not_block_join(host: Host, status: str) -> None:
    await payment_dal.create_payment_record(
        host.session,
        {"user_id": USER_ID, "amount": 100, "currency": "RUB", "status": status},
    )
    await AccessAdapter(host.service).validate_join(host.session, USER_ID)
    assert host.panel.requests == []


async def test_renewal_requires_native_disable_and_is_not_cancelled_by_gate(host: Host) -> None:
    await seed_personal(host, "paid")
    row = await subscription_dal.get_active_subscription_by_user_id(host.session, USER_ID)
    assert row is not None
    await subscription_dal.set_auto_renew(host.session, row.subscription_id, True)
    before = list(host.panel.requests)
    adapter = AccessAdapter(host.service)
    with pytest.raises(AccessError, match="renewal_policy_required"):
        await adapter.validate_join(host.session, USER_ID)
    await host.session.refresh(row)
    assert row.auto_renew_enabled and host.panel.requests == before
    await subscription_dal.set_auto_renew(host.session, row.subscription_id, False)
    await adapter.validate_join(host.session, USER_ID)


@pytest.mark.parametrize("provider", ["wata", "rollypay"])
async def test_provider_managed_renewal_blocks_even_without_local_subscription(
    host: Host, provider: str
) -> None:
    anchor = await payment_dal.create_payment_record(
        host.session,
        {"user_id": USER_ID, "amount": 100, "currency": "RUB", "status": "succeeded"},
    )
    if provider == "wata":
        await wata_subscription_dal.create_from_anchor(
            host.session,
            subscription_id=str(uuid4()),
            anchor=anchor,
            interval="month",
            period=1,
            max_periods=12,
        )
    else:
        await rollypay_dal.create_or_update_subscription(
            host.session,
            subscription_id=str(uuid4()),
            anchor_payment_id=anchor.payment_id,
            user_id=USER_ID,
            provider_state="active",
            billing_status="enabled",
            plan_id="fixture",
            plan_code="fixture",
            plan_version=1,
            interval="month",
            max_cycles=12,
            amount=100,
            months=1,
            sale_mode="subscription",
            tariff_key="personal",
        )
    with pytest.raises(AccessError, match="renewal_policy_required"):
        await AccessAdapter(host.service).validate_join(host.session, USER_ID)
    assert host.panel.requests == []
