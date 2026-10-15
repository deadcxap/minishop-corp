"""Immutable corporate audience selections; delivery remains owned by Minishop."""

from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from .contracts_types import ContractError
from .storage.schema import BroadcastAudience, Contract

PREFIX = "minishop-corp."
CONTRACT_PREFIX = PREFIX + "contract:"
SELECTION_PREFIX = PREFIX + "selection:"


class SelectBroadcastAudience(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    contract_ids: list[UUID] = Field(min_length=1, max_length=100)

    @field_validator("contract_ids")
    @classmethod
    def normalize_ids(cls, value: list[UUID]) -> list[UUID]:
        return sorted(set(value))


def contract_target(identifier: UUID) -> str:
    return CONTRACT_PREFIX + identifier.hex


def parse_target(target: str) -> tuple[str, UUID] | None:
    for prefix in (CONTRACT_PREFIX, SELECTION_PREFIX):
        if target.startswith(prefix):
            value = target.removeprefix(prefix)
            if len(value) == 32:
                try:
                    identifier = UUID(hex=value)
                except ValueError:
                    return None
                if identifier.hex == value:
                    return prefix, identifier
    return None


async def select_audience(session: AsyncSession, draft: SelectBroadcastAudience) -> str:
    # Lock in the same UUID order as contract mutations. A concurrent deletion
    # cannot turn a successful selection into a silently narrowed audience.
    ids = list(
        await session.scalars(
            select(Contract.id)
            .where(Contract.id.in_(draft.contract_ids))
            .order_by(Contract.id)
            .with_for_update(read=True)
        )
    )
    if ids != draft.contract_ids:
        raise ContractError("minishop_corp_broadcast_contract_missing", 404)
    encoded = [str(identifier) for identifier in ids]
    identifier = uuid5(NAMESPACE_URL, PREFIX + "broadcast/" + "/".join(encoded))
    await session.execute(
        insert(BroadcastAudience)
        .values(id=identifier, contract_ids=encoded)
        .on_conflict_do_nothing(index_elements=[BroadcastAudience.id])
    )
    return contract_target(ids[0]) if len(ids) == 1 else SELECTION_PREFIX + identifier.hex
