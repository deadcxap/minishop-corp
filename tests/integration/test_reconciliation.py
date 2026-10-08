from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from aiohttp.test_utils import TestClient
from db.dal import payment_dal, subscription_dal
from minishop_corp.contracts import Contracts, terms_of
from minishop_corp.contracts_types import UpdateContract
from minishop_corp.integration.access import AccessAdapter
from minishop_corp.integration.access_types import panel_time
from minishop_corp.integration.contracts import ContractHost
from minishop_corp.membership_types import DepartMembership
from minishop_corp.memberships import Memberships
from minishop_corp.operation_worker import OperationWorker
from minishop_corp.storage.reconciliation import candidates, schedule_member
from minishop_corp.storage.schema import (
    Contract,
    Invitation,
    Membership,
    Operation,
    ReconciliationSweep,
)
from minishop_corp.storage.sweeps import (
    SWEEP_INTERVAL,
    claim_sweep,
    finish_sweep_batch,
    sweep_candidates,
)
from sqlalchemy import func, select

from .conftest import PERSONAL_SQUAD, TRIAL_SQUAD, USER_ID, Host
from .test_contracts import ADMIN_PATH, MANAGER, MEMBER, OTHER, USER_PATH, authorization
from .test_contracts import client as client
from .test_memberships import execute, invitation, joined

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def revise(host: Host, contract_id: UUID, **changes: object) -> None:
    row = await host.session.get(Contract, contract_id, populate_existing=True)
    assert row is not None
    draft = UpdateContract.model_validate(
        {**terms_of(row).model_dump(), "expected_version": row.version, **changes}
    )
    await Contracts(ContractHost(host.service)).update(host.session, USER_ID, contract_id, draft)


async def due(host: Host, member_id: UUID) -> UUID:
    # Move the shared deadline, never an individual participant's timer.
    sweep = await host.session.get(ReconciliationSweep, 1)
    assert sweep is not None
    sweep.next_run_at = datetime.now(UTC)
    await host.session.flush()
    now = datetime.now(UTC)
    operations = {}
    for _ in range(50):
        lease = await claim_sweep(host.session, now=now)
        if lease is None:
            break
        batch = await sweep_candidates(host.session, lease)
        for identifier in batch:
            operation_id = await schedule_member(host.session, identifier, now=now, sweep=lease)
            assert operation_id is not None
            operations[identifier] = operation_id
        assert await finish_sweep_batch(host.session, lease, batch[-1] if batch else None, now=now)
    else:
        pytest.fail("Global sweep failed to complete")
    return operations[member_id]


@pytest.mark.parametrize("delta", [-90, 90])
async def test_periodic_deadline_repairs_both_directions_including_inactive(
    host: Host, delta: int
) -> None:
    draft, member_id = await joined(host)
    member = await host.session.get(Membership, member_id)
    assert member is not None and member.last_reconciled_at is not None
    sweep = await host.session.get(ReconciliationSweep, 1)
    assert sweep is not None
    deadline = max(sweep.next_run_at, datetime.now(UTC))
    sweep.next_run_at = deadline
    await host.session.flush()
    assert await claim_sweep(host.session, now=deadline - timedelta(microseconds=1)) is None
    lease = await claim_sweep(host.session, now=deadline)
    assert lease is not None and member_id in await sweep_candidates(host.session, lease)
    assert sweep.next_run_at == deadline + SWEEP_INTERVAL
    state = await AccessAdapter(host.service).read(host.session, USER_ID)
    assert state is not None
    drifted = state.end_date + timedelta(days=delta)
    await subscription_dal.update_subscription(
        host.session, state.subscription_id, {"end_date": drifted, "is_active": delta > 0}
    )
    host.panel.users[state.panel_user_uuid]["expireAt"] = drifted.isoformat()
    operation_id = await schedule_member(host.session, member_id, now=deadline, sweep=lease)
    assert operation_id is not None
    assert await schedule_member(host.session, member_id, now=deadline, sweep=lease) is None
    assert await execute(host, operation_id, due=True)
    await host.session.refresh(member)
    restored = await AccessAdapter(host.service).read(host.session, USER_ID)
    assert restored is not None and restored.end_date == state.end_date and restored.is_active
    assert member.state == "active" and member.applied_version == 1
    invite = await host.session.get(Invitation, draft.offer.invitation_id)
    assert invite is not None and invite.used_count == 1 and invite.reserved_count == 0
    assert member.scheduled_run_id == lease.run_id
    assert member.last_reconciled_at is not None


