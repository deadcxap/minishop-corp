from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from bot.services.tariff_worker import TariffTrafficWorker
from db.dal import payment_dal, subscription_dal, tariff_dal, user_dal
from db.dal import user_panel_squad_override_dal as squad_dal
from db.models import Payment, Subscription
from minishop_corp.integration.access import AccessAdapter
from minishop_corp.integration.access_types import (
    AccessError,
    AccessFailure,
    PeriodAccess,
    panel_time,
)
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from .conftest import CORP_SQUAD, EXTERNAL_SQUAD, TRIAL_SQUAD, USER_ID, Host

pytestmark = pytest.mark.asyncio(loop_scope="session")


def target(days: int = 10, *, external: str = EXTERNAL_SQUAD) -> PeriodAccess:
    return PeriodAccess("corp", datetime.now(UTC) + timedelta(days=days), external)


async def seed_personal(host: Host, kind: str) -> None:
    if kind == "new":
        return
    if kind == "trial":
        result = await host.service.activate_trial_subscription(
            host.session, USER_ID, commit=False, emit_event=False
        )
        assert result and result["activated"]
        return
    result = await host.service.extend_active_subscription_days(
        host.session, USER_ID, 60, reason="admin", tariff_key="personal"
    )
    assert result is not None
    row = await subscription_dal.get_active_subscription_by_user_id(host.session, USER_ID)
    assert row is not None
    data: dict[str, object] = {"provider": "yookassa", "auto_renew_enabled": False}
    if kind == "expired":
        data.update(is_active=False, end_date=datetime.now(UTC) - timedelta(days=1))
    await subscription_dal.update_subscription(host.session, row.subscription_id, data)


def assert_panel(host: Host, panel_uuid: str, expected: PeriodAccess) -> None:
    panel = host.panel.users[panel_uuid]
    assert datetime.fromisoformat(str(panel["expireAt"])) == expected.ends_at
    assert panel["externalSquadUuid"] == expected.external_squad_uuid
    assert panel["activeInternalSquads"] == [CORP_SQUAD]
    assert panel["tag"] == "CORP"
    assert panel["trafficLimitBytes"] == 20 * 1024**3
    assert panel["hwidDeviceLimit"] == 5
    assert panel["status"] == ("ACTIVE" if expected.ends_at > datetime.now(UTC) else "EXPIRED")


@pytest.mark.parametrize("before", ["new", "trial", "paid", "expired"])
async def test_assign_hidden_tariff_and_exact_expiry(host: Host, before: str) -> None:
    await seed_personal(host, before)
    adapter = AccessAdapter(host.service)
    expected = target()
    state = await adapter.assign_period(host.session, USER_ID, expected)
    assert state.tariff_key == "corp"
    assert state.end_date == expected.ends_at
    assert state.is_active
    assert state.hwid_device_limit == 5
    assert_panel(host, state.panel_user_uuid, expected)
    assert await host.session.scalar(select(func.count()).select_from(Payment)) == 0
    again = await adapter.assign_period(host.session, USER_ID, expected)
    assert again.subscription_id == state.subscription_id
    assert again.start_date == state.start_date
    assert again.end_date == state.end_date
    assert len(host.panel.users) == 1
    assert await adapter.read(host.session, USER_ID) == again


async def test_expired_contract_and_renewal(host: Host) -> None:
    adapter = AccessAdapter(host.service)
    expired = target(-1)
    state = await adapter.assign_period(host.session, USER_ID, expired)
    assert not state.is_active
    assert_panel(host, state.panel_user_uuid, expired)
    assert await subscription_dal.get_active_subscription_by_user_id(host.session, USER_ID) is None
    renewed = target(40)
    state = await adapter.assign_period(host.session, USER_ID, renewed)
    assert state.is_active
    assert_panel(host, state.panel_user_uuid, renewed)
    shortened = target(2)
    state = await adapter.assign_period(host.session, USER_ID, shortened)
    assert_panel(host, state.panel_user_uuid, shortened)


async def test_native_worker_preserves_corporate_squads_and_limits(
    host: Host, caplog: pytest.LogCaptureFixture
) -> None:
    adapter = AccessAdapter(host.service)
    expected = target()
    state = await adapter.assign_period(host.session, USER_ID, expected)
    override = await squad_dal.get_active_external_override(
        host.session, user_id=USER_ID, panel_user_uuid=state.panel_user_uuid
    )
    assert override is not None and override.squad_uuid == EXTERNAL_SQUAD
    worker = TariffTrafficWorker(
        host.settings,
        async_sessionmaker(bind=host.session.bind, expire_on_commit=False),
        host.service.panel_service,
        host.service,
    )
    # Prove the real tick repairs from the host model, rather than merely doing nothing.
    host.panel.users[state.panel_user_uuid].update(
        trafficLimitBytes=1, hwidDeviceLimit=1, activeInternalSquads=[]
    )
    caplog.clear()
    await worker.traffic_period_tick(host.session)
    assert_panel(host, state.panel_user_uuid, expected)
    assert not [record for record in caplog.records if record.levelno >= 40]
    changed = target(20, external="20000000-0000-4000-8000-000000000002")
    await adapter.assign_period(host.session, USER_ID, changed)
    await worker.traffic_period_tick(host.session)
    assert_panel(host, state.panel_user_uuid, changed)


