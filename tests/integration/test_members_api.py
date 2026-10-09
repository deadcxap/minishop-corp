import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from aiogram import Bot
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from bot.app.web.admin_api_impl.auth import admin_auth_middleware
from bot.app.web.context import BOT, SESSION_FACTORY, SETTINGS, SUBSCRIPTION_SERVICE
from bot.app.web.webapp.assets import _csrf_protection_middleware
from bot.plugins.spec import WEB_SCOPE_WEBAPP, PluginContext
from bot.services.account_roles import grant_role, revoke_role
from db.dal import user_dal
from minishop_corp import plugin
from minishop_corp.contracts import Contracts, terms_of
from minishop_corp.contracts_types import UpdateContract
from minishop_corp.integration.contracts import ContractHost
from minishop_corp.membership_types import DepartMembership
from minishop_corp.memberships import Memberships
from minishop_corp.operation_worker import OperationWorker
from minishop_corp.storage.schema import Contract, Membership, Operation
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from .conftest import USER_ID, Host
from .telegram import PNG, Telegram
from .test_contracts import ADMIN_PATH, MANAGER, MEMBER, OTHER, USER_PATH, authorization
from .test_memberships import execute, invitation

pytestmark = pytest.mark.asyncio(loop_scope="session")


@pytest_asyncio.fixture(loop_scope="session")
async def telegram() -> AsyncIterator[tuple[Telegram, Bot]]:
    server = Telegram()
    async with TestServer(server.app) as transport:
        api = TelegramAPIServer.from_base(str(transport.make_url("/")).rstrip("/"))
        bot = Bot("100000:SYNTHETIC_TEST_TOKEN_NOT_A_REAL_BOT", session=AiohttpSession(api=api))
        try:
            yield server, bot
        finally:
            await bot.session.close()


@pytest_asyncio.fixture(loop_scope="session")
async def client(host: Host, telegram: tuple[Telegram, Bot]) -> AsyncIterator[TestClient]:
    for identifier in (MANAGER, MEMBER, OTHER):
        await user_dal.create_user(host.session, {"user_id": identifier})
    await grant_role(host.session, USER_ID, "admin", source="corp_member_tests")
    app = web.Application(middlewares=[_csrf_protection_middleware, admin_auth_middleware])
    app[SETTINGS] = host.settings
    app[SESSION_FACTORY] = async_sessionmaker(
        bind=host.session.bind, expire_on_commit=False, join_transaction_mode="create_savepoint"
    )
    app[SUBSCRIPTION_SERVICE] = host.service
    app[BOT] = telegram[1]
    plugin.setup_web(PluginContext(host.settings), app, scope=WEB_SCOPE_WEBAPP)
    async with TestClient(TestServer(app)) as result:
        yield result


async def enroll(host: Host, users: tuple[int, ...] = (MEMBER,)) -> tuple[UUID, list[UUID]]:
    draft = await invitation(host.session, manager=MANAGER)
    service = Memberships(ContractHost(host.service))
    members = []
    for user in users:
        result = await service.confirm(host.session, user, draft)
        assert result.operation is not None and await execute(host, result.operation.id)
        members.append(result.operation.membership_id)
    return draft.offer.contract_id, members


def paths(contract_id: UUID) -> tuple[str, str]:
    return (
        ADMIN_PATH + f"/{contract_id}/members",
        USER_PATH + f"/managed-contracts/{contract_id}/members",
    )


