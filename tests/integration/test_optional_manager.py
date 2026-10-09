from uuid import UUID, uuid4

import pytest
from aiohttp.test_utils import TestClient
from minishop_corp.integration.migrations import migrations
from minishop_corp.storage.schema import AuditEvent, Contract, Membership, Revision
from sqlalchemy import select, text

from .conftest import USER_ID, Host
from .test_contracts import ADMIN_PATH, MANAGER, MEMBER, USER_PATH, authorization, draft
from .test_contracts import client as client
from .test_storage import seed_contract

pytestmark = pytest.mark.asyncio(loop_scope="session")


@pytest.mark.parametrize("omitted", [True, False])
async def test_create_without_manager_is_idempotent_and_admin_only(
    client: TestClient, host: Host, omitted: bool
) -> None:
    payload = draft(manager=None)
    payload["external_squad_uuid"] = None
    if omitted:
        payload.pop("manager_user_id")
    for status in (201, 200):
        response = await client.post(ADMIN_PATH, json=payload, headers=authorization(host))
        assert response.status == status, await response.text()
        assert (await response.json())["contract"]["manager_user_id"] is None
    for account in (USER_ID, MANAGER):
        response = await client.get(
            USER_PATH + "/managed-contracts", headers=authorization(host, account)
        )
        assert (await response.json())["contracts"] == []
    assert (await client.get(ADMIN_PATH, headers=authorization(host))).status == 200
    assert not host.panel.requests


async def test_assign_and_clear_manager_revokes_role_without_deleting_account(
    client: TestClient, host: Host
) -> None:
    payload = draft(manager=None)
    assert (await client.post(ADMIN_PATH, json=payload, headers=authorization(host))).status == 201
    identifier = UUID(str(payload["id"]))
    member = Membership(contract_id=identifier, user_id=MEMBER, current_user_id=MEMBER)
    host.session.add(member)
    await host.session.flush()
    member_id = member.id
    managed = USER_PATH + "/managed-contracts/" + str(identifier)
    update = {key: value for key, value in payload.items() if key != "id"}
    for version, manager in ((1, MANAGER), (2, None)):
        update.update(expected_version=version, manager_user_id=manager)
        response = await client.put(
            ADMIN_PATH + "/" + str(identifier), json=update, headers=authorization(host)
        )
        assert response.status == 200, await response.text()
        assert (await response.json())["contract"]["manager_user_id"] == manager
        assert (await client.get(managed, headers=authorization(host, MANAGER))).status == (
            200 if manager else 404
        )
    host.session.expire_all()
    current = await host.session.get(Membership, member_id)
    assert current is not None and current.current_user_id == MEMBER and current.state == "pending"
    actions = list(
        await host.session.scalars(
            select(AuditEvent.action)
            .where(AuditEvent.contract_id == identifier)
            .order_by(AuditEvent.id)
        )
    )
    assert actions == ["contract_created", "contract_updated", "contract_updated"]
    assert (await host.session.get(Revision, (identifier, 2))).manager_user_id == MANAGER
    assert (await host.session.get(Revision, (identifier, 3))).manager_user_id is None
    assert not any(method != "GET" for method, _ in host.panel.requests)


async def test_upgrade_0006_keeps_data_and_distinguishes_manual_clear(host: Host) -> None:
    schema = "ext_minishop_corp_upgrade_" + uuid4().hex
    async with host.session.begin_nested():
        await host.session.execute(text(f'CREATE SCHEMA "{schema}"'))
        await host.session.execute(text(f'SET LOCAL search_path TO "{schema}", public'))
        connection = await host.session.connection()
        for migration in migrations()[:6]:
            await connection.run_sync(migration.upgrade)
        row = await seed_contract(host.session)
        identifier = row.id
        await host.session.execute(
            text("SET CONSTRAINTS ext_minishop_corp_current_revision IMMEDIATE")
        )
        savepoint = await host.session.begin_nested()
        connection = await host.session.connection()
        await connection.run_sync(migrations()[6].upgrade)
        await savepoint.rollback()
        # Rolled-back migration must leave the original function in place.
        function = await host.session.scalar(
            text(
                "SELECT pg_get_functiondef("
                "'ext_minishop_corp_clear_deleted_manager()'::regprocedure)"
            )
        )
        assert "IF EXISTS" not in function
        await connection.run_sync(migrations()[6].upgrade)
        await connection.run_sync(migrations()[6].upgrade)
        row.manager_user_id = None
        await host.session.flush()
        assert not list(
            await host.session.scalars(
                select(AuditEvent).where(AuditEvent.contract_id == identifier)
            )
        )
        original = await host.session.get(Revision, (identifier, 1))
        assert original is not None and original.manager_user_id == USER_ID
        assert (await host.session.get(Contract, identifier)).manager_user_id is None
