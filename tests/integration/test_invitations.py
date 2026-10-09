import asyncio
import json
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from aiohttp.test_utils import TestClient
from bot.services.account_roles import grant_role
from db.dal import user_dal
from minishop_corp.contracts_types import ContractError
from minishop_corp.integration.contracts import ContractHost
from minishop_corp.invitations import Invitations
from minishop_corp.invitations_types import CreateInvitation, RotateInvitation, code_digest
from minishop_corp.storage.invitations import (
    lookup_invitation,
    reserve_invitation,
    settle_reservation,
)
from minishop_corp.storage.operations import OperationDraft, PeriodTarget, prepare_operation
from minishop_corp.storage.schema import AuditEvent, Contract, Invitation, Membership, Operation
from pydantic import SecretStr
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from .conftest import USER_ID, Host
from .test_contracts import ADMIN_PATH, MANAGER, MEMBER, OTHER, USER_PATH, authorization, draft
from .test_contracts import client as client
from .test_storage import seed_contract

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def create_api_contract(client: TestClient, host: Host) -> UUID:
    payload = draft()
    result = await client.post(ADMIN_PATH, json=payload, headers=authorization(host))
    assert result.status == 201
    return UUID(str(payload["id"]))


async def pending_join(
    session: AsyncSession,
    user_id: int,
    contract_id: UUID,
    invitation_id: UUID,
) -> Operation:
    await user_dal.lock_user_by_id(session, user_id)
    contract = await session.get(
        Contract, contract_id, populate_existing=True, with_for_update=True
    )
    assert contract is not None
    member = Membership(
        contract_id=contract_id,
        user_id=user_id,
        current_user_id=user_id,
        invitation_id=invitation_id,
    )
    session.add(member)
    await session.flush()
    return await prepare_operation(
        session,
        OperationDraft(
            contract_id=contract_id,
            contract_version=contract.version,
            membership_id=member.id,
            membership_generation=1,
            user_id=user_id,
            actor_user_id=user_id,
            request_id=uuid4(),
            kind="join",
            target=PeriodTarget(
                tariff_key=contract.tariff_key,
                ends_at=contract.ends_at,
                external_squad_uuid=contract.external_squad_uuid,
            ),
        ),
    )


@pytest.mark.parametrize("kind,limit", [("single", 1), ("reusable", 5)])
async def test_create_codes_metadata_and_retries(
    client: TestClient,
    host: Host,
    kind: str,
    limit: int,
) -> None:
    contract_id = await create_api_contract(client, host)
    path = f"{ADMIN_PATH}/{contract_id}/invitations"
    payload = {"id": str(uuid4()), "kind": kind, "use_limit": limit}
    first = await client.post(path, json=payload, headers=authorization(host))
    assert first.status == 201, await first.text()
    result = await first.json()
    secret = SecretStr(result["code"])
    assert len(secret.get_secret_value()) == 37
    stored = await host.session.get(Invitation, UUID(payload["id"]))
    assert stored is not None and stored.code_digest == code_digest(secret)
    assert stored.used_count == stored.reserved_count == 0
    again = await client.post(path, json=payload, headers=authorization(host))
    assert again.status == 200
    assert (await again.json())["code"] == secret.get_secret_value()
    rows = (await (await client.get(path, headers=authorization(host))).json())["invitations"]
    assert len(rows) == 1 and "code_digest" not in rows[0]
    assert rows[0]["code"] == secret.get_secret_value()
    assert stored.code_value == secret.get_secret_value()
    assert first.headers["Cache-Control"] == "private, no-store"
    manager_path = f"{USER_PATH}/managed-contracts/{contract_id}/invitations"
    for account in (MEMBER, OTHER):
        denied = await client.get(manager_path, headers=authorization(host, account))
        assert denied.status == 404 and secret.get_secret_value() not in await denied.text()
    managed = await client.get(manager_path, headers=authorization(host, MANAGER))
    managed_rows = (await managed.json())["invitations"]
    assert managed_rows == ([] if kind == "single" else rows)
    events = list(
        await host.session.scalars(
            select(AuditEvent.details).where(AuditEvent.contract_id == contract_id)
        )
    )
    assert secret.get_secret_value() not in json.dumps(events)
    assert stored.code_digest not in json.dumps(events)
    before = await host.session.scalar(select(func.count()).select_from(Invitation))
    assert (
        await client.head(path, json={**payload, "id": str(uuid4())}, headers=authorization(host))
    ).status == 200
    assert await host.session.scalar(select(func.count()).select_from(Invitation)) == before


