"""Toggle local pacing independently of upstream Retry-After."""
from alembic import op
import sqlalchemy as sa

revision = "e259b62f943a"
down_revision = "d148a51e832f"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("queue_control", sa.Column("rate_limit_enabled", sa.Boolean(), nullable=False, server_default="1"))
    op.add_column("queue_control", sa.Column("backoff_until", sa.DateTime(), nullable=True))
    # Old versions stored pacing and upstream cooldown in the same timestamp.
    # Preserve an existing reservation once instead of silently skipping 429.
    op.execute("UPDATE queue_control SET backoff_until = next_request_at WHERE next_request_at IS NOT NULL")


def downgrade():
    op.execute("UPDATE queue_control SET next_request_at = backoff_until WHERE backoff_until IS NOT NULL AND (next_request_at IS NULL OR next_request_at < backoff_until)")
    op.drop_column("queue_control", "backoff_until")
    op.drop_column("queue_control", "rate_limit_enabled")
