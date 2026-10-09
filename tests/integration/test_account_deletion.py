"""Native deletion with installed schema, including with no plugin runtime loaded."""

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from bot.app.web.admin_api_impl.auth import admin_auth_middleware
from bot.app.web.admin_api_impl.users_actions import admin_user_delete_route
from bot.app.web.context import SESSION_FACTORY, SETTINGS, SUBSCRIPTION_SERVICE
from bot.app.web.webapp.assets import _csrf_protection_middleware
from bot.plugins.spec import WEB_SCOPE_WEBAPP, PluginContext
from bot.services.account_roles import grant_role
from db.dal import user_dal
from db.dal.user_merge_dal import delete_user_and_relations, merge_users
from db.models import Subscription
from minishop_corp import plugin
from minishop_corp.contracts import Contracts
from minishop_corp.contracts_types import ContractError, ContractPage
from minishop_corp.integration.contracts import ContractHost
from minishop_corp.integration.migrations import migrations
from minishop_corp.membership_types import DepartMembership
from minishop_corp.memberships import Memberships
from minishop_corp.operation_worker import OperationWorker
from minishop_corp.storage.reconciliation import candidates, schedule_member
from minishop_corp.storage.schema import (
    AuditEvent,
    Contract,
    Invitation,
    Membership,
    Operation,
    Reservation,
    Revision,
)
from sqlalchemy import delete as delete_rows
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from .conftest import USER_ID, Host
from .test_contracts import MANAGER, OTHER, authorization
from .test_memberships import execute, invitation
from .test_storage import seed_contract

pytestmark = pytest.mark.asyncio(loop_scope="session")


@pytest.mark.parametrize("kind", ["join", "leave", "reconcile"])
@pytest.mark.parametrize("delivery", ["pending", "running", "retry"])
async def test_deletion_cancels_delivery_and_recreation_cannot_restore_access(
    host: Host, kind: str, delivery: str
) -> None:
    native = ContractHost(host.service)
    service = Memberships(native)
    draft = await invitation(host.session)
    result = await service.confirm(host.session, USER_ID, draft)
    assert result.operation is not None
    operation_id, membership_id = result.operation.id, result.operation.membership_id
    member = await host.session.get(Membership, membership_id)
    assert member is not None
    if kind != "join":
        assert await execute(host, operation_id)
        if kind == "leave":
            departed = await service.depart(
                host.session, USER_ID, membership_id, DepartMembership(request_id=uuid4())
            )
            operation_id = departed.id
        else:
            member.scheduled_version = None
            await host.session.flush()
            scheduled = await schedule_member(host.session, membership_id, now=datetime.now(UTC))
            assert scheduled is not None
            operation_id = scheduled
    worker = OperationWorker(native)
    lease = await worker.claim(host.session, operation_id=operation_id)
    assert lease is not None
    operation = await host.session.get(Operation, operation_id)
    assert operation is not None
    if delivery != "running":
        operation.state = "retry" if delivery == "retry" else "pending"
        operation.lease_token = operation.lease_until = None
    await host.session.flush()
    generation = member.generation
    requests_before = list(host.panel.requests)
    assert await delete_user_and_relations(host.session, USER_ID)
    host.session.expire_all()
    member = await host.session.get(Membership, membership_id)
    operation = await host.session.get(Operation, operation_id)
    assert member is not None and member.state == "deleted"
    assert member.current_user_id is None and member.ended_at is not None
    assert member.generation == generation + 1
    assert operation is not None and operation.state == "cancelled"
    assert operation.error_code == "minishop_corp_account_deleted"
    assert operation.lease_token is None and operation.lease_until is None
    invite = await host.session.get(Invitation, draft.offer.invitation_id)
    reservation = await host.session.scalar(
        select(Reservation).where(Reservation.invitation_id == draft.offer.invitation_id)
    )
    assert invite is not None and invite.reserved_count == 0
    assert invite.used_count == (0 if kind == "join" else 1)
    assert reservation is not None and reservation.finished_at is not None
    assert reservation.state == ("released" if kind == "join" else "consumed")
    assert not await worker.execute(host.session, lease)
    assert await worker.claim(host.session, operation_id=operation_id) is None
    assert await candidates(host.session) == []
    assert await schedule_member(host.session, membership_id, now=datetime.now(UTC)) is None
    await user_dal.create_user(host.session, {"user_id": USER_ID})
    assert await service.current(host.session, USER_ID) is None
    assert await Contracts(native).managed_list(host.session, USER_ID, ContractPage()) == []
    with pytest.raises(ContractError, match="minishop_corp_membership_missing"):
        await service.operation(host.session, USER_ID, operation_id)
    with pytest.raises(ContractError, match="minishop_corp_membership_missing"):
        await service.confirm(host.session, USER_ID, draft)
    assert not await worker.execute(host.session, lease)
    assert host.panel.requests == requests_before
    assert await host.session.scalar(select(func.count()).select_from(Subscription)) == 0
    # Rejoining requires an actual new confirmation; an old lease cannot own it.
    rejoin = await service.confirm(
        host.session,
        USER_ID,
        draft.model_copy(update={"request_id": uuid4()}),
        now=datetime.now(UTC) + timedelta(minutes=2),
    )
    assert rejoin.operation is not None and rejoin.operation.membership_id != membership_id
    assert not await worker.execute(host.session, lease)


