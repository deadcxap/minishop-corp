"""Validate the legacy SDK session factory once at the integration boundary."""

from collections.abc import Callable

from bot.plugins.spec import PluginContext
from sqlalchemy.ext.asyncio import AsyncSession

from ..operation_worker import OperationWorker
from ..reconciliation_worker import run_reconciliation
from .contracts import ContractHost


def sessions_for(context: PluginContext) -> Callable[[], AsyncSession]:
    factory = context.require_session_factory()

    def session() -> AsyncSession:
        value = factory()
        if not isinstance(value, AsyncSession):
            raise TypeError("Minishop must provide an AsyncSession factory")
        return value

    return session


async def run_operations(context: PluginContext) -> None:
    await OperationWorker(ContractHost(context.require_subscription_service())).run(
        sessions_for(context)
    )


async def run_reconciliation_task(context: PluginContext) -> None:
    await run_reconciliation(sessions_for(context))
