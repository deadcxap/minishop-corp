"""Entitlements through Minishop services/DAL on the revision pinned in dev/minishop.json.

The caller owns the transaction, durable operation, membership/version checks and retry.
No method commits. A failed external confirmation is not a successful grant, and a DB
rollback cannot undo an HTTP write: retry the SAME persisted target after revalidation.
"""

from datetime import UTC, datetime, timedelta

from bot.plugins.spec import PluginContext
from bot.services.subscription_service import SubscriptionService
from bot.services.subscription_service_impl.panel_identity import PanelUserCreateOptions
from config.tariffs_config import Tariff, default_currency_key_for_settings
from db.dal import subscription_dal, tariff_dal, user_dal
from db.dal import user_panel_squad_override_dal as squad_dal
from db.dal.user_reads_dal import get_user_by_id
from db.models import Subscription, User
from db.tariff_squad_sync import (
    remember_subscription_tariff_managed_squad_uuids,
    subscription_tariff_managed_squad_uuids,
)
from sqlalchemy.ext.asyncio import AsyncSession

from .access_types import (
    AccessError,
    AccessFailure,
    AccessState,
    DisabledAccess,
    PeriodAccess,
    TrialAccess,
    panel_time,
)
from .billing import require_clear_billing

SOURCE = "minishop-corp"


