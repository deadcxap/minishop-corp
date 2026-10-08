"""Typed inputs and snapshots at the boundary with the host's legacy ORM."""

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict


def panel_time(value: datetime) -> datetime:
    """The host serializes panel timestamps to milliseconds, always in UTC."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("An aware datetime is required")
    value = value.astimezone(UTC)
    return value.replace(microsecond=value.microsecond // 1000 * 1000)


@dataclass(frozen=True)
class PeriodAccess:
    tariff_key: str
    ends_at: datetime
    external_squad_uuid: str

    def __post_init__(self) -> None:
        if not self.tariff_key.strip() or not self.external_squad_uuid.strip():
            raise ValueError("Tariff and external squad are required")
        object.__setattr__(self, "ends_at", panel_time(self.ends_at))


@dataclass(frozen=True)
class TrialAccess:
    """Persist this window with the operation before attempting a departure."""

    starts_at: datetime
    ends_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "starts_at", panel_time(self.starts_at))
        object.__setattr__(self, "ends_at", panel_time(self.ends_at))
        if self.ends_at <= self.starts_at:
            raise ValueError("Trial end must follow its start")


class AccessFailure(StrEnum):
    USER_MISSING = "minishop_corp_user_missing"
    TARIFF_UNAVAILABLE = "minishop_corp_tariff_unavailable"
    PANEL_UNCONFIRMED = "minishop_corp_panel_unconfirmed"
    TRIAL_UNAVAILABLE = "minishop_corp_trial_unavailable"
    RENEWAL_POLICY_REQUIRED = "minishop_corp_renewal_policy_required"
    FLEXIBLE_LIMITS_UNSUPPORTED = "minishop_corp_flexible_limits_unsupported"
    SUBSCRIPTION_MISSING = "minishop_corp_subscription_missing"


class AccessError(RuntimeError):
    def __init__(self, code: AccessFailure) -> None:
        self.code = code
        super().__init__(code.value)


class AccessState(BaseModel):
    """Validate host ORM values instead of exporting its untyped attributes."""

    model_config = ConfigDict(from_attributes=True, frozen=True)

    subscription_id: int
    user_id: int
    panel_user_uuid: str
    panel_subscription_uuid: str | None
    start_date: datetime | None
    end_date: datetime
    is_active: bool
    provider: str | None
    tariff_key: str | None
    auto_renew_enabled: bool
    traffic_limit_bytes: int | None
    traffic_used_bytes: int | None
    hwid_device_limit: int | None
    topup_balance_bytes: int
    regular_bonus_bytes: int
    regular_unlimited_override: bool
    premium_topup_balance_bytes: int
    premium_topup_used_bytes: int
    premium_used_bytes: int
    premium_bonus_bytes: int
    premium_unlimited_override: bool