async def test_revision_is_due_immediately_and_replaces_tariff_squad_and_date(
    client: TestClient, host: Host
) -> None:
    draft, member_id = await joined(host, MEMBER)
    member = await host.session.get(Membership, member_id)
    assert member is not None
    old_joined_at = member.joined_at
    assert member_id not in await candidates(host.session)
    squad = "20000000-0000-4000-8000-000000000002"
    host.panel.external_squads[squad] = {"uuid": squad, "name": "Second synthetic squad"}
    target_end = panel_time(datetime.now(UTC) + timedelta(days=2))
    await revise(
        host,
        draft.offer.contract_id,
        tariff_key="personal",
        external_squad_uuid=squad,
        ends_at=target_end,
    )
    assert member_id in await candidates(host.session)
    operation_id = await schedule_member(host.session, member_id, now=datetime.now(UTC))
    assert operation_id is not None
    assert await execute(host, operation_id)
    await host.session.refresh(member)
    state = await AccessAdapter(host.service).read(host.session, MEMBER)
    assert state is not None and state.tariff_key == "personal" and state.end_date == target_end
    panel = host.panel.users[state.panel_user_uuid]
    assert panel["activeInternalSquads"] == [PERSONAL_SQUAD]
    assert panel["externalSquadUuid"] == squad and panel["tag"] == "PERSONAL"
    assert member.applied_version == 2 and member.joined_at == old_joined_at
    assert await schedule_member(host.session, member_id, now=datetime.now(UTC)) is None


async def test_expiry_keeps_membership_and_renewal_restores_without_code(
    client: TestClient, host: Host
) -> None:
    draft, member_id = await joined(host, MEMBER)
    expired = panel_time(datetime.now(UTC))
    await revise(host, draft.offer.contract_id, ends_at=expired)
    operation_id = await schedule_member(host.session, member_id, now=datetime.now(UTC))
    assert operation_id is not None and await execute(host, operation_id)
    member = await Memberships(ContractHost(host.service)).current(host.session, MEMBER)
    assert (
        member is not None
        and member.id == member_id
        and member.contract.expired
        and member.can_leave
    )
    state = await AccessAdapter(host.service).read(host.session, MEMBER)
    assert (
        state is not None
        and state.end_date == expired
        and not state.is_active
        and state.provider != "trial"
    )
    assert host.panel.users[state.panel_user_uuid]["status"] == "EXPIRED"
    # The periodic sweep still includes an expired contract and inactive subscription.
    sweep = await due(host, member_id)
    assert await execute(host, sweep)
    renewed = panel_time(datetime.now(UTC) + timedelta(days=40))
    await revise(host, draft.offer.contract_id, ends_at=renewed)
    renewal = await schedule_member(host.session, member_id, now=datetime.now(UTC))
    assert renewal is not None and await execute(host, renewal)
    after = await Memberships(ContractHost(host.service)).current(host.session, MEMBER)
    state = await AccessAdapter(host.service).read(host.session, MEMBER)
    assert after is not None and after.id == member_id and not after.contract.expired
    assert state is not None and state.is_active and state.end_date == renewed
    invite = await host.session.get(Invitation, draft.offer.invitation_id)
    assert invite is not None and invite.used_count == 1


