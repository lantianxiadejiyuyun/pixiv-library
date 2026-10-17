"""Index deterministic lanes and library ordering without changing payloads."""
from alembic import op
import sqlalchemy as sa
from app.queue_sql import lane_value

revision = 'fd271c408d3a'
down_revision = 'ea794db01c82'
branch_labels = None
depends_on = None


def upgrade():
    expression = lane_value(sa.column('kind', sa.String()), sa.column('payload', sa.JSON()))
    op.add_column('jobs', sa.Column('queue_lane', sa.String(24), sa.Computed(expression, persisted=False)))
    op.create_index('ix_jobs_lane_claim', 'jobs', ['queue_lane', 'status', 'created_at', 'id'])
    op.create_index('ix_item_source_recent', 'items', ['source', sa.text('created_at DESC'), 'id'])
    op.create_index('ix_item_order_recent', 'items', [sa.text('created_at DESC'), 'id'])


def downgrade():
    op.drop_index('ix_item_order_recent', table_name='items')
    op.drop_index('ix_item_source_recent', table_name='items')
    op.drop_index('ix_jobs_lane_claim', table_name='jobs')
    op.drop_column('jobs', 'queue_lane')
