import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from inspect import signature
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from bot.app.web.admin_api_impl.broadcast import (
    admin_broadcast_audience_counts_route,
    admin_broadcast_route,
)
from bot.app.web.context import I18N, SESSION_FACTORY, SETTINGS, SUBSCRIPTION_SERVICE
from bot.app.web.webapp_auth import create_webapp_session_token
from bot.middlewares.i18n import JsonI18n
from bot.plugins.spec import WEB_SCOPE_WEBAPP, PluginContext
from bot.services.account_roles import grant_role, revoke_role
from bot.services.admin_broadcast_delivery import AdminBroadcastDeliveryService
from bot.services.audience_segmentation import AudienceNotFoundError, AudienceSegmentationService
from bot.utils.message_queue import MessageQueueManager
from db.broadcast_models import AdminBroadcastDelivery
from db.dal import broadcast_dal, user_dal
from minishop_corp import plugin
from minishop_corp.broadcasts import SelectBroadcastAudience, contract_target, select_audience
from minishop_corp.integration.broadcasts import CorporateAudienceService
from minishop_corp.storage.schema import BroadcastAudience, Contract, Membership, Revision
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from .conftest import USER_ID, Host
from .test_contracts import MANAGER, authorization
from .test_contracts import client as client
from .test_members_api import telegram as telegram
from .test_storage import seed_contract

pytestmark = pytest.mark.asyncio(loop_scope="session")
PATH = "/api/admin/minishop-corp/broadcast-audiences"


def services(host: Host) -> tuple[async_sessionmaker, CorporateAudienceService]:
    sessions = async_sessionmaker(
        bind=host.session.bind,
        expire_on_commit=False,
        autoflush=False,
        join_transaction_mode="create_savepoint",
    )
    return sessions, CorporateAudienceService(AudienceSegmentationService(sessions), sessions)


async def add_member(
    host: Host, contract: Contract, offset: int, state: str = "active", **data: object
) -> int:
    identifier = USER_ID + 100 + offset
    await user_dal.create_user(host.session, {"user_id": identifier, **data})
    historical = state in {"left", "excluded", "failed", "deleted"}
    host.session.add(
        Membership(
            contract_id=contract.id,
            user_id=identifier,
            current_user_id=None if historical else identifier,
            state=state,
            joined_at=datetime.now(UTC),
            ended_at=datetime.now(UTC) if historical else None,
        )
    )
    await host.session.flush()
    return identifier


async def test_live_members_and_immutable_selection_survive_new_service(host: Host) -> None:
    first = await seed_contract(host.session)
    second = await seed_contract(host.session)
    outside = await seed_contract(host.session)
    second.ends_at = datetime.now(UTC) - timedelta(days=1)
    first_id = await add_member(host, first, 1)
    second_id = await add_member(host, second, 2)
    await add_member(host, outside, 3)
    for offset, state in enumerate(
        ("pending", "leaving", "left", "excluded", "failed", "deleted"), 4
    ):
        await add_member(host, first, offset, state)
    await add_member(host, first, 20, is_banned=True)
    target = await select_audience(
        host.session, SelectBroadcastAudience(contract_ids=[second.id, first.id])
    )
    assert target == await select_audience(
        host.session, SelectBroadcastAudience(contract_ids=[first.id, second.id, first.id])
    )
    assert await host.session.scalar(select(func.count()).select_from(BroadcastAudience)) == 1
    _, web_service = services(host)
    assert await web_service.resolve_user_ids(target) == [first_id, second_id]
    counts = await web_service.counts()
    assert counts[target] == 2 and counts[contract_target(first.id)] == 1
    assert any(row.target == target for row in web_service.audiences())
    # A worker has never discovered this target; it still resolves it from the DB.
    _, worker = services(host)
    assert not worker.catalog
    member = await host.session.scalar(
        select(Membership).where(Membership.current_user_id == first_id)
    )
    member.state = "leaving"
    await host.session.flush()
    newcomer = await add_member(host, first, 21)
    assert await worker.resolve_user_ids(target) == [second_id, newcomer]
    assert not host.panel.requests