async def test_rotation_is_scoped_and_keeps_membership(client: TestClient, host: Host) -> None:
    contract_id = await create_api_contract(client, host)
    panel_requests = list(host.panel.requests)
    invitations = Invitations(ContractHost(host.service))
    issued = await invitations.create(
        host.session,
        USER_ID,
        contract_id,
        CreateInvitation(id=uuid4(), kind="reusable", use_limit=4),
    )
    assert issued.code is not None
    member = Membership(
        contract_id=contract_id, user_id=MEMBER, current_user_id=MEMBER, state="active"
    )
    host.session.add(member)
    await host.session.flush()
    path = f"{USER_PATH}/managed-contracts/{contract_id}/invitations/{issued.invitation.id}/rotate"
    payload = {"id": str(uuid4())}
    assert (await client.post(path, json=payload, headers=authorization(host, OTHER))).status == 404
    assert (
        await client.post(path, json=payload, headers=authorization(host, MEMBER))
    ).status == 404
    assert (
        await client.post(
            path, json={**payload, "use_limit": 100}, headers=authorization(host, MANAGER)
        )
    ).status == 400
    result = await client.post(path, json=payload, headers=authorization(host, MANAGER))
    assert result.status == 201, await result.text()
    new = await result.json()
    assert new["invitation"]["use_limit"] == 4
    assert new["invitation"]["used_count"] == 0
    assert (
        await client.post(path, json=payload, headers=authorization(host, MANAGER))
    ).status == 200
    assert (
        await client.post(path, json={"id": str(uuid4())}, headers=authorization(host, MANAGER))
    ).status == 409
    with pytest.raises(ContractError, match="invitation_unavailable"):
        await lookup_invitation(host.session, issued.code)
    assert (
        await lookup_invitation(host.session, SecretStr(new["code"]))
    ).contract.id == contract_id
    await host.session.refresh(member)
    assert member.state == "active" and member.current_user_id == MEMBER
    assert host.panel.requests == panel_requests  # Rotation only uses plugin data.


async def test_revoke_between_preview_and_confirmation(client: TestClient, host: Host) -> None:
    contract_id = await create_api_contract(client, host)
    service = Invitations(ContractHost(host.service))
    issued = await service.create(
        host.session, USER_ID, contract_id, CreateInvitation(id=uuid4(), kind="single", use_limit=1)
    )
    assert issued.code is not None
    await user_dal.lock_user_by_id(host.session, MEMBER)
    offer = await lookup_invitation(host.session, issued.code)
    assert offer.contract.id == contract_id
    operation = await pending_join(host.session, MEMBER, contract_id, offer.invitation_id)
    await service.revoke(host.session, USER_ID, contract_id, issued.invitation.id)
    await service.revoke(host.session, USER_ID, contract_id, issued.invitation.id)
    with pytest.raises(ContractError, match="invitation_unavailable"):
        await reserve_invitation(
            host.session,
            user_id=MEMBER,
            invitation_id=issued.invitation.id,
            operation_id=operation.id,
        )
    assert (
        await host.session.scalar(
            select(func.count())
            .select_from(AuditEvent)
            .where(AuditEvent.contract_id == contract_id, AuditEvent.action == "invitation_revoked")
        )
        == 1
    )


@pytest.mark.parametrize("terminal", ["succeeded", "failed", "cancelled"])
async def test_reservation_requires_resolved_operation_and_settles_once(
    client: TestClient,
    host: Host,
    terminal: str,
) -> None:
    contract_id = await create_api_contract(client, host)
    service = Invitations(ContractHost(host.service))
    issued = await service.create(
        host.session, USER_ID, contract_id, CreateInvitation(id=uuid4(), kind="single", use_limit=1)
    )
    operation = await pending_join(host.session, MEMBER, contract_id, issued.invitation.id)
    args = {"user_id": MEMBER, "invitation_id": issued.invitation.id, "operation_id": operation.id}
    reservation = await reserve_invitation(host.session, **args)
    assert (await reserve_invitation(host.session, **args)).id == reservation.id
    row = await host.session.get(Invitation, issued.invitation.id)
    assert row is not None and row.reserved_count == 1 and row.used_count == 0
    assert issued.code is not None
    with pytest.raises(ContractError, match="invitation_unavailable"):
        await lookup_invitation(host.session, issued.code)
    operation.state = "retry"
    await host.session.flush()
    with pytest.raises(ContractError, match="operation_unresolved"):
        await settle_reservation(host.session, **args)
    await service.revoke(host.session, USER_ID, contract_id, row.id)
    assert (await reserve_invitation(host.session, **args)).id == reservation.id
    operation.state = terminal
    await host.session.flush()
    await settle_reservation(host.session, **args)
    await settle_reservation(host.session, **args)
    assert row.reserved_count == 0 and row.used_count == (1 if terminal == "succeeded" else 0)
    member = await host.session.get(Membership, operation.membership_id)
    assert member is not None
    member.state, member.current_user_id, member.ended_at = "left", None, datetime.now(UTC)
    await host.session.flush()
    await settle_reservation(host.session, **args)
    assert row.used_count == (1 if terminal == "succeeded" else 0)


