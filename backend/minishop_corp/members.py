"""Current, authorized contract members with native statistics and private avatars."""

import asyncio
from uuid import UUID

from aiohttp import web
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .contracts import Contracts
from .contracts_types import ContractError, ContractPage
from .integration.contracts import ContractHost
from .integration.member_avatar import MemberAvatars
from .integration.member_data import MemberData
from .member_types import MemberAvatar, MemberInfo, MembersPage
from .membership_types import OperationInfo
from .storage.schema import Membership, Operation


class Members:
    def __init__(self, host: ContractHost) -> None:
        self.host = host
        self.contracts = Contracts(host)
        self.data = MemberData(host.service)
        self.avatars = MemberAvatars(host.service)

    async def authorize(
        self,
        session: AsyncSession,
        actor: int,
        contract_id: UUID,
        *,
        manager: bool,
    ) -> None:
        if manager:
            await self.contracts.require_manager(session, actor, contract_id)
        else:
            await self.host.require_admin(session, actor)
            await self.contracts._get(session, contract_id)

    async def member(
        self,
        session: AsyncSession,
        contract_id: UUID,
        member_id: UUID,
    ) -> Membership:
        row = await session.scalar(
            select(Membership)
            .where(
                Membership.id == member_id,
                Membership.contract_id == contract_id,
                Membership.current_user_id.is_not(None),
            )
            .execution_options(populate_existing=True)
        )
        if row is None:
            raise ContractError("minishop_corp_membership_missing", 404)
        return row

    async def page(
        self,
        session: AsyncSession,
        actor: int,
        contract_id: UUID,
        page: ContractPage,
        *,
        manager: bool,
        refresh: bool = True,
    ) -> MembersPage:
        await self.authorize(session, actor, contract_id, manager=manager)
        query = (
            select(Membership)
            .where(Membership.contract_id == contract_id, Membership.current_user_id.is_not(None))
            .order_by(Membership.id)
            .limit(page.limit + 1)
        )
        if page.after is not None:
            query = query.where(Membership.id > page.after)
        found = list(await session.scalars(query))
        rows = found[: page.limit]
        next_after = rows[-1].id if len(found) > page.limit else None
        sources = await self.data.sources(session, [row.user_id for row in rows])
        selected = {row.id: sources[row.user_id] for row in rows if row.user_id in sources}
        stats = dict(
            zip(
                selected,
                await asyncio.gather(
                    *(self.data.statistics(source, refresh=refresh) for source in selected.values())
                ),
                strict=True,
            )
        )
        # Loading cache misses can await external services. A departing member or
        # revoked role must not be served from the initial identity map afterwards.
        session.expire_all()
        await self.authorize(session, actor, contract_id, manager=manager)
        current = list(
            await session.scalars(
                select(Membership)
                .where(
                    Membership.id.in_(selected),
                    Membership.contract_id == contract_id,
                    Membership.current_user_id.is_not(None),
                )
                .order_by(Membership.id)
            )
        )
        last_operations = (
            select(
                Operation.id,
                func.row_number()
                .over(
                    partition_by=Operation.membership_id,
                    order_by=(Operation.created_at.desc(), Operation.id.desc()),
                )
                .label("position"),
            )
            .where(Operation.membership_id.in_(selected))
            .subquery()
        )
        operations = {
            op.membership_id: OperationInfo.model_validate(op)
            for op in await session.scalars(
                select(Operation)
                .join(last_operations, last_operations.c.id == Operation.id)
                .where(last_operations.c.position == 1)
            )
        }
        prefix = (
            "/api/plugins/minishop-corp/managed-contracts"
            if manager
            else "/api/admin/minishop-corp/contracts"
        )
        result = []
        for row in current:
            source = selected[row.id]
            result.append(
                MemberInfo(
                    id=row.id,
                    state=row.state,
                    joined_at=row.joined_at,
                    profile=source.profile.public(),
                    statistics=stats[row.id],
                    avatar_path=f"{prefix}/{contract_id}/members/{row.id}/avatar"
                    if source.has_avatar or (source.profile.telegram_id or 0) > 0
                    else None,
                    operation=operations.get(row.id),
                )
            )
        return MembersPage(members=result, next_after=next_after)

    async def avatar(
        self,
        request: web.Request,
        session: AsyncSession,
        actor: int,
        contract_id: UUID,
        member_id: UUID,
        *,
        manager: bool,
        refresh: bool = True,
    ) -> MemberAvatar:
        await self.authorize(session, actor, contract_id, manager=manager)
        row = await self.member(session, contract_id, member_id)
        result = await self.avatars.read(request, session, row.user_id, refresh=refresh)
        await session.flush()
        session.expire_all()
        await self.authorize(session, actor, contract_id, manager=manager)
        await self.member(session, contract_id, member_id)
        return result
