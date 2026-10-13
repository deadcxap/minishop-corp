import asyncio
from uuid import uuid4

import pytest
from aiohttp.test_utils import TestClient
from bot.app.web.webapp_auth import create_webapp_session_token
from bot.services.account_roles import grant_role
from db.dal import user_dal
from minishop_corp.contracts import Contracts
from minishop_corp.contracts_types import ContractError, UpdateContract
from minishop_corp.integration.contracts import ContractHost
from minishop_corp.integration.migrations import migrations
from minishop_corp.invitations import Invitations
from minishop_corp.invitations_types import CreateInvitation, UpdateInvitation
from minishop_corp.storage.invitations import reserve_invitation
from minishop_corp.storage.schema import Invitation
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from .conftest import USER_ID, Host
from .test_contracts import ADMIN_PATH, MANAGER, OTHER, USER_PATH, authorization
from .test_contracts import client as client
from .test_invitations import create_api_contract, pending_join
from .test_storage import seed_contract

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def test_manager_creates_multiple_codes_and_edits_limits_scoped(
    client: TestClient, host: Host
) -> None:
    identifier = await create_api_contract(client, host)
    foreign = await create_api_contract(client, host)
    path = f"{USER_PATH}/managed-contracts/{identifier}/invitations"
    headers = authorization(host, MANAGER)
    rows = []
    for kind, limit in (("single", 1), ("reusable", 5), ("reusable", 8)):
        payload = {"id": str(uuid4()), "kind": kind, "use_limit": limit}
        assert (await client.post(path, json=payload)).status == 401
        assert (
            await client.post(path, json=payload, headers=authorization(host, OTHER))
        ).status == 404
        reply = await client.post(path, json=payload, headers=headers)
        assert reply.status == 201, await reply.text()
        issued = (await reply.json())["invitation"]
        rows.append(issued)
        retry = await client.post(path, json=payload, headers=headers)
        assert retry.status == 200 and (await retry.json())["invitation"]["code"] == issued["code"]
    listed = (await (await client.get(path, headers=headers)).json())["invitations"]
    assert len(listed) == 3
    row = rows[1]
    change = {"expected_version": 1, "use_limit": 9}
    url = f"{path}/{row['id']}"
    assert (await client.put(url, json=change, headers=authorization(host, OTHER))).status == 404
    assert (
        await client.put(
            f"{USER_PATH}/managed-contracts/{foreign}/invitations/{row['id']}",
            json=change,
            headers=headers,
        )
    ).status == 404
    cookie = (
        "rw_webapp_session="
        + create_webapp_session_token(host.settings, MANAGER)
        + "; rw_webapp_csrf=fixture-csrf"
    )
    for route, method, payload in (
        (path, client.post, {"id": str(uuid4()), "kind": "single", "use_limit": 1}),
        (url, client.put, change),
    ):
        assert (
            await method(
                route,
                json=payload,
                headers={"Cookie": cookie, "Origin": str(client.make_url("/")).rstrip("/")},
            )
        ).status == 403
    for _ in range(2):
        reply = await client.put(url, json=change, headers=headers)
        assert reply.status == 200, await reply.text()
        saved = (await reply.json())["invitation"]
        assert saved["use_limit"] == 9 and saved["version"] == 2 and saved["code"] == row["code"]
    assert (await client.put(url, json={**change, "use_limit": 10}, headers=headers)).status == 409
    retry_create = await client.post(
        path, json={"id": row["id"], "kind": "reusable", "use_limit": 5}, headers=headers
    )
    assert retry_create.status == 200
    assert (await retry_create.json())["invitation"]["code"] == row["code"]
    assert (
        await client.post(
            path, json={"id": row["id"], "kind": "reusable", "use_limit": 9}, headers=headers
        )
    ).status == 409
    single = await client.put(f"{path}/{rows[0]['id']}", json=change, headers=headers)
    assert single.status == 400
    assert (await client.delete(url, headers=headers)).status == 405
    assert (await client.post(url + "/revoke", json={}, headers=headers)).status == 404
    assert (
        await client.post(
            f"{ADMIN_PATH}/{identifier}/invitations",
            json={"id": str(uuid4()), "kind": "single", "use_limit": 1},
            headers=headers,
        )
    ).status == 403
    contracts = Contracts(ContractHost(host.service))
    current = await contracts.admin_get(host.session, USER_ID, identifier)
    terms = current.model_dump(include={"name", "tariff_key", "external_squad_uuid", "ends_at"})
    await contracts.update(
        host.session,
        USER_ID,
        identifier,
        UpdateContract(**terms, manager_user_id=None, expected_version=1),
    )
    assert (
        await client.put(url, json={"expected_version": 2, "use_limit": 10}, headers=headers)
    ).status == 404
    assert (
        await client.post(
            path, json={"id": str(uuid4()), "kind": "single", "use_limit": 1}, headers=headers
        )
    ).status == 404