async def test_reservation_transaction_rollback_frees_capacity(
    client: TestClient, host: Host
) -> None:
    contract_id = await create_api_contract(client, host)
    issued = await Invitations(ContractHost(host.service)).create(
        host.session, USER_ID, contract_id, CreateInvitation(id=uuid4(), kind="single", use_limit=1)
    )
    operation = await pending_join(host.session, MEMBER, contract_id, issued.invitation.id)
    with pytest.raises(RuntimeError, match="rollback"):
        async with host.session.begin_nested():
            await reserve_invitation(
                host.session,
                user_id=MEMBER,
                invitation_id=issued.invitation.id,
                operation_id=operation.id,
            )
            raise RuntimeError("rollback")
    row = await host.session.get(Invitation, issued.invitation.id, populate_existing=True)
    assert row is not None and row.reserved_count == row.used_count == 0


async def test_expiration_and_invalid_generation(client: TestClient, host: Host) -> None:
    contract_id = await create_api_contract(client, host)
    service = Invitations(ContractHost(host.service))
    issued = await service.create(
        host.session, USER_ID, contract_id, CreateInvitation(id=uuid4(), kind="single", use_limit=1)
    )
    operation = await pending_join(host.session, MEMBER, contract_id, issued.invitation.id)
    future = datetime(2040, 1, 1, tzinfo=UTC)
    assert issued.code is not None
    with pytest.raises(ContractError, match="invitation_unavailable"):
        await lookup_invitation(host.session, issued.code, now=future)
    with pytest.raises(ContractError, match="invitation_unavailable"):
        await reserve_invitation(
            host.session,
            user_id=MEMBER,
            invitation_id=issued.invitation.id,
            operation_id=operation.id,
            now=future,
        )
    member = await host.session.get(Membership, operation.membership_id)
    assert member is not None
    member.generation += 1
    await host.session.flush()
    with pytest.raises(ContractError, match="operation_stale"):
        await reserve_invitation(
            host.session,
            user_id=MEMBER,
            invitation_id=issued.invitation.id,
            operation_id=operation.id,
        )


@pytest.mark.parametrize("kind", ["single", "reusable"])
async def test_parallel_last_slot_has_one_winner(
    engine: AsyncEngine, host: Host, kind: str
) -> None:
    factory = async_sessionmaker(engine, expire_on_commit=False)
    base = 980100 if kind == "single" else 980200
    service = Invitations(ContractHost(host.service))
    async with factory.begin() as session:
        for user_id in (base, base + 1, base + 2):
            await user_dal.create_user(session, {"user_id": user_id})
        await grant_role(session, base, "admin", source="corp_invite_race")
        contract = await seed_contract(session, base)
        issued = await service.create(
            session, base, contract.id, CreateInvitation(id=uuid4(), kind=kind, use_limit=1)
        )
    barrier = asyncio.Barrier(2)

    async def join(user_id: int) -> int:
        try:
            async with factory.begin() as session:
                await barrier.wait()
                operation = await pending_join(session, user_id, contract.id, issued.invitation.id)
                await reserve_invitation(
                    session,
                    user_id=user_id,
                    invitation_id=issued.invitation.id,
                    operation_id=operation.id,
                )
            return 200
        except ContractError as exc:
            return exc.status

    assert sorted(await asyncio.gather(join(base + 1), join(base + 2))) == [200, 400]
    async with factory() as session:
        row = await session.get(Invitation, issued.invitation.id)
        assert row is not None and row.reserved_count == 1 and row.used_count == 0
        assert (
            await session.scalar(
                select(func.count())
                .select_from(Membership)
                .where(Membership.contract_id == contract.id)
            )
            == 1
        )


async def test_parallel_rotation_keeps_one_active_code(engine: AsyncEngine, host: Host) -> None:
    factory = async_sessionmaker(engine, expire_on_commit=False)
    actor = 980300
    service = Invitations(ContractHost(host.service))
    async with factory.begin() as session:
        await user_dal.create_user(session, {"user_id": actor})
        await grant_role(session, actor, "admin", source="corp_rotate_race")
        contract = await seed_contract(session, actor)
        issued = await service.create(
            session, actor, contract.id, CreateInvitation(id=uuid4(), kind="reusable", use_limit=2)
        )
    barrier = asyncio.Barrier(2)

    async def rotate() -> int:
        try:
            async with factory.begin() as session:
                await barrier.wait()
                await service.rotate(
                    session, actor, contract.id, issued.invitation.id, RotateInvitation(id=uuid4())
                )
            return 200
        except ContractError as exc:
            return exc.status

    assert sorted(await asyncio.gather(rotate(), rotate())) == [200, 409]
    async with factory() as session:
        assert (
            await session.scalar(
                select(func.count())
                .select_from(Invitation)
                .where(Invitation.contract_id == contract.id, Invitation.revoked_at.is_(None))
            )
            == 1
        )
