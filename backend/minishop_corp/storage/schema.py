"""Typed mappings; deployed DDL lives in immutable migrations, not create_all()."""

from datetime import datetime
from typing import Literal
from uuid import UUID, uuid4

from pydantic import JsonValue
from sqlalchemy import BigInteger, DateTime, Integer, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

PREFIX = "ext_minishop_corp_"
MembershipState = Literal["pending", "active", "leaving", "left", "excluded", "failed"]
OperationKind = Literal["join", "leave", "exclude", "reconcile"]
OperationState = Literal["pending", "running", "retry", "succeeded", "failed", "cancelled"]


class Base(DeclarativeBase):
    pass


class Contract(Base):
    __tablename__ = PREFIX + "contracts"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(String(200))
    tariff_key: Mapped[str] = mapped_column(String(128))
    external_squad_uuid: Mapped[UUID]
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    manager_user_id: Mapped[int] = mapped_column(BigInteger)
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Revision(Base):
    __tablename__ = PREFIX + "revisions"

    contract_id: Mapped[UUID] = mapped_column(primary_key=True)
    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    tariff_key: Mapped[str] = mapped_column(String(128))
    external_squad_uuid: Mapped[UUID]
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    manager_user_id: Mapped[int] = mapped_column(BigInteger)
    actor_user_id: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Invitation(Base):
    __tablename__ = PREFIX + "invitations"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    contract_id: Mapped[UUID]
    kind: Mapped[Literal["single", "reusable"]] = mapped_column(String(16))
    code_digest: Mapped[str] = mapped_column(String(64))
    use_limit: Mapped[int] = mapped_column(Integer)
    used_count: Mapped[int] = mapped_column(Integer, default=0)
    reserved_count: Mapped[int] = mapped_column(Integer, default=0)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[int] = mapped_column(BigInteger)
    replaces_id: Mapped[UUID | None]
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Membership(Base):
    __tablename__ = PREFIX + "memberships"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    contract_id: Mapped[UUID]
    user_id: Mapped[int] = mapped_column(BigInteger)
    # Historical identity remains after departure; only current links prevent account deletion.
    current_user_id: Mapped[int | None] = mapped_column(BigInteger)
    invitation_id: Mapped[UUID | None]
    state: Mapped[MembershipState] = mapped_column(String(16), default="pending")
    generation: Mapped[int] = mapped_column(Integer, default=1)
    applied_version: Mapped[int | None] = mapped_column(Integer)
    scheduled_version: Mapped[int | None] = mapped_column(Integer)
    scheduled_run_id: Mapped[UUID | None]
    last_reconciled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    joined_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Operation(Base):
    __tablename__ = PREFIX + "operations"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    contract_id: Mapped[UUID]
    contract_version: Mapped[int] = mapped_column(Integer)
    membership_id: Mapped[UUID]
    membership_generation: Mapped[int] = mapped_column(Integer)
    user_id: Mapped[int] = mapped_column(BigInteger)
    actor_user_id: Mapped[int] = mapped_column(BigInteger)
    request_id: Mapped[UUID]
    kind: Mapped[OperationKind] = mapped_column(String(16))
    target: Mapped[dict[str, JsonValue]] = mapped_column(JSONB)
    state: Mapped[OperationState] = mapped_column(String(16), default="pending")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    lease_token: Mapped[UUID | None]
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_code: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Reservation(Base):
    __tablename__ = PREFIX + "reservations"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    contract_id: Mapped[UUID]
    invitation_id: Mapped[UUID]
    operation_id: Mapped[UUID]
    state: Mapped[Literal["held", "consumed", "released"]] = mapped_column(
        String(16), default="held"
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AuditEvent(Base):
    __tablename__ = PREFIX + "audit"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    contract_id: Mapped[UUID]
    actor_user_id: Mapped[int] = mapped_column(BigInteger)
    action: Mapped[str] = mapped_column(String(64))
    membership_id: Mapped[UUID | None]
    operation_id: Mapped[UUID | None]
    details: Mapped[dict[str, JsonValue]] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ReconciliationSweep(Base):
    __tablename__ = PREFIX + "reconciliation_sweep"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[UUID]
    next_run_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    is_running: Mapped[bool]
    after_member_id: Mapped[UUID | None]
    lease_token: Mapped[UUID | None]
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
