"""Retain immutable corporate selections for native scheduled broadcasts."""

from sqlalchemy import text
from sqlalchemy.engine import Connection


def upgrade(connection: Connection) -> None:
    connection.execute(
        text("""CREATE TABLE IF NOT EXISTS ext_minishop_corp_broadcast_audiences (
            id UUID PRIMARY KEY,
            contract_ids JSONB NOT NULL CHECK (
                jsonb_typeof(contract_ids) = 'array'
                AND jsonb_array_length(contract_ids) BETWEEN 1 AND 100
            ),
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )""")
    )
