"""The six-hour clock belongs to one sweep, not to individual memberships."""

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from db.dal import user_dal
from minishop_corp.integration.migrations import migrations
from minishop_corp.reconciliation_worker import dispatch_members, dispatch_sweep_batch
from minishop_corp.storage.reconciliation import schedule_member
from minishop_corp.storage.schema import Membership, Operation, ReconciliationSweep
from minishop_corp.storage.sweeps import (
    BATCH_SIZE,
    SWEEP_INTERVAL,
    SWEEP_LEASE,
    SweepLease,
    claim_sweep,
    finish_sweep_batch,
    sweep_candidates,
)
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from .conftest import USER_ID, Host
from .test_storage import seed_contract

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def seed_members(session: AsyncSession, count: int) -> list[Membership]:
    # Synthetic existing members: transport and subscription semantics are covered
    # by test_reconciliation, while these tests exercise actual PostgreSQL scheduling.
    first = uuid4().int % 1_000_000_000 + 100_000_000
    for index in range(count):
        await user_dal.create_user(session, {"user_id": first + index})
    contracts = [await seed_contract(session, manager=first) for _ in range(2)]
    members = [
        Membership(
            contract_id=contracts[index % 2].id,
            user_id=first + index,
            current_user_id=first + index,
            state="active",
            applied_version=1,
            scheduled_version=1,
            joined_at=datetime.now(UTC),
        )
        for index in range(count)
    ]
    session.add_all(members)
    await session.flush()
    return sorted(members, key=lambda member: member.id)


async def test_global_batches_cover_contracts_and_exclude_former_or_new_members(host: Host) -> None:
    members = await seed_members(host.session, BATCH_SIZE + 5)
    now = datetime.now(UTC)
    members[-1].created_at = now + timedelta(seconds=1)
    members[-2].state = "left"
    members[-2].current_user_id = None
    members[-2].ended_at = now
    members[-3].state = "leaving"
    await host.session.flush()
    lease = await claim_sweep(host.session, now=now)
    assert lease is not None
    collected = []
    while lease is not None:
        batch = await sweep_candidates(host.session, lease)
        assert len(batch) <= BATCH_SIZE
        for member_id in batch:
            assert await schedule_member(host.session, member_id, now=now, sweep=lease)
        collected.extend(batch)
        assert await finish_sweep_batch(host.session, lease, batch[-1] if batch else None, now=now)
        lease = await claim_sweep(host.session, now=now)
    assert collected == [member.id for member in members[:-3]]
    sweep = await host.session.get(ReconciliationSweep, 1)
    assert sweep is not None and not sweep.is_running and sweep.completed_at == now
    assert sweep.next_run_at == now + SWEEP_INTERVAL
    assert (
        await claim_sweep(host.session, now=now + SWEEP_INTERVAL - timedelta(microseconds=1))
        is None
    )
    previous_run_id = sweep.run_id
    next_run = await claim_sweep(host.session, now=now + SWEEP_INTERVAL)
    assert next_run is not None and next_run.run_id != previous_run_id
    collected_again = []
    while next_run is not None:
        batch = await sweep_candidates(host.session, next_run)
        collected_again.extend(batch)
        for member_id in batch:
            assert await schedule_member(
                host.session, member_id, now=now + SWEEP_INTERVAL, sweep=next_run
            )
        assert await finish_sweep_batch(
            host.session, next_run, batch[-1] if batch else None, now=now + SWEEP_INTERVAL
        )
        next_run = await claim_sweep(host.session, now=now + SWEEP_INTERVAL)
    assert collected_again == collected + [members[-1].id]


