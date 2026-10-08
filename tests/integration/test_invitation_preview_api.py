from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, tzinfo
from uuid import UUID

import pytest
from aiohttp.test_utils import TestClient
from bot.app.web.webapp_auth import create_webapp_session_token
from db.dal import user_dal
from minishop_corp import invitation_lookup
from minishop_corp.integration.code_attempts import FAILURE_SCOPE, MINUTE_SCOPE, CodeAttempts
from minishop_corp.invitations_types import code_digest
from minishop_corp.storage.schema import Contract, Invitation, Membership
from sqlalchemy import func, select

from .conftest import Host
from .test_contracts import MEMBER, OTHER, USER_PATH, authorization
from .test_contracts import client as client
from .test_invitation_lookup import code

pytestmark = pytest.mark.asyncio(loop_scope="session")
PATH = USER_PATH + "/invitations/preview"


@dataclass
class Clock:
    instant: datetime


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    clock = Clock(datetime.now(UTC))

    class ControlledDateTime(datetime):
        @classmethod
        def now(cls, tz: tzinfo | None = None) -> datetime:
            return clock.instant.astimezone(tz)

    monkeypatch.setattr(invitation_lookup, "datetime", ControlledDateTime)
    return clock


async def test_preview_by_regular_account_only_returns_public_offer(
    client: TestClient, host: Host, clock: Clock
) -> None:
    secret = await code(host)
    payload = {"code": secret.get_secret_value()}
    before = list(host.panel.requests)
    headers = authorization(host, MEMBER)
    response = await client.post(PATH, json=payload, headers=headers)
    assert response.status == 200, await response.text()
    assert response.headers["Cache-Control"] == "private, no-store"
    result = await response.json()
    assert result["ok"] is True and result["retry_after"] == 60
    assert set(result["offer"]["contract"]) == {"id", "name", "ends_at", "version", "expired"}
    assert result["tariff"]["traffic_limit_bytes"] == 20 * 1024**3
    assert result["tariff"]["hwid_device_limit"] == 5
    for private in ("access_code", "code_digest", "manager_user_id", secret.get_secret_value()):
        assert private not in await response.text()
    invitation = await host.session.scalar(
        select(Invitation).where(Invitation.code_digest == code_digest(secret))
    )
    assert invitation is not None and invitation.used_count == invitation.reserved_count == 0
    assert (
        await host.session.scalar(
            select(func.count()).select_from(Membership).where(Membership.user_id == MEMBER)
        )
        == 0
    )
    assert host.panel.requests == before
    denied = await client.post(PATH, json=payload, headers=headers)
    assert denied.status == 429 and denied.headers["Retry-After"] == "60"
    assert (await denied.json())["error"] == "minishop_corp_code_throttled"
    clock.instant += timedelta(minutes=1)
    assert (await client.post(PATH, json=payload, headers=headers)).status == 200


async def test_http_errors_persist_between_requests_and_accounts_have_separate_budgets(
    client: TestClient, host: Host, clock: Clock
) -> None:
    headers = authorization(host, MEMBER)
    for index, delay in enumerate((60, 60, 60, 60, 900, 900, 900, 900, 3600, 3600), start=1):
        # Empty/malformed codes are still guesses and consume the same budget.
        response = await client.post(PATH, json={"code": ""}, headers=headers)
        assert response.status == 400, await response.text()
        assert await response.json() == {
            "ok": False,
            "error": "minishop_corp_invitation_unavailable",
            "retry_after": delay,
        }
        denied = await client.post(PATH, json={"code": "another-guess"}, headers=headers)
        assert denied.status == 429 and denied.headers["Retry-After"] == str(delay)
        state = await CodeAttempts.state(host.session, FAILURE_SCOPE, MEMBER)
        assert state is not None and state.failures == index
        if index == 1:
            other = await client.post(
                PATH, json={"code": "unknown"}, headers=authorization(host, OTHER)
            )
            assert other.status == 400 and (await other.json())["retry_after"] == 60
        clock.instant += timedelta(seconds=delay)
    clock.instant += timedelta(hours=2)  # Exactly three hours since the last error.
    response = await client.post(PATH, json={"code": "unknown"}, headers=headers)
    assert response.status == 400 and (await response.json())["retry_after"] == 60
    state = await CodeAttempts.state(host.session, FAILURE_SCOPE, MEMBER)
    assert state is not None and state.failures == 1


