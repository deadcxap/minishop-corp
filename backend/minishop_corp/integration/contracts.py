"""Current account roles and reference validation through Minishop services/DAL."""

from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from uuid import UUID

from aiohttp import web
from bot.app.web.context import get_subscription_service
from bot.app.web.webapp.extension_runtime import user_context
from bot.services.account_roles import is_admin
from bot.services.subscription_service import SubscriptionService
from db.dal.user_dal import lock_user_by_id
from db.dal.user_reads_dal import get_user_by_id
from pydantic import BaseModel, ConfigDict, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from ..contracts_types import ContractError, ContractTerms
from ..invitations_types import TariffOffer
from .access import AccessAdapter
from .access_types import AccessError


class Account(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    user_id: int
    is_banned: bool


class ExternalSquad(BaseModel):
    uuid: UUID


@dataclass(frozen=True)
class RequestActor:
    session: AsyncSession
    user_id: int


@asynccontextmanager
async def request_actor(request: web.Request) -> AsyncIterator[RequestActor]:
    async with user_context(request) as context:
        # Native sessionmaker annotations predate SQLAlchemy's AsyncSession typing.
        if not isinstance(context.session, AsyncSession):
            raise TypeError("Minishop must provide an AsyncSession")
        yield RequestActor(context.session, context.user_id)


class ContractHost:
    def __init__(self, service: SubscriptionService) -> None:
        self.service = service

    @classmethod
    def from_request(cls, request: web.Request) -> "ContractHost":
        return cls(get_subscription_service(request))

    def describe_tariff(self, key: str) -> TariffOffer:
        tariff = AccessAdapter(self.service)._period_tariff(key)
        return TariffOffer(
            key=tariff.key,
            names={lang: tariff.name(lang) for lang in ("ru", "en")},
            traffic_limit_bytes=self.service._traffic_limit_for_period_tariff(tariff),
            hwid_device_limit=self.service._base_hwid_limit_for_tariff(tariff),
            traffic_strategy=self.service._period_tariff_traffic_strategy(tariff),
        )

    async def require_account(self, session: AsyncSession, user_id: int) -> None:
        user = await get_user_by_id(session, user_id)
        if user is None or Account.model_validate(user).is_banned:
            raise ContractError("minishop_corp_access_denied", 403)

    async def require_admin(self, session: AsyncSession, user_id: int) -> None:
        await self.require_account(session, user_id)
        if not await is_admin(session, user_id):
            raise ContractError("minishop_corp_access_denied", 403)

    async def lock_accounts(self, session: AsyncSession, user_ids: Iterable[int]) -> None:
        for user_id in sorted(set(user_ids)):
            user = await lock_user_by_id(session, user_id)
            if user is None or Account.model_validate(user).is_banned:
                raise ContractError("minishop_corp_account_unavailable", 422)

    async def validate_references(self, session: AsyncSession, terms: ContractTerms) -> None:
        try:
            AccessAdapter(self.service)._period_tariff(terms.tariff_key)
        except AccessError as exc:
            raise ContractError(exc.code.value, 422) from exc
        if terms.manager_user_id is not None:
            manager = await get_user_by_id(session, terms.manager_user_id)
            if manager is None or Account.model_validate(manager).is_banned:
                raise ContractError("minishop_corp_account_unavailable", 422)
        # Minishop owns the cache and refresh on a miss; no plugin panel client.
        if terms.external_squad_uuid is None:
            return
        squad = await self.service.panel_service.get_external_squad(str(terms.external_squad_uuid))
        try:
            resolved = ExternalSquad.model_validate(squad)
        except ValidationError as exc:
            # The native API returns None for both missing squad and infrastructure failure.
            raise ContractError("minishop_corp_squad_unavailable", 422) from exc
        if resolved.uuid != terms.external_squad_uuid:
            raise ContractError("minishop_corp_squad_unavailable", 422)
