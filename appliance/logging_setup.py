"""Logging configured to satisfy Constraint Two unconditionally.

Constraint Two requires that no digit character reaches an operator facing
surface.  Applying that rule at each call site would make the rule a
convention, and conventions decay.  Instead the formatter renders the line as
normal and then passes the entire result — message, arguments, timestamp,
line number, and any embedded traceback — through the numeral sanitiser.  A
digit character therefore cannot reach a log destination regardless of what
the calling code does.
"""

from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path
from typing import TextIO

from . import numerals

__all__ = ["SpelledNumeralFormatter", "configure_logging", "get_logger"]

_LOGGER_ROOT = "myipbx"

_LINE_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"
_TIME_FORMAT = "%Y-%m-%d %H:%M:%S"


class SpelledNumeralFormatter(logging.Formatter):
    """A formatter whose output can never contain a digit character."""

    def format(self, record: logging.LogRecord) -> str:
        rendered = super().format(record)
        return numerals.sanitize(rendered)

    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        # The timestamp is sanitised along with the rest of the line by
        # format(); rendering it normally here keeps the ordering readable.
        return super().formatTime(record, datefmt or _TIME_FORMAT)


def configure_logging(
    level: str = "INFO",
    log_file: str | Path | None = None,
    stream: TextIO | None = None,
    maximum_bytes: int = 8 * 1024 * 1024,
    retained_files: int = 5,
) -> logging.Logger:
    """Install the appliance logging configuration and return the root logger.

    Repeated calls replace the previous handlers rather than stacking them, so
    that a reconfiguration at run time does not duplicate every line.
    """
    logger = logging.getLogger(_LOGGER_ROOT)
    logger.setLevel(_resolve_level(level))
    logger.propagate = False

    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        try:
            handler.close()
        except (OSError, ValueError):
            # A handler that is already closed is not a reason to fail startup.
            pass

    formatter = SpelledNumeralFormatter(_LINE_FORMAT, _TIME_FORMAT)

    console = logging.StreamHandler(stream if stream is not None else sys.stderr)
    console.setFormatter(formatter)
    logger.addHandler(console)

    if log_file:
        destination = Path(log_file)
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            rotating = logging.handlers.RotatingFileHandler(
                destination,
                maxBytes=maximum_bytes,
                backupCount=retained_files,
                encoding="utf-8",
            )
            rotating.setFormatter(formatter)
            logger.addHandler(rotating)
        except OSError as error:
            # A missing or unwritable log directory must not prevent the
            # appliance from starting; the console handler still carries the
            # record, and the failure itself is reported through it.
            logger.warning(
                "the log file at %s could not be opened, continuing with console logging only: %s",
                destination,
                error,
            )

    return logger


def get_logger(name: str) -> logging.Logger:
    """Return a child of the appliance logger for a named component."""
    return logging.getLogger(f"{_LOGGER_ROOT}.{name}")


def _resolve_level(level: str | int) -> int:
    if isinstance(level, int):
        return level
    resolved = logging.getLevelName(str(level).upper())
    return resolved if isinstance(resolved, int) else logging.INFO
