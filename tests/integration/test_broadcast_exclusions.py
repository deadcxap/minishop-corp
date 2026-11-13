"""Native 3.8.5 audience controls, durable corporate groups and actual delivery."""

import asyncio
from datetime import UTC, datetime, timedelta
from importlib.util import find_spec

import pytest
from aiogram import Bot
from aiohttp.test_utils import TestClient
from bot.middlewares.i18n import JsonI18n
from bot.services.admin_broadcast_delivery import AdminBroadcastDeliveryService
from bot.services.audience_segmentation import AudienceProvider
from bot.utils.message_queue import MessageQueueManager
from db.broadcast_models import AdminBroadcastDelivery
from db.dal import broadcast_dal, user_dal
from minishop_corp import plugin
from minishop_corp.broadcasts import SelectBroadcastAudience, contract_target, select_audience
from minishop_corp.storage.schema import Membership, Revision
from sqlalchemy import delete, select

from .conftest import USER_ID, Host
from .telegram import Telegram
from .test_broadcasts import add_member, services
from .test_contracts import authorization
from .test_members_api import telegram as telegram
from .test_minishop_385_compatibility import audience_service
from .test_minishop_385_compatibility import broadcast_client as broadcast_client
from .test_storage import seed_contract

pytestmark = [
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        find_spec("db.dal.broadcast_audience_dal") is None,
        reason="Native audience exclusions require Minishop 3.8.5",
    ),
]


async def update(
    client: TestClient, host: Host, user_id: int, action: str, target: str | None = None
) -> set[str]:
    body = {"action": action}
    if target is not None:
        body["target"] = target
    reply = await client.patch(
        f"/api/admin/users/{user_id}/broadcast-audiences", json=body, headers=authorization(host)
    )
    assert reply.status == 200, await reply.text()
    return set((await reply.json())["excluded"])


async def test_native_controls_preserve_group_discovery_and_survive_new_worker(
    broadcast_client: TestClient, host: Host
) -> None:
    first, second = await seed_contract(host.session), await seed_contract(host.session)
    one = await add_member(host, first, 1)
    two = await add_member(host, second, 2)
    target = contract_target(first.id)
    selection = await select_audience(
        host.session, SelectBroadcastAudience(contract_ids=[first.id, second.id])
    )
    service = audience_service(broadcast_client)
    assert service.catalog == []
    assert {target, selection} <= set(await service.user_audiences(one))
    assert await update(broadcast_client, host, one, "exclude", target) == {target}
    assert await service.resolve_user_ids(target) == []
    assert await service.resolve_user_ids(selection) == [one, two]
    counts = await service.counts()
    assert counts[target] == 0 and counts[selection] == 2
    excluded = await update(broadcast_client, host, one, "exclude_all")
    assert {"all", target, selection} <= excluded
    _, worker = services(host)
    assert not worker.catalog
    assert await worker.resolve_user_ids(target) == []
    assert await worker.resolve_user_ids(selection) == [two]
    assert one not in await worker.resolve_user_ids("all")
    counts = await worker.counts()
    assert counts[target] == 0 and counts[selection] == 1
    reply = await broadcast_client.get(
        f"/api/admin/users/{one}/broadcast-audiences", headers=authorization(host)
    )
    assert reply.status == 200
    data = await reply.json()
    assert {target, selection} <= set(data["targets"]) and set(data["excluded"]) == excluded
    assert await update(broadcast_client, host, one, "include", target) == excluded - {target}
    assert await worker.resolve_user_ids(target) == [one]
    assert await worker.resolve_user_ids(selection) == [two]
    assert await update(broadcast_client, host, one, "reset") == set()
    assert await worker.resolve_user_ids(selection) == [one, two]
    assert (await worker.counts())[selection] == 2


async def test_discovery_uses_live_membership_and_ignores_invalid_selections(
    broadcast_client: TestClient, host: Host
) -> None:
    first, second, third = [await seed_contract(host.session) for _ in range(3)]
    first.ends_at = datetime.now(UTC) - timedelta(days=1)
    recipient = await add_member(host, first, 1)
    valid = await select_audience(
        host.session, SelectBroadcastAudience(contract_ids=[first.id, second.id])
    )
    stale = await select_audience(
        host.session, SelectBroadcastAudience(contract_ids=[first.id, third.id])
    )
    service = audience_service(broadcast_client)
    await service.counts()
    await host.session.execute(delete(Revision).where(Revision.contract_id == third.id))
    await host.session.delete(third)
    await host.session.flush()
    targets = await service.user_audiences(recipient)
    assert {contract_target(first.id), valid} <= set(targets) and stale not in targets
    assert (await service.counts())[stale] is None
    member = await host.session.scalar(
        select(Membership).where(Membership.current_user_id == recipient)
    )
    assert member is not None
    member.state = "leaving"
    await host.session.flush()
    assert not any(t.startswith("minishop-corp.") for t in await service.user_audiences(recipient))
    member.state = "active"
    await user_dal.update_user(host.session, recipient, {"is_banned": True})
    assert not any(t.startswith("minishop-corp.") for t in await service.user_audiences(recipient))


