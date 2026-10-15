"""Pixiv author home pages and paginated authored/bookmarked catalogues."""
from alembic import op
import sqlalchemy as sa

revision = "c037f40d721e"
down_revision = "b926e39c610d"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("items", sa.Column("author_source_id", sa.String(255), nullable=True))
    op.create_index("ix_items_author_source_id", "items", ["author_source_id"])
    op.create_table("author_profiles",
        sa.Column("remote_id", sa.String(20), primary_key=True),
        sa.Column("name", sa.String(512), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("avatar", sa.JSON(), nullable=True),
        sa.Column("statistics", sa.JSON(), nullable=True),
        sa.Column("synced_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False))
    op.create_table("author_feeds",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("scope_key", sa.String(64), nullable=False, unique=True),
        sa.Column("author_id", sa.String(20), sa.ForeignKey("author_profiles.remote_id", ondelete="CASCADE"), nullable=False),
        sa.Column("account_id", sa.String(36), sa.ForeignKey("source_accounts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("section", sa.String(16), nullable=False),
        sa.Column("category", sa.String(16), nullable=False),
        sa.Column("restrict", sa.String(16), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("next_cursor", sa.JSON(), nullable=True),
        sa.Column("seen_cursors", sa.JSON(), nullable=False),
        sa.Column("complete", sa.Boolean(), nullable=False),
        sa.Column("unavailable", sa.Integer(), nullable=False),
        sa.Column("raw_count", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False))
    op.create_index("ix_author_feeds_author_id", "author_feeds", ["author_id"])
    op.create_table("author_feed_entries",
        sa.Column("feed_id", sa.String(36), sa.ForeignKey("author_feeds.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("item_id", sa.String(36), sa.ForeignKey("items.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False))


def downgrade():
    op.drop_table("author_feed_entries")
    op.drop_index("ix_author_feeds_author_id", table_name="author_feeds")
    op.drop_table("author_feeds")
    op.drop_table("author_profiles")
    op.drop_index("ix_items_author_source_id", table_name="items")
    op.drop_column("items", "author_source_id")
