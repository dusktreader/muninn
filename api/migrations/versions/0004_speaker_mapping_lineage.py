"""Persist conversation-local speaker mappings and summary lineage."""

from datetime import datetime, timezone

from alembic import op
import snick
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0004_speaker_mapping_lineage"
down_revision = "0003_summarization_runs"
branch_labels = None
depends_on = None

PROCESSING_KIND = sa.Enum("transcription", "summarization", name="processing_kind").with_variant(
    postgresql.ENUM("transcription", "summarization", name="processing_kind", create_type=False), "postgresql"
)
PROCESSING_STATE = sa.Enum("queued", "running", "succeeded", "failed", "cancelled", name="processing_state").with_variant(
    postgresql.ENUM("queued", "running", "succeeded", "failed", "cancelled", name="processing_state", create_type=False),
    "postgresql",
)


def _processing_tables() -> tuple[sa.TableClause, sa.TableClause]:
    jobs = sa.table(
        "jobs",
        sa.column("id", sa.Uuid()),
        sa.column("conversation_id", sa.Uuid()),
        sa.column("kind", PROCESSING_KIND),
        sa.column("state", PROCESSING_STATE),
        sa.column("attempts", sa.Integer()),
        sa.column("speaker_revision", sa.Integer()),
    )
    processing_attempts = sa.table(
        "processing_attempts",
        sa.column("job_id", sa.Uuid()),
        sa.column("attempt_number", sa.Integer()),
        sa.column("state", PROCESSING_STATE),
        sa.column("finished_at", sa.DateTime(timezone=True)),
    )
    return jobs, processing_attempts


def upgrade() -> None:
    """Add mapping snapshots and revisions without changing canonical artifacts."""
    op.add_column(
        "conversations",
        sa.Column("speaker_revision", sa.Integer(), server_default=sa.text("0"), nullable=False),
    )
    op.add_column(
        "jobs",
        sa.Column("speaker_revision", sa.Integer(), server_default=sa.text("0"), nullable=False),
    )
    op.add_column(
        "jobs",
        sa.Column("speaker_mapping", sa.JSON(), server_default=sa.text("'{}'"), nullable=False),
    )
    op.add_column(
        "artifact_manifests",
        sa.Column("speaker_revision", sa.Integer(), server_default=sa.text("0"), nullable=False),
    )
    op.execute(
        sa.text(
            snick.dedent(
                """
                UPDATE conversations SET speaker_revision = 1
                WHERE EXISTS (
                    SELECT 1 FROM conversation_speakers
                    WHERE conversation_speakers.conversation_id = conversations.id
                      AND conversation_speakers.display_name IS NOT NULL
                )
                """
            )
        )
    )
    connection = op.get_bind()
    conversations = sa.table(
        "conversations",
        sa.column("id", sa.Uuid()),
        sa.column("speaker_revision", sa.Integer()),
    )
    jobs, processing_attempts = _processing_tables()
    outbox_messages = sa.table(
        "outbox_messages",
        sa.column("id", sa.Uuid()),
        sa.column("conversation_id", sa.Uuid()),
        sa.column("payload", sa.JSON()),
        sa.column("published_at", sa.DateTime(timezone=True)),
    )
    outdated_jobs = connection.execute(
        sa.select(jobs.c.id, jobs.c.conversation_id, jobs.c.attempts)
        .select_from(jobs.join(conversations, jobs.c.conversation_id == conversations.c.id))
        .where(
            jobs.c.kind == "summarization",
            jobs.c.state.in_(("queued", "running")),
            jobs.c.speaker_revision != conversations.c.speaker_revision,
        )
    ).mappings().all()
    now = datetime.now(timezone.utc)
    cancelled_jobs = []
    for job in outdated_jobs:
        closed_attempt = connection.execute(
            sa.update(processing_attempts)
            .where(
                processing_attempts.c.job_id == job["id"],
                processing_attempts.c.attempt_number == job["attempts"] + 1,
                processing_attempts.c.state == "running",
            )
            .values(state="cancelled", finished_at=now)
        ).rowcount
        cancelled = connection.execute(
            sa.update(jobs)
            .where(jobs.c.id == job["id"], jobs.c.state.in_(("queued", "running")))
            .values(state="cancelled", attempts=jobs.c.attempts + (1 if closed_attempt else 0))
        ).rowcount
        if cancelled:
            cancelled_jobs.append((job["id"], job["conversation_id"]))
    for job_id, conversation_id in cancelled_jobs:
        pending_messages = connection.execute(
            sa.select(outbox_messages.c.id, outbox_messages.c.payload).where(
                outbox_messages.c.conversation_id == conversation_id,
                outbox_messages.c.published_at.is_(None),
            )
        ).mappings().all()
        for message in pending_messages:
            payload = message["payload"]
            if isinstance(payload, dict) and payload.get("job_id") in {str(job_id), job_id.hex}:
                connection.execute(
                    sa.update(outbox_messages)
                    .where(outbox_messages.c.id == message["id"], outbox_messages.c.published_at.is_(None))
                    .values(published_at=now)
                )
    op.drop_index("uq_jobs_active_summarization", table_name="jobs")
    op.create_index(
        "uq_jobs_active_summarization",
        "jobs",
        ["conversation_id", "source_version", "transcript_version", "speaker_revision"],
        unique=True,
        postgresql_where=sa.text("kind = 'summarization' AND state IN ('queued', 'running')"),
        sqlite_where=sa.text("kind = 'summarization' AND state IN ('queued', 'running')"),
    )


def downgrade() -> None:
    """Refuse to discard speaker mapping history."""
    raise RuntimeError("Muninn migrations are forward-only; restore a pre-migration backup instead")
