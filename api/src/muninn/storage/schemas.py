"""Define object storage value objects."""

from dataclasses import dataclass

from muninn.constants import ArtifactKind


@dataclass(frozen=True)
class StoredConversationArtifact:
    """Describe a conversation artifact after it has been stored canonically."""

    kind: ArtifactKind
    key: str
    sha256: str
