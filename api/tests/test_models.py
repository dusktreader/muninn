"""Verify SQLAlchemy model metadata conventions."""

import pytest
from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import ColumnDefault, Enum, Float, Integer, Text, UniqueConstraint, create_engine
from sqlalchemy.exc import StatementError
from sqlalchemy.orm import Session

from muninn.api.constants import OperationKind, OperationStatus, UploadState
from muninn.constants import ArtifactKind, AuthorKind
from muninn.db.models import ArtifactManifest, Base, Conversation, ConversationSpeaker, IdempotencyRecord, Operation, ProcessingAttempt, Speaker, UploadSession


EXPECTED_TABLE_NAMES = {
    "Conversation": "conversations",
    "Note": "notes",
    "Tombstone": "tombstones",
    "Operation": "operations",
    "UploadSession": "upload_sessions",
    "ArtifactManifest": "artifact_manifests",
    "ProcessingAttempt": "processing_attempts",
    "Speaker": "speakers",
    "ConversationSpeaker": "conversation_speakers",
    "AuditEvent": "audit_events",
    "OutboxMessage": "outbox_messages",
    "Job": "jobs",
    "IdempotencyRecord": "idempotency_records",
}

EXPECTED_TEXT_COLUMNS = {
    ("conversations", "owner_subject"),
    ("conversations", "name"),
    ("conversations", "description"),
    ("notes", "subject"),
    ("notes", "text"),
    ("notes", "artifact_version"),
    ("operations", "idempotency_key"),
    ("upload_sessions", "expected_sha256"),
    ("artifact_manifests", "object_key"),
    ("artifact_manifests", "sha256"),
    ("processing_attempts", "error_details"),
    ("speakers", "name"),
    ("conversation_speakers", "display_name"),
    ("audit_events", "actor"),
    ("audit_events", "event_type"),
    ("audit_events", "reason"),
    ("outbox_messages", "subject"),
    ("outbox_messages", "deduplication_key"),
    ("idempotency_records", "owner_subject"),
    ("idempotency_records", "operation_kind"),
    ("idempotency_records", "idempotency_key"),
    ("idempotency_records", "request_fingerprint"),
    ("idempotency_records", "status"),
}


@pytest.mark.unit
def test_model_table_names_are_derived_from_class_names() -> None:
    """Derive every mapped model table name without per-model declarations."""
    mapped_table_names = {
        mapper.class_.__name__: table.name
        for mapper in Base.registry.mappers
        for table in Base.metadata.tables.values()
        if table is mapper.local_table
    }

    assert mapped_table_names == EXPECTED_TABLE_NAMES


@pytest.mark.unit
def test_model_foreign_keys_resolve_against_declared_metadata() -> None:
    """Keep stable foreign-key references valid after deriving table names."""
    for table in Base.metadata.tables.values():
        for foreign_key in table.foreign_keys:
            target_table_name, target_column_name = foreign_key.target_fullname.split(".", 1)

            assert target_table_name in Base.metadata.tables
            assert target_column_name in Base.metadata.tables[target_table_name].c
            assert foreign_key.column.table is Base.metadata.tables[target_table_name]


@pytest.mark.unit
def test_idempotency_record_has_no_managed_resource_foreign_keys() -> None:
    """Allow mutation records to survive delayed synchronization and resource deletion."""
    assert not IdempotencyRecord.__table__.c.resource_id.foreign_keys


@pytest.mark.unit
def test_speaker_catalog_and_conversation_association_metadata() -> None:
    """Model reusable speakers separately from transcript-local associations."""
    speakers = Base.metadata.tables[Speaker.__tablename__]
    associations = Base.metadata.tables[ConversationSpeaker.__tablename__]

    assert set(speakers.c.keys()) == {"id", "name"}
    assert set(associations.c.keys()) == {"id", "conversation_id", "speaker_id", "speaker_number", "display_name"}
    assert not speakers.c.id.nullable
    assert not speakers.c.name.nullable
    assert not associations.c.id.nullable
    assert not associations.c.conversation_id.nullable
    assert associations.c.speaker_id.nullable
    assert not associations.c.speaker_number.nullable
    assert associations.c.display_name.nullable
    assert isinstance(associations.c.speaker_number.type, Integer)

    foreign_keys = {(foreign_key.parent.name, foreign_key.ondelete) for foreign_key in associations.foreign_keys}
    assert foreign_keys == {("conversation_id", "CASCADE"), ("speaker_id", "SET NULL")}
    unique_constraints = {tuple(constraint.columns.keys()) for constraint in associations.constraints if isinstance(constraint, UniqueConstraint)}
    assert unique_constraints == {("conversation_id", "speaker_number")}
    known_speaker_index = next(index for index in associations.indexes if index.name == "uq_conversation_speakers_known_speaker")
    assert known_speaker_index.unique
    assert str(known_speaker_index.dialect_options["postgresql"]["where"]) == "speaker_id IS NOT NULL"
    assert str(known_speaker_index.dialect_options["sqlite"]["where"]) == "speaker_id IS NOT NULL"


