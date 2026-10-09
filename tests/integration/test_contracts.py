import asyncio
from collections.abc import AsyncIterator
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from bot.app.web.admin_api_impl.auth import admin_auth_middleware
from bot.app.web.admin_api_impl.users_actions import admin_user_delete_route
from bot.app.web.context import SESSION_FACTORY, SETTINGS, SUBSCRIPTION_SERVICE
from bot.app.web.webapp.assets import _csrf_protection_middleware
from bot.app.web.webapp_auth import create_webapp_session_token
from bot.plugins.spec import WEB_SCOPE_WEBAPP, PluginContext
from bot.services.account_roles import grant_role, revoke_role
from db.dal import user_dal
from db.models import Subscription
from minishop_corp import plugin
from minishop_corp.contracts import Contracts
from minishop_corp.contracts_types import ContractError, CreateContract, UpdateContract
from minishop_corp.integration.contracts import ContractHost
from minishop_corp.storage.schema import AuditEvent, Contract, Membership, Revision
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from .conftest import EXTERNAL_SQUAD, USER_ID, Host

pytestmark = pytest.mark.asyncio(loop_scope="session")
MANAGER = USER_ID + 1
OTHER = USER_ID + 2
MEMBER = USER_ID + 3
ADMIN_PATH = "/api/admin/minishop-corp/contracts"
USER_PATH = "/api/plugins/minishop-corp"


def draft(*, manager: int = MANAGER) -> dict[str, object]:
    return {
        "id": str(uuid4()),
        "name": "Contract fixture",
        "tariff_key": "corp",
        "external_squad_uuid": EXTERNAL_SQUAD,
        "ends_at": "2030-10-01T15:42:23.123+03:00",
        "manager_user_id": manager,
    }


def authorization(host: Host, user_id: int = USER_ID) -> dict[str, str]:
    return {"Authorization": "Bearer " + create_webapp_session_token(host.settings, user_id)}


@pytest_asyncio.fixture(loop_scope="session")
async def client(host: Host) -> AsyncIterator[TestClient]:
    for user_id in (MANAGER, OTHER, MEMBER):
        await user_dal.create_user(host.session, {"user_id": user_id})
    await grant_role(host.session, USER_ID, "admin", source="corp_test")
    app = web.Application(middlewares=[_csrf_protection_middleware, admin_auth_middleware])
    app[SETTINGS] = host.settings
    app[SESSION_FACTORY] = async_sessionmaker(
        bind=host.session.bind, expire_on_commit=False, join_transaction_mode="create_savepoint"
    )
    app[SUBSCRIPTION_SERVICE] = host.service
    plugin.setup_web(PluginContext(host.settings), app, scope=WEB_SCOPE_WEBAPP)
    app.router.add_delete("/api/admin/users/{user_id}", admin_user_delete_route)
    async with TestClient(TestServer(app)) as result:
        yield result


async def test_create_repeat_update_and_version_history(client: TestClient, host: Host) -> None:
    payload = draft()
    headers = authorization(host)
    created = await client.post(ADMIN_PATH, json=payload, headers=headers)
    assert created.status == 201, await created.text()
    row = (await created.json())["contract"]
    assert row["ends_at"] == "2030-10-01T12:42:23.123000Z"
    assert row["version"] == 1 and row["manager_user_id"] == MANAGER
    repeated = await client.post(ADMIN_PATH, json=payload, headers=headers)
    assert repeated.status == 200
    assert (await repeated.json())["contract"] == row
    url = ADMIN_PATH + "/" + str(payload["id"])
    update = {key: value for key, value in payload.items() if key != "id"}
    update.update(name="Updated", expected_version=1, ends_at="2020-01-01T00:00:00Z")
    changed = await client.put(url, json=update, headers=headers)
    assert changed.status == 200, await changed.text()
    assert (await changed.json())["contract"]["expired"] is True
    assert (await client.put(url, json=update, headers=headers)).status == 409
    assert (await client.post(ADMIN_PATH, json=payload, headers=headers)).status == 409
    # Manager assignment does not join a contract or provision a subscription.
    assert await host.session.scalar(select(func.count()).select_from(Membership)) == 0
    assert await host.session.scalar(select(func.count()).select_from(Subscription)) == 0
    identifier = UUID(str(payload["id"]))
    assert (
        await host.session.scalars(
            select(Revision.version)
            .where(Revision.contract_id == identifier)
            .order_by(Revision.version)
        )
    ).all() == [1, 2]
    assert (
        await host.session.scalar(
            select(func.count()).select_from(AuditEvent).where(AuditEvent.contract_id == identifier)
        )
        == 2
    )
    assert not any(method != "GET" for method, _ in host.panel.requests)


