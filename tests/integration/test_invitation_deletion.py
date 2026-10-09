from uuid import uuid4

import pytest
from aiohttp.test_utils import TestClient
from bot.app.web.webapp_auth import create_webapp_session_token
from bot.services.account_roles import grant_role
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
    Invitation,
    InvitationPreview,
    Membership,
    Reservation,
)
from sqlalchemy import func, select

from .conftest import USER_ID, Host
from .test_access import seed_personal
from .test_contracts import ADMIN_PATH, MANAGER, MEMBER, authorization
from .test_contracts import client as client
from .test_invitations import create_api_contract
from .test_memberships import execute, invitation, joined

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def test_deleting_used_code_preserves_access_join_retry_and_departure(host: Host) -> None:
    await grant_role(host.session, USER_ID, "admin", source="corp_test")
    draft, member_id = await joined(host)
    service = Invitations(ContractHost(host.service))
    before = await AccessAdapter(host.service).read(host.session, USER_ID)
    calls = list(host.panel.requests)
    await service.delete(host.session, USER_ID, draft.offer.contract_id, draft.offer.invitation_id)
    await service.delete(host.session, USER_ID, draft.offer.contract_id, draft.offer.invitation_id)
    assert host.panel.requests == calls
    assert (
        await host.session.get(Invitation, draft.offer.invitation_id, populate_existing=True)
        is None
    )
    member = await host.session.get(Membership, member_id, populate_existing=True)
    assert member is not None and member.state == "active" and member.invitation_id is None
    assert await host.session.scalar(select(func.count()).select_from(Reservation)) == 0
    assert await AccessAdapter(host.service).read(host.session, USER_ID) == before
    memberships = Memberships(ContractHost(host.service))
    repeated = await memberships.confirm(host.session, USER_ID, draft)
    assert repeated.operation is not None and repeated.operation.state == "succeeded"
    departure = await memberships.depart(
        host.session, USER_ID, member_id, DepartMembership(request_id=uuid4()), scope="self"
    )
    assert await execute(host, departure.id)
    access = await AccessAdapter(host.service).read(host.session, USER_ID)
    assert access is not None and access.provider == "trial"
    assert (
        await host.session.scalar(
            select(func.count())
            .select_from(AuditEvent)
            .where(
                AuditEvent.action == "invitation_deleted",
                AuditEvent.contract_id == draft.offer.contract_id,
            )
        )
        == 1
    )


@pytest.mark.parametrize("retry", [False, True])
async def test_unresolved_join_prevents_code_deletion(host: Host, retry: bool) -> None:
    await grant_role(host.session, USER_ID, "admin", source="corp_test")
    await seed_personal(host, "paid")
    draft = await invitation(host.session)
    result = await Memberships(ContractHost(host.service)).confirm(host.session, USER_ID, draft)
    assert result.operation is not None
    if retry:
        host.panel.fail_updates = True
        assert not await execute(host, result.operation.id)
    with pytest.raises(ContractError, match="invitation_busy"):
        await Invitations(ContractHost(host.service)).delete(
            host.session, USER_ID, draft.offer.contract_id, draft.offer.invitation_id
        )
    row = await host.session.get(Invitation, draft.offer.invitation_id, populate_existing=True)
    assert row is not None and row.reserved_count == 1


async def test_delete_preview_and_replacement_without_resurrecting_codes(
    client: TestClient, host: Host
) -> None:
    contract_id = await create_api_contract(client, host)
    service = Invitations(ContractHost(host.service))
    create = CreateInvitation(id=uuid4(), kind="reusable", use_limit=3)
    first = await service.create(host.session, USER_ID, contract_id, create)
    assert first.code is not None
    assert (
        await InvitationLookup(ContractHost(host.service)).inspect(host.session, MEMBER, first.code)
    ).status == 200
    rotated = RotateInvitation(id=uuid4())
    second = await service.rotate(host.session, USER_ID, contract_id, create.id, rotated)
    await service.delete(host.session, USER_ID, contract_id, create.id)
    assert await host.session.get(InvitationPreview, MEMBER, populate_existing=True) is None
    repeated = await service.rotate(
        host.session, MANAGER, contract_id, create.id, rotated, manager=True
    )
    assert repeated.code == second.code
    with pytest.raises(ContractError, match="request_conflict"):
        await service.create(
            host.session, USER_ID, contract_id, create.model_copy(update={"id": rotated.id})
        )
    with pytest.raises(ContractError, match="invitation_missing"):
        await service.create(host.session, USER_ID, contract_id, create)
    await service.delete(host.session, USER_ID, contract_id, rotated.id)
    with pytest.raises(ContractError, match="invitation_missing"):
        await service.rotate(host.session, MANAGER, contract_id, create.id, rotated, manager=True)
    third = await service.create(
        host.session, USER_ID, contract_id, create.model_copy(update={"id": uuid4()})
    )
    assert third.code not in (first.code, second.code)


async def test_delete_code_is_admin_scoped_and_requires_native_csrf(
    client: TestClient, host: Host
) -> None:
    contract_id = await create_api_contract(client, host)
    service = Invitations(ContractHost(host.service))
    created = await service.create(
        host.session, USER_ID, contract_id, CreateInvitation(id=uuid4(), kind="single", use_limit=1)
    )
    path = f"{ADMIN_PATH}/{contract_id}/invitations/{created.invitation.id}"
    assert (await client.delete(path)).status == 401
    assert (await client.delete(path, headers=authorization(host, MANAGER))).status == 403
    cookie = "rw_webapp_session=" + create_webapp_session_token(host.settings, USER_ID)
    cookie += "; rw_webapp_csrf=synthetic-csrf"
    origin = str(client.make_url("/")).rstrip("/")
    assert (await client.delete(path, headers={"Cookie": cookie, "Origin": origin})).status == 403
    foreign = await create_api_contract(client, host)
    assert (
        await client.delete(
            f"{ADMIN_PATH}/{foreign}/invitations/{created.invitation.id}",
            headers=authorization(host),
        )
    ).status == 200
    assert (
        await host.session.get(Invitation, created.invitation.id, populate_existing=True)
        is not None
    )
    for _ in range(2):
        result = await client.delete(path, headers=authorization(host))
        assert result.status == 200 and await result.json() == {"ok": True}
    assert await host.session.get(Invitation, created.invitation.id, populate_existing=True) is None
