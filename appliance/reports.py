"""Reports: the questions an owner asks, rather than the calls an engineer reads.

A private branch exchange is not finished when calls connect. Somebody has to
answer, every month, to somebody who is not a telephony engineer: how many calls
did we take, how many did we miss, which extension never picks up, what hour are
we busiest, is the average conversation getting longer. None of those is a call,
and none of them can be answered from a list of the last hundred.

Everything here aggregates the same call detail records the history view reads,
through the same reader, so a figure in a report and a row in the history can
never come from two different parsings of the same line.

Two terms of art are used throughout, because they are what the field judges a
telephony report on:

**Answer seizure ratio** -- of the calls attempted, the share that were
answered. The single number that says whether a system is working.

**Average length of conversation** -- the mean talk time of the calls that were
*answered*. Deliberately not the mean duration of every call: a thousand
unanswered calls of six seconds each would drag that toward zero and say
nothing about how long people actually talk.

On numerals. Every figure is carried twice: once as an integer, and once
spelled. The screen reads the spelled form, because Constraint Two governs what
an operator reads. The chart geometry and the exported file read the integer,
because a bar cannot be drawn from a word and a spreadsheet cannot sum one.
Identifiers -- an extension, a dialled number, a trunk -- keep their digits in
both, as they do everywhere else in this appliance.
"""

from __future__ import annotations

import csv
import io
from datetime import date, datetime, timedelta
from typing import Any, Iterable, Mapping, Sequence

from . import numerals
from .logging_setup import get_logger

__all__ = [
    "WINDOWS",
    "resolve_window",
    "build_report",
    "export_csv",
    "ReportRefused",
]

_LOG = get_logger("reports")

#: How the engine writes the moment a call started.
_STAMP = "%Y-%m-%d %H:%M:%S"

#: Dispositions the engine records, and the order a report lists them in.
#: Anything the engine writes that is not in this list is still counted, under
#: the name the engine used, rather than dropped into an "other" bucket that
#: hides it.
_DISPOSITIONS = ("ANSWERED", "NO ANSWER", "BUSY", "FAILED", "CONGESTION", "CANCEL")

_DAY_NAMES = (
    "Monday", "Tuesday", "Wednesday", "Thursday",
    "Friday", "Saturday", "Sunday",
)


class ReportRefused(ValueError):
    """A report that was asked for in a way it cannot be produced."""


# ---------------------------------------------------------------------------
# The window
# ---------------------------------------------------------------------------

#: The named windows, in the order the console offers them.
#:
#: Named rather than typed wherever a name will do. An operator asking "how did
#: last month go" should not have to work out what last month's first and last
#: day were, and a typed date is a place to make a mistake that a report then
#: presents as a fact.
WINDOWS: tuple[tuple[str, str], ...] = (
    ("today", "today"),
    ("yesterday", "yesterday"),
    ("last-seven-days", "the last seven days"),
    ("this-month", "this month"),
    ("last-month", "last month"),
    ("last-thirty-days", "the last thirty days"),
    ("everything", "everything on record"),
    ("between", "between two dates"),
)

_WINDOW_LABELS = dict(WINDOWS)


def _today(now: datetime | None = None) -> date:
    return (now or datetime.now()).date()


def _month_start(day: date) -> date:
    return day.replace(day=1)


def _previous_month_start(day: date) -> date:
    first = _month_start(day)
    return _month_start(first - timedelta(days=1))


