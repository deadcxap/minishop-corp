"""Native account locking for nonblocking batches.

The pinned DAL exposes blocking lock_user_by_id only. This uses its same ORM,
FOR UPDATE and refresh semantics with SKIP LOCKED, without writing host tables.
Account/ban checks before entitlement writes still belong to OperationWorker.
"""

from db.models import User
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


async def try_lock_account(session: AsyncSession, user_id: int) -> bool:
    identifier = await session.scalar(
        select(User.user_id).where(User.user_id == user_id).with_for_update(skip_locked=True)
    )
    return identifier is not None
