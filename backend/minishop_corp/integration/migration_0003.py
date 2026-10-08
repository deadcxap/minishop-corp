"""Persist per-member reconciliation scheduling; earlier migrations stay immutable."""

from sqlalchemy import text
from sqlalchemy.engine import Connection


def upgrade(connection: Connection) -> None:
    connection.execute(
        text("""
        ALTER TABLE ext_minishop_corp_memberships
        ADD COLUMN scheduled_version INTEGER,
        ADD COLUMN next_reconcile_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        ADD COLUMN last_reconciled_at TIMESTAMPTZ,
        ADD CONSTRAINT ext_minishop_corp_scheduled_revision
            FOREIGN KEY (contract_id, scheduled_version)
            REFERENCES ext_minishop_corp_revisions(contract_id, version)
    """)
    )
    connection.execute(
        text("""
        CREATE INDEX ext_minishop_corp_members_reconcile
        ON ext_minishop_corp_memberships(next_reconcile_at, id)
        WHERE state = 'active' AND current_user_id IS NOT NULL
    """)
    )
