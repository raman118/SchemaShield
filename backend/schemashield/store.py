import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import JSON, DateTime, String, Text, create_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

from .config import Settings


def now() -> datetime:
    return datetime.now(UTC)


def new_id() -> str:
    return str(uuid.uuid4())


class Base(DeclarativeBase):
    pass


class Migration(Base):
    __tablename__ = "migrations"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    title: Mapped[str] = mapped_column(String(120))
    sql: Mapped[str] = mapped_column(Text)
    artifact_hash: Mapped[str] = mapped_column(String(64), index=True)
    analysis: Mapped[dict[str, Any]] = mapped_column(JSON)
    plan: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Job(Base):
    __tablename__ = "jobs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    migration_id: Mapped[str] = mapped_column(String(36), index=True)
    kind: Mapped[str] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(20), default="queued", index=True)
    artifact_hash: Mapped[str] = mapped_column(String(64))
    target: Mapped[str] = mapped_column(String(20), default="migration")
    report: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    cancel_requested: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class Approval(Base):
    __tablename__ = "approvals"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    migration_id: Mapped[str] = mapped_column(String(36), index=True)
    artifact_hash: Mapped[str] = mapped_column(String(64))
    report_id: Mapped[str] = mapped_column(String(36))
    reviewer: Mapped[str] = mapped_column(String(100))
    consumed: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Audit(Base):
    __tablename__ = "audit"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    event: Mapped[str] = mapped_column(String(80))
    subject_id: Mapped[str] = mapped_column(String(36), index=True)
    details: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


def create_store(settings: Settings):
    sqlite = settings.database_url.startswith("sqlite:")
    if sqlite and not settings.allow_sqlite:
        raise ValueError(
            "SQLite requires explicit allow_sqlite; it is a test/development store only"
        )
    engine = create_engine(
        settings.database_url,
        pool_pre_ping=True,
        **(
            {"connect_args": {"check_same_thread": False}}
            if sqlite
            else {"pool_size": 5, "max_overflow": 5}
        ),
    )
    if sqlite:
        Base.metadata.create_all(engine)
    return engine, sessionmaker(engine, expire_on_commit=False)
