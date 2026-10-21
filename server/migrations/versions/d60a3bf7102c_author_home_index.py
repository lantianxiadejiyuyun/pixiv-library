"""Read locally known author works while remote profiles are pending."""
from alembic import op
import sqlalchemy as sa

revision = 'd60a3bf7102c'
down_revision = 'c75ef103b602'
branch_labels = None
depends_on = None


def upgrade():
    op.create_index('ix_items_source_author_type', 'items',
                    ['source', 'author_source_id', 'type', sa.text('created_at DESC'), 'id'])


def downgrade():
    op.drop_index('ix_items_source_author_type', table_name='items')
