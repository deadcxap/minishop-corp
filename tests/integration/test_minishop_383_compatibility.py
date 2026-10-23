"""Minishop 3.8.3 probes, including the corporate-only captcha exemption.

Redis cases require CORP_COMPAT_REDIS_URL pointing at the local stand. Older Core
releases skip these probes; no Core service or captcha proof is replaced.
"""

import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from importlib.util import find_spec
from typing import Literal
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from bot.app.web.admin_api_impl.auth import admin_auth_middleware
from bot.app.web.context import SESSION_FACTORY, SETTINGS, SUBSCRIPTION_SERVICE
from bot.app.web.webapp.assets import _csrf_protection_middleware
from bot.infra.redis import close_redis
from bot.plugins.spec import WEB_SCOPE_WEBAPP, PluginContext
from bot.services.account_roles import grant_role
from db.dal import user_dal
from db.models import Payment, Subscription
from minishop_corp import plugin
from minishop_corp.integration.access import AccessAdapter
from minishop_corp.integration.access_types import TrialAccess
from minishop_corp.integration.contracts import ContractHost
from minishop_corp.membership_types import DepartMembership
from minishop_corp.memberships import Memberships
from minishop_corp.storage.schema import Contract, Invitation, Membership, Operation
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from .conftest import TRIAL_SQUAD, USER_ID, Host
from .test_access import target
from .test_contracts import authorization
from .test_memberships import execute, invitation
from .test_storage import seed_contract

pytestmark = [
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        find_spec("bot.services.trial_captcha") is None,
        reason="These compatibility probes require Minishop 3.8.3",
    ),
]


@pytest_asyncio.fixture(
    loop_scope="session", params=[False, True], ids=["without-redis", "with-redis"]
)
async def captcha(host: Host, request: pytest.FixtureRequest) -> AsyncIterator[str]:
    available = request.param
    assert isinstance(available, bool)
    if available:
        url = os.environ.get("CORP_COMPAT_REDIS_URL")
        if not url:
            pytest.skip("Set CORP_COMPAT_REDIS_URL to the isolated stand Redis")
        host.settings.REDIS_URL = url
    host.settings.REDIS_KEY_PREFIX = f"corp-383-{uuid4().hex}"
    host.settings.TRIAL_CAPTCHA_ENABLED = True
    await close_redis()
    try:
        yield "trial_captcha_required" if available else "trial_captcha_unavailable"
    finally:
        await close_redis()


async def departure(host: Host, scope: Literal["self", "manager", "admin"]) -> UUID:
    actor = USER_ID if scope == "self" else USER_ID + 1
    if actor != USER_ID:
        await user_dal.create_user(host.session, {"user_id": actor})
    if scope == "admin":
        await grant_role(host.session, actor, "admin", source="corp_383_fixture")
    draft = await invitation(host.session, manager=actor)
    service = Memberships(ContractHost(host.service))
    joined = await service.confirm(host.session, USER_ID, draft)
    assert joined.operation is not None and await execute(host, joined.operation.id)
    result = await service.depart(
        host.session,
        actor,
        joined.operation.membership_id,
        DepartMembership(request_id=uuid4()),
        scope=scope,
        contract_id=draft.offer.contract_id if scope != "self" else None,
    )
    return result.id


@pytest.mark.parametrize("scope", ["self", "manager", "admin"])
async def test_departure_completes_with_captcha_enabled(
    host: Host, captcha: str, scope: Literal["self", "manager", "admin"]
) -> None:
    operation_id = await departure(host, scope)
    result = await host.service.activate_trial_subscription(
        host.session, USER_ID, commit=False, emit_event=False
    )
    assert result is not None and not result["activated"] and result["message_key"] == captcha
    assert await execute(host, operation_id)
    operation = await host.session.get(Operation, operation_id)
    assert operation is not None and operation.state == "succeeded"
    member = await host.session.get(Membership, operation.membership_id)
    assert member is not None and member.current_user_id is None
    assert member.state == ("left" if scope == "self" else "excluded")
    adapter = AccessAdapter(host.service)
    state = await adapter.read(host.session, USER_ID)
    assert state is not None and state.provider == "trial" and state.tariff_key is None
    assert state.start_date == datetime.fromisoformat(str(operation.target["starts_at"]))
    assert state.end_date == datetime.fromisoformat(str(operation.target["ends_at"]))
    assert state.end_date - state.start_date == timedelta(days=3)
    assert state.traffic_limit_bytes == 1024**3 and state.hwid_device_limit == 1
    panel = host.panel.users[state.panel_user_uuid]
    assert panel["activeInternalSquads"] == [TRIAL_SQUAD]
    assert panel["externalSquadUuid"] is None and panel["tag"] == "TRIAL"
    assert panel["trafficLimitBytes"] == 1024**3 and panel["hwidDeviceLimit"] == 1
    # Retrying the same grant only verifies its fixed window; no second trial/payment.
    assert (
        await adapter.grant_trial(
            host.session, USER_ID, TrialAccess(state.start_date, state.end_date)
        )
        == state
    )
    assert (
        await host.session.scalar(
            select(func.count()).select_from(Subscription).where(Subscription.user_id == USER_ID)
        )
        == 1
    )
    assert (
        await host.session.scalar(
            select(func.count()).select_from(Payment).where(Payment.user_id == USER_ID)
        )
        == 0
    )
    assert host.settings.TRIAL_CAPTCHA_ENABLED is True
    # No shared setting or account proof was changed by the corporate exception.
    # Both the departing user and a fresh account still need the native captcha.
    other_user = USER_ID + 2
    await user_dal.create_user(host.session, {"user_id": other_user})
    for user_id in (USER_ID, other_user):
        result = await host.service.activate_trial_subscription(
            host.session, user_id, commit=False, emit_event=False
        )
        assert result is not None and not result["activated"]
        assert result["message_key"] == captcha


