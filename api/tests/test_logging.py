"""Verify configurable Loguru setup."""

import sys
from collections.abc import Iterator
from unittest.mock import Mock

import pytest
from loguru import logger
from pydantic import ValidationError

from muninn.config import ServiceSettings, service_settings
from muninn.constants import LogLevel
from muninn.logging import init_logging


@pytest.fixture
def restore_loguru_handlers() -> Iterator[None]:
    """Restore Loguru's default handler after a logging configuration test."""
    logger.remove()
    logger.add(sys.stderr, level="INFO")
    yield
    logger.remove()
    logger.add(sys.stderr, level="INFO")


@pytest.mark.unit
def test_init_logging_keeps_handlers_for_default_info(monkeypatch: pytest.MonkeyPatch, restore_loguru_handlers: None) -> None:
    """Keep the existing handlers when the configured level is INFO."""
    monkeypatch.setattr(service_settings, "log_level", LogLevel.INFO)
    remove = Mock(wraps=logger.remove)
    add = Mock(wraps=logger.add)
    monkeypatch.setattr(logger, "remove", remove)
    monkeypatch.setattr(logger, "add", add)

    init_logging()

    remove.assert_not_called()
    add.assert_not_called()


@pytest.mark.unit
def test_init_logging_replaces_handlers_for_non_default_level(
    monkeypatch: pytest.MonkeyPatch,
    restore_loguru_handlers: None,
) -> None:
    """Replace existing handlers with stderr output at the requested level."""
    monkeypatch.setattr(service_settings, "log_level", LogLevel.DEBUG)
    remove = Mock(wraps=logger.remove)
    add = Mock(wraps=logger.add)
    monkeypatch.setattr(logger, "remove", remove)
    monkeypatch.setattr(logger, "add", add)

    init_logging()

    remove.assert_called_once_with()
    add.assert_called_once()
    assert add.call_args.kwargs["level"] == "DEBUG"
    assert add.call_args.args[0] is sys.stderr


@pytest.mark.unit
def test_service_settings_rejects_invalid_log_level() -> None:
    """Reject a log level that Loguru does not support."""
    with pytest.raises(ValidationError):
        ServiceSettings.model_validate(
            {
                "oidc_issuer_url": "https://issuer.example.com",
                "oidc_jwks_url": "https://issuer.example.com/.well-known/jwks.json",
                "log_level": "not-a-level",
            }
        )


@pytest.mark.unit
def test_init_logging_uses_service_setting_when_level_is_omitted(
    monkeypatch: pytest.MonkeyPatch,
    restore_loguru_handlers: None,
) -> None:
    """Read the service setting at call time rather than capturing it at import time."""
    monkeypatch.setattr(service_settings, "log_level", LogLevel.WARNING)
    add = Mock(wraps=logger.add)
    monkeypatch.setattr(logger, "add", add)

    init_logging()

    assert add.call_args.kwargs["level"] == "WARNING"