async def test_paginated_profiles_statistics_and_pending_operations_are_allowlisted(
    client: TestClient,
    host: Host,
) -> None:
    contract_id, member_ids = await enroll(host, (MEMBER, OTHER))
    await user_dal.update_user(
        host.session,
        MEMBER,
        {
            "first_name": "Synthetic",
            "last_name": "Member",
            "username": "fixture_member",
            "telegram_id": 880001,
            "email": "private@example.invalid",
        },
    )
    admin, manager = paths(contract_id)
    collected = []
    cursor = ""
    for _ in range(2):
        reply = await client.get(
            manager + "?limit=1" + cursor, headers=authorization(host, MANAGER)
        )
        assert reply.status == 200, await reply.text()
        assert reply.headers["Cache-Control"] == "private, no-store"
        page = await reply.json()
        collected.extend(page["members"])
        cursor = f"&after={page['next_after']}" if page["next_after"] else ""
    assert [UUID(row["id"]) for row in collected] == sorted(member_ids)
    assert not cursor
    linked = next(row for row in collected if row["profile"]["user_id"] == MEMBER)
    unlinked = next(row for row in collected if row["profile"]["user_id"] == OTHER)
    native = await user_dal.get_user_by_id(host.session, MEMBER)
    assert native is not None and native.minishop_id.startswith("ms_")
    assert linked["profile"] == {
        "user_id": MEMBER,
        "minishop_id": native.minishop_id,
        "first_name": "Synthetic",
        "last_name": "Member",
        "username": "fixture_member",
        "telegram_url": "https://t.me/fixture_member",
    }
    assert linked["avatar_path"].endswith(f"/{linked['id']}/avatar")
    assert unlinked["avatar_path"] is None and unlinked["profile"]["telegram_url"] is None
    assert unlinked["profile"]["minishop_id"].startswith("ms_")
    assert unlinked["profile"]["minishop_id"] != native.minishop_id
    assert linked["statistics"]["device_count"] == {"value": 0, "state": "available"}
    assert linked["statistics"]["device_limit"]["value"] == 5
    assert linked["operation"]["kind"] == "join" and linked["operation"]["state"] == "succeeded"
    serialized = str(collected)
    for secret in (
        "private@example.invalid",
        "panel_user_uuid",
        "subscriptionUrl",
        "hwid",
        "target",
    ):
        assert secret not in serialized
    reply = await client.get(admin, headers=authorization(host))
    assert reply.status == 200 and len((await reply.json())["members"]) == 2
    empty = await invitation(host.session, manager=MANAGER)
    reply = await client.get(
        paths(empty.offer.contract_id)[1], headers=authorization(host, MANAGER)
    )
    assert (await reply.json())["members"] == []


@pytest.mark.parametrize("suffix", ["", "/avatar"])
async def test_member_and_avatar_access_never_grants_global_or_foreign_permissions(
    client: TestClient,
    host: Host,
    telegram: tuple[Telegram, Bot],
    suffix: str,
) -> None:
    contract_id, members = await enroll(host)
    admin, manager = paths(contract_id)
    if suffix:
        admin += f"/{members[0]}{suffix}"
        manager += f"/{members[0]}{suffix}"
    host.panel.requests.clear()
    for user in (OTHER, MEMBER):
        assert (await client.get(manager, headers=authorization(host, user))).status == 404
    assert (await client.get(admin, headers=authorization(host, MANAGER))).status == 403
    assert (await client.get(manager)).status == 401
    assert not host.panel.requests and not telegram[0].calls
    assert (await client.get(manager, headers=authorization(host, MANAGER))).status == 200
    assert (await client.get(admin, headers=authorization(host))).status == 200
    await user_dal.update_user(host.session, MANAGER, {"is_banned": True})
    assert (await client.get(manager, headers=authorization(host, MANAGER))).status == 403


async def test_avatar_target_must_be_current_and_belong_to_the_requested_contract(
    client: TestClient,
    host: Host,
) -> None:
    contract_id, members = await enroll(host)
    other = await invitation(host.session, manager=MANAGER)
    wrong = paths(other.offer.contract_id)[1] + f"/{members[0]}/avatar"
    assert (await client.get(wrong, headers=authorization(host, MANAGER))).status == 404
    service = Memberships(ContractHost(host.service))
    leave = await service.depart(
        host.session, MEMBER, members[0], DepartMembership(request_id=uuid4())
    )
    assert await execute(host, leave.id)
    admin, manager = paths(contract_id)
    for prefix, actor in ((admin, USER_ID), (manager, MANAGER)):
        assert (
            await client.get(prefix + f"/{members[0]}/avatar", headers=authorization(host, actor))
        ).status == 404
        assert (await client.get(prefix, headers=authorization(host, actor))).status == 200
        response = await client.get(prefix, headers=authorization(host, actor))
        assert (await response.json())["members"] == []


