"""Regression probes for the Minishop 3.8.2 compatibility assessment.

These keep the existing billing and departure requirements explicit. The accepted
3.8.1 baseline has no provider mandates; on 3.8.2 these expose unresolved blockers.
"""

from datetime import UTC, datetime
from importlib.util import find_spec
from uuid import uuid4

import pytest
from db.dal import payment_dal, user_dal
from minishop_corp.integration.access import AccessAdapter
from minishop_corp.integration.access_types import AccessError

from .conftest import USER_ID, Host
from .test_access import seed_personal, target

pytestmark = [
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        find_spec("db.provider_mandate_models") is None,
        reason="These compatibility probes require Minishop 3.8.2 provider mandates",
    ),
]


@pytest.mark.parametrize(
    "provider", ["cryptomus_subscription", "cispay_subscription_card", "cispay_subscription_sbp"]
)
@pytest.mark.parametrize("status", ["pending", "active", "paused", "past_due"])
async def test_live_provider_mandate_blocks_join_without_local_auto_renew(
    host: Host, provider: str, status: str
) -> None:
    from db.provider_mandate_models import ProviderMandate

    await seed_personal(host, "paid")
    anchor = await payment_dal.create_payment_record(
        host.session,
        {
            "user_id": USER_ID,
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
        user_id=USER_ID,
        provider_customer_id=str(USER_ID),
        status=status,
        period_days=30,
    )
    host.session.add(mandate)
    await host.session.flush()
    before = list(host.panel.requests)
    with pytest.raises(AccessError, match="renewal_policy_required"):
        await AccessAdapter(host.service).validate_join(host.session, USER_ID)
    await host.session.refresh(mandate)
    assert mandate.status == status
    assert host.panel.requests == before


@pytest.mark.parametrize("adds_to_trial", [False, True])
async def test_departure_preserves_unclaimed_referral_welcome_bonus(
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
    assert user.referral_welcome_bonus_claimed_at is None
