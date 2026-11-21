"""Preserve fractional-second note offsets."""

from alembic import op
import sqlalchemy as sa

revision = "0002_note_millisecond_precision"
down_revision = "0001_release"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Convert note offsets to fractional seconds without changing existing values."""
    with op.batch_alter_table("notes") as batch:
        batch.alter_column(
            "offset_seconds",
            existing_type=sa.Integer(),
            type_=sa.Float(),
            existing_nullable=False,
        )


def downgrade() -> None:
    """Refuse to discard subsecond note offsets."""
    raise RuntimeError("Muninn migrations are forward-only; restore a pre-migration backup instead")