@pytest.mark.parametrize("redispatch", [False, True])
async def test_old_claim_cannot_apply_old_terms_after_new_revision(
    client: TestClient, host: Host, redispatch: bool
) -> None:
    draft, member_id = await joined(host, MEMBER)
    await revise(host, draft.offer.contract_id, ends_at=datetime.now(UTC) + timedelta(days=35))
    operation_id = await schedule_member(host.session, member_id, now=datetime.now(UTC))
    worker = OperationWorker(ContractHost(host.service))
    assert operation_id is not None
    stale = await worker.claim(host.session, operation_id=operation_id)
    assert stale is not None
    await host.session.commit()
    target_end = panel_time(datetime.now(UTC) + timedelta(days=3))
    await revise(host, draft.offer.contract_id, ends_at=target_end)
    if redispatch:
        assert await schedule_member(host.session, member_id, now=datetime.now(UTC)) == operation_id
        requests = list(host.panel.requests)
        assert not await worker.execute(host.session, stale)
        assert host.panel.requests == requests
        assert await execute(host, operation_id)
    else:
        assert await worker.execute(host.session, stale)
    state = await AccessAdapter(host.service).read(host.session, MEMBER)
    assert state is not None and state.end_date == target_end
    member = await host.session.get(Membership, member_id)
    assert member is not None and member.applied_version == member.scheduled_version == 3
    assert member_id not in await candidates(host.session)


@pytest.mark.parametrize("phase", ["pending", "running", "retry"])
async def test_departure_supersedes_reconciliation_even_after_uncertain_effect(
    host: Host, phase: str
) -> None:
    draft, member_id = await joined(host)
    repair_id = await due(host, member_id)
    worker = OperationWorker(ContractHost(host.service))
    stale = None
    if phase != "pending":
        stale = await worker.claim(host.session, operation_id=repair_id)
        assert stale is not None
        await host.session.commit()
    if phase == "retry":
        state = await AccessAdapter(host.service).read(host.session, USER_ID)
        assert state is not None
        host.panel.users[state.panel_user_uuid]["expireAt"] = (
            datetime.now(UTC) + timedelta(days=100)
        ).isoformat()
        host.panel.fail_updates = True
        assert stale is not None and not await worker.execute(host.session, stale)
        host.panel.fail_updates = False
    departure = await Memberships(ContractHost(host.service)).depart(
        host.session, USER_ID, member_id, DepartMembership(request_id=uuid4())
    )
    old = await host.session.get(Operation, repair_id)
    assert old is not None and old.state == "cancelled" and old.lease_token is None
    assert await execute(host, departure.id)
    state = await AccessAdapter(host.service).read(host.session, USER_ID)
    assert state is not None and state.provider == "trial"
    assert host.panel.users[state.panel_user_uuid]["activeInternalSquads"] == [TRIAL_SQUAD]
    before = list(host.panel.requests)
    if stale is not None:
        assert not await worker.execute(host.session, stale)
    assert (
        await schedule_member(host.session, member_id, now=datetime.now(UTC) + timedelta(days=20))
        is None
    )
    assert host.panel.requests == before
    service = Memberships(ContractHost(host.service))
    rejoin = await service.confirm(
        host.session,
        USER_ID,
        draft.model_copy(update={"request_id": uuid4()}),
        now=datetime.now(UTC) + timedelta(minutes=2),
    )
    assert rejoin.operation is not None and await execute(host, rejoin.operation.id)
    if stale is not None:
        assert not await worker.execute(host.session, stale)
    current = await service.current(host.session, USER_ID)
    assert current is not None and current.id != member_id and current.state == "active"


async def test_personal_payment_does_not_block_repair_or_get_cancelled(host: Host) -> None:
    _, member_id = await joined(host)
    payment = await payment_dal.create_payment_record(
        host.session, {"user_id": USER_ID, "amount": 10, "currency": "RUB", "status": "pending"}
    )
    repair = await due(host, member_id)
    assert await execute(host, repair)
    await host.session.refresh(payment)
    assert payment.status == "pending"
    # Auto-renewal itself cannot be silently switched off by a repair.
    state = await AccessAdapter(host.service).read(host.session, USER_ID)
    assert state is not None
    await subscription_dal.set_auto_renew(host.session, state.subscription_id, True)
    repair = await due(host, member_id)
    assert not await execute(host, repair)
    row = await host.session.get(Operation, repair)
    assert row is not None and row.error_code == "minishop_corp_renewal_policy_required"
    state = await AccessAdapter(host.service).read(host.session, USER_ID)
    assert state is not None and state.auto_renew_enabled


