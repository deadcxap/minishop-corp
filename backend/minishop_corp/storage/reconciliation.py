"""Schedule through user -> contract -> member -> optional sweep -> operation locks.

The committed contract version is the durable propagation request. A different
scheduled_version is immediately eligible, independently of the global sweep.
"""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..integration.worker_locks import try_lock_account
from .operations import OperationDraft, PeriodTarget, prepare_operation
from .schema import AuditEvent, Contract, Membership, Operation
from .sweeps import BATCH_SIZE, SweepLease, lock_sweep

LIVE_STATES = ("pending", "running", "retry")


def period_target(contract: Contract, *, accepted_version: int | None = None) -> PeriodTarget:
    return PeriodTarget(
        tariff_key=contract.tariff_key,
        ends_at=contract.ends_at,
        external_squad_uuid=contract.external_squad_uuid,
        accepted_version=accepted_version,
    )


def mark_reconciled(member: Membership, version: int, now: datetime) -> None:
    member.applied_version = version
    member.scheduled_version = version
    member.last_reconciled_at = now


async def candidates(
    session: AsyncSession,
    *,
    after: UUID | None = None,
    limit: int = BATCH_SIZE,
) -> list[UUID]:
    if not 1 <= limit <= BATCH_SIZE:
        raise ValueError("Invalid reconciliation batch size")
    query = (
        select(Membership.id)
        .join(Contract, Contract.id == Membership.contract_id)
        .where(
            Membership.state == "active",
            Membership.current_user_id.is_not(None),
            Membership.scheduled_version.is_distinct_from(Contract.version),
        )
        .order_by(Membership.id)
        .limit(limit)
    )
    if after is not None:
        query = query.where(Membership.id > after)
    return list((await session.scalars(query)).all())


async def schedule_member(
    session: AsyncSession,
    member_id: UUID,
    *,
    now: datetime,
    sweep: SweepLease | None = None,
) -> UUID | None:
    identity = await session.get(Membership, member_id)
    if identity is None or identity.current_user_id is None:
        return None
    if not await try_lock_account(session, identity.user_id):
        return None
    contract = await session.scalar(
        select(Contract)
        .where(Contract.id == identity.contract_id)
        .execution_options(populate_existing=True)
        .with_for_update(skip_locked=True)
    )
    if contract is None:
        return None
    await session.refresh(identity, with_for_update=True)
    member = identity
    if member.state != "active" or member.current_user_id != member.user_id:
        return None
    revised = member.scheduled_version != contract.version
    if sweep is None:
        if not revised:
            return None
    else:
        if member.created_at > sweep.started_at or member.scheduled_run_id == sweep.run_id:
            return None
        # Claims/finishing hold only this global row, never acquire a user lock
        # afterwards. Keeping it until this intent commits fences a lost lease.
        if await lock_sweep(session, sweep) is None:
            return None
    operation = await session.scalar(
        select(Operation)
        .where(
            Operation.user_id == member.user_id,
            Operation.state.in_(LIVE_STATES),
        )
        .execution_options(populate_existing=True)
        .with_for_update()
    )
    if operation is not None:
        if operation.kind != "reconcile" or operation.membership_id != member.id:
            return None
        if operation.contract_version != contract.version:
            # Fence a claimed/retrying old target and wake it immediately. A live
            # executor holds the user lock, so it cannot be rewritten in flight.
            operation.contract_version = contract.version
            operation.target = period_target(contract).model_dump(mode="json")
            operation.state = "pending"
            operation.lease_token = None
            operation.lease_until = None
            operation.error_code = None
            operation.next_attempt_at = now
            operation.updated_at = now
    else:
        operation = await prepare_operation(
            session,
            OperationDraft(
                contract_id=contract.id,
                contract_version=contract.version,
                membership_id=member.id,
                membership_generation=member.generation,
                user_id=member.user_id,
                actor_user_id=0,
                request_id=uuid4(),
                kind="reconcile",
                target=period_target(contract),
            ),
        )
        operation.next_attempt_at = now
    member.scheduled_version = contract.version
    if sweep is not None:
        member.scheduled_run_id = sweep.run_id
    session.add(
        AuditEvent(
            contract_id=contract.id,
            actor_user_id=0,
            action="reconciliation_requested",
            membership_id=member.id,
            operation_id=operation.id,
            details={
                "version": contract.version,
                "reason": "revision" if sweep is None else "periodic",
                "run_id": str(sweep.run_id) if sweep is not None else None,
            },
        )
    )
    await session.flush()
    return operation.id
