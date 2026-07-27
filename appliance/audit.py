"""The record of who changed what, and from where.

Every read path in this appliance answers a question. Every write path changes
a telephone system, and some of them change the machine underneath it: a
firewall ruleset root loads, a certificate the console serves from, a service
that stops and takes the dialtone with it. Until now none of that left a trace
naming the person who asked for it.

That gap has two costs and they pull in opposite directions. An operator who
finds a trunk disabled on a Monday has no way to learn whether a colleague
disabled it on Friday or whether somebody else did. And an operator who is
accused of having disabled it has no way to show that they did not. A record
serves both of them, which is why it names the account and the address rather
than only the change.

What is written here is deliberately narrow. The line carries when, who, from
where, what was asked, and how it ended. It never carries a request body: the
bodies passing through the write routes contain passwords for telephones,
carrier account credentials, and private keys, and a record that captured them
would be a second copy of every secret on the appliance, in a file kept for
longer than any of them.

The journal is append only from this appliance's point of view -- nothing here
rewrites a line once written -- and rolls over by size so that it cannot fill
the state partition and take the appliance down with it. It is not tamper
proof: an intruder who has become root can edit any file on the machine. It is
a record of operations, not an evidentiary chain, and it says so here rather
than implying otherwise by silence.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from . import numerals
from .logging_setup import get_logger

__all__ = ["AuditJournal", "Entry"]

_LOG = get_logger("audit")

#: One journal, and one previous journal kept beside it. Two files bound the
#: space this takes on a partition that also holds voicemail.
_MAXIMUM_BYTES = 2 * 1024 * 1024

#: How many lines a read path will return at once. A console that asked for
#: everything would pull the whole file into memory to draw twenty rows.
_DEFAULT_LIMIT = 200


class Entry:
    """One recorded operation."""

    __slots__ = ("at", "actor", "source", "action", "target", "outcome", "detail")

    def __init__(
        self,
        at: float,
        actor: str,
        source: str,
        action: str,
        target: str,
        outcome: str,
        detail: str = "",
    ) -> None:
        self.at = at
        self.actor = actor
        self.source = source
        self.action = action
        self.target = target
        self.outcome = outcome
        self.detail = detail

    def as_line(self) -> str:
        return json.dumps(
            {
                "at": round(self.at, 3),
                "actor": self.actor,
                "source": self.source,
                "action": self.action,
                "target": self.target,
                "outcome": self.outcome,
                "detail": self.detail,
            },
            separators=(",", ":"),
        )

    def as_dict(self, now: float | None = None) -> dict[str, Any]:
        moment = time.time() if now is None else now
        return {
            "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.at)),
            "age": numerals.spell_duration(int(max(0.0, moment - self.at))),
            "actor": self.actor,
            "source": self.source,
            "action": self.action,
            "target": self.target,
            "outcome": self.outcome,
            "detail": self.detail,
        }


class AuditJournal:
    """Append one line per operation, and read the recent ones back."""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)
        self.previous_path = self.path.with_name(self.path.name + ".previous")

    # -- writing ------------------------------------------------------------

    def record(
        self,
        actor: str,
        source: str,
        action: str,
        target: str,
        outcome: str,
        detail: str = "",
    ) -> Entry:
        entry = Entry(
            at=time.time(),
            actor=actor or "an unidentified account",
            source=source or "an unrecorded address",
            action=action,
            target=target,
            outcome=outcome,
            detail=detail,
        )
        self._append(entry)
        return entry

    def _append(self, entry: Entry) -> None:
        """Write the line, and never let a failure here break the operation.

        A journal that could refuse a change by failing to record it would be a
        way of stopping the appliance being administered by filling a disk.
        The line is best effort; the failure to write one is itself logged.
        """
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._roll_if_large()
            descriptor = os.open(
                self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600
            )
            with os.fdopen(descriptor, "a", encoding="utf-8") as handle:
                handle.write(entry.as_line() + "\n")
        except OSError as error:
            _LOG.warning("an operation could not be recorded in the journal: %s", error)

    def _roll_if_large(self) -> None:
        try:
            size = self.path.stat().st_size
        except OSError:
            return
        if size < _MAXIMUM_BYTES:
            return
        try:
            os.replace(self.path, self.previous_path)
            os.chmod(self.previous_path, 0o600)
        except OSError as error:
            _LOG.warning("the journal could not be rolled over: %s", error)

    # -- reading ------------------------------------------------------------

    def recent(self, limit: int = _DEFAULT_LIMIT) -> list[dict[str, Any]]:
        """The most recent entries, newest first.

        Read from the end rather than the start. The interesting entry is
        almost always the last one, and a journal read from the start would
        make the console slower exactly as the file it is reading grows.
        """
        limit = max(1, min(int(limit), 1000))
        lines = self._tail(self.path, limit)
        if len(lines) < limit:
            lines = self._tail(self.previous_path, limit - len(lines)) + lines

        now = time.time()
        entries: list[dict[str, Any]] = []
        for line in reversed(lines):
            parsed = self._parse(line)
            if parsed is not None:
                entries.append(parsed.as_dict(now))
        return entries

    @staticmethod
    def _parse(line: str) -> Entry | None:
        try:
            raw = json.loads(line)
        except json.JSONDecodeError:
            return None
        if not isinstance(raw, dict):
            return None
        try:
            at = float(raw.get("at", 0.0))
        except (TypeError, ValueError):
            at = 0.0
        return Entry(
            at=at,
            actor=str(raw.get("actor", "")),
            source=str(raw.get("source", "")),
            action=str(raw.get("action", "")),
            target=str(raw.get("target", "")),
            outcome=str(raw.get("outcome", "")),
            detail=str(raw.get("detail", "")),
        )

    @staticmethod
    def _tail(path: Path, count: int) -> list[str]:
        """The last ``count`` lines of a file, without reading all of it."""
        if count <= 0:
            return []
        try:
            size = path.stat().st_size
        except OSError:
            return []

        block = 8192
        collected = b""
        try:
            with path.open("rb") as handle:
                position = size
                while position > 0 and collected.count(b"\n") <= count:
                    step = min(block, position)
                    position -= step
                    handle.seek(position)
                    collected = handle.read(step) + collected
        except OSError:
            return []

        text = collected.decode("utf-8", "replace")
        lines = [line for line in text.splitlines() if line.strip()]
        return lines[-count:]

    def summary(self) -> dict[str, Any]:
        entries = self.recent(limit=1)
        try:
            size = self.path.stat().st_size
        except OSError:
            size = 0
        return {
            "present": bool(entries),
            "latest": entries[0] if entries else None,
            "size": numerals.spell_integer(size) + " bytes",
        }
