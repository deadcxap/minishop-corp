"""Minishop 3.8.6 integration through its native admin HTTP routes."""

from datetime import UTC, datetime, timedelta
from importlib.util import find_spec

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from bot.app.web.admin_api_impl.auth import admin_auth_middleware
from bot.app.web.context import I18N, SESSION_FACTORY, SETTINGS
from bot.middlewares.i18n import JsonI18n
from bot.plugins.spec import PluginContext
from bot.services.account_roles import grant_role
from db.dal import user_dal
from minishop_corp import plugin
from minishop_corp.broadcasts import SelectBroadcastAudience, contract_target, select_audience
from minishop_corp.integration.access import AccessAdapter
from minishop_corp.integration.access_types import PeriodAccess

from .conftest import USER_ID, Host
from .test_broadcasts import add_member, services
from .test_contracts import authorization
from .test_minishop_385_compatibility import audience_service
from .test_minishop_385_compatibility import broadcast_client as broadcast_client
from .test_storage import seed_contract

pytestmark = [
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        find_spec("bot.app.web.admin_api_impl.users_extensions") is None,
        reason="These compatibility probes require Minishop 3.8.6",
    ),
]


@pytest.mark.parametrize("panel_available", [True, False])
async def test_cold_broadcast_catalog_keeps_corporate_and_platform_groups(
    broadcast_client: TestClient, host: Host, panel_available: bool
) -> None:
    service = audience_service(broadcast_client)
    service.native.panel_service = host.service.panel_service
    assert not service.catalog
    first = await seed_contract(host.session)
    second = await seed_contract(host.session)
    recipient = await add_member(host, first, 1, telegram_id=810001)
    state = await AccessAdapter(host.service).assign_period(
        host.session, recipient, PeriodAccess(first.tariff_key, first.ends_at)
    )
    host.panel.devices[state.panel_user_uuid] = [
        {"hwid": "synthetic-device", "platform": "Windows"}
    ]
    host.panel.fail_devices = not panel_available
    target = await select_audience(
        host.session, SelectBroadcastAudience(contract_ids=[first.id, second.id])
    )

    response = await broadcast_client.get(
        "/api/admin/broadcast/audience-counts", headers=authorization(host)
    )
    assert response.status == 200, await response.text()
    payload = await response.json()
    audiences = {row["target"]: row for row in payload["audiences"]}
    for corporate in (target, contract_target(first.id)):
        assert corporate in audiences and payload["counts"][corporate] == 1
    if panel_available:
        assert "platform:windows" in audiences and payload["counts"]["platform:windows"] == 1
        assert await service.resolve_targets([target, "platform:windows"]) == [recipient]
    else:
        assert audiences["device-platforms-unavailable"]["available"] is False


@pytest.mark.parametrize(
    ("language", "titles", "options", "yes", "no"),
    [
        (
            "ru",
            ("В корпконтракте", "Корпоративная подписка"),
            ("В контракте", "Вне контракта"),
            "Да",
            "Нет",
        ),
        (
            "en",
            ("In corporate contract", "Corporate subscription"),
            ("In a contract", "Outside a contract"),
            "Yes",
            "No",
        ),
    ],
)
async def test_native_users_table_filters_memberships_before_pagination(
    host: Host, language: str, titles: tuple[str, str], options: tuple[str, str], yes: str, no: str
) -> None:
    from bot.app.web.admin_api_impl.users_listing import admin_users_list_route
    from bot.plugins.extensions.registry import ExtensionRegistry, get_registry, set_registry

    host.settings.DEFAULT_LANGUAGE = language
    first = await seed_contract(host.session)
    expired = await seed_contract(host.session)
    first.name, expired.name = "Active contract", "Expired contract"
    expired.ends_at = datetime.now(UTC) - timedelta(days=1)
    active_user = await add_member(host, first, 1, first_name="Corp List Active")
    expired_user = await add_member(host, expired, 2, first_name="Corp List Expired")
    others = set()
    for offset, state in enumerate(("pending", "leaving", "excluded"), 3):
        others.add(await add_member(host, first, offset, state, first_name=f"Corp List {state}"))
    unrelated = USER_ID + 500
    await user_dal.create_user(
        host.session, {"user_id": unrelated, "first_name": "Corp List Other"}
    )
    others.add(unrelated)
    await grant_role(host.session, USER_ID, "admin", source="corp_users_fixture")
    await host.session.flush()

    sessions, _ = services(host)
    i18n = JsonI18n(str(plugin.locales_dir()), default=language)
    registry = ExtensionRegistry()
    registry.register(
        plugin.name, plugin.version, plugin.extensions(PluginContext(host.settings, i18n=i18n))
    )
    app = web.Application(middlewares=[admin_auth_middleware])
    app[SETTINGS], app[SESSION_FACTORY], app[I18N] = host.settings, sessions, i18n
    app.router.add_get("/api/admin/users", admin_users_list_route)
    previous = get_registry()
    set_registry(registry)
    try:
        async with TestClient(TestServer(app)) as client:
            unauthorized = await client.get(
                "/api/admin/users", headers=authorization(host, unrelated)
            )
            assert unauthorized.status == 403
            params = {"q": "Corp List"}
            headers = authorization(host)
            response = await client.get("/api/admin/users", headers=headers, params=params)
            assert response.status == 200, await response.text()
            payload = await response.json()
            member_key = "minishop-corp:corp-member"
            name_key = "minishop-corp:corp-subscription"
            filter_key = "minishop-corp:corp-membership"
            assert payload["extension_columns"] == [
                {"id": member_key, "label": titles[0]},
                {"id": name_key, "label": titles[1]},
            ]
            assert payload["extension_filters"][0]["id"] == filter_key
            assert payload["extension_filters"][0]["options"] == [
                {"value": "member", "label": options[0]},
                {"value": "nonmember", "label": options[1]},
            ]
            values = {row["user_id"]: row["extension_values"] for row in payload["users"]}
            assert values[active_user] == {member_key: yes, name_key: first.name}
            assert values[expired_user] == {member_key: yes, name_key: expired.name}
            assert all(
                values[identifier] == {member_key: no, name_key: None} for identifier in others
            )
            for option, expected in (
                ("member", {active_user, expired_user}),
                ("nonmember", others),
            ):
                found = set()
                for page in range(len(expected)):
                    response = await client.get(
                        "/api/admin/users",
                        headers=headers,
                        params={
                            **params,
                            "page": page,
                            "page_size": 1,
                            "extension_filter": f"{filter_key}={option}",
                        },
                    )
                    assert response.status == 200, await response.text()
                    data = await response.json()
                    assert data["total"] == len(expected) and len(data["users"]) == 1
                    found.add(data["users"][0]["user_id"])
                assert found == expected

            first.name = "Renamed contract"
            await host.session.flush()
            response = await client.get("/api/admin/users", headers=headers, params=params)
            assert response.status == 200, await response.text()
            rows = (await response.json())["users"]
            updated = next(row for row in rows if row["user_id"] == active_user)
            assert updated["extension_values"][name_key] == first.name
    finally:
        set_registry(previous)
