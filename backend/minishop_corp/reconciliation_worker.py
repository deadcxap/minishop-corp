"""One global six-hour sweep plus prompt dispatch of changed contract revisions."""

import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from .diagnostics import event, failure
from .storage.reconciliation import candidates, schedule_member
from .storage.sweeps import SweepLease, claim_sweep, finish_sweep_batch, sweep_candidates

logger = logging.getLogger(__name__)


async def dispatch_members(
    sessions: Callable[[], AsyncSession],
    members: list[UUID],
    *,
    sweep: SweepLease | None = None,
) -> None:
    for member_id in members:
        try:
            async with sessions() as session, session.begin():
                await schedule_member(session, member_id, now=datetime.now(UTC), sweep=sweep)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Only this member rolls back. A later traversal retries the unmarked row.
            event(
                "reconciliation_dispatch_failure",
                level=logging.ERROR,
                membership_id=str(member_id),
                **failure(exc),
            )


async def dispatch_sweep_batch(sessions: Callable[[], AsyncSession]) -> bool:
    async with sessions() as session, session.begin():
        lease = await claim_sweep(session, now=datetime.now(UTC))
    if lease is None:
        return False
    async with sessions() as session:
        batch = await sweep_candidates(session, lease)
    await dispatch_members(sessions, batch, sweep=lease)
    async with sessions() as session, session.begin():
        await finish_sweep_batch(
            session, lease, batch[-1] if batch else None, now=datetime.now(UTC)
        )
    event("reconciliation_batch", run_id=str(lease.run_id), members=len(batch))
    return bool(batch)


async def run_reconciliation(sessions: Callable[[], AsyncSession]) -> None:
    logger.info("Corporate reconciliation scheduler started (one global six-hour sweep)")
    after: UUID | None = None
    while True:
        try:
            async with sessions() as session:
                revisions = await candidates(session, after=after)
            await dispatch_members(sessions, revisions)
            after = revisions[-1] if revisions else None
            sweeping = await dispatch_sweep_batch(sessions)
            if not revisions and not sweeping:
                await asyncio.sleep(2)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            event("reconciliation_failure", level=logging.ERROR, **failure(exc))
            await asyncio.sleep(5)
