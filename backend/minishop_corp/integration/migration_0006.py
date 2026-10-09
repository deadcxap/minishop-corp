"""Allow contracts to inherit native external-squad settings without rewriting history."""

from sqlalchemy import text
from sqlalchemy.engine import Connection


def upgrade(connection: Connection) -> None:
    for table in ("contracts", "revisions"):
        connection.execute(
            text(
                f"ALTER TABLE ext_minishop_corp_{table} "
                "ALTER COLUMN external_squad_uuid DROP NOT NULL"
            )
        )
