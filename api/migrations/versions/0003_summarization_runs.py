"""Allow distinct summary runs and persist transcript lineage."""

from alembic import op
import sqlalchemy as sa

revision = "0003_summarization_runs"
down_revision = "0002_note_millisecond_precision"
branch_labels = None
depends_on = None


JOB_UNIQUE_COLUMNS = ["conversation_id", "kind", "source_version", "transcript_version"]
JOB_UNIQUE_NAME = "uq_jobs_conversation_id_kind_source_version_transcript_version"
JOB_NAMING_CONVENTION = {
    "uq": "uq_%(table_name)s_%(column_0_name)s_%(column_1_name)s_%(column_2_name)s_%(column_3_name)s"
}


def upgrade() -> None:
    """Allow multiple completed runs while preventing duplicate active jobs."""
    op.add_column("artifact_manifests", sa.Column("transcript_version", sa.Integer(), nullable=True))
    op.execute(
        sa.text(
            """
            UPDATE artifact_manifests
            SET transcript_version = (
                SELECT transcript.version
                FROM artifact_manifests AS transcript
                JOIN jobs AS summary_job
                  ON summary_job.conversation_id = transcript.conversation_id
                 AND summary_job.source_version = transcript.source_version
                 AND summary_job.transcript_version = transcript.version
                WHERE transcript.conversation_id = artifact_manifests.conversation_id
                  AND transcript.kind = 'transcript'
                  AND transcript.is_current IS TRUE
                  AND summary_job.kind = 'summarization'
                  AND summary_job.state = 'succeeded'
                  AND transcript.source_version = artifact_manifests.source_version
            )
            WHERE artifact_manifests.kind = 'summary'
              AND artifact_manifests.is_current IS TRUE
              AND artifact_manifests.transcript_version IS NULL
            """
        )
    )

    bind = op.get_bind()
    unique_constraint = next(
        constraint
        for constraint in sa.inspect(bind).get_unique_constraints("jobs")
        if constraint["column_names"] == JOB_UNIQUE_COLUMNS
    )
    if bind.dialect.name == "sqlite":
        with op.batch_alter_table("jobs", naming_convention=JOB_NAMING_CONVENTION) as batch:
            batch.drop_constraint(JOB_UNIQUE_NAME, type_="unique")
    else:
        op.drop_constraint(unique_constraint["name"], "jobs", type_="unique")

    op.create_index(
        "uq_jobs_active_transcription",
        "jobs",
        ["conversation_id", "source_version"],
        unique=True,
        postgresql_where=sa.text("kind = 'transcription' AND state IN ('queued', 'running')"),
        sqlite_where=sa.text("kind = 'transcription' AND state IN ('queued', 'running')"),
    )
    op.create_index(
        "uq_jobs_active_summarization",
        "jobs",
        ["conversation_id", "source_version", "transcript_version"],
        unique=True,
        postgresql_where=sa.text("kind = 'summarization' AND state IN ('queued', 'running')"),
        sqlite_where=sa.text("kind = 'summarization' AND state IN ('queued', 'running')"),
    )


def downgrade() -> None:
    """Refuse to discard re-summarization history."""
    raise RuntimeError("Muninn migrations are forward-only; restore a pre-migration backup instead")
