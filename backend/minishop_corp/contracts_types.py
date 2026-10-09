"""Validated contract inputs and public snapshots; never serialize ORM rows directly."""

from datetime import datetime
from typing import Annotated
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator

from .integration.access_types import panel_time

AccountId = Annotated[int, Field(strict=True, ge=-(2**63), le=2**63 - 1)]
Version = Annotated[int, Field(strict=True, gt=0)]


class ContractError(RuntimeError):
    def __init__(self, code: str, status: int = 400) -> None:
        self.code = code
        self.status = status
        super().__init__(code)


class ContractTerms(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, frozen=True)

    name: str = Field(min_length=1, max_length=200)
    tariff_key: str = Field(min_length=1, max_length=128)
    external_squad_uuid: UUID | None = None
    ends_at: AwareDatetime
    manager_user_id: AccountId | None

    @field_validator("ends_at")
    @classmethod
    def normalize_date(cls, value: datetime) -> datetime:
        return panel_time(value)

    @field_validator("manager_user_id")
    @classmethod
    def nonzero_account(cls, value: int | None) -> int | None:
        if value == 0:
            raise ValueError("An account id is required")
        return value


class CreateContract(ContractTerms):
    id: UUID
    manager_user_id: AccountId


class UpdateContract(ContractTerms):
    expected_version: Version


class ContractSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True, frozen=True)

    id: UUID
    name: str
    ends_at: datetime
    version: int
    expired: bool


class ContractDetails(ContractSummary):
    member_count: int
    tariff_key: str
    external_squad_uuid: UUID | None
    manager_user_id: int | None
    created_at: datetime
    updated_at: datetime


class ContractPage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    limit: int = Field(default=50, ge=1, le=100)
    after: UUID | None = None