@pytest.mark.unit
def test_four_unknown_speakers_are_distinct_conversation_associations() -> None:
    """Represent four unknown transcript speakers without catalog identities or labels."""
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    try:
        with Session(engine) as session:
            conversation = Conversation(occurred_at=datetime.now(timezone.utc), owner_subject="owner")
            session.add(conversation)
            session.flush()
            session.add_all(
                [
                    ConversationSpeaker(conversation_id=conversation.id, speaker_number=number)
                    for number in range(1, 5)
                ]
            )
            session.commit()

            associations = session.query(ConversationSpeaker).all()
            assert len(associations) == 4
            assert {association.speaker_number for association in associations} == {1, 2, 3, 4}
            assert all(association.speaker_id is None and association.display_name is None for association in associations)
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


@pytest.mark.unit
def test_known_speaker_links_to_multiple_conversations_with_local_display_name() -> None:
    """Reuse one catalog speaker while retaining each conversation's display label."""
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    try:
        with Session(engine) as session:
            speaker = Speaker(name="Known speaker")
            first = Conversation(occurred_at=datetime.now(timezone.utc), owner_subject="owner")
            second = Conversation(occurred_at=datetime.now(timezone.utc), owner_subject="owner")
            session.add_all([speaker, first, second])
            session.flush()
            session.add_all(
                [
                    ConversationSpeaker(conversation_id=first.id, speaker_id=speaker.id, speaker_number=1, display_name="First label"),
                    ConversationSpeaker(conversation_id=second.id, speaker_id=speaker.id, speaker_number=7, display_name="Second label"),
                ]
            )
            session.commit()

            associations = session.query(ConversationSpeaker).order_by(ConversationSpeaker.speaker_number).all()
            assert [association.display_name for association in associations] == ["First label", "Second label"]
            assert all(association.speaker_id == speaker.id for association in associations)
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


@pytest.mark.unit
def test_persistence_string_columns_use_unbounded_text() -> None:
    """Use PostgreSQL text columns for every regular string persistence field."""
    text_columns = {
        (table.name, column.name)
        for table in Base.metadata.tables.values()
        for column in table.columns
        if isinstance(column.type, Text)
    }

    assert text_columns == EXPECTED_TEXT_COLUMNS


@pytest.mark.unit
def test_note_author_kind_is_a_native_enum_with_wire_values() -> None:
    """Constrain note authorship to the public human and agent values."""
    author_kind = Base.metadata.tables["notes"].c.author_kind

    assert isinstance(author_kind.type, Enum)
    assert author_kind.type.name == "author_kind"
    assert author_kind.type.enums == [AuthorKind.HUMAN.value, AuthorKind.AGENT.value]


@pytest.mark.unit
def test_artifact_manifest_uses_kind_and_artifact_kind_enum() -> None:
    """Expose artifact identity as `kind` with stable enum metadata and wire values."""
    manifest = Base.metadata.tables[ArtifactManifest.__tablename__]

    assert set(manifest.c.keys()) >= {"kind", "version", "object_key"}
    assert isinstance(manifest.c.kind.type, Enum)
    assert manifest.c.kind.type.name == "artifact_kind"
    assert manifest.c.kind.type.enums == [kind.value for kind in ArtifactKind]


@pytest.mark.unit
@pytest.mark.parametrize(
    "field",
    ["conversation_id", "kind", "version", "object_key", "sha256", "byte_size", "source_version", "is_current", "created_at"],
)
def test_artifact_manifest_required_fields_are_non_nullable(field: str) -> None:
    """Require every artifact-manifest persistence invariant declared by the release schema."""
    assert not Base.metadata.tables[ArtifactManifest.__tablename__].c[field].nullable


@pytest.mark.unit
def test_note_offset_seconds_preserves_fractional_precision() -> None:
    """Store fractional-second offsets in a floating-point database column."""
    offset_seconds = Base.metadata.tables["notes"].c.offset_seconds

    assert isinstance(offset_seconds.type, Float)


