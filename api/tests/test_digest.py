"""Verify shared content digest calculation."""

import pytest

from muninn.utils import calculate_digest


@pytest.mark.unit
def test_calculate_digest_hashes_bytes() -> None:
    """Calculate the lowercase SHA-256 digest for bytes."""
    assert calculate_digest(b"Muninn") == "ba4e367d83960c1fcc053fcc233b82004f97e490507a1c7a20b494599fdae11a"


@pytest.mark.unit
def test_calculate_digest_hashes_chunks_incrementally() -> None:
    """Calculate the same digest when content arrives as separate chunks."""
    chunks = (chunk for chunk in (b"Mu", b"n", b"inn"))

    assert calculate_digest(chunks) == calculate_digest(b"Muninn")
