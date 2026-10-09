import asyncio
from uuid import uuid4

import pytest
from aiohttp.test_utils import TestClient
from bot.app.web.webapp_auth import create_webapp_session_token
from bot.services.account_roles import grant_role
from db.auth_models import AccountRole, AccountRoleEvent
from db.dal import user_dal
from minishop_corp.contracts import Contracts
from minishop_corp.contracts_types import ContractError
from minishop_corp.integration.access import AccessAdapter
from minishop_corp.integration.contracts import ContractHost
from minishop_corp.invitation_lookup import InvitationLookup
from minishop_corp.invitations import Invitations
from minishop_corp.invitations_types import CreateInvitation, RotateInvitation
from minishop_corp.membership_types import DepartMembership
from minishop_corp.memberships import Memberships
from minishop_corp.storage.schema import (
    AuditEvent,
    Contract,
    Invitation,
    InvitationPreview,
    Membership,
    Operation,
    Reservation,
    Revision,
)
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from .conftest import USER_ID, Host
from .test_contracts import ADMIN_PATH, MANAGER, MEMBER, authorization
from .test_contracts import client as client
from .test_invitations import create_api_contract
from .test_memberships import execute, invitation

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def test_full_deletion_requires_departure_and_preserves_core(
    client: TestClient, host: Host
) -> None:
    contract_id = await create_api_contract(client, host)
    draft = await invitation(host.session, contract_id=contract_id)
    memberships = Memberships(ContractHost(host.service))
    joined = await memberships.confirm(host.session, MEMBER, draft)
    assert joined.operation is not None
    path = f"{ADMIN_PATH}/{contract_id}"
    assert (await client.delete(path, headers=authorization(host))).status == 409
    assert await execute(host, joined.operation.id)
    assert (await client.delete(path, headers=authorization(host))).status == 409
    departure = await memberships.depart(
        host.session, MEMBER, joined.operation.membership_id, DepartMembership(request_id=uuid4())
    )
    assert (await client.delete(path, headers=authorization(host))).status == 409
    assert await execute(host, departure.id)
    before = await AccessAdapter(host.service).read(host.session, MEMBER)
    calls = list(host.panel.requests)
    # Keep an unrelated subscription to verify deletion is strictly scoped.
    other = await create_api_contract(client, host)
    for _ in range(2):
        result = await client.delete(path, headers=authorization(host))
        assert result.status == 200, await result.text()
    assert host.panel.requests == calls or all(
        method == "GET" for method, _ in host.panel.requests[len(calls) :]
    )
    assert await AccessAdapter(host.service).read(host.session, MEMBER) == before
    assert await user_dal.get_user_by_id(host.session, MEMBER) is not None
    assert (await client.get(path, headers=authorization(host))).status == 404
    assert (await client.get(f"{ADMIN_PATH}/{other}", headers=authorization(host))).status == 200
    for model in (
        InvitationPreview,
        AuditEvent,
        Reservation,
        Operation,
        Membership,
        Invitation,
        Revision,
    ):
        assert (
            await host.session.scalar(
                select(func.count()).select_from(model).where(model.contract_id == contract_id)
            )
            == 0
        )


async def test_deletion_requires_admin_and_native_csrf(client: TestClient, host: Host) -> None:
    contract_id = await create_api_contract(client, host)
    path = f"{ADMIN_PATH}/{contract_id}"
    assert (await client.delete(path)).status == 401
    assert (await client.delete(path, headers=authorization(host, MANAGER))).status == 403
    cookie = (
        "rw_webapp_session="
        + create_webapp_session_token(host.settings, USER_ID)
        + "; rw_webapp_csrf=fixture-csrf"
    )
    assert (
        await client.delete(
            path, headers={"Cookie": cookie, "Origin": str(client.make_url("/")).rstrip("/")}
        )
    ).status == 403


async def test_empty_contract_deletion_removes_rotated_codes_and_previews(
    client: TestClient, host: Host
) -> None:
    contract_id = await create_api_contract(client, host)
    service = Invitations(ContractHost(host.service))
    first = await service.create(
        host.session,
        USER_ID,
        contract_id,
        CreateInvitation(id=uuid4(), kind="reusable", use_limit=5),
    )
    assert first.code is not None
    assert (
        await InvitationLookup(ContractHost(host.service)).inspect(host.session, MEMBER, first.code)
    ).status == 200
    await service.rotate(
        host.session, USER_ID, contract_id, first.invitation.id, RotateInvitation(id=uuid4())
    )
    calls = list(host.panel.requests)
    result = await client.delete(f"{ADMIN_PATH}/{contract_id}", headers=authorization(host))
    assert result.status == 200, await result.text()
    assert host.panel.requests == calls
    assert await host.session.get(InvitationPreview, MEMBER, populate_existing=True) is None
    assert (
        await host.session.scalar(
            select(func.count())
            .select_from(Invitation)
            .where(Invitation.contract_id == contract_id)
        )
        == 0
    )


@pytest.mark.parametrize("resource", ["contract", "invitation"])
async def test_delete_racing_join_is_serialized(
    engine: AsyncEngine, host: Host, resource: str
) -> None:
    factory = async_sessionmaker(engine, expire_on_commit=False)
    admin, participant = (993101, 993102) if resource == "contract" else (993201, 993202)
    async with factory.begin() as session:
        for user_id in (admin, participant):
            await user_dal.create_user(session, {"user_id": user_id})
        await grant_role(session, admin, "admin", source="corp_cleanup_race")
        draft = await invitation(session, manager=admin)
    contract_id = draft.offer.contract_id
    host_adapter = ContractHost(host.service)
    gate = asyncio.Barrier(2)

    async def join() -> int:
        async with factory.begin() as session:
            await gate.wait()
            return (await Memberships(host_adapter).confirm(session, participant, draft)).status

    async def remove() -> int:
        try:
            async with factory.begin() as session:
                await gate.wait()
                if resource == "contract":
                    await Contracts(host_adapter).delete(session, admin, contract_id)
                else:
                    await Invitations(host_adapter).delete(
                        session, admin, contract_id, draft.offer.invitation_id
                    )
            return 200
        except ContractError as error:
            return error.status

    try:
        removed, joined = await asyncio.wait_for(asyncio.gather(remove(), join()), timeout=15)
        assert (removed, joined) in ((200, 400), (409, 202))
        async with factory() as session:
            count = await session.scalar(
                select(func.count())
                .select_from(Membership)
                .where(Membership.contract_id == contract_id)
            )
            assert count == (1 if joined == 202 else 0)
    finally:
        async with factory.begin() as session:
            for model in (
                InvitationPreview,
                AuditEvent,
                Reservation,
                Operation,
                Membership,
                Invitation,
                Revision,
            ):
                await session.execute(delete(model).where(model.contract_id == contract_id))
            await session.execute(delete(Contract).where(Contract.id == contract_id))
            # Synthetic native roles are test-owned; Minishop's user-deletion DAL
            # does not remove these references for an administrator account.
            await session.execute(delete(AccountRoleEvent).where(AccountRoleEvent.user_id == admin))
            await session.execute(delete(AccountRole).where(AccountRole.user_id == admin))
            for user_id in (admin, participant):
                await user_dal.delete_user_and_relations(session, user_id)
