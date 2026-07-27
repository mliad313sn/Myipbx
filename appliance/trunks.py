"""Asynchronous non blocking trunk registration layer.

Registration is modelled as an explicit state machine per trunk, advanced only
by engine events, by attempt outcomes, and by timers.  Every trunk is driven by
its own task, so registrations proceed concurrently: a carrier that accepts a
connection and then goes silent delays only its own trunk, never the others.

Each transition is timestamped, given a plain language reason, and published so
that the dashboard shows registration progress as it happens rather than as a
result discovered later.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Awaitable, Callable, Iterable, Mapping

from .logging_setup import get_logger
from .retry import compute_backoff

__all__ = ["TrunkState", "Trunk", "TrunkRegistry"]

_LOG = get_logger("trunks")


class TrunkState(str, Enum):
    """The complete set of states a trunk may occupy."""

    UNCONFIGURED = "unconfigured"
    REGISTERING = "registering"
    REGISTERED = "registered"
    RETRYING = "retrying"
    FAILED = "failed"
    DISABLED = "disabled"
    UNKNOWN = "unknown"


#: Transitions that the state machine permits.  Any transition outside this
#: table is a defect and is rejected rather than silently applied.
_PERMITTED: dict[TrunkState, frozenset[TrunkState]] = {
    TrunkState.UNCONFIGURED: frozenset(
        {TrunkState.REGISTERING, TrunkState.DISABLED, TrunkState.UNKNOWN}
    ),
    TrunkState.REGISTERING: frozenset(
        {
            TrunkState.REGISTERED,
            TrunkState.RETRYING,
            TrunkState.FAILED,
            TrunkState.DISABLED,
            TrunkState.UNKNOWN,
        }
    ),
    TrunkState.REGISTERED: frozenset(
        {
            TrunkState.RETRYING,
            TrunkState.REGISTERING,
            TrunkState.FAILED,
            TrunkState.DISABLED,
            TrunkState.UNKNOWN,
        }
    ),
    TrunkState.RETRYING: frozenset(
        {
            TrunkState.REGISTERING,
            TrunkState.REGISTERED,
            TrunkState.FAILED,
            TrunkState.DISABLED,
            TrunkState.UNKNOWN,
        }
    ),
    TrunkState.FAILED: frozenset(
        {TrunkState.REGISTERING, TrunkState.DISABLED, TrunkState.UNKNOWN}
    ),
    TrunkState.DISABLED: frozenset({TrunkState.UNCONFIGURED, TrunkState.REGISTERING}),
    TrunkState.UNKNOWN: frozenset(
        {
            TrunkState.REGISTERING,
            TrunkState.REGISTERED,
            TrunkState.RETRYING,
            TrunkState.FAILED,
            TrunkState.DISABLED,
        }
    ),
}

#: Engine registration states mapped onto appliance trunk states.
_ENGINE_STATE_MAP = {
    "registered": TrunkState.REGISTERED,
    "reachable": TrunkState.REGISTERED,
    "auth. sent": TrunkState.REGISTERING,
    "request sent": TrunkState.REGISTERING,
    "registering": TrunkState.REGISTERING,
    "unregistered": TrunkState.RETRYING,
    "unreachable": TrunkState.RETRYING,
    "rejected": TrunkState.FAILED,
    "failed": TrunkState.FAILED,
    "no authentication": TrunkState.FAILED,
}


@dataclass
class Trunk:
    """One logical trunk and its registration history."""

    name: str
    technology: str = "PJSIP"
    host: str = ""
    username: str = ""
    enabled: bool = True
    state: TrunkState = TrunkState.UNCONFIGURED
    attempts: int = 0
    last_transition_at: float = 0.0
    last_reason: str = "the trunk has not yet been driven"
    registered_at: float | None = None
    next_attempt_at: float | None = None
    history: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self, now: float | None = None) -> dict[str, Any]:
        moment = time.time() if now is None else now
        return {
            "name": self.name,
            "technology": self.technology,
            "host": self.host,
            "username": self.username,
            "enabled": self.enabled,
            "state": self.state.value,
            "attempts": self.attempts,
            "reason": self.last_reason,
            "seconds_in_state": max(0, int(moment - self.last_transition_at))
            if self.last_transition_at
            else 0,
            "registered_seconds": int(moment - self.registered_at)
            if self.registered_at
            else None,
            "seconds_until_next_attempt": max(0, int(self.next_attempt_at - moment))
            if self.next_attempt_at
            else None,
        }


AttemptFunction = Callable[[Trunk], Awaitable[bool]]
Publisher = Callable[[str, dict[str, Any]], Any]


class TrunkRegistry:
    """Owns every trunk and drives their registration concurrently."""

    def __init__(
        self,
        manager: Any = None,
        publisher: Publisher | None = None,
        base_seconds: float = 2.0,
        ceiling_seconds: float = 300.0,
        jitter_ratio: float = 0.25,
        attempt_timeout_seconds: float = 30.0,
        maximum_attempts: int = 0,
        attempt_fn: AttemptFunction | None = None,
        clock: Callable[[], float] | None = None,
        random_fn: Callable[[], float] | None = None,
    ) -> None:
        self._manager = manager
        self._publisher = publisher
        self.base_seconds = base_seconds
        self.ceiling_seconds = ceiling_seconds
        self.jitter_ratio = jitter_ratio
        self.attempt_timeout_seconds = attempt_timeout_seconds
        #: Zero means retry indefinitely, which is correct for a trunk whose
        #: carrier may be restored at any hour without human involvement.
        self.maximum_attempts = maximum_attempts
        self._attempt_fn = attempt_fn or self._attempt_via_manager
        self._clock = clock or time.time
        self._random_fn = random_fn

        self._trunks: dict[str, Trunk] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._stopping = False

    # -- declaration -------------------------------------------------------

    def declare(
        self,
        name: str,
        technology: str = "PJSIP",
        host: str = "",
        username: str = "",
        enabled: bool = True,
    ) -> Trunk:
        """Declare or update a trunk without starting its driver."""
        trunk = self._trunks.get(name)
        if trunk is None:
            trunk = Trunk(name=name, technology=technology, host=host, username=username)
            trunk.last_transition_at = self._clock()
            self._trunks[name] = trunk
        else:
            trunk.technology = technology
            trunk.host = host
            trunk.username = username

        trunk.enabled = enabled
        if not enabled and trunk.state is not TrunkState.DISABLED:
            self._transition(trunk, TrunkState.DISABLED, "the trunk is administratively disabled")
        return trunk

    def declare_many(self, definitions: Iterable[Mapping[str, Any]]) -> list[Trunk]:
        return [
            self.declare(
                name=str(item["name"]),
                technology=str(item.get("technology", "PJSIP")),
                host=str(item.get("host", "")),
                username=str(item.get("username", "")),
                enabled=bool(item.get("enabled", True)),
            )
            for item in definitions
            if item.get("name")
        ]

    def get(self, name: str) -> Trunk | None:
        return self._trunks.get(name)

    def all(self) -> list[Trunk]:
        return list(self._trunks.values())

    # -- driving -----------------------------------------------------------

    def start_all(self) -> int:
        """Start a driver task for every enabled trunk.

        Returns the number of drivers started.  Drivers run concurrently by
        construction, which is the property that keeps one unreachable carrier
        from delaying the registration of every other trunk.
        """
        self._stopping = False
        started = 0
        for trunk in self._trunks.values():
            if not trunk.enabled:
                continue
            if self._start_one(trunk):
                started += 1
        return started

    def _start_one(self, trunk: Trunk) -> bool:
        existing = self._tasks.get(trunk.name)
        if existing is not None and not existing.done():
            return False
        self._tasks[trunk.name] = asyncio.create_task(
            self._drive(trunk), name=f"trunk-{trunk.name}"
        )
        return True

    def start(self, name: str) -> bool:
        trunk = self._trunks.get(name)
        if trunk is None or not trunk.enabled:
            return False
        return self._start_one(trunk)

    async def stop_all(self) -> None:
        """Cancel every driver and wait for the tasks to unwind."""
        self._stopping = True
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()

    async def wait_all(self, timeout: float | None = None) -> bool:
        """Await every driver, reporting whether they all settled in time."""
        tasks = [task for task in self._tasks.values() if not task.done()]
        if not tasks:
            return True
        done, pending = await asyncio.wait(tasks, timeout=timeout)
        return not pending

    async def _drive(self, trunk: Trunk) -> None:
        """The registration state machine for one trunk."""
        try:
            while not self._stopping and trunk.enabled:
                self._transition(trunk, TrunkState.REGISTERING, "a registration attempt is in flight")
                trunk.next_attempt_at = None

                succeeded = False
                reason = ""
                try:
                    succeeded = await asyncio.wait_for(
                        self._attempt_fn(trunk), timeout=self.attempt_timeout_seconds
                    )
                    if not succeeded:
                        reason = "the carrier declined the registration"
                except asyncio.TimeoutError:
                    reason = "the carrier did not answer within the attempt timeout"
                except asyncio.CancelledError:
                    raise
                except Exception as error:  # noqa: BLE001 - a driver must survive
                    reason = f"the attempt raised an error: {error}"

                if succeeded:
                    trunk.attempts = 0
                    trunk.registered_at = self._clock()
                    self._transition(
                        trunk, TrunkState.REGISTERED, "the carrier accepted the registration"
                    )
                    return

                trunk.attempts += 1
                trunk.registered_at = None

                if self.maximum_attempts and trunk.attempts >= self.maximum_attempts:
                    self._transition(
                        trunk,
                        TrunkState.FAILED,
                        f"{reason}; the attempt limit has been reached",
                    )
                    return

                delay = compute_backoff(
                    trunk.attempts,
                    self.base_seconds,
                    self.ceiling_seconds,
                    self.jitter_ratio,
                    self._random_fn,
                )
                trunk.next_attempt_at = self._clock() + delay
                self._transition(trunk, TrunkState.RETRYING, reason)
                await asyncio.sleep(delay)
        except asyncio.CancelledError:
            if trunk.state not in (TrunkState.REGISTERED, TrunkState.DISABLED):
                self._transition(
                    trunk, TrunkState.UNKNOWN, "the driver was stopped before it settled"
                )
            raise

    async def _attempt_via_manager(self, trunk: Trunk) -> bool:
        """Ask the engine to register the trunk.

        A missing manager is reported as a failed attempt rather than as an
        exception, so that an appliance whose engine has not yet started
        retries on the normal schedule instead of collapsing.
        """
        if self._manager is None or not getattr(self._manager, "connected", False):
            return False
        response = await self._manager.send_action(
            "PJSIPRegister" if trunk.technology.upper() == "PJSIP" else "SIPregistry",
            {"Registration": trunk.name},
        )
        return bool(getattr(response, "is_success", False))

    # -- engine events ------------------------------------------------------

    def apply_engine_event(self, message: Any) -> Trunk | None:
        """Advance a trunk from an engine registration event.

        Engine events are authoritative: a trunk the engine reports as
        unregistered is unregistered regardless of what the driver believed.
        """
        getter = getattr(message, "get", None)
        if getter is None:
            return None

        event_name = (getter("event") or "").lower()
        if event_name not in {"registry", "contactstatus", "registrationstatus"}:
            return None

        name = getter("username") or getter("aor") or getter("channeltype") or getter("domain")
        if not name:
            return None

        trunk = self._trunks.get(str(name))
        if trunk is None:
            return None

        raw_state = (getter("status") or getter("state") or "").strip().lower()
        target = _ENGINE_STATE_MAP.get(raw_state)
        if target is None:
            return None

        if target is TrunkState.REGISTERED:
            trunk.registered_at = self._clock()
            trunk.attempts = 0
        self._transition(trunk, target, f"the engine reported the state {raw_state}")
        return trunk

    # -- transitions -------------------------------------------------------

    def _transition(self, trunk: Trunk, target: TrunkState, reason: str) -> bool:
        if trunk.state is target:
            trunk.last_reason = reason
            return False
        if target not in _PERMITTED[trunk.state]:
            _LOG.error(
                "a transition of the trunk named %s from %s to %s was rejected as invalid",
                trunk.name,
                trunk.state.value,
                target.value,
            )
            return False

        previous = trunk.state
        trunk.state = target
        trunk.last_reason = reason
        trunk.last_transition_at = self._clock()
        record = {
            "from": previous.value,
            "to": target.value,
            "reason": reason,
            "at": trunk.last_transition_at,
        }
        trunk.history.append(record)
        # The history is a diagnostic aid, not an archive; keeping it bounded
        # means a trunk that flaps for a month cannot exhaust memory.
        if len(trunk.history) > 64:
            del trunk.history[:-64]

        _LOG.info(
            "the trunk named %s moved from %s to %s because %s",
            trunk.name,
            previous.value,
            target.value,
            reason,
        )
        self._publish("trunk.transition", {"trunk": trunk.name, **record})
        return True

    def _publish(self, topic: str, payload: dict[str, Any]) -> None:
        if self._publisher is None:
            return
        try:
            self._publisher(topic, payload)
        except Exception as error:  # noqa: BLE001 - publication is best effort
            _LOG.error("a trunk transition could not be published: %s", error)

    # -- introspection -----------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        now = self._clock()
        trunks = [trunk.as_dict(now) for trunk in self._trunks.values()]
        counts: dict[str, int] = {}
        for trunk in self._trunks.values():
            counts[trunk.state.value] = counts.get(trunk.state.value, 0) + 1
        return {
            "trunks": trunks,
            "counts": counts,
            "total": len(trunks),
            "registered": counts.get(TrunkState.REGISTERED.value, 0),
        }
