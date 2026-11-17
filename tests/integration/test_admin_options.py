from datetime import UTC, datetime
from uuid import UUID

import pytest
from aiohttp.test_utils import TestClient
from bot.services.account_roles import revoke_role
from db.dal import user_dal
from db.dal.user_email_dal import upsert_user_email_address
from minishop_corp.storage.schema import Membership

from .conftest import EXTERNAL_SQUAD, USER_ID, Host
from .test_contracts import ADMIN_PATH, MANAGER, MEMBER, OTHER, authorization, draft
from .test_contracts import client as client

pytestmark = pytest.mark.asyncio(loop_scope="session")
BASE = "/api/admin/minishop-corp/options/"


@pytest.mark.parametrize("path", ["context", "tariffs", "accounts", "squad?uuid=" + EXTERNAL_SQUAD])
async def test_choices_require_current_global_admin(
    client: TestClient, host: Host, path: str
) -> None:
    assert (await client.get(BASE + path)).status == 401
    assert (await client.get(BASE + path, headers=authorization(host, MANAGER))).status == 403
    assert not host.panel.requests
    await revoke_role(host.session, USER_ID, "admin", actor_user_id=USER_ID)
    assert (await client.get(BASE + path, headers=authorization(host))).status == 403
    assert not host.panel.requests


async def test_tariffs_include_hidden_without_access_secrets(
    client: TestClient, host: Host
) -> None:
    response = await client.get(BASE + "tariffs", headers=authorization(host))
    assert response.status == 200
    rows = (await response.json())["tariffs"]
    assert [(row["key"], row["hidden"]) for row in rows] == [("personal", False), ("corp", True)]
    assert "access_code" not in str(rows) and "0000000000000000" not in str(rows)
    assert response.headers["Cache-Control"] == "private, no-store"
    assert not host.panel.requests


async def test_squad_lookup_uses_native_cache_and_rejects_outage(
    client: TestClient, host: Host
) -> None:
    path = BASE + "squad?uuid=" + EXTERNAL_SQUAD
    first = await client.get(path, headers=authorization(host))
    assert (await first.json())["squad"] == {"uuid": EXTERNAL_SQUAD, "name": "Synthetic squad"}
    requests = list(host.panel.requests)
    assert (await client.get(path, headers=authorization(host))).status == 200
    assert host.panel.requests == requests
    host.panel.fail_squads = True
    response = await client.get(
        BASE + "squad?uuid=20000000-0000-4000-8000-000000000099", headers=authorization(host)
    )
    assert (
        response.status == 422
        and (await response.json())["error"] == "minishop_corp_squad_unavailable"
    )


async def test_account_selection_uses_native_ids_and_hides_banned_and_private_data(
    client: TestClient, host: Host
) -> None:
    await user_dal.update_user(
        host.session, MANAGER, {"username": "Fixture", "email": "secret@example.invalid"}
    )
    await user_dal.update_user(host.session, OTHER, {"is_banned": True})
    await upsert_user_email_address(
        host.session,
        user_id=MANAGER,
        email="alias@example.invalid",
        verified_at=datetime.now(UTC),
        source="corp_test",
    )
    for query in (
        "?q=@fixture",
        "?q=" + str(MANAGER),
        "?q=SECRET@example.invalid",
        "?q=alias@example.invalid",
    ):
        reply = await client.get(BASE + "accounts" + query, headers=authorization(host))
        assert reply.status == 200
        payload = await reply.json()
        identifiers = [row["user_id"] for row in payload["accounts"]]
        assert MANAGER in identifiers and OTHER not in identifiers
        assert "secret@example.invalid" not in str(payload)
        if query:
            assert identifiers == [MANAGER] and payload["next_page"] is None
    for query in ("", "   ", "@fix", "secret@", str(OTHER), str(2**100), "unknown"):
        response = await client.get(BASE + "accounts?q=" + query, headers=authorization(host))
        assert (await response.json())["accounts"] == []


async def test_account_search_uses_display_names_and_returns_all_matching_accounts(
    client: TestClient, host: Host
) -> None:
    await user_dal.update_user(
        host.session, MANAGER, {"first_name": "Анна Мария", "last_name": "Орлова"}
    )
    await user_dal.update_user(
        host.session,
        MEMBER,
        {"first_name": "Anna", "last_name": "Орлова-Ким", "username": "anna_id"},
    )
    await user_dal.update_user(
        host.session, OTHER, {"first_name": "Анна Мария", "last_name": "Орлова", "is_banned": True}
    )
    await user_dal.update_user(host.session, USER_ID, {"username": "anna"})
    for query, matches in (
        (" Анна ", [MANAGER]),
        ("ОРЛОВ", [MANAGER, MEMBER]),
        ("анна мария орлова", [MANAGER]),
        ("anna", [USER_ID, MEMBER]),
        ("@anna", [USER_ID]),
        ("@Анна", []),
        ("%", []),
        ("_", []),
    ):
        response = await client.get(
            BASE + "accounts", params={"q": query}, headers=authorization(host)
        )
        assert response.status == 200, await response.text()
        payload = await response.json()
        assert [row["user_id"] for row in payload["accounts"]] == matches
        assert payload["truncated"] is False and payload["next_page"] is None
    assert not host.panel.requests


