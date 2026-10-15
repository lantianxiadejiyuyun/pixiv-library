"""Allow encrypted managed proxy subscriptions larger than 64 KiB."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.mysql import MEDIUMTEXT

revision = "c240f019ae62"
down_revision = "b173c019267a"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("network_settings") as batch:
        batch.alter_column("value_encrypted", existing_type=sa.Text(),
            type_=sa.Text().with_variant(MEDIUMTEXT(), "mysql"), existing_nullable=False)


def downgrade():
    # Refuse a lossy narrowing rather than truncate stored subscription secrets.
    connection = op.get_bind()
    if connection.execute(sa.text("SELECT COUNT(*) FROM network_settings WHERE LENGTH(value_encrypted)>65535")).scalar():
        raise RuntimeError("Remove oversized managed subscriptions before downgrading")
    with op.batch_alter_table("network_settings") as batch:
        batch.alter_column("value_encrypted", existing_type=sa.Text().with_variant(MEDIUMTEXT(), "mysql"),
            type_=sa.Text(), existing_nullable=False)
