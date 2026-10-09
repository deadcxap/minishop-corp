"""Authorized invitation management, independent of subscription mutations."""

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .contracts import Contracts
from .contracts_types import ContractError, ContractPage
from .integration.contracts import ContractHost
from .invitations_types import (
    CreateInvitation,
    InvitationInfo,
    IssuedInvitation,
    RotateInvitation,
    code_digest,
    new_code,
)
from .storage.schema import AuditEvent, Contract, Invitation


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
            query = query.where(Invitation.kind == "reusable", Invitation.revoked_at.is_(None))
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
        now: datetime | None = None,
    ) -> IssuedInvitation:
        contract = await self.authorize(session, actor, contract_id, lock=True)
        existing = await session.get(Invitation, draft.id, populate_existing=True)
        if existing is not None:
            if (
                existing.contract_id != contract_id
                or existing.kind != draft.kind
                or existing.use_limit != draft.use_limit
                or existing.replaces_id is not None
            ):
                raise ContractError("minishop_corp_request_conflict", 409)
            return self._issued(existing)
        if contract.ends_at <= (now or datetime.now(UTC)):
            raise ContractError("minishop_corp_contract_expired", 409)
        if draft.kind == "reusable":
            current = await session.scalar(
                select(Invitation.id).where(
                    Invitation.contract_id == contract_id,
                    Invitation.kind == "reusable",
                    Invitation.revoked_at.is_(None),
                )
            )
            if current is not None:
                raise ContractError("minishop_corp_invitation_exists", 409)
        return await self._insert(session, actor, contract_id, draft)

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
        previous = await self._get(session, contract_id, invitation_id)
        if previous.kind != "reusable":
            raise ContractError("minishop_corp_invitation_missing", 404)
        existing = await session.get(Invitation, draft.id, populate_existing=True)
        if existing is not None:
            if existing.contract_id != contract_id or existing.replaces_id != invitation_id:
                raise ContractError("minishop_corp_request_conflict", 409)
            return self._issued(existing)
        instant = now or datetime.now(UTC)
        if contract.ends_at <= instant:
            raise ContractError("minishop_corp_contract_expired", 409)
        if previous.revoked_at is not None:
            raise ContractError("minishop_corp_request_conflict", 409)
        previous.revoked_at = instant
        await session.flush()  # Release the unique active reusable-code slot before inserting.
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
