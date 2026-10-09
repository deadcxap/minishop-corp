"""Persist a successful preview without granting another code guess."""

from sqlalchemy import text
from sqlalchemy.engine import Connection


def upgrade(connection: Connection) -> None:
    connection.execute(
        text("""CREATE TABLE ext_minishop_corp_previews (
            user_id BIGINT PRIMARY KEY REFERENCES users(user_id) ON DELETE CASCADE,
            code_digest VARCHAR(64) NOT NULL CHECK (code_digest ~ '^[0-9a-f]{64}$'),
            invitation_id UUID NOT NULL,
            contract_id UUID NOT NULL,
            contract_version INTEGER NOT NULL CHECK (contract_version > 0),
            expires_at TIMESTAMPTZ NOT NULL,
            FOREIGN KEY (invitation_id, contract_id)
                REFERENCES ext_minishop_corp_invitations(id, contract_id)
        )""")
    )