async def test_role_changes_apply_to_list_and_avatar_without_relogging(
    client: TestClient, host: Host
) -> None:
    contract_id, members = await enroll(host)
    row = await host.session.get(Contract, contract_id)
    assert row is not None
    await Contracts(ContractHost(host.service)).update(
        host.session,
        USER_ID,
        contract_id,
        UpdateContract.model_validate(
            {**terms_of(row).model_dump(), "expected_version": 1, "manager_user_id": OTHER}
        ),
    )
    admin, manager = paths(contract_id)
    for suffix in ("", f"/{members[0]}/avatar"):
        assert (
            await client.get(manager + suffix, headers=authorization(host, MANAGER))
        ).status == 404
        assert (
            await client.get(manager + suffix, headers=authorization(host, OTHER))
        ).status == 200
    await revoke_role(host.session, USER_ID, "admin", actor_user_id=USER_ID)
    for suffix in ("", f"/{members[0]}/avatar"):
        assert (await client.get(admin + suffix, headers=authorization(host))).status == 403


@pytest.mark.parametrize("query", ["limit=0", "limit=101", "after=invalid", "user_id=123"])
async def test_invalid_member_page_is_rejected(client: TestClient, host: Host, query: str) -> None:
    contract_id, _ = await enroll(host)
    result = await client.get(
        paths(contract_id)[1] + "?" + query, headers=authorization(host, MANAGER)
    )
    assert result.status == 400 and (await result.json())["ok"] is False


async def test_avatar_missing_cache_refreshes_via_native_telegram_and_then_reuses_cache(
    client: TestClient,
    host: Host,
    telegram: tuple[Telegram, Bot],
) -> None:
    contract_id, members = await enroll(host)
    await user_dal.update_user(
        host.session, MEMBER, {"telegram_id": uuid4().int % 1_000_000_000 + 1}
    )
    path = paths(contract_id)[1] + f"/{members[0]}/avatar"
    reply = await client.get(path, headers=authorization(host, MANAGER))
    assert reply.status == 200, await reply.text()
    avatar = (await reply.json())["avatar"]
    assert avatar["state"] == "available" and avatar["data_url"].startswith(
        "data:image/png;base64,"
    )
    assert reply.headers["Cache-Control"] == "private, no-store"
    assert telegram[0].calls == ["getUserProfilePhotos", "getFile", "download"]
    assert await user_dal.get_user_telegram_avatar(host.session, MEMBER) is not None
    telegram[0].calls.clear()
    again = await client.get(path, headers=authorization(host, MANAGER))
    assert (await again.json())["avatar"] == avatar and not telegram[0].calls
    assert (
        await client.get(path + "?user_id=123", headers=authorization(host, MANAGER))
    ).status == 400


@pytest.mark.parametrize("stored", [False, True])
async def test_avatar_refresh_failure_keeps_stale_or_unknown_without_remote_urls(
    client: TestClient,
    host: Host,
    telegram: tuple[Telegram, Bot],
    stored: bool,
) -> None:
    contract_id, members = await enroll(host)
    await user_dal.update_user(
        host.session,
        MEMBER,
        {
            "telegram_id": uuid4().int % 1_000_000_000 + 1,
            "telegram_photo_url": "https://untrusted.invalid/avatar?token=secret",
        },
    )
    if stored:
        row = await user_dal.upsert_user_telegram_avatar(
            host.session,
            user_id=MEMBER,
            file_unique_id="synthetic",
            content_type="image/png",
            image_bytes=PNG,
        )
        row.updated_at = datetime.now(UTC) - timedelta(days=30)
        await host.session.flush()
    telegram[0].fail = True
    reply = await client.get(
        paths(contract_id)[1] + f"/{members[0]}/avatar", headers=authorization(host, MANAGER)
    )
    assert reply.status == 200, await reply.text()
    payload = await reply.json()
    assert payload["avatar"]["state"] == ("stale" if stored else "unavailable")
    assert bool(payload["avatar"]["data_url"]) is stored
    assert "untrusted.invalid" not in str(payload) and "token" not in str(payload)
    assert telegram[0].calls == ["getUserProfilePhotos"]


