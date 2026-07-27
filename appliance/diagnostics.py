"""Diagnostics: log reading and call detail records.

Reading logs and call history is the other half of "everything from the
interface". An operator who must open a terminal to see why a call failed does
not have a complete graphical interface, however good the configuration forms
are.

Both readers are strictly bounded. Only named files may be read, only from the
end, and only up to a line limit, so a request can neither wander the
filesystem nor pull an unbounded amount of a multiple gigabyte log into memory.
"""

from __future__ import annotations

import csv
import io
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from . import numerals
from .logging_setup import get_logger

__all__ = ["LogSource", "LogReader", "CallRecordReader", "DEFAULT_LOG_SOURCES"]

_LOG = get_logger("diagnostics")

_MAXIMUM_LINES = 2000
_READ_CHUNK = 65536

_LEVEL_PATTERN = re.compile(
    r"\b(DEBUG|INFO|INFORMATION|NOTICE|WARNING|WARN|ERROR|CRITICAL|VERBOSE)\b",
    re.IGNORECASE,
)

_LEVEL_NAMES = {
    "debug": "debug",
    "verbose": "debug",
    "info": "information",
    "information": "information",
    "notice": "information",
    "warn": "warning",
    "warning": "warning",
    "error": "error",
    "critical": "critical",
}


@dataclass(frozen=True)
class LogSource:
    """One log an operator is permitted to read from the interface."""

    key: str
    label: str
    path: str
    description: str = ""

    def as_dict(self, available: bool, size: int | None) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "path": self.path,
            "description": self.description,
            "available": available,
            "size": _spell_bytes(size) if size is not None else "unknown",
        }


#: The complete set of readable logs. A key absent from here cannot be read,
#: which is what keeps the log viewer from becoming a file browser.
DEFAULT_LOG_SOURCES: tuple[LogSource, ...] = (
    LogSource(
        "appliance", "appliance control plane", "/var/log/myipbx/appliance.log",
        "what the control plane itself did",
    ),
    LogSource(
        "installation", "installation", "/var/log/myipbx/installation.log",
        "what the installer did, stage by stage",
    ),
    LogSource(
        "engine", "telephony engine", "/var/log/asterisk/full",
        "what the telephony engine did, including call handling",
    ),
    LogSource(
        "engine-messages", "telephony engine messages", "/var/log/asterisk/messages",
        "the engine's notices, warnings, and errors",
    ),
)


class LogReader:
    """Reads the tail of an allowlisted log."""

    def __init__(
        self,
        sources: Iterable[LogSource] = DEFAULT_LOG_SOURCES,
        root: str | Path = "/",
    ) -> None:
        self.root = Path(root)
        self.sources = {source.key: source for source in sources}

    def _resolve(self, source: LogSource) -> Path:
        # The configured path is joined to the root so that the test suite can
        # point the whole reader at a fixture tree.
        relative = source.path.lstrip("/")
        return self.root / relative

    def catalogue(self) -> list[dict[str, Any]]:
        entries: list[dict[str, Any]] = []
        for source in self.sources.values():
            path = self._resolve(source)
            try:
                size = path.stat().st_size if path.is_file() else None
            except OSError:
                size = None
            entries.append(source.as_dict(available=size is not None, size=size))
        return entries

    def read(
        self,
        key: str,
        lines: int = 200,
        search: str = "",
        level: str = "",
    ) -> dict[str, Any]:
        """Return the last lines of a log, optionally filtered.

        The file is read backwards in chunks rather than from the beginning, so
        a log of any size costs the same to tail.
        """
        source = self.sources.get(key)
        if source is None:
            raise KeyError(f"there is no log named {key}")

        limit = max(1, min(int(lines or 200), _MAXIMUM_LINES))
        path = self._resolve(source)

        if not path.is_file():
            return {
                "key": key,
                "label": source.label,
                "available": False,
                "lines": [],
                "line_count": "zero",
                "explanation": (
                    "this log does not exist on this machine yet; it appears once "
                    "the component that writes it has run"
                ),
            }

        try:
            collected = self._tail(path, limit, search, level)
        except OSError as error:
            _LOG.error("the log named %s could not be read: %s", key, error)
            return {
                "key": key,
                "label": source.label,
                "available": False,
                "lines": [],
                "line_count": "zero",
                "explanation": f"this log could not be read: {error}",
            }

        return {
            "key": key,
            "label": source.label,
            "available": True,
            "lines": collected,
            "line_count": numerals.spell_integer(len(collected)),
            "filtered": bool(search or level),
            "explanation": "",
        }

    def _tail(self, path: Path, limit: int, search: str, level: str) -> list[dict[str, Any]]:
        wanted_level = _LEVEL_NAMES.get((level or "").strip().lower(), "")
        needle = (search or "").strip().lower()

        collected: list[dict[str, Any]] = []
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            position = handle.tell()
            remainder = b""

            while position > 0 and len(collected) < limit:
                step = min(_READ_CHUNK, position)
                position -= step
                handle.seek(position)
                chunk = handle.read(step) + remainder

                pieces = chunk.split(b"\n")
                # The first piece may be a partial line continued in the next
                # chunk further back, so it is carried rather than emitted.
                remainder = pieces[0]
                for raw in reversed(pieces[1:]):
                    entry = self._entry(raw)
                    if entry is None:
                        continue
                    if needle and needle not in entry["text"].lower():
                        continue
                    if wanted_level and entry["level"] != wanted_level:
                        continue
                    collected.append(entry)
                    if len(collected) >= limit:
                        break

            if position == 0 and remainder and len(collected) < limit:
                entry = self._entry(remainder)
                if entry is not None:
                    matches_search = not needle or needle in entry["text"].lower()
                    matches_level = not wanted_level or entry["level"] == wanted_level
                    if matches_search and matches_level:
                        collected.append(entry)

        collected.reverse()
        return collected

    @staticmethod
    def _entry(raw: bytes) -> dict[str, Any] | None:
        text = raw.decode("utf-8", "replace").rstrip("\r")
        if not text.strip():
            return None

        match = _LEVEL_PATTERN.search(text)
        level = _LEVEL_NAMES.get(match.group(1).lower(), "information") if match else "information"
        # Constraint Two applies to the log viewer as much as to the log: a
        # line from a component that does not spell its numerals is spelled
        # here before it reaches the operator's screen.
        return {"text": numerals.sanitize(text), "level": level}


