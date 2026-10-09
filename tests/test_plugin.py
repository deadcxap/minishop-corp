import json
from importlib.metadata import entry_points
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from bot.app.web.context import SETTINGS
from bot.plugins.spec import WEB_SCOPE_WEBAPP, WEB_SCOPE_WEBHOOKS, Plugin, PluginContext
from config.settings import Settings
from minishop_corp import plugin


@pytest.fixture
def context() -> PluginContext:
    settings = Settings(
        _env_file=None,
        POSTGRES_USER="corp_test",
        POSTGRES_PASSWORD="local-tests-only",
        TELEGRAM_ENABLED=False,
        WEBAPP_SESSION_SECRET="local-test-session-secret",
        TELEMETRY_ENABLED=False,
    )
    return PluginContext(settings=settings)


def test_installed_entry_point_and_worker_hooks(context: PluginContext) -> None:
    points = entry_points(group="minishop.plugins", name="minishop-corp")
    assert len(points) == 1
    loaded = next(iter(points)).load()
    assert isinstance(loaded, Plugin)
    assert loaded is plugin
    assert [task.name for task in plugin.worker_tasks(context)] == [
        "minishop-corp.operations",
        "minishop-corp.reconciliation",
    ]
    assert [item.id for item in plugin.migrations()] == [
        "minishop-corp.0001_initial",
        "minishop-corp.0002_invitation_replacements",
        "minishop-corp.0003_membership_reconciliation",
        "minishop-corp.0004_shared_reconciliation_sweep",
        "minishop-corp.0005_account_deletion",
        "minishop-corp.0006_optional_external_squad",
    ]


def test_locales_have_identical_keys_and_no_empty_values() -> None:
    directory = plugin.locales_dir()
    dictionaries = [
        json.loads((directory / f"{language}.json").read_text()) for language in ("ru", "en")
    ]
    assert dictionaries[0].keys() == dictionaries[1].keys()
    assert all(isinstance(value, str) and value for data in dictionaries for value in data.values())
    assert Path(directory).is_dir()


def test_routes_only_in_webapp(context: PluginContext) -> None:
    app = web.Application()
    plugin.setup_web(context, app, scope=WEB_SCOPE_WEBHOOKS)
    assert not list(app.router.routes())
    plugin.setup_web(context, app, scope=WEB_SCOPE_WEBAPP)
    assert {route.resource.canonical for route in app.router.routes()} == {
        "/api/plugins/minishop-corp/status",
        "/api/admin/minishop-corp/status",
        "/api/admin/minishop-corp/options/{kind}",
        "/api/admin/minishop-corp/contracts",
        "/api/admin/minishop-corp/contracts/{contract_id}",
        "/api/plugins/minishop-corp/managed-contracts",
        "/api/plugins/minishop-corp/managed-contracts/{contract_id}",
        "/api/plugins/minishop-corp/contracts/{contract_id}",
        "/api/admin/minishop-corp/contracts/{contract_id}/members",
        "/api/admin/minishop-corp/contracts/{contract_id}/members/{membership_id}/avatar",
        "/api/plugins/minishop-corp/managed-contracts/{contract_id}/members",
        "/api/plugins/minishop-corp/managed-contracts/{contract_id}/members/{membership_id}/avatar",
        "/api/plugins/minishop-corp/invitations/preview",
        "/api/admin/minishop-corp/contracts/{contract_id}/synchronization",
        "/api/admin/minishop-corp/contracts/{contract_id}/synchronization/operations",
        "/api/plugins/minishop-corp/managed-contracts/{contract_id}/synchronization",
        "/api/plugins/minishop-corp/managed-contracts/{contract_id}/synchronization/operations",
        "/api/plugins/minishop-corp/membership",
        "/api/plugins/minishop-corp/membership/confirm",
        "/api/plugins/minishop-corp/memberships/{membership_id}/leave",
        "/api/plugins/minishop-corp/operations/{operation_id}",
        "/api/admin/minishop-corp/contracts/{contract_id}/members/{membership_id}/exclude",
        "/api/admin/minishop-corp/contracts/{contract_id}/operations/{operation_id}",
        "/api/plugins/minishop-corp/managed-contracts/{contract_id}/members/{membership_id}/exclude",
        "/api/plugins/minishop-corp/managed-contracts/{contract_id}/operations/{operation_id}",
        "/api/admin/minishop-corp/contracts/{contract_id}/invitations",
        "/api/admin/minishop-corp/contracts/{contract_id}/invitations/{invitation_id}/rotate",
        "/api/admin/minishop-corp/contracts/{contract_id}/invitations/{invitation_id}/revoke",
        "/api/plugins/minishop-corp/managed-contracts/{contract_id}/invitations",
        "/api/plugins/minishop-corp/managed-contracts/{contract_id}/invitations/{invitation_id}/rotate",
    }


@pytest.mark.parametrize("prefix", ["/api/plugins/minishop-corp", "/api/admin/minishop-corp"])
async def test_anonymous_request_rejected(context: PluginContext, prefix: str) -> None:
    app = web.Application()
    app[SETTINGS] = context.settings
    plugin.setup_web(context, app, scope=WEB_SCOPE_WEBAPP)
    async with TestClient(TestServer(app)) as client:
        response = await client.get(f"{prefix}/status")
        assert response.status == 401
        assert await response.json() == {"ok": False, "error": "unauthorized"}
