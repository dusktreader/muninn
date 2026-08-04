"""Exceptions owned by the public API boundary."""

from muninn.constants import ErrorKind
from muninn.exceptions import MuninnError


class ApiError(MuninnError):
    """Represent a failure owned by the public API boundary."""


class ConversationDeletedError(ApiError):
    """Represent an attempt to recreate a conversation protected by a tombstone."""

    kind = ErrorKind.CONVERSATION_DELETED
    status_code = 409
    safe_message = "Conversation has been deleted"


class RevisionConflictError(ApiError):
    """Represent an update based on a stale conversation revision."""

    kind = ErrorKind.REVISION_CONFLICT
    status_code = 409
    safe_message = "Conversation revision conflict"


class ConversationCleanupError(ApiError):
    """Represent a failure while removing a conversation's stored artifacts."""

    safe_message = "Conversation cleanup failed"


class RecordingUploadError(ApiError):
    """Represent an unexpected failure while storing a recording upload."""

    status_code = 503
    safe_message = "Recording upload failed"


class RecordingExportError(ApiError):
    """Represent a failure while converting a recording for export."""

    status_code = 503
    safe_message = "Recording export failed"


class RecordingPlaybackError(ApiError):
    """Represent a failure while preparing a recording for playback."""

    status_code = 503
    safe_message = "Recording playback failed"


class RecordingAccessGrantError(ApiError):
    """Represent a missing or invalid short-lived recording access grant."""

    status_code = 404
    safe_message = "Recording was not found"


class UnsupportedMediaError(ApiError):
    """Represent a recording media type unsupported by the API."""

    kind = ErrorKind.UNSUPPORTED_MEDIA
    status_code = 415
    safe_message = "Unsupported media type"


class MediaTooLargeError(ApiError):
    """Represent a recording larger than the API accepts."""

    kind = ErrorKind.MEDIA_TOO_LARGE
    status_code = 413
    safe_message = "Media is too large"


class MediaDigestMismatchError(ApiError):
    """Represent a recording whose supplied digest does not match its content."""

    kind = ErrorKind.MEDIA_DIGEST_MISMATCH
    status_code = 400
    safe_message = "Media digest does not match content"


class DatabaseNotReadyError(ApiError):
    """Represent a failed database readiness check."""

    kind = ErrorKind.DATABASE_NOT_READY
    status_code = 503
    safe_message = "Database is not ready"


class NoteOffsetOutOfRangeError(ApiError):
    """Represent a note offset outside the recording duration."""

    kind = ErrorKind.OFFSET_OUT_OF_RANGE
    status_code = 422
    safe_message = "Note offset is out of range"
