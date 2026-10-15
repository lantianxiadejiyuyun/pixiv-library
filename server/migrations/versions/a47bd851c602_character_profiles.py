"""Persist confirmed online character biographies for the local work wall."""
from alembic import op
import sqlalchemy as sa

revision = "a47bd851c602"
down_revision = "f36ac740b51d"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("character_profiles",
        sa.Column("tag_id", sa.String(36), sa.ForeignKey("tags.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("provider", sa.String(24), nullable=False),
        sa.Column("provider_id", sa.String(32), nullable=False),
        sa.Column("name", sa.String(512), nullable=False),
        sa.Column("native_name", sa.String(512), nullable=True),
        sa.Column("aliases", sa.JSON(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("works", sa.JSON(), nullable=False),
        sa.Column("source_url", sa.String(512), nullable=False),
        sa.Column("search_text", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False))


def downgrade():
    op.drop_table("character_profiles")