def resolve_window(
    name: str = "last-seven-days",
    since: str = "",
    until: str = "",
    now: datetime | None = None,
) -> dict[str, Any]:
    """Turn a window name, or a pair of dates, into a closed range of days.

    The range is inclusive at both ends, because that is how somebody asking
    for "the first to the seventh" means it, and an exclusive end silently
    drops a day's calls out of every report drawn that way.
    """
    name = (name or "last-seven-days").strip().lower()
    if name not in _WINDOW_LABELS:
        raise ReportRefused(
            f"there is no report window named {name}; the windows are "
            + ", ".join(key for key, _ in WINDOWS)
        )

    today = _today(now)

    if name == "between":
        first = _parse_day(since, "the first day")
        last = _parse_day(until, "the last day")
        if last < first:
            raise ReportRefused(
                "the last day of the window falls before the first; a report "
                "cannot be drawn over a period that runs backwards"
            )
        label = f"between {first.isoformat()} and {last.isoformat()}"
        return {"name": name, "label": label, "from": first, "to": last}

    if name == "today":
        first = last = today
    elif name == "yesterday":
        first = last = today - timedelta(days=1)
    elif name == "last-seven-days":
        first, last = today - timedelta(days=6), today
    elif name == "last-thirty-days":
        first, last = today - timedelta(days=29), today
    elif name == "this-month":
        first, last = _month_start(today), today
    elif name == "last-month":
        first = _previous_month_start(today)
        last = _month_start(today) - timedelta(days=1)
    else:  # everything
        first, last = date.min, date.max

    if name == "everything":
        label = _WINDOW_LABELS[name]
    else:
        label = f"{_WINDOW_LABELS[name]}, {first.isoformat()} to {last.isoformat()}"
    return {"name": name, "label": label, "from": first, "to": last}


def _parse_day(value: str, which: str) -> date:
    text = (value or "").strip()
    if not text:
        raise ReportRefused(f"{which} of the window was not given")
    try:
        return date.fromisoformat(text)
    except ValueError as error:
        raise ReportRefused(
            f"{which} of the window, {text}, is not a date written as "
            "year, month and day separated by hyphens"
        ) from error


# ---------------------------------------------------------------------------
# Counting
# ---------------------------------------------------------------------------


class _Tally:
    """One row's worth of counting, before it is presented.

    Kept as a small object rather than a dictionary of running totals because
    every breakdown counts exactly the same things, and a report whose
    per-extension answer rate is computed differently from its per-hour answer
    rate is a report nobody can reconcile.
    """

    __slots__ = (
        "calls", "answered", "no_answer", "busy", "failed", "other",
        "inbound", "outbound", "internal",
        "inbound_answered", "outbound_answered",
        "inbound_seconds", "outbound_seconds",
        "conversation_seconds", "ring_seconds", "longest_seconds",
    )

    def __init__(self) -> None:
        for field in self.__slots__:
            setattr(self, field, 0)

    def add(self, record: Mapping[str, Any], direction: str) -> None:
        self.calls += 1
        disposition = record["disposition"]
        answered = disposition == "ANSWERED"

        if answered:
            self.answered += 1
        elif disposition == "NO ANSWER":
            self.no_answer += 1
        elif disposition == "BUSY":
            self.busy += 1
        elif disposition in ("FAILED", "CONGESTION"):
            self.failed += 1
        else:
            self.other += 1

        talk = record["billable_seconds"]
        # What is left of a call after the talking is the ringing. Taken as a
        # difference rather than from the answered-at stamp because that stamp
        # is empty on every call that was never answered, which is exactly the
        # set whose ring time matters most.
        ring = max(0, record["duration"] - talk)

        self.conversation_seconds += talk
        self.ring_seconds += ring
        self.longest_seconds = max(self.longest_seconds, talk)

        if direction == "inbound":
            self.inbound += 1
            self.inbound_seconds += talk
            if answered:
                self.inbound_answered += 1
        elif direction == "outbound":
            self.outbound += 1
            self.outbound_seconds += talk
            if answered:
                self.outbound_answered += 1
        else:
            self.internal += 1

    @property
    def missed(self) -> int:
        """Everything that was not answered, however it failed to be."""
        return self.calls - self.answered

    @property
    def answer_ratio(self) -> float:
        return (self.answered / self.calls) if self.calls else 0.0

    @property
    def average_conversation(self) -> int:
        return round(self.conversation_seconds / self.answered) if self.answered else 0

    @property
    def average_ring(self) -> int:
        return round(self.ring_seconds / self.calls) if self.calls else 0


def _figure(value: int) -> dict[str, Any]:
    return {"count": value, "text": numerals.spell_integer(value)}


def _seconds(value: int) -> dict[str, Any]:
    return {"count": value, "text": numerals.spell_duration(max(0, value))}


