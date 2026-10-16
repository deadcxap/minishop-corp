"""Native 3.8.2 mandates and the accepted standard-trial welcome bonus policy.

3.8.1 has neither provider mandates nor deferred welcome bonuses.
"""

from __future__ import annotations

from datetime import UTC, datetime
from importlib.util import find_spec
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from bot.services.account_roles import grant_role
from db.dal import payment_dal, user_dal
from minishop_corp.contracts_types import ContractError
from minishop_corp.integration.access import AccessAdapter
from minishop_corp.integration.access_types import AccessError
from minishop_corp.integration.contracts import ContractHost
from minishop_corp.membership_types import AddMembership
from minishop_corp.memberships import Memberships
from minishop_corp.storage.schema import Invitation, Operation

from .conftest import USER_ID, Host
from .test_access import seed_personal, target
from .test_memberships import execute, invitation

if TYPE_CHECKING:
    from db.provider_mandate_models import ProviderMandate

PROVIDERS = ("cryptomus_subscription", "cispay_subscription_card", "cispay_subscription_sbp")

pytestmark = [
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        find_spec("db.provider_mandate_models") is None,
        reason="These compatibility probes require Minishop 3.8.2 provider mandates",
    ),
]


async def seed_mandate(
    host: Host, provider: str, status: str, *, user_id: int = USER_ID
) -> ProviderMandate:
    from db.provider_mandate_models import ProviderMandate

    anchor = await payment_dal.create_payment_record(
        host.session,
        {
            "user_id": user_id,
            "amount": 100,
            "currency": "RUB",
            "status": "succeeded",
            "provider": provider,
        },
    )
    mandate = ProviderMandate(
        provider=provider,
        remote_id=str(uuid4()),
        anchor_payment_id=anchor.payment_id,
        user_id=user_id,
        provider_customer_id=str(user_id),
        status=status,
        period_days=30,
    )
    host.session.add(mandate)
    await host.session.flush()
    return mandate


@pytest.mark.parametrize("provider", PROVIDERS)
@pytest.mark.parametrize("status", ["pending", "active", "paused", "past_due"])
async def test_live_provider_mandate_blocks_join_without_local_auto_renew(
    host: Host, provider: str, status: str
) -> None:
    await seed_personal(host, "paid")
    mandate = await seed_mandate(host, provider, status)
    before = list(host.panel.requests)
    with pytest.raises(AccessError, match="renewal_policy_required"):
        await AccessAdapter(host.service).validate_join(host.session, USER_ID)
    await host.session.refresh(mandate)
    assert mandate.status == status
    assert host.panel.requests == before


@pytest.mark.parametrize("provider", PROVIDERS)
@pytest.mark.parametrize("status", ["cancelled", "failed"])
async def test_terminal_mandate_does_not_block_join(host: Host, provider: str, status: str) -> None:
    mandate = await seed_mandate(host, provider, status)
    await AccessAdapter(host.service).validate_join(host.session, USER_ID)
    await host.session.refresh(mandate)
    assert mandate.status == status and host.panel.requests == []


async def test_other_accounts_live_mandates_do_not_block_join(host: Host) -> None:
    await user_dal.create_user(host.session, {"user_id": USER_ID + 1})
    for provider in PROVIDERS:
        await seed_mandate(host, provider, "active", user_id=USER_ID + 1)
    await AccessAdapter(host.service).validate_join(host.session, USER_ID)
    assert host.panel.requests == []


@pytest.mark.parametrize("enrollment", ["invitation", "admin"])
async def test_live_mandate_rejection_does_not_enroll_or_reserve(
    host: Host, enrollment: str
) -> None:
    draft = await invitation(host.session)
    mandate = await seed_mandate(host, "cryptomus_subscription", "active")
    service = Memberships(ContractHost(host.service))
    if enrollment == "admin":
        await grant_role(host.session, USER_ID, "admin", source="corp_compatibility_fixture")
        with pytest.raises(ContractError, match="renewal_policy_required"):
            await service.add(
                host.session,
                USER_ID,
                draft.offer.contract_id,
                AddMembership(request_id=uuid4(), user_id=USER_ID, expected_version=1),
            )
    else:
        result = await service.confirm(host.session, USER_ID, draft)
        assert result.status == 409 and result.error == "minishop_corp_renewal_policy_required"
        assert result.operation is None
    assert await service.current(host.session, USER_ID) is None
    invite = await host.session.get(Invitation, draft.offer.invitation_id)
    assert invite is not None and invite.used_count == invite.reserved_count == 0
    await host.session.refresh(mandate)
    assert mandate.status == "active" and host.panel.requests == []


