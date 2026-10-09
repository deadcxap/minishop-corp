import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from db.dal import security_dal, user_dal
from db.dal.user_merge_dal import delete_user_and_relations
from minishop_corp.contracts import Contracts
from minishop_corp.contracts_types import ContractError, UpdateContract
from minishop_corp.integration.code_attempts import FAILURE_SCOPE, CodeAttempts
from minishop_corp.integration.contracts import ContractHost
from minishop_corp.invitations_types import OfferReference
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
from pydantic import SecretStr
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from .conftest import USER_ID, Host
from .test_invitation_lookup import code, lookup
from .test_memberships import invitation

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def proposal(host: Host) -> tuple[SecretStr, OfferReference, datetime]:
    secret = await code(host)
    now = datetime.now(UTC)
    result = await lookup(host).inspect(host.session, USER_ID, secret, now=now)
    assert result.offer is not None
    return (
        secret,
        OfferReference(
            invitation_id=result.offer.invitation_id,
            contract_id=result.offer.contract.id,
            contract_version=result.offer.contract.version,
        ),
        now,
    )


@pytest.mark.parametrize("tamper", ["code", "version", "account"])
async def test_preview_cannot_exempt_another_guess_or_actor(host: Host, tamper: str) -> None:
    secret, expected, now = await proposal(host)
    actor = USER_ID
    if tamper == "code":
        secret = SecretStr("CORP-" + "0" * 32)
    elif tamper == "version":
        expected = expected.model_copy(update={"contract_version": expected.contract_version + 1})
    else:
        actor = USER_ID + 100
        await user_dal.create_user(host.session, {"user_id": actor})
        await lookup(host).inspect(host.session, actor, SecretStr("unknown"), now=now)
    denied = await lookup(host).inspect(host.session, actor, secret, expected=expected, now=now)
    assert denied.status == 429 and denied.retry_after == 60


async def test_changed_contract_is_rechecked_immediately_without_bad_guess_penalty(
    host: Host,
) -> None:
    secret, expected, now = await proposal(host)
    row = await host.session.get(Contract, expected.contract_id)
    assert row is not None
    await Contracts(ContractHost(host.service)).update(
        host.session,
        USER_ID,
        row.id,
        UpdateContract(
            expected_version=row.version,
            name="Changed corporate subscription",
            tariff_key=row.tariff_key,
            external_squad_uuid=row.external_squad_uuid,
            ends_at=row.ends_at,
            manager_user_id=row.manager_user_id,
        ),
    )
    result = await lookup(host).inspect(host.session, USER_ID, secret, expected=expected, now=now)
    assert result.status == 409 and result.error == "minishop_corp_offer_changed"
    assert await CodeAttempts.state(host.session, FAILURE_SCOPE, USER_ID) is None


async def test_preview_expiry_restores_normal_attempt_accounting(host: Host) -> None:
    secret, expected, now = await proposal(host)
    result = await lookup(host).inspect(
        host.session, USER_ID, secret, expected=expected, now=now + timedelta(minutes=16)
    )
    assert result.status == 200 and result.retry_after == 60


async def test_native_deletion_removes_preview_and_does_not_restore_it_on_registration(
    host: Host,
) -> None:
    secret = await code(host)
    actor = USER_ID + 100
    await user_dal.create_user(host.session, {"user_id": actor})
    await lookup(host).inspect(host.session, actor, secret)
    assert await host.session.get(InvitationPreview, actor) is not None
    assert await delete_user_and_relations(host.session, actor)
    host.session.expire_all()
    assert await host.session.get(InvitationPreview, actor) is None
    await user_dal.create_user(host.session, {"user_id": actor})
    assert await host.session.get(InvitationPreview, actor) is None


async def test_native_promo_lock_still_applies_after_successful_preview(host: Host) -> None:
    secret, expected, now = await proposal(host)
    await security_dal.record_throttle_failure(
        host.session,
        scope=security_dal.PROMO_CODE_APPLY_SCOPE,
        identifier=f"user:{USER_ID}",
        max_failures=1,
        window_seconds=600,
        lock_seconds=600,
        now=now,
    )
    denied = await lookup(host).inspect(host.session, USER_ID, secret, expected=expected, now=now)
    assert denied.status == 429 and denied.retry_after == 600


async def test_two_immediate_confirmations_share_one_membership(
    engine: AsyncEngine, host: Host
) -> None:
    actor = 969001
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory.begin() as session:
        await user_dal.create_user(session, {"user_id": actor})
        draft = await invitation(session, manager=actor)
        assert (await lookup(host).inspect(session, actor, draft.code)).status == 200

    async def join() -> UUID | str:
        async with factory.begin() as session:
            try:
                result = await Memberships(ContractHost(host.service)).confirm(
                    session, actor, draft.model_copy(update={"request_id": uuid4()})
                )
                assert result.operation is not None
                return result.operation.id
            except ContractError as exc:
                return exc.code

    try:
        results = await asyncio.wait_for(asyncio.gather(join(), join()), timeout=15)
        assert sum(isinstance(value, UUID) for value in results) == 1
        assert "minishop_corp_already_member" in results
    finally:
        # Concurrent connections commit outside the host fixture's rollback.
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
                await session.execute(
                    delete(model).where(model.contract_id == draft.offer.contract_id)
                )
            await session.execute(delete(Contract).where(Contract.id == draft.offer.contract_id))
            await delete_user_and_relations(session, actor)
