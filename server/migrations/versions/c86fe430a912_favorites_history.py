"""Library favorites, groups, personal scores, history and upstream bookmarks."""
from datetime import datetime
from alembic import op
import sqlalchemy as sa

revision = "c86fe430a912"
down_revision = "b58ce962d713"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("items", sa.Column("source_bookmark_count", sa.BigInteger(), nullable=True))
    op.add_column("items", sa.Column("user_score", sa.Integer(), server_default="0", nullable=False))
    groups = op.create_table("favorite_groups",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(80), nullable=False, unique=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False))
    moment = datetime(2026, 10, 15)
    op.bulk_insert(groups, [{"id": f"57135c87-cd5f-4cbd-9e46-{i:012d}", "name": name,
        "created_at": moment, "updated_at": moment} for i, name in enumerate(("想看", "在看", "已看", "推荐"), 1)])
    op.create_table("item_favorites",
        sa.Column("item_id", sa.String(36), sa.ForeignKey("items.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("created_at", sa.DateTime(), nullable=False))
    op.create_table("favorite_group_items",
        sa.Column("group_id", sa.String(36), sa.ForeignKey("favorite_groups.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("item_id", sa.String(36), sa.ForeignKey("item_favorites.item_id", ondelete="CASCADE"), primary_key=True))
    op.create_index("ix_favorite_group_items_item_id", "favorite_group_items", ["item_id"])
    op.create_table("item_visits",
        sa.Column("item_id", sa.String(36), sa.ForeignKey("items.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("viewed_at", sa.DateTime(), nullable=False))
    op.create_index("ix_item_visits_viewed_at", "item_visits", ["viewed_at"])


def downgrade():
    op.drop_index("ix_item_visits_viewed_at", table_name="item_visits")
    op.drop_table("item_visits")
    op.drop_index("ix_favorite_group_items_item_id", table_name="favorite_group_items")
    op.drop_table("favorite_group_items")
    op.drop_table("item_favorites")
    op.drop_table("favorite_groups")
    op.drop_column("items", "user_score")
    op.drop_column("items", "source_bookmark_count")
