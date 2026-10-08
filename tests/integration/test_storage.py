import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from db.dal import user_dal
from db.dal.user_merge_dal import delete_user_and_relations, merge_users
from minishop_corp.storage.schema import Contract, Invitation, Membership, Revision
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from .conftest import EXTERNAL_SQUAD, USER_ID, Host

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def seed_contract(session: AsyncSession, manager: int = USER_ID) -> Contract:
    contract = Contract(
        name="Synthetic organization",
        tariff_key="corp",
        external_squad_uuid=UUID(EXTERNAL_SQUAD),
        ends_at=datetime.now(UTC) + timedelta(days=30),
        manager_user_id=manager,
    )
    session.add(contract)
    await session.flush()
    session.add(
        Revision(
            contract_id=contract.id,
            version=contract.version,
            name=contract.name,
            tariff_key=contract.tariff_key,
            external_squad_uuid=contract.external_squad_uuid,
            ends_at=contract.ends_at,
            manager_user_id=manager,
            actor_user_id=manager,
        )
    )
    await session.flush()
    return contract


async def test_native_migration_chain_rolled_back_then_replayed(engine: AsyncEngine) -> None:
    # The session fixture runs the rollback + first apply + second apply with the native runner.
    async with engine.connect() as connection:
        assert (
            await connection.scalar(
                text("SELECT count(*) FROM schema_migrations WHERE id LIKE 'minishop-corp.%'")
            )
            == 4
        )
        names = (
            await connection.scalars(
                text("SELECT tablename FROM pg_tables WHERE tablename LIKE 'ext_minishop_corp_%'")
            )
        ).all()
        assert len(names) == 8


async def test_constraints_and_historical_departure(host: Host) -> None:
    first = await seed_contract(host.session)
    second = await seed_contract(host.session)
    member = Membership(contract_id=first.id, user_id=USER_ID, current_user_id=USER_ID)
    host.session.add(member)
    await host.session.flush()
    with pytest.raises(IntegrityError):
        async with host.session.begin_nested():
            host.session.add(
                Membership(contract_id=second.id, user_id=USER_ID, current_user_id=USER_ID)
            )
            await host.session.flush()
    member.state = "left"
    member.current_user_id = None
    member.ended_at = datetime.now(UTC)
    await host.session.flush()
    host.session.add(Membership(contract_id=second.id, user_id=USER_ID, current_user_id=USER_ID))
    await host.session.flush()
    assert (
        await host.session.scalar(
            select(func.count()).select_from(Membership).where(Membership.user_id == USER_ID)
        )
        == 2
    )
    await host.session.execute(text("SET CONSTRAINTS ext_minishop_corp_current_revision IMMEDIATE"))


@pytest.mark.parametrize("bad", ["single_limit", "capacity", "foreign_contract", "identity"])
async def test_reject_invalid_invitation_or_membership(host: Host, bad: str) -> None:
    contract = await seed_contract(host.session)
    other = await seed_contract(host.session)
    invitation = Invitation(
        contract_id=contract.id,
        kind="single",
        code_digest="a" * 64,
        use_limit=1,
        created_by=USER_ID,
    )
    host.session.add(invitation)
    await host.session.flush()
    with pytest.raises(IntegrityError):
        async with host.session.begin_nested():
            if bad == "single_limit":
                invitation.use_limit = 2
            elif bad == "capacity":
                invitation.used_count = invitation.reserved_count = 1
            else:
                host.session.add(
                    Membership(
                        contract_id=other.id if bad == "foreign_contract" else contract.id,
                        user_id=USER_ID + 1 if bad == "identity" else USER_ID,
                        current_user_id=USER_ID,
                        invitation_id=invitation.id,
                    )
                )
            await host.session.flush()


@pytest.mark.parametrize("role", ["manager", "member"])
@pytest.mark.parametrize("action", ["delete", "merge"])
async def test_native_account_removal_cannot_drop_current_links(
    host: Host, role: str, action: str
) -> None:
    target_id = USER_ID + 1
    await user_dal.create_user(host.session, {"user_id": target_id})
    contract = await seed_contract(host.session, USER_ID if role == "manager" else target_id)
    if role == "member":
        host.session.add(
            Membership(contract_id=contract.id, user_id=USER_ID, current_user_id=USER_ID)
        )
        await host.session.flush()
    with pytest.raises(IntegrityError):
        async with host.session.begin_nested():
            if action == "delete":
                await delete_user_and_relations(host.session, USER_ID)
            else:
                await merge_users(host.session, source_user_id=USER_ID, target_user_id=target_id)
    assert await user_dal.get_user_by_id(host.session, USER_ID) is not None
    assert await user_dal.get_user_by_id(host.session, target_id) is not None
    if role == "manager":
        await host.session.refresh(contract)
        assert contract.manager_user_id == USER_ID
    else:
        assert (
            await host.session.scalar(
                select(Membership.current_user_id).where(Membership.contract_id == contract.id)
            )
            == USER_ID
        )


async def test_concurrent_membership_uses_one_account_slot(engine: AsyncEngine) -> None:
    factory = async_sessionmaker(engine, expire_on_commit=False)
    # This test needs committed rows and separate connections, unlike the savepoint host fixture.
    user_id = 930001
    async with factory.begin() as session:
        await user_dal.create_user(session, {"user_id": user_id})
        contracts = [(await seed_contract(session, user_id)).id for _ in range(2)]
    gate = asyncio.Barrier(2)

    async def attempt(contract_id: UUID) -> bool:
        try:
            async with factory.begin() as session:
                await gate.wait()
                session.add(
                    Membership(contract_id=contract_id, user_id=user_id, current_user_id=user_id)
                )
                await session.flush()
            return True
        except IntegrityError:
            return False

    assert sorted(await asyncio.gather(*(attempt(item) for item in contracts))) == [False, True]
    async with factory() as session:
        assert (
            await session.scalar(
                select(func.count())
                .select_from(Membership)
                .where(Membership.current_user_id == user_id)
            )
            == 1
        )
    # All committed fixtures live only in the disposable per-run database.


async def test_former_member_and_manager_history_does_not_block_deletion(host: Host) -> None:
    successor = USER_ID + 1
    await user_dal.create_user(host.session, {"user_id": successor})
    contract = await seed_contract(host.session)
    contract.manager_user_id = successor
    host.session.add(
        Membership(
            contract_id=contract.id,
            user_id=USER_ID,
            current_user_id=None,
            state="left",
            ended_at=datetime.now(UTC),
        )
    )
    await host.session.flush()
    assert await delete_user_and_relations(host.session, USER_ID)
    assert await user_dal.get_user_by_id(host.session, USER_ID) is None
    assert (
        await host.session.scalar(
            select(Membership.user_id).where(Membership.contract_id == contract.id)
        )
        == USER_ID
    )
    assert (
        await host.session.scalar(
            select(Revision.manager_user_id).where(Revision.contract_id == contract.id)
        )
        == USER_ID
    )
