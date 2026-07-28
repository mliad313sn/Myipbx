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

from decimal import Decimal

from . import numerals, rating
from .logging_setup import get_logger

__all__ = [
    "WINDOWS",
    "resolve_window",
    "build_report",
    "build_queue_report",
    "export_csv",
    "export_queue_csv",
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
        "calls", "answered", "no_answer", "busy", "failed", "other", "voicemail",
        "inbound", "outbound", "internal",
        "inbound_answered", "outbound_answered",
        "inbound_seconds", "outbound_seconds",
        "conversation_seconds", "ring_seconds", "longest_seconds",
        "cost", "rated", "unrated",
    )

    def __init__(self) -> None:
        for field in self.__slots__:
            setattr(self, field, 0)

    def add(
        self,
        record: Mapping[str, Any],
        direction: str,
        cost: "Decimal | None" = None,
    ) -> None:
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

        # A call that ended in a mailbox and a call that rang out are both
        # recorded as unanswered, and they are not the same thing: one reached
        # somebody's attention and the other did not. The engine writes the
        # last application it ran, which is the only place the difference
        # survives.
        if str(record.get("application", "")).strip().lower() == "voicemail":
            self.voicemail += 1

        talk = record["billable_seconds"]
        # What is left of a call after the talking is the ringing. Taken as a
        # difference rather than from the answered-at stamp because that stamp
        # is empty on every call that was never answered, which is exactly the
        # set whose ring time matters most.
        ring = max(0, record["duration"] - talk)

        self.conversation_seconds += talk
        self.ring_seconds += ring
        self.longest_seconds = max(self.longest_seconds, talk)

        # Cost is counted only where there is one to count. A call with no
        # matching rate is unrated, which is a different fact from free, and
        # the report says which rather than adding a zero to the total.
        if direction == "outbound" and answered:
            if cost is None:
                self.unrated += 1
            else:
                self.rated += 1
                self.cost += cost

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


def _money(amount: Any, currency: str) -> dict[str, Any]:
    """One amount, as a number for the file and in words for the screen."""
    value = rating.round_money(Decimal(amount or 0))
    return {"count": str(value), "text": rating.spell_money(value, currency)}


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

#: The cost columns, appended to every breakdown only when the rate table can
#: produce a total anybody could act on. A column of zeroes on an appliance
#: with no rate table would read as "these calls were free".
COST_COLUMNS: tuple[tuple[str, str], ...] = (
    ("cost", "cost"),
    ("unrated", "unrated calls"),
)

#: The extra columns a per-extension row carries, which no other breakdown has
#: a meaningful direction for.
EXTENSION_COLUMNS: tuple[tuple[str, str], ...] = (
    ("inbound", "calls in"),
    ("inbound_answered", "answered in"),
    ("outbound", "calls out"),
    ("outbound_answered", "answered out"),
    ("voicemail", "left a message"),
)


