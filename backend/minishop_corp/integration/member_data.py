"""Read native records/caches; refresh missing statistics through existing Minishop services.

No subscription mutation, entitlement refresh or direct panel client belongs here.
Legacy host dictionaries are validated before they become public observations.
"""

import asyncio
import re
from dataclasses import dataclass
from typing import Literal
from weakref import WeakKeyDictionary

from bot.services.subscription_service import SubscriptionService
from db.models import Subscription, User, UserTelegramAvatar
from pydantic import AliasChoices, AliasPath, BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..member_types import MemberProfile, MemberStatistics, Nonnegative, Statistic

CONCURRENCY = 4
_limiters: WeakKeyDictionary[SubscriptionService, asyncio.Semaphore] = WeakKeyDictionary()


def limiter_for(service: SubscriptionService) -> asyncio.Semaphore:
    limiter = _limiters.get(service)
    if limiter is None:
        limiter = asyncio.Semaphore(CONCURRENCY)
        _limiters[service] = limiter
    return limiter


class ProfileRecord(BaseModel):
    model_config = ConfigDict(from_attributes=True, frozen=True)
    user_id: int
    first_name: str | None
    last_name: str | None
    username: str | None
    telegram_id: int | None
    panel_user_uuid: str | None

    def public(self) -> MemberProfile:
        link = None
        # Internal ids and usernames of accounts without Telegram are not Telegram identities.
        if self.telegram_id is not None and self.telegram_id > 0:
            if self.username and re.fullmatch(r"[A-Za-z0-9_]{1,32}", self.username):
                link = "https://t.me/" + self.username
            else:
                link = f"tg://user?id={self.telegram_id}"
        return MemberProfile(
            user_id=self.user_id,
            first_name=self.first_name,
            last_name=self.last_name,
            username=self.username,
            telegram_url=link,
        )


class StoredSubscription(BaseModel):
    model_config = ConfigDict(from_attributes=True, frozen=True)
    traffic_used_bytes: Nonnegative | None
    traffic_limit_bytes: Nonnegative | None
    hwid_device_limit: Nonnegative | None
    extra_hwid_devices: Nonnegative


class PanelStatistics(BaseModel):
    model_config = ConfigDict(frozen=True)
    uuid: str
    used: Nonnegative | None = Field(
        default=None,
        validation_alias=AliasChoices(
            AliasPath("userTraffic", "usedTrafficBytes"), "usedTrafficBytes"
        ),
    )
    limit: Nonnegative | None = Field(default=None, validation_alias="trafficLimitBytes")
    devices: Nonnegative | None = Field(default=None, validation_alias="hwidDeviceLimit")


@dataclass(frozen=True)
class MemberSource:
    profile: ProfileRecord
    subscription: StoredSubscription | None
    has_avatar: bool


def snapshot(value: object, reference: str) -> PanelStatistics | None:
    try:
        parsed = PanelStatistics.model_validate(value)
    except ValidationError:
        return None
    return parsed if parsed.uuid == reference else None


def count_devices(value: object) -> int | None:
    if isinstance(value, list) and all(isinstance(device, dict) for device in value):
        return len(value)
    return None


def observation(value: int | None, state: Literal["available", "stored", "stale"]) -> Statistic:
    return Statistic(value=value, state=state) if value is not None else Statistic()


def with_snapshot(
    values: tuple[Statistic, Statistic, Statistic],
    data: PanelStatistics | None,
    state: Literal["available", "stale"],
) -> tuple[Statistic, Statistic, Statistic]:
    if data is None:
        return values

    def choose(value: int | None, previous: Statistic) -> Statistic:
        if value is None or (state == "stale" and previous.value is not None):
            return previous
        return observation(value, state)

    return (
        choose(data.used, values[0]),
        choose(data.limit, values[1]),
        choose(data.devices, values[2]),
    )