def _ratio(value: float) -> dict[str, Any]:
    """A share of one, presented as a share of a hundred.

    Rounded to one place, because a report that claims an answer rate to two
    decimal places is claiming a precision its own rounding of seconds does not
    have.
    """
    percent = round(value * 100, 1)
    return {
        "count": percent,
        "text": f"{numerals.spell_decimal(percent, 1)} in every hundred",
    }


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


def _classify(
    record: Mapping[str, Any],
    extensions: frozenset[str],
    inbound_context: str,
    internal_context: str,
) -> str:
    """Which way a call went.

    Decided from the dialplan context the engine recorded, because that is what
    the appliance itself configured and so is the only answer that cannot drift
    from the routing. Where the context says nothing useful, the extension list
    decides: a call from a configured extension to something that is not one
    went out, and the reverse came in.
    """
    context = record.get("context", "")
    if inbound_context and context == inbound_context:
        return "inbound"
    if internal_context and context == internal_context:
        return "internal"

    source_is_extension = record.get("source", "") in extensions
    destination_is_extension = record.get("destination", "") in extensions

    if source_is_extension and destination_is_extension:
        return "internal"
    if source_is_extension:
        return "outbound"
    if destination_is_extension:
        return "inbound"
    return "outbound"


def _within(record: Mapping[str, Any], first: date, last: date) -> datetime | None:
    """The moment a call started, if it started inside the window."""
    stamp = record.get("started_at", "")
    if not stamp:
        return None
    try:
        moment = datetime.strptime(stamp[:19], _STAMP)
    except ValueError:
        return None
    if not (first <= moment.date() <= last):
        return None
    return moment


# ---------------------------------------------------------------------------
# The report
# ---------------------------------------------------------------------------

#: What a breakdown row carries, and the heading each column gets.
#:
#: One list, used by the table on the screen, by the exported file and by the
#: test that checks the two agree, so a column can never be added to one and
#: forgotten in the others.
BREAKDOWN_COLUMNS: tuple[tuple[str, str], ...] = (
    ("calls", "calls"),
    ("answered", "answered"),
    ("missed", "missed"),
    ("answer_ratio", "answered per hundred"),
    ("conversation", "total conversation"),
    ("average_conversation", "average conversation"),
)

#: The extra columns a per-extension row carries, which no other breakdown has
#: a meaningful direction for.
EXTENSION_COLUMNS: tuple[tuple[str, str], ...] = (
    ("inbound", "calls in"),
    ("inbound_answered", "answered in"),
    ("outbound", "calls out"),
    ("outbound_answered", "answered out"),
)


def _row(key: str, label: str, tally: _Tally, extended: bool = False) -> dict[str, Any]:
    figures: dict[str, Any] = {
        "calls": _figure(tally.calls),
        "answered": _figure(tally.answered),
        "missed": _figure(tally.missed),
        "answer_ratio": _ratio(tally.answer_ratio),
        "conversation": _seconds(tally.conversation_seconds),
        "average_conversation": _seconds(tally.average_conversation),
    }
    if extended:
        figures.update({
            "inbound": _figure(tally.inbound),
            "inbound_answered": _figure(tally.inbound_answered),
            "outbound": _figure(tally.outbound),
            "outbound_answered": _figure(tally.outbound_answered),
        })
    return {"key": key, "label": label, "figures": figures}


def _ranked(rows: list[dict[str, Any]], limit: int = 0) -> list[dict[str, Any]]:
    rows.sort(key=lambda row: (-row["figures"]["calls"]["count"], row["key"]))
    return rows[:limit] if limit else rows


