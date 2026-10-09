"""Core rewards, multi-member renewal and loss of a real worker process."""

import asyncio
import json
import sys
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from bot.plugins.extensions.contracts import ExtensionContributions
from bot.plugins.extensions.registry import ExtensionRegistry, get_registry, set_registry
from bot.plugins.extensions.rewards import Reward, grant
from bot.services.account_roles import grant_role
from db.dal import user_dal
from db.dal.user_merge_dal import delete_user_and_relations
from db.models import Subscription
from minishop_corp.integration.access import AccessAdapter
from minishop_corp.integration.access_types import panel_time
from minishop_corp.integration.contracts import ContractHost
from minishop_corp.membership_types import ConfirmMembership, DepartMembership
from minishop_corp.memberships import Memberships
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
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from .conftest import USER_ID, Host
from .test_memberships import execute, invitation, joined
from .test_reconciliation import due, revise

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def test_core_reward_days_are_reconciled_without_reissuing_reward(host: Host) -> None:
    previous = get_registry()
    owner = "corp-reward-fixture"
    registry = ExtensionRegistry()
    registry.register(
        owner, "1.0.0", ExtensionContributions(permissions=frozenset({"rewards.days"}))
    )
    set_registry(registry)
    try:
        draft, member_id = await joined(host)
        access = AccessAdapter(host.service)
        before = await access.read(host.session, USER_ID)
        assert before is not None
        reward = Reward(kind="days", amount=7)
        key = "acceptance:" + uuid4().hex
        first = await grant(
            host.session, owner=owner, user_id=USER_ID, idempotency_key=key, reward=reward
        )
        repeated = await grant(
            host.session, owner=owner, user_id=USER_ID, idempotency_key=key, reward=reward
        )
        assert repeated.id == first.id
        drifted = await access.read(host.session, USER_ID)
        assert drifted is not None and drifted.end_date == before.end_date + timedelta(days=7)
        # This is the same native sync invoked by Core's extension reward worker.
        assert await host.service.sync_main_traffic_limit_to_panel(host.session, USER_ID)
        assert await execute(host, await due(host, member_id))
        repeated = await grant(
            host.session, owner=owner, user_id=USER_ID, idempotency_key=key, reward=reward
        )
        assert repeated.id == first.id
        restored = await access.read(host.session, USER_ID)
        assert restored is not None and restored.end_date == before.end_date
        assert (
            datetime.fromisoformat(str(host.panel.users[before.panel_user_uuid]["expireAt"]))
            == before.end_date
        )
        invite = await host.session.get(Invitation, draft.offer.invitation_id)
        assert invite is not None and invite.used_count == 1
    finally:
        set_registry(previous)


async def test_renewal_applies_exact_terms_to_every_current_member(host: Host) -> None:
    await grant_role(host.session, USER_ID, "admin", source="corp_acceptance_fixture")
    draft = await invitation(host.session)
    service = Memberships(ContractHost(host.service))
    members: list[UUID] = []
    accounts = [960100 + index for index in range(5)]
    for user_id in accounts:
        await user_dal.create_user(host.session, {"user_id": user_id, "telegram_id": user_id})
        result = await service.confirm(
            host.session, user_id, draft.model_copy(update={"request_id": uuid4()})
        )
        assert result.operation is not None
        assert await execute(host, result.operation.id)
        members.append(result.operation.membership_id)
    departure = await service.depart(
        host.session, accounts[-1], members[-1], DepartMembership(request_id=uuid4())
    )
    assert await execute(host, departure.id)
    former = await AccessAdapter(host.service).read(host.session, accounts[-1])
    target = panel_time(datetime.now(UTC) + timedelta(days=100))
    await revise(host, draft.offer.contract_id, ends_at=target)
    assert set(await candidates(host.session)) == set(members[:-1])
    for member_id in members[:-1]:
        operation = await schedule_member(host.session, member_id, now=datetime.now(UTC))
        assert operation is not None and await execute(host, operation)
    for user_id in accounts[:-1]:
        state = await AccessAdapter(host.service).read(host.session, user_id)
        assert state is not None and state.end_date == target
        assert (
            datetime.fromisoformat(str(host.panel.users[state.panel_user_uuid]["expireAt"]))
            == target
        )
    assert await AccessAdapter(host.service).read(host.session, accounts[-1]) == former