async def test_progress_and_errors_are_scoped_and_one_failure_does_not_undo_others(
    client: TestClient, host: Host
) -> None:
    draft = await invitation(host.session)
    service = Memberships(ContractHost(host.service))
    member_ids = []
    for user in (MEMBER, OTHER):
        result = await service.confirm(host.session, user, draft)
        assert result.operation is not None and await execute(host, result.operation.id)
        member_ids.append(result.operation.membership_id)
    contract_id = draft.offer.contract_id
    path = ADMIN_PATH + f"/{contract_id}/synchronization"
    manager_path = USER_PATH + f"/managed-contracts/{contract_id}/synchronization"
    sweep = await host.session.get(ReconciliationSweep, 1)
    assert sweep is not None
    sweep.next_run_at = datetime.now(UTC) + SWEEP_INTERVAL
    await host.session.flush()

    async def progress() -> dict[str, object]:
        reply = await client.get(path, headers=authorization(host))
        assert reply.status == 200, await reply.text()
        return (await reply.json())["synchronization"]

    initial = await progress()
    assert initial["current_members"] == initial["confirmed"] == 2
    await revise(
        host, contract_id, ends_at=datetime.now(UTC) + timedelta(days=10), manager_user_id=MANAGER
    )
    assert (await progress())["awaiting_dispatch"] == 2
    pending = [
        await schedule_member(host.session, member, now=datetime.now(UTC)) for member in member_ids
    ]
    assert None not in pending and (await progress())["pending"] == 2
    host.panel.fail_updates = True
    assert not await execute(host, pending[0])
    host.panel.fail_updates = False
    assert await execute(host, pending[1])
    result = await progress()
    assert result["retrying"] == result["confirmed"] == 1
    for suffix in ("", "/operations"):
        assert (
            await client.get(manager_path + suffix, headers=authorization(host, OTHER))
        ).status == 404
        assert (await client.get(path + suffix, headers=authorization(host, MANAGER))).status == 403
        reply = await client.get(manager_path + suffix, headers=authorization(host, MANAGER))
        assert reply.status == 200 and reply.headers["Cache-Control"] == "private, no-store"
    reply = await client.get(
        manager_path + "/operations?limit=1", headers=authorization(host, MANAGER)
    )
    rows = (await reply.json())["operations"]
    assert len(rows) == 1 and rows[0]["error_code"] == "minishop_corp_panel_unconfirmed"
    assert rows[0]["user_id"] == MEMBER and "target" not in rows[0]
    assert (
        await client.get(
            manager_path + "/operations?limit=101", headers=authorization(host, MANAGER)
        )
    ).status == 400
    before = await host.session.scalar(select(func.count()).select_from(Operation))
    assert (await client.head(manager_path, headers=authorization(host, MANAGER))).status == 200
    assert await host.session.scalar(select(func.count()).select_from(Operation)) == before
    assert await execute(host, pending[0], due=True)
    assert (await progress())["confirmed"] == 2
    # Finishing the global traversal is not a claim that access is already applied.
    sweep.next_run_at = datetime.now(UTC)
    await host.session.flush()
    now = datetime.now(UTC)
    lease = await claim_sweep(host.session, now=now)
    assert lease is not None
    running = await progress()
    assert running["sweep_running"] is True and running["awaiting_dispatch"] == 2
    repairs = []
    for member_id in member_ids:
        identifier = await schedule_member(host.session, member_id, now=now, sweep=lease)
        assert identifier is not None
        repairs.append(identifier)
    assert await finish_sweep_batch(host.session, lease, None, now=now)
    queued = await progress()
    assert queued["sweep_running"] is False and queued["sweep_completed_at"] is not None
    assert queued["confirmed"] == 0 and queued["pending"] == 2
    for identifier in repairs:
        assert await execute(host, identifier)
    assert (await progress())["confirmed"] == 2
