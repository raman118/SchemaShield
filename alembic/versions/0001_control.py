"""Initial immutable analysis artifacts, jobs, approvals and audit."""

import sqlalchemy as sa

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "migrations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("title", sa.String(120), nullable=False),
        sa.Column("sql", sa.Text(), nullable=False),
        sa.Column("artifact_hash", sa.String(64), nullable=False),
        sa.Column("analysis", sa.JSON(), nullable=False),
        sa.Column("plan", sa.JSON()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_migrations_artifact_hash", "migrations", ["artifact_hash"])
    op.create_table(
        "jobs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("migration_id", sa.String(36), nullable=False),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("artifact_hash", sa.String(64), nullable=False),
        sa.Column("target", sa.String(20), nullable=False),
        sa.Column("report", sa.JSON()),
        sa.Column("cancel_requested", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_jobs_migration_id", "jobs", ["migration_id"])
    op.create_index("ix_jobs_status", "jobs", ["status"])
    op.create_table(
        "approvals",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("migration_id", sa.String(36), nullable=False),
        sa.Column("artifact_hash", sa.String(64), nullable=False),
        sa.Column("report_id", sa.String(36), nullable=False),
        sa.Column("reviewer", sa.String(100), nullable=False),
        sa.Column("consumed", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_approvals_migration_id", "approvals", ["migration_id"])
    op.create_table(
        "audit",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("event", sa.String(80), nullable=False),
        sa.Column("subject_id", sa.String(36), nullable=False),
        sa.Column("details", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_audit_subject_id", "audit", ["subject_id"])


def downgrade():
    raise RuntimeError(
        "Destructive control-store downgrade is prohibited; restore a verified backup manually"
    )