class AccessAdapter:
    def __init__(self, service: SubscriptionService) -> None:
        self.service = service

    @classmethod
    def from_context(cls, context: PluginContext) -> "AccessAdapter":
        return cls(context.require_subscription_service())

    async def read(self, session: AsyncSession, user_id: int) -> AccessState | None:
        row = await self._current(session, user_id)
        return AccessState.model_validate(row) if row is not None else None

    def prepare_trial(self, *, starts_at: datetime) -> TrialAccess:
        self._require_trial()
        return TrialAccess(
            starts_at, starts_at + timedelta(days=self.service.settings.TRIAL_DURATION_DAYS)
        )

    def prepare_departure(self, *, starts_at: datetime) -> TrialAccess | DisabledAccess:
        if (
            not self.service.settings.TRIAL_ENABLED
            or self.service.settings.TRIAL_DURATION_DAYS <= 0
        ):
            return DisabledAccess(starts_at)
        return self.prepare_trial(starts_at=starts_at)

    async def validate_join(self, session: AsyncSession, user_id: int) -> None:
        await self._lock_user(session, user_id)
        await require_clear_billing(session, user_id)
        await self._require_supported_quotas(session, await self._current(session, user_id))

    async def assign_period(
        self, session: AsyncSession, user_id: int, target: PeriodAccess
    ) -> AccessState:
        user = await self._lock_user(session, user_id)
        previous = await self._current(session, user_id)
        self._require_resolved_renewal(previous)
        await self._require_supported_quotas(session, previous)
        tariff = self._period_tariff(target.tariff_key)
        previous_squads = self._managed_squads(previous)
        managed = self.service._panel_squads_for_tariff(tariff) or []
        base_devices = self.service._base_hwid_limit_for_tariff(tariff)
        strategy = self.service._period_tariff_traffic_strategy(tariff)
        link = await self.service._get_or_create_panel_user_link(
            session,
            user_id,
            user,
            create_options=PanelUserCreateOptions(
                default_expire_days=0,
                expire_at=target.ends_at,
                default_traffic_limit_bytes=self.service._traffic_limit_for_period_tariff(tariff),
                default_traffic_limit_strategy=strategy,
                hwid_device_limit=base_devices,
                specific_squad_uuids=tuple(managed),
                external_squad_uuid=target.external_squad_uuid,
                tag=tariff.key,
            ),
        )
        if not link.panel_user_uuid or not link.panel_subscription_uuid or not link.panel_user:
            raise AccessError(AccessFailure.PANEL_UNCONFIRMED)
        # Preserve real manual exceptions through the host, excluding both sides of
        # this transition so a retry cannot mistake old/new managed squads for manual ones.
        await self.service.capture_panel_squad_overrides(
            session,
            user_id=user_id,
            panel_user_uuid=link.panel_user_uuid,
            managed_internal_squads=[*previous_squads, *managed],
            panel_user_snapshot=link.panel_user,
        )
        now = panel_time(datetime.now(UTC))
        # Upsert uses the stable panel subscription identity, including inactive subscriptions.
        row = await subscription_dal.upsert_subscription(
            session,
            {
                "user_id": user_id,
                "panel_user_uuid": link.panel_user_uuid,
                "panel_subscription_uuid": link.panel_subscription_uuid,
                "start_date": (
                    (AccessState.model_validate(previous).start_date or now)
                    if previous is not None
                    else now
                ),
                "end_date": target.ends_at,
                "is_active": target.ends_at > now,
                "status_from_panel": "ACTIVE" if target.ends_at > now else "EXPIRED",
                "provider": "admin",
                "auto_renew_enabled": False,
                "tariff_key": tariff.key,
                "tariff_binding_source": "admin",
                "tariff_binding_note": SOURCE,
                "gift_terms_snapshot": None,
                "duration_months": 0,
                "duration_days": None,
                "period_semantics": None,
                "tier_baseline_bytes": tariff.monthly_bytes,
                "premium_baseline_bytes": tariff.premium_monthly_bytes,
                "hwid_device_limit": base_devices,
                "hwid_device_limit_is_override": False,
                "is_throttled": False,
                "skip_notifications": False,
                "suppress_early_expiry_notifications": False,
                "effective_monthly_price_rub": self.service._tariff_effective_monthly_price(
                    tariff, default_currency_key_for_settings(self.service.settings)
                ),
            },
        )
        state = AccessState.model_validate(row)
        extra_devices = await tariff_dal.sum_active_hwid_devices(
            session, subscription_id=state.subscription_id, at=now
        )
        limit = self.service._traffic_limit_for_period_tariff(
            tariff,
            topup_balance_bytes=state.topup_balance_bytes,
            regular_bonus_bytes=state.regular_bonus_bytes,
            regular_unlimited_override=state.regular_unlimited_override,
            traffic_used_bytes=state.traffic_used_bytes or 0,
            hwid_device_bonus_bytes=await self.service._hwid_device_traffic_bonus_bytes_for_sub(
                session, row
            ),
        )
        premium_limit = self.service._premium_effective_limit_bytes(
            tariff.premium_monthly_bytes,
            state.premium_topup_balance_bytes,
            state.premium_topup_used_bytes,
            state.premium_bonus_bytes,
        )
        premium_limited = self.service._premium_access_should_be_limited(
            tariff,
            premium_limit_bytes=premium_limit,
            premium_used_bytes=state.premium_used_bytes,
            premium_unlimited_override=state.premium_unlimited_override,
        )
        await subscription_dal.update_subscription(
            session,
            state.subscription_id,
            {
                "traffic_limit_bytes": limit,
                "extra_hwid_devices": extra_devices,
                "premium_is_limited": premium_limited,
            },
        )
        await subscription_dal.deactivate_other_active_subscriptions(
            session, state.panel_user_uuid, link.panel_subscription_uuid
        )
        # The old panel value must not overwrite this new, persisted corporate override.
        await squad_dal.set_external_override(
            session,
            user_id=user_id,
            panel_user_uuid=state.panel_user_uuid,
            mode="set",
            squad_uuid=target.external_squad_uuid,
            source=SOURCE,
        )
        await self._remove_obsolete_panel_overrides(session, state, [*previous_squads, *managed])
        active_squads = (
            self.service._panel_squads_for_tariff(tariff, include_premium=not premium_limited) or []
        )
        await self._synchronize(
            session,
            user,
            row,
            managed_squads=active_squads,
            strategy=strategy,
            devices=self.service._effective_hwid_limit(base_devices, extra_devices),
            is_trial=False,
        )
        remember_subscription_tariff_managed_squad_uuids(row, managed)
        await session.flush()
        return AccessState.model_validate(row)

    async def grant_trial(
        self, session: AsyncSession, user_id: int, target: TrialAccess
    ) -> AccessState:
        self._require_trial()
        user = await self._lock_user(session, user_id)
        row = await self._current(session, user_id)
        if row is None:
            raise AccessError(AccessFailure.SUBSCRIPTION_MISSING)
        self._require_resolved_renewal(row)
        await self._require_supported_quotas(session, row)
        state = AccessState.model_validate(row)
        repeated = (
            state.provider == "trial"
            and state.tariff_key is None
            and state.start_date == target.starts_at
            and state.end_date == target.ends_at
        )
        settings = self.service.settings
        trial_squads = self.service._trial_all_panel_squad_uuids()
        snapshot = await self.service._get_panel_user_for_entitlement_verification(
            state.panel_user_uuid
        )
        if snapshot is None:
            raise AccessError(AccessFailure.PANEL_UNCONFIRMED)
        await self.service.capture_panel_squad_overrides(
            session,
            user_id=user_id,
            panel_user_uuid=state.panel_user_uuid,
            managed_internal_squads=[*self._managed_squads(row), *trial_squads],
            panel_user_snapshot=snapshot,
        )
        await self._remove_obsolete_panel_overrides(session, state, self._managed_squads(row))
        # Explicitly clear a corporate external squad when there is no default squad.
        external = settings.parsed_user_external_squad_uuid
        await squad_dal.set_external_override(
            session,
            user_id=user_id,
            panel_user_uuid=state.panel_user_uuid,
            mode="set" if external else "cleared",
            squad_uuid=external,
            source=SOURCE,
        )
        if not repeated:
            # A new standard trial replaces additional device entitlements but keeps
            # their purchase/payment history. Use the host's expiry operation.
            purchases = await tariff_dal.get_hwid_device_value_entries(
                session, subscription_id=state.subscription_id, at=target.starts_at
            )
            await tariff_dal.expire_hwid_device_purchases(
                session,
                purchase_ids=[int(purchase["purchase_id"]) for purchase in purchases],
                at=target.starts_at,
            )
            # Native reset eligibility keeps account, payments and older subscription rows.
            # Clear fields which activate_trial_subscription leaves on the reused current row.
            await subscription_dal.update_subscription(
                session,
                state.subscription_id,
                {
                    "is_active": False,
                    "start_date": target.starts_at,
                    "tariff_key": None,
                    "gift_terms_snapshot": None,
                    "tier_baseline_bytes": settings.trial_traffic_limit_bytes,
                    "topup_balance_bytes": 0,
                    "regular_bonus_bytes": 0,
                    "regular_unlimited_override": False,
                    "premium_bonus_bytes": 0,
                    "premium_unlimited_override": False,
                    # Host generic limit sync otherwise falls back to USER_HWID_DEVICE_LIMIT.
                    "hwid_device_limit_is_override": True,
                    "extra_hwid_devices": 0,
                    "period_start_at": None,
                    "premium_period_start_at": None,
                    "traffic_period_lifetime_start_bytes": None,
                    "traffic_topup_accounting_state": None,
                    "effective_monthly_price_rub": None,
                    "is_throttled": False,
                },
            )
            await user_dal.mark_trial_eligibility_reset(session, user_id, reset_at=target.starts_at)
            # Apply squad changes first: native trial activation discovers panel overrides.
            # Otherwise it would adopt the old corporate squads as manual exceptions.
            fields = await self.service.build_effective_panel_squad_fields(
                session,
                user_id=user_id,
                panel_user_uuid=state.panel_user_uuid,
                managed_internal_squads=trial_squads,
                discover_panel_overrides=False,
            )
            await self._push(state.panel_user_uuid, fields)
            result = await self.service.activate_trial_subscription(
                session, user_id, commit=False, emit_event=False
            )
            if not result or result.get("activated") is not True:
                raise AccessError(AccessFailure.PANEL_UNCONFIRMED)
            # The native activation may roll back on failure; never continue after that.
            row = await self._current(session, user_id)
            if row is None:
                raise AccessError(AccessFailure.SUBSCRIPTION_MISSING)
            state = AccessState.model_validate(row)
        await subscription_dal.update_subscription(
            session,
            state.subscription_id,
            {
                "start_date": target.starts_at,
                "end_date": target.ends_at,
                "is_active": target.ends_at > datetime.now(UTC),
                "status_from_panel": "TRIAL" if target.ends_at > datetime.now(UTC) else "EXPIRED",
            },
        )
        await self._synchronize(
            session,
            user,
            row,
            managed_squads=trial_squads,
            strategy=settings.TRIAL_TRAFFIC_STRATEGY,
            devices=settings.TRIAL_HWID_DEVICE_LIMIT,
            is_trial=True,
        )
        remember_subscription_tariff_managed_squad_uuids(row, trial_squads)
        # After verified removal, standard default inheritance can resume.
        await squad_dal.deactivate_external_override(
            session, user_id=user_id, panel_user_uuid=state.panel_user_uuid
        )
        await session.flush()
        return AccessState.model_validate(row)

    async def disable_access(
        self, session: AsyncSession, user_id: int, target: DisabledAccess
    ) -> AccessState:
        """Q-02: remove corporate entitlements without creating a trial or a payment."""
        user = await self._lock_user(session, user_id)
        row = await self._current(session, user_id)
        if row is None:
            raise AccessError(AccessFailure.SUBSCRIPTION_MISSING)
        self._require_resolved_renewal(row)
        await self._require_supported_quotas(session, row)
        state = AccessState.model_validate(row)
        previous = self._managed_squads(row)
        defaults = list(self.service.settings.parsed_user_squad_uuids or [])
        snapshot = await self.service._get_panel_user_for_entitlement_verification(
            state.panel_user_uuid
        )
        if snapshot is None:
            raise AccessError(AccessFailure.PANEL_UNCONFIRMED)
        await self.service.capture_panel_squad_overrides(
            session,
            user_id=user_id,
            panel_user_uuid=state.panel_user_uuid,
            managed_internal_squads=[*previous, *defaults],
            panel_user_snapshot=snapshot,
        )
        await self._remove_obsolete_panel_overrides(session, state, previous)
        external = self.service.settings.parsed_user_external_squad_uuid
        await squad_dal.set_external_override(
            session,
            user_id=user_id,
            panel_user_uuid=state.panel_user_uuid,
            mode="set" if external else "cleared",
            squad_uuid=external,
            source=SOURCE,
        )
        await subscription_dal.update_subscription(
            session,
            state.subscription_id,
            {
                "end_date": min(target.ends_at, panel_time(datetime.now(UTC))),
                "is_active": False,
                "status_from_panel": "EXPIRED",
                "tariff_key": None,
                "provider": "admin",
                "tariff_binding_source": "admin",
                "tariff_binding_note": SOURCE,
                "gift_terms_snapshot": None,
                "tier_baseline_bytes": None,
                "premium_baseline_bytes": 0,
                "hwid_device_limit_is_override": False,
                "effective_monthly_price_rub": None,
                "is_throttled": False,
            },
        )
        await subscription_dal.deactivate_all_user_subscriptions(session, user_id)
        await session.refresh(row)
        await self._synchronize(
            session,
            user,
            row,
            managed_squads=defaults,
            strategy=self.service.settings.USER_TRAFFIC_STRATEGY,
            devices=self.service.settings.USER_HWID_DEVICE_LIMIT,
            is_trial=False,
        )
        remember_subscription_tariff_managed_squad_uuids(row, defaults)
        await squad_dal.deactivate_external_override(
            session, user_id=user_id, panel_user_uuid=state.panel_user_uuid
        )
        await session.flush()
        return AccessState.model_validate(row)

    async def _synchronize(
        self,
        session: AsyncSession,
        user: User,
        row: Subscription,
        *,
        managed_squads: list[str],
        strategy: str,
        devices: int | None,
        is_trial: bool,
    ) -> None:
        state = AccessState.model_validate(row)
        snapshot = await self.service._get_panel_user_for_entitlement_verification(
            state.panel_user_uuid
        )
        if snapshot is None:
            raise AccessError(AccessFailure.PANEL_UNCONFIRMED)
        tag = self.service._plan_panel_tariff_tag(
            user, snapshot, state.tariff_key, source=SOURCE, is_trial=is_trial
        )
        payload = self.service._build_panel_update_payload(
            expire_at=state.end_date,
            status="ACTIVE" if state.is_active else "EXPIRED",
            traffic_limit_bytes=state.traffic_limit_bytes,
            traffic_limit_strategy=strategy,
            hwid_device_limit=devices,
            include_default_squads=False,
        )
        payload.update(
            await self.service.build_effective_panel_squad_fields(
                session,
                user_id=state.user_id,
                panel_user_uuid=state.panel_user_uuid,
                managed_internal_squads=managed_squads,
                discover_panel_overrides=False,
            )
        )
        payload.update(self.service._panel_identity_payload_for_user(user))
        payload.update(tag.verification_payload)
        confirmed = await self._push(state.panel_user_uuid, payload)
        self.service._remember_confirmed_panel_tariff_tag(user, tag, confirmed)

    async def _push(self, panel_user_uuid: str, fields: dict[str, object]) -> dict[str, object]:
        result = await self.service.panel_service.update_user_details_on_panel(
            panel_user_uuid, fields
        )
        confirmed = await self.service._confirmed_panel_entitlement(
            panel_user_uuid, result, fields, source=SOURCE
        )
        if confirmed is None:
            raise AccessError(AccessFailure.PANEL_UNCONFIRMED)
        return dict(confirmed)

    async def _current(self, session: AsyncSession, user_id: int) -> Subscription | None:
        user = await get_user_by_id(session, user_id)
        if user is None:
            raise AccessError(AccessFailure.USER_MISSING)
        if user.panel_user_uuid:
            snapshot = await self.service.panel_service.get_user_by_uuid(str(user.panel_user_uuid))
            if snapshot:
                reference = snapshot.get("subscriptionUuid") or snapshot.get("shortUuid")
                if isinstance(reference, str):
                    row = await subscription_dal.get_subscription_by_panel_subscription_uuid(
                        session, reference
                    )
                    if row is not None:
                        await session.refresh(row)
                        if AccessState.model_validate(row).user_id != user_id:
                            raise AccessError(AccessFailure.PANEL_UNCONFIRMED)
                        return row
        # This includes expired rows; get_active_subscription_by_user_id would lose them.
        row = await subscription_dal.get_latest_subscription_by_user_id(
            session,
            user_id,
            panel_user_uuid=str(user.panel_user_uuid) if user.panel_user_uuid else None,
        )
        if row is not None:
            await session.refresh(row)
        return row

    async def _lock_user(self, session: AsyncSession, user_id: int) -> User:
        user = await user_dal.lock_user_by_id(session, user_id)
        if user is None:
            raise AccessError(AccessFailure.USER_MISSING)
        return user

    def _period_tariff(self, key: str) -> Tariff:
        try:
            tariff = self.service._resolve_tariff(key, "period")
        except (KeyError, ValueError) as exc:
            raise AccessError(AccessFailure.TARIFF_UNAVAILABLE) from exc
        if tariff is None:
            raise AccessError(AccessFailure.TARIFF_UNAVAILABLE)
        return tariff

    def _require_trial(self) -> None:
        settings = self.service.settings
        if not settings.TRIAL_ENABLED or settings.TRIAL_DURATION_DAYS <= 0:
            raise AccessError(AccessFailure.TRIAL_UNAVAILABLE)

    @staticmethod
    def _require_resolved_renewal(row: Subscription | None) -> None:
        if row is not None and AccessState.model_validate(row).auto_renew_enabled:
            # Q-03: require native cancellation, never cancel an external recurrence here.
            raise AccessError(AccessFailure.RENEWAL_POLICY_REQUIRED)

    async def _require_supported_quotas(
        self, session: AsyncSession, row: Subscription | None
    ) -> None:
        if row is None:
            return
        # Flexible quota windows override the tariff in the native worker. This host
        # revision has no DAL/service operation to retire them while retaining history.
        # Do not announce a tariff/trial change that its worker would undo.
        windows = await tariff_dal.list_flexible_traffic_limit_records_in_window(
            session,
            subscription_id=AccessState.model_validate(row).subscription_id,
            valid_from=datetime.now(UTC),
            valid_until=datetime.max.replace(tzinfo=UTC),
        )
        if windows:
            raise AccessError(AccessFailure.FLEXIBLE_LIMITS_UNSUPPORTED)

    def _managed_squads(self, row: Subscription | None) -> list[str]:
        managed, _ = self.service._managed_panel_squad_uuids_for_subscription(row)
        return list(dict.fromkeys([*managed, *subscription_tariff_managed_squad_uuids(row)]))

    async def _remove_obsolete_panel_overrides(
        self, session: AsyncSession, state: AccessState, squads: list[str]
    ) -> None:
        await squad_dal.deactivate_panel_internal_overrides_for_squads(
            session,
            user_id=state.user_id,
            panel_user_uuid=state.panel_user_uuid,
            squad_uuids=squads,
        )
