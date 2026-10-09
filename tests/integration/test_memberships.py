import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from aiohttp.test_utils import TestClient
from db.dal import payment_dal, user_dal
from minishop_corp.contracts import Contracts
from minishop_corp.contracts_types import ContractError, UpdateContract
from minishop_corp.integration.access import AccessAdapter
from minishop_corp.integration.access_types import panel_time
from minishop_corp.integration.contracts import ContractHost
from minishop_corp.invitations_types import OfferReference, code_digest, new_code
from minishop_corp.membership_types import ConfirmMembership, DepartMembership
from minishop_corp.memberships import Memberships
from minishop_corp.operation_worker import OperationWorker
from minishop_corp.storage.schema import Contract, Invitation, Membership, Operation, Reservation
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from .conftest import EXTERNAL_SQUAD, TRIAL_SQUAD, USER_ID, Host
from .test_access import seed_personal
from .test_contracts import ADMIN_PATH, MANAGER, MEMBER, OTHER, USER_PATH, authorization
from .test_contracts import client as client
from .test_invitations import create_api_contract
from .test_storage import seed_contract

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def invitation(
    session: AsyncSession,
    *,
    manager: int = USER_ID,
    contract_id: UUID | None = None,
) -> ConfirmMembership:
    contract = (
        await seed_contract(session, manager=manager)
        if contract_id is None
        else await session.get(Contract, contract_id)
    )
    assert contract is not None
    secret = new_code()
    invite = Invitation(
        contract_id=contract.id,
        kind="reusable",
        code_digest=code_digest(secret),
        use_limit=5,
        created_by=manager,
    )
    session.add(invite)
    await session.flush()
    return ConfirmMembership(
        code=secret,
        request_id=uuid4(),
        offer=OfferReference(
            invitation_id=invite.id,
            contract_id=contract.id,
            contract_version=contract.version,
        ),
    )


async def execute(host: Host, operation_id: UUID, *, due: bool = False) -> bool:
    worker = OperationWorker(ContractHost(host.service))
    lease = await worker.claim(
        host.session,
        operation_id=operation_id,
        now=datetime.now(UTC) + timedelta(days=1) if due else None,
    )
    assert lease is not None
    # A real worker commits its claim before processing. In this fixture commits
    # release a savepoint; its outer test transaction remains isolated.
    await host.session.commit()
    return await worker.execute(host.session, lease)


async def joined(host: Host, user_id: int = USER_ID) -> tuple[ConfirmMembership, UUID]:
    draft = await invitation(host.session)
    result = await Memberships(ContractHost(host.service)).confirm(host.session, user_id, draft)
    assert result.operation is not None and result.status == 202
    assert await execute(host, result.operation.id)
    return draft, result.operation.membership_id


@pytest.mark.parametrize("before", ["new", "trial", "paid", "expired"])
async def test_confirmation_applies_exact_contract_and_consumes_once(
    host: Host, before: str
) -> None:
    await seed_personal(host, before)
    service = Memberships(ContractHost(host.service))
    draft = await invitation(host.session)
    result = await service.confirm(host.session, USER_ID, draft)
    assert result.status == 202 and result.operation is not None
    identifier = result.operation.id
    member_id = result.operation.membership_id
    reserved = await host.session.get(Invitation, draft.offer.invitation_id)
    assert reserved is not None and (reserved.reserved_count, reserved.used_count) == (1, 0)
    again = await service.confirm(host.session, USER_ID, draft)
    assert again.operation == result.operation and again.status == 200
    assert await execute(host, identifier)
    await host.session.refresh(reserved)
    assert (reserved.reserved_count, reserved.used_count) == (0, 1)
    member = await host.session.get(Membership, member_id)
    assert member is not None and member.state == "active" and member.applied_version == 1
    state = await AccessAdapter(host.service).read(host.session, USER_ID)
    contract = await host.session.get(Contract, draft.offer.contract_id)
    assert (
        state is not None
        and contract is not None
        and state.end_date == panel_time(contract.ends_at)
    )
    assert state.tariff_key == "corp" and not state.auto_renew_enabled
    calls = list(host.panel.requests)
    repeated = await service.confirm(host.session, USER_ID, draft)
    assert repeated.operation is not None and repeated.operation.state == "succeeded"
    assert host.panel.requests == calls
    with pytest.raises(ContractError, match="already_member"):
        await service.confirm(
            host.session, USER_ID, draft.model_copy(update={"request_id": uuid4()})
        )


