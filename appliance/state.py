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

    def as_dict(self, now: float) -> dict[str, Any]:
        return {
            "key": self.key,
            "severity": self.severity,
            "message": self.message,
            "detail": self.detail,
            "age_seconds": max(0, int(now - self.raised_at)),
        }


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
        return True

    # -- alarms ------------------------------------------------------------

    def raise_alarm(self, key: str, severity: str, message: str, detail: str = "") -> bool:
        existing = self.alarms.get(key)
        if existing is not None and existing.severity == severity and existing.message == message:
            return False
        self.alarms[key] = Alarm(
            key=key, severity=severity, message=message, raised_at=self.clock(), detail=detail
        )
        _LOG.warning("an alarm was raised: %s", message)
        self.touch()
        return True

    def clear_alarm(self, key: str) -> bool:
        if self.alarms.pop(key, None) is None:
            return False
        _LOG.info("the alarm identified as %s was cleared", key)
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
            "alarms": [alarm.as_dict(now) for alarm in self.alarms.values()],
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
