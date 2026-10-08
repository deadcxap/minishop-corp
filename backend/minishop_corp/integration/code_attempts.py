"""Corporate policy over Minishop's persistent throttle DAL, with no chosen defaults.

Call admit before either preview or confirmation, and record_failure only for an
unavailable code. The caller commits expected error outcomes too; an HTTP exception
must not roll back the failure budget. Q-01 selects the production policy later.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from math import ceil

from db.dal import security_dal
from db.dal.user_dal import lock_user_by_id
from pydantic import AwareDatetime, BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession

from ..contracts_types import ContractError
from .contracts import Account

MINUTE_SCOPE = "minishop-corp.code_minute"
FAILURE_SCOPE = "minishop-corp.code_errors"


@dataclass(frozen=True)
class AttemptPolicy:
    attempts_per_minute: int
    failures_per_step: int
    delays_seconds: tuple[int, ...]
    reset_after_seconds: int

    def __post_init__(self) -> None:
        if (
            self.attempts_per_minute < 1
            or self.failures_per_step < 1
            or not self.delays_seconds
            or any(delay < 1 for delay in self.delays_seconds)
            or tuple(sorted(self.delays_seconds)) != self.delays_seconds
            or self.reset_after_seconds <= self.delays_seconds[-1]
        ):
            raise ValueError("Invalid corporate code attempt policy")


@dataclass(frozen=True)
class AttemptDecision:
    allowed: bool
    retry_after: int | None = None


class ThrottleState(BaseModel):
    model_config = ConfigDict(from_attributes=True, frozen=True)
    failures: int
    window_started_at: AwareDatetime | None
    locked_until: AwareDatetime | None
    last_attempt_at: AwareDatetime | None


class CodeAttempts:
    def __init__(self, policy: AttemptPolicy) -> None:
        self.policy = policy

    @staticmethod
    def _instant(now: datetime | None) -> datetime:
        instant = now or datetime.now(UTC)
        if instant.tzinfo is None or instant.utcoffset() is None:
            raise ValueError("An aware datetime is required")
        return instant.astimezone(UTC)

    @staticmethod
    async def _lock_account(session: AsyncSession, user_id: int) -> None:
        user = await lock_user_by_id(session, user_id)
        if user is None or Account.model_validate(user).is_banned:
            raise ContractError("minishop_corp_access_denied", 403)

    @staticmethod
    async def state(session: AsyncSession, scope: str, user_id: int) -> ThrottleState | None:
        row = await security_dal.get_throttle_state(
            session, scope=scope, identifier=f"user:{user_id}"
        )
        if row is None:
            return None
        # Native upserts do not refresh preloaded legacy ORM instances.
        await session.refresh(row)
        return ThrottleState.model_validate(row)

    async def _reset_idle(
        self,
        session: AsyncSession,
        user_id: int,
        now: datetime,
    ) -> ThrottleState | None:
        state = await self.state(session, FAILURE_SCOPE, user_id)
        if state is not None and (
            state.last_attempt_at is None
            or state.last_attempt_at <= now - timedelta(seconds=self.policy.reset_after_seconds)
        ):
            await security_dal.clear_throttle_state(
                session, scope=FAILURE_SCOPE, identifier=f"user:{user_id}"
            )
            return None
        return state

    async def _blocked(
        self,
        session: AsyncSession,
        user_id: int,
        now: datetime,
    ) -> AttemptDecision:
        retry_after = 0
        # Respect an existing ordinary promo lock, but never modify that scope.
        for scope in (security_dal.PROMO_CODE_APPLY_SCOPE, MINUTE_SCOPE, FAILURE_SCOPE):
            state = await self.state(session, scope, user_id)
            decision = await security_dal.check_throttle(
                session,
                scope=scope,
                identifier=f"user:{user_id}",
                now=now,
            )
            if decision.locked and state is not None and state.locked_until is not None:
                # Native retry_after floors seconds; use ceil so subsecond locks remain positive.
                retry_after = max(
                    retry_after, max(1, ceil((state.locked_until - now).total_seconds()))
                )
        return AttemptDecision(allowed=retry_after == 0, retry_after=retry_after or None)

    async def admit(
        self,
        session: AsyncSession,
        user_id: int,
        *,
        now: datetime | None = None,
    ) -> AttemptDecision:
        instant = self._instant(now)
        await self._lock_account(session, user_id)
        await self._reset_idle(session, user_id, instant)
        blocked = await self._blocked(session, user_id, instant)
        if not blocked.allowed:
            return blocked
        state = await self.state(session, MINUTE_SCOPE, user_id)
        started = state.window_started_at if state else None
        if started is None or started <= instant - timedelta(seconds=60):
            started = instant
        remaining = max(1, ceil((started + timedelta(seconds=60) - instant).total_seconds()))
        await security_dal.record_throttle_failure(
            session,
            scope=MINUTE_SCOPE,
            identifier=f"user:{user_id}",
            max_failures=self.policy.attempts_per_minute,
            window_seconds=60,
            lock_seconds=remaining,
            now=instant,
        )
        # The Nth attempt is admitted; its counter prevents attempt N+1.
        return AttemptDecision(allowed=True)

    async def record_failure(
        self,
        session: AsyncSession,
        user_id: int,
        *,
        now: datetime | None = None,
    ) -> AttemptDecision:
        instant = self._instant(now)
        await self._lock_account(session, user_id)
        state = await self._reset_idle(session, user_id, instant)
        count = state.failures if state else 0
        threshold = (count // self.policy.failures_per_step + 1) * self.policy.failures_per_step
        step = min(
            (threshold // self.policy.failures_per_step) - 1, len(self.policy.delays_seconds) - 1
        )
        # Native windows start at the first failure. Extend that window while errors
        # continue; only _reset_idle implements the required period without errors.
        elapsed = 0
        if state is not None and state.window_started_at is not None:
            elapsed = max(0, ceil((instant - state.window_started_at).total_seconds()))
        await security_dal.record_throttle_failure(
            session,
            scope=FAILURE_SCOPE,
            identifier=f"user:{user_id}",
            max_failures=threshold,
            window_seconds=elapsed + self.policy.reset_after_seconds + 1,
            lock_seconds=self.policy.delays_seconds[step],
            now=instant,
        )
        return await self._blocked(session, user_id, instant)