class CallRecordReader:
    """Reads the telephony engine's call detail records."""

    #: The engine's own column order for its comma separated record file.
    COLUMNS = (
        "accountcode", "source", "destination", "context", "caller_identity",
        "channel", "destination_channel", "application", "data", "started_at",
        "answered_at", "ended_at", "duration", "billable_seconds",
        "disposition", "flags", "unique_identifier", "user_field",
    )

    def __init__(
        self,
        path: str | Path = "/var/log/asterisk/cdr-csv/Master.csv",
        root: str | Path = "/",
    ) -> None:
        self.root = Path(root)
        self.path = self.root / str(path).lstrip("/")

    def available(self) -> bool:
        return self.path.is_file()

    def read(self, limit: int = 100, search: str = "") -> dict[str, Any]:
        """Return the most recent call records, newest first."""
        limit = max(1, min(int(limit or 100), 1000))

        if not self.available():
            return {
                "available": False,
                "records": [],
                "record_count": "zero",
                "explanation": (
                    "the telephony engine is not writing call detail records to a "
                    "comma separated file on this machine; enable that module in "
                    "the engine to populate this view"
                ),
            }

        try:
            # Records are appended, so the tail is the recent history.  The
            # read is bounded by bytes as well as by lines.
            size = self.path.stat().st_size
            window = min(size, 4 * 1024 * 1024)
            with open(self.path, "rb") as handle:
                handle.seek(size - window)
                payload = handle.read(window).decode("utf-8", "replace")
        except OSError as error:
            _LOG.error("the call records could not be read: %s", error)
            return {
                "available": False,
                "records": [],
                "record_count": "zero",
                "explanation": f"the call records could not be read: {error}",
            }

        lines = payload.splitlines()
        if window < size and lines:
            # The first line is probably truncated by the window.
            lines = lines[1:]

        needle = (search or "").strip().lower()
        records: list[dict[str, Any]] = []

        for row in reversed(lines):
            if not row.strip():
                continue
            record = self._parse(row)
            if record is None:
                continue
            if needle and needle not in " ".join(str(value) for value in record.values()).lower():
                continue
            records.append(record)
            if len(records) >= limit:
                break

        answered = sum(1 for record in records if record["disposition"] == "ANSWERED")
        return {
            "available": True,
            "records": records,
            "record_count": numerals.spell_integer(len(records)),
            "answered_count": numerals.spell_integer(answered),
            "explanation": "",
        }

    def _parse(self, row: str) -> dict[str, Any] | None:
        try:
            values = next(csv.reader(io.StringIO(row)))
        except (csv.Error, StopIteration):
            return None
        if len(values) < 15:
            return None

        record = dict(zip(self.COLUMNS, values))
        try:
            duration = int(record.get("duration") or 0)
            billable = int(record.get("billable_seconds") or 0)
        except ValueError:
            duration, billable = 0, 0

        return {
            "source": numerals.sanitize(record.get("source", "")),
            "destination": numerals.sanitize(record.get("destination", "")),
            "caller_identity": numerals.sanitize(record.get("caller_identity", "")),
            "started_at": numerals.sanitize(record.get("started_at", "")),
            "duration": numerals.spell_duration(max(0, duration)),
            "talk_time": numerals.spell_duration(max(0, billable)),
            "disposition": record.get("disposition", "").strip().upper(),
            "answered": record.get("disposition", "").strip().upper() == "ANSWERED",
            "unique_identifier": record.get("unique_identifier", ""),
        }


_UNITS = (
    (1024 ** 3, "gibibytes"),
    (1024 ** 2, "mebibytes"),
    (1024, "kibibytes"),
)


def _spell_bytes(value: int | None) -> str:
    if not value:
        return "zero bytes"
    for size, name in _UNITS:
        if value >= size:
            return f"{numerals.spell_decimal(value / size, 1)} {name}"
    return f"{numerals.spell_integer(value)} bytes"


def describe_sources(sources: Mapping[str, LogSource] | None = None) -> list[dict[str, Any]]:
    """Describe the readable logs without touching the filesystem."""
    chosen = sources.values() if sources else DEFAULT_LOG_SOURCES
    return [
        {"key": source.key, "label": source.label, "description": source.description}
        for source in chosen
    ]
