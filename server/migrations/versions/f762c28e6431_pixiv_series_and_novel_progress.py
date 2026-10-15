"""Keep remote page metadata, ordered series and text reading positions.

Revision ID: f762c28e6431
Revises: d581a32b410a
"""
from alembic import op
import sqlalchemy as sa

revision = "f762c28e6431"
down_revision = "d581a32b410a"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("items", sa.Column("remote_page_count", sa.Integer(), nullable=True))
    op.add_column("items", sa.Column("source_series", sa.JSON(), nullable=True))
    op.add_column("subscriptions", sa.Column("content_scope", sa.String(16), nullable=False, server_default="images"))
    op.add_column("reading_progress", sa.Column("section", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("reading_progress", sa.Column("offset", sa.Float(), nullable=False, server_default="0"))
    op.create_table("series",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("origin", sa.String(16), nullable=False),
        sa.Column("source", sa.String(24), nullable=False),
        sa.Column("source_id", sa.String(255), nullable=True),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("title", sa.String(512), nullable=False),
        sa.Column("complete", sa.Boolean(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("source", "source_id", "kind", name="uq_series_source_identity"))
    op.create_table("series_entries",
        sa.Column("series_id", sa.String(36), sa.ForeignKey("series.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("item_id", sa.String(36), sa.ForeignKey("items.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("label", sa.String(512), nullable=False),
        sa.UniqueConstraint("series_id", "position", name="uq_series_entry_position"))


def downgrade():
    op.drop_table("series_entries")
    op.drop_table("series")
    op.drop_column("reading_progress", "offset")
    op.drop_column("reading_progress", "section")
    op.drop_column("subscriptions", "content_scope")
    op.drop_column("items", "source_series")
    op.drop_column("items", "remote_page_count")