@pytest.mark.parametrize("kind", ["svg", "mismatch", "oversize"])
async def test_avatar_never_serves_active_or_unbounded_cached_content(
    client: TestClient, host: Host, kind: str
) -> None:
    contract_id, members = await enroll(host)
    data = b"<svg onload='alert(1)'/>" if kind != "oversize" else PNG + b"0" * (512 * 1024)
    await user_dal.upsert_user_telegram_avatar(
        host.session,
        user_id=MEMBER,
        file_unique_id="synthetic",
        content_type="image/svg+xml" if kind == "svg" else "image/png",
        image_bytes=data,
    )
    reply = await client.get(
        paths(contract_id)[1] + f"/{members[0]}/avatar", headers=authorization(host, MANAGER)
    )
    assert reply.status == 200
    assert (await reply.json())["avatar"] == {
        "state": "unavailable",
        "data_url": None,
        "updated_at": None,
    }


async def test_head_does_not_refresh_statistics_or_avatars(
    client: TestClient, host: Host, telegram: tuple[Telegram, Bot]
) -> None:
    contract_id, members = await enroll(host)
    await user_dal.update_user(host.session, MEMBER, {"telegram_id": 880002})
    host.panel.requests.clear()
    before = await host.session.scalar(select(func.count()).select_from(Operation))
    path = paths(contract_id)[1]
    for url in (path, path + f"/{members[0]}/avatar"):
        response = await client.head(url, headers=authorization(host, MANAGER))
        assert response.status == 200 and await response.read() == b""
    assert not host.panel.requests and not telegram[0].calls
    assert await user_dal.get_user_telegram_avatar(host.session, MEMBER) is None
    assert await host.session.scalar(select(func.count()).select_from(Operation)) == before


async def test_stats_outage_preserves_members_and_does_not_report_zero(
    client: TestClient, host: Host
) -> None:
    contract_id, _ = await enroll(host, (MEMBER, OTHER))
    host.panel.fail_reads = host.panel.fail_devices = True
    reply = await client.get(paths(contract_id)[1], headers=authorization(host, MANAGER))
    assert reply.status == 200
    rows = (await reply.json())["members"]
    assert len(rows) == 2
    for row in rows:
        assert row["statistics"]["traffic_used_bytes"] == {"value": None, "state": "unavailable"}
        assert row["statistics"]["device_count"] == {"value": None, "state": "unavailable"}
        assert row["statistics"]["device_limit"]["value"] == 5


async def test_no_telegram_photo_uses_native_negative_cache(
    client: TestClient, host: Host, telegram: tuple[Telegram, Bot]
) -> None:
    contract_id, members = await enroll(host)
    await user_dal.update_user(
        host.session, MEMBER, {"telegram_id": uuid4().int % 1_000_000_000 + 1}
    )
    telegram[0].no_photo = True
    for _ in range(2):
        response = await client.get(
            paths(contract_id)[1] + f"/{members[0]}/avatar", headers=authorization(host, MANAGER)
        )
        assert response.status == 200
        assert (await response.json())["avatar"]["data_url"] is None
    assert telegram[0].calls == ["getUserProfilePhotos"]