async def test_roles_are_scoped_independent_and_revoked_immediately(
    client: TestClient, host: Host
) -> None:
    first, second = draft(), draft(manager=OTHER)
    for payload in (first, second):
        assert (
            await client.post(ADMIN_PATH, json=payload, headers=authorization(host))
        ).status == 201
    managed = USER_PATH + "/managed-contracts/"
    normal = USER_PATH + "/contracts/"
    headers = authorization(host, MANAGER)
    rows = await client.get(USER_PATH + "/managed-contracts", headers=headers)
    assert [row["id"] for row in (await rows.json())["contracts"]] == [first["id"]]
    assert (await client.get(managed + str(first["id"]), headers=headers)).status == 200
    for suffix in (second["id"], str(uuid4())):
        response = await client.get(managed + str(suffix), headers=headers)
        assert response.status == 404
        assert (await response.json())["error"] == "minishop_corp_contract_missing"
    assert (await client.get(ADMIN_PATH, headers=headers)).status == 403
    assert (
        await client.put(ADMIN_PATH + "/" + str(first["id"]), json={}, headers=headers)
    ).status == 403
    assert (await client.get(normal + str(first["id"]), headers=headers)).status == 404
    host.session.add(
        Membership(contract_id=UUID(str(first["id"])), user_id=MEMBER, current_user_id=MEMBER)
    )
    await host.session.flush()
    assert (
        await client.get(normal + str(first["id"]), headers=authorization(host, MEMBER))
    ).status == 200
    assert (
        await client.get(normal + str(second["id"]), headers=authorization(host, MEMBER))
    ).status == 404
    assert (
        await client.get(managed + str(first["id"]), headers=authorization(host, MEMBER))
    ).status == 404
    replacement = {key: value for key, value in first.items() if key != "id"}
    replacement.update(expected_version=1, manager_user_id=OTHER)
    response = await client.put(
        ADMIN_PATH + "/" + str(first["id"]), json=replacement, headers=authorization(host)
    )
    assert response.status == 200, await response.text()
    assert (await client.get(managed + str(first["id"]), headers=headers)).status == 404
    assert (
        await client.get(managed + str(first["id"]), headers=authorization(host, OTHER))
    ).status == 200
    await revoke_role(host.session, USER_ID, "admin", actor_user_id=USER_ID)
    assert (await client.get(ADMIN_PATH, headers=authorization(host))).status == 403


@pytest.mark.parametrize(
    "change",
    [
        {"tariff_key": "unknown"},
        {"external_squad_uuid": str(uuid4())},
        {"external_squad_uuid": "invalid"},
        {"manager_user_id": 999999},
        {"manager_user_id": True},
        {"ends_at": "2030-01-01T00:00:00"},
        {"name": "  "},
        {"unexpected": "field"},
    ],
)
async def test_reject_invalid_input_without_partial_rows(
    client: TestClient, host: Host, change: dict[str, object]
) -> None:
    payload = {**draft(), **change}
    response = await client.post(ADMIN_PATH, json=payload, headers=authorization(host))
    assert response.status in {400, 422}, await response.text()
    assert (await response.json())["ok"] is False
    assert await host.session.get(Contract, UUID(str(payload["id"]))) is None


