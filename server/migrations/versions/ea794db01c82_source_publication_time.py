"""Keep original source publication time separate from library timestamps."""
from alembic import op
import sqlalchemy as sa

revision = 'ea794db01c82'
down_revision = 'd97b164ea031'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('items', sa.Column('source_published_at', sa.DateTime(), nullable=True))


def downgrade():
    op.drop_column('items', 'source_published_at')