@pytest.mark.parametrize("crash_after", ["claim", "intent", "batch"])
async def test_restart_preserves_global_run_and_fences_obsolete_scheduler(
    host: Host, crash_after: str
) -> None:
    members = await seed_members(host.session, 2)
    now = datetime.now(UTC)
    old = await claim_sweep(host.session, now=now)
    assert old is not None
    first = None
    if crash_after != "claim":
        first = await schedule_member(host.session, members[0].id, now=now, sweep=old)
        assert first is not None
    if crash_after == "batch":
        assert await finish_sweep_batch(host.session, old, members[0].id, now=now)
    else:
        assert (
            await claim_sweep(host.session, now=now + SWEEP_LEASE - timedelta(microseconds=1))
            is None
        )
    replacement = await claim_sweep(host.session, now=now + SWEEP_LEASE)
    assert replacement is not None and replacement.run_id == old.run_id
    assert replacement.token != old.token and replacement.started_at == now
    if crash_after == "batch":
        assert replacement.after_member_id == members[0].id
    assert await schedule_member(host.session, members[1].id, now=now, sweep=old) is None
    assert not await finish_sweep_batch(host.session, old, None, now=now)
    pending = await sweep_candidates(host.session, replacement)
    assert pending == [member.id for member in members[(0 if first is None else 1) :]]
    for member_id in pending:
        assert await schedule_member(host.session, member_id, now=now, sweep=replacement)
    assert await finish_sweep_batch(host.session, replacement, None, now=now + SWEEP_LEASE)
    count = await host.session.scalar(
        select(func.count())
        .select_from(Operation)
        .where(
            Operation.membership_id.in_([member.id for member in members]),
            Operation.kind == "reconcile",
        )
    )
    assert count == 2
    # Days of downtime coalesce into one new run, without replaying every missed interval.
    later = now + timedelta(days=9)
    overdue = await claim_sweep(host.session, now=later)
    assert overdue is not None and overdue.run_id != old.run_id
    for member_id in await sweep_candidates(host.session, overdue):
        assert await schedule_member(host.session, member_id, now=later, sweep=overdue)
    assert await finish_sweep_batch(host.session, overdue, None, now=later)
    sweep = await host.session.get(ReconciliationSweep, 1)
    assert sweep is not None and sweep.next_run_at == later + SWEEP_INTERVAL
    assert await claim_sweep(host.session, now=later) is None
    assert (
        await host.session.scalar(
            select(func.count())
            .select_from(Operation)
            .where(
                Operation.membership_id.in_([member.id for member in members]),
            )
        )
        == 2
    )  # Still-pending repairs were reused, not duplicated.


@pytest_asyncio.fixture(loop_scope="session")
async def committed_members(
    engine: AsyncEngine,
) -> AsyncIterator[tuple[async_sessionmaker[AsyncSession], list[Membership]]]:
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory.begin() as session:
        sweep = await session.get(ReconciliationSweep, 1, with_for_update=True)
        assert sweep is not None
        sweep.is_running = False
        sweep.lease_token = sweep.lease_until = None
        sweep.next_run_at = datetime.now(UTC)
        members = await seed_members(session, 3)
    try:
        yield factory, members
    finally:
        async with factory.begin() as session:
            for identifier in [member.id for member in members]:
                row = await session.get(Membership, identifier, with_for_update=True)
                assert row is not None
                row.current_user_id = None
                row.state = "left"
                row.ended_at = datetime.now(UTC)
            for operation in await session.scalars(
                select(Operation).where(Operation.membership_id.in_([m.id for m in members]))
            ):
                operation.state = "cancelled"
                operation.lease_token = operation.lease_until = None
            sweep = await session.get(ReconciliationSweep, 1, with_for_update=True)
            assert sweep is not None
            sweep.is_running = False
            sweep.lease_token = sweep.lease_until = None
            sweep.next_run_at = datetime.now(UTC)


async def test_two_workers_share_one_sweep_and_busy_account_is_revisited(
    committed_members: tuple[async_sessionmaker[AsyncSession], list[Membership]],
) -> None:
    factory, members = committed_members
    now = datetime.now(UTC)

    async def claim_one() -> SweepLease | None:
        async with factory.begin() as session:
            return await claim_sweep(session, now=now)

    claims = await asyncio.gather(claim_one(), claim_one())
    leases = [lease for lease in claims if lease is not None]
    assert len(leases) == 1
    lease = leases[0]
    async with factory.begin() as blocker:
        await user_dal.lock_user_by_id(blocker, members[0].user_id)
        await asyncio.wait_for(dispatch_members(factory, [m.id for m in members], sweep=lease), 5)
        async with factory.begin() as session:
            rows = list(
                await session.scalars(
                    select(Operation).where(
                        Operation.membership_id.in_([m.id for m in members]),
                    )
                )
            )
            assert {row.membership_id for row in rows} == {m.id for m in members[1:]}
            assert await finish_sweep_batch(session, lease, members[-1].id, now=now)
        # End of traversal wraps to the skipped member; no per-account deadline exists.
        assert not await dispatch_sweep_batch(factory)
    assert await dispatch_sweep_batch(factory)
    assert not await dispatch_sweep_batch(factory)
    async with factory() as session:
        sweep = await session.get(ReconciliationSweep, 1)
        assert sweep is not None and not sweep.is_running and sweep.run_id == lease.run_id
        assert (
            await session.scalar(
                select(func.count())
                .select_from(Operation)
                .where(
                    Operation.membership_id.in_([m.id for m in members]),
                )
            )
            == 3
        )


