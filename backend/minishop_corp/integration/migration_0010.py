"""Allow independent reusable codes and version invitation limit edits."""

from sqlalchemy import text
from sqlalchemy.engine import Connection


def upgrade(connection: Connection) -> None:
    connection.execute(text("DROP INDEX IF EXISTS ext_minishop_corp_reusable_code"))
    connection.execute(
        text("""ALTER TABLE ext_minishop_corp_invitations
        ADD COLUMN IF NOT EXISTS version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0)""")
    )