async def test_departure_retry_fixed_trial_and_rejoin_after_exclusion(host: Host) -> None:
    draft, member_id = await joined(host)
    service = Memberships(ContractHost(host.service))
    intent = DepartMembership(request_id=uuid4())
    departure = await service.depart(
        host.session,
        USER_ID,
        member_id,
        intent,
        scope="manager",
        contract_id=draft.offer.contract_id,
    )
    target = dict((await host.session.get(Operation, departure.id)).target)
    host.panel.fail_updates = True
    assert not await execute(host, departure.id)
    row = await host.session.get(Operation, departure.id)
    assert row is not None and row.state == "retry" and row.target == target
    member = await host.session.get(Membership, member_id)
    assert member is not None and member.state == "leaving" and member.current_user_id == USER_ID
    host.panel.fail_updates = False
    assert await execute(host, departure.id, due=True)
    state = await AccessAdapter(host.service).read(host.session, USER_ID)
    assert state is not None and state.provider == "trial" and state.tariff_key is None
    assert state.start_date == datetime.fromisoformat(str(target["starts_at"]))
    assert state.end_date == datetime.fromisoformat(str(target["ends_at"]))
    panel = host.panel.users[state.panel_user_uuid]
    assert panel["activeInternalSquads"] == [TRIAL_SQUAD] and panel["externalSquadUuid"] is None
    before = list(host.panel.requests)
    assert (
        await service.depart(
            host.session,
            USER_ID,
            member_id,
            intent,
            scope="manager",
            contract_id=draft.offer.contract_id,
        )
    ).id == departure.id
    assert host.panel.requests == before
    new = await service.confirm(
        host.session,
        USER_ID,
        draft.model_copy(update={"request_id": uuid4()}),
        now=datetime.now(UTC) + timedelta(minutes=2),
    )
    assert new.operation is not None and new.operation.membership_id != member_id
    assert await execute(host, new.operation.id)
    await host.session.refresh(member)
    assert member.state == "excluded" and member.current_user_id is None
    previous = await service.confirm(host.session, USER_ID, draft)
    assert previous.operation is not None and previous.operation.membership_id == member_id
    invite = await host.session.get(Invitation, draft.offer.invitation_id)
    assert invite is not None and invite.used_count == 2 and invite.reserved_count == 0
    # Retrying the old exclusion must not remove the new membership or grant trial.
    await service.depart(
        host.session,
        USER_ID,
        member_id,
        intent,
        scope="manager",
        contract_id=draft.offer.contract_id,
    )
    current = await service.current(host.session, USER_ID)
    assert (
        current is not None
        and current.id == new.operation.membership_id
        and current.state == "active"
    )


@pytest.mark.parametrize("disable_after_request", [False, True])
async def test_disabled_trial_departure_finishes_without_grant(
    host: Host, disable_after_request: bool
) -> None:
    _, member_id = await joined(host)
    if not disable_after_request:
        host.settings.TRIAL_ENABLED = False
    service = Memberships(ContractHost(host.service))
    result = await service.depart(
        host.session, USER_ID, member_id, DepartMembership(request_id=uuid4())
    )
    host.settings.TRIAL_ENABLED = False
    assert await execute(host, result.id)
    state = await AccessAdapter(host.service).read(host.session, USER_ID)
    assert state is not None and not state.is_active and state.provider != "trial"
    assert (
        state.tariff_key is None and host.panel.users[state.panel_user_uuid]["status"] == "EXPIRED"
    )
    assert await service.current(host.session, USER_ID) is None
    # Prove normal native purchasing remains possible once the departure finishes.
    assert (
        await host.service.extend_active_subscription_days(
            host.session, USER_ID, 30, reason="admin", tariff_key="personal"
        )
        is not None
    )


async def test_payment_gate_checked_again_before_worker_effect(host: Host) -> None:
    service = Memberships(ContractHost(host.service))
    draft = await invitation(host.session)
    result = await service.confirm(host.session, USER_ID, draft)
    assert result.operation is not None
    payment = await payment_dal.create_payment_record(
        host.session, {"user_id": USER_ID, "amount": 100, "currency": "RUB", "status": "pending"}
    )
    assert not await execute(host, result.operation.id)
    row = await host.session.get(Operation, result.operation.id)
    assert row is not None and row.error_code == "minishop_corp_payment_pending"
    assert not host.panel.users
    reserve = await host.session.scalar(
        select(Reservation).where(Reservation.operation_id == row.id)
    )
    assert reserve is not None and reserve.state == "held"
    await payment_dal.update_payment_status_by_db_id(host.session, payment.payment_id, "canceled")
    assert await execute(host, row.id, due=True)


