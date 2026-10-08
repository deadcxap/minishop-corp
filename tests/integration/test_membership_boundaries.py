import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from aiohttp.test_utils import TestClient
from bot.app.web.webapp_auth import create_webapp_session_token
from db.dal import payment_dal, user_dal
from minishop_corp.contracts_types import ContractError
from minishop_corp.integration.access import AccessAdapter
from minishop_corp.integration.access_types import AccessError, AccessState, TrialAccess
from minishop_corp.integration.contracts import ContractHost
from minishop_corp.membership_types import DepartMembership
from minishop_corp.memberships import Memberships
from minishop_corp.operation_worker import OperationWorker
from minishop_corp.storage.schema import Invitation, Membership, Operation, Reservation
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from .conftest import USER_ID, Host
from .test_contracts import MEMBER, OTHER, USER_PATH, authorization
from .test_contracts import client as client
from .test_memberships import execute, invitation, joined

pytestmark = pytest.mark.asyncio(loop_scope="session")


@pytest.mark.parametrize("invalid", ["code", "version", "revoked"])
async def test_rejected_confirmation_preserves_budget_and_creates_no_membership(
    host: Host, invalid: str
) -> None:
    draft = await invitation(host.session)
    if invalid == "code":
        draft = draft.model_copy(update={"code": SecretStr("invalid")})
    elif invalid == "version":
        draft = draft.model_copy(
            update={"offer": draft.offer.model_copy(update={"contract_version": 2})}
        )
    else:
        invite = await host.session.get(Invitation, draft.offer.invitation_id)
        assert invite is not None
        invite.revoked_at = datetime.now(UTC)
        await host.session.flush()
    service = Memberships(ContractHost(host.service))
    first = await service.confirm(host.session, USER_ID, draft)
    assert first.status == (409 if invalid == "version" else 400) and first.operation is None
    assert (await service.confirm(host.session, USER_ID, draft)).status == 429
    assert await service.current(host.session, USER_ID) is None
    assert not host.panel.users


async def test_billing_rejection_does_not_reserve_a_slot(host: Host) -> None:
    draft = await invitation(host.session)
    await payment_dal.create_payment_record(
        host.session, {"user_id": USER_ID, "amount": 1, "currency": "RUB", "status": "pending"}
    )
    result = await Memberships(ContractHost(host.service)).confirm(host.session, USER_ID, draft)
    assert result.status == 409 and result.error == "minishop_corp_payment_pending"
    invite = await host.session.get(Invitation, draft.offer.invitation_id)
    assert invite is not None and invite.used_count == invite.reserved_count == 0
    assert not host.panel.users


async def test_ban_before_delivery_defers_effect_and_records_reason(host: Host) -> None:
    draft = await invitation(host.session)
    result = await Memberships(ContractHost(host.service)).confirm(host.session, USER_ID, draft)
    assert result.operation is not None
    await user_dal.update_user(host.session, USER_ID, {"is_banned": True})
    assert not await execute(host, result.operation.id)
    row = await host.session.get(Operation, result.operation.id)
    assert (
        row is not None
        and row.state == "retry"
        and row.error_code == "minishop_corp_account_unavailable"
    )
    assert not host.panel.users


async def test_pending_join_and_conflicting_ids_do_not_create_new_intents(host: Host) -> None:
    draft = await invitation(host.session)
    service = Memberships(ContractHost(host.service))
    result = await service.confirm(host.session, USER_ID, draft)
    assert result.operation is not None
    with pytest.raises(ContractError, match="operation_busy"):
        await service.depart(
            host.session,
            USER_ID,
            result.operation.membership_id,
            DepartMembership(request_id=uuid4()),
        )
    with pytest.raises(ContractError, match="request_conflict"):
        await service.confirm(
            host.session,
            USER_ID,
            draft.model_copy(
                update={"offer": draft.offer.model_copy(update={"contract_version": 5})}
            ),
        )
    assert await execute(host, result.operation.id)
    with pytest.raises(ContractError, match="request_conflict"):
        await service.depart(
            host.session,
            USER_ID,
            result.operation.membership_id,
            DepartMembership(request_id=draft.request_id),
        )
    current = await service.current(host.session, USER_ID)
    assert current is not None and current.state == "active"


async def test_csrf_uses_native_guard_and_head_never_applies_access(
    client: TestClient, host: Host
) -> None:
    draft = await invitation(host.session)
    payload = draft.model_dump(mode="json")
    payload["code"] = draft.code.get_secret_value()
    token = create_webapp_session_token(host.settings, MEMBER)
    headers = {"Cookie": f"rw_webapp_session={token}; rw_webapp_csrf=fixture-csrf"}
    response = await client.post(USER_PATH + "/membership/confirm", json=payload, headers=headers)
    assert response.status == 403
    headers.update(Origin=str(client.make_url("/")).rstrip("/"), **{"X-CSRF-Token": "fixture-csrf"})
    response = await client.post(USER_PATH + "/membership/confirm", json=payload, headers=headers)
    assert response.status == 202, await response.text()
    identifier = UUID((await response.json())["operation"]["id"])
    assert (
        await client.head(USER_PATH + "/membership", headers=authorization(host, MEMBER))
    ).status == 200
    assert not host.panel.users
    assert await execute(host, identifier)
    current = await Memberships(ContractHost(host.service)).current(host.session, MEMBER)
    assert current is not None
    path = USER_PATH + f"/memberships/{current.id}/leave"
    assert (
        await client.post(
            path, json={"request_id": str(uuid4())}, headers={"Cookie": headers["Cookie"]}
        )
    ).status == 403
    response = await client.post(path, json={"request_id": str(uuid4())}, headers=headers)
    assert response.status == 202


