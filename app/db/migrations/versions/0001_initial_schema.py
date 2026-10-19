"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-10-19 03:57:47.986726
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

import app.db.base

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "projects",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("created_at", app.db.base.UTCDateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_projects")),
    )
    op.create_table(
        "api_keys",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("key_hash", sa.String(length=64), nullable=False),
        sa.Column("prefix", sa.String(length=16), nullable=False),
        sa.Column("created_at", app.db.base.UTCDateTime(timezone=True), nullable=False),
        sa.Column("last_used_at", app.db.base.UTCDateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", app.db.base.UTCDateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name=op.f("fk_api_keys_project_id_projects"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_api_keys")),
        sa.UniqueConstraint("key_hash", name=op.f("uq_api_keys_key_hash")),
        sa.UniqueConstraint("prefix", name=op.f("uq_api_keys_prefix")),
    )
    with op.batch_alter_table("api_keys", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_api_keys_project_id"), ["project_id"], unique=False)

    op.create_table(
        "files",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("original_name", sa.String(length=512), nullable=False),
        sa.Column("content_type", sa.String(length=255), nullable=False),
        sa.Column("detected_type", sa.String(length=255), nullable=True),
        sa.Column("media_type", sa.String(length=16), nullable=False),
        sa.Column("size", sa.BigInteger(), nullable=False),
        sa.Column("storage_key", sa.String(length=1024), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("preset", sa.String(length=100), nullable=True),
        sa.Column("multipart_upload_id", sa.String(length=1024), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "metadata",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column("created_at", app.db.base.UTCDateTime(timezone=True), nullable=False),
        sa.Column("updated_at", app.db.base.UTCDateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name=op.f("fk_files_project_id_projects"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_files")),
    )
    with op.batch_alter_table("files", schema=None) as batch_op:
        batch_op.create_index(
            "ix_files_project_created", ["project_id", "created_at"], unique=False
        )
        batch_op.create_index("ix_files_project_status", ["project_id", "status"], unique=False)
        batch_op.create_index("ix_files_status_created", ["status", "created_at"], unique=False)

    op.create_table(
        "presets",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("media_type", sa.String(length=16), nullable=False),
        sa.Column(
            "config",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column("created_at", app.db.base.UTCDateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name=op.f("fk_presets_project_id_projects"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_presets")),
        sa.UniqueConstraint("project_id", "media_type", "name", name=op.f("uq_presets_project_id")),
    )
    with op.batch_alter_table("presets", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_presets_project_id"), ["project_id"], unique=False)

    op.create_table(
        "webhook_endpoints",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("url", sa.String(length=2048), nullable=False),
        sa.Column("secret", sa.String(length=128), nullable=False),
        sa.Column(
            "events",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("created_at", app.db.base.UTCDateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name=op.f("fk_webhook_endpoints_project_id_projects"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_webhook_endpoints")),
    )
    with op.batch_alter_table("webhook_endpoints", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_webhook_endpoints_project_id"), ["project_id"], unique=False
        )

    op.create_table(
        "jobs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("file_id", sa.Uuid(), nullable=False),
        sa.Column("type", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("progress", sa.Integer(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "config",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column("created_at", app.db.base.UTCDateTime(timezone=True), nullable=False),
        sa.Column("updated_at", app.db.base.UTCDateTime(timezone=True), nullable=False),
        sa.Column("started_at", app.db.base.UTCDateTime(timezone=True), nullable=True),
        sa.Column("finished_at", app.db.base.UTCDateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["file_id"], ["files.id"], name=op.f("fk_jobs_file_id_files"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_jobs")),
    )
    with op.batch_alter_table("jobs", schema=None) as batch_op:
        batch_op.create_index("ix_jobs_file_status", ["file_id", "status"], unique=False)

    op.create_table(
        "variants",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("file_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("content_type", sa.String(length=255), nullable=False),
        sa.Column("size", sa.BigInteger(), nullable=False),
        sa.Column("width", sa.Integer(), nullable=True),
        sa.Column("height", sa.Integer(), nullable=True),
        sa.Column("duration", sa.Float(), nullable=True),
        sa.Column("storage_key", sa.String(length=1024), nullable=False),
        sa.Column("created_at", app.db.base.UTCDateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["file_id"], ["files.id"], name=op.f("fk_variants_file_id_files"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_variants")),
        sa.UniqueConstraint("file_id", "name", name=op.f("uq_variants_file_id")),
    )
    op.create_table(
        "webhook_deliveries",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("endpoint_id", sa.Uuid(), nullable=False),
        sa.Column("event", sa.String(length=64), nullable=False),
        sa.Column(
            "payload",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column("status_code", sa.Integer(), nullable=True),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("success", sa.Boolean(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("created_at", app.db.base.UTCDateTime(timezone=True), nullable=False),
        sa.Column("delivered_at", app.db.base.UTCDateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["endpoint_id"],
            ["webhook_endpoints.id"],
            name=op.f("fk_webhook_deliveries_endpoint_id_webhook_endpoints"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_webhook_deliveries")),
    )
    with op.batch_alter_table("webhook_deliveries", schema=None) as batch_op:
        batch_op.create_index(
            "ix_webhook_deliveries_endpoint_created", ["endpoint_id", "created_at"], unique=False
        )


def downgrade() -> None:
    with op.batch_alter_table("webhook_deliveries", schema=None) as batch_op:
        batch_op.drop_index("ix_webhook_deliveries_endpoint_created")

    op.drop_table("webhook_deliveries")
    op.drop_table("variants")
    with op.batch_alter_table("jobs", schema=None) as batch_op:
        batch_op.drop_index("ix_jobs_file_status")

    op.drop_table("jobs")
    with op.batch_alter_table("webhook_endpoints", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_webhook_endpoints_project_id"))

    op.drop_table("webhook_endpoints")
    with op.batch_alter_table("presets", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_presets_project_id"))

    op.drop_table("presets")
    with op.batch_alter_table("files", schema=None) as batch_op:
        batch_op.drop_index("ix_files_status_created")
        batch_op.drop_index("ix_files_project_status")
        batch_op.drop_index("ix_files_project_created")

    op.drop_table("files")
    with op.batch_alter_table("api_keys", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_api_keys_project_id"))

    op.drop_table("api_keys")
    op.drop_table("projects")