async def test_native_trial_rollback_retains_operation_and_user_lock(
    host: Host, engine: AsyncEngine
) -> None:
    _, member_id = await joined(host)
    departure = await Memberships(ContractHost(host.service)).depart(
        host.session, USER_ID, member_id, DepartMembership(request_id=uuid4())
    )
    # Fail the native trial PATCH, after the adapter's earlier squad PATCH succeeded.
    host.panel.fail_trial_activation = True
    assert not await execute(host, departure.id)
    row = await host.session.get(Operation, departure.id)
    member = await host.session.get(Membership, member_id)
    assert (
        row is not None
        and row.state == "retry"
        and member is not None
        and member.state == "leaving"
    )
    assert not host.panel.trial_applied
    # The test's user insert is still uncommitted. A native rollback escaping the
    # savepoint would remove it and all accepted operations from this fixture.
    assert await user_dal.get_user_by_id(host.session, USER_ID) is not None
    host.panel.fail_trial_activation = False
    assert await execute(host, departure.id, due=True)


@pytest.mark.parametrize("failure", ["before", "after", "create"])
async def test_join_transport_failures_keep_one_reservation(host: Host, failure: str) -> None:
    await user_dal.update_user(host.session, USER_ID, {"telegram_id": USER_ID})
    if failure == "before":
        await seed_personal(host, "paid")
    draft = await invitation(host.session)
    result = await Memberships(ContractHost(host.service)).confirm(host.session, USER_ID, draft)
    assert result.operation is not None
    host.panel.fail_updates = failure == "before"
    host.panel.lose_update_response = failure == "after"
    host.panel.lose_create_response = failure == "create"
    success = await execute(host, result.operation.id)
    if failure == "before":
        assert not success
    if not success:
        invite = await host.session.get(Invitation, draft.offer.invitation_id)
        assert invite is not None and invite.reserved_count == 1 and invite.used_count == 0
        host.panel.fail_updates = host.panel.lose_update_response = (
            host.panel.lose_create_response
        ) = False
        assert await execute(host, result.operation.id, due=True)
    assert len(host.panel.users) == 1
    assert (
        await host.session.scalar(
            select(func.count())
            .select_from(Membership)
            .where(Membership.contract_id == draft.offer.contract_id)
        )
        == 1
    )
    invite = await host.session.get(Invitation, draft.offer.invitation_id)
    assert invite is not None and invite.used_count == 1 and invite.reserved_count == 0


async def test_http_scopes_and_immediate_confirmation_of_verified_preview(
    client: TestClient, host: Host
) -> None:
    contract_id = await create_api_contract(client, host)
    draft = await invitation(host.session, contract_id=contract_id)
    headers = authorization(host, MEMBER)
    payload = draft.model_dump(mode="json")
    payload["code"] = draft.code.get_secret_value()
    url = USER_PATH + "/membership/confirm"
    assert (await client.post(url, json=payload)).status == 401
    assert (
        await client.post(url, json={**payload, "tariff_key": "corp"}, headers=headers)
    ).status == 400
    response = await client.post(
        USER_PATH + "/invitations/preview", json={"code": payload["code"]}, headers=headers
    )
    assert response.status == 200
    response = await client.post(url, json=payload, headers=headers)
    assert response.status == 202, await response.text()
    accepted = (await response.json())["operation"]
    result = await Memberships(ContractHost(host.service)).confirm(host.session, MEMBER, draft)
    assert result.operation is not None
    assert str(result.operation.id) == accepted["id"]
    assert await execute(host, result.operation.id)
    own_op = USER_PATH + f"/operations/{result.operation.id}"
    assert (await client.get(own_op, headers=authorization(host, OTHER))).status == 404
    assert (await client.get(own_op, headers=headers)).status == 200
    member_id = result.operation.membership_id
    assert (
        await client.post(
            USER_PATH + f"/memberships/{member_id}/leave",
            json={"request_id": str(uuid4())},
            headers=authorization(host, OTHER),
        )
    ).status == 404
    manage = USER_PATH + f"/managed-contracts/{contract_id}"
    exclude = manage + f"/members/{member_id}/exclude"
    intent = {"request_id": str(uuid4())}
    assert (
        await client.post(exclude, json=intent, headers=authorization(host, OTHER))
    ).status == 404
    assert (
        await client.post(
            ADMIN_PATH + f"/{contract_id}/members/{member_id}/exclude",
            json=intent,
            headers=authorization(host, MANAGER),
        )
    ).status == 403
    wrong_contract = USER_PATH + f"/managed-contracts/{uuid4()}/members/{member_id}/exclude"
    assert (
        await client.post(wrong_contract, json=intent, headers=authorization(host, MANAGER))
    ).status == 404
    response = await client.post(exclude, json=intent, headers=authorization(host, MANAGER))
    assert response.status == 202, await response.text()
    operation_id = UUID((await response.json())["operation"]["id"])
    assert (
        await client.get(
            manage + f"/operations/{operation_id}", headers=authorization(host, MANAGER)
        )
    ).status == 200
    assert await execute(host, operation_id)


