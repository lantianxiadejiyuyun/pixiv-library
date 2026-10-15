"""Persist independent queue limits and pause controls."""
from alembic import op
import sqlalchemy as sa

revision = "b58ce962d713"
down_revision = "a47bd851c602"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("queue_control", sa.Column("lanes", sa.JSON(), nullable=True))
    op.create_index("ix_jobs_claim_order", "jobs", ["status", "kind", "created_at", "id"])


def downgrade():
    op.drop_index("ix_jobs_claim_order", "jobs")
    op.drop_column("queue_control", "lanes")