@pytest.mark.parametrize("previous", [2, 3])
async def test_upgrade_existing_member_to_shared_clock(host: Host, previous: int) -> None:
    # A separate schema inside the rollback-only host fixture models installed
    # versions without changing published migrations or any native table.
    schema = "ext_minishop_corp_upgrade_" + uuid4().hex
    async with host.session.begin_nested():
        await host.session.execute(text(f'CREATE SCHEMA "{schema}"'))
        await host.session.execute(text(f'SET LOCAL search_path TO "{schema}", public'))
        connection = await host.session.connection()
        for migration in migrations()[:previous]:
            await connection.run_sync(migration.upgrade)
        contract = await seed_contract(host.session)
        member_id = uuid4()
        await host.session.execute(
            text("""
            INSERT INTO ext_minishop_corp_memberships
            (id, contract_id, user_id, current_user_id, state, applied_version)
            VALUES (:id, :contract, :user, :user, 'active', 1)
        """),
            {"id": member_id, "contract": contract.id, "user": USER_ID},
        )
        if previous == 3:
            await host.session.execute(
                text("""
                UPDATE ext_minishop_corp_memberships
                SET scheduled_version = 1, next_reconcile_at = now() + interval '5 hours'
            """)
            )
        # Installed rows have no deferred events at the start of an upgrade.
        await host.session.execute(
            text("SET CONSTRAINTS ext_minishop_corp_current_revision IMMEDIATE")
        )
        await host.session.execute(
            text("SET CONSTRAINTS ext_minishop_corp_current_revision DEFERRED")
        )
        for migration in migrations()[previous:]:
            await connection.run_sync(migration.upgrade)
        member = await host.session.get(Membership, member_id)
        assert member is not None and member.state == "active" and member.applied_version == 1
        assert member.scheduled_run_id is None and member.last_reconciled_at is None
        lease = await claim_sweep(host.session, now=datetime.now(UTC))
        assert lease is not None
        assert await sweep_candidates(host.session, lease) == [member_id]
        assert await schedule_member(host.session, member_id, now=datetime.now(UTC), sweep=lease)


