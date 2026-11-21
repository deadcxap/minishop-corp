"""Billing checks and the native opt-out for grants issued by the plugin.

The native DAL has no user-scoped query for all nonterminal payment/cycle states.
Those two bounded EXISTS reads use host ORM models here; mutations remain native.
The caller locks the native user before this check and until its transaction ends.
"""

from importlib.util import find_spec

from db.dal import (
    auto_renew_dal,
    payment_dal,
    rollypay_dal,
    subscription_dal,
    tribute_dal,
    wata_subscription_dal,
)
from db.models import AutoRenewCycle, Payment, Subscription
from sqlalchemy import ColumnExpressionArgument, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.selectable import Exists

from .access_types import AccessError, AccessFailure, AccessState


def open_renewal_cycle(subscription_id: ColumnExpressionArgument[int] | int) -> Exists:
    return (
        select(AutoRenewCycle.cycle_id)
        .where(
            AutoRenewCycle.subscription_id == subscription_id,
            AutoRenewCycle.state.in_(auto_renew_dal.OPEN_CYCLE_STATES),
        )
        .exists()
    )


async def record_grant_opt_out(session: AsyncSession, row: Subscription) -> None:
    """Record explicit disabled renewal on a locked corporate period or departure trial."""
    if row.auto_renew_consent_version != 0:
        return
    subscription_id = int(row.subscription_id)
    if row.auto_renew_enabled or await session.scalar(select(open_renewal_cycle(subscription_id))):
        raise AccessError(AccessFailure.RENEWAL_POLICY_REQUIRED)
    # A false flag with consent version zero still qualifies for funded grant renewal.
    if await subscription_dal.set_auto_renew(session, subscription_id, False) is None:
        raise AccessError(AccessFailure.SUBSCRIPTION_MISSING)


async def _has_live_provider_mandate(session: AsyncSession, user_id: int) -> bool:
    # Minishop 3.8.1 has no mandates. Only absence of this optional module is
    # tolerated; broken imports or failed queries must not allow enrollment.
    if find_spec("db.dal.provider_mandate_dal") is None:
        return False
    from db.dal.provider_mandate_dal import list_mandates

    # Native 3.8.2 provider keys, independent of which providers are enabled now:
    # disabling checkout does not revoke an existing provider-owned schedule.
    return bool(
        await list_mandates(
            session,
            providers=(
                "cryptomus_subscription",
                "cispay_subscription_card",
                "cispay_subscription_sbp",
            ),
            user_id=user_id,
            limit=1,
        )
    )


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
        or await _has_live_provider_mandate(session, user_id)
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
