"""Host migration registration. Never derive deployed DDL from mutable ORM mappings."""

from db.migrator.engine import Migration as Migration
from sqlalchemy import text
from sqlalchemy.engine import Connection

from .migration_0002 import upgrade as invitation_replacements

# Fixed, namespaced schema; all constraints/indexes belong to plugin tables.
INITIAL_DDL = (
    """CREATE TABLE ext_minishop_corp_contracts (
        id UUID PRIMARY KEY,
        name VARCHAR(200) NOT NULL CHECK (length(trim(name)) > 0),
        tariff_key VARCHAR(128) NOT NULL CHECK (length(trim(tariff_key)) > 0),
        external_squad_uuid UUID NOT NULL,
        ends_at TIMESTAMPTZ NOT NULL,
        manager_user_id BIGINT NOT NULL REFERENCES users(user_id) ON DELETE RESTRICT,
        version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )""",
    """CREATE INDEX ext_minishop_corp_contracts_manager
        ON ext_minishop_corp_contracts(manager_user_id, id)""",
    "CREATE INDEX ext_minishop_corp_contracts_expiry ON ext_minishop_corp_contracts(ends_at, id)",
    """CREATE TABLE ext_minishop_corp_revisions (
        contract_id UUID NOT NULL REFERENCES ext_minishop_corp_contracts(id),
        version INTEGER NOT NULL CHECK (version > 0),
        name VARCHAR(200) NOT NULL,
        tariff_key VARCHAR(128) NOT NULL,
        external_squad_uuid UUID NOT NULL,
        ends_at TIMESTAMPTZ NOT NULL,
        manager_user_id BIGINT NOT NULL,
        actor_user_id BIGINT NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY (contract_id, version)
    )""",
    """ALTER TABLE ext_minishop_corp_contracts ADD CONSTRAINT ext_minishop_corp_current_revision
        FOREIGN KEY (id, version) REFERENCES ext_minishop_corp_revisions(contract_id, version)
        DEFERRABLE INITIALLY DEFERRED""",
    """CREATE TABLE ext_minishop_corp_invitations (
        id UUID PRIMARY KEY,
        contract_id UUID NOT NULL REFERENCES ext_minishop_corp_contracts(id),
        kind VARCHAR(16) NOT NULL CHECK (kind IN ('single', 'reusable')),
        code_digest VARCHAR(64) NOT NULL UNIQUE CHECK (code_digest ~ '^[0-9a-f]{64}$'),
        use_limit INTEGER NOT NULL CHECK (use_limit > 0),
        used_count INTEGER NOT NULL DEFAULT 0 CHECK (used_count >= 0),
        reserved_count INTEGER NOT NULL DEFAULT 0 CHECK (reserved_count >= 0),
        revoked_at TIMESTAMPTZ,
        created_by BIGINT NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        UNIQUE (id, contract_id),
        CHECK (kind <> 'single' OR use_limit = 1),
        CHECK (used_count + reserved_count <= use_limit)
    )""",
    """CREATE INDEX ext_minishop_corp_invitations_contract
        ON ext_minishop_corp_invitations(contract_id)""",
    """CREATE UNIQUE INDEX ext_minishop_corp_reusable_code
        ON ext_minishop_corp_invitations(contract_id)
        WHERE kind = 'reusable' AND revoked_at IS NULL""",
    """CREATE TABLE ext_minishop_corp_memberships (
        id UUID PRIMARY KEY,
        contract_id UUID NOT NULL REFERENCES ext_minishop_corp_contracts(id),
        user_id BIGINT NOT NULL,
        current_user_id BIGINT UNIQUE REFERENCES users(user_id) ON DELETE RESTRICT,
        invitation_id UUID,
        state VARCHAR(16) NOT NULL DEFAULT 'pending',
        generation INTEGER NOT NULL DEFAULT 1 CHECK (generation > 0),
        applied_version INTEGER,
        joined_at TIMESTAMPTZ,
        ended_at TIMESTAMPTZ,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        UNIQUE (id, contract_id, user_id),
        UNIQUE (id, contract_id),
        FOREIGN KEY (invitation_id, contract_id)
            REFERENCES ext_minishop_corp_invitations(id, contract_id),
        FOREIGN KEY (contract_id, applied_version)
            REFERENCES ext_minishop_corp_revisions(contract_id, version),
        CHECK ((state IN ('pending', 'active', 'leaving') AND current_user_id IS NOT NULL
                    AND current_user_id = user_id AND ended_at IS NULL)
            OR (state IN ('left', 'excluded', 'failed') AND current_user_id IS NULL
                AND ended_at IS NOT NULL))
    )""",
    """CREATE INDEX ext_minishop_corp_members_contract
        ON ext_minishop_corp_memberships(contract_id, state, id)""",
    """CREATE INDEX ext_minishop_corp_members_history
        ON ext_minishop_corp_memberships(user_id, created_at)""",
    """CREATE TABLE ext_minishop_corp_operations (
        id UUID PRIMARY KEY,
        contract_id UUID NOT NULL,
        contract_version INTEGER NOT NULL,
        membership_id UUID NOT NULL,
        membership_generation INTEGER NOT NULL CHECK (membership_generation > 0),
        user_id BIGINT NOT NULL,
        actor_user_id BIGINT NOT NULL,
        request_id UUID NOT NULL,
        kind VARCHAR(16) NOT NULL CHECK (kind IN ('join', 'leave', 'exclude', 'reconcile')),
        target JSONB NOT NULL CHECK (jsonb_typeof(target) = 'object'),
        state VARCHAR(16) NOT NULL DEFAULT 'pending'
            CHECK (state IN ('pending', 'running', 'retry', 'succeeded', 'failed', 'cancelled')),
        attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
        next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        lease_token UUID,
        lease_until TIMESTAMPTZ,
        error_code VARCHAR(100),
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        UNIQUE (actor_user_id, request_id),
        UNIQUE (id, contract_id),
        FOREIGN KEY (membership_id, contract_id, user_id)
            REFERENCES ext_minishop_corp_memberships(id, contract_id, user_id),
        FOREIGN KEY (contract_id, contract_version)
            REFERENCES ext_minishop_corp_revisions(contract_id, version),
        CHECK ((state = 'running' AND lease_token IS NOT NULL AND lease_until IS NOT NULL)
            OR (state <> 'running' AND lease_token IS NULL AND lease_until IS NULL))
    )""",
    """CREATE UNIQUE INDEX ext_minishop_corp_one_pending_operation
        ON ext_minishop_corp_operations(user_id) WHERE state IN ('pending', 'running', 'retry')""",
    """CREATE INDEX ext_minishop_corp_operations_queue
        ON ext_minishop_corp_operations(next_attempt_at, id) WHERE state IN ('pending', 'retry')""",
    """CREATE INDEX ext_minishop_corp_operations_leases
        ON ext_minishop_corp_operations(lease_until, id) WHERE state = 'running'""",
    """CREATE INDEX ext_minishop_corp_operations_contract
        ON ext_minishop_corp_operations(contract_id, created_at)""",
    """CREATE TABLE ext_minishop_corp_reservations (
        id UUID PRIMARY KEY,
        contract_id UUID NOT NULL,
        invitation_id UUID NOT NULL,
        operation_id UUID NOT NULL UNIQUE,
        state VARCHAR(16) NOT NULL DEFAULT 'held' CHECK (state IN ('held', 'consumed', 'released')),
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        finished_at TIMESTAMPTZ,
        FOREIGN KEY (invitation_id, contract_id)
            REFERENCES ext_minishop_corp_invitations(id, contract_id),
        FOREIGN KEY (operation_id, contract_id)
            REFERENCES ext_minishop_corp_operations(id, contract_id),
        CHECK ((state = 'held') = (finished_at IS NULL))
    )""",
    """CREATE INDEX ext_minishop_corp_reservations_invite
        ON ext_minishop_corp_reservations(invitation_id, state)""",
    """CREATE TABLE ext_minishop_corp_audit (
        id BIGSERIAL PRIMARY KEY,
        contract_id UUID NOT NULL REFERENCES ext_minishop_corp_contracts(id),
        actor_user_id BIGINT NOT NULL,
        action VARCHAR(64) NOT NULL,
        membership_id UUID,
        operation_id UUID,
        details JSONB NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(details) = 'object'),
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        FOREIGN KEY (membership_id, contract_id)
            REFERENCES ext_minishop_corp_memberships(id, contract_id),
        FOREIGN KEY (operation_id, contract_id)
            REFERENCES ext_minishop_corp_operations(id, contract_id)
    )""",
    "CREATE INDEX ext_minishop_corp_audit_contract ON ext_minishop_corp_audit(contract_id, id)",
)


def initial_schema(connection: Connection) -> None:
    for statement in INITIAL_DDL:
        connection.execute(text(statement))


def migrations() -> list[Migration]:
    return [
        Migration(
            id="minishop-corp.0001_initial",
            description="Corporate contracts, memberships, invitations, operations and audit",
            upgrade=initial_schema,
        ),
        Migration(
            id="minishop-corp.0002_invitation_replacements",
            description="Track invitation replacement and make rotation retries safe",
            upgrade=invitation_replacements,
        ),
    ]
