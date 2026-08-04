"""Define constants owned by the object storage boundary."""

from auto_name_enum import AutoNameEnum


MISSING_OBJECT_ERROR_CODES = frozenset({"404", "NoSuchKey", "NoSuchObject"})
CONVERSATION_ARTIFACT_KEY = "conversations/{conversation_id}/{kind}/{job_id}.json"


class ArtifactContentType(AutoNameEnum):
    """Represent content types Muninn writes to object storage."""

    RECORDING = "application/octet-stream"
    JSON = "application/json"
