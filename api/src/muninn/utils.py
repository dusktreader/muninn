"""Provide shared utility functions used by Muninn."""

import hashlib
import json
from hashlib import sha256
from typing import Any, Iterable


def calculate_digest(content: bytes | Iterable[bytes]) -> str:
    """Calculate the lowercase SHA-256 hexadecimal digest for content."""
    digest = sha256()
    if isinstance(content, bytes):
        digest.update(content)
    else:
        for chunk in content:
            digest.update(chunk)
    return digest.hexdigest()


def request_fingerprint(value: Any) -> str:
    """Hash canonical request metadata without retaining the submitted request."""
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
