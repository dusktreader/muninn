"""Verify release migration metadata for artifact manifests."""

import importlib.util
from pathlib import Path
from uuid import uuid4

from alembic import command
from alembic.config import Config
import pytest
import snick
from sqlalchemy import create_engine, inspect, select, text, update
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.exc import IntegrityError

from muninn.config import service_settings


def _release_migration():
    """Load the release migration as a test module."""
    path = Path(__file__).parents[1] / "migrations" / "versions" / "0001_release.py"
    spec = importlib.util.spec_from_file_location("muninn_release_migration", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _speaker_mapping_migration():
    path = Path(__file__).parents[1] / "migrations" / "versions" / "0004_speaker_mapping_lineage.py"
    spec = importlib.util.spec_from_file_location("muninn_speaker_mapping_migration", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.unit
def test_release_migration_declares_artifact_kind_metadata() -> None:
    """Keep migration enum and artifact-manifest identity names aligned."""
    migration = _release_migration()

    assert migration.ARTIFACT_KIND.name == "artifact_kind"
    assert migration.ARTIFACT_KIND.enums == ["recording", "transcript", "summary"]


@pytest.mark.unit
def test_speaker_mapping_migration_uses_existing_postgresql_enum_bind_types() -> None:
    migration = _speaker_mapping_migration()
    jobs, processing_attempts = migration._processing_tables()
    dialect = postgresql.dialect()
    statements = (
        (
            select(jobs.c.id).where(
                jobs.c.kind == "summarization",
                jobs.c.state.in_(("queued", "running")),
            ),
            {"processing_kind", "processing_state"},
        ),
        (
            update(jobs).where(jobs.c.state.in_(("queued", "running"))).values(state="cancelled"),
            {"processing_state"},
        ),
        (
            update(processing_attempts)
            .where(processing_attempts.c.state == "running")
            .values(state="cancelled"),
            {"processing_state"},
        ),
    )

    for statement, expected_types in statements:
        compiled = statement.compile(dialect=dialect)
        bind_types = [bind.type.dialect_impl(dialect) for bind in compiled.binds.values()]
        actual_types = set()
        for bind_type in bind_types:
            assert isinstance(bind_type, postgresql.ENUM)
            actual_types.add(bind_type.name)
            assert not bind_type.create_type
        assert actual_types == expected_types

    assert jobs.c.kind.type.compile(dialect=sqlite.dialect()).startswith("VARCHAR")
    assert jobs.c.state.type.compile(dialect=sqlite.dialect()).startswith("VARCHAR")
    assert processing_attempts.c.state.type.compile(dialect=sqlite.dialect()).startswith("VARCHAR")


@pytest.mark.unit
def test_note_offset_migration_preserves_old_seconds_and_allows_fractional_values(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Convert legacy integer note offsets to fractional seconds without changing their value."""
    database = tmp_path / "notes.sqlite"
    monkeypatch.setattr(service_settings, "database_url", f"sqlite:///{database}")
    config = Config(str(Path(__file__).parents[1] / "alembic.ini"))
    config.set_main_option("script_location", str(Path(__file__).parents[1] / "migrations"))
    command.upgrade(config, "0001_release")

    engine = create_engine(f"sqlite:///{database}")
    note_id = uuid4().hex
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO notes "
                "(id, conversation_id, subject, author_kind, offset_seconds, text, created_at) "
                "VALUES (:id, :conversation_id, :subject, :author_kind, :offset_seconds, :text, :created_at)"
            ),
            {
                "id": note_id,
                "conversation_id": uuid4().hex,
                "subject": "owner-1",
                "author_kind": "human",
                "offset_seconds": 12,
                "text": "Legacy note",
                "created_at": "2026-01-01T00:00:00+00:00",
            },
        )

    command.upgrade(config, "head")

    with engine.connect() as connection:
        offset = connection.execute(
            text("SELECT offset_seconds FROM notes WHERE id = :id"), {"id": note_id}
        ).scalar_one()
    assert float(offset) == 12.0
    offset_column = next(column for column in inspect(engine).get_columns("notes") if column["name"] == "offset_seconds")
    assert "FLOAT" in str(offset_column["type"]).upper()
    engine.dispose()


@pytest.mark.unit
def test_summarization_migration_allows_new_runs_and_enforces_one_active_job(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Preserve transcript lineage while permitting reruns and rejecting concurrent active runs."""
    database = tmp_path / "summarization.sqlite"
    monkeypatch.setattr(service_settings, "database_url", f"sqlite:///{database}")
    config = Config(str(Path(__file__).parents[1] / "alembic.ini"))
    config.set_main_option("script_location", str(Path(__file__).parents[1] / "migrations"))
    command.upgrade(config, "head")

    engine = create_engine(f"sqlite:///{database}")
    manifest_columns = {column["name"] for column in inspect(engine).get_columns("artifact_manifests")}
    conversation_columns = {column["name"] for column in inspect(engine).get_columns("conversations")}
    job_columns = {column["name"] for column in inspect(engine).get_columns("jobs")}
    job_indexes = {index["name"]: index for index in inspect(engine).get_indexes("jobs")}
    job_uniques = inspect(engine).get_unique_constraints("jobs")
    assert {"transcript_version", "speaker_revision"}.issubset(manifest_columns)
    assert "speaker_revision" in conversation_columns
    assert {"speaker_revision", "speaker_mapping"}.issubset(job_columns)
    assert {"uq_jobs_active_transcription", "uq_jobs_active_summarization"}.issubset(job_indexes)
    assert job_indexes["uq_jobs_active_summarization"]["column_names"] == [
        "conversation_id",
        "source_version",
        "transcript_version",
        "speaker_revision",
    ]
    assert all(
        constraint["column_names"] != ["conversation_id", "kind", "source_version", "transcript_version"]
        for constraint in job_uniques
    )

    conversation_id = uuid4().hex
    timestamp = "2026-01-01T00:00:00+00:00"
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO conversations "
                "(id, owner_subject, name, occurred_at, people, tags, revision, sync_state, visibility, created_at, updated_at) "
                "VALUES (:id, 'owner-1', 'Migration test', :timestamp, '[]', '[]', 1, 'local_only', 'active', :timestamp, :timestamp)"
            ),
            {"id": conversation_id, "timestamp": timestamp},
        )
        job_statement = text(
            "INSERT INTO jobs "
            "(id, conversation_id, kind, source_version, transcript_version, state, attempts, created_at) "
            "VALUES (:id, :conversation_id, 'summarization', 1, 1, :state, 0, :timestamp)"
        )
        connection.execute(
            job_statement,
            [
                {"id": uuid4().hex, "conversation_id": conversation_id, "state": "succeeded", "timestamp": timestamp},
                {"id": uuid4().hex, "conversation_id": conversation_id, "state": "succeeded", "timestamp": timestamp},
            ],
        )
        connection.execute(
            job_statement,
            {"id": uuid4().hex, "conversation_id": conversation_id, "state": "queued", "timestamp": timestamp},
        )
        connection.execute(
            text(
                "INSERT INTO jobs "
                "(id, conversation_id, kind, source_version, transcript_version, speaker_revision, state, attempts, created_at) "
                "VALUES (:id, :conversation_id, 'summarization', 1, 1, 1, 'queued', 0, :timestamp)"
            ),
            {"id": uuid4().hex, "conversation_id": conversation_id, "timestamp": timestamp},
        )
        defaults = connection.execute(
            text("SELECT speaker_revision, speaker_mapping FROM jobs WHERE state = 'succeeded' LIMIT 1")
        ).one()
        assert defaults[0] == 0
        assert defaults[1] == "{}"
        with connection.begin_nested():
            with pytest.raises(IntegrityError):
                connection.execute(
                    job_statement,
                    {"id": uuid4().hex, "conversation_id": conversation_id, "state": "running", "timestamp": timestamp},
                )
    engine.dispose()


@pytest.mark.unit
def test_summarization_migration_backfills_only_provable_legacy_lineage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Associate legacy summaries only when a successful job matches the current transcript."""
    database = tmp_path / "legacy-summaries.sqlite"
    monkeypatch.setattr(service_settings, "database_url", f"sqlite:///{database}")
    config = Config(str(Path(__file__).parents[1] / "alembic.ini"))
    config.set_main_option("script_location", str(Path(__file__).parents[1] / "migrations"))
    command.upgrade(config, "0002_note_millisecond_precision")

    engine = create_engine(f"sqlite:///{database}")
    matched_conversation_id = uuid4().hex
    stale_conversation_id = uuid4().hex
    timestamp = "2026-01-01T00:00:00+00:00"
    with engine.begin() as connection:
        for conversation_id in (matched_conversation_id, stale_conversation_id):
            connection.execute(
                text(
                    "INSERT INTO conversations "
                    "(id, owner_subject, name, occurred_at, people, tags, revision, sync_state, visibility, created_at, updated_at) "
                    "VALUES (:id, 'owner-1', 'Legacy test', :timestamp, '[]', '[]', 1, 'local_only', 'active', :timestamp, :timestamp)"
                ),
                {"id": conversation_id, "timestamp": timestamp},
            )
        manifest_statement = text(
            "INSERT INTO artifact_manifests "
            "(id, conversation_id, kind, version, object_key, sha256, byte_size, source_version, is_current, created_at) "
            "VALUES (:id, :conversation_id, :kind, :version, :object_key, :sha256, 10, 2, 1, :timestamp)"
        )
        for conversation_id, transcript_version, prefix in (
            (matched_conversation_id, 4, "matched"),
            (stale_conversation_id, 5, "stale"),
        ):
            connection.execute(
                manifest_statement,
                [
                    {
                        "id": uuid4().hex,
                        "conversation_id": conversation_id,
                        "kind": "transcript",
                        "version": transcript_version,
                        "object_key": f"{prefix}-transcript.json",
                        "sha256": "a" * 64,
                        "timestamp": timestamp,
                    },
                    {
                        "id": uuid4().hex,
                        "conversation_id": conversation_id,
                        "kind": "summary",
                        "version": 1,
                        "object_key": f"{prefix}-summary.json",
                        "sha256": "b" * 64,
                        "timestamp": timestamp,
                    },
                ],
            )
        connection.execute(
            text(
                "INSERT INTO jobs "
                "(id, conversation_id, kind, source_version, transcript_version, state, attempts, created_at) "
                "VALUES (:id, :conversation_id, 'summarization', 2, :transcript_version, 'succeeded', 1, :timestamp)"
            ),
            [
                {
                    "id": uuid4().hex,
                    "conversation_id": matched_conversation_id,
                    "transcript_version": 4,
                    "timestamp": timestamp,
                },
                {
                    "id": uuid4().hex,
                    "conversation_id": stale_conversation_id,
                    "transcript_version": 4,
                    "timestamp": timestamp,
                },
            ],
        )

    command.upgrade(config, "head")

    with engine.connect() as connection:
        lineage: dict[str, int | None] = {}
        for conversation_id, transcript_version in connection.execute(
            text(
                "SELECT conversation_id, transcript_version FROM artifact_manifests "
                "WHERE kind = 'summary' AND is_current = 1"
            )
        ):
            lineage[str(conversation_id)] = transcript_version
    assert lineage == {matched_conversation_id: 4, stale_conversation_id: None}
    engine.dispose()


@pytest.mark.unit
def test_speaker_mapping_migration_invalidates_legacy_mapped_summaries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Advance legacy conversations with custom names beyond their unmapped summaries."""
    database = tmp_path / "legacy-speakers.sqlite"
    monkeypatch.setattr(service_settings, "database_url", f"sqlite:///{database}")
    config = Config(str(Path(__file__).parents[1] / "alembic.ini"))
    config.set_main_option("script_location", str(Path(__file__).parents[1] / "migrations"))
    command.upgrade(config, "0003_summarization_runs")

    engine = create_engine(f"sqlite:///{database}")
    conversation_id = uuid4().hex
    timestamp = "2026-01-01T00:00:00+00:00"
    with engine.begin() as connection:
        connection.execute(
            text(
                snick.dedent(
                    """
                    INSERT INTO conversations
                    (id, owner_subject, name, occurred_at, people, tags, revision, sync_state, visibility, created_at, updated_at)
                    VALUES (:id, 'owner-1', 'Legacy speaker', :timestamp, '[]', '[]', 1, 'ready', 'active', :timestamp, :timestamp)
                    """
                )
            ),
            {"id": conversation_id, "timestamp": timestamp},
        )
        manifest_statement = text(
            snick.dedent(
                """
                INSERT INTO artifact_manifests
                (id, conversation_id, kind, version, object_key, sha256, byte_size, source_version, transcript_version, is_current, created_at)
                VALUES (:id, :conversation_id, :kind, 1, :object_key, :sha256, 10, 1, 1, 1, :timestamp)
                """
            )
        )
        connection.execute(
            manifest_statement,
            [
                {
                    "id": uuid4().hex,
                    "conversation_id": conversation_id,
                    "kind": "transcript",
                    "object_key": "legacy-transcript.json",
                    "sha256": "a" * 64,
                    "timestamp": timestamp,
                },
                {
                    "id": uuid4().hex,
                    "conversation_id": conversation_id,
                    "kind": "summary",
                    "object_key": "legacy-summary.json",
                    "sha256": "b" * 64,
                    "timestamp": timestamp,
                },
            ],
        )
        connection.execute(
            text(
                snick.dedent(
                    """
                    INSERT INTO conversation_speakers
                    (id, conversation_id, speaker_id, speaker_number, display_name)
                    VALUES (:id, :conversation_id, NULL, 1, 'Ada Lovelace')
                    """
                )
            ),
            {"id": uuid4().hex, "conversation_id": conversation_id},
        )
        running_job_id = uuid4().hex
        queued_job_id = uuid4().hex
        connection.execute(
            text(
                "INSERT INTO jobs "
                "(id, conversation_id, kind, source_version, transcript_version, state, attempts, created_at) "
                "VALUES (:id, :conversation_id, 'summarization', 1, :transcript_version, :state, 0, :timestamp)"
            ),
            [
                {
                    "id": running_job_id,
                    "conversation_id": conversation_id,
                    "transcript_version": 1,
                    "state": "running",
                    "timestamp": timestamp,
                },
                {
                    "id": queued_job_id,
                    "conversation_id": conversation_id,
                    "transcript_version": 2,
                    "state": "queued",
                    "timestamp": timestamp,
                },
            ],
        )
        connection.execute(
            text(
                "INSERT INTO processing_attempts "
                "(id, conversation_id, job_id, kind, attempt_number, state, started_at) "
                "VALUES (:id, :conversation_id, :job_id, 'summarization', 1, 'running', :timestamp)"
            ),
            {"id": uuid4().hex, "conversation_id": conversation_id, "job_id": running_job_id, "timestamp": timestamp},
        )
        connection.execute(
            text(
                "INSERT INTO outbox_messages "
                "(id, conversation_id, subject, payload, deduplication_key, published_at, attempts) "
                "VALUES (:id, :conversation_id, 'muninn.jobs', :payload, :deduplication_key, NULL, 0)"
            ),
            [
                {
                    "id": uuid4().hex,
                    "conversation_id": conversation_id,
                    "payload": f'{{"job_id":"{running_job_id}","kind":"summarization"}}',
                    "deduplication_key": f"legacy-summary-{running_job_id}",
                },
                {
                    "id": uuid4().hex,
                    "conversation_id": conversation_id,
                    "payload": f'{{"job_id":"{queued_job_id}","kind":"summarization"}}',
                    "deduplication_key": f"legacy-summary-{queued_job_id}",
                },
            ],
        )

    command.upgrade(config, "head")

    with engine.connect() as connection:
        speaker_revision = connection.execute(
            text("SELECT speaker_revision FROM conversations WHERE id = :id"), {"id": conversation_id}
        ).scalar_one()
        summary_revision = connection.execute(
            text("SELECT speaker_revision FROM artifact_manifests WHERE kind = 'summary'")
        ).scalar_one()
        migrated_jobs = connection.execute(
            text(
                "SELECT state, attempts, speaker_revision FROM jobs "
                "WHERE conversation_id = :id ORDER BY transcript_version"
            ),
            {"id": conversation_id},
        ).all()
        migrated_attempt = connection.execute(
            text("SELECT state, finished_at FROM processing_attempts WHERE job_id = :id"),
            {"id": running_job_id},
        ).one()
        pending_outbox_count = connection.execute(
            text("SELECT count(*) FROM outbox_messages WHERE conversation_id = :id AND published_at IS NULL"),
            {"id": conversation_id},
        ).scalar_one()
    assert speaker_revision == 1
    assert summary_revision == 0
    assert migrated_jobs == [("cancelled", 1, 0), ("cancelled", 0, 0)]
    assert migrated_attempt.state == "cancelled"
    assert migrated_attempt.finished_at is not None
    assert pending_outbox_count == 0
    engine.dispose()
