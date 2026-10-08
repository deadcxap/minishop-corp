"""A single durable global sweep, leased one batch at a time across worker processes."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .schema import Membership, ReconciliationSweep

SWEEP_INTERVAL = timedelta(hours=6)
SWEEP_LEASE = timedelta(minutes=2)
BATCH_SIZE = 100


@dataclass(frozen=True)
class SweepLease:
    run_id: UUID
    token: UUID
    started_at: datetime
    after_member_id: UUID | None


async def claim_sweep(session: AsyncSession, *, now: datetime) -> SweepLease | None:
    row = await session.scalar(
        select(ReconciliationSweep)
        .where(ReconciliationSweep.id == 1)
        .execution_options(populate_existing=True)
        .with_for_update(skip_locked=True)
    )
    if row is None or (row.lease_until is not None and row.lease_until > now):
        return None
    if not row.is_running:
        if row.next_run_at > now:
            return None
        row.run_id = uuid4()
        row.started_at = now
        row.after_member_id = None
        row.is_running = True
        # Coalesce missed intervals to one current sweep after downtime.
        row.next_run_at = now + SWEEP_INTERVAL
    if row.started_at is None:
        raise RuntimeError("A running sweep requires a start time")
    row.lease_token = uuid4()
    row.lease_until = now + SWEEP_LEASE
    await session.flush()
    return SweepLease(row.run_id, row.lease_token, row.started_at, row.after_member_id)


async def sweep_candidates(
    session: AsyncSession,
    lease: SweepLease,
    *,
    limit: int = BATCH_SIZE,
    from_start: bool = False,
) -> list[UUID]:
    if not 1 <= limit <= BATCH_SIZE:
        raise ValueError("Invalid reconciliation batch size")
    query = (
        select(Membership.id)
        .where(
            Membership.state == "active",
            Membership.current_user_id.is_not(None),
            Membership.created_at <= lease.started_at,
            Membership.scheduled_run_id.is_distinct_from(lease.run_id),
        )
        .order_by(Membership.id)
        .limit(limit)
    )
    if not from_start and lease.after_member_id is not None:
        query = query.where(Membership.id > lease.after_member_id)
    return list((await session.scalars(query)).all())


async def lock_sweep(session: AsyncSession, lease: SweepLease) -> ReconciliationSweep | None:
    row = await session.get(ReconciliationSweep, 1, populate_existing=True, with_for_update=True)
    if (
        row is None
        or not row.is_running
        or row.run_id != lease.run_id
        or row.lease_token != lease.token
    ):
        return None
    return row


async def finish_sweep_batch(
    session: AsyncSession,
    lease: SweepLease,
    after: UUID | None,
    *,
    now: datetime,
) -> bool:
    row = await lock_sweep(session, lease)
    if row is None:
        return False
    row.after_member_id = after
    if after is None and not await sweep_candidates(session, lease, limit=1, from_start=True):
        row.is_running = False
        row.completed_at = now
    # A skipped busy account stays unmarked. At end of traversal the cursor wraps
    # and the same run revisits it; other users were already queued independently.
    row.lease_token = None
    row.lease_until = None
    await session.flush()
    return True
