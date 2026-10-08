"""A real, killable worker process for the restart acceptance test."""

import asyncio
import json
import sys
from datetime import UTC, datetime, timedelta
from uuid import UUID

from bot.services.panel_api_service import PanelApiService
from bot.services.subscription_service import SubscriptionService
from config.settings import Settings
from db.database_setup import init_db_connection
from minishop_corp.integration.contracts import ContractHost
from minishop_corp.operation_worker import OperationWorker


async def main() -> None:
    inputs = json.loads(sys.stdin.readline())
    settings = Settings(_env_file=None, **inputs["settings"])
    factory = init_db_connection(settings)
    panel = PanelApiService(settings)
    try:
        worker = OperationWorker(ContractHost(SubscriptionService(settings, panel)))
        async with factory.begin() as session:
            lease = await worker.claim(
                session,
                operation_id=UUID(inputs["operation"]),
                now=datetime.now(UTC) + timedelta(minutes=3) if inputs["deliver"] else None,
            )
            assert lease is not None
        print("CLAIMED", flush=True)
        if not inputs["deliver"]:
            await asyncio.Event().wait()  # The parent kills this process after its durable claim.
        async with factory.begin() as session:
            assert await worker.execute(session, lease)
        print("DELIVERED", flush=True)
    finally:
        await panel.close()


if __name__ == "__main__":
    asyncio.run(main())
