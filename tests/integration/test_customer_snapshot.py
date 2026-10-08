from uuid import uuid4

import pytest
from aiohttp.test_utils import TestClient
from bot.plugins.extensions import UserContext
from minishop_corp.integration.contracts import ContractHost
from minishop_corp.integration.views import view_policy
from minishop_corp.membership_types import DepartMembership
from minishop_corp.memberships import Memberships
from minishop_corp.storage.schema import Contract

from .conftest import USER_ID, Host
from .test_contracts import OTHER, USER_PATH, authorization
from .test_contracts import client as client
from .test_memberships import execute, joined

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def test_member_tariff_is_public_and_removed_tariff_does_not_block_exit(host: Host) -> None:
    await joined(host)
    service = Memberships(ContractHost(host.service))
    current = await service.current(host.session, USER_ID)
    assert current is not None and current.tariff is not None
    assert current.tariff.key == "corp" and current.can_leave
    assert "access_code" not in current.model_dump_json()
    host.settings.TARIFFS_CONFIG_PATH += ".missing"
    current = await service.current(host.session, USER_ID)
    assert current is not None and current.tariff is None and current.can_leave


async def test_manager_discovery_follows_current_assignment_and_needs_no_membership(
    client: TestClient, host: Host
) -> None:
    from .test_storage import seed_contract

    user = UserContext(host.session, USER_ID, "ru")
    other = UserContext(host.session, OTHER, "en")
    assert await view_policy(user, "corporate-card")
    assert await view_policy(user, "corporate-home")
    assert not await view_policy(user, "corporate-manager")
    row = await seed_contract(host.session, manager=USER_ID)
    assert await view_policy(user, "corporate-manager")
    assert not await view_policy(other, "corporate-manager")
    stored = await host.session.get(Contract, row.id)
    assert stored is not None
    stored.manager_user_id = OTHER
    await host.session.flush()
    assert not await view_policy(user, "corporate-manager")
    assert await view_policy(other, "corporate-manager")
    assert not await view_policy(other, "unknown-view")


@pytest.mark.parametrize("trial", [True, False])
async def test_departure_receipt_is_confirmed_persistent_and_scoped(
    client: TestClient, host: Host, trial: bool
) -> None:
    _, member_id = await joined(host)
    service = Memberships(ContractHost(host.service))
    result = await service.depart(
        host.session, USER_ID, member_id, DepartMembership(request_id=uuid4())
    )
    assert result.departure is None
    assert await service.last_departure(host.session, USER_ID) is None
    # The receipt follows the worker's final target, including Q-02 fallback.
    host.settings.TRIAL_ENABLED = trial
    assert await execute(host, result.id)
    await host.session.commit()
    response = await client.get(USER_PATH + "/membership", headers=authorization(host))
    assert response.status == 200
    data = await response.json()
    assert data["membership"] is None
    receipt = data["last_departure"]
    assert receipt["state"] == "succeeded"
    assert receipt["departure"]["kind"] == ("trial" if trial else "disabled")
    assert set(receipt["departure"]) == {"kind", "ends_at"}
    assert "target" not in receipt
    operation = await client.get(
        USER_PATH + f"/operations/{result.id}", headers=authorization(host)
    )
    assert (await operation.json())["operation"] == receipt
    other = await client.get(USER_PATH + "/membership", headers=authorization(host, OTHER))
    assert (await other.json())["last_departure"] is None
    assert (
        await client.get(USER_PATH + f"/operations/{result.id}", headers=authorization(host, OTHER))
    ).status == 404
