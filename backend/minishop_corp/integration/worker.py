"""Validate the legacy SDK session factory once at the integration boundary."""

from bot.plugins.spec import PluginContext
from sqlalchemy.ext.asyncio import AsyncSession

from ..operation_worker import OperationWorker
from .contracts import ContractHost


async def run_operations(context: PluginContext) -> None:
    factory = context.require_session_factory()

    def session() -> AsyncSession:
        value = factory()
        if not isinstance(value, AsyncSession):
            raise TypeError("Minishop must provide an AsyncSession factory")
        return value

    await OperationWorker(ContractHost(context.require_subscription_service())).run(session)
