"""Exercise the actual host, database, signed UI assets and both launcher roles."""

import asyncio
import json
import time
from pathlib import Path

import aiohttp
from bot.app.web.webapp_auth import create_webapp_session_token
from bot.plugins.packages import package_root, read_state
from bot.services.account_roles import grant_role
from config.settings import get_settings
from db.dal import user_dal
from db.database_setup import init_db_connection
from sqlalchemy import text


async def main() -> None:
    settings = get_settings()
    assert settings.POSTGRES_HOST == "postgres" and settings.POSTGRES_DB == "corp_dev"
    base = "http://127.0.0.1:8081"
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as client:
        deadline = time.monotonic() + 90
        while True:
            try:
                async with client.get(f"{base}/api/plugins/minishop-corp/status") as response:
                    if response.status == 401:
                        break
            except aiohttp.ClientError:
                pass
            if time.monotonic() > deadline:
                raise RuntimeError("Backend did not become ready")
            await asyncio.sleep(1)

        deadline = time.monotonic() + 60
        while True:
            state = read_state(package_root())
            expected = {"generation": state["generation"], "status": "active"}
            if all(
                state.get("observations", {}).get(role) == expected
                for role in ("backend", "worker")
            ):
                break
            if time.monotonic() > deadline:
                raise RuntimeError("Both launcher roles must be active in the current generation")
            await asyncio.sleep(1)

        factory = init_db_connection(settings)
        async with factory() as session:
            for user_id, name, banned in (
                (910001, "Corp test member", False),
                (910002, "Corp test admin", False),
                (910003, "Corp test banned", True),
            ):
                await user_dal.create_user(
                    session,
                    {"user_id": user_id, "first_name": name, "is_banned": banned},
                    registered_via=None,
                )
            await grant_role(session, 910002, "admin", source="corp_dev_fixture")
            await session.commit()
            assert (
                await session.scalar(
                    text(
                        "SELECT count(*) FROM schema_migrations "
                        "WHERE id IN ('minishop-corp.0001_initial', "
                        "'minishop-corp.0002_invitation_replacements', "
                        "'minishop-corp.0003_membership_reconciliation', "
                        "'minishop-corp.0004_shared_reconciliation_sweep')"
                    )
                )
                == 4
            )

        async def get(path: str, user_id: int, expected: int = 200) -> dict[str, object]:
            headers = {"Authorization": f"Bearer {create_webapp_session_token(settings, user_id)}"}
            async with client.get(base + path, headers=headers) as response:
                assert response.status == expected, (path, response.status, await response.text())
                payload: dict[str, object] = await response.json()
                assert payload["ok"] is (expected == 200)
                return payload

        for user_id in (910001, 910002):
            assert (await get("/api/plugins/minishop-corp/status", user_id))["stage"] == "scaffold"
        await get("/api/plugins/minishop-corp/status", 910003, 403)
        await get("/api/plugins/minishop-corp/status", 919999, 403)
        await get("/api/admin/minishop-corp/status", 910001, 403)
        await get("/api/admin/minishop-corp/status", 910003, 403)
        await get("/api/admin/minishop-corp/status", 910002)
        await get("/api/admin/minishop-corp/contracts", 910001, 403)
        assert "contracts" in await get("/api/admin/minishop-corp/contracts", 910002)
        assert "contracts" in await get("/api/plugins/minishop-corp/managed-contracts", 910001)
        assert (await get("/api/plugins/minishop-corp/membership", 910001))["membership"] is None
        await get("/api/plugins/minishop-corp/membership", 910003, 403)
        await get(
            "/api/plugins/minishop-corp/operations/00000000-0000-4000-8000-000000000000",
            910001,
            404,
        )
        missing_contract = "00000000-0000-4000-8000-000000000000"
        for suffix in ("/synchronization", "/synchronization/operations"):
            path = f"/api/admin/minishop-corp/contracts/{missing_contract}{suffix}"
            await get(path, 910001, 403)
            await get(path, 910002, 404)
            await get(
                f"/api/plugins/minishop-corp/managed-contracts/{missing_contract}{suffix}",
                910001,
                404,
            )
        deadline = time.monotonic() + 15
        while True:
            async with factory() as session:
                started = await session.scalar(
                    text(
                        "SELECT started_at IS NOT NULL "
                        "AND next_run_at - started_at = interval '6 hours' "
                        "FROM ext_minishop_corp_reconciliation_sweep WHERE id = 1"
                    )
                )
            if started:
                break
            if time.monotonic() >= deadline:
                raise RuntimeError("Global reconciliation worker did not start its first sweep")
            await asyncio.sleep(1)

        preview_path = base + "/api/plugins/minishop-corp/invitations/preview"
        async with client.post(preview_path, json={"code": ""}) as response:
            assert response.status == 401
        headers = {"Authorization": f"Bearer {create_webapp_session_token(settings, 910001)}"}
        async with client.post(preview_path, json={"code": ""}, headers=headers) as response:
            # The stand keeps its DB across runs, including the real attempt budget.
            assert response.status in {400, 429}, await response.text()
            payload = await response.json()
            assert payload["ok"] is False and payload["retry_after"] > 0
        async with client.post(preview_path, json={"code": ""}, headers=headers) as response:
            assert response.status == 429
            assert int(response.headers["Retry-After"]) > 0

        customer = await get("/api/extensions/runtime", 910001)
        admin = await get("/api/admin/plugins/runtime", 910002)
        # The host must advertise the actual signed package on both UI surfaces.
        assert "minishop-corp" in json.dumps(customer)
        assert "minishop-corp" in json.dumps(admin)
        state = read_state(package_root())
        digest = state["installations"]["minishop-corp"]["digest"]
        for audience, user_id, prefix in (
            ("customer", 910001, "/api/extensions/assets"),
            ("admin", 910002, "/api/admin/plugins/assets"),
        ):
            headers = {"Authorization": f"Bearer {create_webapp_session_token(settings, user_id)}"}
            for suffix in ("js", "css"):
                path = f"{prefix}/minishop-corp/{digest}/{audience}/index.{suffix}"
                async with client.get(base + path, headers=headers) as response:
                    assert response.status == 200, (path, await response.text())
                    assert await response.read()

        # Save only synthetic development sessions for the optional local browser preview.
        await asyncio.to_thread(
            Path("/runtime/sessions.json").write_text,
            json.dumps(
                {
                    "customer": create_webapp_session_token(settings, 910001),
                    "admin": create_webapp_session_token(settings, 910002),
                }
            ),
        )
        print("PASS: customer/admin authorization, banned/missing users, runtime and signed assets")
        print("PASS: backend and worker are active in the same package generation")
        print("PASS: corporate migration applied and contract collection routes installed")
        print("PASS: invitation preview authenticates and persists the Q-01 attempt limit")
        print("PASS: membership and operation routes authenticate and preserve scoped responses")
        print("PASS: shared six-hour sweep started and synchronization routes enforce scope")


if __name__ == "__main__":
    asyncio.run(main())