async def test_dispatch_rollback_is_local_to_one_member(
    committed_members: tuple[async_sessionmaker[AsyncSession], list[Membership]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from minishop_corp import reconciliation_worker

    factory, members = committed_members
    async with factory.begin() as session:
        lease = await claim_sweep(session, now=datetime.now(UTC))
    assert lease is not None

    async def interrupt_first(
        session: AsyncSession,
        member_id: UUID,
        *,
        now: datetime,
        sweep: SweepLease | None = None,
    ) -> UUID | None:
        result = await schedule_member(session, member_id, now=now, sweep=sweep)
        if member_id == members[0].id:
            raise RuntimeError("Synthetic interruption after flushing an intent")
        return result

    with monkeypatch.context() as patch:
        patch.setattr(reconciliation_worker, "schedule_member", interrupt_first)
        await dispatch_members(factory, [member.id for member in members], sweep=lease)
    async with factory() as session:
        assert await sweep_candidates(session, lease) == [members[0].id]
        assert (
            await session.scalar(
                select(func.count())
                .select_from(Operation)
                .where(Operation.membership_id.in_([member.id for member in members]))
            )
            == 2
        )
    await dispatch_members(factory, [member.id for member in members], sweep=lease)
    async with factory.begin() as session:
        assert await finish_sweep_batch(session, lease, None, now=datetime.now(UTC))
        assert (
            await session.scalar(
                select(func.count())
                .select_from(Operation)
                .where(Operation.membership_id.in_([member.id for member in members]))
            )
            == 3
        )


@pytest.mark.parametrize("action", ["leave", "revision"])
async def test_new_intent_during_native_sync_wins_after_inflight_effect(
    committed_members: tuple[async_sessionmaker[AsyncSession], list[Membership]],
    host: Host,
    action: str,
) -> None:
    from bot.services.account_roles import grant_role
    from minishop_corp.contracts import Contracts, terms_of
    from minishop_corp.contracts_types import UpdateContract
    from minishop_corp.integration.access import AccessAdapter
    from minishop_corp.integration.access_types import panel_time
    from minishop_corp.integration.contracts import ContractHost
    from minishop_corp.membership_types import DepartMembership
    from minishop_corp.memberships import Memberships
    from minishop_corp.operation_worker import OperationWorker
    from minishop_corp.storage.reconciliation import period_target
    from minishop_corp.storage.schema import Contract

    factory, members = committed_members
    member = members[0]
    access = AccessAdapter(host.service)
    contracts = Contracts(ContractHost(host.service))
    worker = OperationWorker(ContractHost(host.service))
    target_end = panel_time(datetime.now(UTC) + timedelta(days=4))
    async with factory.begin() as session:
        await grant_role(session, member.user_id, "admin", source="corp_sweep_race")
        contract = await session.get(Contract, member.contract_id)
        assert contract is not None
        state = await access.assign_period(
            session, member.user_id, period_target(contract).access()
        )
        update = UpdateContract.model_validate(
            {
                **terms_of(contract).model_dump(),
                "expected_version": 1,
                "ends_at": target_end,
            }
        )
        sweep = await claim_sweep(session, now=datetime.now(UTC))
        assert sweep is not None
        operation_id = await schedule_member(session, member.id, now=datetime.now(UTC), sweep=sweep)
        assert operation_id is not None
    async with factory.begin() as session:
        lease = await worker.claim(session, operation_id=operation_id)
    assert lease is not None
    host.panel.users[state.panel_user_uuid]["expireAt"] = target_end.isoformat()
    host.panel.update_started = asyncio.Event()
    host.panel.continue_update = asyncio.Event()

    async def effect() -> bool:
        async with factory.begin() as session:
            return await worker.execute(session, lease)

    async def change() -> UUID | None:
        async with factory.begin() as session:
            if action == "leave":
                result = await Memberships(ContractHost(host.service)).depart(
                    session, member.user_id, member.id, DepartMembership(request_id=uuid4())
                )
                return result.id
            await contracts.update(session, member.user_id, member.contract_id, update)
            return None

    effect_task = asyncio.create_task(effect())
    change_task = None
    try:
        await asyncio.wait_for(host.panel.update_started.wait(), 5)
        change_task = asyncio.create_task(change())
        done, _ = await asyncio.wait([change_task], timeout=0.1)
        assert not done  # The outer native-user/contract locks cover the entire HTTP write.
    finally:
        host.panel.continue_update.set()
        assert await asyncio.wait_for(effect_task, 5)
    assert change_task is not None
    next_operation = await asyncio.wait_for(change_task, 5)
    if action == "revision":
        async with factory.begin() as session:
            next_operation = await schedule_member(session, member.id, now=datetime.now(UTC))
    assert next_operation is not None
    async with factory.begin() as session:
        next_lease = await worker.claim(session, operation_id=next_operation)
    assert next_lease is not None
    async with factory.begin() as session:
        assert await worker.execute(session, next_lease)
    requests = list(host.panel.requests)
    async with factory.begin() as session:
        assert not await worker.execute(session, lease)
        assert host.panel.requests == requests
        result = await access.read(session, member.user_id)
        assert result is not None
        if action == "leave":
            assert result.provider == "trial"
            assert (
                await schedule_member(session, member.id, now=datetime.now(UTC), sweep=sweep)
                is None
            )
        else:
            assert result.end_date == target_end and result.provider == "admin"
            row = await session.get(Membership, member.id)
            assert row is not None and row.applied_version == row.scheduled_version == 2
