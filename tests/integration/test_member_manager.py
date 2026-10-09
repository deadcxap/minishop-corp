from datetime import UTC, datetime

import pytest
from aiohttp.test_utils import TestClient
from bot.app.web.webapp_auth import create_webapp_session_token
from minishop_corp.storage.schema import Membership

from .conftest import USER_ID, Host
from .test_contracts import ADMIN_PATH, MANAGER, MEMBER, USER_PATH, authorization
from .test_contracts import client as client
from .test_invitations import create_api_contract

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def test_manager_assignment_requires_admin_and_native_csrf(
    client: TestClient, host: Host
) -> None:
    contract_id = await create_api_contract(client, host)
    member = Membership(
        contract_id=contract_id, user_id=MEMBER, current_user_id=MEMBER, state="active"
    )
    host.session.add(member)
    await host.session.flush()
    path = f"{ADMIN_PATH}/{contract_id}/members/{member.id}/manager"
    payload = {"expected_version": 1}
    assert (await client.post(path, json=payload)).status == 401
    assert (
        await client.post(path, json=payload, headers=authorization(host, MANAGER))
    ).status == 403
    cookie = (
        "rw_webapp_session="
        + create_webapp_session_token(host.settings, USER_ID)
        + "; rw_webapp_csrf=fixture-csrf"
    )
    assert (
        await client.post(
            path,
            json=payload,
            headers={"Cookie": cookie, "Origin": str(client.make_url("/")).rstrip("/")},
        )
    ).status == 403


async def test_promote_only_current_member_and_revoke_old_manager(
    client: TestClient, host: Host
) -> None:
    contract_id = await create_api_contract(client, host)
    member = Membership(
        contract_id=contract_id, user_id=MEMBER, current_user_id=MEMBER, state="active"
    )
    host.session.add(member)
    await host.session.flush()
    path = f"{ADMIN_PATH}/{contract_id}/members/{member.id}/manager"
    foreign = await create_api_contract(client, host)
    assert (
        await client.post(
            f"{ADMIN_PATH}/{foreign}/members/{member.id}/manager",
            headers=authorization(host),
            json={"expected_version": 1},
        )
    ).status == 404
    before = (
        await (await client.get(f"{ADMIN_PATH}/{contract_id}", headers=authorization(host))).json()
    )["contract"]
    calls = list(host.panel.requests)
    for _ in range(2):
        result = await client.post(path, headers=authorization(host), json={"expected_version": 1})
        assert result.status == 200, await result.text()
        saved = (await result.json())["contract"]
        assert saved["manager_user_id"] == MEMBER and saved["version"] == 2
        for key in ("tariff_key", "ends_at", "external_squad_uuid", "member_count"):
            assert saved[key] == before[key]
    assert host.panel.requests == calls
    managed = f"{USER_PATH}/managed-contracts/{contract_id}"
    assert (await client.get(managed, headers=authorization(host, MANAGER))).status == 404
    assert (await client.get(managed, headers=authorization(host, MEMBER))).status == 200
    assert (await client.get(ADMIN_PATH, headers=authorization(host, MEMBER))).status == 403
    assert (
        await client.post(path, headers=authorization(host), json={"expected_version": 9})
    ).status == 409
    member = await host.session.get(Membership, member.id, populate_existing=True)
    assert member is not None
    member.state = "left"
    member.current_user_id = None
    member.ended_at = datetime.now(UTC)
    await host.session.flush()
    assert (
        await client.post(path, headers=authorization(host), json={"expected_version": 2})
    ).status == 404