async def test_broad_name_search_returns_a_bounded_list_and_reports_more_matches(
    client: TestClient, host: Host
) -> None:
    for identifier in range(MEMBER + 1, MEMBER + 27):
        await user_dal.create_user(host.session, {"user_id": identifier, "last_name": "Namesake"})
    response = await client.get(
        BASE + "accounts", params={"q": "namesake"}, headers=authorization(host)
    )
    assert response.status == 200, await response.text()
    payload = await response.json()
    assert [row["user_id"] for row in payload["accounts"]] == list(range(MEMBER + 1, MEMBER + 26))
    assert payload["truncated"] is True


@pytest.mark.parametrize(
    "path",
    [
        "tariffs?secret=1",
        "accounts?page=-1",
        "accounts?q=" + "x" * 321,
        "squad?uuid=bad",
        "accounts?q=fixture&user_id=1",
        "accounts?user_id=invalid",
    ],
)
async def test_choices_reject_invalid_queries(client: TestClient, host: Host, path: str) -> None:
    response = await client.get(BASE + path, headers=authorization(host))
    assert response.status == 400 and (await response.json())["ok"] is False


async def test_contract_counts_include_current_only_without_remote_reads(
    client: TestClient, host: Host
) -> None:
    created = await client.post(ADMIN_PATH, json=draft(), headers=authorization(host))
    contract = (await created.json())["contract"]
    assert contract["member_count"] == 0
    identifier = UUID(contract["id"])
    host.session.add_all(
        [
            Membership(contract_id=identifier, user_id=MEMBER, current_user_id=MEMBER),
            Membership(
                contract_id=identifier,
                user_id=OTHER,
                current_user_id=None,
                state="left",
                ended_at=datetime.now(UTC),
            ),
        ]
    )
    await host.session.flush()
    host.panel.requests.clear()
    for suffix in ("", "/" + str(identifier)):
        response = await client.get(ADMIN_PATH + suffix, headers=authorization(host))
        result = await response.json()
        rows = [result["contract"]] if suffix else result["contracts"]
        assert rows[0]["member_count"] == 1
    assert not host.panel.requests


async def test_draft_context_exposes_only_current_admin_identity(
    client: TestClient, host: Host
) -> None:
    response = await client.get(BASE + "context", headers=authorization(host))
    assert response.status == 200
    assert await response.json() == {"ok": True, "actor_user_id": USER_ID}
    assert response.headers["Cache-Control"] == "private, no-store"
    assert not host.panel.requests
    assert (await client.get(BASE + "context?user_id=1", headers=authorization(host))).status == 400


async def test_public_and_telegram_ids_resolve_to_the_canonical_account(
    client: TestClient, host: Host
) -> None:
    telegram_id = 770001234
    await user_dal.update_user(host.session, MANAGER, {"telegram_id": telegram_id})
    user = await user_dal.get_user_by_id(host.session, MANAGER)
    assert user is not None
    public_id = str(user.minishop_id)
    assert public_id.startswith("ms_") and telegram_id != MANAGER
    for selector in (
        {"q": public_id},
        {"q": public_id.upper()},
        {"q": str(telegram_id)},
        {"q": str(MANAGER)},
        {"user_id": str(MANAGER)},
    ):
        response = await client.get(BASE + "accounts", params=selector, headers=authorization(host))
        assert response.status == 200, await response.text()
        rows = (await response.json())["accounts"]
        assert len(rows) == 1 and rows[0]["user_id"] == MANAGER
        assert rows[0]["minishop_id"] == public_id and rows[0]["telegram_id"] == telegram_id
    for value in (public_id[:-1], "ms_invalid", str(telegram_id + 1)):
        response = await client.get(
            BASE + "accounts", params={"q": value}, headers=authorization(host)
        )
        assert (await response.json())["accounts"] == []
    await user_dal.update_user(host.session, MANAGER, {"is_banned": True})
    for value in (public_id, str(telegram_id)):
        response = await client.get(
            BASE + "accounts", params={"q": value}, headers=authorization(host)
        )
        assert (await response.json())["accounts"] == []
    assert not host.panel.requests


async def test_number_collision_requires_an_unambiguous_selector(
    client: TestClient, host: Host
) -> None:
    await user_dal.update_user(host.session, MANAGER, {"telegram_id": 770002345})
    await user_dal.update_user(host.session, OTHER, {"telegram_id": MANAGER})
    response = await client.get(
        BASE + "accounts", params={"q": str(MANAGER)}, headers=authorization(host)
    )
    assert response.status == 422
    assert (await response.json())["error"] == "minishop_corp_account_ambiguous"
    # Reopening a saved assignment uses an explicit internal-id selector.
    response = await client.get(
        BASE + "accounts", params={"user_id": str(MANAGER)}, headers=authorization(host)
    )
    assert [row["user_id"] for row in (await response.json())["accounts"]] == [MANAGER]
