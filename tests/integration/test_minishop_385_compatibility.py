"""Minishop 3.8.5 compatibility probes using native HTTP routes and PostgreSQL."""

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from importlib.util import find_spec

import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from bot.app.web.admin_api_impl.auth import admin_auth_middleware
from bot.app.web.admin_api_impl.broadcast import (
    admin_broadcast_audience_counts_route,
    admin_broadcast_route,
)
from bot.app.web.context import I18N, SESSION_FACTORY, SETTINGS, SUBSCRIPTION_SERVICE
from bot.app.web.webapp.assets import _csrf_protection_middleware
from bot.middlewares.i18n import JsonI18n
from bot.plugins.spec import WEB_SCOPE_WEBAPP, PluginContext
from bot.services.account_roles import grant_role, revoke_role
from bot.services.audience_segmentation import AudienceProvider, AudienceSegmentationService
from db.dal import payment_dal, subscription_dal, user_balance_dal, user_broadcast_dal, user_dal
from db.models import AccountRole, AccountRoleEvent, AutoRenewCycle, Payment
from minishop_corp import plugin
from minishop_corp.broadcasts import SelectBroadcastAudience, contract_target, select_audience
from minishop_corp.integration.access import AccessAdapter
from minishop_corp.integration.access_types import AccessError, AccessFailure, PeriodAccess
from minishop_corp.integration.broadcasts import CorporateAudienceService
from minishop_corp.integration.contracts import ContractHost
from minishop_corp.memberships import Memberships
from minishop_corp.storage.schema import Contract, Membership, Operation, Reservation
from sqlalchemy import func, select

from .conftest import EXTERNAL_SQUAD, USER_ID, Host
from .test_access import seed_personal
from .test_broadcasts import add_member, services
from .test_contracts import authorization
from .test_memberships import execute, invitation, joined
from .test_storage import seed_contract

pytestmark = [
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        find_spec("db.dal.broadcast_audience_dal") is None,
        reason="These compatibility probes require Minishop 3.8.5",
    ),
]


@pytest_asyncio.fixture(loop_scope="session")
async def broadcast_client(host: Host) -> AsyncIterator[TestClient]:
    from bot.app.web.admin_api_impl.user_broadcast_audiences import (
        admin_user_broadcast_audiences_get_route,
        admin_user_broadcast_audiences_patch_route,
    )

    await grant_role(host.session, USER_ID, "admin", source="corp_385_fixture")
    sessions, _ = services(host)
    context = PluginContext(
        host.settings,
        session_factory=sessions,
        services={
            "audience_segmentation_service": AudienceSegmentationService(
                sessions, settings=host.settings
            ),
        },
    )
    plugin.setup(context)
    app = web.Application(middlewares=[_csrf_protection_middleware, admin_auth_middleware])
    app[SETTINGS], app[SUBSCRIPTION_SERVICE], app[SESSION_FACTORY] = (
        host.settings,
        host.service,
        sessions,
    )
    app[I18N] = JsonI18n(str(plugin.locales_dir()), default="en")
    plugin.setup_web(context, app, scope=WEB_SCOPE_WEBAPP)
    app.router.add_get(
        "/api/admin/broadcast/audience-counts", admin_broadcast_audience_counts_route
    )
    app.router.add_post("/api/admin/broadcast", admin_broadcast_route)
    path = "/api/admin/users/{user_id}/broadcast-audiences"
    app.router.add_get(path, admin_user_broadcast_audiences_get_route)
    app.router.add_patch(path, admin_user_broadcast_audiences_patch_route)
    async with TestClient(TestServer(app)) as client:
        yield client


def audience_service(client: TestClient) -> CorporateAudienceService:
    service = client.app["audience_segmentation_service"]
    assert isinstance(service, CorporateAudienceService)
    return service


@pytest.mark.parametrize("kind", ["corporate", "other-provider", "required-channel"])
@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="1.1.0 does not adapt native user_audiences providers, settings or corporate targets",
)
async def test_user_audience_settings_preserve_native_and_corporate_groups(
    broadcast_client: TestClient, host: Host, kind: str
) -> None:
    service = audience_service(broadcast_client)
    contract = await seed_contract(host.session)
    recipient = await add_member(host, contract, 1, telegram_id=810001)
    if kind == "corporate":
        expected = contract_target(contract.id)
        # The admin editor warms the per-process corporate catalog first.
        reply = await broadcast_client.get(
            "/api/admin/broadcast/audience-counts", headers=authorization(host)
        )
        assert reply.status == 200 and (await reply.json())["counts"][expected] == 1
    elif kind == "other-provider":
        expected = "other.audience"

        async def members() -> list[int]:
            return [recipient]

        service.register_provider(AudienceProvider(expected, "other", "Other", members))
    else:
        expected = "without_required_channel"
        host.settings.REQUIRED_CHANNEL_ID = -1001234567
        await user_dal.update_user(
            host.session,
            recipient,
            {
                "channel_subscription_verified_for": host.settings.REQUIRED_CHANNEL_ID,
                "channel_subscription_verified": False,
                "channel_subscription_checked_at": datetime.now(UTC),
            },
        )
    reply = await broadcast_client.get(
        f"/api/admin/users/{recipient}/broadcast-audiences", headers=authorization(host)
    )
    assert reply.status == 200, await reply.text()
    assert expected in (await reply.json())["targets"]


