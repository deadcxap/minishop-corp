"""Distinguish an explicit manager removal from native account deletion."""

from sqlalchemy import text
from sqlalchemy.engine import Connection

DDL = """CREATE OR REPLACE FUNCTION ext_minishop_corp_clear_deleted_manager() RETURNS trigger
    LANGUAGE plpgsql AS $$
    BEGIN
        -- Explicit edits are audited with their actor and revision by Contracts.
        -- The FK's ON DELETE SET NULL runs after the native account row is gone.
        IF EXISTS (SELECT 1 FROM users WHERE user_id = OLD.manager_user_id) THEN
            RETURN NEW;
        END IF;
        NEW.updated_at := clock_timestamp();
        INSERT INTO ext_minishop_corp_audit
            (contract_id, actor_user_id, action, details, created_at)
            VALUES (NEW.id, 0, 'contract_manager_deleted',
                jsonb_build_object('user_id', OLD.manager_user_id), NEW.updated_at);
        RETURN NEW;
    END $$"""


def upgrade(connection: Connection) -> None:
    connection.execute(text(DDL))
