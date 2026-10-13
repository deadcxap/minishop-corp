"""Authorized invitation management, independent of subscription mutations."""

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from .contracts import Contracts
from .contracts_types import ContractError, ContractPage
from .integration.contracts import ContractHost
from .invitations_types import (
    CreateInvitation,
    InvitationInfo,
    IssuedInvitation,
    RotateInvitation,
    UpdateInvitation,
    code_digest,
    new_code,
)
from .storage.schema import (
    AuditEvent,
    Contract,
    Invitation,
    InvitationPreview,
    Membership,
    Operation,
    Reservation,
)


class Invitations:
    def __init__(self, host: ContractHost) -> None:
        self.host = host
        self.contracts = Contracts(host)

    async def authorize(
        self,
        session: AsyncSession,
        actor: int,
        contract_id: UUID,
        *,
        manager: bool = False,
        lock: bool = False,
    ) -> Contract:
        if lock:
            await self.host.lock_accounts(session, [actor])
        if manager:
            return await self.contracts.require_manager(session, actor, contract_id, lock=lock)
        await self.host.require_admin(session, actor)
        return await self.contracts._get(session, contract_id, lock=lock)

    async def list(
        self,
        session: AsyncSession,
        actor: int,
        contract_id: UUID,
        page: ContractPage,
        *,
        manager: bool = False,
    ) -> list[InvitationInfo]:
        await self.authorize(session, actor, contract_id, manager=manager)
        query = select(Invitation).where(Invitation.contract_id == contract_id)
        if manager:
            query = query.where(Invitation.revoked_at.is_(None))
        if page.after is not None:
            query = query.where(Invitation.id > page.after)
        rows = await session.scalars(query.order_by(Invitation.id).limit(page.limit))
        return [InvitationInfo.model_validate(row) for row in rows]

    async def create(
        self,
        session: AsyncSession,
        actor: int,
        contract_id: UUID,
        draft: CreateInvitation,
        *,
        manager: bool = False,
        now: datetime | None = None,
    ) -> IssuedInvitation:
        contract = await self.authorize(session, actor, contract_id, manager=manager, lock=True)
        existing = await session.get(Invitation, draft.id, populate_existing=True)
        if existing is not None:
            if (
                existing.contract_id != contract_id
                or existing.kind != draft.kind
                or await self._initial_limit(session, existing) != draft.use_limit
                or existing.replaces_id is not None
                or await self._recorded(session, contract_id, existing.id, "invitation_rotated")
            ):
                raise ContractError("minishop_corp_request_conflict", 409)
            return self._issued(existing)
        await self._require_not_deleted(session, contract_id, draft.id)
        if contract.ends_at <= (now or datetime.now(UTC)):
            raise ContractError("minishop_corp_contract_expired", 409)
        return await self._insert(session, actor, contract_id, draft)

    async def update(
        self,
        session: AsyncSession,
        actor: int,
        contract_id: UUID,
        invitation_id: UUID,
        draft: UpdateInvitation,
        *,
        manager: bool = False,
    ) -> InvitationInfo:
        contract = await self.authorize(session, actor, contract_id, manager=manager, lock=True)
        row = await self._get(session, contract_id, invitation_id)
        if row.use_limit == draft.use_limit and row.version in (
            draft.expected_version,
            draft.expected_version + 1,
        ):
            return InvitationInfo.model_validate(row)
        if row.version != draft.expected_version or row.revoked_at is not None:
            raise ContractError("minishop_corp_request_conflict", 409)
        if contract.ends_at <= datetime.now(UTC):
            raise ContractError("minishop_corp_contract_expired", 409)
        if row.kind != "reusable":
            raise ContractError("minishop_corp_invalid_request", 400)
        if draft.use_limit < row.used_count + row.reserved_count:
            raise ContractError("minishop_corp_invitation_limit_too_low", 409)
        previous_limit = row.use_limit
        row.use_limit = draft.use_limit
        row.version += 1
        session.add(
            AuditEvent(
                contract_id=contract_id,
                actor_user_id=actor,
                action="invitation_updated",
                details={
                    "invitation_id": str(row.id),
                    "use_limit": row.use_limit,
                    "previous_use_limit": previous_limit,
                    "version": row.version,
                },
            )
        )
        await session.flush()
        return InvitationInfo.model_validate(row)

    @staticmethod
    async def _initial_limit(session: AsyncSession, row: Invitation) -> int:
        # Editing a code must not change the identity of its accepted create request.
        previous = await session.scalar(
            select(AuditEvent.details["previous_use_limit"].as_integer())
            .where(
                AuditEvent.contract_id == row.contract_id,
                AuditEvent.action == "invitation_updated",
                AuditEvent.details["invitation_id"].astext == str(row.id),
            )
            .order_by(AuditEvent.id)
            .limit(1)
        )
        return row.use_limit if previous is None else previous

    async def rotate(
        self,
        session: AsyncSession,
        actor: int,
        contract_id: UUID,
        invitation_id: UUID,
        draft: RotateInvitation,
        *,
        manager: bool = False,
        now: datetime | None = None,
    ) -> IssuedInvitation:
        contract = await self.authorize(session, actor, contract_id, manager=manager, lock=True)
        existing = await session.get(Invitation, draft.id, populate_existing=True)
        if existing is not None:
            replacement = (
                existing.replaces_id == invitation_id
                or await session.scalar(
                    select(AuditEvent.id)
                    .where(
                        AuditEvent.contract_id == contract_id,
                        AuditEvent.action == "invitation_rotated",
                        AuditEvent.details["invitation_id"].astext == str(draft.id),
                        AuditEvent.details["replaces_id"].astext == str(invitation_id),
                    )
                    .limit(1)
                )
                is not None
            )
            if existing.contract_id != contract_id or not replacement:
                raise ContractError("minishop_corp_request_conflict", 409)
            return self._issued(existing)
        await self._require_not_deleted(session, contract_id, draft.id)
        previous = await self._get(session, contract_id, invitation_id)
        if previous.kind != "reusable":
            raise ContractError("minishop_corp_invitation_missing", 404)
        instant = now or datetime.now(UTC)
        if contract.ends_at <= instant:
            raise ContractError("minishop_corp_contract_expired", 409)
        if previous.revoked_at is not None:
            raise ContractError("minishop_corp_request_conflict", 409)
        previous.revoked_at = instant
        await session.flush()
        return await self._insert(
            session,
            actor,
            contract_id,
            CreateInvitation(id=draft.id, kind="reusable", use_limit=previous.use_limit),
            replaces_id=previous.id,
        )

    async def revoke(
        self,
        session: AsyncSession,
        actor: int,
        contract_id: UUID,
        invitation_id: UUID,
        *,
        now: datetime | None = None,
    ) -> InvitationInfo:
        await self.authorize(session, actor, contract_id, lock=True)
        row = await self._get(session, contract_id, invitation_id)
        if row.revoked_at is None:
            row.revoked_at = now or datetime.now(UTC)
            session.add(
                AuditEvent(
                    contract_id=contract_id,
                    actor_user_id=actor,
                    action="invitation_revoked",
                    details={"invitation_id": str(row.id)},
                )
            )
            await session.flush()
        return InvitationInfo.model_validate(row)

    async def delete(
        self, session: AsyncSession, actor: int, contract_id: UUID, invitation_id: UUID
    ) -> None:
        """Remove the secret, not the access or resolved operation history.

        The contract lock serializes preview/confirmation/rotation/settlement.
        Never discard an unresolved reservation, including an uncertain panel result.
        """
        await self.authorize(session, actor, contract_id, lock=True)
        row = await session.scalar(
            select(Invitation)
            .where(Invitation.id == invitation_id, Invitation.contract_id == contract_id)
            .execution_options(populate_existing=True)
            .with_for_update()
        )
        if row is None:
            return  # DELETE retries after a lost response are harmless.
        held = await session.scalar(
            select(Reservation.id)
            .where(Reservation.invitation_id == invitation_id, Reservation.state == "held")
            .limit(1)
        )
        pending = await session.scalar(
            select(Operation.id)
            .join(Membership, Membership.id == Operation.membership_id)
            .where(
                Membership.invitation_id == invitation_id,
                Operation.kind == "join",
                Operation.state.in_(("pending", "running", "retry")),
            )
            .limit(1)
        )
        if row.reserved_count or held is not None or pending is not None:
            raise ContractError("minishop_corp_invitation_busy", 409)
        await session.execute(
            delete(InvitationPreview).where(InvitationPreview.invitation_id == invitation_id)
        )
        await session.execute(delete(Reservation).where(Reservation.invitation_id == invitation_id))
        await session.execute(
            update(Membership)
            .where(Membership.invitation_id == invitation_id)
            .values(invitation_id=None)
        )
        await session.execute(
            update(Invitation)
            .where(Invitation.replaces_id == invitation_id)
            .values(replaces_id=None)
        )
        await session.delete(row)
        session.add(
            AuditEvent(
                contract_id=contract_id,
                actor_user_id=actor,
                action="invitation_deleted",
                details={"invitation_id": str(invitation_id)},
            )
        )
        await session.flush()

    @staticmethod
    async def _recorded(
        session: AsyncSession, contract_id: UUID, invitation_id: UUID, action: str
    ) -> bool:
        return (
            await session.scalar(
                select(AuditEvent.id)
                .where(
                    AuditEvent.contract_id == contract_id,
                    AuditEvent.action == action,
                    AuditEvent.details["invitation_id"].astext == str(invitation_id),
                )
                .limit(1)
            )
            is not None
        )

    async def _require_not_deleted(
        self, session: AsyncSession, contract_id: UUID, invitation_id: UUID
    ) -> None:
        # An old create/rotate retry must never regenerate an intentionally deleted code.
        if await self._recorded(session, contract_id, invitation_id, "invitation_deleted"):
            raise ContractError("minishop_corp_invitation_missing", 404)

    @staticmethod
    async def _get(session: AsyncSession, contract_id: UUID, invitation_id: UUID) -> Invitation:
        row = await session.scalar(
            select(Invitation)
            .where(
                Invitation.id == invitation_id,
                Invitation.contract_id == contract_id,
            )
            .execution_options(populate_existing=True)
            .with_for_update()
        )
        if row is None:
            raise ContractError("minishop_corp_invitation_missing", 404)
        return row

    @staticmethod
    def _issued(row: Invitation) -> IssuedInvitation:
        info = InvitationInfo.model_validate(row)
        return IssuedInvitation(invitation=info, code=info.code, created=False)

    @staticmethod
    async def _insert(
        session: AsyncSession,
        actor: int,
        contract_id: UUID,
        draft: CreateInvitation,
        *,
        replaces_id: UUID | None = None,
    ) -> IssuedInvitation:
        code = new_code()
        row = Invitation(
            id=draft.id,
            contract_id=contract_id,
            kind=draft.kind,
            use_limit=draft.use_limit,
            code_digest=code_digest(code),
            code_value=code.get_secret_value(),
            created_by=actor,
            replaces_id=replaces_id,
        )
        session.add(row)
        session.add(
            AuditEvent(
                contract_id=contract_id,
                actor_user_id=actor,
                action="invitation_rotated" if replaces_id else "invitation_created",
                details={
                    "invitation_id": str(row.id),
                    "replaces_id": str(replaces_id) if replaces_id else None,
                },
            )
        )
        await session.flush()
        return IssuedInvitation(
            invitation=InvitationInfo.model_validate(row), code=code, created=True
        )
