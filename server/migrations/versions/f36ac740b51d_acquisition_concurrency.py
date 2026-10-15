"""Persist the acquisition worker concurrency limit."""
from alembic import op
import sqlalchemy as sa

revision = "f36ac740b51d"
down_revision = "e259b62f943a"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("queue_control", sa.Column("concurrency", sa.Integer(), nullable=False, server_default="1"))
    op.add_column("queue_control", sa.Column("download_account_cursor", sa.Integer(), nullable=False, server_default="0"))


def downgrade():
    op.drop_column("queue_control", "download_account_cursor")
    op.drop_column("queue_control", "concurrency")