async def test_trial_is_new_standard_and_retry_keeps_its_window_and_history(host: Host) -> None:
    await seed_personal(host, "trial")
    adapter = AccessAdapter(host.service)
    corporate = await adapter.assign_period(host.session, USER_ID, target())
    # Historical rows may have a later expiry than the current corporate subscription.
    history = await subscription_dal.upsert_subscription(
        host.session,
        {
            "user_id": USER_ID,
            "panel_user_uuid": corporate.panel_user_uuid,
            "panel_subscription_uuid": str(uuid4()),
            "start_date": datetime.now(UTC) - timedelta(days=90),
            "end_date": datetime.now(UTC) + timedelta(days=90),
            "provider": "yookassa",
            "is_active": False,
            "auto_renew_enabled": False,
        },
    )
    payment = await payment_dal.create_payment_record(
        host.session,
        {"user_id": USER_ID, "amount": 100, "currency": "RUB", "status": "succeeded"},
    )
    trial = adapter.prepare_trial(starts_at=datetime.now(UTC))
    first = await adapter.grant_trial(host.session, USER_ID, trial)
    assert first.provider == "trial"
    assert first.tariff_key is None
    assert first.start_date == trial.starts_at
    assert first.end_date - first.start_date == timedelta(days=3)
    assert first.traffic_limit_bytes == 1024**3
    assert first.hwid_device_limit == 1
    panel = host.panel.users[first.panel_user_uuid]
    assert panel["activeInternalSquads"] == [TRIAL_SQUAD]
    assert panel["externalSquadUuid"] is None
    assert panel["tag"] == "TRIAL"
    assert panel["trafficLimitBytes"] == 1024**3
    assert panel["hwidDeviceLimit"] == 1
    before_requests = len(host.panel.requests)
    again = await adapter.grant_trial(host.session, USER_ID, trial)
    assert again == first
    # A retry only verifies/repairs existing access. It does not reset eligibility again.
    assert sum(method == "POST" for method, _ in host.panel.requests[before_requests:]) == 0
    await host.session.refresh(history)
    assert history.provider == "yookassa" and not history.is_active
    assert await payment_dal.get_payment_by_db_id(host.session, payment.payment_id) is not None
    assert await host.session.scalar(select(func.count()).select_from(Subscription)) == 2


@pytest.mark.parametrize("failure", ["fail_updates", "echo_without_saving"])
async def test_failure_is_not_success_and_retry_uses_same_expiry(host: Host, failure: str) -> None:
    adapter = AccessAdapter(host.service)
    await adapter.assign_period(host.session, USER_ID, target())
    expected = target(5)
    setattr(host.panel, failure, True)
    with pytest.raises(AccessError) as error:
        await adapter.assign_period(host.session, USER_ID, expected)
    assert error.value.code == AccessFailure.PANEL_UNCONFIRMED
    setattr(host.panel, failure, False)
    state = await adapter.assign_period(host.session, USER_ID, expected)
    assert_panel(host, state.panel_user_uuid, expected)


async def test_lost_update_response_is_verified_by_native_read(host: Host) -> None:
    adapter = AccessAdapter(host.service)
    await adapter.assign_period(host.session, USER_ID, target())
    host.panel.lose_update_response = True
    expected = target(3)
    state = await adapter.assign_period(host.session, USER_ID, expected)
    assert_panel(host, state.panel_user_uuid, expected)


async def test_disabled_trial_and_autorenew_need_policy_before_mutation(host: Host) -> None:
    adapter = AccessAdapter(host.service)
    state = await adapter.assign_period(host.session, USER_ID, target())
    trial = adapter.prepare_trial(starts_at=datetime.now(UTC))
    before_requests = len(host.panel.requests)
    host.settings.TRIAL_ENABLED = False
    with pytest.raises(AccessError) as error:
        await adapter.grant_trial(host.session, USER_ID, trial)
    assert error.value.code == AccessFailure.TRIAL_UNAVAILABLE
    assert len(host.panel.requests) == before_requests
    await subscription_dal.update_subscription(
        host.session, state.subscription_id, {"auto_renew_enabled": True}
    )
    with pytest.raises(AccessError) as error:
        await adapter.assign_period(host.session, USER_ID, target(20))
    assert error.value.code == AccessFailure.RENEWAL_POLICY_REQUIRED
    assert (await adapter.read(host.session, USER_ID)).end_date == state.end_date