async def test_panel_failure_and_cache_use_native_service(client: TestClient, host: Host) -> None:
    host.panel.fail_squads = True
    payload = draft()
    failed = await client.post(ADMIN_PATH, json=payload, headers=authorization(host))
    assert failed.status == 422
    assert await host.session.get(Contract, UUID(str(payload["id"]))) is None
    host.panel.fail_squads = False
    assert (await client.post(ADMIN_PATH, json=payload, headers=authorization(host))).status == 201
    requests = host.panel.requests.count(("GET", "external-squads/" + EXTERNAL_SQUAD))
    assert requests >= 2  # An unavailable value must not become a successful cached lookup.
    # The host cache refresh/hit policy remains authoritative.
    assert (await client.post(ADMIN_PATH, json=draft(), headers=authorization(host))).status == 201


async def test_real_cookie_csrf_and_anonymous_boundary(client: TestClient, host: Host) -> None:
    payload = draft()
    assert (await client.post(ADMIN_PATH, json=payload)).status == 401
    assert (await client.get(USER_PATH + "/managed-contracts")).status == 401
    cookie = "rw_webapp_session=" + create_webapp_session_token(host.settings, USER_ID)
    cookie += "; rw_webapp_csrf=synthetic-csrf"
    origin = str(client.make_url("/")).rstrip("/")
    for headers in (
        {"Cookie": cookie, "Origin": origin},
        {"Cookie": cookie, "Origin": "https://foreign.invalid", "X-CSRF-Token": "synthetic-csrf"},
        {"Cookie": cookie, "Origin": origin, "X-CSRF-Token": "wrong"},
    ):
        response = await client.post(ADMIN_PATH, json=payload, headers=headers)
        assert response.status == 403
        assert (await response.json())["error"] == "csrf_failed"
    valid = {"Cookie": cookie, "Origin": origin, "X-CSRF-Token": "synthetic-csrf"}
    assert (await client.post(ADMIN_PATH, json=payload, headers=valid)).status == 201
    assert (await client.get(ADMIN_PATH, headers=authorization(host, 999999))).status == 403
    await user_dal.update_user(host.session, MANAGER, {"is_banned": True})
    assert (
        await client.get(USER_PATH + "/managed-contracts", headers=authorization(host, MANAGER))
    ).status == 403


async def test_pagination_and_malformed_bodies(client: TestClient, host: Host) -> None:
    headers = authorization(host)
    identifiers = []
    for _ in range(3):
        payload = draft()
        identifiers.append(payload["id"])
        assert (await client.post(ADMIN_PATH, json=payload, headers=headers)).status == 201
    first = await client.get(ADMIN_PATH + "?limit=2", headers=headers)
    rows = (await first.json())["contracts"]
    assert len(rows) == 2
    next_page = await client.get(ADMIN_PATH + "?limit=2&after=" + rows[-1]["id"], headers=headers)
    assert [row["id"] for row in rows + (await next_page.json())["contracts"]] == sorted(
        identifiers
    )
    assert (await client.get(ADMIN_PATH + "?limit=1001", headers=headers)).status == 400
    assert (await client.get(ADMIN_PATH + "/bad-id", headers=headers)).status == 400
    for data, extra, status in (
        ("{}", {}, 400),
        ("[", {"Content-Type": "application/json"}, 400),
        (" " * 17000, {"Content-Type": "application/json"}, 413),
    ):
        assert (
            await client.post(ADMIN_PATH, data=data, headers={**headers, **extra})
        ).status == status


async def test_implicit_head_routes_never_write(client: TestClient, host: Host) -> None:
    payload = draft()
    headers = authorization(host)
    assert (await client.head(ADMIN_PATH, json=payload, headers=headers)).status == 200
    assert await host.session.get(Contract, UUID(str(payload["id"]))) is None
    assert (await client.post(ADMIN_PATH, json=payload, headers=headers)).status == 201
    url = ADMIN_PATH + "/" + str(payload["id"])
    update = {key: value for key, value in payload.items() if key != "id"}
    update.update(expected_version=1, name="Must not be saved")
    assert (await client.head(url, json=update, headers=headers)).status == 200
    row = (await (await client.get(url, headers=headers)).json())["contract"]
    assert row["name"] == payload["name"] and row["version"] == 1


