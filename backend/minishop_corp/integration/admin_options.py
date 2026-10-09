"""Read-only form choices through the pinned host's config, services and DAL."""

from uuid import UUID

from db.dal.user_email_dal import get_user_by_verified_email_address
from db.dal.user_reads_dal import (
    get_user_by_email,
    get_user_by_id,
    get_user_by_telegram_id,
    get_user_by_username,
)
from db.models import User
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..contracts_types import ContractError
from ..diagnostics import event
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
    user_id: int | None = Field(default=None, ge=-(2**63), le=2**63 - 1)

    @model_validator(mode="after")
    def one_selector(self) -> "AccountQuery":
        if self.q and self.user_id is not None:
            raise ValueError("Use a single account selector")
        return self


class AccountChoice(MemberProfile):
    telegram_id: int | None


class AccountRecord(ProfileRecord):
    def choice(self) -> AccountChoice:
        return AccountChoice(
            **self.public().model_dump(),
            telegram_id=self.telegram_id,
        )


class AccountChoices(BaseModel):
    accounts: list[AccountChoice]
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
        if query.user_id is not None:
            kind = "internal_id"
            # Reopening a stored assignment uses its canonical internal identity,
            # which must not be confused with another account's Telegram id.
            row = await get_user_by_id(session, query.user_id)
        elif not value:
            event("account_lookup", query_kind="empty", result="not_found")
            return AccountChoices(accounts=[], next_page=None)
        elif value.lower().startswith("ms_"):
            kind = "minishop_id"
            # Minishop 3.8.1 has no public-id DAL helper. Use the same exact
            # read-only model query as its native admin user detail endpoint.
            row = await session.scalar(select(User).where(User.minishop_id == value.lower()))
        elif "@" in value and not value.startswith("@"):
            kind = "email"
            row = await get_user_by_verified_email_address(session, value)
            if row is None:
                row = await get_user_by_email(session, value)
        elif value.removeprefix("-").isdecimal():
            kind = "numeric_id"
            identifier = int(value) if len(value) <= 20 else 2**63
            row = (
                await get_user_by_id(session, identifier)
                if -(2**63) <= identifier < 2**63
                else None
            )
            telegram = (
                await get_user_by_telegram_id(session, identifier)
                if 0 < identifier < 2**63
                else None
            )
            if row is not None and telegram is not None and row.user_id != telegram.user_id:
                event("account_lookup", query_kind=kind, result="ambiguous")
                raise ContractError("minishop_corp_account_ambiguous", 422)
            if row is not None:
                kind = "internal_id"
            elif telegram is not None:
                kind = "telegram_id"
            row = row if row is not None else telegram
        else:
            kind = "username"
            row = await get_user_by_username(session, value)
        event(
            "account_lookup",
            query_kind=kind,
            result="not_found" if row is None else "blocked" if row.is_banned else "matched",
            matched_user_id=int(row.user_id) if row is not None and not row.is_banned else None,
        )
        return AccountChoices(
            accounts=[AccountRecord.model_validate(row).choice()]
            if row is not None and not row.is_banned
            else [],
            next_page=None,
        )
