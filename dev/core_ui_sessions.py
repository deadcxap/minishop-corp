"""Fresh synthetic identities keep UI cases independent of persisted rate limits."""

import asyncio
import json
from pathlib import Path
from uuid import uuid4

from bot.app.web.webapp_auth import create_webapp_session_token
from bot.plugins.packages import package_root
from bot.services.account_roles import grant_role
from config.settings import get_settings
from db.dal import user_dal
from db.database_setup import init_db_connection


async def main() -> None:
    settings = get_settings()
    assert settings.POSTGRES_HOST == "postgres" and settings.POSTGRES_DB == "corp_dev"
    assert str(package_root()).startswith("/runtime/")
    factory = init_db_connection(settings)
    sessions: dict[str, str] = {}
    async with factory.begin() as session:
        for audience in ("customer", "admin"):
            for language in ("ru", "en"):
                for width in (375, 1280):
                    user_id = 8_000_000_000 + uuid4().int % 1_000_000_000
                    await user_dal.create_user(
                        session,
                        {
                            "user_id": user_id,
                            "first_name": "Synthetic full shell fixture",
                            "language_code": language,
                        },
                    )
                    if audience == "admin":
                        await grant_role(session, user_id, "admin", source="corp_core_ui_fixture")
                    sessions[f"{audience}-{language}-{width}"] = create_webapp_session_token(
                        settings, user_id
                    )
    await asyncio.to_thread(Path("/runtime/core-ui-sessions.json").write_text, json.dumps(sessions))


if __name__ == "__main__":
    asyncio.run(main())