class MemberData:
    def __init__(self, service: SubscriptionService) -> None:
        self.service = service
        self.limiter = limiter_for(service)

    async def sources(self, session: AsyncSession, user_ids: list[int]) -> dict[int, MemberSource]:
        if not user_ids:
            return {}
        if len(user_ids) > 100:
            raise ValueError("Member page exceeds the supported limit")
        users = await session.scalars(select(User).where(User.user_id.in_(user_ids)))
        profiles = {}
        for user in users:
            record = ProfileRecord.model_validate(user)
            profiles[record.user_id] = record
        avatars = set(
            await session.scalars(
                select(UserTelegramAvatar.user_id).where(UserTelegramAvatar.user_id.in_(user_ids))
            )
        )
        # The host has no batch DAL that includes inactive rows and detects ambiguous
        # history. A grouped read returns at most one row per requested account. If
        # several subscriptions share its canonical panel identity, use native cache
        # refresh instead of guessing that the longest historical subscription is current.
        rows = await session.execute(
            select(
                Subscription.user_id,
                func.count().label("rows"),
                func.max(Subscription.traffic_used_bytes).label("traffic_used_bytes"),
                func.max(Subscription.traffic_limit_bytes).label("traffic_limit_bytes"),
                func.max(Subscription.hwid_device_limit).label("hwid_device_limit"),
                func.max(Subscription.extra_hwid_devices).label("extra_hwid_devices"),
            )
            .join(User, User.user_id == Subscription.user_id)
            .where(User.user_id.in_(user_ids), Subscription.panel_user_uuid == User.panel_user_uuid)
            .group_by(Subscription.user_id)
        )
        subscriptions = {}
        for row in rows.mappings():
            if row["rows"] != 1:
                continue
            try:
                subscriptions[row["user_id"]] = StoredSubscription.model_validate(row)
            except ValidationError:
                # A malformed stored counter must not break other members' statistics.
                continue
        return {
            identifier: MemberSource(profile, subscriptions.get(identifier), identifier in avatars)
            for identifier, profile in profiles.items()
        }

    async def statistics(self, source: MemberSource, *, refresh: bool = True) -> MemberStatistics:
        reference = source.profile.panel_user_uuid
        if not reference:
            return MemberStatistics()
        async with self.limiter:
            return await self._statistics(source, reference, refresh=refresh)

    async def _statistics(
        self,
        source: MemberSource,
        reference: str,
        *,
        refresh: bool,
    ) -> MemberStatistics:
        panel = self.service.panel_service
        local = source.subscription
        used = observation(local.traffic_used_bytes if local else None, "stored")
        limit = observation(local.traffic_limit_bytes if local else None, "stored")
        device_limit = Statistic()
        if local is not None:
            base = local.hwid_device_limit
            if base is None:
                base = self.service.settings.USER_HWID_DEVICE_LIMIT
            device_limit = observation(
                self.service._effective_hwid_limit(base, local.extra_hwid_devices), "stored"
            )
        raw_cached: object = panel._users_cache.get_fresh(f"uuid:{reference}")
        cached = snapshot(raw_cached, reference)
        fresh = cached
        values = with_snapshot((used, limit, device_limit), cached, "available")
        stale = snapshot(panel._users_cache.get_stale(f"uuid:{reference}"), reference)
        if refresh and any(metric.value is None for metric in values):
            try:
                if raw_cached is not None:
                    # An incomplete/invalid fresh entry is still a miss for the
                    # requested values. Refresh it using the host's invalidation path.
                    await panel._invalidate_user_cache(reference)
                fresh = snapshot(await panel.get_user_by_uuid(reference), reference)
            except Exception:
                # Host failure is represented per value; never replace missing data with zero.
                fresh = None
        used, limit, device_limit = with_snapshot(
            with_snapshot((used, limit, device_limit), fresh, "available"), stale, "stale"
        )
        device_count = Statistic()
        devices: object = panel._devices_cache.get_fresh(f"user:{reference}")
        stale_devices: object = panel._devices_cache.get_stale(f"user:{reference}")
        if count_devices(devices) is None and refresh:
            try:
                if devices is not None:
                    await panel._invalidate_devices_cache(reference)
                devices = await panel.get_user_devices(reference)
            except Exception:
                devices = None
        device_state: Literal["available", "stale"] = "available"
        if count_devices(devices) is None:
            devices = stale_devices
            device_state = "stale"
        device_count = observation(count_devices(devices), device_state)
        return MemberStatistics(
            traffic_used_bytes=used,
            traffic_limit_bytes=limit,
            device_count=device_count,
            device_limit=device_limit,
        )
