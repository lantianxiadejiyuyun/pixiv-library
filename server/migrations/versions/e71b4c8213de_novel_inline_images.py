"""Local novel illustrations with stable geometry and source marker keys."""
from alembic import op
import sqlalchemy as sa

revision = 'e71b4c8213de'
down_revision = 'd60a3bf7102c'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('assets', sa.Column('inline_key', sa.String(80), nullable=True))
    op.add_column('assets', sa.Column('width', sa.Integer(), nullable=True))
    op.add_column('assets', sa.Column('height', sa.Integer(), nullable=True))


def downgrade():
    op.drop_column('assets', 'height')
    op.drop_column('assets', 'width')
    op.drop_column('assets', 'inline_key')