async def test_concurrent_update_does_not_overwrite_newer_terms(
    engine: AsyncEngine, host: Host
) -> None:
    factory = async_sessionmaker(engine, expire_on_commit=False)
    actor = 940001
    service = Contracts(ContractHost(host.service))
    # Do not use the host fixture's uncommitted users for separate-connection races.
    async with factory.begin() as session:
        await user_dal.create_user(session, {"user_id": actor})
        await grant_role(session, actor, "admin", source="corp_race")
        created, _ = await service.create(
            session, actor, CreateContract.model_validate(draft(manager=actor))
        )
    gate = asyncio.Barrier(2)

    async def update(name: str) -> int:
        try:
            async with factory.begin() as session:
                await gate.wait()
                terms = created.model_dump(
                    include={
                        "name",
                        "tariff_key",
                        "external_squad_uuid",
                        "ends_at",
                        "manager_user_id",
                    }
                )
                terms.update(name=name, expected_version=1)
                await service.update(
                    session, actor, created.id, UpdateContract.model_validate(terms)
                )
            return 200
        except ContractError as exc:
            return exc.status

    assert sorted(await asyncio.gather(update("First"), update("Second"))) == [200, 409]
    async with factory() as session:
        row = await service.admin_get(session, actor, created.id)
        assert row.version == 2
        assert row.name in {"First", "Second"}


async def test_concurrent_create_retry_is_one_contract(engine: AsyncEngine, host: Host) -> None:
    factory = async_sessionmaker(engine, expire_on_commit=False)
    actor = 940002
    service = Contracts(ContractHost(host.service))
    async with factory.begin() as session:
        await user_dal.create_user(session, {"user_id": actor})
        await grant_role(session, actor, "admin", source="corp_race")
    gate = asyncio.Barrier(2)
    payload = CreateContract.model_validate(draft(manager=actor))

    async def create() -> bool:
        async with factory.begin() as session:
            await gate.wait()
            _, created = await service.create(session, actor, payload)
            return created

    assert sorted(await asyncio.gather(create(), create())) == [False, True]
    async with factory() as session:
        assert (
            await session.scalar(
                select(func.count())
                .select_from(AuditEvent)
                .where(AuditEvent.contract_id == payload.id)
            )
            == 1
        )


async def test_native_delete_manager_keeps_an_editable_contract(
    client: TestClient, host: Host
) -> None:
    payload = draft()
    assert (await client.post(ADMIN_PATH, json=payload, headers=authorization(host))).status == 201
    await host.service.extend_active_subscription_days(
        host.session, MANAGER, 5, reason="admin", tariff_key="personal"
    )
    assert len(host.panel.users) == 1
    response = await client.delete(f"/api/admin/users/{MANAGER}", headers=authorization(host))
    assert response.status == 200, await response.text()
    assert not host.panel.users
    assert await user_dal.get_user_by_id(host.session, MANAGER) is None
    row = await host.session.get(Contract, UUID(str(payload["id"])))
    assert row is not None and row.manager_user_id is None and row.version == 1
    url = ADMIN_PATH + "/" + str(payload["id"])
    response = await client.get(url, headers=authorization(host))
    assert response.status == 200
    assert (await response.json())["contract"]["manager_user_id"] is None
    update = {key: value for key, value in payload.items() if key != "id"}
    update.update(name="Without a manager", manager_user_id=None, expected_version=1)
    response = await client.put(url, json=update, headers=authorization(host))
    assert response.status == 200, await response.text()
    assert (await response.json())["contract"]["version"] == 2
    update.update(manager_user_id=OTHER, expected_version=2)
    response = await client.put(url, json=update, headers=authorization(host))
    assert response.status == 200, await response.text()
    assert (await response.json())["contract"]["manager_user_id"] == OTHER
    response = await client.post(
        ADMIN_PATH, json={**draft(), "manager_user_id": None}, headers=authorization(host)
    )
    assert response.status == 400
