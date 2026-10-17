"""Persist account-scoped Pixiv series watchlists."""
from alembic import op
import sqlalchemy as sa

revision = "d97b164ea031"
down_revision = "c86fe430a912"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("subscriptions", sa.Column("download_works", sa.Boolean(), nullable=True))
    op.add_column("subscriptions", sa.Column("download_bookmarks", sa.Boolean(), nullable=True))
    op.create_table("pixiv_watchlist",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("account_id", sa.String(36), sa.ForeignKey("source_accounts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False), sa.Column("remote_id", sa.String(32), nullable=False),
        sa.Column("title", sa.String(512), nullable=False), sa.Column("author", sa.String(191), nullable=False),
        sa.Column("author_id", sa.String(32), nullable=False), sa.Column("content_count", sa.Integer(), nullable=False),
        sa.Column("latest_content_id", sa.String(32)), sa.Column("seen_content_id", sa.String(32)),
        sa.Column("remote_updated_at", sa.String(64)), sa.Column("present_remote", sa.Boolean(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False), sa.Column("auto_download", sa.Boolean(), nullable=False),
        sa.Column("series_id", sa.String(36), sa.ForeignKey("series.id", ondelete="SET NULL")),
        sa.Column("last_checked_at", sa.DateTime()), sa.Column("last_error", sa.Text()),
        sa.Column("created_at", sa.DateTime(), nullable=False), sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("account_id", "kind", "remote_id", name="uq_pixiv_watchlist_owner_series"))
    op.create_index("ix_pixiv_watchlist_account_id", "pixiv_watchlist", ["account_id"])


def downgrade():
    op.drop_table("pixiv_watchlist")
    op.drop_column("subscriptions", "download_bookmarks")
    op.drop_column("subscriptions", "download_works")
