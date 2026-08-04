"""Exercise a small local integration contract without requiring external infrastructure."""

import pytest


@pytest.mark.integration
def test_integration_marker_is_selectable() -> None:
    """Select at least one integration test through the documented QA target."""
    from muninn.schemas import SummaryResult

    result = SummaryResult(provider="fake", model="free-test", overview=None)
    assert result.provider == "fake"