async def test_trial_retry_after_its_expiry_does_not_reopen_access(host: Host) -> None:
    adapter = AccessAdapter(host.service)
    await adapter.assign_period(host.session, USER_ID, target())
    trial = adapter.prepare_trial(starts_at=panel_time(datetime.now(UTC) - timedelta(days=10)))
    state = await adapter.grant_trial(host.session, USER_ID, trial)
    assert not state.is_active
    assert host.panel.users[state.panel_user_uuid]["status"] == "EXPIRED"
    again = await adapter.grant_trial(host.session, USER_ID, trial)
    assert again.end_date == state.end_date and not again.is_active


async def test_rollback_after_panel_trial_activation_reuses_persisted_window(host: Host) -> None:
    adapter = AccessAdapter(host.service)
    corporate = await adapter.assign_period(host.session, USER_ID, target())
    await host.session.commit()
    # A delayed job must retain the operation's original window, not now + 3 days.
    trial = adapter.prepare_trial(starts_at=datetime.now(UTC) - timedelta(hours=2))
    host.panel.fail_after_trial_activation = True
    with pytest.raises(AccessError) as error:
        async with host.session.begin_nested():
            await adapter.grant_trial(host.session, USER_ID, trial)
    assert error.value.code == AccessFailure.PANEL_UNCONFIRMED
    after_rollback = await adapter.read(host.session, USER_ID)
    assert after_rollback is not None and after_rollback.tariff_key == "corp"
    assert host.panel.users[corporate.panel_user_uuid]["tag"] == "TRIAL"
    host.panel.fail_after_trial_activation = False
    # A fresh adapter represents a restarted job, with the same persisted target.
    result = await AccessAdapter(host.service).grant_trial(host.session, USER_ID, trial)
    assert result.start_date == trial.starts_at and result.end_date == trial.ends_at
    assert len(host.panel.users) == 1
    assert await host.session.scalar(select(func.count()).select_from(Subscription)) == 1


async def test_reserved_personal_days_do_not_override_contract_end(host: Host) -> None:
    await seed_personal(host, "paid")
    await user_dal.update_user(
        host.session,
        USER_ID,
        {"period_accrual_reserved_until": datetime.now(UTC) + timedelta(days=200)},
    )
    adapter = AccessAdapter(host.service)
    expected = target()
    state = await adapter.assign_period(host.session, USER_ID, expected)
    assert state.end_date == expected.ends_at
    assert_panel(host, state.panel_user_uuid, expected)


async def test_trial_clears_old_unlimited_overrides_and_uses_host_default_external(
    host: Host,
) -> None:
    adapter = AccessAdapter(host.service)
    state = await adapter.assign_period(host.session, USER_ID, target())
    await subscription_dal.update_subscription(
        host.session,
        state.subscription_id,
        {
            "regular_unlimited_override": True,
            "premium_unlimited_override": True,
            "regular_bonus_bytes": 10 * 1024**3,
            "premium_bonus_bytes": 20 * 1024**3,
            "topup_balance_bytes": 15 * 1024**3,
            "premium_topup_balance_bytes": 16 * 1024**3,
            "hwid_device_limit_is_override": True,
            "hwid_device_limit": 100,
        },
    )
    default = "20000000-0000-4000-8000-000000000009"
    host.settings.USER_EXTERNAL_SQUAD_UUID = default
    trial = adapter.prepare_trial(starts_at=datetime.now(UTC))
    result = await adapter.grant_trial(host.session, USER_ID, trial)
    assert not result.regular_unlimited_override and not result.premium_unlimited_override
    assert result.regular_bonus_bytes == result.premium_bonus_bytes == 0
    assert result.topup_balance_bytes == result.premium_topup_balance_bytes == 0
    assert result.hwid_device_limit == 1
    assert host.panel.users[result.panel_user_uuid]["externalSquadUuid"] == default
    assert (
        await squad_dal.get_active_external_override(
            host.session, user_id=USER_ID, panel_user_uuid=result.panel_user_uuid
        )
        is None
    )


async def test_unknown_tariff_and_missing_user_do_not_grant_access(host: Host) -> None:
    adapter = AccessAdapter(host.service)
    with pytest.raises(AccessError) as error:
        await adapter.assign_period(host.session, USER_ID + 1, target())
    assert error.value.code == AccessFailure.USER_MISSING
    with pytest.raises(AccessError) as error:
        await adapter.assign_period(
            host.session, USER_ID, PeriodAccess("missing", target().ends_at, EXTERNAL_SQUAD)
        )
    assert error.value.code == AccessFailure.TARIFF_UNAVAILABLE
    assert not host.panel.users


