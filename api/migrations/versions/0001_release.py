"""Create the first-release control-plane tables."""

from alembic import op
import sqlalchemy as sa

revision = "0001_release"
down_revision = None
branch_labels = None
depends_on = None

SYNC_STATE = sa.Enum("local_only", "syncing", "processing", "ready", "failed", "attention_required", name="sync_state")
VISIBILITY_STATE = sa.Enum("active", "deleted", name="visibility_state")
ARTIFACT_KIND = sa.Enum("recording", "transcript", "summary", name="artifact_kind")
PROCESSING_KIND = sa.Enum("transcription", "summarization", name="processing_kind")
PROCESSING_STATE = sa.Enum("queued", "running", "succeeded", "failed", "cancelled", name="processing_state")
AUTHOR_KIND = sa.Enum("human", "agent", name="author_kind")
OPERATION_KIND = sa.Enum("recording_upload", name="operation_kind")
OPERATION_STATUS = sa.Enum("accepted", "committed", name="operation_status")
UPLOAD_STATE = sa.Enum("uploading", "committed", name="upload_state")


def upgrade() -> None:
    """Create conversations, notes, and tombstones."""
    op.create_table("conversations", sa.Column("id", sa.Uuid(), primary_key=True), sa.Column("owner_subject", sa.Text(), nullable=False), sa.Column("name", sa.Text(), nullable=False), sa.Column("description", sa.Text()), sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False), sa.Column("people", sa.JSON(), nullable=False), sa.Column("tags", sa.JSON(), nullable=False), sa.Column("revision", sa.Integer(), nullable=False), sa.Column("sync_state", SYNC_STATE, nullable=False), sa.Column("visibility", VISIBILITY_STATE, nullable=False), sa.Column("recording_duration_seconds", sa.Float()), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False), sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False))
    op.create_index("ix_conversations_owner_subject", "conversations", ["owner_subject"])
    op.create_table("notes", sa.Column("id", sa.Uuid(), primary_key=True), sa.Column("conversation_id", sa.Uuid(), sa.ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False), sa.Column("subject", sa.Text(), nullable=False), sa.Column("author_kind", AUTHOR_KIND, nullable=False), sa.Column("delegated_session_id", sa.Uuid()), sa.Column("offset_seconds", sa.Integer(), nullable=False), sa.Column("text", sa.Text(), nullable=False), sa.Column("artifact_version", sa.Text()), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False), sa.Column("deleted_at", sa.DateTime(timezone=True)))
    op.create_table("tombstones", sa.Column("conversation_id", sa.Uuid(), primary_key=True), sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=False))
    op.create_table("operations", sa.Column("id", sa.Uuid(), primary_key=True), sa.Column("conversation_id", sa.Uuid(), sa.ForeignKey("conversations.id"), nullable=False), sa.Column("idempotency_key", sa.Text(), nullable=False), sa.Column("kind", OPERATION_KIND, nullable=False), sa.Column("status", OPERATION_STATUS, nullable=False), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False), sa.UniqueConstraint("conversation_id", "idempotency_key"))
    op.create_table("upload_sessions", sa.Column("id", sa.Uuid(), primary_key=True), sa.Column("conversation_id", sa.Uuid(), sa.ForeignKey("conversations.id"), nullable=False), sa.Column("operation_id", sa.Uuid(), sa.ForeignKey("operations.id"), nullable=False, unique=True), sa.Column("expected_size", sa.BigInteger(), nullable=False), sa.Column("expected_sha256", sa.Text(), nullable=False), sa.Column("acknowledged_bytes", sa.BigInteger(), nullable=False), sa.Column("state", UPLOAD_STATE, nullable=False), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False), sa.UniqueConstraint("conversation_id", "operation_id"))
    op.create_table("artifact_manifests", sa.Column("id", sa.Uuid(), primary_key=True), sa.Column("conversation_id", sa.Uuid(), sa.ForeignKey("conversations.id"), nullable=False), sa.Column("kind", ARTIFACT_KIND, nullable=False), sa.Column("version", sa.Integer(), nullable=False), sa.Column("object_key", sa.Text(), nullable=False, unique=True), sa.Column("sha256", sa.Text(), nullable=False), sa.Column("byte_size", sa.BigInteger(), nullable=False), sa.Column("source_version", sa.Integer(), nullable=False), sa.Column("is_current", sa.Boolean(), nullable=False), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False), sa.UniqueConstraint("conversation_id", "kind", "version"))
    op.create_index("uq_artifact_manifests_current", "artifact_manifests", ["conversation_id", "kind"], unique=True, postgresql_where=sa.text("is_current"))
    op.create_table("processing_attempts", sa.Column("id", sa.Uuid(), primary_key=True), sa.Column("conversation_id", sa.Uuid(), sa.ForeignKey("conversations.id"), nullable=False), sa.Column("job_id", sa.Uuid(), nullable=False), sa.Column("kind", PROCESSING_KIND, nullable=False), sa.Column("attempt_number", sa.Integer(), nullable=False), sa.Column("state", PROCESSING_STATE, nullable=False), sa.Column("error_code", sa.Text()), sa.Column("error_details", sa.Text()), sa.Column("started_at", sa.DateTime(timezone=True), nullable=False), sa.Column("finished_at", sa.DateTime(timezone=True)), sa.UniqueConstraint("job_id", "attempt_number"))
    op.create_table("speakers", sa.Column("id", sa.Uuid(), primary_key=True), sa.Column("name", sa.Text(), nullable=False))
    op.create_table("conversation_speakers", sa.Column("id", sa.Uuid(), primary_key=True), sa.Column("conversation_id", sa.Uuid(), sa.ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False), sa.Column("speaker_id", sa.Uuid(), sa.ForeignKey("speakers.id", ondelete="SET NULL")), sa.Column("speaker_number", sa.Integer(), nullable=False), sa.Column("display_name", sa.Text()), sa.UniqueConstraint("conversation_id", "speaker_number"))
    op.create_index("uq_conversation_speakers_known_speaker", "conversation_speakers", ["conversation_id", "speaker_id"], unique=True, postgresql_where=sa.text("speaker_id IS NOT NULL"), sqlite_where=sa.text("speaker_id IS NOT NULL"))
    op.create_table("audit_events", sa.Column("id", sa.Uuid(), primary_key=True), sa.Column("conversation_id", sa.Uuid(), sa.ForeignKey("conversations.id")), sa.Column("operation_id", sa.Uuid(), sa.ForeignKey("operations.id")), sa.Column("actor", sa.Text(), nullable=False), sa.Column("event_type", sa.Text(), nullable=False), sa.Column("reason", sa.Text(), nullable=False), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False))
    op.create_table("outbox_messages", sa.Column("id", sa.Uuid(), primary_key=True), sa.Column("conversation_id", sa.Uuid(), sa.ForeignKey("conversations.id"), nullable=False), sa.Column("subject", sa.Text(), nullable=False), sa.Column("payload", sa.JSON(), nullable=False), sa.Column("deduplication_key", sa.Text(), nullable=False, unique=True), sa.Column("published_at", sa.DateTime(timezone=True)), sa.Column("attempts", sa.Integer(), nullable=False))
    op.create_table("jobs", sa.Column("id", sa.Uuid(), primary_key=True), sa.Column("conversation_id", sa.Uuid(), sa.ForeignKey("conversations.id"), nullable=False), sa.Column("kind", PROCESSING_KIND, nullable=False), sa.Column("source_version", sa.Integer(), nullable=False), sa.Column("transcript_version", sa.Integer()), sa.Column("state", PROCESSING_STATE, nullable=False), sa.Column("attempts", sa.Integer(), nullable=False), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False), sa.UniqueConstraint("conversation_id", "kind", "source_version", "transcript_version"))
    op.create_table("idempotency_records", sa.Column("id", sa.Uuid(), primary_key=True), sa.Column("owner_subject", sa.Text(), nullable=False), sa.Column("operation_kind", sa.Text(), nullable=False), sa.Column("idempotency_key", sa.Text(), nullable=False), sa.Column("request_fingerprint", sa.Text(), nullable=False), sa.Column("resource_id", sa.Uuid()), sa.Column("response_data", sa.JSON()), sa.Column("status", sa.Text(), nullable=False), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False), sa.Column("completed_at", sa.DateTime(timezone=True)), sa.UniqueConstraint("owner_subject", "operation_kind", "idempotency_key"))
    op.create_index("ix_idempotency_records_resource_id", "idempotency_records", ["resource_id"])


def downgrade() -> None:
    """Refuse destructive downgrade of the release schema."""
    raise RuntimeError("Muninn migrations are forward-only; restore a pre-migration backup instead")
