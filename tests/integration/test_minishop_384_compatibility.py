"""Probe Minishop 3.8.4 using native services, PostgreSQL and HTTP panel responses."""

import time
from copy import deepcopy
from datetime import UTC, datetime
from importlib.util import find_spec
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from bot.app.web.admin_api_impl.auth import admin_auth_middleware
from bot.app.web.context import SESSION_FACTORY, SETTINGS, SUBSCRIPTION_SERVICE
from bot.app.web.webapp.assets import _csrf_protection_middleware
from bot.plugins.spec import WEB_SCOPE_WEBAPP, PluginContext
from bot.services.account_roles import grant_role
from config.tariffs_config import Tariff
from db.dal import payment_dal, subscription_dal
from db.dal import user_panel_squad_override_dal as squad_dal
from db.models import Payment
from minishop_corp import plugin
from minishop_corp.integration.access import AccessAdapter
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from .conftest import CORP_SQUAD, USER_ID, Host
from .test_access import assert_panel, seed_personal, target
from .test_contracts import authorization
from .test_storage import seed_contract

pytestmark = [
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        find_spec("bot.services.subscription_days_quotes") is None,
        reason="These compatibility probes require Minishop 3.8.4",
    ),
]


def enable_day_purchases(host: Host, key: str) -> None:
    host.settings.SUBSCRIPTION_DAYS_DEVICES_ENABLED = True
    host.settings.SUBSCRIPTION_DAYS_TRAFFIC_ENABLED = True
    host.settings.MY_DEVICES_SECTION_ENABLED = True
    config = host.settings.tariffs_config
    assert config is not None
    tariff = config.require_configured(key)
    updated = Tariff.model_validate(
        {
            **tariff.model_dump(),
            "topup_packages": {"rub": [{"gb": 1, "price": 10}]},
            "premium_squad_uuids": ["10000000-0000-4000-8000-000000000004"],
            "premium_monthly_gb": 10,
            "premium_topup_packages": {"rub": [{"gb": 1, "price": 10}]},
            "hwid_device_packages": {"rub": [{"count": 1, "price": 10}]},
        }
    )
    config.tariffs = [updated if item.key == key else item for item in config.tariffs]
    Path(host.settings.TARIFFS_CONFIG_PATH).write_text(config.model_dump_json())


async def days_quote(host: Host, mode: str) -> dict[str, object]:
    from bot.services.subscription_days_quotes import attach_days_quotes

    sub = await subscription_dal.get_active_subscription_by_user_id(host.session, USER_ID)
    assert sub is not None
    plan: dict[str, object] = {
        "sale_mode": mode,
        "tariff_key": sub.tariff_key,
        "currency": "RUB",
        "traffic_gb": 1,
        "device_count": 1,
    }
    await attach_days_quotes(host.session, host.service, sub, [plan])
    quote = plan["subscription_days"]
    assert isinstance(quote, dict)
    return quote


async def paid_history(host: Host, tariff: str) -> None:
    await payment_dal.create_payment_record(
        host.session,
        {
            "user_id": USER_ID,
            "amount": 100,
            "currency": "RUB",
            "status": "succeeded",
            "provider": "yookassa",
            "tariff_key": tariff,
            "sale_mode": f"subscription@{tariff}",
            "subscription_duration_days": 30,
        },
    )


@pytest.mark.parametrize("mode", ["hwid_devices", "topup", "premium_topup"])
@pytest.mark.parametrize("history", [False, True], ids=["new", "paid-history"])
async def test_corporate_days_cannot_fund_addons(host: Host, mode: str, history: bool) -> None:
    enable_day_purchases(host, "corp")
    if history:
        await paid_history(host, "corp")
    adapter = AccessAdapter(host.service)
    expected = target(30)
    before = await adapter.assign_period(host.session, USER_ID, expected)
    panel_before = deepcopy(host.panel.users[before.panel_user_uuid])
    quote = await days_quote(host, mode)
    assert quote["eligible"] is False
    # Corporate dates are absolute, with no paid period duration or spendable basis.
    assert quote["disabled_reason"] == "duration_required"
    assert quote["quote_token"] is None and quote["days_to_charge"] is None
    assert await adapter.read(host.session, USER_ID) == before
    assert before.provider == "admin" and before.end_date == expected.ends_at
    assert host.panel.users[before.panel_user_uuid] == panel_before


