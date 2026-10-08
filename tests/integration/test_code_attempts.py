import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from db.dal import security_dal, user_dal
from minishop_corp.integration.code_attempts import (
    FAILURE_SCOPE,
    MINUTE_SCOPE,
    AttemptPolicy,
    CodeAttempts,
)
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from .conftest import USER_ID, Host

pytestmark = pytest.mark.asyncio(loop_scope="session")
START = datetime(2031, 1, 1, tzinfo=UTC)


def policy(limit: int = 5) -> AttemptPolicy:
    # Both proposed Q-01 choices are test inputs; no production default is selected.
    return AttemptPolicy(limit, limit, (60, 300, 900, 3600), 86400)


@pytest.mark.parametrize("limit", [5, 10])
async def test_successful_previews_and_confirmations_share_minute_budget(
    host: Host, limit: int
) -> None:
    guard = CodeAttempts(policy(limit))
    for _ in range(limit):
        assert (await guard.admit(host.session, USER_ID, now=START)).allowed
    assert await guard.state(host.session, FAILURE_SCOPE, USER_ID) is None
    denied = await CodeAttempts(policy(limit)).admit(host.session, USER_ID, now=START)
    assert not denied.allowed and denied.retry_after == 60
    almost = await guard.admit(host.session, USER_ID, now=START + timedelta(seconds=59.9))
    assert not almost.allowed and almost.retry_after == 1
    assert (await guard.admit(host.session, USER_ID, now=START + timedelta(seconds=60))).allowed


@pytest.mark.parametrize("limit", [5, 10])
async def test_failure_stages_are_capped_and_reset_after_idle(host: Host, limit: int) -> None:
    guard = CodeAttempts(policy(limit))
    now = START
    for expected_delay in (60, 300, 900, 3600, 3600):
        for index in range(limit):
            assert (await guard.admit(host.session, USER_ID, now=now)).allowed
            result = await guard.record_failure(host.session, USER_ID, now=now)
            if index + 1 < limit:
                assert result.allowed
        assert not result.allowed and result.retry_after == expected_delay
        before = await guard.state(host.session, FAILURE_SCOPE, USER_ID)
        assert not (await guard.admit(host.session, USER_ID, now=now)).allowed
        assert await guard.state(host.session, FAILURE_SCOPE, USER_ID) == before
        now += timedelta(seconds=expected_delay)
    assert (await guard.admit(host.session, USER_ID, now=now + timedelta(days=1))).allowed
    assert await guard.state(host.session, FAILURE_SCOPE, USER_ID) is None


async def test_success_does_not_erase_error_history_or_touch_promo_budget(host: Host) -> None:
    guard = CodeAttempts(policy())
    for index in range(4):
        now = START + timedelta(minutes=index)
        assert (await guard.admit(host.session, USER_ID, now=now)).allowed
        await guard.record_failure(host.session, USER_ID, now=now)
    before = await guard.state(host.session, FAILURE_SCOPE, USER_ID)
    # A successful lookup or infrastructure failure only calls admit, never record_failure.
    assert (await guard.admit(host.session, USER_ID, now=START + timedelta(minutes=5))).allowed
    assert await guard.state(host.session, FAILURE_SCOPE, USER_ID) == before
    assert await guard.state(host.session, security_dal.PROMO_CODE_APPLY_SCOPE, USER_ID) is None


async def test_idle_reset_uses_last_error_not_first_error(host: Host) -> None:
    guard = CodeAttempts(policy())
    for hours in (0, 23, 46):
        now = START + timedelta(hours=hours)
        assert (await guard.admit(host.session, USER_ID, now=now)).allowed
        await guard.record_failure(host.session, USER_ID, now=now)
    state = await guard.state(host.session, FAILURE_SCOPE, USER_ID)
    assert state is not None and state.failures == 3
    assert (await guard.admit(host.session, USER_ID, now=START + timedelta(hours=70))).allowed
    assert await guard.state(host.session, FAILURE_SCOPE, USER_ID) is None


async def test_existing_native_promo_lock_is_respected_without_mutation(host: Host) -> None:
    guard = CodeAttempts(policy())
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


@pytest.mark.parametrize("limit", [5, 10])
async def test_parallel_requests_and_new_instances_keep_budget(
    engine: AsyncEngine, limit: int
) -> None:
    factory = async_sessionmaker(engine, expire_on_commit=False)
    user_id = 990000 + limit
    async with factory.begin() as session:
        await user_dal.create_user(session, {"user_id": user_id})

    # Use no barrier after opening a connection: the pool is intentionally smaller than this burst.
    async def attempt() -> bool:
        async with factory.begin() as session:
            result = await CodeAttempts(policy(limit)).admit(session, user_id, now=START)
            return result.allowed

    results = await asyncio.gather(*(attempt() for _ in range(limit + 3)))
    assert sum(results) == limit
    async with factory.begin() as session:
        after_restart = CodeAttempts(policy(limit))
        denied = await after_restart.admit(session, user_id, now=START)
        assert not denied.allowed and denied.retry_after == 60
        state = await after_restart.state(session, MINUTE_SCOPE, user_id)
        assert state is not None and state.failures == limit
