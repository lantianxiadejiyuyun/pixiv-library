"""Bound author pages and job lookups without scanning the whole library."""
from alembic import op
import sqlalchemy as sa

revision = 'c75ef103b602'
down_revision = 'a613cb902f84'
branch_labels = None
depends_on = None


def upgrade():
    op.create_index('ix_items_author_recent', 'items', ['author', sa.text('created_at DESC'), 'id'])
    op.create_index('ix_author_feed_page', 'author_feed_entries',
                    ['feed_id', sa.text('generation DESC'), 'position', 'item_id'])
    op.create_index('ix_jobs_kind_recent', 'jobs', ['kind', sa.text('updated_at DESC'), sa.text('id DESC')])


def downgrade():
    op.drop_index('ix_jobs_kind_recent', table_name='jobs')
    op.drop_index('ix_author_feed_page', table_name='author_feed_entries')
    op.drop_index('ix_items_author_recent', table_name='items')
