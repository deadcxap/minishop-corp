"""Durable delivery of membership intents, fenced by lease and membership generation.

Claim transactions are short. Execution locks the native user, contract, member,
invitation and operation, in that order. Native services run in a separate session
on a savepoint so their rollback cannot release these outer locks.
"""

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID, uuid4

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from .contracts import Contracts
from .contracts_types import ContractError
from .integration.access import AccessAdapter
from .integration.access_types import AccessError, DisabledAccess
from .integration.contracts import ContractHost
from .storage.invitations import settle_reservation
from .storage.operations import DisabledTarget, OperationDraft, PeriodTarget, TrialTarget
from .storage.schema import AuditEvent, Invitation, Membership, Operation, Reservation

logger = logging.getLogger(__name__)
LEASE_TIME = timedelta(minutes=2)


@dataclass(frozen=True)
class Lease:
    operation_id: UUID
    token: UUID


class OperationWorker:
    def __init__(self, host: ContractHost) -> None:
        self.host = host
        self.access = AccessAdapter(host.service)

    async def claim(
        self,
        session: AsyncSession,
        *,
        now: datetime | None = None,
        operation_id: UUID | None = None,
    ) -> Lease | None:
        instant = now or datetime.now(UTC)
        query = select(Operation)
        if operation_id is not None:
            query = query.where(Operation.id == operation_id)
        row = await session.scalar(
            query.where(
                or_(
                    and_(
                        Operation.state.in_(("pending", "retry")),
                        Operation.next_attempt_at <= instant,
                    ),
                    and_(Operation.state == "running", Operation.lease_until <= instant),
                )
            )
            .order_by(Operation.next_attempt_at, Operation.id)
            .limit(1)
            .with_for_update(skip_locked=True)
            .execution_options(populate_existing=True)
        )
        if row is None:
            return None
        token = uuid4()
        row.state = "running"
        row.lease_token = token
        row.lease_until = instant + LEASE_TIME
        row.attempts += 1
        row.updated_at = instant
        await session.flush()
        return Lease(row.id, token)

    async def execute(self, session: AsyncSession, lease: Lease) -> bool:
        identity = await session.get(Operation, lease.operation_id)
        if identity is None:
            return False
        # An outer user lock survives even a native trial service rollback.
        account_error: str | None = None
        try:
            await self.host.lock_accounts(session, [identity.user_id])
        except ContractError as exc:
            account_error = exc.code
        contract = await Contracts._get(session, identity.contract_id, lock=True)
        member = await session.get(
            Membership,
            identity.membership_id,
            populate_existing=True,
            with_for_update=True,
        )
        if member is None:
            return False
        if member.invitation_id is not None:
            await session.get(
                Invitation, member.invitation_id, populate_existing=True, with_for_update=True
            )
        await session.refresh(identity, with_for_update=True)
        operation = identity
        if operation.state != "running" or operation.lease_token != lease.token:
            return False
        if account_error is not None:
            self._retry(operation, account_error)
            await session.flush()
            return False
        if (
            member.current_user_id != operation.user_id
            or member.generation != operation.membership_generation
            or member.state != ("pending" if operation.kind == "join" else "leaving")
        ):
            # A newer generation owns access. Never compensate over its result.
            # Preserve any unresolved reservation for diagnosis, not speculative reuse.
            self._finish(operation, "cancelled", "minishop_corp_operation_stale")
            await session.flush()
            return False
        draft = OperationDraft.model_validate(operation, from_attributes=True)
        target = draft.target
        if isinstance(target, PeriodTarget):
            reservation = await session.scalar(
                select(Reservation)
                .where(
                    Reservation.operation_id == operation.id,
                    Reservation.state == "held",
                )
                .with_for_update()
            )
            if reservation is None:
                self._retry(operation, "minishop_corp_operation_unresolved")
                await session.flush()
                return False
            # Confirmation enrolled the member in this contract. As for current
            # members, queued activation uses its latest committed terms. Keep
            # the originally accepted version for request idempotency.
            if operation.contract_version != contract.version:
                target = PeriodTarget(
                    tariff_key=contract.tariff_key,
                    ends_at=contract.ends_at,
                    external_squad_uuid=contract.external_squad_uuid,
                    accepted_version=target.accepted_version,
                )
                operation.contract_version = contract.version
                operation.target = target.model_dump(mode="json")
        elif isinstance(target, TrialTarget):
            # If trial was disabled after accepting departure, Q-02 still permits
            # leaving. Persist the fallback so a later setting change cannot grant it.
            if isinstance(
                self.access.prepare_departure(starts_at=target.starts_at), DisabledAccess
            ):
                target = DisabledTarget(ends_at=target.starts_at)
                operation.target = target.model_dump(mode="json")
        await session.flush()
        async with AsyncSession(
            bind=await session.connection(),
            expire_on_commit=False,
            join_transaction_mode="create_savepoint",
        ) as effects:
            try:
                if isinstance(target, PeriodTarget):
                    await self.access.validate_join(effects, operation.user_id)
                    await self.access.assign_period(effects, operation.user_id, target.access())
                elif isinstance(target, TrialTarget):
                    await self.access.grant_trial(effects, operation.user_id, target.access())
                else:
                    await self.access.disable_access(effects, operation.user_id, target.access())
                await effects.commit()
            except (AccessError, ContractError) as exc:
                await effects.rollback()
                self._retry(operation, str(exc.code))
                await session.flush()
                return False
            except Exception as exc:
                await effects.rollback()
                # Never log provider payloads, request bodies or exception text.
                logger.error("Operation %s failed (%s)", operation.id, type(exc).__name__)
                self._retry(operation, "minishop_corp_operation_failed")
                await session.flush()
                return False
        self._finish(operation, "succeeded")
        instant = datetime.now(UTC)
        if operation.kind == "join":
            member.state = "active"
            member.joined_at = member.joined_at or instant
            member.applied_version = operation.contract_version
            await session.flush()
            if member.invitation_id is None:
                raise ContractError("minishop_corp_operation_stale", 409)
            await settle_reservation(
                session,
                user_id=operation.user_id,
                invitation_id=member.invitation_id,
                operation_id=operation.id,
            )
        else:
            member.state = "excluded" if operation.kind == "exclude" else "left"
            member.current_user_id = None
            member.ended_at = instant
        session.add(
            AuditEvent(
                contract_id=operation.contract_id,
                actor_user_id=operation.actor_user_id,
                membership_id=member.id,
                operation_id=operation.id,
                action="membership_" + operation.kind + "_succeeded",
                details={
                    "contract_version": operation.contract_version,
                    "access_kind": target.kind,
                },
            )
        )
        await session.flush()
        return True

    @staticmethod
    def _retry(operation: Operation, code: str) -> None:
        operation.state = "retry"
        operation.error_code = code
        operation.lease_token = None
        operation.lease_until = None
        operation.updated_at = datetime.now(UTC)
        operation.next_attempt_at = operation.updated_at + timedelta(
            seconds=min(300, 5 * 2 ** min(operation.attempts - 1, 6)),
        )

    @staticmethod
    def _finish(
        operation: Operation,
        state: Literal["succeeded", "cancelled"],
        code: str | None = None,
    ) -> None:
        operation.state = state
        operation.error_code = code
        operation.lease_token = None
        operation.lease_until = None
        operation.updated_at = datetime.now(UTC)

    async def run(self, sessions: Callable[[], AsyncSession]) -> None:
        logger.info("Corporate membership operation worker started")
        while True:
            try:
                async with sessions() as session, session.begin():
                    lease = await self.claim(session)
                if lease is None:
                    await asyncio.sleep(2)
                    continue
                async with sessions() as session, session.begin():
                    await self.execute(session, lease)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # A crash before recording a retry is recovered by lease expiry.
                logger.error("Operation worker iteration failed (%s)", type(exc).__name__)
                await asyncio.sleep(5)
