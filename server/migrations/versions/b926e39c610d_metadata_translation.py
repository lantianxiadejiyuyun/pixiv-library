"""Preserve original metadata alongside AI Chinese display names."""
from alembic import op
import sqlalchemy as sa

revision = "b926e39c610d"
down_revision = "a815d28b509c"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("items", sa.Column("title_zh", sa.String(512), nullable=True))
    op.add_column("items", sa.Column("title_zh_source", sa.String(512), nullable=True))
    op.add_column("tags", sa.Column("name_zh", sa.String(191), nullable=True))


def downgrade():
    op.drop_column("tags", "name_zh")
    op.drop_column("items", "title_zh_source")
    op.drop_column("items", "title_zh")