@pytest.mark.parametrize("kind", ["contract", "selection"])
@pytest.mark.parametrize("check", ["recipients", "counts"])
@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="1.1.0 corporate recipient selection and counts ignore native audience exclusions",
)
async def test_native_exclusion_removes_recipient_from_corporate_audience(
    broadcast_client: TestClient, host: Host, kind: str, check: str
) -> None:
    from db.dal import broadcast_audience_dal

    first = await seed_contract(host.session)
    recipient = await add_member(host, first, 1, telegram_id=810001)
    target = contract_target(first.id)
    if kind == "selection":
        second = await seed_contract(host.session)
        target = await select_audience(
            host.session, SelectBroadcastAudience(contract_ids=[first.id, second.id])
        )
    service = audience_service(broadcast_client)
    assert await service.resolve_user_ids(target) == [recipient]
    reply = await broadcast_client.patch(
        f"/api/admin/users/{recipient}/broadcast-audiences",
        json={"action": "exclude", "target": target},
        headers=authorization(host),
    )
    assert reply.status == 200 and (await reply.json())["excluded"] == [target]
    assert await broadcast_audience_dal.user_exclusions(host.session, recipient) == {target}
    if check == "recipients":
        assert await service.resolve_user_ids(target) == []
    else:
        reply = await broadcast_client.get(
            "/api/admin/broadcast/audience-counts", headers=authorization(host)
        )
        assert reply.status == 200
        assert (await reply.json())["counts"][target] == 0