@pytest.mark.parametrize("failure", ["fail_trial_activation", "fail_after_trial_activation"])
async def test_departure_retry_keeps_trial_window_with_captcha_enabled(
    host: Host, captcha: str, failure: str
) -> None:
    operation_id = await departure(host, "self")
    operation = await host.session.get(Operation, operation_id)
    assert operation is not None
    # Simulate queue delay: the native now-based trial must need a correction
    # back to the persisted window, even if both calls run in one millisecond.
    saved_target = dict(operation.target)
    for key in ("starts_at", "ends_at"):
        saved_target[key] = (
            datetime.fromisoformat(str(saved_target[key])) - timedelta(hours=1)
        ).isoformat()
    operation.target = saved_target
    await host.session.flush()
    result = await host.service.activate_trial_subscription(
        host.session, USER_ID, commit=False, emit_event=False
    )
    assert result is not None and not result["activated"] and result["message_key"] == captcha
    setattr(host.panel, failure, True)
    assert not await execute(host, operation_id)
    operation = await host.session.get(Operation, operation_id)
    assert operation is not None and operation.state == "retry"
    assert operation.error_code == "minishop_corp_panel_unconfirmed"
    assert operation.target == saved_target
    member = await host.session.get(Membership, operation.membership_id)
    assert member is not None and member.state == "leaving"
    state = await AccessAdapter(host.service).read(host.session, USER_ID)
    assert state is not None and state.tariff_key == "corp"
    assert not await execute(host, operation_id, due=True)
    await host.session.refresh(operation)
    assert operation.target == saved_target and operation.state == "retry"
    setattr(host.panel, failure, False)
    assert await execute(host, operation_id, due=True)
    state = await AccessAdapter(host.service).read(host.session, USER_ID)
    assert state is not None and state.provider == "trial" and state.tariff_key is None
    assert state.start_date == datetime.fromisoformat(str(saved_target["starts_at"]))
    assert state.end_date == datetime.fromisoformat(str(saved_target["ends_at"]))
    await host.session.refresh(member)
    assert member.state == "left" and member.current_user_id is None
    assert host.settings.TRIAL_CAPTCHA_ENABLED is True
    assert (
        await host.session.scalar(
            select(func.count()).select_from(Subscription).where(Subscription.user_id == USER_ID)
        )
        == 1
    )
    assert (
        await host.session.scalar(
            select(func.count()).select_from(Payment).where(Payment.user_id == USER_ID)
        )
        == 0
    )


@pytest.mark.parametrize("unavailable", ["disabled", "zero-duration"])
async def test_departure_without_trial_does_not_require_captcha(
    host: Host, captcha: str, unavailable: str
) -> None:
    operation_id = await departure(host, "self")
    # Also cover settings changing after a trial target was already persisted.
    if unavailable == "disabled":
        host.settings.TRIAL_ENABLED = False
    else:
        host.settings.TRIAL_DURATION_DAYS = 0
    assert await execute(host, operation_id)
    state = await AccessAdapter(host.service).read(host.session, USER_ID)
    assert state is not None and not state.is_active and state.provider != "trial"
    assert host.panel.users[state.panel_user_uuid]["status"] == "EXPIRED"
    assert state.end_date <= datetime.now(UTC)
    operation = await host.session.get(Operation, operation_id)
    assert operation is not None and operation.state == "succeeded"
    member = await host.session.get(Membership, operation.membership_id)
    assert member is not None and member.state == "left" and member.current_user_id is None
    assert host.settings.TRIAL_CAPTCHA_ENABLED is True
    assert (
        await host.session.scalar(
            select(func.count()).select_from(Payment).where(Payment.user_id == USER_ID)
        )
        == 0
    )


