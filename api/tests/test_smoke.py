import pytest

from muninn.api import app


@pytest.mark.unit
def test_api_metadata() -> None:
    assert app.title == "Muninn API"