@pytest.mark.parametrize("mode", ["hwid_devices", "topup", "premium_topup"])
async def test_personal_day_quote_cannot_charge_after_corporate_join(host: Host, mode: str) -> None:
    from bot.services.subscription_days_purchase import purchase_with_days
    from bot.services.subscription_days_quotes import AddonSelection, SubscriptionDaysError

    enable_day_purchases(host, "personal")
    await seed_personal(host, "paid")
    sub = await subscription_dal.get_active_subscription_by_user_id(host.session, USER_ID)
    assert sub is not None
    # The shared fixture grants time administratively. Add the paid-period metadata
    # represented by this synthetic account's successful personal payment.
    await subscription_dal.update_subscription(
        host.session, sub.subscription_id, {"duration_days": 60, "duration_months": 2}
    )
    await paid_history(host, "personal")
    quote = await days_quote(host, mode)
    assert quote["eligible"] is True and isinstance(quote["quote_token"], str), quote
    adapter = AccessAdapter(host.service)
    expected = target(30)
    before = await adapter.assign_period(host.session, USER_ID, expected)
    with pytest.raises(SubscriptionDaysError) as error:
        await purchase_with_days(
            host.session,
            host.service,
            user_id=USER_ID,
            selection=AddonSelection.parse(mode, "personal", 1),
            token=quote["quote_token"],
            description="Synthetic add-on",
        )
    assert error.value.code in {"subscription_days_unavailable", "subscription_days_quote_stale"}
    assert await adapter.read(host.session, USER_ID) == before
    assert_panel(host, before.panel_user_uuid, expected)
    assert (
        await host.session.scalar(
            select(func.count()).select_from(Payment).where(Payment.user_id == USER_ID)
        )
        == 1
    )


@pytest.mark.parametrize("role", ["customer", "manager", "admin"])
async def test_automatic_maintenance_blocks_and_recovers_plugin_api(host: Host, role: str) -> None:
    from bot.app.web.webapp.maintenance import maintenance_middleware
    from bot.services.panel_maintenance import (
        panel_maintenance_identity,
        read_panel_maintenance_state,
    )
    from bot.services.panel_maintenance_worker import PanelMaintenanceWorker

    host.settings.MAINTENANCE_MODE_AUTO_ENABLED = True
    host.settings.MAINTENANCE_MODE_AUTO_DELAY_SECONDS = 60
    if role == "manager":
        await seed_contract(host.session)
    elif role == "admin":
        await grant_role(host.session, USER_ID, "admin", source="corp_384_fixture")
    await host.session.commit()
    factory = async_sessionmaker(
        bind=host.session.bind, expire_on_commit=False, join_transaction_mode="create_savepoint"
    )
    now = [time.time() - 61]
    worker = PanelMaintenanceWorker(
        host.settings, factory, host.service.panel_service, None, clock=lambda: now[0]
    )
    host.panel.fail_stats = True
    await worker.tick()
    now[0] = time.time()
    await worker.tick()
    state = await read_panel_maintenance_state(
        host.session, panel_maintenance_identity(host.settings)
    )
    assert state is not None and state.active
    app = web.Application(
        middlewares=[_csrf_protection_middleware, admin_auth_middleware, maintenance_middleware]
    )
    app[SETTINGS], app[SUBSCRIPTION_SERVICE], app[SESSION_FACTORY] = (
        host.settings,
        host.service,
        factory,
    )
    plugin.setup_web(PluginContext(host.settings), app, scope=WEB_SCOPE_WEBAPP)
    async with TestClient(TestServer(app)) as client:
        path = "/api/plugins/minishop-corp/status"
        response = await client.get(path, headers=authorization(host))
        assert response.status == (200 if role == "admin" else 503)
        if role != "admin":
            assert await response.json() == {"ok": False, "error": "maintenance_mode"}
            assert response.headers["Retry-After"] == "60"
        host.panel.fail_stats = False
        now[0] += 16
        await worker.tick()
        state = await read_panel_maintenance_state(
            host.session, panel_maintenance_identity(host.settings)
        )
        assert state is not None and not state.active
        response = await client.get(path, headers=authorization(host))
        assert response.status == 200 and (await response.json())["ok"] is True
    assert host.settings.MAINTENANCE_MODE_ENABLED is False


async def test_native_sync_drops_deleted_manual_squad_and_keeps_corporate_access(
    host: Host,
) -> None:
    adapter = AccessAdapter(host.service)
    expected = target()
    before = await adapter.assign_period(host.session, USER_ID, expected)
    removed = "10000000-0000-4000-8000-000000000099"
    override = await squad_dal.upsert_internal_override(
        host.session,
        user_id=USER_ID,
        panel_user_uuid=before.panel_user_uuid,
        squad_uuid=removed,
        source="panel",
    )
    host.panel.internal_squads = {CORP_SQUAD: {"uuid": CORP_SQUAD, "name": "Corporate"}}
    host.panel.users[before.panel_user_uuid]["activeInternalSquads"] = [CORP_SQUAD, removed]
    assert await host.service.sync_main_traffic_limit_to_panel(host.session, USER_ID)
    await host.session.refresh(override)
    assert not override.is_active
    assert_panel(host, before.panel_user_uuid, expected)
    assert host.panel.requests.count(("GET", "internal-squads")) >= 2


async def test_trial_device_details_keep_standard_limit_after_departure(host: Host) -> None:
    adapter = AccessAdapter(host.service)
    await adapter.assign_period(host.session, USER_ID, target())
    trial = adapter.prepare_trial(starts_at=datetime.now(UTC))
    after = await adapter.grant_trial(host.session, USER_ID, trial)
    details = await host.service.get_active_subscription_details(host.session, USER_ID)
    assert details is not None
    assert details["base_hwid_device_limit"] == details["max_devices"] == 1
    assert host.panel.users[after.panel_user_uuid]["hwidDeviceLimit"] == 1
