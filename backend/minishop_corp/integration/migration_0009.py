"""Retain new invitation values for authorized administration, keeping old hashes valid."""

from sqlalchemy import text
from sqlalchemy.engine import Connection


def upgrade(connection: Connection) -> None:
    connection.execute(
        text("""ALTER TABLE ext_minishop_corp_invitations
            ADD COLUMN code_value VARCHAR(37)
            CHECK (code_value IS NULL OR code_value ~ '^CORP-[0-9A-F]{32}$')""")
    )
