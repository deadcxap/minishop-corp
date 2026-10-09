"""Keep native account deletion independent of plugin workers and preserve history."""

from sqlalchemy import text
from sqlalchemy.engine import Connection

DDL = (
    """ALTER TABLE ext_minishop_corp_contracts
        DROP CONSTRAINT ext_minishop_corp_contracts_manager_user_id_fkey,
        ALTER COLUMN manager_user_id DROP NOT NULL,
        ADD CONSTRAINT ext_minishop_corp_contracts_manager_user_id_fkey
            FOREIGN KEY (manager_user_id) REFERENCES users(user_id) ON DELETE SET NULL""",
    "ALTER TABLE ext_minishop_corp_revisions ALTER COLUMN manager_user_id DROP NOT NULL",
    """ALTER TABLE ext_minishop_corp_memberships
        DROP CONSTRAINT ext_minishop_corp_memberships_current_user_id_fkey,
        DROP CONSTRAINT ext_minishop_corp_memberships_check,
        ADD CONSTRAINT ext_minishop_corp_memberships_current_user_id_fkey
            FOREIGN KEY (current_user_id) REFERENCES users(user_id) ON DELETE SET NULL,
        ADD CONSTRAINT ext_minishop_corp_memberships_check
            CHECK ((state IN ('pending', 'active', 'leaving') AND current_user_id IS NOT NULL
                        AND current_user_id = user_id AND ended_at IS NULL)
                OR (state IN ('left', 'excluded', 'failed', 'deleted')
                    AND current_user_id IS NULL AND ended_at IS NOT NULL))""",
    """CREATE FUNCTION ext_minishop_corp_close_deleted_membership() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE
            instant TIMESTAMPTZ := clock_timestamp();
        BEGIN
            NEW.state := 'deleted';
            NEW.ended_at := instant;
            NEW.generation := OLD.generation + 1;

            -- The audit FK needs KEY SHARE on the contract. Acquire it before
            -- the invitation to avoid a cycle with another member's worker,
            -- which locks the contract before taking that shared invitation.
            PERFORM 1 FROM ext_minishop_corp_contracts
                WHERE id = NEW.contract_id FOR KEY SHARE;

            -- Native deletion already owns the user row. Lock shared invitations
            -- before operations/reservations, as ordinary membership delivery does.
            PERFORM 1 FROM ext_minishop_corp_invitations i
                WHERE i.id IN (
                    SELECT r.invitation_id FROM ext_minishop_corp_reservations r
                    JOIN ext_minishop_corp_operations o ON o.id = r.operation_id
                    WHERE o.membership_id = NEW.id AND r.state = 'held'
                ) ORDER BY i.id FOR UPDATE;

            UPDATE ext_minishop_corp_operations
                SET state = 'cancelled', lease_token = NULL, lease_until = NULL,
                    error_code = 'minishop_corp_account_deleted', updated_at = instant
                WHERE membership_id = NEW.id AND state IN ('pending', 'running', 'retry');

            WITH released AS (
                UPDATE ext_minishop_corp_reservations
                    SET state = 'released', finished_at = instant
                    WHERE state = 'held' AND operation_id IN (
                        SELECT id FROM ext_minishop_corp_operations WHERE membership_id = NEW.id
                    ) RETURNING invitation_id
            ), counts AS (
                SELECT invitation_id, count(*) AS total FROM released GROUP BY invitation_id
            )
            UPDATE ext_minishop_corp_invitations i
                SET reserved_count = i.reserved_count - counts.total
                FROM counts WHERE i.id = counts.invitation_id;

            INSERT INTO ext_minishop_corp_audit
                (contract_id, actor_user_id, action, membership_id, details, created_at)
                VALUES (NEW.contract_id, 0, 'membership_account_deleted', NEW.id,
                    jsonb_build_object('user_id', OLD.user_id, 'previous_state', OLD.state),
                    instant);
            RETURN NEW;
        END $$""",
    """CREATE TRIGGER ext_minishop_corp_membership_account_deleted
        BEFORE UPDATE OF current_user_id ON ext_minishop_corp_memberships
        FOR EACH ROW WHEN (OLD.current_user_id IS NOT NULL AND NEW.current_user_id IS NULL
            AND NEW.state IN ('pending', 'active', 'leaving'))
        EXECUTE FUNCTION ext_minishop_corp_close_deleted_membership()""",
    """CREATE FUNCTION ext_minishop_corp_clear_deleted_manager() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            NEW.updated_at := clock_timestamp();
            -- Access terms and their revision stay unchanged. Audit the detached
            -- role separately so deletion never schedules subscription rewrites.
            INSERT INTO ext_minishop_corp_audit
                (contract_id, actor_user_id, action, details, created_at)
                VALUES (NEW.id, 0, 'contract_manager_deleted',
                    jsonb_build_object('user_id', OLD.manager_user_id), NEW.updated_at);
            RETURN NEW;
        END $$""",
    """CREATE TRIGGER ext_minishop_corp_manager_account_deleted
        BEFORE UPDATE OF manager_user_id ON ext_minishop_corp_contracts
        FOR EACH ROW WHEN (OLD.manager_user_id IS NOT NULL AND NEW.manager_user_id IS NULL)
        EXECUTE FUNCTION ext_minishop_corp_clear_deleted_manager()""",
)


def upgrade(connection: Connection) -> None:
    for statement in DDL:
        connection.execute(text(statement))