async def child(host: Host, engine: AsyncEngine, operation: UUID, *, deliver: bool) -> None:
    settings = host.settings.model_dump(mode="json")
    # Core's output model includes computed aliases that are not Settings inputs.
    settings.pop("ADMIN_IDS", None)
    settings["POSTGRES_DB"] = engine.url.database
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "tests/integration/worker_process.py",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        assert process.stdin is not None and process.stdout is not None
        process.stdin.write(
            json.dumps(
                {"settings": settings, "operation": str(operation), "deliver": deliver}
            ).encode()
            + b"\n"
        )
        await process.stdin.drain()
        if deliver:
            output, error = await asyncio.wait_for(process.communicate(), 30)
            assert process.returncode == 0, error.decode()
            assert b"DELIVERED" in output
        else:
            line = await asyncio.wait_for(process.stdout.readline(), 30)
            assert line == b"CLAIMED\n"
            process.kill()
            await process.communicate()
            assert process.returncode == -9
    finally:
        if process.returncode is None:
            process.kill()
            await process.communicate()


@pytest.mark.parametrize("kind", ["join", "leave"])
async def test_process_death_recovers_one_membership_and_one_trial(
    engine: AsyncEngine, host: Host, kind: str
) -> None:
    factory = async_sessionmaker(engine, expire_on_commit=False)
    user_id = 970001 if kind == "join" else 970002
    service = Memberships(ContractHost(host.service))
    try:
        async with factory.begin() as session:
            await user_dal.create_user(session, {"user_id": user_id, "telegram_id": user_id})
            draft: ConfirmMembership = await invitation(session, manager=user_id)
            joined = await service.confirm(session, user_id, draft)
            assert joined.operation is not None
            operation = joined.operation.id
        if kind == "leave":
            await child(host, engine, operation, deliver=True)
            async with factory.begin() as session:
                departed = await service.depart(
                    session,
                    user_id,
                    joined.operation.membership_id,
                    DepartMembership(request_id=uuid4()),
                )
                operation = departed.id
        await child(host, engine, operation, deliver=False)
        async with factory() as session:
            saved = await session.get(Operation, operation)
            assert saved is not None and saved.state == "running" and saved.attempts == 1
            target = saved.target
        await child(host, engine, operation, deliver=True)
        async with factory() as session:
            saved = await session.get(Operation, operation)
            member = await session.get(Membership, joined.operation.membership_id)
            invite = await session.get(Invitation, draft.offer.invitation_id)
            assert saved is not None and saved.state == "succeeded" and saved.target == target
            assert saved.attempts == 2 and member is not None
            assert member.state == ("active" if kind == "join" else "left")
            assert invite is not None and (invite.used_count, invite.reserved_count) == (1, 0)
            assert await session.scalar(
                select(func.count())
                .select_from(Subscription)
                .where(Subscription.user_id == user_id, Subscription.provider == "trial")
            ) == (0 if kind == "join" else 1)
    finally:
        # These processes commit real data; remove only this fixture's namespace
        # before other acceptance cases inspect global counts or the shared sweep.
        async with factory.begin() as session:
            identifiers = list(
                await session.scalars(
                    select(Contract.id).where(Contract.manager_user_id == user_id)
                )
            )
            for model in (AuditEvent, Reservation, Operation, Membership, Invitation, Revision):
                await session.execute(delete(model).where(model.contract_id.in_(identifiers)))
            await session.execute(delete(Contract).where(Contract.id.in_(identifiers)))
            await delete_user_and_relations(session, user_id)
