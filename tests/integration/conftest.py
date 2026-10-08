import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import asyncpg
import pytest_asyncio
from aiohttp.test_utils import TestServer
from bot.services.panel_api_service import PanelApiService
from bot.services.subscription_service import SubscriptionService
from config.settings import Settings
from db import database_setup
from db.dal import user_dal
from db.migrator.engine import run_migration_chains
from minishop_corp import plugin
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from .panel import Panel

CORP_SQUAD = "10000000-0000-4000-8000-000000000001"
TRIAL_SQUAD = "10000000-0000-4000-8000-000000000002"
PERSONAL_SQUAD = "10000000-0000-4000-8000-000000000003"
EXTERNAL_SQUAD = "20000000-0000-4000-8000-000000000001"
USER_ID = 920001


def make_settings(**overrides: object) -> Settings:
    return Settings(
        _env_file=None,
        POSTGRES_HOST="postgres",
        POSTGRES_USER="corp_dev",
        POSTGRES_PASSWORD="local-development-only",
        TELEGRAM_ENABLED=False,
        TELEMETRY_ENABLED=False,
        PLUGINS_ENABLED=False,
        REDIS_URL=None,
        WEBAPP_THEMES_DIR=".local/themes",
        **overrides,
    )


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def engine() -> AsyncIterator[AsyncEngine]:
    # Database lifecycle is test infrastructure; schema and contents use native DAL/migrations.
    name = "corp_test_" + uuid4().hex
    admin = await asyncpg.connect(
        host="postgres", user="corp_dev", password="local-development-only", database="corp_dev"
    )
    await admin.execute(f'CREATE DATABASE "{name}"')
    settings = make_settings(POSTGRES_DB=name)
    factory = database_setup.init_db_connection(settings)
    try:
        await database_setup.init_db(settings, factory)
        assert database_setup.async_engine is not None
        # The host runner must roll back both DDL and revision tracking together.
        async with database_setup.async_engine.connect() as connection:
            transaction = await connection.begin()
            await connection.run_sync(
                lambda sync: run_migration_chains(sync, {plugin.name: plugin.migrations()})
            )
            await transaction.rollback()
            assert (
                await connection.scalar(text("SELECT to_regclass('ext_minishop_corp_contracts')"))
                is None
            )
            await connection.rollback()
        async with database_setup.async_engine.begin() as connection:
            for _ in range(2):
                await connection.run_sync(
                    lambda sync: run_migration_chains(sync, {plugin.name: plugin.migrations()})
                )
        yield database_setup.async_engine
    finally:
        if database_setup.async_engine is not None:
            await database_setup.async_engine.dispose()
        await admin.execute(f'DROP DATABASE "{name}" WITH (FORCE)')
        await admin.close()


@dataclass
class Host:
    session: AsyncSession
    service: SubscriptionService
    panel: Panel
    settings: Settings


@pytest_asyncio.fixture(loop_scope="session")
async def host(engine: AsyncEngine, tmp_path: Path) -> AsyncIterator[Host]:
    tariffs_path = tmp_path / "tariffs.json"
    tariffs_path.write_text(
        json.dumps(
            {
                "version": 1,
                "default_tariff": "personal",
                "tariffs": [
                    {
                        "key": key,
                        "name": {"ru": key, "en": key},
                        "billing_model": "period",
                        "enabled": key != "corp",
                        "access_code": "0" * 32 if key == "corp" else None,
                        "squad_uuids": [squad],
                        "monthly_gb": gb,
                        "hwid_device_limit": devices,
                        "prices_rub": {"1": 100},
                        "enabled_periods": [1],
                    }
                    for key, squad, gb, devices in [
                        ("personal", PERSONAL_SQUAD, 5, 2),
                        ("corp", CORP_SQUAD, 20, 5),
                    ]
                ],
            }
        )
    )
    panel = Panel()
    async with TestServer(panel.app) as server:
        settings = make_settings(
            PANEL_API_URL=str(server.make_url("/api")),
            PANEL_API_KEY="local-test-key",
            PANEL_WRITE_MODE="live",
            PANEL_USER_CACHE_TTL_SECONDS=0,
            TARIFFS_CONFIG_PATH=str(tariffs_path),
            TRIAL_ENABLED=True,
            TRIAL_DURATION_DAYS=3,
            TRIAL_TRAFFIC_LIMIT_GB=1,
            TRIAL_HWID_DEVICE_LIMIT=1,
            TRIAL_SQUAD_UUIDS=TRIAL_SQUAD,
        )
        panel_service = PanelApiService(settings)
        service = SubscriptionService(settings, panel_service)
        try:
            async with engine.connect() as connection, connection.begin():
                async with AsyncSession(
                    bind=connection,
                    expire_on_commit=False,
                    join_transaction_mode="create_savepoint",
                ) as session:
                    await user_dal.create_user(
                        session, {"user_id": USER_ID, "first_name": "Fixture"}
                    )
                    yield Host(session, service, panel, settings)
                # Native worker commits release savepoints, never this test's outer transaction.
                await connection.rollback()
        finally:
            await panel_service.close()
