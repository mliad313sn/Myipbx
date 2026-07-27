"""Live appliance state, driven by the engine event stream.

This is the model the dashboard renders.  It is advanced only by engine events
and by explicit control plane updates, never by polling, which is what makes
sub second call activity visible rather than averaged away between snapshots.

Every mutation bumps a monotonic sequence number.  The dashboard reports that
sequence back in its heartbeat, so the appliance can tell a browser that is
merely quiet from one that has silently fallen behind.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable

from .logging_setup import get_logger

__all__ = ["Channel", "Alarm", "ApplianceState"]

_LOG = get_logger("state")

#: Engine channel state numbers mapped onto plain language.
_CHANNEL_STATE_NAMES = {
    "0": "down",
    "1": "reserved",
    "2": "off hook",
    "3": "dialling",
    "4": "ringing",
    "5": "ringing inbound",
    "6": "answered",
    "7": "busy",
    "8": "dialling off hook",
    "9": "pre-ringing",
}

#: Engine channel state descriptions mapped onto the same vocabulary.  The
#: engine supplies the description on every modern event, so this table — not
#: the numeric one above — is what the appliance normally reads.  In
#: particular the engine names the answered state "Up", which must resolve to
#: the appliance's answered state or no call would ever accrue talk time.
_CHANNEL_STATE_DESCRIPTIONS = {
    "down": "down",
    "rsrvd": "reserved",
    "reserved": "reserved",
    "offhook": "off hook",
    "off hook": "off hook",
    "dialing": "dialling",
    "dialling": "dialling",
    "ring": "ringing",
    "ringing": "ringing inbound",
    "up": "answered",
    "answered": "answered",
    "busy": "busy",
    "dialing offhook": "dialling off hook",
    "pre-ring": "pre-ringing",
    "prering": "pre-ringing",
    "unknown": "unknown",
}

#: The states in which a call is carrying audio.
ANSWERED_STATES = frozenset({"answered"})


@dataclass
class Channel:
    """One live channel on the engine."""

    unique_identifier: str
    name: str
    state: str = "unknown"
    caller_number: str = ""
    caller_name: str = ""
    connected_number: str = ""
    context: str = ""
    extension: str = ""
    started_at: float = 0.0
    answered_at: float | None = None
    bridge_identifier: str | None = None

    @property
    def answered(self) -> bool:
        return self.answered_at is not None

    def as_dict(self, now: float) -> dict[str, Any]:
        return {
            "unique_identifier": self.unique_identifier,
            "name": self.name,
            "state": self.state,
            "caller_number": self.caller_number,
            "caller_name": self.caller_name,
            "connected_number": self.connected_number,
            "context": self.context,
            "extension": self.extension,
            "duration_seconds": max(0, int(now - self.started_at)) if self.started_at else 0,
            "talk_seconds": max(0, int(now - self.answered_at)) if self.answered_at else 0,
            "answered": self.answered,
            "bridge_identifier": self.bridge_identifier,
        }


@dataclass
class Alarm:
    """A condition the operator must see."""

    key: str
    severity: str
    message: str
    raised_at: float
    detail: str = ""
    #: When an operator said they had seen this, and who said so. An alarm is
    #: not cleared by being acknowledged -- the condition is still there and
    #: the appliance keeps saying so -- but a panel that cannot be worked
    #: through is a panel that stops being read, and a standing alarm nobody
    #: has looked at should not sit beside one somebody is already on.
    acknowledged_at: float | None = None
    acknowledged_by: str = ""

    def as_dict(self, now: float) -> dict[str, Any]:
        return {
            "key": self.key,
            "severity": self.severity,
            "message": self.message,
            "detail": self.detail,
            "age_seconds": max(0, int(now - self.raised_at)),
            "acknowledged": self.acknowledged_at is not None,
            "acknowledged_by": self.acknowledged_by,
            "acknowledged_age_seconds": (
                max(0, int(now - self.acknowledged_at))
                if self.acknowledged_at is not None
                else None
            ),
        }


#: Worst first. An operator reading down the panel meets the thing that is
#: taking calls away before the thing that is merely worth knowing, whatever
#: order the conditions happened to arise in.
SEVERITY_ORDER: dict[str, int] = {
    "critical": 0,
    "warning": 1,
    "information": 2,
}


def _alarm_rank(alarm: Alarm) -> tuple[int, int, float]:
    """Sort key: severity, then whether anybody is on it, then age.

    An unacknowledged alarm sorts above an acknowledged one of the same
    severity, and among equals the oldest is first, because the one that has
    been standing longest is the one least likely to be in hand.
    """
    return (
        SEVERITY_ORDER.get(alarm.severity, len(SEVERITY_ORDER)),
        1 if alarm.acknowledged_at is not None else 0,
        alarm.raised_at,
    )


@dataclass
class ApplianceState:
    """The complete live state of the appliance."""

    clock: Callable[[], float] = time.time
    started_at: float = 0.0
    sequence: int = 0

    channels: dict[str, Channel] = field(default_factory=dict)
    alarms: dict[str, Alarm] = field(default_factory=dict)

    events_applied: int = 0
    events_ignored: int = 0
    last_event_at: float | None = None
    last_event_name: str | None = None

    calls_started: int = 0
    calls_completed: int = 0
    peak_concurrent_calls: int = 0

    engine_connected: bool = False
    hardware: dict[str, Any] = field(default_factory=dict)

    #: Set by the composition root so that mutations reach the socket hub.
    publisher: Callable[[str, dict[str, Any]], Any] | None = None

    def __post_init__(self) -> None:
        if not self.started_at:
            self.started_at = self.clock()

    # -- engine events -----------------------------------------------------

    def apply_engine_event(self, message: Any) -> bool:
        """Advance the state from one engine event.

        Returns whether the event changed anything, so that the caller can
        avoid publishing a no change update to every connected browser.
        """
        getter = getattr(message, "get", None)
        if getter is None:
            self.events_ignored += 1
            return False

        name = (getter("event") or "").strip()
        if not name:
            self.events_ignored += 1
            return False

        self.last_event_at = self.clock()
        self.last_event_name = name

        handler = _EVENT_HANDLERS.get(name.lower())
        if handler is None:
            self.events_ignored += 1
            return False

        changed = handler(self, getter)
        if changed:
            self.events_applied += 1
            self.touch()
        else:
            self.events_ignored += 1
        return changed

    # -- channel transitions -----------------------------------------------

    def _on_new_channel(self, getter: Callable[..., Any]) -> bool:
        identifier = getter("uniqueid")
        if not identifier:
            return False
        self.channels[identifier] = Channel(
            unique_identifier=identifier,
            name=getter("channel", "") or "",
            state=_state_name(getter),
            caller_number=getter("calleridnum", "") or "",
            caller_name=getter("calleridname", "") or "",
            context=getter("context", "") or "",
            extension=getter("exten", "") or "",
            started_at=self.clock(),
        )
        # A channel that is already answered when the appliance first sees it —
        # which happens when the appliance reconnects to an engine mid call —
        # must start accruing talk time immediately rather than waiting for a
        # state change that has already been and gone.
        channel = self.channels[identifier]
        if channel.state in ANSWERED_STATES:
            channel.answered_at = channel.started_at
        self.calls_started += 1
        self.peak_concurrent_calls = max(self.peak_concurrent_calls, len(self.channels))
        return True

    def _on_new_state(self, getter: Callable[..., Any]) -> bool:
        channel = self.channels.get(getter("uniqueid") or "")
        if channel is None:
            return False
        channel.state = _state_name(getter)
        if channel.state in ANSWERED_STATES and channel.answered_at is None:
            channel.answered_at = self.clock()
        connected = getter("connectedlinenum")
        if connected:
            channel.connected_number = connected
        return True

    def _on_new_caller_identity(self, getter: Callable[..., Any]) -> bool:
        channel = self.channels.get(getter("uniqueid") or "")
        if channel is None:
            return False
        channel.caller_number = getter("calleridnum", channel.caller_number) or ""
        channel.caller_name = getter("calleridname", channel.caller_name) or ""
        return True

    def _on_hangup(self, getter: Callable[..., Any]) -> bool:
        identifier = getter("uniqueid") or ""
        if identifier not in self.channels:
            return False
        del self.channels[identifier]
        self.calls_completed += 1
        return True

    def _on_bridge_enter(self, getter: Callable[..., Any]) -> bool:
        channel = self.channels.get(getter("uniqueid") or "")
        if channel is None:
            return False
        channel.bridge_identifier = getter("bridgeuniqueid")
        if channel.answered_at is None:
            channel.answered_at = self.clock()
        return True

    def _on_bridge_leave(self, getter: Callable[..., Any]) -> bool:
        channel = self.channels.get(getter("uniqueid") or "")
        if channel is None:
            return False
        channel.bridge_identifier = None
        return True

    def _on_manager_connected(self, getter: Callable[..., Any]) -> bool:
        self.engine_connected = True
        self.clear_alarm("engine-disconnected")
        return True

    def _on_manager_disconnected(self, getter: Callable[..., Any]) -> bool:
        self.engine_connected = False
        # A lost engine invalidates every channel: they are not known to be
        # gone, but they are certainly no longer known to be present.
        self.channels.clear()
        self.raise_alarm(
            "engine-disconnected",
            "critical",
            "the telephony engine manager interface is not connected",
            "channel state is unknown until the connection is restored",
        )
        self._publish_urgently(
            "engine.disconnected",
            {
                "engine_connected": False,
                "detail": "channel state is unknown until the connection is restored",
            },
        )
        return True

    # -- alarms ------------------------------------------------------------

    def raise_alarm(self, key: str, severity: str, message: str, detail: str = "") -> bool:
        existing = self.alarms.get(key)
        if existing is not None and existing.severity == severity and existing.message == message:
            return False
        # A changed severity or message is a changed condition, so the new
        # alarm arrives unacknowledged even where the old one had been seen:
        # somebody acknowledging "the trunk is retrying" has not acknowledged
        # "the trunk has failed".
        alarm = Alarm(
            key=key, severity=severity, message=message, raised_at=self.clock(), detail=detail
        )
        self.alarms[key] = alarm
        _LOG.warning("an alarm was raised: %s", message)
        self.touch()
        # The snapshot the touch above published already carries this alarm,
        # but that snapshot may be held for a coalescing window and an alarm is
        # the one thing an operator acts on immediately.  The dedicated topic
        # is on the transport's bypass list, so it overtakes the window and
        # drags the held snapshot out with it.
        self._publish_urgently("alarm.raised", alarm.as_dict(self.clock()))
        return True

    def clear_alarm(self, key: str) -> bool:
        if self.alarms.pop(key, None) is None:
            return False
        _LOG.info("the alarm identified as %s was cleared", key)
        self.touch()
        self._publish_urgently("alarm.cleared", {"key": key})
        return True

    def acknowledge_alarm(self, key: str, actor: str) -> bool:
        """Record that somebody has seen this alarm and is dealing with it.

        Acknowledging does not clear anything. The condition is still there,
        the alarm is still raised, and the appliance goes on saying so; the
        only thing that clears an alarm is the condition ending. What this
        changes is the panel: a standing alarm somebody is already working
        sinks below one nobody has looked at, and the panel stays worth
        reading on a machine where one condition has been true for a week.

        A suppression that hid the alarm entirely was considered and not
        built. An alarm an operator can make invisible is an alarm the next
        operator never sees, and on a telephone system the next operator is
        usually the one who finds out that a trunk has been down since Friday.
        """
        alarm = self.alarms.get(key)
        if alarm is None:
            return False
        if alarm.acknowledged_at is not None:
            return False
        alarm.acknowledged_at = self.clock()
        alarm.acknowledged_by = actor or "an unidentified account"
        _LOG.info(
            "the alarm identified as %s was acknowledged by %s",
            key, alarm.acknowledged_by,
        )
        self.touch()
        return True

    # -- publication -------------------------------------------------------

    def touch(self) -> int:
        """Advance the sequence number and publish the new snapshot."""
        self.sequence += 1
        if self.publisher is not None:
            try:
                self.publisher("state.changed", self.snapshot())
            except Exception as error:  # noqa: BLE001 - publication is best effort
                _LOG.error("a state change could not be published: %s", error)
        return self.sequence

    def _publish_urgently(self, topic: str, payload: dict[str, Any]) -> None:
        """Publish on a topic the transport is told never to hold back."""
        if self.publisher is None:
            return
        try:
            self.publisher(topic, payload)
        except Exception as error:  # noqa: BLE001 - publication is best effort
            _LOG.error("an urgent notice could not be published: %s", error)

    # -- introspection -----------------------------------------------------

    @property
    def active_call_count(self) -> int:
        return len(self.channels)

    @property
    def answered_call_count(self) -> int:
        return sum(1 for channel in self.channels.values() if channel.answered)

    def uptime_seconds(self) -> int:
        return max(0, int(self.clock() - self.started_at))

    def snapshot(self) -> dict[str, Any]:
        now = self.clock()
        return {
            "sequence": self.sequence,
            "uptime_seconds": self.uptime_seconds(),
            "engine_connected": self.engine_connected,
            "active_calls": self.active_call_count,
            "answered_calls": self.answered_call_count,
            "calls_started": self.calls_started,
            "calls_completed": self.calls_completed,
            "peak_concurrent_calls": self.peak_concurrent_calls,
            "events_applied": self.events_applied,
            "events_ignored": self.events_ignored,
            "seconds_since_last_event": int(now - self.last_event_at)
            if self.last_event_at
            else None,
            "last_event_name": self.last_event_name,
            "channels": [channel.as_dict(now) for channel in self.channels.values()],
            "alarms": [
                alarm.as_dict(now)
                for alarm in sorted(self.alarms.values(), key=_alarm_rank)
            ],
            "hardware": self.hardware,
        }


def _state_name(getter: Callable[..., Any]) -> str:
    """Resolve an engine channel state into the appliance's own vocabulary."""
    description = getter("channelstatedesc")
    if description:
        cleaned = str(description).strip().lower()
        # An unrecognised description is passed through rather than discarded,
        # so a newer engine's state is still displayed even before this table
        # knows its name.
        return _CHANNEL_STATE_DESCRIPTIONS.get(cleaned, cleaned)
    number = getter("channelstate")
    if number is not None:
        return _CHANNEL_STATE_NAMES.get(str(number).strip(), "unknown")
    return "unknown"


#: Event dispatch table.  Keeping it explicit means an unrecognised engine
#: event is counted and ignored rather than misinterpreted.
_EVENT_HANDLERS: dict[str, Callable[[ApplianceState, Callable[..., Any]], bool]] = {
    "newchannel": ApplianceState._on_new_channel,
    "newstate": ApplianceState._on_new_state,
    "newcallerid": ApplianceState._on_new_caller_identity,
    "hangup": ApplianceState._on_hangup,
    "bridgeenter": ApplianceState._on_bridge_enter,
    "bridgeleave": ApplianceState._on_bridge_leave,
    "appliancemanagerconnected": ApplianceState._on_manager_connected,
    "appliancemanagerdisconnected": ApplianceState._on_manager_disconnected,
}