@pytest.mark.parametrize("source", ["statistics", "avatar"])
@pytest.mark.parametrize("change", ["manager", "ban", "departure"])
async def test_access_is_rechecked_after_external_refresh(
    committed_members: tuple[async_sessionmaker[AsyncSession], list[UUID]],
    host: Host,
    telegram: tuple[Telegram, Bot],
    source: str,
    change: str,
) -> None:
    # Independent committed transactions make concurrent revocation visible to the
    # HTTP request; the test database itself is discarded after the suite.
    manager = 960000000 + uuid4().int % 100000000
    member, replacement = manager + 100000000, manager + 200000000
    factory, tracked = committed_members
    service = Memberships(ContractHost(host.service))
    worker = OperationWorker(ContractHost(host.service))
    async with factory.begin() as session:
        for identifier in (manager, member, replacement):
            await user_dal.create_user(session, {"user_id": identifier, "telegram_id": identifier})
        await grant_role(session, manager, "admin", source="corp_member_tests")
        draft = await invitation(session, manager=manager)
        confirmed = await service.confirm(session, member, draft)
        assert confirmed.operation is not None
        operation_id, member_id = confirmed.operation.id, confirmed.operation.membership_id
        tracked.append(member_id)
    async with factory.begin() as session:
        lease = await worker.claim(session, operation_id=operation_id)
    assert lease is not None
    async with factory.begin() as session:
        assert await worker.execute(session, lease)
    app = web.Application(middlewares=[_csrf_protection_middleware, admin_auth_middleware])
    app[SETTINGS], app[SESSION_FACTORY] = host.settings, factory
    app[SUBSCRIPTION_SERVICE], app[BOT] = host.service, telegram[1]
    plugin.setup_web(PluginContext(host.settings), app, scope=WEB_SCOPE_WEBAPP)
    started, release = asyncio.Event(), asyncio.Event()
    path = paths(draft.offer.contract_id)[1]
    if source == "avatar":
        telegram[0].started, telegram[0].release = started, release
        path += f"/{member_id}/avatar"
    else:
        host.panel.reads_started, host.panel.continue_reads = started, release
    async with TestClient(TestServer(app)) as transport:
        pending = asyncio.create_task(transport.get(path, headers=authorization(host, manager)))
        try:
            await asyncio.wait_for(started.wait(), 5)
            # Pause only the already-running statistics read, not departure's
            # independent native subscription calls.
            host.panel.continue_reads = None
            async with factory.begin() as session:
                if change == "manager":
                    contract = await session.get(Contract, draft.offer.contract_id)
                    assert contract is not None
                    await Contracts(ContractHost(host.service)).update(
                        session,
                        manager,
                        contract.id,
                        UpdateContract.model_validate(
                            {
                                **terms_of(contract).model_dump(),
                                "expected_version": 1,
                                "manager_user_id": replacement,
                            }
                        ),
                    )
                elif change == "ban":
                    await user_dal.update_user(session, manager, {"is_banned": True})
                else:
                    departure = await service.depart(
                        session, member, member_id, DepartMembership(request_id=uuid4())
                    )
            if change == "departure":
                async with factory.begin() as session:
                    leave_lease = await worker.claim(session, operation_id=departure.id)
                assert leave_lease is not None
                async with factory.begin() as session:
                    assert await worker.execute(session, leave_lease)
        finally:
            release.set()
            response = await asyncio.wait_for(pending, 5)
        if change == "ban":
            assert response.status == 403
        elif change == "manager" or source == "avatar":
            assert response.status == 404
        else:
            assert response.status == 200 and (await response.json())["members"] == []


async def test_pending_member_has_operation_and_unknown_statistics(
    client: TestClient, host: Host, telegram: tuple[Telegram, Bot]
) -> None:
    draft = await invitation(host.session, manager=MANAGER)
    confirmed = await Memberships(ContractHost(host.service)).confirm(host.session, MEMBER, draft)
    assert confirmed.operation is not None
    host.panel.requests.clear()
    path = paths(draft.offer.contract_id)[1]
    response = await client.get(path, headers=authorization(host, MANAGER))
    assert response.status == 200
    rows = (await response.json())["members"]
    assert len(rows) == 1
    row = rows[0]
    assert row["state"] == "pending" and row["joined_at"] is None
    assert row["operation"]["id"] == str(confirmed.operation.id)
    assert row["operation"]["state"] == "pending"
    assert row["avatar_path"] is None and row["profile"]["telegram_url"] is None
    assert all(
        stat == {"value": None, "state": "unavailable"} for stat in row["statistics"].values()
    )
    avatar = await client.get(path + f"/{row['id']}/avatar", headers=authorization(host, MANAGER))
    assert (await avatar.json())["avatar"] == {
        "state": "missing",
        "data_url": None,
        "updated_at": None,
    }
    assert not host.panel.requests and not telegram[0].calls


@pytest_asyncio.fixture(loop_scope="session")
async def committed_members(
    engine: AsyncEngine,
) -> AsyncIterator[tuple[async_sessionmaker[AsyncSession], list[UUID]]]:
    factory = async_sessionmaker(engine, expire_on_commit=False)
    identifiers: list[UUID] = []
    try:
        yield factory, identifiers
    finally:
        # Concurrency fixtures commit real transactions; retire only their own
        # records so later global sweep tests cannot pick them up.
        async with factory.begin() as session:
            for row in await session.scalars(
                select(Membership).where(Membership.id.in_(identifiers))
            ):
                row.current_user_id, row.state = None, "left"
                row.ended_at = datetime.now(UTC)
            for operation in await session.scalars(
                select(Operation).where(Operation.membership_id.in_(identifiers))
            ):
                if operation.state not in {"succeeded", "cancelled", "failed"}:
                    operation.state = "cancelled"
                    operation.lease_token = operation.lease_until = None