async def test_multiple_native_targets_include_corporate_members_once(
    broadcast_client: TestClient, host: Host
) -> None:
    first = await seed_contract(host.session)
    second = await seed_contract(host.session)
    one = await add_member(host, first, 1, telegram_id=810001)
    two = await add_member(host, second, 2, telegram_id=810002)
    selection = await select_audience(
        host.session, SelectBroadcastAudience(contract_ids=[first.id, second.id])
    )
    targets = [contract_target(first.id), selection, "all"]
    service = audience_service(broadcast_client)
    recipients = await service.resolve_targets(targets)
    assert recipients.count(one) == recipients.count(two) == 1
    # The full suite also creates committed accounts for concurrency probes.
    # Native "all" includes them; compare with real deliverable Telegram users.
    expected = {
        user_id
        for user_id, _ in await user_broadcast_dal.get_telegram_recipients_for_broadcast(
            host.session, recipients, exclude_blocked=True
        )
    }
    assert {one, two} <= expected
    reply = await broadcast_client.post(
        "/api/admin/broadcast",
        headers=authorization(host),
        json={
            "targets": targets,
            "texts": {"en": "Synthetic multi-audience message"},
            "channels": ["telegram"],
            "scheduled_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        },
    )
    assert reply.status == 200, await reply.text()
    saved = (await reply.json())["broadcast"]
    assert saved["targets"] == targets
    assert saved["recipient_count"] == saved["total_deliveries"] == len(expected)


@pytest.mark.parametrize("before", ["new", "paid"])
async def test_balance_scheduler_cannot_renew_corporate_period(host: Host, before: str) -> None:
    from bot.services.auto_renew_retry_worker import AutoRenewRetryWorker
    from bot.services.balance_recurring import BalanceRecurringService
    from bot.services.user_balance_service import UserBalanceService

    # 3.8.5 enables balance recurrence by default and schedules its first charge
    # even when the general scheduler and retries are disabled.
    assert host.settings.USER_BALANCE_RECURRING_ENABLED
    host.settings.USER_BALANCE_ENABLED = True
    host.settings.AUTO_RENEW_RETRY_ENABLED = False
    host.settings.AUTO_RENEW_SCHEDULER_ENABLED = False
    balance = UserBalanceService(host.settings)
    await balance.admin_adjust(
        host.session,
        user_id=USER_ID,
        actor_admin_id=USER_ID,
        mode="add",
        amount=1000,
        reason="Synthetic compatibility fixture",
        idempotency_key="corp-385-balance",
    )
    recurring = BalanceRecurringService(
        host.settings,
        provider="user_balance",
        subscription_service=host.service,
        referral_service=None,
        bot=None,
        i18n=JsonI18n(str(plugin.locales_dir()), default="en"),
    )
    assert recurring.recurring_active
    host.service.recurring_provider_services["user_balance"] = recurring
    await seed_personal(host, before)
    adapter = AccessAdapter(host.service)
    if before == "paid":
        sub = await subscription_dal.get_active_subscription_by_user_id(host.session, USER_ID)
        assert sub is not None
        await subscription_dal.update_subscription(
            host.session,
            sub.subscription_id,
            {"provider": "user_balance", "auto_renew_enabled": True},
        )
        with pytest.raises(AccessError) as error:
            await adapter.validate_join(host.session, USER_ID)
        assert error.value.code == AccessFailure.RENEWAL_POLICY_REQUIRED
        # Represent the user's separate native opt-out, required before joining.
        await subscription_dal.update_subscription(
            host.session, sub.subscription_id, {"auto_renew_enabled": False}
        )
    await adapter.validate_join(host.session, USER_ID)
    expected = PeriodAccess("corp", datetime.now(UTC) + timedelta(minutes=30), EXTERNAL_SQUAD)
    assigned = await adapter.assign_period(host.session, USER_ID, expected)
    assert assigned.provider == "admin" and not assigned.auto_renew_enabled
    funded = await user_balance_dal.balance_minor(host.session, USER_ID, balance.currency)
    assert funded > 0
    sessions, _ = services(host)
    await host.session.commit()
    worker = AutoRenewRetryWorker(host.settings, sessions, host.service)
    await worker.tick()
    await worker.tick()
    host.session.expire_all()
    assert await adapter.read(host.session, USER_ID) == assigned
    assert await user_balance_dal.balance_minor(host.session, USER_ID, balance.currency) == funded
    assert await host.session.scalar(select(func.count()).select_from(Payment)) == 0
    assert await host.session.scalar(select(func.count()).select_from(AutoRenewCycle)) == 0


@pytest.mark.parametrize(
    ("status", "terminal"),
    [("pending", "canceled"), ("succeeded_pending_finalization", "succeeded")],
)
async def test_new_payhot_payment_blocks_pending_join_until_native_completion(
    host: Host, status: str, terminal: str
) -> None:
    draft = await invitation(host.session)
    result = await Memberships(ContractHost(host.service)).confirm(host.session, USER_ID, draft)
    assert result.operation is not None
    payment = await payment_dal.create_payment_record(
        host.session,
        {
            "user_id": USER_ID,
            "amount": 100,
            "currency": "RUB",
            "status": status,
            "provider": "payhot",
        },
    )
    assert not await execute(host, result.operation.id)
    operation = await host.session.get(Operation, result.operation.id)
    assert operation is not None and operation.error_code == "minishop_corp_payment_pending"
    assert not host.panel.users
    reserve = await host.session.scalar(
        select(Reservation).where(Reservation.operation_id == operation.id)
    )
    assert reserve is not None and reserve.state == "held"
    await payment_dal.update_payment_status_by_db_id(host.session, payment.payment_id, terminal)
    assert await execute(host, operation.id, due=True)


@pytest.mark.parametrize("revoked", [False, True])
async def test_native_deletion_clears_role_actor_and_corporate_links_without_trial(
    host: Host, revoked: bool
) -> None:
    draft, membership_id = await joined(host)
    other = USER_ID + 10
    await user_dal.create_user(host.session, {"user_id": other})
    await grant_role(host.session, other, "admin", actor_user_id=USER_ID, source="corp_385_fixture")
    if revoked:
        await revoke_role(host.session, other, "admin", actor_user_id=USER_ID)
    requests = list(host.panel.requests)
    # The core DAL runs without a loaded plugin, as when packages are disabled.
    assert await user_dal.delete_user_and_relations(host.session, USER_ID)
    host.session.expire_all()
    contract = await host.session.get(Contract, draft.offer.contract_id)
    member = await host.session.get(Membership, membership_id)
    assert contract is not None and contract.manager_user_id is None
    assert member is not None and member.state == "deleted" and member.current_user_id is None
    role = await host.session.get(AccountRole, (other, "admin"))
    assert role is not None and role.granted_by is None and role.revoked_by is None
    assert (role.revoked_at is not None) == revoked
    events = list(
        (
            await host.session.scalars(
                select(AccountRoleEvent).where(AccountRoleEvent.user_id == other)
            )
        ).all()
    )
    assert len(events) == (2 if revoked else 1)
    assert all(event.actor_user_id is None for event in events)
    assert host.panel.requests == requests
    assert await host.session.scalar(select(func.count()).select_from(Payment)) == 0
