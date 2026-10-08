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
                        "WHERE id = 'minishop-corp.0001_initial'"
                    )
                )
                == 1
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


if __name__ == "__main__":
    asyncio.run(main())
