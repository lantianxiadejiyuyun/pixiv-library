"""Short-lived QR pairing and scoped revocable mobile device sessions.

Revision ID: a815d28b509c
Revises: f762c28e6431
"""
from alembic import op
import sqlalchemy as sa

revision = "a815d28b509c"
down_revision = "f762c28e6431"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("device_pairings",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("slot", sa.Integer(), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("consumed_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("slot"), sa.UniqueConstraint("token_hash"))
    op.create_table("device_sessions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("device_name", sa.String(191), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("last_used_at", sa.DateTime(), nullable=True),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("token_hash"))
    op.create_index("ix_device_sessions_expires_at", "device_sessions", ["expires_at"])


def downgrade():
    op.drop_index("ix_device_sessions_expires_at", table_name="device_sessions")
    op.drop_table("device_sessions")
    op.drop_table("device_pairings")