@pytest.mark.parametrize("enabled", [True, False])
async def test_native_http_deletes_member_and_manager_without_plugin_runtime(
    host: Host, enabled: bool
) -> None:
    for user_id in (MANAGER, OTHER):
        await user_dal.create_user(host.session, {"user_id": user_id})
    await grant_role(host.session, USER_ID, "admin", source="corp_test")
    draft = await invitation(host.session, manager=MANAGER)
    service = Memberships(ContractHost(host.service))
    result = await service.confirm(host.session, MANAGER, draft)
    assert result.operation is not None and await execute(host, result.operation.id)
    survivor = await service.confirm(
        host.session, OTHER, draft.model_copy(update={"request_id": uuid4()})
    )
    assert survivor.operation is not None and await execute(host, survivor.operation.id)
    others = list(
        (
            await host.session.scalars(select(Subscription).where(Subscription.user_id == OTHER))
        ).all()
    )
    assert len(others) == 1 and len(host.panel.users) == 2
    other_end = others[0].end_date
    app = web.Application(middlewares=[_csrf_protection_middleware, admin_auth_middleware])
    app[SETTINGS], app[SUBSCRIPTION_SERVICE] = host.settings, host.service
    app[SESSION_FACTORY] = async_sessionmaker(
        bind=host.session.bind, expire_on_commit=False, join_transaction_mode="create_savepoint"
    )
    if enabled:
        plugin.setup_web(PluginContext(host.settings), app, scope=WEB_SCOPE_WEBAPP)
    app.router.add_delete("/api/admin/users/{user_id}", admin_user_delete_route)
    async with TestClient(TestServer(app)) as client:
        response = await client.delete(f"/api/admin/users/{MANAGER}", headers=authorization(host))
        assert response.status == 200, await response.text()
    host.session.expire_all()
    assert await user_dal.get_user_by_id(host.session, MANAGER) is None
    assert len(host.panel.users) == 1
    contract = await host.session.get(Contract, draft.offer.contract_id)
    assert contract is not None and contract.manager_user_id is None and contract.version == 1
    member = await host.session.get(Membership, result.operation.membership_id)
    assert member is not None and member.state == "deleted"
    other_member = await host.session.get(Membership, survivor.operation.membership_id)
    assert other_member is not None and other_member.state == "active"
    other = await host.session.scalar(select(Subscription).where(Subscription.user_id == OTHER))
    assert other is not None and other.end_date == other_end
    assert await candidates(host.session) == []


async def test_rollback_native_deletion_restores_roles_jobs_and_reservations(host: Host) -> None:
    draft = await invitation(host.session)
    result = await Memberships(ContractHost(host.service)).confirm(host.session, USER_ID, draft)
    assert result.operation is not None
    savepoint = await host.session.begin_nested()
    assert await delete_user_and_relations(host.session, USER_ID)
    await savepoint.rollback()
    host.session.expire_all()
    assert await user_dal.get_user_by_id(host.session, USER_ID) is not None
    contract = await host.session.get(Contract, draft.offer.contract_id)
    member = await host.session.get(Membership, result.operation.membership_id)
    operation = await host.session.get(Operation, result.operation.id)
    invite = await host.session.get(Invitation, draft.offer.invitation_id)
    assert contract is not None and contract.manager_user_id == USER_ID
    assert member is not None and member.state == "pending" and member.current_user_id == USER_ID
    assert operation is not None and operation.state == "pending"
    assert invite is not None and invite.reserved_count == 1 and invite.used_count == 0
    assert (
        await host.session.scalar(
            select(func.count())
            .select_from(AuditEvent)
            .where(
                AuditEvent.action.in_(("membership_account_deleted", "contract_manager_deleted"))
            )
        )
        == 0
    )


