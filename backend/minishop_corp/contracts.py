"""Scoped contract management; callers own the transaction, never the authorization."""

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .contracts_types import (
    ContractDetails,
    ContractError,
    ContractPage,
    ContractSummary,
    ContractTerms,
    CreateContract,
    UpdateContract,
)
from .integration.contracts import ContractHost
from .storage.schema import AuditEvent, Contract, Membership, Revision


def terms_of(row: Contract) -> ContractTerms:
    return ContractTerms.model_validate(row, from_attributes=True)


def summary_of(row: Contract) -> ContractSummary:
    return ContractSummary(
        id=row.id,
        name=row.name,
        ends_at=row.ends_at,
        version=row.version,
        expired=row.ends_at <= datetime.now(UTC),
    )


def details_of(row: Contract, member_count: int) -> ContractDetails:
    return ContractDetails(
        **summary_of(row).model_dump(),
        member_count=member_count,
        tariff_key=row.tariff_key,
        external_squad_uuid=row.external_squad_uuid,
        manager_user_id=row.manager_user_id,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


class Contracts:
    def __init__(self, host: ContractHost) -> None:
        self.host = host

    async def create(
        self, session: AsyncSession, actor: int, draft: CreateContract
    ) -> tuple[ContractDetails, bool]:
        await self.host.require_admin(session, actor)
        # Serialize retries for this actor and protect the manager from deletion before the FK.
        accounts = [actor]
        if draft.manager_user_id is not None:
            accounts.append(draft.manager_user_id)
        await self.host.lock_accounts(session, accounts)
        existing = await session.get(Contract, draft.id, populate_existing=True)
        terms = ContractTerms.model_validate(draft.model_dump(exclude={"id"}))
        if existing is not None:
            if existing.version != 1 or terms_of(existing) != terms:
                raise ContractError("minishop_corp_request_conflict", 409)
            return await self._details(session, existing), False
        await self.host.validate_references(session, terms)
        row = Contract(id=draft.id, **terms.model_dump())
        session.add(row)
        await session.flush()
        await self._record(session, row, actor, "contract_created")
        return details_of(row, 0), True

    async def update(
        self, session: AsyncSession, actor: int, contract_id: UUID, draft: UpdateContract
    ) -> ContractDetails:
        await self.host.require_admin(session, actor)
        accounts = [actor]
        if draft.manager_user_id is not None:
            accounts.append(draft.manager_user_id)
        await self.host.lock_accounts(session, accounts)
        row = await self._get(session, contract_id, lock=True)
        if row.version != draft.expected_version:
            raise ContractError("minishop_corp_version_conflict", 409)
        terms = ContractTerms.model_validate(draft.model_dump(exclude={"expected_version"}))
        if terms == terms_of(row):
            return await self._details(session, row)
        await self.host.validate_references(session, terms)
        row.name = terms.name
        row.tariff_key = terms.tariff_key
        row.external_squad_uuid = terms.external_squad_uuid
        row.ends_at = terms.ends_at
        row.manager_user_id = terms.manager_user_id
        row.version += 1
        row.updated_at = datetime.now(UTC)
        await self._record(session, row, actor, "contract_updated")
        return await self._details(session, row)

    async def admin_get(
        self, session: AsyncSession, actor: int, contract_id: UUID
    ) -> ContractDetails:
        await self.host.require_admin(session, actor)
        return await self._details(session, await self._get(session, contract_id))

    async def admin_list(
        self, session: AsyncSession, actor: int, page: ContractPage
    ) -> list[ContractDetails]:
        await self.host.require_admin(session, actor)
        rows = await self._list(session, page)
        counts = dict(
            (
                await session.execute(
                    select(Membership.contract_id, func.count())
                    .where(
                        Membership.contract_id.in_([row.id for row in rows]),
                        Membership.current_user_id.is_not(None),
                    )
                    .group_by(Membership.contract_id)
                )
            )
            .tuples()
            .all()
        )
        return [details_of(row, counts.get(row.id, 0)) for row in rows]

    @staticmethod
    async def _details(session: AsyncSession, row: Contract) -> ContractDetails:
        count = await session.scalar(
            select(func.count())
            .select_from(Membership)
            .where(Membership.contract_id == row.id, Membership.current_user_id.is_not(None))
        )
        return details_of(row, count or 0)

    async def managed_list(
        self, session: AsyncSession, actor: int, page: ContractPage
    ) -> list[ContractSummary]:
        await self.host.require_account(session, actor)
        return [summary_of(row) for row in await self._list(session, page, manager=actor)]

    async def require_manager(
        self, session: AsyncSession, actor: int, contract_id: UUID, *, lock: bool = False
    ) -> Contract:
        await self.host.require_account(session, actor)
        row = await self._get(session, contract_id, lock=lock)
        if row.manager_user_id != actor:
            raise ContractError("minishop_corp_contract_missing", 404)
        return row

    async def require_member(
        self, session: AsyncSession, actor: int, contract_id: UUID
    ) -> Contract:
        await self.host.require_account(session, actor)
        row = await self._get(session, contract_id)
        current = await session.scalar(
            select(Membership.id).where(
                Membership.contract_id == contract_id, Membership.current_user_id == actor
            )
        )
        if current is None:
            raise ContractError("minishop_corp_contract_missing", 404)
        return row

    @staticmethod
    async def _get(session: AsyncSession, contract_id: UUID, *, lock: bool = False) -> Contract:
        query = (
            select(Contract)
            .where(Contract.id == contract_id)
            .execution_options(populate_existing=True)
        )
        if lock:
            query = query.with_for_update()
        row = await session.scalar(query)
        if row is None:
            raise ContractError("minishop_corp_contract_missing", 404)
        return row

    @staticmethod
    async def _list(
        session: AsyncSession, page: ContractPage, *, manager: int | None = None
    ) -> list[Contract]:
        query = select(Contract).order_by(Contract.id).limit(page.limit)
        if page.after is not None:
            query = query.where(Contract.id > page.after)
        if manager is not None:
            query = query.where(Contract.manager_user_id == manager)
        return list((await session.scalars(query)).all())

    @staticmethod
    async def _record(session: AsyncSession, row: Contract, actor: int, action: str) -> None:
        session.add(
            Revision(
                contract_id=row.id,
                version=row.version,
                actor_user_id=actor,
                **terms_of(row).model_dump(),
            )
        )
        session.add(
            AuditEvent(
                contract_id=row.id,
                actor_user_id=actor,
                action=action,
                details={"version": row.version},
            )
        )
        await session.flush()