async def test_deleted_contract_invalidates_entire_selection_without_blocking_deletion(
    host: Host,
) -> None:
    first = await seed_contract(host.session)
    second = await seed_contract(host.session)
    target = await select_audience(
        host.session, SelectBroadcastAudience(contract_ids=[first.id, second.id])
    )
    await host.session.execute(delete(Revision).where(Revision.contract_id == second.id))
    await host.session.delete(second)
    await host.session.flush()
    _, service = services(host)
    with pytest.raises(AudienceNotFoundError):
        await service.resolve_user_ids(target)
    assert (await service.counts())[target] is None
    unavailable = next(row for row in service.audiences() if row.target == target)
    assert unavailable.available and unavailable.label_key == "minishop_corp_broadcast_unavailable"
    for unknown in (
        contract_target(uuid4()),
        "minishop-corp.selection:" + uuid4().hex,
        "minishop-corp.bad",
    ):
        with pytest.raises(AudienceNotFoundError):
            await service.resolve_user_ids(unknown)
    with pytest.raises(AudienceNotFoundError):
        await service.native.resolve_user_ids(target)  # plugin disabled


async def test_selection_api_authorization_csrf_and_safe_retries(
    client: TestClient, host: Host
) -> None:
    first = await seed_contract(host.session, manager=MANAGER)
    second = await seed_contract(host.session)
    body = {"contract_ids": [str(first.id), str(second.id)]}
    for headers, status in (({}, 401), (authorization(host, MANAGER), 403)):
        assert (await client.post(PATH, json=body, headers=headers)).status == status
    cookie = "rw_webapp_session=" + create_webapp_session_token(host.settings, USER_ID)
    cookie += "; rw_webapp_csrf=synthetic-csrf"
    origin = str(client.make_url("/")).rstrip("/")
    assert (
        await client.post(PATH, json=body, headers={"Cookie": cookie, "Origin": origin})
    ).status == 403
    headers = {"Cookie": cookie, "Origin": origin, "X-CSRF-Token": "synthetic-csrf"}
    reply = await client.post(PATH, json=body, headers=headers)
    assert reply.status == 200
    target = (await reply.json())["target"]
    assert reply.headers["Cache-Control"] == "private, no-store"
    repeated = await client.post(
        PATH, json={"contract_ids": list(reversed(body["contract_ids"]))}, headers=headers
    )
    assert (await repeated.json())["target"] == target
    missing = await client.post(PATH, json={"contract_ids": [str(uuid4())]}, headers=headers)
    assert missing.status == 404
    for invalid in ([], ["not-a-uuid"], [str(uuid4()) for _ in range(101)]):
        assert (
            await client.post(PATH, json={"contract_ids": invalid}, headers=headers)
        ).status == 400
    await revoke_role(host.session, USER_ID, "admin", actor_user_id=USER_ID)
    assert (await client.post(PATH, json=body, headers=headers)).status == 403


async def test_concurrent_selection_and_transaction_rollback(engine: AsyncEngine) -> None:
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as session:
        actor = -(uuid4().int % 1_000_000_000)
        await user_dal.create_user(session, {"user_id": actor})
        first = await seed_contract(session, manager=actor)
        second = await seed_contract(session, manager=actor)
        ids = [first.id, second.id]
        await session.commit()
    draft = SelectBroadcastAudience(contract_ids=ids)
    try:
        async with sessions() as session:
            target = await select_audience(session, draft)
            await session.rollback()
        async with sessions() as session:
            assert await session.get(BroadcastAudience, UUID(hex=target.split(":")[1])) is None

        async def create() -> str:
            async with sessions() as session:
                result = await select_audience(session, draft)
                await session.commit()
                return result

        assert await asyncio.gather(create(), create()) == [target, target]
    finally:
        async with sessions() as session:
            await session.execute(
                delete(BroadcastAudience).where(
                    BroadcastAudience.id == UUID(hex=target.split(":")[1])
                )
            )
            await session.execute(delete(Revision).where(Revision.contract_id.in_(ids)))
            await session.execute(delete(Contract).where(Contract.id.in_(ids)))
            await user_dal.delete_user_and_relations(session, actor)
            await session.commit()


