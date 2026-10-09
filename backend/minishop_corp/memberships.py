"""Authorized membership intents. HTTP never performs a subscription write.

Native user locks precede contract/member locks. An unresolved intent keeps its
membership and reservation. Departure may supersede a reconciliation operation;
other access-changing intents must await resolution.
"""

from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .contracts import Contracts, summary_of
from .contracts_types import ContractError
from .integration.access import AccessAdapter
from .integration.access_types import AccessError, TrialAccess
from .integration.contracts import ContractHost
from .invitation_lookup import InvitationLookup
from .membership_types import (
    ConfirmationResult,
    ConfirmMembership,
    DepartMembership,
    MembershipInfo,
    OperationInfo,
    operation_info,
)
from .storage.invitations import reserve_invitation
from .storage.operations import (
    DisabledTarget,
    OperationDraft,
    PeriodTarget,
    TrialTarget,
    prepare_operation,
)
from .storage.schema import AuditEvent, Contract, InvitationPreview, Membership, Operation

LIVE_STATES = ("pending", "running", "retry")


class Memberships:
    def __init__(self, host: ContractHost) -> None:
        self.host = host
        self.access = AccessAdapter(host.service)

    async def current(self, session: AsyncSession, actor: int) -> MembershipInfo | None:
        await self.host.require_account(session, actor)
        row = await session.scalar(select(Membership).where(Membership.current_user_id == actor))
        if row is None:
            return None
        contract = await Contracts(self.host).require_member(session, actor, row.contract_id)
        operation = await session.scalar(
            select(Operation)
            .where(Operation.membership_id == row.id)
            .order_by(Operation.created_at.desc(), Operation.id.desc())
            .limit(1)
        )
        summary = summary_of(contract)
        try:
            tariff = self.host.describe_tariff(contract.tariff_key)
        except AccessError:
            # A removed tariff must not prevent departure from the group.
            tariff = None
        return MembershipInfo(
            id=row.id,
            state=row.state,
            contract=summary,
            tariff=tariff,
            joined_at=row.joined_at,
            can_leave=row.state == "active",
            expiry_notice="wa_minishop_corp_expired_notice" if summary.expired else None,
            operation=operation_info(operation) if operation is not None else None,
        )

    async def last_departure(self, session: AsyncSession, actor: int) -> OperationInfo | None:
        await self.host.require_account(session, actor)
        row = await session.scalar(
            select(Operation)
            .where(
                Operation.user_id == actor,
                Operation.kind.in_(("leave", "exclude")),
                Operation.state == "succeeded",
            )
            .order_by(Operation.created_at.desc(), Operation.id.desc())
            .limit(1)
        )
        return operation_info(row) if row is not None else None

    async def confirm(
        self,
        session: AsyncSession,
        actor: int,
        draft: ConfirmMembership,
        *,
        now: datetime | None = None,
    ) -> ConfirmationResult:
        await self.host.lock_accounts(session, [actor])
        existing = await self._request(session, actor, draft.request_id)
        if existing is not None:
            # A request id is scoped to the authenticated actor. Retrying an
            # accepted request is not another code check, even after revocation.
            target = (
                PeriodTarget.model_validate(existing.target) if existing.kind == "join" else None
            )
            member = await session.get(Membership, existing.membership_id)
            if (
                target is None
                or member is None
                or member.invitation_id != draft.offer.invitation_id
                or existing.contract_id != draft.offer.contract_id
                or target.accepted_version != draft.offer.contract_version
            ):
                raise ContractError("minishop_corp_request_conflict", 409)
            if member.state == "deleted":
                raise ContractError("minishop_corp_membership_missing", 404)
            return ConfirmationResult(200, operation=operation_info(existing))
        if await session.scalar(select(Membership.id).where(Membership.current_user_id == actor)):
            raise ContractError("minishop_corp_already_member", 409)
        checked = await InvitationLookup(self.host).inspect(
            session,
            actor,
            draft.code,
            expected=draft.offer,
            now=now,
        )
        if checked.offer is None:
            return ConfirmationResult(
                checked.status,
                error=checked.error,
                retry_after=checked.retry_after,
            )
        try:
            await self.access.validate_join(session, actor)
        except AccessError as exc:
            # Retain the admitted attempt even when billing prevents the join.
            return ConfirmationResult(409, error=exc.code.value, retry_after=checked.retry_after)
        contract = await session.get(Contract, checked.offer.contract.id)
        if contract is None:
            raise ContractError("minishop_corp_contract_missing", 404)
        member = Membership(
            contract_id=contract.id,
            user_id=actor,
            current_user_id=actor,
            invitation_id=checked.offer.invitation_id,
        )
        session.add(member)
        await session.flush()
        operation = await prepare_operation(
            session,
            OperationDraft(
                contract_id=contract.id,
                contract_version=contract.version,
                membership_id=member.id,
                membership_generation=member.generation,
                user_id=actor,
                actor_user_id=actor,
                request_id=draft.request_id,
                kind="join",
                target=PeriodTarget(
                    tariff_key=contract.tariff_key,
                    ends_at=contract.ends_at,
                    external_squad_uuid=contract.external_squad_uuid,
                    accepted_version=contract.version,
                ),
            ),
        )
        await reserve_invitation(
            session,
            user_id=actor,
            invitation_id=checked.offer.invitation_id,
            operation_id=operation.id,
            now=now,
        )
        self._audit(session, operation, "membership_join_requested")
        preview = await session.get(InvitationPreview, actor)
        if preview is not None:
            await session.delete(preview)
        await session.flush()
        return ConfirmationResult(202, operation=operation_info(operation))

    async def depart(
        self,
        session: AsyncSession,
        actor: int,
        membership_id: UUID,
        draft: DepartMembership,
        *,
        scope: Literal["self", "manager", "admin"] = "self",
        contract_id: UUID | None = None,
        now: datetime | None = None,
    ) -> OperationInfo:
        # Read only the lock identity first; authorize and refresh under locks.
        member = await session.get(Membership, membership_id)
        if member is None:
            raise ContractError("minishop_corp_membership_missing", 404)
        await self.host.lock_accounts(session, [actor, member.user_id])
        contract = await Contracts._get(session, member.contract_id, lock=True)
        await session.refresh(member, with_for_update=True)
        await self._authorize(session, actor, member, contract, scope, contract_id)
        kind: Literal["leave", "exclude"] = "leave" if scope == "self" else "exclude"
        previous = await self._request(session, actor, draft.request_id)
        if previous is not None:
            if previous.membership_id != member.id or previous.kind != kind:
                raise ContractError("minishop_corp_request_conflict", 409)
            return operation_info(previous)
        if member.current_user_id != member.user_id:
            raise ContractError("minishop_corp_membership_missing", 404)
        if member.state != "active":
            raise ContractError("minishop_corp_operation_busy", 409)
        pending = await session.scalar(
            select(Operation)
            .where(
                Operation.user_id == member.user_id,
                Operation.state.in_(LIVE_STATES),
            )
            .execution_options(populate_existing=True)
            .with_for_update()
        )
        if pending is not None:
            if pending.kind != "reconcile" or pending.membership_id != member.id:
                raise ContractError("minishop_corp_operation_busy", 409)
            # Departure supersedes a repair, including an uncertain retry. User
            # and contract locks fence its old worker before accepting departure.
            pending.state = "cancelled"
            pending.lease_token = None
            pending.lease_until = None
            pending.error_code = "minishop_corp_operation_stale"
            pending.updated_at = datetime.now(UTC)
            self._audit(session, pending, "reconciliation_superseded")
            await session.flush()
        window = self.access.prepare_departure(starts_at=now or datetime.now(UTC))
        target: TrialTarget | DisabledTarget
        if isinstance(window, TrialAccess):
            target = TrialTarget(starts_at=window.starts_at, ends_at=window.ends_at)
        else:
            target = DisabledTarget(ends_at=window.ends_at)
        member.generation += 1
        member.state = "leaving"
        operation = await prepare_operation(
            session,
            OperationDraft(
                contract_id=contract.id,
                contract_version=contract.version,
                membership_id=member.id,
                membership_generation=member.generation,
                user_id=member.user_id,
                actor_user_id=actor,
                request_id=draft.request_id,
                kind=kind,
                target=target,
            ),
        )
        self._audit(session, operation, "membership_departure_requested")
        await session.flush()
        return operation_info(operation)

    async def operation(
        self,
        session: AsyncSession,
        actor: int,
        operation_id: UUID,
        *,
        scope: Literal["self", "manager", "admin"] = "self",
        contract_id: UUID | None = None,
    ) -> OperationInfo:
        await self.host.require_account(session, actor)
        row = await session.get(Operation, operation_id)
        if row is None:
            raise ContractError("minishop_corp_operation_missing", 404)
        member = await session.get(Membership, row.membership_id)
        contract = await Contracts._get(session, row.contract_id)
        if member is None:
            raise ContractError("minishop_corp_operation_missing", 404)
        await self._authorize(session, actor, member, contract, scope, contract_id)
        return operation_info(row)

    async def _authorize(
        self,
        session: AsyncSession,
        actor: int,
        member: Membership,
        contract: Contract,
        scope: Literal["self", "manager", "admin"],
        contract_id: UUID | None,
    ) -> None:
        if scope == "self":
            allowed = member.user_id == actor and member.state != "deleted"
        else:
            if scope == "admin":
                await self.host.require_admin(session, actor)
            allowed = contract.id == contract_id and (
                scope == "admin" or contract.manager_user_id == actor
            )
        if not allowed:
            raise ContractError("minishop_corp_membership_missing", 404)

    @staticmethod
    async def _request(session: AsyncSession, actor: int, request_id: UUID) -> Operation | None:
        result = await session.scalar(
            select(Operation).where(
                Operation.actor_user_id == actor,
                Operation.request_id == request_id,
            )
        )
        return result

    @staticmethod
    def _audit(session: AsyncSession, operation: Operation, action: str) -> None:
        session.add(
            AuditEvent(
                contract_id=operation.contract_id,
                actor_user_id=operation.actor_user_id,
                membership_id=operation.membership_id,
                operation_id=operation.id,
                action=action,
            )
        )
