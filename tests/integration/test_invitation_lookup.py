from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from bot.services.account_roles import grant_role
from minishop_corp.integration.code_attempts import FAILURE_SCOPE, AttemptPolicy, CodeAttempts
from minishop_corp.integration.contracts import ContractHost
from minishop_corp.invitation_lookup import InvitationLookup
from minishop_corp.invitations import Invitations
from minishop_corp.invitations_types import CreateInvitation, OfferReference
from minishop_corp.storage.schema import Contract, Invitation, Membership
from pydantic import SecretStr
from sqlalchemy import func, select

from .conftest import USER_ID, Host
from .test_storage import seed_contract

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def code(host: Host) -> SecretStr:
    await grant_role(host.session, USER_ID, "admin", source="corp_lookup_test")
    contract = await seed_contract(host.session)
    issued = await Invitations(ContractHost(host.service)).create(
        host.session,
        USER_ID,
        contract.id,
        CreateInvitation(id=uuid4(), kind="reusable", use_limit=5),
    )
    assert issued.code is not None
    return issued.code


def lookup(host: Host, limit: int = 5) -> InvitationLookup:
    return InvitationLookup(
        ContractHost(host.service), AttemptPolicy(limit, limit, (60, 300, 900, 3600), 86400)
    )


async def test_preview_and_confirmation_use_same_budget_without_consuming_code(host: Host) -> None:
    secret = await code(host)
    now = datetime.now(UTC)
    service = lookup(host)
    preview = await service.inspect(host.session, USER_ID, secret, now=now)
    assert preview.status == 200 and preview.offer is not None and preview.tariff is not None
    assert preview.tariff.names == {"ru": "corp", "en": "corp"}
    assert preview.tariff.traffic_limit_bytes == 20 * 1024**3
    assert preview.tariff.hwid_device_limit == 5
    assert "access_code" not in preview.tariff.model_dump()
    expected = OfferReference(
        invitation_id=preview.offer.invitation_id,
        contract_id=preview.offer.contract.id,
        contract_version=preview.offer.contract.version,
    )
    for _ in range(4):
        assert (
            await service.inspect(host.session, USER_ID, secret, expected=expected, now=now)
        ).status == 200
    denied = await lookup(host).inspect(host.session, USER_ID, secret, expected=expected, now=now)
    assert denied.status == 429 and denied.retry_after == 60
    invitation = await host.session.get(Invitation, expected.invitation_id)
    assert invitation is not None and invitation.used_count == invitation.reserved_count == 0
    assert (
        await host.session.scalar(
            select(func.count()).select_from(Membership).where(Membership.user_id == USER_ID)
        )
        == 0
    )


async def test_invalid_codes_commit_failure_budget_and_do_not_echo_secret(host: Host) -> None:
    now = datetime.now(UTC)
    service = lookup(host)
    secret = SecretStr("CORP-" + "0" * 32)
    for _ in range(4):
        result = await service.inspect(host.session, USER_ID, secret, now=now)
        assert result.status == 400 and result.error == "minishop_corp_invitation_unavailable"
    result = await service.inspect(host.session, USER_ID, secret, now=now)
    assert result.status == 429 and result.retry_after == 60
    assert secret.get_secret_value() not in repr(result)
    await host.session.commit()  # An expected error response must retain the budget.
    state = await CodeAttempts.state(host.session, FAILURE_SCOPE, USER_ID)
    assert state is not None and state.failures == 5
    assert (await lookup(host).inspect(host.session, USER_ID, secret, now=now)).status == 429


async def test_changed_offer_and_host_configuration_error_do_not_add_code_failures(
    host: Host,
) -> None:
    secret = await code(host)
    service = lookup(host)
    preview = await service.inspect(host.session, USER_ID, secret)
    assert preview.offer is not None
    expected = OfferReference(
        invitation_id=preview.offer.invitation_id,
        contract_id=preview.offer.contract.id,
        contract_version=preview.offer.contract.version + 1,
    )
    changed = await service.inspect(host.session, USER_ID, secret, expected=expected)
    assert changed.status == 409 and changed.error == "minishop_corp_offer_changed"
    contract = await host.session.get(Contract, expected.contract_id)
    assert contract is not None
    contract.tariff_key = "removed-from-host-catalog"
    await host.session.flush()
    unavailable = await service.inspect(host.session, USER_ID, secret)
    assert unavailable.status == 503 and unavailable.error == "minishop_corp_tariff_unavailable"
    assert await CodeAttempts.state(host.session, FAILURE_SCOPE, USER_ID) is None


async def test_revocation_is_checked_again_for_confirmation(host: Host) -> None:
    secret = await code(host)
    service = lookup(host)
    now = datetime.now(UTC)
    preview = await service.inspect(host.session, USER_ID, secret, now=now)
    assert preview.offer is not None
    expected = OfferReference(
        invitation_id=preview.offer.invitation_id,
        contract_id=preview.offer.contract.id,
        contract_version=preview.offer.contract.version,
    )
    await Invitations(ContractHost(host.service)).revoke(
        host.session, USER_ID, expected.contract_id, expected.invitation_id
    )
    confirmed = await service.inspect(
        host.session, USER_ID, secret, expected=expected, now=now + timedelta(seconds=1)
    )
    assert confirmed.status == 400 and confirmed.offer is None