@pytest_asyncio.fixture(loop_scope="session")
async def native_client(host: Host) -> AsyncIterator[TestClient]:
    from bot.app.web.admin_api_impl.auth import admin_auth_middleware
    from bot.app.web.webapp.assets import _csrf_protection_middleware

    await grant_role(host.session, USER_ID, "admin", source="corp_broadcast_test")
    sessions, _ = services(host)
    context = PluginContext(
        host.settings,
        session_factory=sessions,
        services={
            "audience_segmentation_service": AudienceSegmentationService(sessions),
        },
    )
    plugin.setup(context)
    app = web.Application(middlewares=[_csrf_protection_middleware, admin_auth_middleware])
    app[SETTINGS] = host.settings
    app[SESSION_FACTORY] = sessions
    app[SUBSCRIPTION_SERVICE] = host.service
    app[I18N] = JsonI18n(str(plugin.locales_dir()), default="en")
    plugin.setup_web(context, app, scope=WEB_SCOPE_WEBAPP)
    assert app["audience_segmentation_service"] is context.require_audience_segmentation_service()
    app.router.add_get(
        "/api/admin/broadcast/audience-counts", admin_broadcast_audience_counts_route
    )
    app.router.add_post("/api/admin/broadcast", admin_broadcast_route)
    async with TestClient(TestServer(app)) as client:
        yield client


async def test_native_http_discovery_and_scheduling(native_client: TestClient, host: Host) -> None:
    first = await seed_contract(host.session)
    target = contract_target(first.id)
    await add_member(host, first, 1)
    reply = await native_client.get(
        "/api/admin/broadcast/audience-counts", headers=authorization(host)
    )
    assert reply.status == 200
    data = await reply.json()
    assert data["counts"][target] == 1
    assert any(row["target"] == target for row in data["audiences"])
    draft = {
        "target": target,
        "texts": {"en": "Corporate news"},
        "channels": ["telegram"],
        "scheduled_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
    }
    reply = await native_client.post(
        "/api/admin/broadcast", json=draft, headers=authorization(host)
    )
    assert reply.status == 200, await reply.text()
    saved = (await reply.json())["broadcast"]
    assert saved["target"] == target and saved["status"] == "scheduled"
    draft["target"] = contract_target(uuid4())
    reply = await native_client.post(
        "/api/admin/broadcast", json=draft, headers=authorization(host)
    )
    assert reply.status == 400 and (await reply.json())["error"] == "invalid_audience"


async def test_native_delivery_filters_preferences_and_sends_once(host: Host, telegram) -> None:
    transport, bot = telegram
    first = await seed_contract(host.session)
    second = await seed_contract(host.session)
    recipient = await add_member(host, first, 1, telegram_id=810001, language_code="en")
    await add_member(
        host, second, 2, telegram_id=810002, marketing_notifications_telegram_enabled=False
    )
    await add_member(host, second, 3, telegram_id=810003, telegram_notifications_status="blocked")
    await add_member(host, second, 4, is_banned=True, telegram_id=810004)
    await add_member(host, first, 5)  # no linked Telegram
    target = await select_audience(
        host.session, SelectBroadcastAudience(contract_ids=[first.id, second.id])
    )
    sessions, service = services(host)
    queue = MessageQueueManager(bot)
    delivery = AdminBroadcastDeliveryService(
        settings=host.settings,
        session_factory=sessions,
        i18n=JsonI18n(str(plugin.locales_dir()), default="en"),
        audience_service=service,
        queue_manager=queue,
        bot_username="synthetic_bot",
    )
    # Core 3.8.3 requires recipients to calculate progress before delivery.
    # The production HTTP route supplies them; keep this DAL fixture valid on 3.8.2 too.
    initial_audience = (
        {"user_ids": await service.resolve_user_ids(target)}
        if "user_ids" in signature(broadcast_dal.create_broadcast).parameters
        else {}
    )
    item = await broadcast_dal.create_broadcast(
        host.session,
        actor_id=USER_ID,
        target=target,
        channels=["telegram"],
        exclude_blocked_telegram=True,
        texts={"en": "Corporate news", "ru": "Новости"},
        email_subjects={},
        buttons=[{"kind": "url", "label": "Details", "url": "https://example.invalid/news"}],
        scheduled_at=datetime.now(UTC) - timedelta(seconds=1),
        **initial_audience,
    )
    if initial_audience:
        assert item.recipient_count == item.total_deliveries == 1
    transport.release = asyncio.Event()
    result = await delivery.dispatch(item.broadcast_id)
    transport.release.set()
    if queue.user_queue._processing_task is not None:
        await queue.user_queue._processing_task
    assert result.queued == 1
    assert transport.calls == ["sendMessage"]
    rows = list(
        await host.session.scalars(
            select(AdminBroadcastDelivery).where(
                AdminBroadcastDelivery.broadcast_id == item.broadcast_id
            )
        )
    )
    assert [(row.user_id, row.status) for row in rows] == [(recipient, "sent")]
    assert (await delivery.dispatch(item.broadcast_id)).queued == 0