@pytest.mark.parametrize("enrollment", ["invitation", "admin"])
async def test_mandate_created_before_delivery_blocks_until_cancelled(
    host: Host, enrollment: str
) -> None:
    draft = await invitation(host.session)
    service = Memberships(ContractHost(host.service))
    if enrollment == "admin":
        await grant_role(host.session, USER_ID, "admin", source="corp_compatibility_fixture")
        accepted = await service.add(
            host.session,
            USER_ID,
            draft.offer.contract_id,
            AddMembership(request_id=uuid4(), user_id=USER_ID, expected_version=1),
        )
    else:
        result = await service.confirm(host.session, USER_ID, draft)
        assert result.operation is not None
        accepted = result.operation
    mandate = await seed_mandate(host, "cispay_subscription_sbp", "pending")
    assert not await execute(host, accepted.id)
    operation = await host.session.get(Operation, accepted.id)
    assert operation is not None and operation.state == "retry"
    assert operation.error_code == "minishop_corp_renewal_policy_required"
    assert not host.panel.users and host.panel.requests == []
    invite = await host.session.get(Invitation, draft.offer.invitation_id)
    assert invite is not None and invite.used_count == 0
    assert invite.reserved_count == (1 if enrollment == "invitation" else 0)
    await host.session.refresh(mandate)
    assert mandate.status == "pending"
    # Simulate Minishop confirming cancellation; the plugin never cancels it.
    mandate.status = "cancelled"
    await host.session.flush()
    assert await execute(host, accepted.id, due=True)
    current = await service.current(host.session, USER_ID)
    assert current is not None and current.state == "active"
    await host.session.refresh(invite)
    assert invite.reserved_count == 0
    assert invite.used_count == (1 if enrollment == "invitation" else 0)


@pytest.mark.parametrize("adds_to_trial", [False, True])
async def test_departure_keeps_standard_trial_and_native_welcome_bonus_consumption(
    host: Host, adds_to_trial: bool
) -> None:
    from bot.services.referral_welcome_trial import referral_welcome_trial_bonus_days

    host.settings.REFERRAL_PROGRAM_ENABLED = True
    host.settings.REFERRAL_WELCOME_BONUS_DAYS = 7
    host.settings.REFERRAL_WELCOME_BONUS_ADDS_TO_TRIAL = adds_to_trial
    host.settings.REFERRAL_WELCOME_BONUS_WITHOUT_TELEGRAM_ENABLED = True
    host.settings.TRIAL_PAYMENT_ENABLED = False
    await user_dal.create_user(
        host.session, {"user_id": USER_ID + 1, "first_name": "Referrer fixture"}
    )
    user = await user_dal.update_user(host.session, USER_ID, {"referred_by_id": USER_ID + 1})
    assert user is not None and user.referral_welcome_bonus_claimed_at is None
    assert await referral_welcome_trial_bonus_days(host.session, host.settings, user) == (
        7 if adds_to_trial else 0
    )
    adapter = AccessAdapter(host.service)
    await adapter.assign_period(host.session, USER_ID, target())
    departure = adapter.prepare_trial(starts_at=datetime.now(UTC))
    trial = await adapter.grant_trial(host.session, USER_ID, departure)
    assert (trial.end_date - trial.start_date).days == host.settings.TRIAL_DURATION_DAYS
    await host.session.refresh(user)
    claimed_at = user.referral_welcome_bonus_claimed_at
    assert (claimed_at is not None) == adds_to_trial
    repeated = await adapter.grant_trial(host.session, USER_ID, departure)
    await host.session.refresh(user)
    assert repeated.start_date == trial.start_date and repeated.end_date == trial.end_date
    assert user.referral_welcome_bonus_claimed_at == claimed_at
