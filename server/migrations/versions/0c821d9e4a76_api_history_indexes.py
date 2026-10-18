"""Bound history reads for task lists and account statistics."""
from alembic import op
import sqlalchemy as sa
from app.job_account_sql import ACCOUNT_FIELDS, account_value

revision = '0c821d9e4a76'
down_revision = 'fd271c408d3a'
branch_labels = None
depends_on = None


def upgrade():
    for field in ACCOUNT_FIELDS:
        op.add_column('jobs', sa.Column(field, sa.String(36), sa.Computed(account_value(sa.column('payload', sa.JSON()), field), persisted=False)))
        op.create_index('ix_jobs_' + field.removesuffix('_account_id') + '_stats', 'jobs', [field, 'status'])
    op.create_index('ix_jobs_lane_recent', 'jobs', ['queue_lane', sa.text('created_at DESC'), 'id'])
    op.create_index('ix_jobs_recent', 'jobs', [sa.text('created_at DESC'), 'id'])
    op.create_index('ix_assets_size', 'assets', ['size_bytes'])


def downgrade():
    op.drop_index('ix_assets_size', table_name='assets')
    op.drop_index('ix_jobs_recent', table_name='jobs')
    op.drop_index('ix_jobs_lane_recent', table_name='jobs')
    for field in reversed(ACCOUNT_FIELDS):
        op.drop_index('ix_jobs_' + field.removesuffix('_account_id') + '_stats', table_name='jobs')
        op.drop_column('jobs', field)