async def test_manager_role_is_rechecked_for_requests_and_operation_reads(
    client: TestClient, host: Host
) -> None:
    draft, member_id = await joined(host, MEMBER)
    service = Memberships(ContractHost(host.service))
    departure = await service.depart(
        host.session,
        USER_ID,
        member_id,
        DepartMembership(request_id=uuid4()),
        scope="manager",
        contract_id=draft.offer.contract_id,
    )
    # Change manager through the same contract update machinery used by the API.
    from minishop_corp.contracts import Contracts
    from minishop_corp.contracts_types import UpdateContract
    from minishop_corp.storage.schema import Contract

    row = await host.session.get(Contract, draft.offer.contract_id)
    assert row is not None
    await Contracts(ContractHost(host.service)).update(
        host.session,
        USER_ID,
        row.id,
        UpdateContract(
            expected_version=row.version,
            name=row.name,
            tariff_key=row.tariff_key,
            external_squad_uuid=row.external_squad_uuid,
            ends_at=row.ends_at,
            manager_user_id=OTHER,
        ),
    )
    # An already accepted intent is durable; new requests and reads use current roles.
    with pytest.raises(ContractError, match="membership_missing"):
        await service.operation(
            host.session, USER_ID, departure.id, scope="manager", contract_id=row.id
        )
    assert (
        await service.operation(
            host.session, OTHER, departure.id, scope="manager", contract_id=row.id
        )
    ).id == departure.id
    assert await execute(host, departure.id)


async def test_native_rollback_cannot_release_outer_locks_or_duplicate_delivery(
    engine: AsyncEngine, host: Host
) -> None:
    identifier = 950002
    factory = async_sessionmaker(engine, expire_on_commit=False)
    service = Memberships(ContractHost(host.service))
    worker = OperationWorker(ContractHost(host.service))
    async with factory.begin() as session:
        await user_dal.create_user(session, {"user_id": identifier, "telegram_id": identifier})
        draft = await invitation(session, manager=identifier)
        result = await service.confirm(session, identifier, draft)
        assert result.operation is not None
        join_id, member_id = result.operation.id, result.operation.membership_id
    async with factory.begin() as session:
        lease = await worker.claim(session, operation_id=join_id)
    assert lease is not None
    async with factory.begin() as session:
        assert await worker.execute(session, lease)
    async with factory.begin() as session:
        departure = await service.depart(
            session, identifier, member_id, DepartMembership(request_id=uuid4())
        )
    async with factory.begin() as session:
        leave_lease = await worker.claim(session, operation_id=departure.id)
    assert leave_lease is not None
    failed = asyncio.Event()
    release = asyncio.Event()

    class PausedAfterNativeRollback(AccessAdapter):
        async def grant_trial(
            self, session: AsyncSession, user_id: int, target: TrialAccess
        ) -> AccessState:
            try:
                return await super().grant_trial(session, user_id, target)
            except AccessError:
                failed.set()
                await release.wait()
                raise

    worker.access = PausedAfterNativeRollback(host.service)
    host.panel.fail_trial_activation = True

    async def failing_delivery() -> bool:
        async with factory.begin() as session:
            return await worker.execute(session, leave_lease)

    task = asyncio.create_task(failing_delivery())
    try:
        await asyncio.wait_for(failed.wait(), timeout=5)
        async with factory.begin() as session:
            assert (
                await worker.claim(
                    session, operation_id=departure.id, now=datetime.now(UTC) + timedelta(minutes=3)
                )
                is None
            )
        # A concurrent native mutation needs this lock. Bound the wait and roll
        # back on timeout; the result proves the outer lock survived rollback.
        async with factory.begin() as session:
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(user_dal.lock_user_by_id(session, identifier), timeout=0.1)
            await session.rollback()
    finally:
        release.set()
        assert not await asyncio.wait_for(task, timeout=5)
    host.panel.fail_trial_activation = False
    worker.access = AccessAdapter(host.service)
    async with factory.begin() as session:
        recovered = await worker.claim(
            session, operation_id=departure.id, now=datetime.now(UTC) + timedelta(minutes=3)
        )
    assert recovered is not None
    async with factory.begin() as session:
        assert await worker.execute(session, recovered)
        member = await session.get(Membership, member_id)
        assert member is not None and member.state == "left"
        reserve = await session.scalar(
            select(Reservation).where(Reservation.operation_id == join_id)
        )
        assert reserve is not None and reserve.state == "consumed"