async def test_limit_cannot_drop_below_used_and_reserved_and_edit_rolls_back(
    client: TestClient, host: Host
) -> None:
    identifier = await create_api_contract(client, host)
    service = Invitations(ContractHost(host.service))
    issued = await service.create(
        host.session,
        USER_ID,
        identifier,
        CreateInvitation(id=uuid4(), kind="reusable", use_limit=5),
    )
    row = await host.session.get(Invitation, issued.invitation.id)
    assert row is not None
    row.used_count, row.reserved_count = 2, 1
    await host.session.flush()
    for limit in (0, -1, 2**31, 2, True):
        reply = await client.put(
            f"{ADMIN_PATH}/{identifier}/invitations/{row.id}",
            headers=authorization(host),
            json={"expected_version": 1, "use_limit": limit},
        )
        assert reply.status in (400, 409)
    change = UpdateInvitation(expected_version=1, use_limit=3)
    with pytest.raises(RuntimeError):
        async with host.session.begin_nested():
            await service.update(host.session, USER_ID, identifier, row.id, change)
            raise RuntimeError("rollback")
    await host.session.refresh(row)
    assert row.version == 1 and row.use_limit == 5
    saved = await service.update(host.session, USER_ID, identifier, row.id, change)
    assert saved.used_count == 2 and saved.reserved_count == 1 and saved.use_limit == 3
    await service.revoke(host.session, USER_ID, identifier, row.id)
    with pytest.raises(ContractError, match="request_conflict"):
        await service.update(
            host.session,
            USER_ID,
            identifier,
            row.id,
            UpdateInvitation(expected_version=2, use_limit=6),
        )


async def test_limit_edit_races_with_reservation_and_another_edit(
    engine: AsyncEngine, host: Host
) -> None:
    actor, target = 985001, 985002
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    service = Invitations(ContractHost(host.service))
    async with factory.begin() as session:
        for user in (actor, target):
            await user_dal.create_user(session, {"user_id": user})
        await grant_role(session, actor, "admin", source="corp_test")
        contract = await seed_contract(session, actor)
        issued = await service.create(
            session, actor, contract.id, CreateInvitation(id=uuid4(), kind="reusable", use_limit=2)
        )
        row = await session.get(Invitation, issued.invitation.id)
        assert row is not None
        row.used_count = 1

    async def edit(limit: int) -> str:
        try:
            async with factory.begin() as session:
                await service.update(
                    session,
                    actor,
                    contract.id,
                    issued.invitation.id,
                    UpdateInvitation(expected_version=1, use_limit=limit),
                )
            return "edited"
        except ContractError as exc:
            return exc.code

    async def reserve() -> str:
        try:
            async with factory.begin() as session:
                operation = await pending_join(session, target, contract.id, issued.invitation.id)
                await reserve_invitation(
                    session,
                    user_id=target,
                    invitation_id=issued.invitation.id,
                    operation_id=operation.id,
                )
            return "reserved"
        except ContractError as exc:
            return exc.code

    results = await asyncio.gather(edit(1), reserve())
    assert results in (
        ["edited", "minishop_corp_invitation_unavailable"],
        ["minishop_corp_invitation_limit_too_low", "reserved"],
    )
    async with factory.begin() as session:
        row = await session.get(Invitation, issued.invitation.id)
        assert row is not None and row.used_count + row.reserved_count <= row.use_limit
        version = row.version

    async def competing(limit: int) -> int:
        try:
            async with factory.begin() as session:
                await service.update(
                    session,
                    actor,
                    contract.id,
                    issued.invitation.id,
                    UpdateInvitation(expected_version=version, use_limit=limit),
                )
            return 200
        except ContractError as exc:
            return exc.status

    assert sorted(await asyncio.gather(competing(7), competing(8))) == [200, 409]


async def test_upgrade_multiple_codes_preserves_old_code_and_rolls_back(host: Host) -> None:
    schema = "ext_minishop_corp_upgrade_" + uuid4().hex
    async with host.session.begin_nested():
        await host.session.execute(text(f'CREATE SCHEMA "{schema}"'))
        await host.session.execute(text(f'SET LOCAL search_path TO "{schema}", public'))
        connection = await host.session.connection()
        for migration in migrations()[:9]:
            await connection.run_sync(migration.upgrade)
        contract = await seed_contract(host.session)
        identifier = uuid4()
        await host.session.execute(
            text("""INSERT INTO ext_minishop_corp_invitations
            (id, contract_id, kind, code_digest, use_limit, used_count, created_by)
            VALUES (:id, :contract, 'reusable', :digest, 5, 2, :actor)"""),
            {"id": identifier, "contract": contract.id, "digest": "e" * 64, "actor": USER_ID},
        )
        savepoint = await host.session.begin_nested()
        connection = await host.session.connection()
        await connection.run_sync(migrations()[9].upgrade)
        await savepoint.rollback()
        assert (
            await host.session.scalar(text("SELECT to_regclass('ext_minishop_corp_reusable_code')"))
            is not None
        )
        for _ in range(2):
            await connection.run_sync(migrations()[9].upgrade)
        old = await host.session.get(Invitation, identifier)
        assert old is not None and old.version == 1 and old.used_count == 2 and old.use_limit == 5
        host.session.add(
            Invitation(
                contract_id=contract.id,
                kind="reusable",
                code_digest="f" * 64,
                use_limit=3,
                created_by=USER_ID,
            )
        )
        await host.session.flush()
