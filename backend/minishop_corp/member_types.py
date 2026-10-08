"""Allowlisted member data. Unknown counters are distinct from an observed zero."""

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .contracts_types import ContractPage
from .membership_types import OperationInfo
from .storage.schema import MembershipState

Nonnegative = Annotated[int, Field(strict=True, ge=0)]
DataState = Literal["available", "stored", "stale", "unavailable"]


class Statistic(BaseModel):
    model_config = ConfigDict(frozen=True)
    value: Nonnegative | None = None
    state: DataState = "unavailable"

    @model_validator(mode="after")
    def known_value(self) -> "Statistic":
        if (self.value is None) != (self.state == "unavailable"):
            raise ValueError("Unavailable observations have no value")
        return self


class MemberStatistics(BaseModel):
    model_config = ConfigDict(frozen=True)
    traffic_used_bytes: Statistic = Statistic()
    traffic_limit_bytes: Statistic = Statistic()
    device_count: Statistic = Statistic()
    device_limit: Statistic = Statistic()


class MemberProfile(BaseModel):
    model_config = ConfigDict(frozen=True)
    user_id: int
    first_name: str | None
    last_name: str | None
    username: str | None
    telegram_url: str | None


class MemberInfo(BaseModel):
    model_config = ConfigDict(frozen=True)
    id: UUID
    state: MembershipState
    joined_at: datetime | None
    profile: MemberProfile
    statistics: MemberStatistics
    avatar_path: str | None
    operation: OperationInfo | None


class MemberPage(ContractPage):
    pass


class MembersPage(BaseModel):
    model_config = ConfigDict(frozen=True)
    members: list[MemberInfo]
    next_after: UUID | None


class MemberAvatar(BaseModel):
    model_config = ConfigDict(frozen=True)
    data_url: str | None = None
    state: Literal["available", "stale", "missing", "unavailable"] = "unavailable"
    updated_at: datetime | None = None
