from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from db.dal import auto_renew_dal, subscription_dal
from minishop_corp.integration.access import SOURCE
from minishop_corp.integration.access_types import panel_time
from minishop_corp.integration.migration_0012 import upgrade
from minishop_corp.storage.operations import TrialTarget
from minishop_corp.storage.schema import Membership, Operation

from .conftest import USER_ID, Host
from .test_storage import seed_contract

pytestmark = pytest.mark.asyncio(loop_scope="session")


@pytest.mark.parametrize(
    ("case", "expected"),
    [
        ("corporate", 1),
        ("departure-trial", 1),
        ("later-trial", 0),
        ("personal-trial", 0),
        ("personal-grant", 0),
        ("open-cycle", 0),
        ("previous-consent", 3),
        ("enabled", 0),
    ],
)
async def test_grant_opt_out_migration_is_scoped_and_transactional(
    host: Host, case: str, expected: int
) -> None:
    contract = await seed_contract(host.session)
    start = panel_time(datetime.now(UTC) - timedelta(minutes=5))
    end = start + timedelta(days=1)
    is_trial = case.endswith("trial")
    corporate = case in {"corporate", "open-cycle", "previous-consent", "enabled"}
    consent = 3 if case == "previous-consent" else 0
    enabled = case == "enabled"
    row = await subscription_dal.upsert_subscription(
        host.session,
        {
            "user_id": USER_ID,
            "panel_user_uuid": str(uuid4()),
            "panel_subscription_uuid": str(uuid4()),
            "provider": "trial" if is_trial else "admin",
            "tariff_key": "corp" if corporate else None,
            "tariff_binding_note": SOURCE if corporate else None,
            "start_date": start,
            "end_date": end,
            "is_active": True,
            "auto_renew_enabled": enabled,
            "auto_renew_consent_version": consent,
        },
    )
    if corporate:
        host.session.add(
            Membership(
                contract_id=contract.id, user_id=USER_ID, current_user_id=USER_ID, state="active"
            )
        )
    if case in {"departure-trial", "later-trial"}:
        member = Membership(
            contract_id=contract.id, user_id=USER_ID, state="excluded", ended_at=start
        )
        host.session.add(member)
        await host.session.flush()
        issued_at = start if case == "departure-trial" else start - timedelta(days=2)
        host.session.add(
            Operation(
                contract_id=contract.id,
                contract_version=contract.version,
                membership_id=member.id,
                membership_generation=member.generation,
                user_id=USER_ID,
                actor_user_id=USER_ID,
                request_id=uuid4(),
                kind="exclude",
                state="succeeded",
                target=TrialTarget(
                    starts_at=issued_at, ends_at=issued_at + timedelta(days=1)
                ).model_dump(mode="json"),
            )
        )
    if case == "open-cycle":
        cycle, created = await auto_renew_dal.create_or_get_cycle(
            host.session,
            {
                "subscription_id": row.subscription_id,
                "user_id": USER_ID,
                "provider": "user_balance",
                "cycle_anchor": end.date(),
                "renewal_cycle_end": end,
                "base_idempotence_key": uuid4().hex,
                "payment_method_provider_id": "synthetic-wallet",
                "request_snapshot": "{}",
            },
        )
        assert created
    await host.session.flush()

    connection = await host.session.connection()
    savepoint = await connection.begin_nested()
    await connection.run_sync(upgrade)
    await host.session.refresh(row)
    assert row.auto_renew_consent_version == expected
    await savepoint.rollback()
    await host.session.refresh(row)
    assert row.auto_renew_consent_version == consent

    await connection.run_sync(upgrade)
    await connection.run_sync(upgrade)
    await host.session.refresh(row)
    assert row.auto_renew_enabled == enabled and row.auto_renew_consent_version == expected
    assert row.start_date == start and row.end_date == end
    if case == "open-cycle":
        await host.session.refresh(cycle)
        assert cycle.state == "scheduled"
