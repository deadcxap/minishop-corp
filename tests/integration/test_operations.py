from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from db.dal.user_dal import lock_user_by_id
from minishop_corp.contracts_types import ContractError
from minishop_corp.storage.operations import (
    OperationDraft,
    PeriodTarget,
    TrialTarget,
    prepare_operation,
)
from minishop_corp.storage.schema import Membership, Operation
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from .conftest import USER_ID, Host
from .test_storage import seed_contract

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def intent(host: Host, *, kind: str = "join") -> OperationDraft:
    await lock_user_by_id(host.session, USER_ID)
    contract = await seed_contract(host.session)
    member = Membership(contract_id=contract.id, user_id=USER_ID, current_user_id=USER_ID)
    host.session.add(member)
    await host.session.flush()
    target = (
        PeriodTarget(
            tariff_key=contract.tariff_key,
            ends_at=contract.ends_at,
            external_squad_uuid=contract.external_squad_uuid,
        )
        if kind == "join"
        else TrialTarget(starts_at=datetime.now(UTC), ends_at=datetime.now(UTC) + timedelta(days=3))
    )
    return OperationDraft.model_validate(
        {
            "contract_id": contract.id,
            "contract_version": 1,
            "membership_id": member.id,
            "membership_generation": 1,
            "user_id": USER_ID,
            "actor_user_id": USER_ID,
            "request_id": uuid4(),
            "kind": kind,
            "target": target,
        }
    )


@pytest.mark.parametrize("kind", ["join", "leave"])
async def test_durable_intent_retry_preserves_target(host: Host, kind: str) -> None:
    draft = await intent(host, kind=kind)
    first = await prepare_operation(host.session, draft)
    saved_id = first.id
    host.session.expunge(first)
    second = await prepare_operation(host.session, draft)
    assert second.id == saved_id
    assert OperationDraft.model_validate(second, from_attributes=True) == draft
    second.state = "succeeded"
    member = await host.session.get(Membership, draft.membership_id)
    assert member is not None
    member.state = "left"
    member.current_user_id = None
    member.ended_at = datetime.now(UTC)
    member.generation += 1
    await host.session.flush()
    assert (await prepare_operation(host.session, draft)).id == saved_id
    assert (
        await host.session.scalar(
            select(func.count()).select_from(Operation).where(Operation.user_id == USER_ID)
        )
        == 1
    )
    changed = draft.model_copy(update={"request_id": uuid4()})
    with pytest.raises(ContractError, match="operation_stale"):
        await prepare_operation(host.session, changed)


async def test_request_reuse_cannot_change_target(host: Host) -> None:
    draft = await intent(host, kind="leave")
    await prepare_operation(host.session, draft)
    assert isinstance(draft.target, TrialTarget)
    changed = draft.model_copy(
        update={
            "target": draft.target.model_copy(
                update={"ends_at": draft.target.ends_at + timedelta(days=1)}
            )
        }
    )
    with pytest.raises(ContractError, match="request_conflict"):
        await prepare_operation(host.session, changed)


@pytest.mark.parametrize(
    "field", ["contract_version", "membership_generation", "user_id", "target"]
)
async def test_stale_intent_is_not_enqueued(host: Host, field: str) -> None:
    draft = await intent(host)
    if field == "target":
        assert isinstance(draft.target, PeriodTarget)
        changed = draft.model_copy(
            update={
                "target": draft.target.model_copy(
                    update={"ends_at": draft.target.ends_at + timedelta(days=1)}
                )
            }
        )
    else:
        changed = draft.model_copy(update={field: 2})
    with pytest.raises(ContractError, match="operation_stale"):
        await prepare_operation(host.session, changed)


async def test_one_pending_operation_even_for_different_keys(host: Host) -> None:
    draft = await intent(host)
    await prepare_operation(host.session, draft)
    with pytest.raises(IntegrityError):
        async with host.session.begin_nested():
            await prepare_operation(host.session, draft.model_copy(update={"request_id": uuid4()}))