async def test_upgrade_populated_0004_then_delete_and_replay(host: Host) -> None:
    schema = "ext_minishop_corp_upgrade_" + uuid4().hex
    async with host.session.begin_nested():
        await host.session.execute(text(f'CREATE SCHEMA "{schema}"'))
        await host.session.execute(text(f'SET LOCAL search_path TO "{schema}", public'))
        connection = await host.session.connection()
        for migration in migrations()[:4]:
            await connection.run_sync(migration.upgrade)
        draft = await invitation(host.session)
        result = await Memberships(ContractHost(host.service)).confirm(host.session, USER_ID, draft)
        assert result.operation is not None
        # The installed data is committed before a real upgrade. Drain the
        # deferred revision check in this rollback-only test transaction too.
        await host.session.execute(
            text("SET CONSTRAINTS ext_minishop_corp_current_revision IMMEDIATE")
        )
        await host.session.execute(
            text("SET CONSTRAINTS ext_minishop_corp_current_revision DEFERRED")
        )
        savepoint = await host.session.begin_nested()
        # Materialize the session's lazy SAVEPOINT before using its connection
        # directly for DDL; otherwise only the subsequent DAL deletion rolls back.
        connection = await host.session.connection()
        await connection.run_sync(migrations()[4].upgrade)
        assert await delete_user_and_relations(host.session, USER_ID)
        await savepoint.rollback()
        host.session.expire_all()
        assert await user_dal.get_user_by_id(host.session, USER_ID) is not None
        assert (
            await host.session.scalar(
                text("""
            SELECT confdeltype::text FROM pg_constraint
            WHERE conrelid = 'ext_minishop_corp_contracts'::regclass
            AND conname = 'ext_minishop_corp_contracts_manager_user_id_fkey'
        """)
            )
            == "r"
        )
        await connection.run_sync(migrations()[4].upgrade)
        assert await delete_user_and_relations(host.session, USER_ID)
        host.session.expire_all()
        contract = await host.session.get(Contract, draft.offer.contract_id)
        member = await host.session.get(Membership, result.operation.membership_id)
        revision = await host.session.get(Revision, (draft.offer.contract_id, 1))
        invite = await host.session.get(Invitation, draft.offer.invitation_id)
        assert contract is not None and contract.manager_user_id is None
        assert member is not None and member.state == "deleted"
        assert revision is not None and revision.manager_user_id == USER_ID
        assert invite is not None and invite.reserved_count == 0


async def test_merge_keeps_target_membership_and_does_not_transfer_manager_role(host: Host) -> None:
    await user_dal.create_user(host.session, {"user_id": OTHER})
    source = await seed_contract(host.session)
    target = await seed_contract(host.session, manager=OTHER)
    for user_id, contract in ((USER_ID, source), (OTHER, target)):
        host.session.add(
            Membership(
                contract_id=contract.id, user_id=user_id, current_user_id=user_id, state="active"
            )
        )
    await host.session.flush()
    source_id, target_id = source.id, target.id
    await merge_users(host.session, source_user_id=USER_ID, target_user_id=OTHER)
    host.session.expire_all()
    source = await host.session.get(Contract, source_id)
    target = await host.session.get(Contract, target_id)
    assert source is not None and source.manager_user_id is None
    assert target is not None and target.manager_user_id == OTHER
    current = await host.session.scalar(
        select(Membership).where(Membership.current_user_id == OTHER)
    )
    assert current is not None and current.contract_id == target_id and current.state == "active"


async def test_concurrent_delete_and_other_member_delivery_share_invitation_safely(
    host: Host, engine: AsyncEngine
) -> None:
    factory = async_sessionmaker(engine, expire_on_commit=False)
    first = uuid4().int % 1_000_000_000 + 100_000_000
    native = ContractHost(host.service)
    service, worker = Memberships(native), OperationWorker(native)
    async with factory.begin() as session:
        for user_id in (first, first + 1, first + 2):
            await user_dal.create_user(session, {"user_id": user_id})
        draft = await invitation(session, manager=first + 2)
        pending = []
        for user_id in (first, first + 1):
            result = await service.confirm(
                session, user_id, draft.model_copy(update={"request_id": uuid4()})
            )
            assert result.operation is not None
            pending.append(result.operation)
        lease = await worker.claim(session, operation_id=pending[1].id)
        assert lease is not None
    barrier = asyncio.Barrier(2)

    async def delete() -> None:
        async with factory.begin() as session:
            await barrier.wait()
            assert await delete_user_and_relations(session, first)

    async def deliver() -> None:
        async with factory.begin() as session:
            await barrier.wait()
            assert await worker.execute(session, lease)

    try:
        await asyncio.wait_for(asyncio.gather(delete(), deliver()), timeout=15)
        async with factory() as session:
            invite = await session.get(Invitation, draft.offer.invitation_id)
            assert invite is not None and invite.reserved_count == 0 and invite.used_count == 1
            member = await session.get(Membership, pending[0].membership_id)
            survivor = await session.get(Membership, pending[1].membership_id)
            assert member is not None and member.state == "deleted"
            assert survivor is not None and survivor.state == "active"
    finally:
        # This scenario commits to exercise concurrent connections. Remove its
        # own rows so later tests of the global sweep see only their fixtures.
        async with factory.begin() as session:
            for model in (AuditEvent, Reservation, Operation, Membership, Invitation, Revision):
                await session.execute(
                    delete_rows(model).where(model.contract_id == draft.offer.contract_id)
                )
            await session.execute(
                delete_rows(Contract).where(Contract.id == draft.offer.contract_id)
            )
            for user_id in (first, first + 1, first + 2):
                await delete_user_and_relations(session, user_id)