def _row(
    key: str,
    label: str,
    tally: _Tally,
    extended: bool = False,
    currency: str = "",
) -> dict[str, Any]:
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
            "voicemail": _figure(tally.voicemail),
        })
    if currency:
        # A row whose calls were all unrated has no cost to show, and showing
        # it as zero would read as "these calls were free" -- which is the one
        # thing a cost report must never say by accident.
        if tally.rated == 0 and tally.unrated > 0:
            figures["cost"] = {"count": "", "text": "not rated"}
        else:
            figures["cost"] = _money(tally.cost, currency)
        figures["unrated"] = _figure(tally.unrated)
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
    tariffs: Iterable[Mapping[str, Any]] = (),
    truncated: bool = False,
    top: int = 20,
) -> dict[str, Any]:
    """Aggregate raw call records into everything the reports view shows."""
    first, last = window["from"], window["to"]

    # The rate table, if there is one. Costs are reported only when every rate
    # names the same currency: totalling two currencies into one figure would
    # produce a number that is not an amount of anything.
    rates = rating.load_tariffs(tariffs)
    currencies = rating.currencies_of(rates)
    currency = currencies[0] if len(currencies) == 1 else ""
    conflicting = currencies if len(currencies) > 1 else []

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
        cost = None
        if currency and direction == "outbound":
            matched = rating.match_tariff(rates, record.get("destination", ""))
            if matched is not None:
                cost = rating.charge(matched, record.get("billable_seconds", 0))

        overall.add(record, direction, cost)
        by_direction[direction].add(record, direction, cost)
        by_hour[moment.hour].add(record, direction, cost)
        by_day.setdefault(moment.date(), _Tally()).add(record, direction, cost)

        disposition = record["disposition"] or "UNRECORDED"
        by_disposition[disposition] = by_disposition.get(disposition, 0) + 1

        # An extension is counted once per call it took part in, on whichever
        # side it appears. A call between two configured extensions therefore
        # counts for both, which is what somebody reading a per-extension table
        # expects: it is a table of what each extension did, not a partition of
        # the calls.
        source, destination = record.get("source", ""), record.get("destination", "")
        if source in numbers:
            by_extension.setdefault(source, _Tally()).add(record, direction, cost)
        if destination in numbers and destination != source:
            by_extension.setdefault(destination, _Tally()).add(record, direction, cost)

        if destination and destination not in numbers:
            by_destination.setdefault(destination, _Tally()).add(record, direction, cost)

        trunk = record.get("context", "") or "unrecorded"
        by_trunk.setdefault(trunk, _Tally()).add(record, direction, cost)

    summary = {
        "calls": _figure(overall.calls),
        "answered": _figure(overall.answered),
        "missed": _figure(overall.missed),
        "no_answer": _figure(overall.no_answer),
        "busy": _figure(overall.busy),
        "failed": _figure(overall.failed),
        "voicemail": _figure(overall.voicemail),
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
    if currency:
        summary["cost"] = (
            {"count": "", "text": "not rated"}
            if overall.rated == 0 and overall.unrated > 0
            else _money(overall.cost, currency)
        )
        summary["rated"] = _figure(overall.rated)
        summary["unrated"] = _figure(overall.unrated)

    hours = [
        _row(f"{hour:02d}", _hour_label(hour), by_hour[hour], currency=currency)
        for hour in range(24)
    ]
    days = [
        _row(day.isoformat(), f"{_DAY_NAMES[day.weekday()]} {day.isoformat()}",
             by_day[day], currency=currency)
        for day in sorted(by_day)
    ]

    breakdowns = {
        "by_extension": _ranked([
            _row(number, catalogue.get(number, ""), tally, extended=True,
                 currency=currency)
            for number, tally in by_extension.items()
        ]),
        "by_destination": _ranked([
            _row(number, "", tally, currency=currency)
            for number, tally in by_destination.items()
        ], limit=top),
        "by_trunk": _ranked([
            _row(name, "", tally, currency=currency)
            for name, tally in by_trunk.items()
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
            "standard": [
                {"key": key, "heading": heading}
                for key, heading in BREAKDOWN_COLUMNS + (COST_COLUMNS if currency else ())
            ],
            "extension": [
                {"key": key, "heading": heading}
                for key, heading in EXTENSION_COLUMNS + BREAKDOWN_COLUMNS
                + (COST_COLUMNS if currency else ())
            ],
        },
        "currency": currency,
        "currency_conflict": conflicting,
        "currency_note": (
            "the rate table names more than one currency -- "
            + ", ".join(conflicting)
            + " -- so no cost is reported: adding two currencies together "
            "produces a number that is not an amount of anything"
        ) if conflicting else "",
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
# Queues
# ---------------------------------------------------------------------------
#
# Deliberately a separate report from everything above, because it is drawn
# from a separate file. A queue can be reported on when the call records are
# absent, and the call records can be reported on when no queue has ever been
# configured; folding the two together would make each unavailable whenever the
# other was.

#: What a queue row carries. Same shape as the call breakdowns, different
#: questions: a queue is judged on what happened to the people waiting.
QUEUE_COLUMNS: tuple[tuple[str, str], ...] = (
    ("offered", "offered"),
    ("answered", "answered"),
    ("abandoned", "abandoned"),
    ("answer_ratio", "answered per hundred"),
    ("service_level", "answered in time per hundred"),
    ("average_wait", "average wait"),
    ("longest_wait", "longest wait"),
    ("average_conversation", "average conversation"),
)

#: What a member row carries.
MEMBER_COLUMNS: tuple[tuple[str, str], ...] = (
    ("answered", "answered"),
    ("no_answer", "rang out"),
    ("pickup_ratio", "picked up per hundred"),
    ("conversation", "total conversation"),
    ("average_conversation", "average conversation"),
)

#: The share of answered calls that must be picked up inside the agreed number
#: of seconds. Twenty seconds is the number the field has settled on and the
#: engine's own default, and it is per queue rather than global because a
#: switchboard and an out-of-hours line are not held to the same promise.
DEFAULT_SERVICE_LEVEL_SECONDS = 20


class _QueueTally:
    """One queue's or one member's worth of counting."""

    __slots__ = (
        "offered", "answered", "abandoned", "no_answer",
        "wait_seconds", "longest_wait", "in_time",
        "conversation_seconds", "reasons",
    )

    def __init__(self) -> None:
        for field in self.__slots__:
            setattr(self, field, 0)
        self.reasons: dict[str, int] = {}

    @property
    def answer_ratio(self) -> float:
        return (self.answered / self.offered) if self.offered else 0.0

    @property
    def service_ratio(self) -> float:
        """Of the calls that were answered, the share answered in time.

        Measured against the answered calls rather than against everything
        offered. Both readings are defensible and they are not the same number,
        so the heading says which this is: a queue that answered ten of a
        hundred, all of them instantly, has a service level of a hundred and an
        answer rate of ten, and the pair together is the honest picture that
        either alone is not.
        """
        return (self.in_time / self.answered) if self.answered else 0.0

    @property
    def pickup_ratio(self) -> float:
        offered = self.answered + self.no_answer
        return (self.answered / offered) if offered else 0.0

    @property
    def average_wait(self) -> int:
        counted = self.answered + self.abandoned
        return round(self.wait_seconds / counted) if counted else 0

    @property
    def average_conversation(self) -> int:
        return round(self.conversation_seconds / self.answered) if self.answered else 0


def _queue_row(key: str, label: str, tally: _QueueTally) -> dict[str, Any]:
    return {
        "key": key,
        "label": label,
        "figures": {
            "offered": _figure(tally.offered),
            "answered": _figure(tally.answered),
            "abandoned": _figure(tally.abandoned),
            "answer_ratio": _ratio(tally.answer_ratio),
            "service_level": _ratio(tally.service_ratio),
            "average_wait": _seconds(tally.average_wait),
            "longest_wait": _seconds(tally.longest_wait),
            "average_conversation": _seconds(tally.average_conversation),
        },
    }


def _member_row(key: str, tally: _QueueTally) -> dict[str, Any]:
    return {
        "key": key,
        "label": "",
        "figures": {
            "answered": _figure(tally.answered),
            "no_answer": _figure(tally.no_answer),
            "pickup_ratio": _ratio(tally.pickup_ratio),
            "conversation": _seconds(tally.conversation_seconds),
            "average_conversation": _seconds(tally.average_conversation),
        },
    }


def build_queue_report(
    events: Sequence[Mapping[str, Any]],
    window: Mapping[str, Any],
    queues: Iterable[Mapping[str, Any]] = (),
    truncated: bool = False,
) -> dict[str, Any]:
    """Aggregate queue log events into what the queues panel shows."""
    first, last = window["from"], window["to"]

    catalogue = {
        str(entry.get("number", "")).strip(): str(entry.get("name", "")).strip()
        for entry in queues
        if str(entry.get("number", "")).strip()
    }
    thresholds = {
        str(entry.get("number", "")).strip(): int(
            entry.get("service_level_seconds") or DEFAULT_SERVICE_LEVEL_SECONDS
        )
        for entry in queues
        if str(entry.get("number", "")).strip()
    }

    overall = _QueueTally()
    by_queue: dict[str, _QueueTally] = {}
    by_member: dict[str, _QueueTally] = {}
    reasons: dict[str, int] = {}
    considered = 0

    for event in events:
        moment = event.get("at")
        if moment is None or not (first <= moment.date() <= last):
            continue

        name = event.get("event", "")
        queue = event.get("queue", "") or "unrecorded"
        member = _member_name(event.get("member", ""))
        tally = by_queue.setdefault(queue, _QueueTally())
        threshold = thresholds.get(queue, DEFAULT_SERVICE_LEVEL_SECONDS)

        if name == "ENTERQUEUE":
            considered += 1
            tally.offered += 1
            overall.offered += 1
        elif name == "CONNECT":
            waited = int(event.get("waited", 0))
            tally.answered += 1
            tally.wait_seconds += waited
            tally.longest_wait = max(tally.longest_wait, waited)
            overall.answered += 1
            overall.wait_seconds += waited
            overall.longest_wait = max(overall.longest_wait, waited)
            if waited <= threshold:
                tally.in_time += 1
                overall.in_time += 1
            if member:
                by_member.setdefault(member, _QueueTally()).answered += 1
        elif name in _ABANDON_REASONS:
            waited = int(event.get("waited", 0))
            tally.abandoned += 1
            tally.wait_seconds += waited
            tally.longest_wait = max(tally.longest_wait, waited)
            overall.abandoned += 1
            overall.wait_seconds += waited
            overall.longest_wait = max(overall.longest_wait, waited)
            reasons[name] = reasons.get(name, 0) + 1
        elif name in ("COMPLETEAGENT", "COMPLETECALLER"):
            talked = int(event.get("talked", 0))
            tally.conversation_seconds += talked
            overall.conversation_seconds += talked
            if member:
                by_member.setdefault(member, _QueueTally()).conversation_seconds += talked
        elif name == "RINGNOANSWER":
            if member:
                by_member.setdefault(member, _QueueTally()).no_answer += 1

    queue_rows = [
        _queue_row(number, catalogue.get(number, ""), tally)
        for number, tally in by_queue.items()
    ]
    queue_rows.sort(key=lambda row: (-row["figures"]["offered"]["count"], row["key"]))

    member_rows = [_member_row(name, tally) for name, tally in by_member.items()]
    member_rows.sort(key=lambda row: (-row["figures"]["answered"]["count"], row["key"]))

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
        "summary": {
            "offered": _figure(overall.offered),
            "answered": _figure(overall.answered),
            "abandoned": _figure(overall.abandoned),
            "answer_ratio": _ratio(overall.answer_ratio),
            "service_level": _ratio(overall.service_ratio),
            "average_wait": _seconds(overall.average_wait),
            "longest_wait": _seconds(overall.longest_wait),
            "average_conversation": _seconds(overall.average_conversation),
        },
        "breakdowns": {
            "by_queue": queue_rows,
            "by_member": member_rows,
            "by_reason": [
                {
                    "key": name,
                    "label": _ABANDON_REASONS[name],
                    "figures": {"calls": _figure(count)},
                }
                for name, count in sorted(reasons.items(), key=lambda pair: -pair[1])
            ],
        },
        "columns": {
            "queue": [{"key": key, "heading": heading} for key, heading in QUEUE_COLUMNS],
            "member": [{"key": key, "heading": heading} for key, heading in MEMBER_COLUMNS],
        },
        "explanation": "",
    }


#: Why a waiting caller stopped waiting. Kept apart rather than summed, because
#: a queue nobody is staffing and a queue people give up on are different
#: faults with different answers.
_ABANDON_REASONS = {
    "ABANDON": "the caller hung up while waiting",
    "EXITWITHTIMEOUT": "the caller waited longer than the queue allows",
    "EXITEMPTY": "no member was available to take the call",
    "EXITWITHKEY": "the caller pressed a key to leave the queue",
}


def _member_name(interface: str) -> str:
    """The extension behind a member interface.

    The engine records a member as a channel -- ``PJSIP/201`` -- and an
    operator reading a report is looking for the extension, which is what the
    rest of the console calls that person.
    """
    interface = (interface or "").strip()
    if not interface:
        return ""
    if "/" in interface:
        interface = interface.rsplit("/", 1)[1]
    return interface.split("@", 1)[0].split("-", 1)[0]


def queue_unavailable(explanation: str) -> dict[str, Any]:
    """What the queues panel looks like when there is no queue log."""
    return {
        "available": False,
        "window": {"name": "", "label": "", "from": "", "to": ""},
        "considered": _figure(0),
        "truncated": False,
        "summary": {},
        "breakdowns": {},
        "columns": {"queue": [], "member": []},
        "explanation": explanation,
    }


def export_queue_csv(report: Mapping[str, Any], breakdown: str) -> tuple[str, str]:
    """One queue breakdown as a file, in digits."""
    rows = (report.get("breakdowns") or {}).get(breakdown)
    if rows is None:
        raise ReportRefused(
            f"there is no queue breakdown named {breakdown}; the breakdowns "
            "are " + ", ".join(sorted((report.get("breakdowns") or {}).keys()))
        )

    if breakdown == "by_member":
        columns = report["columns"]["member"]
    elif breakdown == "by_queue":
        columns = report["columns"]["queue"]
    else:
        columns = [{"key": "calls", "heading": "calls"}]

    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    window = report.get("window") or {}
    writer.writerow([f"report: queues, {breakdown.replace('_', ' ')}"])
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
    return buffer.getvalue(), f"crossbar-queues-{breakdown.replace('_', '-')}-{stamp}.csv"


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
