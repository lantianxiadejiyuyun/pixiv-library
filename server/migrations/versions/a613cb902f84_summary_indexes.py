"""Aggregate disjoint account assignments using covering indexes."""
from alembic import op
import sqlalchemy as sa
from app.job_account_sql import ACCOUNT_FIELDS, unique_account_value

revision = 'a613cb902f84'
down_revision = '0c821d9e4a76'
branch_labels = None
depends_on = None


def upgrade():
    for field in ACCOUNT_FIELDS[1:]:
        name = 'stats_' + field
        index = 'ix_jobs_' + field.removesuffix('_account_id') + '_stats'
        op.add_column('jobs', sa.Column(name, sa.String(36), sa.Computed(unique_account_value(field), persisted=False)))
        op.drop_index(index, table_name='jobs')
        op.create_index(index, 'jobs', [name, 'status'])
    op.create_index('ix_item_tags_item', 'item_tags', ['item_id'])


def downgrade():
    op.drop_index('ix_item_tags_item', table_name='item_tags')
    for field in reversed(ACCOUNT_FIELDS[1:]):
        index = 'ix_jobs_' + field.removesuffix('_account_id') + '_stats'
        op.drop_index(index, table_name='jobs')
        op.create_index(index, 'jobs', [field, 'status'])
        op.drop_column('jobs', 'stats_' + field)
