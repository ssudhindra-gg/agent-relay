"""PostgreSQL database setup and durable Agent Relay models.

The rest of the application talks to the models through :mod:`storage`.
Concurrency is coordinated with ordinary PostgreSQL transactions and row
locks: every writer that changes a task or its attempts first locks the task
row, and queue scans use ``FOR UPDATE SKIP LOCKED`` so concurrent claimers and
recovery passes step around each other instead of blocking.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Generator

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, create_engine, func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship, sessionmaker


DEFAULT_DATABASE_URL = "postgresql+psycopg://relay:relay@localhost:5432/relay"


def _database_url() -> str:
    url = os.getenv("RELAY_DATABASE_URL") or os.getenv("DATABASE_URL") or DEFAULT_DATABASE_URL
    # Accept the common libpq-style schemes and pin SQLAlchemy to psycopg 3,
    # the driver this project installs (bare "postgresql://" means psycopg2).
    for scheme in ("postgres://", "postgresql://"):
        if url.startswith(scheme):
            url = "postgresql+psycopg://" + url[len(scheme):]
    if not url.startswith("postgresql+psycopg://"):
        raise RuntimeError("RELAY_DATABASE_URL must be a PostgreSQL URL, e.g. " + DEFAULT_DATABASE_URL)
    return url


def positive_int(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        return default
    return value if value > 0 else default


DATABASE_URL = _database_url()
LEASE_SECONDS = positive_int("RELAY_LEASE_SECONDS", 60)
MAX_ATTEMPTS = positive_int("RELAY_MAX_ATTEMPTS", 5)
RECOVERY_INTERVAL_SECONDS = max(1, positive_int("RELAY_RECOVERY_INTERVAL_SECONDS", 5))
MAX_BODY_BYTES = positive_int("RELAY_MAX_BODY_BYTES", 256 * 1024)
DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 100


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def as_db_time(value: datetime) -> datetime:
    """Normalize to an aware UTC datetime for ``timestamptz`` columns."""

    return value.astimezone(timezone.utc)


def db_time(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def iso_time(value: datetime | None) -> str | None:
    value = db_time(value)
    if value is None:
        return None
    return value.isoformat(timespec="seconds").replace("+00:00", "Z")


class Base(DeclarativeBase):
    pass


class Agent(Base):
    __tablename__ = "agents"

    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    sent_tasks: Mapped[list[Task]] = relationship(
        "Task", foreign_keys="Task.sender_id", back_populates="sender", passive_deletes=True
    )
    received_tasks: Mapped[list[Task]] = relationship(
        "Task", foreign_keys="Task.recipient_id", back_populates="recipient", passive_deletes=True
    )


class Task(Base):
    __tablename__ = "tasks"
    __table_args__ = (UniqueConstraint("sender_id", "idempotency_key", name="uq_task_sender_idempotency"),)

    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    sender_id: Mapped[str] = mapped_column(String(100), ForeignKey("agents.id"), nullable=False, index=True)
    recipient_id: Mapped[str] = mapped_column(String(100), ForeignKey("agents.id"), nullable=False, index=True)
    input: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    output: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    idempotency_key: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    sender: Mapped[Agent] = relationship("Agent", foreign_keys=[sender_id], back_populates="sent_tasks")
    recipient: Mapped[Agent] = relationship("Agent", foreign_keys=[recipient_id], back_populates="received_tasks")
    attempts: Mapped[list[Attempt]] = relationship(
        "Attempt", back_populates="task", cascade="all, delete-orphan", order_by="Attempt.attempt_number"
    )


class Attempt(Base):
    __tablename__ = "attempts"
    __table_args__ = (UniqueConstraint("task_id", "attempt_number", name="uq_attempt_task_number"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(
        String(100), ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False, index=True
    )
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    worker_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    claim_token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    claimed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    lease_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    outcome: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    terminal_action: Mapped[str | None] = mapped_column(String(10), nullable=True)
    terminal_payload_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)

    task: Mapped[Task] = relationship("Task", back_populates="attempts")


engine: Engine = create_engine(DATABASE_URL, future=True, pool_pre_ping=True)

SessionLocal = sessionmaker(bind=engine, class_=Session, expire_on_commit=False, autoflush=True)


# Arbitrary constant identifying the schema-creation advisory lock.
_SCHEMA_LOCK_ID = 0x52454C4159  # "RELAY"


def init_db() -> None:
    # Several API replicas may start at once against an empty database;
    # without a lock their CREATE TABLE passes race and one of them fails.
    with engine.begin() as connection:
        connection.execute(select(func.pg_advisory_xact_lock(_SCHEMA_LOCK_ID)))
        Base.metadata.create_all(connection)


@contextmanager
def db_session() -> Generator[Session, None, None]:
    """One READ COMMITTED transaction, committed on success."""

    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def recover_expired_in_session(db: Session, now: datetime) -> int:
    """Expire active leases and requeue/fail their tasks within ``db``.

    Tasks being worked on by another transaction (a heartbeat, a terminal
    submission, or a concurrent recovery pass) are skipped rather than waited
    on; a later pass picks them up if their lease is still expired.
    """

    now_db = as_db_time(now)
    tasks = list(
        db.scalars(
            select(Task)
            .join(Attempt, Attempt.task_id == Task.id)
            .where(Attempt.outcome == "processing", Attempt.lease_expires_at <= now_db)
            .order_by(Attempt.lease_expires_at, Attempt.id)
            .with_for_update(of=Task, skip_locked=True)
            .execution_options(populate_existing=True)
        ).unique()
    )
    count = 0
    for task in tasks:
        # The task row is now locked; re-read its attempts so the decision
        # uses the latest committed state, not the scan's snapshot.
        expired = list(
            db.scalars(
                select(Attempt)
                .where(
                    Attempt.task_id == task.id,
                    Attempt.outcome == "processing",
                    Attempt.lease_expires_at <= now_db,
                )
                .execution_options(populate_existing=True)
            )
        )
        for attempt in expired:
            attempt.outcome = "expired"
            attempt.finished_at = now_db
            count += 1
        if expired and task.status == "processing":
            if task.attempt_count >= MAX_ATTEMPTS:
                task.status = "failed"
                task.error = "attempts_exhausted"
                task.output = None
                task.finished_at = now_db
            else:
                task.status = "queued"
                task.finished_at = None
    db.flush()
    return count


def recover_expired() -> int:
    """Run one recovery pass and return the number of expired attempts."""

    with db_session() as db:
        return recover_expired_in_session(db, utcnow())


__all__ = [
    "Agent",
    "Attempt",
    "Base",
    "DATABASE_URL",
    "DEFAULT_PAGE_SIZE",
    "LEASE_SECONDS",
    "MAX_ATTEMPTS",
    "MAX_BODY_BYTES",
    "MAX_PAGE_SIZE",
    "RECOVERY_INTERVAL_SECONDS",
    "Task",
    "as_db_time",
    "db_session",
    "db_time",
    "engine",
    "init_db",
    "iso_time",
    "recover_expired",
    "recover_expired_in_session",
    "utcnow",
]
