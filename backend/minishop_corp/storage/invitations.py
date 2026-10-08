"""Transactional code lookup and capacity accounting; no panel calls or commits.

All callers must authorize and lock the Minishop account before using these helpers.
The public lookup boundary also applies the persistent throttle before reading a code.
"""

from datetime import UTC, datetime
from uuid import UUID

from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..contracts import summary_of
from ..contracts_types import ContractError
from ..invitations_types import InvitationOffer, code_digest
from .schema import AuditEvent, Contract, Invitation, Membership, Operation, Reservation


async def lookup_invitation(
    session: AsyncSession,
    code: SecretStr,
    *,
    now: datetime | None = None,
) -> InvitationOffer:
    digest = code_digest(code)
    contract_id = await session.scalar(
        select(Invitation.contract_id).where(Invitation.code_digest == digest)
    )
    if contract_id is None:
        raise ContractError("minishop_corp_invitation_unavailable", 400)
    contract = await session.scalar(
        select(Contract)
        .where(Contract.id == contract_id)
        .execution_options(populate_existing=True)
        .with_for_update()
    )
    # Every invitation mutation also locks its contract; refresh after waiting for it.
    invitation = await session.scalar(
        select(Invitation)
        .where(Invitation.code_digest == digest)
        .execution_options(populate_existing=True)
    )
    if contract is None or invitation is None:
        raise ContractError("minishop_corp_invitation_unavailable", 400)
    require_available(contract, invitation, now or datetime.now(UTC))
    return InvitationOffer(
        invitation_id=invitation.id,
        contract=summary_of(contract).model_copy(update={"expired": False}),
    )


def require_available(contract: Contract, invitation: Invitation, now: datetime) -> None:
    if (
        contract.ends_at <= now
        or invitation.revoked_at is not None
        or invitation.used_count + invitation.reserved_count >= invitation.use_limit
    ):
        raise ContractError("minishop_corp_invitation_unavailable", 400)


async def _locked_context(
    session: AsyncSession,
    user_id: int,
    invitation_id: UUID,
    operation_id: UUID,
) -> tuple[Contract, Membership, Invitation, Operation]:
    operation = await session.get(Operation, operation_id, populate_existing=True)
    if operation is None or operation.user_id != user_id or operation.kind != "join":
        raise ContractError("minishop_corp_operation_stale", 409)
    contract = await session.scalar(
        select(Contract)
        .where(Contract.id == operation.contract_id)
        .execution_options(populate_existing=True)
        .with_for_update()
    )
    member = await session.scalar(
        select(Membership)
        .where(Membership.id == operation.membership_id)
        .execution_options(populate_existing=True)
        .with_for_update()
    )
    invitation = await session.scalar(
        select(Invitation)
        .where(Invitation.id == invitation_id)
        .execution_options(populate_existing=True)
        .with_for_update()
    )
    await session.refresh(operation, with_for_update=True)
    if (
        contract is None
        or member is None
        or invitation is None
        or invitation.contract_id != contract.id
        or member.contract_id != contract.id
        or member.user_id != user_id
        or member.invitation_id != invitation_id
    ):
        raise ContractError("minishop_corp_operation_stale", 409)
    return contract, member, invitation, operation


async def reserve_invitation(
    session: AsyncSession,
    *,
    user_id: int,
    invitation_id: UUID,
    operation_id: UUID,
    now: datetime | None = None,
) -> Reservation:
    contract, member, invitation, operation = await _locked_context(
        session, user_id, invitation_id, operation_id
    )
    existing = await session.scalar(
        select(Reservation)
        .where(Reservation.operation_id == operation_id)
        .execution_options(populate_existing=True)
    )
    if existing is not None:
        if existing.invitation_id != invitation_id:
            raise ContractError("minishop_corp_request_conflict", 409)
        return existing
    if (
        operation.state != "pending"
        or member.current_user_id != user_id
        or member.state != "pending"
        or member.generation != operation.membership_generation
        or contract.version != operation.contract_version
    ):
        raise ContractError("minishop_corp_operation_stale", 409)
    require_available(contract, invitation, now or datetime.now(UTC))
    invitation.reserved_count += 1
    reservation = Reservation(
        contract_id=contract.id,
        invitation_id=invitation.id,
        operation_id=operation_id,
    )
    session.add(reservation)
    session.add(
        AuditEvent(
            contract_id=contract.id,
            actor_user_id=operation.actor_user_id,
            action="invitation_reserved",
            membership_id=member.id,
            operation_id=operation_id,
            details={"invitation_id": str(invitation.id)},
        )
    )
    await session.flush()
    return reservation


async def settle_reservation(
    session: AsyncSession,
    *,
    user_id: int,
    invitation_id: UUID,
    operation_id: UUID,
    now: datetime | None = None,
) -> Reservation:
    """Only a resolved terminal operation can consume/release; timeouts keep the slot."""
    contract, member, invitation, operation = await _locked_context(
        session, user_id, invitation_id, operation_id
    )
    reservation = await session.scalar(
        select(Reservation)
        .where(Reservation.operation_id == operation_id)
        .execution_options(populate_existing=True)
        .with_for_update()
    )
    if reservation is None or reservation.invitation_id != invitation.id:
        raise ContractError("minishop_corp_operation_stale", 409)
    if operation.state == "succeeded":
        target_state = "consumed"
    elif operation.state in {"failed", "cancelled"}:
        target_state = "released"
    else:
        raise ContractError("minishop_corp_operation_unresolved", 409)
    if reservation.state == target_state:
        return reservation
    if reservation.state != "held" or invitation.reserved_count <= 0:
        raise ContractError("minishop_corp_operation_stale", 409)
    invitation.reserved_count -= 1
    if target_state == "consumed":
        invitation.used_count += 1
        reservation.state = "consumed"
    else:
        reservation.state = "released"
    reservation.finished_at = now or datetime.now(UTC)
    session.add(
        AuditEvent(
            contract_id=contract.id,
            actor_user_id=operation.actor_user_id,
            action="invitation_" + target_state,
            membership_id=member.id,
            operation_id=operation.id,
            details={"invitation_id": str(invitation.id)},
        )
    )
    await session.flush()
    return reservation
