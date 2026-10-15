"""Per-process service decoration through PluginContext's service container.

The v1 AudienceProvider catalog is static and cannot resolve a new database-backed
target in another worker. This adapter delegates native targets/providers and
resolves only our namespace from durable plugin data. No core class, global method,
route, delivery queue or core table is modified.
"""

from collections.abc import Callable
from uuid import UUID

from aiohttp import web
from bot.app.web.context import set_service_context
from bot.plugins.spec import PluginContext
from bot.services.audience_segmentation import (
    AudienceDefinition,
    AudienceNotFoundError,
    AudienceProvider,
    AudienceSegmentationService,
)
from db.models import User
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..broadcasts import CONTRACT_PREFIX, PREFIX, SELECTION_PREFIX, contract_target, parse_target
from ..storage.schema import BroadcastAudience, Contract, Membership
from .worker import sessions_for


class CorporateAudienceService(AudienceSegmentationService):
    def __init__(
        self, native: AudienceSegmentationService, sessions: Callable[[], AsyncSession]
    ) -> None:
        super().__init__(
            native.session_factory, panel_service=native.panel_service, tariffs=native.tariffs
        )
        self.native = native
        self.sessions = sessions
        self.catalog: list[AudienceDefinition] = []

    def register_provider(self, provider: AudienceProvider) -> None:
        if provider.target.strip().lower().startswith(PREFIX):
            raise ValueError("Corporate audience namespace is reserved")
        self.native.register_provider(provider)

    def unregister_provider(self, target: str) -> bool:
        return self.native.unregister_provider(target)

    def has_target(self, target: str) -> bool:
        normalized = target.strip().lower()
        if normalized.startswith(PREFIX):
            return parse_target(normalized) is not None
        return self.native.has_target(target)

    def is_target_available(self, target: str) -> bool:
        normalized = target.strip().lower()
        if normalized.startswith(PREFIX):
            # Synchronous discovery is advisory; the async resolver always checks
            # all contracts again, including before native scheduled delivery.
            return parse_target(normalized) is not None
        return self.native.is_target_available(target)

    def audiences(self) -> list[AudienceDefinition]:
        return [*self.native.audiences(), *self.catalog]

    async def resolve_user_ids(self, target: str) -> list[int]:
        normalized = target.strip().lower()
        if not normalized.startswith(PREFIX):
            return await self.native.resolve_user_ids(target)
        parsed = parse_target(normalized)
        if parsed is None:
            raise AudienceNotFoundError(normalized)
        prefix, identifier = parsed
        async with self.sessions() as session:
            if prefix == CONTRACT_PREFIX:
                ids = [identifier]
            else:
                selection = await session.get(BroadcastAudience, identifier)
                if selection is None:
                    raise AudienceNotFoundError(normalized)
                ids = [UUID(value) for value in selection.contract_ids]
            # Keep selected contracts alive until the recipient query completes.
            existing = list(
                await session.scalars(
                    select(Contract.id)
                    .where(Contract.id.in_(ids))
                    .order_by(Contract.id)
                    .with_for_update(read=True)
                )
            )
            if not ids or set(existing) != set(ids):
                raise AudienceNotFoundError(normalized)
            return list(
                await session.scalars(
                    select(Membership.current_user_id)
                    .join(User, User.user_id == Membership.current_user_id)
                    .where(
                        Membership.contract_id.in_(ids),
                        Membership.state == "active",
                        User.is_banned.is_(False),
                    )
                    .distinct()
                    .order_by(Membership.current_user_id)
                )
            )

    async def counts(self) -> dict[str, int | None]:
        counts = await self.native.counts()
        async with self.sessions() as session:
            rows = (await session.execute(select(Contract.id, Contract.name))).all()
            names = {identifier: name for identifier, name in rows}
            grouped = (
                await session.execute(
                    select(Membership.contract_id, func.count(Membership.id))
                    .join(User, User.user_id == Membership.current_user_id)
                    .where(Membership.state == "active", User.is_banned.is_(False))
                    .group_by(Membership.contract_id)
                )
            ).all()
            totals = {identifier: int(total) for identifier, total in grouped}
            catalog = [
                self.definition(contract_target(identifier), name)
                for identifier, name in sorted(rows, key=lambda row: (row[1], row[0]))
            ]
            counts.update(
                {contract_target(identifier): totals.get(identifier, 0) for identifier in names}
            )
            selections = list(await session.scalars(select(BroadcastAudience)))
            for selection in sorted(selections, key=lambda item: item.id):
                ids = [UUID(value) for value in selection.contract_ids]
                target = (
                    contract_target(ids[0])
                    if len(ids) == 1
                    else SELECTION_PREFIX + selection.id.hex
                )
                if not ids or any(identifier not in names for identifier in ids):
                    # Keep the target selectable, labelled unavailable, so the
                    # native editor preserves it instead of resetting to "all".
                    # Actual sending still fails in resolve_user_ids above.
                    catalog.append(
                        AudienceDefinition(
                            target=target,
                            label_key="minishop_corp_broadcast_unavailable",
                            fallback_label="Unavailable corporate audience",
                            order=600,
                            group_label_key="minishop_corp_broadcast_group",
                            group_fallback_label="Corporate subscriptions",
                        )
                    )
                    counts[target] = None
                    continue
                if len(ids) == 1:
                    continue
                label = " + ".join(names[identifier] for identifier in ids)
                catalog.append(
                    self.definition(target, label if len(label) <= 240 else label[:239] + "…")
                )
                counts[target] = sum(totals.get(identifier, 0) for identifier in ids)
        # Publish only a complete catalog. An unavailable database never becomes
        # an empty audience or a fabricated zero count.
        self.catalog = catalog
        return counts

    @staticmethod
    def definition(target: str, label: str) -> AudienceDefinition:
        return AudienceDefinition(
            target=target,
            label_key="minishop_corp_audience_" + target.removeprefix(PREFIX).replace(":", "_"),
            fallback_label=label,
            order=600,
            group_label_key="minishop_corp_broadcast_group",
            group_fallback_label="Corporate subscriptions",
            icon="shield",
        )


def setup_broadcasts(context: PluginContext) -> None:
    native = context.audience_segmentation_service
    # Auxiliary entrypoints need not provide the messaging services.
    if native is not None and not isinstance(native, CorporateAudienceService):
        context.services["audience_segmentation_service"] = CorporateAudienceService(
            native, sessions_for(context)
        )


def setup_broadcast_web(context: PluginContext, app: web.Application) -> None:
    # 3.8.1 copies this service to the webhook app, but omits it when building
    # the separate Mini App. Use the host context setter before routes start.
    service = context.audience_segmentation_service
    if service is not None:
        set_service_context(app, "audience_segmentation_service", service)