async def test_trial_retires_extra_devices_and_native_sync_keeps_standard_limit(host: Host) -> None:
    adapter = AccessAdapter(host.service)
    corporate = await adapter.assign_period(host.session, USER_ID, target())
    purchase = await tariff_dal.create_hwid_device_purchase(
        host.session,
        subscription_id=corporate.subscription_id,
        payment_id=None,
        purchased_devices=10,
        valid_until=datetime.now(UTC) + timedelta(days=30),
    )
    host.settings.USER_HWID_DEVICE_LIMIT = 99
    trial = adapter.prepare_trial(starts_at=datetime.now(UTC))
    result = await adapter.grant_trial(host.session, USER_ID, trial)
    await host.session.refresh(purchase)
    assert purchase.valid_until == trial.starts_at
    assert await host.service.sync_hwid_device_limit_to_panel(host.session, USER_ID) == 1
    assert host.panel.users[result.panel_user_uuid]["hwidDeviceLimit"] == 1


async def test_flexible_quota_window_is_rejected_before_changing_access(host: Host) -> None:
    adapter = AccessAdapter(host.service)
    corporate = await adapter.assign_period(host.session, USER_ID, target())
    await tariff_dal.create_flexible_traffic_limit(
        host.session,
        subscription_id=corporate.subscription_id,
        payment_id=None,
        kind="traffic",
        tariff_key="corp",
        limit_bytes=100 * 1024**3,
        valid_from=datetime.now(UTC) + timedelta(days=1),
        valid_until=datetime.now(UTC) + timedelta(days=30),
    )
    before_requests = len(host.panel.requests)
    with pytest.raises(AccessError) as error:
        await adapter.assign_period(host.session, USER_ID, target(2))
    assert error.value.code == AccessFailure.FLEXIBLE_LIMITS_UNSUPPORTED
    trial = adapter.prepare_trial(starts_at=datetime.now(UTC))
    with pytest.raises(AccessError) as error:
        await adapter.grant_trial(host.session, USER_ID, trial)
    assert error.value.code == AccessFailure.FLEXIBLE_LIMITS_UNSUPPORTED
    assert all(method == "GET" for method, _ in host.panel.requests[before_requests:])
    assert (await adapter.read(host.session, USER_ID)).end_date == corporate.end_date


async def test_native_manual_tag_ownership_is_preserved(host: Host) -> None:
    await seed_personal(host, "paid")
    panel = next(iter(host.panel.users.values()))
    panel["tag"] = "MANUAL"
    manual_squad = "10000000-0000-4000-8000-000000000099"
    panel["activeInternalSquads"] = [*panel["activeInternalSquads"], manual_squad]
    adapter = AccessAdapter(host.service)
    result = await adapter.assign_period(host.session, USER_ID, target())
    assert host.panel.users[result.panel_user_uuid]["tag"] == "MANUAL"
    assert set(host.panel.users[result.panel_user_uuid]["activeInternalSquads"]) == {
        CORP_SQUAD,
        manual_squad,
    }
    trial = adapter.prepare_trial(starts_at=datetime.now(UTC))
    result = await adapter.grant_trial(host.session, USER_ID, trial)
    assert host.panel.users[result.panel_user_uuid]["tag"] == "MANUAL"
    assert set(host.panel.users[result.panel_user_uuid]["activeInternalSquads"]) == {
        TRIAL_SQUAD,
        manual_squad,
    }


@pytest.mark.parametrize("identity", ["telegram", "email", "none"])
async def test_lost_creation_response_does_not_duplicate_panel_account(
    host: Host, identity: str
) -> None:
    adapter = AccessAdapter(host.service)
    expected = target()
    if identity == "telegram":
        await user_dal.update_user(host.session, USER_ID, {"telegram_id": 1920001})
    elif identity == "email":
        await user_dal.update_user(
            host.session,
            USER_ID,
            {"email": "fixture@example.invalid", "email_verified_at": datetime.now(UTC)},
        )
    await host.session.commit()
    host.panel.lose_create_response = True
    with pytest.raises(AccessError) as error:
        await adapter.assign_period(host.session, USER_ID, expected)
    assert error.value.code == AccessFailure.PANEL_UNCONFIRMED
    await host.session.rollback()
    assert len(host.panel.users) == 1
    host.panel.lose_create_response = False
    if identity == "none":
        # Native collision protection requires TG/verified email to adopt an unlinked
        # profile. Preserve that safeguard and surface the need for reconciliation.
        with pytest.raises(AccessError) as error:
            await AccessAdapter(host.service).assign_period(host.session, USER_ID, expected)
        assert error.value.code == AccessFailure.PANEL_UNCONFIRMED
    else:
        state = await AccessAdapter(host.service).assign_period(host.session, USER_ID, expected)
        assert_panel(host, state.panel_user_uuid, expected)
    assert len(host.panel.users) == 1
    assert sum(method == "POST" and path == "users" for method, path in host.panel.requests) == 1
