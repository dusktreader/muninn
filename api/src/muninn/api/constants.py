"""Constants owned by the public API boundary."""

from auto_name_enum import AutoNameEnum, LowerCaseMixin, auto

MAX_RECORDING_BYTES = 512 * 1024 * 1024
MAX_RECORDING_SPOOL_BYTES = 8 * 1024 * 1024
MAX_PLAYBACK_DERIVATIVE_BYTES = MAX_RECORDING_BYTES + 16 * 1024 * 1024
PLAYBACK_REMUX_TIMEOUT_SECONDS = 60
RECORDING_READ_CHUNK_BYTES = 1024 * 1024
ALLOWED_RECORDING_TYPES = frozenset(
    {
        "audio/wav",
        "audio/x-wav",
        "audio/mp4",
        "audio/aac",
        "audio/mpeg",
        "audio/webm",
        "audio/ogg",
    }
)


class OperationKind(AutoNameEnum, LowerCaseMixin):
    """Identify the kind of client operation recorded by the API."""

    RECORDING_UPLOAD = auto()


class OperationStatus(AutoNameEnum, LowerCaseMixin):
    """Describe the admission and completion state of a client operation."""

    ACCEPTED = auto()
    COMMITTED = auto()


class UploadState(AutoNameEnum, LowerCaseMixin):
    """Describe the lifecycle state of one resumable upload."""

    UPLOADING = auto()
    COMMITTED = auto()
