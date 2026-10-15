"""Persist encrypted network settings and account login verification.

Revision ID: b173c019267a
Revises: 97c016913066
"""
from alembic import op
import sqlalchemy as sa

revision = "b173c019267a"
down_revision = "97c016913066"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("source_accounts", sa.Column("verified_at", sa.DateTime(), nullable=True))
    op.create_table("network_settings",
        sa.Column("key", sa.String(64), nullable=False),
        sa.Column("value_encrypted", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("key"))


def downgrade():
    op.drop_table("network_settings")
    op.drop_column("source_accounts", "verified_at")
