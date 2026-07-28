"""The engine's queue log, which answers questions the call records cannot.

A call that waited four minutes in a queue and then gave up appears in the call
detail records as one unanswered call, and nothing in that record says it
waited at all, let alone how long, or which queue, or how many people were
ahead of it. The engine keeps a second file for exactly this, in a different
format, and everything a queue report is judged on comes out of it:

- **offered** -- somebody joined the queue
- **answered** -- a member picked the call up
- **abandoned** -- the caller hung up while waiting, which is the number a
  contact centre is actually managed by
- **service level** -- the share of answered calls picked up inside the queue's
  own agreed number of seconds

The format is one line per event, fields separated by vertical bars:

    epoch|call identifier|queue|member|event|first|second|third

and what the three trailing fields mean depends on the event, which is why they
are named here rather than being read positionally at every call site.

Bounded exactly as the call record reader is: only this file, only up to a
ceiling, and a sweep that reaches the ceiling says so rather than quietly
reporting on part of a period.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from .logging_setup import get_logger

__all__ = ["QueueLogReader", "EVENTS_OFFERED", "EVENTS_ENDED"]

_LOG = get_logger("queuelog")

#: The event that means a caller joined a queue.
EVENT_ENTER = "ENTERQUEUE"

#: The event that means a member picked the call up.
EVENT_CONNECT = "CONNECT"

#: The events that end a waiting call without it being answered. Each is a
#: different thing to have gone wrong, and a report that merged them would hide
#: the difference between a queue nobody is staffing and a queue people give up
#: on.
EVENTS_ABANDONED = {
    "ABANDON": "the caller hung up while waiting",
    "EXITWITHTIMEOUT": "the caller waited longer than the queue allows",
    "EXITEMPTY": "no member was available to take the call",
    "EXITWITHKEY": "the caller pressed a key to leave the queue",
}

#: The events that end an answered call.
EVENTS_ENDED = {"COMPLETEAGENT", "COMPLETECALLER"}

#: A member was offered the call and did not pick it up. Counted per member,
#: because it is the difference between a queue that is busy and a person who
#: is not answering.
EVENT_NO_ANSWER = "RINGNOANSWER"

EVENTS_OFFERED = {EVENT_ENTER}


class QueueLogReader:
    """Reads the telephony engine's queue log."""

    #: The most events one sweep will parse.
    SWEEP_LIMIT = 500_000

    def __init__(
        self,
        path: str | Path = "/var/log/asterisk/queue_log",
        root: str | Path = "/",
    ) -> None:
        self.root = Path(root)
        self.path = self.root / str(path).lstrip("/")

    def available(self) -> bool:
        return self.path.is_file()

    def parse(self, row: str) -> dict[str, Any] | None:
        """One event, with the three trailing fields named for what they hold.

        The engine writes them positionally and their meaning changes with the
        event, so they are interpreted once, here, rather than at each place
        that counts something.
        """
        parts = row.rstrip("\n").split("|")
        if len(parts) < 5:
            return None

        stamp, identifier, queue, member, event = parts[:5]
        trailing = (parts[5:] + ["", "", ""])[:3]

        try:
            moment = datetime.fromtimestamp(int(float(stamp)))
        except (TypeError, ValueError):
            return None

        record: dict[str, Any] = {
            "at": moment,
            "identifier": identifier.strip(),
            "queue": queue.strip(),
            "member": member.strip(),
            "event": event.strip().upper(),
            "waited": 0,
            "talked": 0,
            "position": 0,
        }

        event_name = record["event"]
        if event_name == EVENT_CONNECT:
            # holdtime, bridged channel, ringtime
            record["waited"] = _whole(trailing[0])
        elif event_name in EVENTS_ENDED:
            # holdtime, calltime, original position
            record["waited"] = _whole(trailing[0])
            record["talked"] = _whole(trailing[1])
            record["position"] = _whole(trailing[2])
        elif event_name == "ABANDON":
            # position, original position, holdtime
            record["position"] = _whole(trailing[0])
            record["waited"] = _whole(trailing[2])
        elif event_name in ("EXITWITHTIMEOUT", "EXITWITHKEY", "EXITEMPTY"):
            record["position"] = _whole(trailing[0])
            # The engine records the wait on some of these and not others; a
            # missing one is zero rather than a guess.
            record["waited"] = _whole(trailing[2])
        return record

    def sweep(self) -> tuple[list[dict[str, Any]], bool]:
        """Every event in the file, oldest first, and whether any were left."""
        if not self.available():
            return [], False

        events: list[dict[str, Any]] = []
        truncated = False
        try:
            with open(self.path, "r", encoding="utf-8", errors="replace") as handle:
                for row in handle:
                    if not row.strip():
                        continue
                    event = self.parse(row)
                    if event is None:
                        continue
                    events.append(event)
                    if len(events) >= self.SWEEP_LIMIT:
                        truncated = True
                        break
        except OSError as error:
            _LOG.error("the queue log could not be swept: %s", error)
            return [], False
        return events, truncated


def _whole(value: str) -> int:
    try:
        return max(0, int(float(value)))
    except (TypeError, ValueError):
        return 0
