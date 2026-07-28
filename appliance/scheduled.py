"""Reports the appliance draws for itself, keeps, and sends.

Every report elsewhere in this appliance is pulled by a person looking at a
screen. These are pushed: somebody said "the switchboard's figures, every
Monday", and after that nobody has to remember.

Two things are kept apart on purpose.

**Drawing and keeping come first, sending second.** The snapshot is written to
the appliance's own state before any attempt is made to send it, so a mail
server that is down costs the site a delivery and not the report. The report is
still there on Tuesday when somebody looks.

**Due-ness is decided against a date, not against an interval.** A weekly
report set to run on a Monday runs on the Monday, once, whether the appliance
was restarted eleven times over the weekend or ran without interruption. An
interval counted from the last run drifts, and a report that arrives on
Wednesday having been asked for on Monday is a report nobody trusts.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable, Mapping

from . import numerals
from .logging_setup import get_logger

__all__ = ["ScheduleStore", "is_due", "snapshot_name", "FREQUENCIES"]

_LOG = get_logger("scheduled")

#: How often a scheduled report may run, and what "due" means for each.
FREQUENCIES = ("daily", "weekly", "monthly")

#: The most snapshots kept for one report before the oldest are removed. A
#: scheduled report is a habit rather than an archive, and an appliance that
#: filled its own state directory with two years of unread files would be doing
#: the site harm on its own initiative.
KEEP_SNAPSHOTS = 60


def is_due(frequency: str, last_run: str, today: date) -> bool:
    """Whether a report should run today, given when it last did.

    Decided against the calendar rather than against an elapsed interval, so a
    weekly report runs on its day and not on whichever day happens to fall
    seven days after the appliance was last restarted.
    """
    frequency = (frequency or "daily").strip().lower()
    if frequency not in FREQUENCIES:
        return False

    if last_run:
        try:
            previous = date.fromisoformat(last_run[:10])
        except ValueError:
            previous = None
        if previous is not None and previous >= today:
            # Already run today. Restarting the appliance does not produce a
            # second copy of the same report.
            return False

    if frequency == "daily":
        return True
    if frequency == "weekly":
        return today.weekday() == 0
    return today.day == 1


def snapshot_name(name: str, window: Mapping[str, Any]) -> str:
    """What one run of one scheduled report is filed under."""
    first = window.get("from") or "all"
    last = window.get("to") or "all"
    safe = "".join(
        character if character.isalnum() or character in "._-" else "-"
        for character in str(name)
    )
    # A leading dot would make the file hidden, and a leading pair of them
    # reads as a parent directory to anybody skimming a listing. The entity's
    # own pattern already forbids both; this is the second lock on the door.
    safe = safe.lstrip(".") or "report"
    return f"{safe}_{first}_to_{last}.csv"


class ScheduleStore:
    """Where the snapshots live, and what ran when.

    The record of what ran is a small file beside the snapshots rather than a
    field on the configuration document, because it is not configuration: it is
    something the appliance did, and writing it into the source of truth would
    make every scheduled run look like a change somebody made.
    """

    def __init__(self, directory: str | Path) -> None:
        self.directory = Path(directory)
        self.ledger = self.directory / "last-run.json"

    def prepare(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)

    # -- what ran when -----------------------------------------------------

    def last_runs(self) -> dict[str, str]:
        try:
            payload = json.loads(self.ledger.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return {str(key): str(value) for key, value in payload.items()} \
            if isinstance(payload, dict) else {}

    def record_run(self, name: str, when: datetime) -> None:
        runs = self.last_runs()
        runs[name] = when.date().isoformat()
        self.prepare()
        try:
            # Written beside and moved into place, so an appliance that loses
            # power mid-write does not come back with a ledger it cannot read
            # and therefore run every report again.
            temporary = self.ledger.with_suffix(".json.partial")
            temporary.write_text(json.dumps(runs, indent=2, sort_keys=True),
                                 encoding="utf-8")
            temporary.replace(self.ledger)
        except OSError as error:
            _LOG.error("the scheduled report ledger could not be written: %s", error)

    # -- the snapshots themselves ------------------------------------------

    def write(self, name: str, payload: str) -> Path:
        self.prepare()
        path = self.directory / name
        path.write_text(payload, encoding="utf-8")
        return path

    def list(self) -> list[dict[str, Any]]:
        """Every snapshot on the appliance, newest first."""
        if not self.directory.is_dir():
            return []
        found: list[dict[str, Any]] = []
        for entry in sorted(self.directory.iterdir(), reverse=True):
            if not entry.is_file() or entry.suffix != ".csv":
                continue
            try:
                stat = entry.stat()
            except OSError:
                continue
            found.append({
                "name": entry.name,
                "at": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
                "size": numerals.spell_integer(max(0, stat.st_size // 1024))
                        + " kibibytes",
            })
        return found

    def read(self, name: str) -> str | None:
        """One snapshot, by name, or nothing.

        The same two rules the recordings store applies, for the same reason:
        this hands a file off the disk in response to a name from outside.
        """
        candidate = (name or "").strip()
        if not candidate.endswith(".csv") or "/" in candidate or "\\" in candidate:
            return None
        if candidate != Path(candidate).name or candidate.startswith("."):
            return None
        try:
            path = (self.directory / candidate).resolve()
            directory = self.directory.resolve()
        except OSError:
            return None
        if not path.is_file() or directory not in path.parents:
            return None
        try:
            return path.read_text(encoding="utf-8")
        except OSError as error:
            _LOG.error("a report snapshot could not be read: %s", error)
            return None

    def prune(self, keep: int = KEEP_SNAPSHOTS) -> int:
        """Keep the newest snapshots and remove the rest."""
        snapshots = self.list()
        removed = 0
        for entry in snapshots[keep:]:
            try:
                (self.directory / entry["name"]).unlink()
                removed += 1
            except OSError as error:
                _LOG.error("an old report snapshot could not be removed: %s", error)
        return removed


def due_reports(
    schedules: Iterable[Mapping[str, Any]],
    last_runs: Mapping[str, str],
    today: date,
) -> list[Mapping[str, Any]]:
    """The enabled schedules that should run today."""
    return [
        schedule for schedule in schedules or ()
        if schedule.get("enabled", True)
        and str(schedule.get("name", "")).strip()
        and is_due(
            str(schedule.get("frequency", "daily")),
            last_runs.get(str(schedule.get("name", "")).strip(), ""),
            today,
        )
    ]
