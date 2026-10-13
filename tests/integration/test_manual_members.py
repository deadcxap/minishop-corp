import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from aiohttp.test_utils import TestClient
from bot.app.web.webapp_auth import create_webapp_session_token
from bot.services.account_roles import grant_role
from db.dal import payment_dal, subscription_dal, user_dal
from minishop_corp.contracts import Contracts
from minishop_corp.contracts_types import ContractError, UpdateContract
from minishop_corp.integration.access import AccessAdapter
from minishop_corp.integration.access_types import panel_time
from minishop_corp.integration.contracts import ContractHost
from minishop_corp.membership_types import AddMembership, DepartMembership
from minishop_corp.memberships import Memberships
from minishop_corp.storage.schema import Contract, Invitation, Membership, Operation, Reservation
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from .conftest import USER_ID, Host
from .test_access import seed_personal
from .test_contracts import ADMIN_PATH, MANAGER, MEMBER, USER_PATH, authorization
from .test_contracts import client as client
from .test_invitations import create_api_contract
from .test_memberships import execute
from .test_storage import seed_contract

pytestmark = pytest.mark.asyncio(loop_scope="session")


@pytest.mark.parametrize("before", ["new", "trial", "paid", "expired"])
async def test_admin_add_replaces_access_without_invitation_and_retries(
    client: TestClient, host: Host, before: str
) -> None:
    await seed_personal(host, before)
    contract_id = await create_api_contract(client, host)
    path = f"{ADMIN_PATH}/{contract_id}/members"
    payload = {"request_id": str(uuid4()), "user_id": USER_ID, "expected_version": 1}
    calls = list(host.panel.requests)
    response = await client.post(path, headers=authorization(host), json=payload)
    assert response.status == 202, await response.text()
    accepted = (await response.json())["operation"]
    assert accepted["state"] == "pending"
    assert all(method == "GET" for method, _ in host.panel.requests[len(calls) :])
    operation_id = UUID(accepted["id"])
    assert await execute(host, operation_id, due=True)
    state = await AccessAdapter(host.service).read(host.session, USER_ID)
    contract = await host.session.get(Contract, contract_id)
    assert state is not None and contract is not None
    assert state.tariff_key == "corp" and state.end_date == panel_time(contract.ends_at)
    assert contract.manager_user_id == MANAGER
    for model in (Invitation, Reservation):
        assert (
            await host.session.scalar(
                select(func.count()).select_from(model).where(model.contract_id == contract_id)
            )
            == 0
        )
    calls = list(host.panel.requests)
    response = await client.post(path, headers=authorization(host), json=payload)
    assert response.status == 202 and (await response.json())["operation"]["id"] == accepted["id"]
    assert host.panel.requests == calls
    conflict = await client.post(
        path, headers=authorization(host), json={**payload, "user_id": MEMBER}
    )
    assert conflict.status == 409
    duplicate = await client.post(
        path, headers=authorization(host), json={**payload, "request_id": str(uuid4())}
    )
    assert duplicate.status == 409
    assert (await duplicate.json())["error"] == "minishop_corp_already_member"
    member_id = UUID(accepted["membership_id"])
    departure = await Memberships(ContractHost(host.service)).depart(
        host.session, USER_ID, member_id, DepartMembership(request_id=uuid4())
    )
    assert await execute(host, departure.id, due=True)
    # A delayed add retry cannot re-enrol someone who has left.
    response = await client.post(path, headers=authorization(host), json=payload)
    assert (await response.json())["operation"]["id"] == accepted["id"]
    assert await Memberships(ContractHost(host.service)).current(host.session, USER_ID) is None


async def test_manual_add_authorization_csrf_and_versions(client: TestClient, host: Host) -> None:
    contract_id = await create_api_contract(client, host)
    path = f"{ADMIN_PATH}/{contract_id}/members"
    payload = {"request_id": str(uuid4()), "user_id": MEMBER, "expected_version": 1}
    assert (await client.post(path, json=payload)).status == 401
    assert (
        await client.post(path, json=payload, headers=authorization(host, MANAGER))
    ).status == 403
    assert (
        await client.post(
            f"{USER_PATH}/managed-contracts/{contract_id}/members",
            json=payload,
            headers=authorization(host, MANAGER),
        )
    ).status == 405
    cookie = (
        "rw_webapp_session="
        + create_webapp_session_token(host.settings, USER_ID)
        + "; rw_webapp_csrf=fixture-csrf"
    )
    assert (
        await client.post(
            path,
            json=payload,
            headers={
                "Cookie": cookie,
                "Origin": str(client.make_url("/")).rstrip("/"),
            },
        )
    ).status == 403
    for changes, status in (
        ({"expected_version": 2}, 409),
        ({"user_id": 999999999}, 422),
        ({"tariff_key": "personal"}, 400),
    ):
        response = await client.post(path, json={**payload, **changes}, headers=authorization(host))
        assert response.status == status, await response.text()
    await user_dal.update_user(host.session, MEMBER, {"is_banned": True})
    assert (await client.post(path, json=payload, headers=authorization(host))).status == 422
    await user_dal.update_user(host.session, MEMBER, {"is_banned": False})
    row = await host.session.get(Contract, contract_id)
    assert row is not None
    row.ends_at = datetime.now(UTC) - timedelta(days=1)
    await host.session.flush()
    response = await client.post(path, json=payload, headers=authorization(host))
    assert (
        response.status == 409
        and (await response.json())["error"] == "minishop_corp_contract_expired"
    )
    assert (
        await host.session.scalar(
            select(func.count())
            .select_from(Membership)
            .where(Membership.contract_id == contract_id)
        )
        == 0
    )


