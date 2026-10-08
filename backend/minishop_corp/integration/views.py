"""Native discovery policy; route authorization remains authoritative."""

from bot.plugins.extensions import UserContext
from db.dal.user_reads_dal import get_user_by_id
from sqlalchemy import select

from ..storage.schema import Contract


async def view_policy(context: UserContext, view_id: str) -> bool:
    user = await get_user_by_id(context.session, context.user_id)
    if user is None or user.is_banned:
        return False
    if view_id == "corporate-manager":
        return (
            await context.session.scalar(
                select(Contract.id).where(Contract.manager_user_id == context.user_id).limit(1)
            )
            is not None
        )
    return view_id in {"corporate-home", "corporate-card"}