def build_report(
    records: Sequence[Mapping[str, Any]],
    window: Mapping[str, Any],
    extensions: Iterable[Mapping[str, Any]] = (),
    inbound_context: str = "",
    internal_context: str = "",
    truncated: bool = False,
    top: int = 20,
) -> dict[str, Any]:
    """Aggregate raw call records into everything the reports view shows."""
    first, last = window["from"], window["to"]

    catalogue = {
        str(entry.get("number", "")).strip(): str(entry.get("name", "")).strip()
        for entry in extensions
        if str(entry.get("number", "")).strip()
    }
    numbers = frozenset(catalogue)

    overall = _Tally()
    by_extension: dict[str, _Tally] = {}
    by_destination: dict[str, _Tally] = {}
    by_trunk: dict[str, _Tally] = {}
    by_hour: dict[int, _Tally] = {hour: _Tally() for hour in range(24)}
    by_day: dict[date, _Tally] = {}
    by_disposition: dict[str, int] = {}
    by_direction: dict[str, _Tally] = {
        "inbound": _Tally(), "outbound": _Tally(), "internal": _Tally(),
    }

    considered = 0
    for record in records:
        moment = _within(record, first, last)
        if moment is None:
            continue
        considered += 1

        direction = _classify(record, numbers, inbound_context, internal_context)
        overall.add(record, direction)
        by_direction[direction].add(record, direction)
        by_hour[moment.hour].add(record, direction)
        by_day.setdefault(moment.date(), _Tally()).add(record, direction)

        disposition = record["disposition"] or "UNRECORDED"
        by_disposition[disposition] = by_disposition.get(disposition, 0) + 1

        # An extension is counted once per call it took part in, on whichever
        # side it appears. A call between two configured extensions therefore
        # counts for both, which is what somebody reading a per-extension table
        # expects: it is a table of what each extension did, not a partition of
        # the calls.
        source, destination = record.get("source", ""), record.get("destination", "")
        if source in numbers:
            by_extension.setdefault(source, _Tally()).add(record, direction)
        if destination in numbers and destination != source:
            by_extension.setdefault(destination, _Tally()).add(record, direction)

        if destination and destination not in numbers:
            by_destination.setdefault(destination, _Tally()).add(record, direction)

        trunk = record.get("context", "") or "unrecorded"
        by_trunk.setdefault(trunk, _Tally()).add(record, direction)

    summary = {
        "calls": _figure(overall.calls),
        "answered": _figure(overall.answered),
        "missed": _figure(overall.missed),
        "no_answer": _figure(overall.no_answer),
        "busy": _figure(overall.busy),
        "failed": _figure(overall.failed),
        "answer_ratio": _ratio(overall.answer_ratio),
        "conversation": _seconds(overall.conversation_seconds),
        "average_conversation": _seconds(overall.average_conversation),
        "longest_call": _seconds(overall.longest_seconds),
        "ringing": _seconds(overall.ring_seconds),
        "average_ring": _seconds(overall.average_ring),
        "inbound": _figure(by_direction["inbound"].calls),
        "outbound": _figure(by_direction["outbound"].calls),
        "internal": _figure(by_direction["internal"].calls),
    }

    hours = [
        _row(f"{hour:02d}", _hour_label(hour), by_hour[hour])
        for hour in range(24)
    ]
    days = [
        _row(day.isoformat(), f"{_DAY_NAMES[day.weekday()]} {day.isoformat()}", by_day[day])
        for day in sorted(by_day)
    ]

    breakdowns = {
        "by_extension": _ranked([
            _row(number, catalogue.get(number, ""), tally, extended=True)
            for number, tally in by_extension.items()
        ]),
        "by_destination": _ranked([
            _row(number, "", tally) for number, tally in by_destination.items()
        ], limit=top),
        "by_trunk": _ranked([
            _row(name, "", tally) for name, tally in by_trunk.items()
        ]),
        "by_hour": hours,
        "by_day": days,
        "by_disposition": [
            _row(name, "", _disposition_tally(count))
            for name, count in _ordered_dispositions(by_disposition)
        ],
    }

    return {
        "available": True,
        "window": {
            "name": window["name"],
            "label": window["label"],
            "from": "" if first == date.min else first.isoformat(),
            "to": "" if last == date.max else last.isoformat(),
        },
        "considered": _figure(considered),
        "truncated": truncated,
        "truncation_note": (
            "the call record file holds more calls than one sweep reads, so "
            "this report covers the oldest part of the file only; narrow the "
            "window or archive the older records before relying on these "
            "figures"
        ) if truncated else "",
        "summary": summary,
        "breakdowns": breakdowns,
        "columns": {
            "standard": [{"key": key, "heading": heading} for key, heading in BREAKDOWN_COLUMNS],
            "extension": [
                {"key": key, "heading": heading}
                for key, heading in EXTENSION_COLUMNS + BREAKDOWN_COLUMNS
            ],
        },
        "explanation": "",
    }


