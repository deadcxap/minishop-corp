"""One durable six-hour sweep instead of per-member timers. Keep applied DDL append-only."""

from sqlalchemy import text
from sqlalchemy.engine import Connection


def upgrade(connection: Connection) -> None:
    connection.execute(text("DROP INDEX ext_minishop_corp_members_reconcile"))
    connection.execute(
        text("""
        ALTER TABLE ext_minishop_corp_memberships
        DROP COLUMN next_reconcile_at,
        ADD COLUMN scheduled_run_id UUID
    """)
    )
    connection.execute(
        text("""
        CREATE TABLE ext_minishop_corp_reconciliation_sweep (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            run_id UUID NOT NULL,
            next_run_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            started_at TIMESTAMPTZ,
            completed_at TIMESTAMPTZ,
            is_running BOOLEAN NOT NULL DEFAULT false,
            after_member_id UUID,
            lease_token UUID,
            lease_until TIMESTAMPTZ,
            CHECK (NOT is_running OR started_at IS NOT NULL),
            CHECK ((lease_token IS NULL AND lease_until IS NULL)
                OR (is_running AND lease_token IS NOT NULL AND lease_until IS NOT NULL))
        )
    """)
    )
    connection.execute(
        text("""
        INSERT INTO ext_minishop_corp_reconciliation_sweep(id, run_id)
        VALUES (1, '00000000-0000-0000-0000-000000000000')
    """)
    )
