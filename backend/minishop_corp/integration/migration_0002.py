"""Append-only invitation replacement metadata; initial schema remains unchanged."""

from sqlalchemy import text
from sqlalchemy.engine import Connection


def upgrade(connection: Connection) -> None:
    connection.execute(
        text("""
        ALTER TABLE ext_minishop_corp_invitations ADD COLUMN replaces_id UUID,
        ADD CONSTRAINT ext_minishop_corp_invitation_replaces
            FOREIGN KEY (replaces_id, contract_id)
            REFERENCES ext_minishop_corp_invitations(id, contract_id),
        ADD CONSTRAINT ext_minishop_corp_invitation_not_self CHECK (replaces_id <> id)
    """)
    )
    connection.execute(
        text("""
        CREATE UNIQUE INDEX ext_minishop_corp_one_replacement
        ON ext_minishop_corp_invitations(replaces_id) WHERE replaces_id IS NOT NULL
    """)
    )
