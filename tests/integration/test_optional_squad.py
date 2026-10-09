from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from aiohttp.test_utils import TestClient
from bot.services.account_roles import grant_role
from db.dal import user_panel_squad_override_dal as squad_dal
from minishop_corp.integration.access import AccessAdapter
from minishop_corp.integration.access_types import PeriodAccess
from minishop_corp.integration.migrations import migrations
from minishop_corp.storage.reconciliation import schedule_member
from minishop_corp.storage.schema import Contract, Revision
from sqlalchemy import text

from .conftest import EXTERNAL_SQUAD, USER_ID, Host
from .test_access import seed_personal, target
from .test_contracts import ADMIN_PATH, authorization, draft
from .test_contracts import client as client
from .test_memberships import execute, joined
from .test_reconciliation import revise
from .test_storage import seed_contract

pytestmark = pytest.mark.asyncio(loop_scope="session")


@pytest.mark.parametrize("omitted", [True, False])
async def test_contract_accepts_missing_or_null_squad_without_panel_lookup(
    client: TestClient, host: Host, omitted: bool
) -> None:
    payload = draft()
    if omitted:
        payload.pop("external_squad_uuid")
    else:
        payload["external_squad_uuid"] = None
    host.panel.fail_squads = True
    response = await client.post(ADMIN_PATH, json=payload, headers=authorization(host))
    assert response.status == 201, await response.text()
    result = (await response.json())["contract"]
    assert result["external_squad_uuid"] is None
    assert not host.panel.requests
    assert (await client.post(ADMIN_PATH, json=payload, headers=authorization(host))).status == 200
    revision = await host.session.get(Revision, (UUID(result["id"]), 1))
    assert revision is not None and revision.external_squad_uuid is None


@pytest.mark.parametrize("before", ["new", "paid"])
@pytest.mark.parametrize("default", [None, EXTERNAL_SQUAD])
async def test_absent_squad_inherits_native_default_and_repeats(
    host: Host, before: str, default: str | None
) -> None:
    host.settings.USER_EXTERNAL_SQUAD_UUID = default
    await seed_personal(host, before)
    adapter = AccessAdapter(host.service)
    desired = PeriodAccess("corp", target().ends_at)
    for _ in range(2):
        state = await adapter.assign_period(host.session, USER_ID, desired)
        assert host.panel.users[state.panel_user_uuid].get("externalSquadUuid") == default
        assert state.end_date == desired.ends_at
        assert (
            await squad_dal.get_active_external_override(
                host.session, user_id=USER_ID, panel_user_uuid=state.panel_user_uuid
            )
            is None
        )


@pytest.mark.parametrize("default", [None, "20000000-0000-4000-8000-000000000099"])
async def test_revision_removes_corporate_squad_through_durable_worker(
    host: Host, default: str | None
) -> None:
    await grant_role(host.session, USER_ID, "admin", source="corp_test")
    accepted, member_id = await joined(host)
    host.settings.USER_EXTERNAL_SQUAD_UUID = default
    await revise(host, accepted.offer.contract_id, external_squad_uuid=None)
    operation_id = await schedule_member(host.session, member_id, now=datetime.now(UTC))
    assert operation_id is not None
    assert await execute(host, operation_id)
    adapter = AccessAdapter(host.service)
    state = await adapter.read(host.session, USER_ID)
    assert state is not None
    assert host.panel.users[state.panel_user_uuid]["externalSquadUuid"] == default
    assert (
        await squad_dal.get_active_external_override(
            host.session, user_id=USER_ID, panel_user_uuid=state.panel_user_uuid
        )
        is None
    )
    await adapter.assign_period(host.session, USER_ID, PeriodAccess("corp", state.end_date))
    assert host.panel.users[state.panel_user_uuid]["externalSquadUuid"] == default


async def test_absent_corporate_squad_selects_native_inheritance(host: Host) -> None:
    await seed_personal(host, "paid")
    adapter = AccessAdapter(host.service)
    state = await adapter.read(host.session, USER_ID)
    assert state is not None
    await squad_dal.set_external_override(
        host.session,
        user_id=USER_ID,
        panel_user_uuid=state.panel_user_uuid,
        mode="set",
        squad_uuid=EXTERNAL_SQUAD,
        source="admin",
    )
    await adapter.assign_period(host.session, USER_ID, PeriodAccess("corp", target().ends_at))
    assert host.panel.users[state.panel_user_uuid]["externalSquadUuid"] is None
    record = await squad_dal.get_active_external_override(
        host.session, user_id=USER_ID, panel_user_uuid=state.panel_user_uuid
    )
    assert record is None


async def test_upgrade_populated_0005_preserves_history_and_allows_null(host: Host) -> None:
    schema = "ext_minishop_corp_upgrade_" + uuid4().hex
    async with host.session.begin_nested():
        await host.session.execute(text(f'CREATE SCHEMA "{schema}"'))
        await host.session.execute(text(f'SET LOCAL search_path TO "{schema}", public'))
        connection = await host.session.connection()
        for migration in migrations()[:5]:
            await connection.run_sync(migration.upgrade)
        row = await seed_contract(host.session)
        identifier = row.id
        await host.session.execute(
            text("SET CONSTRAINTS ext_minishop_corp_current_revision IMMEDIATE")
        )
        savepoint = await host.session.begin_nested()
        connection = await host.session.connection()
        await connection.run_sync(migrations()[5].upgrade)
        await savepoint.rollback()
        nullable = await host.session.scalar(
            text("""
            SELECT is_nullable FROM information_schema.columns
            WHERE table_schema = :schema AND table_name = 'ext_minishop_corp_contracts'
                AND column_name = 'external_squad_uuid'
        """),
            {"schema": schema},
        )
        assert nullable == "NO"
        await connection.run_sync(migrations()[5].upgrade)
        host.session.expire_all()
        saved = await host.session.get(Contract, identifier)
        history = await host.session.get(Revision, (identifier, 1))
        assert saved is not None and history is not None
        assert saved.external_squad_uuid == history.external_squad_uuid == UUID(EXTERNAL_SQUAD)
        saved.external_squad_uuid = None
        history.external_squad_uuid = None
        await host.session.flush()