async def test_cleaned_trial_history_allows_native_administrative_departure_reset(
    host: Host,
) -> None:
    from db.dal.account_cleanup_dal import has_cleaned_trial, remember_cleaned_trial

    await user_dal.update_user(host.session, USER_ID, {"telegram_id": USER_ID})
    await remember_cleaned_trial(host.session, USER_ID, datetime.now(UTC) - timedelta(days=90))
    operation_id = await departure(host, "self")
    assert await execute(host, operation_id)
    state = await AccessAdapter(host.service).read(host.session, USER_ID)
    assert state is not None and state.provider == "trial"
    assert await has_cleaned_trial(host.session, USER_ID)


@pytest.mark.parametrize("days", [-1, 30], ids=["expired", "active"])
async def test_cleanup_preserves_corporate_subscription(host: Host, days: int) -> None:
    from db.dal.account_cleanup_dal import still_cleanup_candidate

    host.settings.UNUSED_ACCOUNTS_NO_TRIAL_ENABLED = True
    host.settings.UNUSED_ACCOUNTS_UNUSED_TRIAL_ENABLED = True
    await user_dal.update_user(
        host.session,
        USER_ID,
        {"telegram_id": USER_ID, "registration_date": datetime.now(UTC) - timedelta(days=90)},
    )
    await AccessAdapter(host.service).assign_period(host.session, USER_ID, target(days))
    assert not await still_cleanup_candidate(
        host.session, host.settings, datetime.now(UTC), USER_ID
    )


@pytest.mark.parametrize("pending_join", [False, True], ids=["manager", "pending-member"])
async def test_native_cleanup_clears_corporate_links_without_trial(
    host: Host, pending_join: bool
) -> None:
    from bot.services.account_cleanup_worker import AccountCleanupWorker

    host.settings.UNUSED_ACCOUNTS_CLEANUP_ENABLED = True
    host.settings.UNUSED_ACCOUNTS_NO_TRIAL_ENABLED = True
    await user_dal.update_user(
        host.session,
        USER_ID,
        {"telegram_id": USER_ID, "registration_date": datetime.now(UTC) - timedelta(days=90)},
    )
    draft = await invitation(host.session)
    operation_id = None
    if pending_join:
        accepted = await Memberships(ContractHost(host.service)).confirm(
            host.session, USER_ID, draft
        )
        assert accepted.operation is not None
        operation_id = accepted.operation.id
    await host.session.commit()
    worker = AccountCleanupWorker(
        host.settings,
        async_sessionmaker(
            bind=host.session.bind, expire_on_commit=False, join_transaction_mode="create_savepoint"
        ),
        host.service.panel_service,
    )
    assert await worker.cleanup_user(USER_ID)
    host.session.expire_all()
    assert await user_dal.get_user_by_id(host.session, USER_ID) is None
    contract = await host.session.get(Contract, draft.offer.contract_id)
    assert contract is not None and contract.manager_user_id is None
    invite = await host.session.get(Invitation, draft.offer.invitation_id)
    assert invite is not None and invite.used_count == invite.reserved_count == 0
    if operation_id is not None:
        operation = await host.session.get(Operation, operation_id)
        assert operation is not None and operation.state == "cancelled"
        member = await host.session.get(Membership, operation.membership_id)
        assert member is not None and member.state == "deleted" and member.current_user_id is None
    assert host.panel.requests == []


@pytest.mark.parametrize("role", ["customer", "manager", "admin"])
async def test_maintenance_respects_global_admin_boundary(host: Host, role: str) -> None:
    from bot.app.web.webapp.maintenance import maintenance_middleware

    host.settings.MAINTENANCE_MODE_ENABLED = True
    if role == "manager":
        await seed_contract(host.session)
    if role == "admin":
        await grant_role(host.session, USER_ID, "admin", source="corp_383_fixture")
    app = web.Application(
        middlewares=[_csrf_protection_middleware, admin_auth_middleware, maintenance_middleware]
    )
    app[SETTINGS], app[SUBSCRIPTION_SERVICE] = host.settings, host.service
    app[SESSION_FACTORY] = async_sessionmaker(
        bind=host.session.bind, expire_on_commit=False, join_transaction_mode="create_savepoint"
    )
    plugin.setup_web(PluginContext(host.settings), app, scope=WEB_SCOPE_WEBAPP)
    async with TestClient(TestServer(app)) as client:
        response = await client.get(
            "/api/plugins/minishop-corp/status", headers=authorization(host)
        )
        assert response.status == (200 if role == "admin" else 503)
        if role != "admin":
            assert await response.json() == {"ok": False, "error": "maintenance_mode"}
            assert response.headers["Retry-After"] == "60"
