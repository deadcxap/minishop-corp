"""Read-only form choices through the pinned host's config, services and DAL."""

from uuid import UUID

from db.dal.user_reads_dal import (
    get_all_users_paginated,
    get_user_by_id,
    get_user_by_username,
)
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from ..contracts_types import ContractError
from ..invitations_types import TariffOffer
from ..member_types import MemberProfile
from .contracts import ContractHost
from .member_data import ProfileRecord


class TariffChoice(TariffOffer):
    hidden: bool


class SquadChoice(BaseModel):
    uuid: UUID
    name: str


class SquadQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    uuid: UUID


class AccountQuery(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    q: str = Field(default="", max_length=100)
    page: int = Field(default=0, ge=0, le=100_000)


class AccountChoices(BaseModel):
    accounts: list[MemberProfile]
    next_page: int | None


class AdminOptions:
    def __init__(self, host: ContractHost) -> None:
        self.host = host

    def tariffs(self) -> list[TariffChoice]:
        config = self.host.service.settings.tariffs_config
        if config is None:
            return []
        return [
            TariffChoice(**self.host.describe_tariff(row.key).model_dump(), hidden=not row.enabled)
            for row in config.tariffs
            if row.billing_model == "period"
        ]

    async def squad(self, identifier: UUID) -> SquadChoice:
        # This host revision exposes only a detail lookup, not an external-squad
        # catalogue. Verify the supplied UUID and show the native name in the form.
        raw = await self.host.service.panel_service.get_external_squad(str(identifier))
        try:
            result = SquadChoice.model_validate(raw)
        except ValidationError as exc:
            raise ContractError("minishop_corp_squad_unavailable", 422) from exc
        if result.uuid != identifier:
            raise ContractError("minishop_corp_squad_unavailable", 422)
        return result

    async def accounts(self, session: AsyncSession, query: AccountQuery) -> AccountChoices:
        value = query.q.lstrip("@")
        if not value:
            rows = await get_all_users_paginated(session, page=query.page, page_size=20)
            next_page = query.page + 1 if len(rows) == 20 else None
        else:
            if value.removeprefix("-").isdecimal():
                identifier = int(value) if len(value) <= 20 else 2**63
                row = (
                    await get_user_by_id(session, identifier)
                    if -(2**63) <= identifier < 2**63
                    else None
                )
            else:
                row = await get_user_by_username(session, value)
            rows = [row] if row is not None else []
            next_page = None
        return AccountChoices(
            accounts=[
                ProfileRecord.model_validate(row).public() for row in rows if not row.is_banned
            ],
            next_page=next_page,
        )
