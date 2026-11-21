"""Protect already issued corporate access before the host starts its worker."""

from db.models import Subscription
from sqlalchemy import DateTime, func, select
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.util import await_only

from ..storage.schema import Membership, Operation
from .access import SOURCE
from .billing import open_renewal_cycle, record_grant_opt_out


async def _backfill(connection: Connection) -> None:
    corporate = (Subscription.provider == "admin") & (Subscription.tariff_binding_note == SOURCE)
    current_member = (
        select(Membership.id)
        .where(
            Membership.current_user_id == Subscription.user_id,
            Membership.state.in_(("active", "leaving")),
        )
        .exists()
    )
    departure = (
        select(Operation.id)
        .where(
            Operation.user_id == Subscription.user_id,
            Operation.kind.in_(("leave", "exclude")),
            Operation.state == "succeeded",
            Operation.target["kind"].astext == "trial",
            Operation.target["starts_at"].astext.cast(DateTime(timezone=True))
            == Subscription.start_date,
            Operation.target["ends_at"].astext.cast(DateTime(timezone=True))
            == Subscription.end_date,
        )
        .exists()
    )
    statement = (
        select(Subscription)
        .where(
            Subscription.is_active.is_(True),
            Subscription.end_date > func.now(),
            Subscription.auto_renew_enabled.is_(False),
            Subscription.auto_renew_consent_version == 0,
            ~open_renewal_cycle(Subscription.subscription_id),
            (corporate & current_member) | ((Subscription.provider == "trial") & departure),
        )
        .order_by(Subscription.subscription_id)
        .with_for_update(of=Subscription)
        .execution_options(yield_per=200)
    )
    # The host runs its synchronous migration hooks through AsyncConnection.run_sync.
    # The DAL uses an AsyncSession on that same connection, inside the migration savepoint.
    async with AsyncSession(join_transaction_mode="rollback_only") as session:
        session.sync_session.bind = connection
        subscriptions = await session.stream_scalars(statement)
        try:
            async for row in subscriptions:
                await record_grant_opt_out(session, row)
        finally:
            await subscriptions.close()


def upgrade(connection: Connection) -> None:
    await_only(_backfill(connection))
