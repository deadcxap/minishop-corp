"""Exercise real HTTP installation and launcher restarts against a fresh local DB."""

import asyncio
import hashlib
import json
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import aiohttp
from bot.app.web.webapp_auth import create_webapp_session_token
from bot.plugins.packages import (
    _running_core_revision,
    managed_entry_points,
    package_root,
    read_state,
)
from bot.services.account_roles import grant_role
from config.settings import get_settings
from db.dal import user_dal
from db.dal.user_reads_dal import get_user_by_id
from db.database_setup import init_db_connection
from sqlalchemy import text

BASE = "http://127.0.0.1:8081"
ACTOR = 980001


async def ready() -> None:
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        state = read_state(package_root())
        expected = {"generation": state["generation"], "status": "active"}
        if all(
            state.get("observations", {}).get(role) == expected for role in ("backend", "worker")
        ):
            # Launcher observations precede application startup and native migrations.
            try:
                async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=2)) as client:
                    async with client.get(BASE + "/api/admin/plugins") as response:
                        if response.status == 401:
                            return
            except (aiohttp.ClientError, TimeoutError):
                pass
        await asyncio.sleep(0.5)
    raise RuntimeError("Both launcher roles must reach the selected generation")


async def main() -> None:
    pin = json.loads(await asyncio.to_thread(Path("/corp-dev/minishop.json").read_text))
    revision = await asyncio.to_thread(_running_core_revision)
    assert revision == pin["revision"], "Host must expose the pinned release commit"
    settings = get_settings()
    assert settings.POSTGRES_DB == "corp_dev" and settings.POSTGRES_HOST == "postgres"
    assert str(package_root()).startswith("/runtime/")
    await ready()
    assert not read_state(package_root())["installations"], "This check requires a fresh store"
    factory = init_db_connection(settings)
    async with factory.begin() as session:
        await user_dal.create_user(session, {"user_id": ACTOR, "first_name": "Package fixture"})
        await grant_role(session, ACTOR, "admin", source="corp_package_fixture")
    headers = {"Authorization": f"Bearer {create_webapp_session_token(settings, ACTOR)}"}
    async with aiohttp.ClientSession(
        headers=headers,
        timeout=aiohttp.ClientTimeout(total=15),
        # Each mutation can restart the server; never reuse a previous generation's socket.
        connector=aiohttp.TCPConnector(force_close=True),
    ) as http:

        async def post(path: str, body: object) -> dict[str, object]:
            async with http.post(BASE + path, json=body) as response:
                result: dict[str, object] = await response.json()
                assert response.status == 200 and result["ok"], (path, response.status, result)
                return result

        async def upload(path: str, body: bytes, expected: int = 200) -> dict[str, object]:
            data = aiohttp.FormData()
            data.add_field("file", body, filename="plugin.zip", content_type="application/zip")
            async with http.post(BASE + path, data=data) as response:
                result: dict[str, object] = await response.json()
                assert response.status == expected, (path, response.status, result)
                return result

        async def present(enabled: bool) -> None:
            async with http.get(BASE + "/api/admin/minishop-corp/contracts") as response:
                assert response.status == (200 if enabled else 404), await response.text()

        async def restart_post(path: str, body: dict[str, object], action: str) -> None:
            try:
                await post(path, body)
            except (aiohttp.ServerDisconnectedError, aiohttp.ClientPayloadError):
                # The launcher may stop the old backend after the mutation commits,
                # before its HTTP response arrives. Do not replay a mutation blindly.
                state = read_state(package_root())
                generation = body["generation"]
                assert isinstance(generation, int)
                assert state["generation"] == generation + 1, "Mutation was not committed"
                operation = state["operations"][-1]
                assert operation["actor"] == ACTOR and operation["plugin"] == "minishop-corp"
                assert operation["action"] == action
                if action == "install":
                    assert operation["id"] == body["operation_id"]
                    assert operation["digest"] == body["digest"]
                    assert operation["status"] == "completed"
                print(f"HTTP response lost during restart; native state confirms {action}")
            await ready()

        async def install(body: bytes) -> str:
            staged = await upload("/api/admin/plugins/stage", body)
            await restart_post(
                "/api/admin/plugins/install",
                {
                    "operation_id": staged["operation_id"],
                    "digest": staged["digest"],
                    "generation": read_state(package_root())["generation"],
                },
                "install",
            )
            assert (
                read_state(package_root())["installations"]["minishop-corp"]["digest"]
                == staged["digest"]
            )
            return str(staged["digest"])

        async def enabled(value: bool) -> None:
            await restart_post(
                "/api/admin/plugins/minishop-corp/enabled",
                {"enabled": value, "generation": read_state(package_root())["generation"]},
                "enable" if value else "disable",
            )
            assert read_state(package_root())["installations"]["minishop-corp"]["enabled"] is value
            await present(value)

        index = json.loads(
            await asyncio.to_thread(Path("/acceptance/release/minishop-plugin.json").read_bytes)
        )
        archive = await asyncio.to_thread(
            (Path("/acceptance/release") / index["artifact"]).read_bytes
        )
        upgrade_archive = await asyncio.to_thread(Path("/acceptance/upgrade.zip").read_bytes)
        assert hashlib.sha256(archive).hexdigest() == index["sha256"]
        preview = await upload("/api/admin/plugins/preview", archive)
        assert preview["trusted"] is False
        assert (await upload("/api/admin/plugins/stage", archive, 403))["ok"] is False
        manifest = preview["manifest"]
        assert isinstance(manifest, dict)
        await post(
            "/api/admin/plugins/trust",
            {
                "publisher": manifest["publisher"],
                "public_key": manifest["publisher_public_key"],
                "fingerprint": manifest["publisher_fingerprint"],
            },
        )
        original = await install(archive)
        await present(False)
        await enabled(True)
        print("PASS: clean HTTP install, verified publisher, explicit enable, both launcher roles")

        points = managed_entry_points(package_root())
        assert len(points) == 1
        loaded = points[0].load()
        assert loaded.version == index["version"]
        from minishop_corp.storage.schema import Contract, Invitation, Membership, Revision

        async with factory.begin() as session:
            contract = Contract(
                name="Synthetic retained organization",
                manager_user_id=ACTOR,
                tariff_key="corp",
                ends_at=datetime.now(UTC) + timedelta(days=30),
                external_squad_uuid=UUID("20000000-0000-4000-8000-000000000001"),
            )
            session.add(contract)
            await session.flush()
            session.add(
                Revision(
                    contract_id=contract.id,
                    version=1,
                    name=contract.name,
                    manager_user_id=ACTOR,
                    actor_user_id=ACTOR,
                    tariff_key=contract.tariff_key,
                    ends_at=contract.ends_at,
                    external_squad_uuid=contract.external_squad_uuid,
                )
            )
            session.add(
                Invitation(
                    contract_id=contract.id,
                    kind="reusable",
                    use_limit=10,
                    code_digest=hashlib.sha256(uuid4().bytes).hexdigest(),
                    created_by=ACTOR,
                )
            )
            await session.flush()  # Membership's applied version references the stored revision.
            session.add(
                Membership(
                    contract_id=contract.id,
                    user_id=ACTOR,
                    current_user_id=None,
                    state="left",
                    ended_at=datetime.now(UTC),
                    applied_version=1,
                )
            )

        async def snapshot() -> list[list[dict[str, object]]]:
            async with factory() as session:
                tables = ["contracts", "revisions", "invitations", "memberships", "audit"]
                rows = [
                    [
                        dict(row)
                        for row in (
                            await session.execute(text(f"SELECT * FROM ext_minishop_corp_{table}"))
                        ).mappings()
                    ]
                    for table in tables
                ]
                assert (
                    await session.scalar(
                        text(
                            "SELECT count(*) FROM schema_migrations WHERE id LIKE 'minishop-corp.%'"
                        )
                    )
                ) == 8
                return rows

        before = await snapshot()
        await enabled(False)
        assert await snapshot() == before
        await enabled(True)
        assert await snapshot() == before
        print("PASS: disable/enable removes/restores routes and preserves contracts and history")

        # A stopped plugin has no Python callback to clean up accounts. The
        # installed schema must handle native deletion and survive re-enabling.
        await enabled(False)
        deleted_user = ACTOR + 1
        async with factory.begin() as session:
            await user_dal.create_user(session, {"user_id": deleted_user})
            detached = Contract(
                name="Synthetic deleted manager",
                manager_user_id=deleted_user,
                tariff_key="corp",
                ends_at=datetime.now(UTC) + timedelta(days=30),
                external_squad_uuid=None,
            )
            session.add(detached)
            await session.flush()
            session.add(
                Revision(
                    contract_id=detached.id,
                    version=1,
                    name=detached.name,
                    manager_user_id=deleted_user,
                    actor_user_id=ACTOR,
                    tariff_key=detached.tariff_key,
                    ends_at=detached.ends_at,
                    external_squad_uuid=detached.external_squad_uuid,
                )
            )
            deleted_member = Membership(
                contract_id=detached.id, user_id=deleted_user, current_user_id=deleted_user
            )
            session.add(deleted_member)
            await session.flush()
            detached_id, member_id = detached.id, deleted_member.id
        async with http.delete(BASE + f"/api/admin/users/{deleted_user}") as response:
            assert response.status == 200, await response.text()
            assert (await response.json())["ok"] is True
        async with factory() as session:
            assert await get_user_by_id(session, deleted_user) is None
            detached_after = await session.get(Contract, detached_id)
            member_after = await session.get(Membership, member_id)
            assert detached_after is not None and detached_after.manager_user_id is None
            assert member_after is not None and member_after.state == "deleted"
        before = await snapshot()
        await enabled(True)
        assert await snapshot() == before
        async with http.get(BASE + f"/api/admin/minishop-corp/contracts/{detached_id}") as response:
            assert response.status == 200, await response.text()
            detached_payload = (await response.json())["contract"]
            assert detached_payload["manager_user_id"] is None
            assert detached_payload["external_squad_uuid"] is None
        print("PASS: native account deletion while disabled closes membership and clears manager")

        upgraded = await install(upgrade_archive)
        assert upgraded != original
        await present(True)
        assert await snapshot() == before
        # Reinstalling identical signed bytes and returning to the candidate are explicit tests.
        assert await install(upgrade_archive) == upgraded
        assert await snapshot() == before
        assert await install(archive) == original
        await present(True)
        assert await snapshot() == before
        print("PASS: synthetic version upgrade, repeat install and candidate restore preserve data")
        await enabled(False)
        await restart_post(
            "/api/admin/plugins/minishop-corp/remove",
            {"generation": read_state(package_root())["generation"]},
            "remove",
        )
        assert not read_state(package_root())["installations"]
        assert await snapshot() == before
        assert await install(archive) == original
        await enabled(True)
        assert await snapshot() == before
        print("PASS: reinstall reuses all eight migrations without data loss")


if __name__ == "__main__":
    asyncio.run(main())
