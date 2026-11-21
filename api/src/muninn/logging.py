"""Provide shared exception logging for Buzz callbacks and miscellaneous exception logging."""

import sys
from collections.abc import Mapping

from buzz import DoExceptParams
from loguru import logger

from muninn.config import service_settings
from muninn.constants import LogLevel


def init_logging() -> None:
    """Configure the shared Loguru stderr handler for the service."""
    requested_level = service_settings.log_level
    logger.level(requested_level.value)
    if requested_level != LogLevel.INFO:
        logger.remove()
        logger.add(sys.stderr, level=requested_level.value)


def log_error(exception: BaseException, message: str, *, context: Mapping[str, object] | None = None) -> None:
    """Log an exception with its type, original text, and traceback at error level."""
    bound_logger = logger.bind(**context) if context is not None else logger
    bound_logger.opt(exception=(type(exception), exception, exception.__traceback__)).error(message)


def log_buzz_error(params: DoExceptParams) -> None:
    """Provide a logging callback for py-buzz `do_except` kwargs."""
    log_error(params.err, params.final_message)
