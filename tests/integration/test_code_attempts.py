import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from db.dal import security_dal, user_dal
from minishop_corp.integration.code_attempts import FAILURE_SCOPE, MINUTE_SCOPE, CodeAttempts
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from .conftest import USER_ID, Host

pytestmark = pytest.mark.asyncio(loop_scope="session")
START = datetime(2031, 1, 1, tzinfo=UTC)


async def accumulate_errors(session: AsyncSession, user_id: int, count: int) -> datetime:
    now = START
    for error in range(1, count + 1):
        guard = CodeAttempts()
        assert (await guard.admit(session, user_id, now=now)).allowed
        result = await guard.record_failure(session, user_id, now=now)
        expected = 60 if error < 5 else 900 if error < 9 else 3600
        assert not result.allowed and result.retry_after == expected
        if error < count:
            now += timedelta(seconds=expected)
    return now


async def test_successful_previews_and_confirmations_share_minute_interval(host: Host) -> None:
    guard = CodeAttempts()
    first = await guard.admit(host.session, USER_ID, now=START)
    assert first.allowed and first.retry_after == 60
    assert await guard.state(host.session, FAILURE_SCOPE, USER_ID) is None
    denied = await CodeAttempts().admit(host.session, USER_ID, now=START)
    assert not denied.allowed and denied.retry_after == 60
    almost = await guard.admit(host.session, USER_ID, now=START + timedelta(seconds=59.9))
    assert not almost.allowed and almost.retry_after == 1
    assert (await guard.admit(host.session, USER_ID, now=START + timedelta(seconds=60))).allowed


async def test_fifth_and_ninth_errors_raise_rate_until_three_idle_hours(host: Host) -> None:
    guard = CodeAttempts()
    now = START
    for delay in (60, 60, 60, 60, 900, 900, 900, 900, 3600, 3600, 3600):
        assert (await guard.admit(host.session, USER_ID, now=now)).allowed
        result = await guard.record_failure(host.session, USER_ID, now=now)
        assert not result.allowed and result.retry_after == delay
        before = await guard.state(host.session, FAILURE_SCOPE, USER_ID)
        # Rejected requests neither add failures nor push the cooldown forward.
        blocked = await guard.admit(host.session, USER_ID, now=now + timedelta(seconds=delay - 0.1))
        assert not blocked.allowed and blocked.retry_after == 1
        assert await guard.state(host.session, FAILURE_SCOPE, USER_ID) == before
        now += timedelta(seconds=delay)
    assert (await guard.admit(host.session, USER_ID, now=now + timedelta(hours=2))).allowed
    assert await guard.state(host.session, FAILURE_SCOPE, USER_ID) is None


@pytest.mark.parametrize(("errors", "delay"), [(5, 900), (9, 3600)])
async def test_success_keeps_elevated_interval_and_error_history(
    host: Host, errors: int, delay: int
) -> None:
    last_error = await accumulate_errors(host.session, USER_ID, errors)
    guard = CodeAttempts()
    before = await guard.state(host.session, FAILURE_SCOPE, USER_ID)
    now = last_error + timedelta(seconds=delay)
    admitted = await guard.admit(host.session, USER_ID, now=now)
    assert admitted.allowed and admitted.retry_after == delay
    denied = await guard.admit(host.session, USER_ID, now=now + timedelta(seconds=60))
    assert not denied.allowed and denied.retry_after == delay - 60
    assert await guard.state(host.session, FAILURE_SCOPE, USER_ID) == before
    assert await guard.state(host.session, security_dal.PROMO_CODE_APPLY_SCOPE, USER_ID) is None


async def test_idle_reset_uses_last_error_not_first_error(host: Host) -> None:
    guard = CodeAttempts()
    for hours in (0, 2, 4):
        now = START + timedelta(hours=hours)
        assert (await guard.admit(host.session, USER_ID, now=now)).allowed
        await guard.record_failure(host.session, USER_ID, now=now)
    state = await guard.state(host.session, FAILURE_SCOPE, USER_ID)
    assert state is not None and state.failures == 3
    assert (await guard.admit(host.session, USER_ID, now=START + timedelta(hours=7))).allowed
    assert await guard.state(host.session, FAILURE_SCOPE, USER_ID) is None


@pytest.mark.parametrize("seconds_before_reset", [1800, 30])
async def test_cooling_resets_elevated_rate_but_retains_last_minute(
    host: Host, seconds_before_reset: int
) -> None:
    last_error = await accumulate_errors(host.session, USER_ID, 9)
    guard = CodeAttempts()
    reset_at = last_error + timedelta(hours=3)
    success_at = reset_at - timedelta(seconds=seconds_before_reset)
    result = await guard.admit(host.session, USER_ID, now=success_at)
    assert result.allowed and result.retry_after == max(60, seconds_before_reset)
    result = await guard.admit(host.session, USER_ID, now=reset_at)
    assert result.allowed is (seconds_before_reset >= 60)
    assert await guard.state(host.session, FAILURE_SCOPE, USER_ID) is None
    if not result.allowed:
        assert result.retry_after == 30
        result = await guard.admit(host.session, USER_ID, now=reset_at + timedelta(seconds=30))
    assert result.allowed and result.retry_after == 60


async def test_existing_native_promo_lock_is_respected_without_mutation(host: Host) -> None:
    guard = CodeAttempts()
    await security_dal.record_throttle_failure(
        host.session,
        scope=security_dal.PROMO_CODE_APPLY_SCOPE,
        identifier=f"user:{USER_ID}",
        max_failures=1,
        window_seconds=60,
        lock_seconds=1800,
        now=START,
    )
    before = await guard.state(host.session, security_dal.PROMO_CODE_APPLY_SCOPE, USER_ID)
    denied = await guard.admit(host.session, USER_ID, now=START)
    assert not denied.allowed and denied.retry_after == 1800
    assert await guard.state(host.session, MINUTE_SCOPE, USER_ID) is None
    assert await guard.state(host.session, security_dal.PROMO_CODE_APPLY_SCOPE, USER_ID) == before
    assert (await guard.admit(host.session, USER_ID, now=START + timedelta(seconds=1800))).allowed


@pytest.mark.parametrize(("errors", "delay"), [(0, 60), (5, 900), (9, 3600)])
async def test_parallel_requests_and_new_instances_keep_each_rate(
    engine: AsyncEngine, errors: int, delay: int
) -> None:
    factory = async_sessionmaker(engine, expire_on_commit=False)
    user_id = 990000 + errors
    async with factory.begin() as session:
        await user_dal.create_user(session, {"user_id": user_id})
        last_error = await accumulate_errors(session, user_id, errors)
    now = last_error + timedelta(seconds=delay)

    # Use no barrier after opening a connection: the pool is smaller than this burst.
    async def attempt() -> bool:
        async with factory.begin() as session:
            result = await CodeAttempts().admit(session, user_id, now=now)
            return result.allowed

    results = await asyncio.gather(*(attempt() for _ in range(8)))
    assert sum(results) == 1
    async with factory.begin() as session:
        after_restart = CodeAttempts()
        denied = await after_restart.admit(session, user_id, now=now)
        assert not denied.allowed and denied.retry_after == delay
        state = await after_restart.state(session, MINUTE_SCOPE, user_id)
        assert state is not None and state.failures == 1