def _hour_label(hour: int) -> str:
    """An hour of the day, named the way somebody says it out loud."""
    following = (hour + 1) % 24
    return f"{hour:02d}:00 to {following:02d}:00"


def _disposition_tally(count: int) -> _Tally:
    tally = _Tally()
    tally.calls = count
    return tally


def _ordered_dispositions(counts: Mapping[str, int]) -> list[tuple[str, int]]:
    """Known dispositions in their usual order, then anything else by volume."""
    known = [(name, counts[name]) for name in _DISPOSITIONS if name in counts]
    rest = sorted(
        ((name, count) for name, count in counts.items() if name not in _DISPOSITIONS),
        key=lambda pair: (-pair[1], pair[0]),
    )
    return known + rest


def unavailable(explanation: str) -> dict[str, Any]:
    """What a report looks like when there is nothing to draw one from."""
    return {
        "available": False,
        "window": {"name": "", "label": "", "from": "", "to": ""},
        "considered": _figure(0),
        "truncated": False,
        "truncation_note": "",
        "summary": {},
        "breakdowns": {},
        "columns": {"standard": [], "extension": []},
        "explanation": explanation,
    }


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


def export_csv(report: Mapping[str, Any], breakdown: str) -> tuple[str, str]:
    """One breakdown as a comma separated file, and the name to save it under.

    The file carries digits, not the spelled words the screen carries. This is
    the same distinction the appliance draws everywhere: what a person reads is
    prose and is spelled, and what a machine reads is not. A spreadsheet cannot
    sum "one thousand two hundred forty-three", so a report exported in words
    is a report that cannot be used for the thing anybody exports a report for.
    """
    rows = (report.get("breakdowns") or {}).get(breakdown)
    if rows is None:
        raise ReportRefused(
            f"there is no report breakdown named {breakdown}; the breakdowns "
            "are " + ", ".join(sorted((report.get("breakdowns") or {}).keys()))
        )

    columns = report["columns"]["extension" if breakdown == "by_extension" else "standard"]
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")

    window = report.get("window") or {}
    writer.writerow([f"report: {breakdown.replace('_', ' ')}"])
    writer.writerow([f"window: {window.get('label', '')}"])
    writer.writerow([])
    writer.writerow(["key", "label"] + [column["heading"] for column in columns])

    for row in rows:
        figures = row["figures"]
        writer.writerow(
            [row["key"], row["label"]]
            + [figures.get(column["key"], {}).get("count", "") for column in columns]
        )

    stamp = (window.get("from") or "all") + "-to-" + (window.get("to") or "all")
    return buffer.getvalue(), f"crossbar-{breakdown.replace('_', '-')}-{stamp}.csv"


def export_records_csv(
    records: Sequence[Mapping[str, Any]],
    window: Mapping[str, Any],
) -> tuple[str, str]:
    """Every call inside the window, one per line, as the engine recorded it."""
    first, last = window["from"], window["to"]
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow([
        "started at", "source", "destination", "caller identity", "context",
        "duration seconds", "conversation seconds", "disposition",
        "unique identifier",
    ])

    kept = 0
    for record in records:
        if _within(record, first, last) is None:
            continue
        kept += 1
        writer.writerow([
            record.get("started_at", ""),
            record.get("source", ""),
            record.get("destination", ""),
            record.get("caller_identity", ""),
            record.get("context", ""),
            record.get("duration", 0),
            record.get("billable_seconds", 0),
            record.get("disposition", ""),
            record.get("unique_identifier", ""),
        ])

    _LOG.info("exported %s call records", kept)
    stamp = (
        ("all" if first == date.min else first.isoformat())
        + "-to-"
        + ("all" if last == date.max else last.isoformat())
    )
    return buffer.getvalue(), f"crossbar-calls-{stamp}.csv"