async def test_queued_join_uses_latest_terms_and_expired_member_can_leave(
    client: TestClient, host: Host
) -> None:
    contract_id = await create_api_contract(client, host)
    draft = await invitation(host.session, contract_id=contract_id)
    service = Memberships(ContractHost(host.service))
    result = await service.confirm(host.session, MEMBER, draft)
    assert result.operation is not None
    await Contracts(ContractHost(host.service)).update(
        host.session,
        USER_ID,
        contract_id,
        UpdateContract(
            expected_version=1,
            name="Expired contract",
            tariff_key="corp",
            external_squad_uuid=EXTERNAL_SQUAD,
            ends_at=datetime.now(UTC) - timedelta(days=1),
            manager_user_id=MANAGER,
        ),
    )
    assert await execute(host, result.operation.id)
    current = await service.current(host.session, MEMBER)
    assert current is not None and current.contract.expired and current.can_leave
    assert current.expiry_notice == "wa_minishop_corp_expired_notice"
    assert current.contract.version == 2
    state = await AccessAdapter(host.service).read(host.session, MEMBER)
    assert state is not None and not state.is_active and state.provider != "trial"
    assert (await service.confirm(host.session, MEMBER, draft)).operation.id == result.operation.id
    departure = await service.depart(
        host.session, MEMBER, current.id, DepartMembership(request_id=uuid4())
    )
    assert await execute(host, departure.id)


async def test_two_confirmations_and_workers_restart_fences(
    engine: AsyncEngine, host: Host
) -> None:
    identifier = 950001
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory.begin() as session:
        await user_dal.create_user(session, {"user_id": identifier, "telegram_id": identifier})
        first = await invitation(session, manager=identifier)
        second = await invitation(session, manager=identifier)
    service = Memberships(ContractHost(host.service))

    async def confirm_one(draft: ConfirmMembership) -> UUID | str:
        async with factory.begin() as session:
            try:
                result = await service.confirm(session, identifier, draft)
                assert result.operation is not None
                return result.operation.id
            except ContractError as exc:
                return exc.code

    results = await asyncio.gather(confirm_one(first), confirm_one(second))
    operations = [result for result in results if isinstance(result, UUID)]
    assert len(operations) == 1 and "minishop_corp_already_member" in results
    operation_id = operations[0]
    worker = OperationWorker(ContractHost(host.service))

    async def claim_one(now: datetime | None = None):
        async with factory.begin() as session:
            return await worker.claim(session, operation_id=operation_id, now=now)

    claims = await asyncio.gather(claim_one(), claim_one())
    leases = [lease for lease in claims if lease is not None]
    assert len(leases) == 1
    # Simulate death after the durable claim. A new worker reclaims the expired lease.
    replacement = await claim_one(datetime.now(UTC) + timedelta(minutes=3))
    assert replacement is not None and replacement.token != leases[0].token
    async with factory.begin() as session:
        assert not await worker.execute(session, leases[0])
        assert not host.panel.users
    async with factory.begin() as session:
        assert await worker.execute(session, replacement)
    async with factory.begin() as session:
        row = await session.get(Operation, operation_id)
        assert row is not None and row.state == "succeeded"
        departure = await service.depart(
            session, identifier, row.membership_id, DepartMembership(request_id=uuid4())
        )
    async with factory.begin() as session:
        lease = await worker.claim(session, operation_id=departure.id)
    assert lease is not None
    async with factory.begin() as session:
        assert await worker.execute(session, lease)
    before = list(host.panel.requests)
    async with factory.begin() as session:
        assert not await worker.execute(session, replacement)
        assert host.panel.requests == before
        assert await service.current(session, identifier) is None