@pytest.mark.parametrize("blocked", ["renewal", "payment"])
async def test_manual_add_checks_personal_billing_without_cancelling(
    client: TestClient, host: Host, blocked: str
) -> None:
    await seed_personal(host, "paid")
    if blocked == "renewal":
        row = await subscription_dal.get_active_subscription_by_user_id(host.session, USER_ID)
        assert row is not None
        await subscription_dal.set_auto_renew(host.session, row.subscription_id, True)
    else:
        await payment_dal.create_payment_record(
            host.session,
            {
                "user_id": USER_ID,
                "amount": 100,
                "currency": "RUB",
                "status": "pending",
            },
        )
    identifier = await create_api_contract(client, host)
    calls = list(host.panel.requests)
    response = await client.post(
        f"{ADMIN_PATH}/{identifier}/members",
        headers=authorization(host),
        json={
            "request_id": str(uuid4()),
            "user_id": USER_ID,
            "expected_version": 1,
        },
    )
    assert response.status == 409
    assert (await response.json())["error"] == (
        "minishop_corp_renewal_policy_required"
        if blocked == "renewal"
        else "minishop_corp_payment_pending"
    )
    assert host.panel.requests == calls
    assert (
        await host.session.scalar(
            select(func.count()).select_from(Operation).where(Operation.contract_id == identifier)
        )
        == 0
    )


async def test_manual_join_rechecks_payment_and_preserves_source_on_new_terms(
    client: TestClient, host: Host
) -> None:
    identifier = await create_api_contract(client, host)
    service = Memberships(ContractHost(host.service))
    intent = AddMembership(request_id=uuid4(), user_id=MANAGER, expected_version=1)
    result = await service.add(host.session, USER_ID, identifier, intent)
    row = await Contracts(ContractHost(host.service)).admin_get(host.session, USER_ID, identifier)
    await Contracts(ContractHost(host.service)).update(
        host.session,
        USER_ID,
        identifier,
        UpdateContract(
            **row.model_dump(
                include={"name", "tariff_key", "external_squad_uuid", "manager_user_id"}
            ),
            expected_version=1,
            ends_at=row.ends_at + timedelta(days=3),
        ),
    )
    payment = await payment_dal.create_payment_record(
        host.session,
        {
            "user_id": MANAGER,
            "amount": 100,
            "currency": "RUB",
            "status": "pending",
        },
    )
    assert not await execute(host, result.id, due=True)
    op = await host.session.get(Operation, result.id)
    assert op is not None and op.error_code == "minishop_corp_payment_pending"
    assert op.target["enrollment"] == "admin" and op.contract_version == 2
    await payment_dal.update_payment_status_by_db_id(host.session, payment.payment_id, "canceled")
    assert await execute(host, result.id, due=True)
    assert (await service.add(host.session, USER_ID, identifier, intent)).id == result.id
    current = await service.current(host.session, MANAGER)
    assert current is not None and current.state == "active" and current.contract.version == 2


async def test_manual_add_concurrency_and_rollback(engine: AsyncEngine, host: Host) -> None:
    actor, target = 984001, 984002
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    async with factory.begin() as session:
        for user in (actor, target):
            await user_dal.create_user(session, {"user_id": user})
        await grant_role(session, actor, "admin", source="corp_test")
        contract = await seed_contract(session, manager=actor)
        identifier = contract.id
    service = Memberships(ContractHost(host.service))
    draft = AddMembership(request_id=uuid4(), user_id=target, expected_version=1)
    async with factory() as session:
        await service.add(session, actor, identifier, draft)
        await session.rollback()
        assert (
            await session.scalar(select(Membership.id).where(Membership.user_id == target)) is None
        )

    async def add(intent: AddMembership) -> UUID | str:
        async with factory.begin() as session:
            try:
                return (await service.add(session, actor, identifier, intent)).id
            except ContractError as exc:
                return exc.code

    first, second = await asyncio.gather(add(draft), add(draft))
    assert isinstance(first, UUID) and first == second
    conflicts = await asyncio.gather(
        *[add(draft.model_copy(update={"request_id": uuid4()})) for _ in range(2)]
    )
    assert conflicts == ["minishop_corp_already_member"] * 2
    async with factory() as session:
        assert (
            await session.scalar(
                select(func.count()).select_from(Membership).where(Membership.user_id == target)
            )
            == 1
        )
