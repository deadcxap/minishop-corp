"""Persist operation intent. Execution, leases and retries belong to S05/S06."""

from typing import Annotated, Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..contracts_types import AccountId, ContractError, Version
from ..integration.access_types import PeriodAccess, TrialAccess
from .schema import Contract, Membership, Operation, OperationKind, Revision


class PeriodTarget(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["period"] = "period"
    tariff_key: str = Field(min_length=1, max_length=128)
    ends_at: AwareDatetime
    external_squad_uuid: UUID

    def access(self) -> PeriodAccess:
        return PeriodAccess(self.tariff_key, self.ends_at, str(self.external_squad_uuid))


class TrialTarget(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["trial"] = "trial"
    starts_at: AwareDatetime
    ends_at: AwareDatetime

    @model_validator(mode="after")
    def valid_window(self) -> "TrialTarget":
        self.access()
        return self

    def access(self) -> TrialAccess:
        return TrialAccess(self.starts_at, self.ends_at)


class OperationDraft(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    contract_id: UUID
    contract_version: Version
    membership_id: UUID
    membership_generation: Version
    user_id: AccountId
    actor_user_id: AccountId
    request_id: UUID
    kind: OperationKind
    target: Annotated[PeriodTarget | TrialTarget, Field(discriminator="kind")]

    @model_validator(mode="after")
    def valid_target(self) -> "OperationDraft":
        if (self.kind in {"join", "reconcile"}) != isinstance(self.target, PeriodTarget):
            raise ValueError("Operation kind and access target do not match")
        return self


async def prepare_operation(session: AsyncSession, draft: OperationDraft) -> Operation:
    """Caller must authorize and lock Minishop user first; this function never commits.

    An identical request returns its original intent, even after completion. A new
    request must refer to the current terms and membership generation. Before a
    panel call the executor must lock and validate these again, not trust this row.
    """
    payload = draft.model_dump(mode="json")
    existing = await session.scalar(
        select(Operation).where(
            Operation.actor_user_id == draft.actor_user_id, Operation.request_id == draft.request_id
        )
    )
    if existing is not None:
        if OperationDraft.model_validate(existing, from_attributes=True) != draft:
            raise ContractError("minishop_corp_request_conflict", 409)
        return existing
    contract = await session.scalar(
        select(Contract)
        .where(Contract.id == draft.contract_id)
        .execution_options(populate_existing=True)
        .with_for_update()
    )
    member = await session.scalar(
        select(Membership)
        .where(Membership.id == draft.membership_id)
        .execution_options(populate_existing=True)
        .with_for_update()
    )
    if (
        contract is None
        or contract.version != draft.contract_version
        or member is None
        or member.contract_id != draft.contract_id
        or member.current_user_id != draft.user_id
        or member.generation != draft.membership_generation
    ):
        raise ContractError("minishop_corp_operation_stale", 409)
    if isinstance(draft.target, PeriodTarget):
        revision = await session.get(Revision, (draft.contract_id, draft.contract_version))
        if revision is None or draft.target.access() != PeriodAccess(
            revision.tariff_key, revision.ends_at, str(revision.external_squad_uuid)
        ):
            raise ContractError("minishop_corp_operation_stale", 409)
    operation = Operation(
        contract_id=draft.contract_id,
        contract_version=draft.contract_version,
        membership_id=draft.membership_id,
        membership_generation=draft.membership_generation,
        user_id=draft.user_id,
        actor_user_id=draft.actor_user_id,
        request_id=draft.request_id,
        kind=draft.kind,
        target=payload["target"],
    )
    session.add(operation)
    await session.flush()
    return operation