async def test_counts_do_not_subtract_excluded_nonmembers_or_ineligible_members(
    broadcast_client: TestClient, host: Host
) -> None:
    first, outside = await seed_contract(host.session), await seed_contract(host.session)
    active = await add_member(host, first, 1)
    former = await add_member(host, first, 2, state="left")
    banned = await add_member(host, first, 3, is_banned=True)
    unrelated = await add_member(host, outside, 4)
    target = contract_target(first.id)
    for user_id in (former, banned, unrelated):
        await update(broadcast_client, host, user_id, "exclude", target)
    service = audience_service(broadcast_client)
    assert (await service.counts())[target] == 1
    assert await service.resolve_user_ids(target) == [active]
    await update(broadcast_client, host, active, "exclude", target)
    assert (await service.counts())[target] == 0
    assert await service.resolve_user_ids(target) == []


async def test_native_provider_exclusions_and_direct_user_messages_keep_native_semantics(
    broadcast_client: TestClient, host: Host
) -> None:
    service = audience_service(broadcast_client)

    async def members() -> list[int]:
        return [USER_ID, USER_ID]

    service.register_provider(AudienceProvider("other.audience", "other", "Other", members))
    await update(broadcast_client, host, USER_ID, "exclude", "other.audience")
    assert await service.resolve_user_ids("other.audience") == []
    assert (await service.counts())["other.audience"] == 0
    assert "other.audience" in await service.user_audiences(USER_ID)
    assert await service.resolve_user_ids(f"user:{USER_ID}") == [USER_ID]


@pytest.mark.parametrize("kind", ["contract", "selection"])
@pytest.mark.parametrize("multiple", [False, True], ids=["legacy-target", "targets"])
async def test_delivery_rechecks_exclusions_after_scheduling(
    broadcast_client: TestClient,
    host: Host,
    telegram: tuple[Telegram, Bot],
    kind: str,
    multiple: bool,
) -> None:
    transport, bot = telegram
    first, second = await seed_contract(host.session), await seed_contract(host.session)
    excluded = await add_member(host, first, 1, telegram_id=810001, language_code="en")
    recipient = await add_member(host, first, 2, telegram_id=810002, language_code="en")
    target = contract_target(first.id)
    if kind == "selection":
        target = await select_audience(
            host.session, SelectBroadcastAudience(contract_ids=[first.id, second.id])
        )
    item = await broadcast_dal.create_broadcast(
        host.session,
        actor_id=USER_ID,
        target=target,
        targets=[target] if multiple else None,
        user_ids=[excluded, recipient],
        channels=["telegram"],
        exclude_blocked_telegram=True,
        texts={"en": "Synthetic corporate news"},
        email_subjects={},
        buttons=[],
        scheduled_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    assert item.recipient_count == item.total_deliveries == 2
    await update(broadcast_client, host, excluded, "exclude", target)
    sessions, worker = services(host)
    queue = MessageQueueManager(bot)
    delivery = AdminBroadcastDeliveryService(
        settings=host.settings,
        session_factory=sessions,
        i18n=JsonI18n(str(plugin.locales_dir()), default="en"),
        audience_service=worker,
        queue_manager=queue,
        bot_username="synthetic_bot",
    )
    transport.release = asyncio.Event()
    result = await delivery.dispatch(item.broadcast_id)
    transport.release.set()
    if queue.user_queue._processing_task is not None:
        await queue.user_queue._processing_task
    assert result.queued == 1 and transport.calls == ["sendMessage"]
    rows = list(
        await host.session.scalars(
            select(AdminBroadcastDelivery).where(
                AdminBroadcastDelivery.broadcast_id == item.broadcast_id
            )
        )
    )
    assert [(row.user_id, row.status) for row in rows] == [(recipient, "sent")]
    assert (await delivery.dispatch(item.broadcast_id)).queued == 0