@pytest.mark.unit
def test_operation_metadata_uses_conversation_scoped_idempotency_and_enums() -> None:
    """Constrain operation identity and lifecycle fields in SQLAlchemy metadata."""
    operations = Base.metadata.tables["operations"]

    assert "owner_subject" not in operations.c
    assert {tuple(constraint.columns.keys()) for constraint in operations.constraints if isinstance(constraint, UniqueConstraint)} == {
        ("conversation_id", "idempotency_key")
    }
    assert not operations.c.idempotency_key.nullable
    assert isinstance(operations.c.kind.type, Enum)
    assert operations.c.kind.type.name == "operation_kind"
    assert operations.c.kind.type.enums == [OperationKind.RECORDING_UPLOAD.value]
    assert isinstance(operations.c.status.type, Enum)
    assert operations.c.status.type.name == "operation_status"
    assert operations.c.status.type.enums == [OperationStatus.ACCEPTED.value, OperationStatus.COMMITTED.value]
    assert operations.c.status.default is not None
    assert isinstance(operations.c.status.default, ColumnDefault)
    assert operations.c.status.default.arg == OperationStatus.ACCEPTED


@pytest.mark.unit
@pytest.mark.parametrize(
    "field",
    [
        "conversation_id",
        "operation_id",
        "expected_size",
        "expected_sha256",
        "acknowledged_bytes",
        "state",
        "created_at",
    ],
)
def test_upload_session_fields_are_non_nullable(field: str) -> None:
    """Require every persisted upload-session field that the migration declares non-null."""
    assert not Base.metadata.tables[UploadSession.__tablename__].c[field].nullable


@pytest.mark.unit
def test_upload_session_state_is_a_native_enum_with_wire_values() -> None:
    """Constrain upload state to its supported uploading and committed values."""
    state = Base.metadata.tables[UploadSession.__tablename__].c.state

    assert isinstance(state.type, Enum)
    assert state.type.name == "upload_state"
    assert state.type.enums == [UploadState.UPLOADING.value, UploadState.COMMITTED.value]
    assert state.type.validate_strings
    assert state.default is not None
    assert isinstance(state.default, ColumnDefault)
    assert state.default.arg == UploadState.UPLOADING


@pytest.mark.unit
def test_upload_session_state_rejects_invalid_values() -> None:
    """Reject upload states outside the supported lifecycle when binding persistence values."""
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    try:
        with Session(engine) as session:
            conversation = Conversation(occurred_at=datetime.now(timezone.utc), owner_subject="owner")
            session.add(conversation)
            session.flush()
            operation = Operation(
                conversation_id=conversation.id,
                idempotency_key=str(uuid4()),
                kind=OperationKind.RECORDING_UPLOAD,
                status=OperationStatus.ACCEPTED,
            )
            session.add(operation)
            session.flush()
            session.add(
                UploadSession(
                    conversation_id=conversation.id,
                    operation_id=operation.id,
                    expected_size=1,
                    expected_sha256="a" * 64,
                    state="invalid",
                )
            )
            with pytest.raises((LookupError, StatementError)):
                session.flush()
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


@pytest.mark.unit
@pytest.mark.parametrize("field", ["conversation_id", "job_id", "kind", "attempt_number", "state", "started_at"])
def test_processing_attempt_required_fields_are_non_nullable(field: str) -> None:
    """Require every processing-attempt field needed for lifecycle tracking."""
    assert not Base.metadata.tables[ProcessingAttempt.__tablename__].c[field].nullable


@pytest.mark.unit
@pytest.mark.parametrize("field", ["error_code", "error_details", "finished_at"])
def test_processing_attempt_completion_fields_are_nullable(field: str) -> None:
    """Allow completion details to remain absent while attempts run or succeed."""
    assert Base.metadata.tables[ProcessingAttempt.__tablename__].c[field].nullable


@pytest.mark.unit
@pytest.mark.parametrize("field", ["kind", "status"])
def test_operation_enum_fields_reject_invalid_values(field: str) -> None:
    """Reject values outside the operation enums when binding them for persistence."""
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    try:
        with Session(engine) as session:
            conversation = Conversation(occurred_at=datetime.now(timezone.utc), owner_subject="owner")
            session.add(conversation)
            session.flush()
            values = {
                "conversation_id": conversation.id,
                "idempotency_key": str(uuid4()),
                "kind": OperationKind.RECORDING_UPLOAD,
                "status": OperationStatus.ACCEPTED,
            }
            values[field] = "invalid"
            session.add(Operation(**values))
            with pytest.raises((LookupError, StatementError)):
                session.flush()
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()
