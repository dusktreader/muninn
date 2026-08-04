"""Vocabulary shared by multiple Muninn packages."""

from auto_name_enum import AutoNameEnum, auto, LowerCaseMixin


class Environment(AutoNameEnum, LowerCaseMixin):
    """Define the deploy environments."""

    LOCAL = auto()
    DEV = auto()
    QA = auto()
    STAGING = auto()
    PROD = auto()


class LogLevel(AutoNameEnum):
    """Define the Loguru levels supported by the service configuration."""

    TRACE = auto()
    DEBUG = auto()
    INFO = auto()
    SUCCESS = auto()
    WARNING = auto()
    ERROR = auto()
    CRITICAL = auto()


class ArtifactKind(AutoNameEnum, LowerCaseMixin):
    """Identify the kind of content represented by an object reference."""

    RECORDING = auto()
    TRANSCRIPT = auto()
    SUMMARY = auto()


class ProcessingKind(AutoNameEnum, LowerCaseMixin):
    """Identify the processing operation represented by a job."""

    TRANSCRIPTION = auto()
    SUMMARIZATION = auto()


class ProcessingState(AutoNameEnum, LowerCaseMixin):
    """Describe the lifecycle state of one processing operation."""

    QUEUED = auto()
    RUNNING = auto()
    SUCCEEDED = auto()
    FAILED = auto()
    CANCELLED = auto()


class ProcessingErrorCode(AutoNameEnum, LowerCaseMixin):
    """Identify a processing failure with a stable machine-readable code."""

    MISSING_INPUT = auto()
    INVALID_INPUT = auto()
    PROVIDER_TIMEOUT = auto()
    PROVIDER_QUOTA = auto()
    PROVIDER_INVALID_RESPONSE = auto()
    PROVIDER_UNAVAILABLE = auto()
    INVALID_CONFIGURATION = auto()
    INVALID_MODEL = auto()
    INFRASTRUCTURE_UNAVAILABLE = auto()
    UNKNOWN = auto()


class SyncState(AutoNameEnum, LowerCaseMixin):
    """Describe synchronization independently from processing and visibility."""

    LOCAL_ONLY = auto()
    SYNCING = auto()
    PROCESSING = auto()
    READY = auto()
    FAILED = auto()
    ATTENTION_REQUIRED = auto()


class VisibilityState(AutoNameEnum, LowerCaseMixin):
    """Describe visibility and tombstone state."""

    ACTIVE = auto()
    DELETED = auto()


class AuthorKind(AutoNameEnum, LowerCaseMixin):
    """Identify whether a note was authored by a person or an agent."""

    HUMAN = auto()
    AGENT = auto()


class ErrorKind(AutoNameEnum, LowerCaseMixin):
    """Enumerate stable symbolic API error reasons."""

    AUTHENTICATION_REQUIRED = auto()
    CREDENTIAL_INVALID = auto()
    CREDENTIAL_EXPIRED = auto()
    CREDENTIAL_NOT_ACTIVE = auto()
    CREDENTIAL_WRONG_ISSUER = auto()
    CREDENTIAL_WRONG_AUDIENCE = auto()
    CREDENTIAL_REVOKED = auto()
    CAPABILITY_DENIED = auto()
    RESOURCE_NOT_FOUND = auto()
    REVISION_CONFLICT = auto()
    CONVERSATION_DELETED = auto()
    OFFSET_OUT_OF_RANGE = auto()
    DELEGATED_SESSION_REQUIRED = auto()
    VALIDATION_ERROR = auto()
    REQUEST_FAILED = auto()
    IDEMPOTENCY_KEY_REUSED = auto()
    RESOURCE_ID_CONFLICT = auto()
    UNSUPPORTED_MEDIA = auto()
    MEDIA_TOO_LARGE = auto()
    MEDIA_DIGEST_MISMATCH = auto()
    DATABASE_NOT_READY = auto()
