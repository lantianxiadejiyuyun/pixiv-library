"""Separate preview covers from body assets and persist acquisition controls.

Revision ID: d581a32b410a
Revises: c240f019ae62
"""
from alembic import op
import sqlalchemy as sa

revision = "d581a32b410a"
down_revision = "c240f019ae62"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("items", sa.Column("preview_cover", sa.JSON(), nullable=True))
    op.add_column("items", sa.Column("content_state", sa.String(24), nullable=False, server_default="metadata"))
    op.add_column("items", sa.Column("content_error", sa.Text(), nullable=True))
    op.add_column("items", sa.Column("content_job_id", sa.String(36), nullable=True))
    op.create_index("ix_items_content_state", "items", ["content_state"])
    op.execute("UPDATE items SET content_state = 'ready' WHERE EXISTS (SELECT 1 FROM assets WHERE assets.item_id = items.id)")
    op.create_table("queue_control", sa.Column("id", sa.Integer(), primary_key=True),
                    sa.Column("paused", sa.Boolean(), nullable=False),
                    sa.Column("min_interval_seconds", sa.Float(), nullable=False),
                    sa.Column("max_interval_seconds", sa.Float(), nullable=False),
                    sa.Column("next_request_at", sa.DateTime(), nullable=True),
                    sa.Column("cover_streak", sa.Integer(), nullable=False),
                    sa.Column("created_at", sa.DateTime(), nullable=False),
                    sa.Column("updated_at", sa.DateTime(), nullable=False))


def downgrade():
    op.drop_table("queue_control")
    op.drop_index("ix_items_content_state", table_name="items")
    for column in ("content_job_id", "content_error", "content_state", "preview_cover"):
        op.drop_column("items", column)
