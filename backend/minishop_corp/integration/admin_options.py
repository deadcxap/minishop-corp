"""Read-only form choices through the pinned host's config, services and DAL."""

from uuid import UUID

from db.dal.user_email_dal import get_user_by_verified_email_address
from db.dal.user_reads_dal import (
    get_user_by_email,
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
    q: str = Field(default="", max_length=320)


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
        value = query.q
        if not value:
            return AccountChoices(accounts=[], next_page=None)
        if "@" in value and not value.startswith("@"):
            row = await get_user_by_verified_email_address(session, value)
            if row is None:
                row = await get_user_by_email(session, value)
        elif value.removeprefix("-").isdecimal():
            identifier = int(value) if len(value) <= 20 else 2**63
            row = (
                await get_user_by_id(session, identifier)
                if -(2**63) <= identifier < 2**63
                else None
            )
        else:
            row = await get_user_by_username(session, value)
        return AccountChoices(
            accounts=[ProfileRecord.model_validate(row).public()]
            if row is not None and not row.is_banned
            else [],
            next_page=None,
        )
