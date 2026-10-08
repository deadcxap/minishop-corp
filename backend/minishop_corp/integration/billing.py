"""Read-only Q-03 gate. No cancellation, charge, payment mutation or provider client.

The native DAL has no user-scoped query for all nonterminal payment/cycle states.
Those two bounded EXISTS reads use host ORM models here; mutations remain native.
The caller locks the native user before this check and until its transaction ends.
"""

from db.dal import (
    auto_renew_dal,
    payment_dal,
    rollypay_dal,
    subscription_dal,
    tribute_dal,
    wata_subscription_dal,
)
from db.models import AutoRenewCycle, Payment
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .access_types import AccessError, AccessFailure, AccessState


async def require_clear_billing(session: AsyncSession, user_id: int) -> None:
    subscriptions = await subscription_dal.get_active_subscriptions_for_user(session, user_id)
    latest = await subscription_dal.get_latest_subscription_by_user_id(session, user_id)
    identifiers = {AccessState.model_validate(row).subscription_id for row in subscriptions}
    if latest is not None:
        identifiers.add(AccessState.model_validate(latest).subscription_id)
    for identifier in sorted(identifiers):
        row = await subscription_dal.get_subscription_by_id_for_update(session, identifier)
        if row is not None and AccessState.model_validate(row).auto_renew_enabled:
            raise AccessError(AccessFailure.RENEWAL_POLICY_REQUIRED)
    if (
        await rollypay_dal.list_live_for_user(session, user_id)
        or await wata_subscription_dal.list_live_for_user(session, user_id)
        or await tribute_dal.get_other_active_shop_order_uuid(session, user_id=user_id)
        or await tribute_dal.get_other_active_creator_subscription_id(session, user_id=user_id)
        or await session.scalar(
            select(
                select(AutoRenewCycle.cycle_id)
                .where(
                    AutoRenewCycle.user_id == user_id,
                    AutoRenewCycle.state.in_(auto_renew_dal.OPEN_CYCLE_STATES),
                )
                .exists()
            )
        )
    ):
        raise AccessError(AccessFailure.RENEWAL_POLICY_REQUIRED)
    # Pending finalization/review and unknown statuses stay blocked. Age alone is
    # not cancellation; only the native terminal state resolves the payment.
    if await session.scalar(
        select(
            select(Payment.payment_id)
            .where(
                Payment.user_id == user_id,
                func.lower(func.trim(func.coalesce(Payment.status, ""))).not_in(
                    payment_dal._PAYMENT_TERMINAL_STATUSES
                ),
            )
            .exists()
        )
    ):
        raise AccessError(AccessFailure.PAYMENT_PENDING)
