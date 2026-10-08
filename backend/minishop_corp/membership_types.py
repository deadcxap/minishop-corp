"""Public membership intents and snapshots; never expose subscription credentials."""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from .contracts_types import ContractSummary
from .invitations_types import OfferReference, PreviewInvitation, TariffOffer
from .storage.operations import DisabledTarget, TrialTarget
from .storage.schema import MembershipState, Operation, OperationKind, OperationState


class ConfirmMembership(PreviewInvitation):
    request_id: UUID
    offer: OfferReference


class DepartMembership(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    request_id: UUID


class DepartureResult(BaseModel):
    model_config = ConfigDict(frozen=True)
    kind: Literal["trial", "disabled"]
    ends_at: datetime


class OperationInfo(BaseModel):
    model_config = ConfigDict(from_attributes=True, frozen=True)
    id: UUID
    membership_id: UUID
    kind: OperationKind
    state: OperationState
    attempts: int
    error_code: str | None
    next_attempt_at: datetime
    updated_at: datetime
    departure: DepartureResult | None = None


def operation_info(row: Operation) -> OperationInfo:
    result = OperationInfo.model_validate(row)
    if row.state == "succeeded" and row.kind in {"leave", "exclude"}:
        target = (
            TrialTarget.model_validate(row.target)
            if row.target.get("kind") == "trial"
            else DisabledTarget.model_validate(row.target)
        )
        result = result.model_copy(
            update={"departure": DepartureResult(kind=target.kind, ends_at=target.ends_at)}
        )
    return result


class MembershipInfo(BaseModel):
    model_config = ConfigDict(frozen=True)
    id: UUID
    state: MembershipState
    contract: ContractSummary
    tariff: TariffOffer | None
    joined_at: datetime | None
    can_leave: bool
    notice: str = "wa_minishop_corp_payment_notice"
    expiry_notice: str | None
    operation: OperationInfo | None


@dataclass(frozen=True)
class ConfirmationResult:
    status: int
    operation: OperationInfo | None = None
    error: str | None = None
    retry_after: int | None = None
