from datetime import UTC, datetime, timedelta

import pytest
from db.dal import subscription_dal, user_dal

from .conftest import USER_ID, Host

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def test_native_extension_cannot_shorten_absolute_expiry(host: Host) -> None:
    later = datetime.now(UTC) + timedelta(days=30)
    first = await host.service.extend_active_subscription_days(
        host.session, USER_ID, 1, reason="admin", tariff_key="corp", target_end_date=later
    )
    assert first == later
    earlier = later - timedelta(days=20)
    second = await host.service.extend_active_subscription_days(
        host.session, USER_ID, 1, reason="admin", tariff_key="corp", target_end_date=earlier
    )
    assert second == later


async def test_native_switch_requires_active_subscription(host: Host) -> None:
    result = await host.service.switch_tariff_without_payment(
        host.session, USER_ID, "corp", mode="admin_assign"
    )
    assert result is None
    assert host.panel.users == {}


async def test_native_trial_reset_does_not_clear_previous_tariff(host: Host) -> None:
    await host.service.extend_active_subscription_days(
        host.session, USER_ID, 30, reason="admin", tariff_key="corp"
    )
    sub = await subscription_dal.get_active_subscription_by_user_id(host.session, USER_ID)
    assert sub is not None
    await subscription_dal.update_subscription(
        host.session, sub.subscription_id, {"is_active": False}
    )
    await user_dal.mark_trial_eligibility_reset(host.session, USER_ID, reset_at=datetime.now(UTC))
    result = await host.service.activate_trial_subscription(
        host.session, USER_ID, commit=False, emit_event=False
    )
    assert result and result["activated"]
    await host.session.refresh(sub)
    assert sub.provider == "trial"
    # A wrapper must clear the old binding, otherwise the host worker reapplies it.
    assert sub.tariff_key == "corp"
