import json
import logging
import re

import pytest
from aiohttp.test_utils import TestClient
from minishop_corp.integration.admin_options import AccountChoices, AccountQuery, AdminOptions
from minishop_corp.integration.contracts import ContractHost
from minishop_corp.memberships import Memberships
from minishop_corp.operation_worker import OperationWorker
from minishop_corp.storage.schema import Operation
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .conftest import USER_ID, Host
from .test_access import seed_personal
from .test_contracts import authorization
from .test_contracts import client as client
from .test_memberships import invitation

pytestmark = pytest.mark.asyncio(loop_scope="session")


def events(caplog: pytest.LogCaptureFixture) -> list[dict[str, object]]:
    return [
        json.loads(record.getMessage())
        for record in caplog.records
        if record.name == "minishop_corp"
    ]


async def test_lookup_diagnostic_matches_response_without_search_text(
    client: TestClient, host: Host, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="minishop_corp")
    query = "private-lookup@example.invalid"
    response = await client.get(
        "/api/admin/minishop-corp/options/accounts",
        params={"q": query},
        headers={**authorization(host), "X-Corp-Request-ID": "untrusted-input"},
    )
    identifier = response.headers["X-Corp-Request-ID"]
    assert re.fullmatch("[a-f0-9]{32}", identifier)
    assert (await response.json())["accounts"] == []
    rows = events(caplog)
    assert rows == [
        {
            "event": "account_lookup",
            "request_id": identifier,
            "query_kind": "email",
            "result": "not_found",
            "matched_user_id": None,
        }
    ]
    assert query not in str(rows) and "untrusted-input" not in str(rows)


async def test_unexpected_api_error_is_correlated_without_exception_payload(
    client: TestClient,
    host: Host,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    caplog.set_level(logging.INFO, logger="minishop_corp")
    secret = "CORP-PRIVATE-PAYLOAD-token@example.invalid"

    async def explode(
        self: AdminOptions, session: AsyncSession, query: AccountQuery
    ) -> AccountChoices:
        raise RuntimeError(secret)

    monkeypatch.setattr(AdminOptions, "accounts", explode)
    response = await client.get(
        "/api/admin/minishop-corp/options/accounts",
        params={"q": secret},
        headers=authorization(host),
    )
    assert response.status == 500
    assert await response.json() == {"ok": False, "error": "minishop_corp_internal_error"}
    rows = events(caplog)
    assert all(row["request_id"] == response.headers["X-Corp-Request-ID"] for row in rows)
    assert any(row["event"] == "api_failure" and row["exception"] == "RuntimeError" for row in rows)
    assert "explode" in str(rows) and secret not in str(rows)


@pytest.mark.parametrize("fails", [False, True])
async def test_worker_records_committed_success_or_retry_without_invitation(
    host: Host, caplog: pytest.LogCaptureFixture, fails: bool
) -> None:
    caplog.set_level(logging.INFO, logger="minishop_corp")
    await seed_personal(host, "paid")
    native = ContractHost(host.service)
    offer = await invitation(host.session)
    accepted = await Memberships(native).confirm(host.session, USER_ID, offer)
    assert accepted.operation is not None
    worker = OperationWorker(native)
    lease = await worker.claim(host.session, operation_id=accepted.operation.id)
    assert lease is not None
    await host.session.commit()
    sessions = async_sessionmaker(
        bind=host.session.bind, expire_on_commit=False, join_transaction_mode="create_savepoint"
    )
    host.panel.fail_updates = fails
    assert await worker.deliver(sessions, lease) is (not fails)
    row = await host.session.get(Operation, lease.operation_id, populate_existing=True)
    assert row is not None
    logged = [item for item in events(caplog) if item["event"] == "operation_result"]
    assert len(logged) == 1
    assert logged[0]["operation_id"] == str(row.id)
    assert logged[0]["state"] == row.state == ("retry" if fails else "succeeded")
    assert logged[0]["attempts"] == row.attempts == 1
    assert logged[0]["error_code"] == row.error_code
    assert offer.code.get_secret_value() not in str(logged) and str(lease.token) not in str(logged)