async def test_native_authentication_ban_and_cookie_csrf_cover_preview(
    client: TestClient, host: Host
) -> None:
    payload = {"code": "unknown"}
    assert (await client.post(PATH, json=payload)).status == 401
    assert (
        await client.post(PATH, json=payload, headers=authorization(host, 999999))
    ).status == 403
    cookie = "rw_webapp_session=" + create_webapp_session_token(host.settings, MEMBER)
    cookie += "; rw_webapp_csrf=synthetic-csrf"
    origin = str(client.make_url("/")).rstrip("/")
    for headers in (
        {"Cookie": cookie, "Origin": origin},
        {"Cookie": cookie, "Origin": "https://foreign.invalid", "X-CSRF-Token": "synthetic-csrf"},
        {"Cookie": cookie, "Origin": origin, "X-CSRF-Token": "wrong"},
    ):
        response = await client.post(PATH, json=payload, headers=headers)
        assert response.status == 403 and (await response.json())["error"] == "csrf_failed"
    assert await CodeAttempts.state(host.session, MINUTE_SCOPE, MEMBER) is None
    valid = {"Cookie": cookie, "Origin": origin, "X-CSRF-Token": "synthetic-csrf"}
    response = await client.post(PATH, json=payload, headers=valid)
    assert response.status == 400
    assert (await response.json())["error"] == "minishop_corp_invitation_unavailable"
    await user_dal.update_user(host.session, MEMBER, {"is_banned": True})
    assert (
        await client.post(PATH, json=payload, headers=authorization(host, MEMBER))
    ).status == 403


async def test_invalid_body_never_exposes_input_or_looks_up_a_code(
    client: TestClient, host: Host
) -> None:
    headers = authorization(host, MEMBER)
    for payload in ({}, {"code": 123}, {"code": "x" * 257}, {"code": "x", "user_id": OTHER}):
        response = await client.post(PATH, json=payload, headers=headers)
        assert response.status == 400
        assert await response.json() == {"ok": False, "error": "minishop_corp_invalid_request"}
    for data, content_type, status in (
        ("{}", "text/plain", 400),
        ("[", "application/json", 400),
        (" " * 17000, "application/json", 413),
    ):
        response = await client.post(
            PATH, data=data, headers={**headers, "Content-Type": content_type}
        )
        assert response.status == status
        assert (await response.json())["error"] == "minishop_corp_invalid_request"
    assert await CodeAttempts.state(host.session, MINUTE_SCOPE, MEMBER) is None
    assert (await client.get(PATH, headers=headers)).status == 405


async def test_host_error_does_not_increase_wrong_code_counter(
    client: TestClient, host: Host, clock: Clock
) -> None:
    secret = await code(host)
    payload = {"code": secret.get_secret_value()}
    headers = authorization(host, MEMBER)
    response = await client.post(PATH, json=payload, headers=headers)
    contract_id = UUID((await response.json())["offer"]["contract"]["id"])
    contract = await host.session.get(Contract, contract_id)
    assert contract is not None
    contract.tariff_key = "removed-from-host-catalog"
    await host.session.flush()
    clock.instant += timedelta(minutes=1)
    response = await client.post(PATH, json=payload, headers=headers)
    assert response.status == 503
    assert (await response.json())["error"] == "minishop_corp_tariff_unavailable"
    assert await CodeAttempts.state(host.session, FAILURE_SCOPE, MEMBER) is None
    assert (await client.post(PATH, json=payload, headers=headers)).status == 429
