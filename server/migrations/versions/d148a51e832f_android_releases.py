"""Verified, immutable Android update releases."""
from alembic import op
import sqlalchemy as sa

revision = "d148a51e832f"
down_revision = "c037f40d721e"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("android_releases",
        sa.Column("version_code", sa.Integer(), primary_key=True, autoincrement=False),
        sa.Column("version_name", sa.String(80), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False, unique=True),
        sa.Column("certificate_sha256", sa.String(64), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("min_sdk", sa.Integer(), nullable=False),
        sa.Column("relative_path", sa.String(255), nullable=False),
        sa.Column("notes", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False))


def downgrade():
    op.drop_table("android_releases")
