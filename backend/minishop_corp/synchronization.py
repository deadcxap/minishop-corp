"""Scoped progress from plugin state; no remote reads and no subscription secrets."""

from datetime import UTC, datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict
from sqlalchemy import and_, case, func, literal, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from .contracts import Contracts
from .contracts_types import ContractError, ContractPage
from .integration.contracts import ContractHost
from .membership_types import OperationInfo
from .storage.reconciliation import LIVE_STATES
from .storage.schema import Contract, Membership, Operation, ReconciliationSweep


class SynchronizationProgress(BaseModel):
    model_config = ConfigDict(frozen=True)
    contract_id: UUID
    version: int
    current_members: int
    joining: int
    departing: int
    confirmed: int
    awaiting_dispatch: int
    pending: int
    running: int
    retrying: int
    last_checked_at: datetime | None
    next_sweep_at: datetime
    sweep_running: bool
    sweep_started_at: datetime | None
    sweep_completed_at: datetime | None


class SynchronizationOperation(OperationInfo):
    user_id: int
    contract_version: int


class Synchronization:
    def __init__(self, host: ContractHost) -> None:
        self.contracts = Contracts(host)

    async def _authorize(
        self,
        session: AsyncSession,
        actor: int,
        contract_id: UUID,
        *,
        manager: bool,
    ) -> Contract:
        if manager:
            return await self.contracts.require_manager(session, actor, contract_id)
        await self.contracts.host.require_admin(session, actor)
        return await self.contracts._get(session, contract_id)

    async def progress(
        self,
        session: AsyncSession,
        actor: int,
        contract_id: UUID,
        *,
        manager: bool = False,
        now: datetime | None = None,
    ) -> SynchronizationProgress:
        contract = await self._authorize(session, actor, contract_id, manager=manager)
        instant = now or datetime.now(UTC)
        sweep = await session.get(ReconciliationSweep, 1, populate_existing=True)
        if sweep is None:
            raise ContractError("minishop_corp_operation_failed", status=503)
        periodic = (
            and_(
                Membership.created_at <= sweep.started_at,
                Membership.scheduled_run_id.is_distinct_from(sweep.run_id),
            )
            if sweep.is_running and sweep.started_at is not None
            else literal(sweep.next_run_at <= instant)
        )
        needs_check = or_(
            Membership.applied_version.is_distinct_from(contract.version),
            Membership.scheduled_version.is_distinct_from(contract.version),
            periodic,
        )
        active = Membership.state == "active"
        waiting = and_(
            active,
            Operation.id.is_(None),
            needs_check,
        )
        confirmed = and_(
            active,
            Operation.id.is_(None),
            ~needs_check,
        )
        query = (
            select(
                func.count().label("current_members"),
                func.count().filter(Membership.state == "pending").label("joining"),
                func.count().filter(Membership.state == "leaving").label("departing"),
                func.count().filter(confirmed).label("confirmed"),
                func.count().filter(waiting).label("awaiting_dispatch"),
                func.count().filter(and_(active, Operation.state == "pending")).label("pending"),
                func.count().filter(and_(active, Operation.state == "running")).label("running"),
                func.count().filter(and_(active, Operation.state == "retry")).label("retrying"),
                case(
                    (
                        func.count().filter(and_(active, Membership.last_reconciled_at.is_(None)))
                        > 0,
                        None,
                    ),
                    else_=func.min(Membership.last_reconciled_at).filter(active),
                ).label("last_checked_at"),
            )
            .select_from(Membership)
            .outerjoin(
                Operation,
                and_(
                    Operation.membership_id == Membership.id,
                    Operation.state.in_(LIVE_STATES),
                ),
            )
            .where(Membership.contract_id == contract_id, Membership.current_user_id.is_not(None))
        )
        counts = (await session.execute(query)).mappings().one()
        return SynchronizationProgress.model_validate(
            {
                "contract_id": contract.id,
                "version": contract.version,
                "next_sweep_at": sweep.next_run_at,
                "sweep_running": sweep.is_running,
                "sweep_started_at": sweep.started_at,
                "sweep_completed_at": sweep.completed_at,
                **counts,
            }
        )

    async def operations(
        self,
        session: AsyncSession,
        actor: int,
        contract_id: UUID,
        page: ContractPage,
        *,
        manager: bool = False,
    ) -> list[SynchronizationOperation]:
        await self._authorize(session, actor, contract_id, manager=manager)
        query = (
            select(Operation)
            .join(Membership, Membership.id == Operation.membership_id)
            .where(
                Operation.contract_id == contract_id,
                Operation.state.in_(LIVE_STATES),
                Membership.current_user_id.is_not(None),
            )
            .order_by(Operation.id)
            .limit(page.limit)
        )
        if page.after is not None:
            query = query.where(Operation.id > page.after)
        rows = await session.scalars(query)
        return [SynchronizationOperation.model_validate(row) for row in rows]
