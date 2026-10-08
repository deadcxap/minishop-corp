"""Invitation credentials are write-once outputs; stored/public metadata has no secret."""

import hashlib
import re
import secrets
from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator

from .contracts_types import ContractError, ContractSummary

UseLimit = Annotated[int, Field(strict=True, gt=0, le=2**31 - 1)]


class CreateInvitation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: UUID
    kind: Literal["single", "reusable"]
    use_limit: UseLimit

    @model_validator(mode="after")
    def single_use(self) -> "CreateInvitation":
        if self.kind == "single" and self.use_limit != 1:
            raise ValueError("A single-use invitation has one use")
        return self


class RotateInvitation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: UUID


class InvitationInfo(BaseModel):
    model_config = ConfigDict(from_attributes=True, frozen=True)
    id: UUID
    contract_id: UUID
    kind: Literal["single", "reusable"]
    use_limit: int
    used_count: int
    reserved_count: int
    revoked_at: datetime | None
    created_at: datetime
    replaces_id: UUID | None


class IssuedInvitation(BaseModel):
    model_config = ConfigDict(frozen=True)
    invitation: InvitationInfo
    code: SecretStr | None
    created: bool


class InvitationOffer(BaseModel):
    model_config = ConfigDict(frozen=True)
    invitation_id: UUID
    contract: ContractSummary


class OfferReference(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    invitation_id: UUID
    contract_id: UUID
    contract_version: int = Field(strict=True, gt=0)


class PreviewInvitation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    # Format checks happen inside the attempt budget, including an empty string.
    code: SecretStr = Field(max_length=256)


class TariffOffer(BaseModel):
    model_config = ConfigDict(frozen=True)
    key: str
    names: dict[str, str]
    traffic_limit_bytes: int | None
    hwid_device_limit: int | None
    traffic_strategy: str


def new_code() -> SecretStr:
    return SecretStr("CORP-" + secrets.token_hex(16).upper())


def code_digest(code: SecretStr) -> str:
    normalized = code.get_secret_value().strip().upper().replace("-", "").replace(" ", "")
    if re.fullmatch(r"CORP[0-9A-F]{32}", normalized) is None:
        raise ContractError("minishop_corp_invitation_unavailable", 400)
    return hashlib.sha256(normalized.encode("ascii")).hexdigest()
