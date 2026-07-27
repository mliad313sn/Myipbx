"""Persistent bidirectional socket transport and connection hub.

The protocol lives in ``wsproto``; this module carries it over the network and
manages the population of connected dashboards.

Three design points matter under load.  First, every connection owns a bounded
outbound queue drained by its own writer task, so a browser on a congested link
cannot apply backpressure to the appliance — it is disconnected as a slow
consumer instead, and told why.  Second, the heartbeat is dual layer: a
protocol level ping proves the socket is alive, and an application level
heartbeat carries the state sequence number and the age of the last engine
event, which is what lets the dashboard distinguish live from reconnecting from
stale rather than mistaking a frozen screen for a quiet network.

Third, publication is coalesced.  A hundred simultaneous calls raise several
hundred engine events in a few milliseconds, and sending one frame per event to
every connected dashboard multiplies that by the population before it reaches
the wire.  The work is real but the frames are not: the state each one carries
is superseded by the next within milliseconds, so an operator could not have
read any of them.  The hub therefore holds a superseding topic for a short
window and puts only the newest payload on the wire, which is what leaves the
single event loop free for the engine and for the interface.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Awaitable, Callable, Iterable

from . import wsproto
from .logging_setup import get_logger

__all__ = [
    "COALESCED_TOPICS",
    "IMMEDIATE_TOPICS",
    "SocketConnection",
    "SocketHub",
]

_LOG = get_logger("socket")

MessageHandler = Callable[["SocketConnection", dict[str, Any]], Awaitable[None] | None]

#: Topics whose payload wholly replaces the payload before it.  Only these may
#: be coalesced, because discarding an intermediate payload loses nothing when
#: the payload that replaces it already describes everything the discarded one
#: described.  A topic that reports a discrete occurrence — a trunk transition,
#: a task starting — is absent from this set on purpose: coalescing such a
#: topic would silently drop occurrences the operator is entitled to see.
COALESCED_TOPICS: frozenset[str] = frozenset({"state.changed"})

#: The bypass list.  A message published on one of these topics reaches the
#: wire in the same turn of the loop it was published in, whatever the
#: coalescing window is set to, and it flushes anything already held so that a
#: dashboard never sees an urgent frame ahead of the state that explains it.
#:
#: Membership is earned by being something an operator acts on within seconds
#: rather than something they watch: a raised or cleared alarm, the telephony
#: engine going away, and an interface card driver bring-up stage that failed
#: midway.  This is a named constant rather than a condition evaluated at each
#: call site so that the whole of the bypass can be read in one place, and so
#: that a test can assert its membership directly.
IMMEDIATE_TOPICS: frozenset[str] = frozenset(
    {
        "alarm.raised",
        "alarm.cleared",
        "engine.disconnected",
        "driver.stage-failed",
    }
)


class SocketConnection:
    """One connected dashboard."""

    def __init__(
        self,
        identifier: str,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        maximum_message_bytes: int = 262144,
        send_queue_limit: int = 512,
        username: str = "unknown",
        peer: str = "unknown",
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.identifier = identifier
        self.username = username
        self.peer = peer
        self._reader = reader
        self._writer = writer
        self._clock = clock or time.time
        self._decoder = wsproto.FrameDecoder(
            maximum_message_bytes=maximum_message_bytes, require_mask=True
        )
        self._outbound: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=send_queue_limit)
        self._writer_task: asyncio.Task[None] | None = None

        self.connected_at = self._clock()
        self.last_received_at = self.connected_at
        self.last_pong_at = self.connected_at
        self.missed_heartbeats = 0
        self.messages_received = 0
        self.messages_sent = 0
        self.dropped_messages = 0
        self.closed = False
        self.close_reason = ""

    # -- sending -----------------------------------------------------------

    def start_writer(self) -> None:
        if self._writer_task is None:
            self._writer_task = asyncio.create_task(
                self._drain(), name=f"socket-writer-{self.identifier}"
            )

    def enqueue(self, frame: bytes) -> bool:
        """Queue a pre-serialised frame.  Reports whether it was accepted.

        A full queue means the peer is not keeping up.  Rather than growing the
        queue without bound, the message is refused and the caller closes the
        connection as a slow consumer.
        """
        if self.closed:
            return False
        try:
            self._outbound.put_nowait(frame)
            return True
        except asyncio.QueueFull:
            self.dropped_messages += 1
            return False

    def send_message(self, topic: str, payload: Any) -> bool:
        """Queue an application message as a text frame."""
        body = json.dumps(
            {"topic": topic, "payload": payload, "at": self._clock()},
            default=str,
            separators=(",", ":"),
        )
        return self.enqueue(wsproto.build_frame(wsproto.OPCODE_TEXT, body.encode("utf-8")))

    def send_ping(self, payload: bytes = b"") -> bool:
        return self.enqueue(wsproto.build_frame(wsproto.OPCODE_PING, payload))

    async def _drain(self) -> None:
        try:
            while True:
                frame = await self._outbound.get()
                if frame is None:
                    break
                self._writer.write(frame)
                await self._writer.drain()
                self.messages_sent += 1
        except asyncio.CancelledError:
            raise
        except (OSError, ConnectionError) as error:
            _LOG.info(
                "the socket connection identified as %s failed while writing: %s",
                self.identifier,
                error,
            )
        finally:
            self.closed = True

    # -- receiving ---------------------------------------------------------

    async def serve(self, handler: MessageHandler) -> None:
        """Read frames until the peer goes away or violates the protocol."""
        try:
            while not self.closed:
                chunk = await self._reader.read(65536)
                if not chunk:
                    self.close_reason = "the peer closed the connection"
                    break

                self.last_received_at = self._clock()
                try:
                    events = self._decoder.feed(chunk)
                except wsproto.ProtocolError as error:
                    _LOG.info(
                        "the socket connection identified as %s violated the protocol: %s",
                        self.identifier,
                        error,
                    )
                    await self.close(error.code, str(error))
                    return

                for event in events:
                    if not await self._handle_event(event, handler):
                        return
        except asyncio.CancelledError:
            raise
        except (OSError, ConnectionError) as error:
            self.close_reason = f"the connection failed while reading: {error}"
        finally:
            await self.close(wsproto.CLOSE_NORMAL, self.close_reason or "closing")

    async def _handle_event(self, event: object, handler: MessageHandler) -> bool:
        if isinstance(event, wsproto.PingReceived):
            self.enqueue(wsproto.build_frame(wsproto.OPCODE_PONG, event.payload))
            return True

        if isinstance(event, wsproto.PongReceived):
            self.last_pong_at = self._clock()
            self.missed_heartbeats = 0
            return True

        if isinstance(event, wsproto.CloseReceived):
            self.close_reason = event.reason or "the peer sent a close frame"
            await self.close(wsproto.CLOSE_NORMAL, "acknowledged")
            return False

        if isinstance(event, wsproto.BinaryMessage):
            # The dashboard protocol is textual; a binary message is a client
            # defect and is refused rather than guessed at.
            await self.close(wsproto.CLOSE_UNSUPPORTED, "binary messages are not accepted")
            return False

        if isinstance(event, wsproto.TextMessage):
            self.messages_received += 1
            try:
                decoded = json.loads(event.text)
            except json.JSONDecodeError:
                self.send_message("error", {"reason": "the message was not valid encoding"})
                return True
            if not isinstance(decoded, dict):
                self.send_message("error", {"reason": "a message must be a mapping"})
                return True
            outcome = handler(self, decoded)
            if asyncio.iscoroutine(outcome):
                await outcome
            return True

        return True

    # -- closing -----------------------------------------------------------

    async def close(self, code: int = wsproto.CLOSE_NORMAL, reason: str = "") -> None:
        if self.closed:
            return
        self.closed = True
        self.close_reason = reason or self.close_reason

        try:
            self._writer.write(wsproto.build_close_frame(code, reason))
            await self._writer.drain()
        except (OSError, ConnectionError, RuntimeError):
            pass

        if self._writer_task is not None:
            try:
                self._outbound.put_nowait(None)
            except asyncio.QueueFull:
                self._writer_task.cancel()
            try:
                await asyncio.wait_for(self._writer_task, timeout=2.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._writer_task.cancel()
            self._writer_task = None

        try:
            self._writer.close()
            await self._writer.wait_closed()
        except (OSError, ConnectionError, RuntimeError):
            pass

    # -- introspection -----------------------------------------------------

    def as_dict(self, now: float | None = None) -> dict[str, Any]:
        moment = self._clock() if now is None else now
        return {
            "identifier": self.identifier,
            "username": self.username,
            "peer": self.peer,
            "connected_seconds": max(0, int(moment - self.connected_at)),
            "idle_seconds": max(0, int(moment - self.last_received_at)),
            "messages_received": self.messages_received,
            "messages_sent": self.messages_sent,
            "dropped_messages": self.dropped_messages,
            "missed_heartbeats": self.missed_heartbeats,
            "queue_depth": self._outbound.qsize(),
        }


class SocketHub:
    """The population of connected dashboards and the heartbeat that proves it."""

    def __init__(
        self,
        heartbeat_interval_seconds: float = 5.0,
        missed_limit: int = 3,
        maximum_connections: int = 256,
        state_provider: Callable[[], dict[str, Any]] | None = None,
        clock: Callable[[], float] | None = None,
        coalesce_milliseconds: int = 150,
    ) -> None:
        self.heartbeat_interval_seconds = heartbeat_interval_seconds
        self.missed_limit = missed_limit
        self.maximum_connections = maximum_connections
        self.coalesce_milliseconds = max(0, int(coalesce_milliseconds))
        self._state_provider = state_provider
        self._clock = clock or time.time

        self._connections: dict[str, SocketConnection] = {}
        self._heartbeat_task: asyncio.Task[None] | None = None
        self._sequence = 0
        self.rejected_connections = 0
        self.slow_consumer_disconnections = 0

        # The newest payload held for each superseding topic, and the timer
        # that will put it on the wire.
        self._pending: dict[str, Any] = {}
        self._flush_handle: asyncio.TimerHandle | None = None
        self.coalesced_publications = 0
        self.flushed_publications = 0

        # Background closes, retained so that the loop is not the only
        # reference holding them alive.
        self._closing_tasks: set[asyncio.Task[None]] = set()

    # -- population --------------------------------------------------------

    @property
    def connection_count(self) -> int:
        return len(self._connections)

    def has_capacity(self) -> bool:
        return len(self._connections) < self.maximum_connections

    def add(self, connection: SocketConnection) -> bool:
        if not self.has_capacity():
            self.rejected_connections += 1
            return False
        self._connections[connection.identifier] = connection
        connection.start_writer()
        _LOG.info(
            "a dashboard connected as %s; there are now %d connected",
            connection.username,
            len(self._connections),
        )
        return True

    def remove(self, connection: SocketConnection) -> None:
        if self._connections.pop(connection.identifier, None) is not None:
            _LOG.info(
                "a dashboard disconnected; there are now %d connected",
                len(self._connections),
            )

    def connections(self) -> Iterable[SocketConnection]:
        return tuple(self._connections.values())

    # -- publication -------------------------------------------------------

    def publish(self, topic: str, payload: Any) -> None:
        """Offer a message for publication, coalescing it where that is safe.

        This is the entry point every producer uses.  ``broadcast`` remains the
        unconditional path beneath it, so a caller that has already decided a
        frame must go out now — the heartbeat, a test — is not routed through
        the window.
        """
        if topic in IMMEDIATE_TOPICS:
            # An urgent frame overtakes nothing: whatever is held is flushed
            # first, so the dashboard reads the state and then the alarm that
            # the state already contains, never the other way round.
            self.flush_pending()
            self.broadcast(topic, payload)
            return

        if topic not in COALESCED_TOPICS or self.coalesce_milliseconds <= 0:
            self.broadcast(topic, payload)
            return

        loop = _running_loop()
        if loop is None:
            # Without a loop there is nothing that would ever fire the timer,
            # so holding the payload would lose it rather than delay it.
            self.broadcast(topic, payload)
            return

        if topic in self._pending:
            self.coalesced_publications += 1
        self._pending[topic] = payload

        if self._flush_handle is None:
            self._flush_handle = loop.call_later(
                self.coalesce_milliseconds / 1000.0, self._on_flush_due
            )

    def _on_flush_due(self) -> None:
        self._flush_handle = None
        try:
            self.flush_pending()
        except Exception as error:  # noqa: BLE001 - publication is best effort
            _LOG.error("a coalesced publication could not be delivered: %s", error)

    def flush_pending(self) -> int:
        """Put every held payload on the wire now.  Reports how many went."""
        if self._flush_handle is not None:
            self._flush_handle.cancel()
            self._flush_handle = None
        if not self._pending:
            return 0

        held = self._pending
        self._pending = {}
        for topic, payload in held.items():
            self.flushed_publications += 1
            self.broadcast(topic, payload)
        return len(held)

    @property
    def pending_topics(self) -> tuple[str, ...]:
        """The topics currently held, in the order they were first held."""
        return tuple(self._pending)

    def broadcast(self, topic: str, payload: Any) -> int:
        """Publish a message to every connection.

        Serialisation happens once and the resulting frame is shared, which is
        what keeps a broadcast to a large population inexpensive.  Connections
        that cannot accept the frame are disconnected as slow consumers.
        """
        self._sequence += 1
        body = json.dumps(
            {"topic": topic, "payload": payload, "sequence": self._sequence, "at": self._clock()},
            default=str,
            separators=(",", ":"),
        )
        frame = wsproto.build_frame(wsproto.OPCODE_TEXT, body.encode("utf-8"))

        delivered = 0
        overwhelmed: list[SocketConnection] = []
        for connection in tuple(self._connections.values()):
            if connection.closed:
                overwhelmed.append(connection)
                continue
            if connection.enqueue(frame):
                delivered += 1
            else:
                overwhelmed.append(connection)

        for connection in overwhelmed:
            self._disconnect_slow_consumer(connection)
        return delivered

    def _disconnect_slow_consumer(self, connection: SocketConnection) -> None:
        self.remove(connection)
        if connection.closed:
            return
        self.slow_consumer_disconnections += 1
        _LOG.warning(
            "the dashboard connection identified as %s could not keep up and was "
            "disconnected as a slow consumer",
            connection.identifier,
        )
        self._spawn(
            connection.close(
                wsproto.CLOSE_POLICY_VIOLATION,
                "the connection could not keep up with the update rate",
            )
        )

    def _spawn(self, coroutine: Awaitable[None]) -> None:
        """Run a closing coroutine in the background, holding on to the task.

        The task is retained until it finishes.  A task referenced only by the
        loop may be collected before it has run, and a connection collected
        mid-close is a socket left open, which is the precise failure the slow
        consumer path exists to prevent.
        """
        loop = _running_loop()
        if loop is None:
            # Nothing can be awaited without a loop.  The connection has
            # already been removed from the population, so it can no longer
            # receive anything; closing its transport is all that is missed.
            coroutine.close()  # type: ignore[attr-defined]
            return
        task = loop.create_task(coroutine)  # type: ignore[arg-type]
        self._closing_tasks.add(task)
        task.add_done_callback(self._closing_tasks.discard)

    # -- heartbeat ---------------------------------------------------------

    def start_heartbeat(self) -> None:
        if self._heartbeat_task is None or self._heartbeat_task.done():
            self._heartbeat_task = asyncio.create_task(
                self._heartbeat_loop(), name="socket-heartbeat"
            )

    async def stop(self) -> None:
        # Anything still held is delivered before the connections are taken
        # down, so a shutdown never swallows the last state change.
        self.flush_pending()

        if self._heartbeat_task is not None:
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass
            self._heartbeat_task = None

        for connection in tuple(self._connections.values()):
            await connection.close(wsproto.CLOSE_GOING_AWAY, "the appliance is shutting down")
        self._connections.clear()

    async def _heartbeat_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(self.heartbeat_interval_seconds)
            except asyncio.CancelledError:
                raise
            try:
                self.sweep_once()
            except Exception as error:  # noqa: BLE001 - the heartbeat must survive
                _LOG.error("the heartbeat sweep failed: %s", error)

    def sweep_once(self) -> dict[str, int]:
        """Emit one heartbeat round and evict peers that stopped answering.

        Separated from the loop so that the test suite can drive heartbeat
        behaviour deterministically rather than by waiting on wall clock time.
        """
        evicted = 0
        for connection in tuple(self._connections.values()):
            if connection.closed:
                self.remove(connection)
                continue

            if connection.missed_heartbeats >= self.missed_limit:
                evicted += 1
                self.remove(connection)
                self._spawn(
                    connection.close(
                        wsproto.CLOSE_GOING_AWAY,
                        "no heartbeat reply was received within the permitted number "
                        "of consecutive attempts",
                    )
                )
                continue

            connection.missed_heartbeats += 1
            connection.send_ping(b"appliance")

        payload = self._heartbeat_payload()
        delivered = self.broadcast("heartbeat", payload)
        return {"delivered": delivered, "evicted": evicted}

    def _heartbeat_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "connections": len(self._connections),
            "interval_seconds": self.heartbeat_interval_seconds,
            "missed_limit": self.missed_limit,
        }
        if self._state_provider is not None:
            try:
                snapshot = self._state_provider()
            except Exception as error:  # noqa: BLE001 - never fail the heartbeat
                _LOG.error("the heartbeat could not read the appliance state: %s", error)
            else:
                payload["state_sequence"] = snapshot.get("sequence")
                payload["engine_connected"] = snapshot.get("engine_connected")
                payload["active_calls"] = snapshot.get("active_calls")
                payload["seconds_since_last_event"] = snapshot.get("seconds_since_last_event")
        return payload

    # -- introspection -----------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        now = self._clock()
        return {
            "connection_count": len(self._connections),
            "maximum_connections": self.maximum_connections,
            "rejected_connections": self.rejected_connections,
            "slow_consumer_disconnections": self.slow_consumer_disconnections,
            "broadcast_sequence": self._sequence,
            "coalesce_milliseconds": self.coalesce_milliseconds,
            "coalesced_publications": self.coalesced_publications,
            "flushed_publications": self.flushed_publications,
            "connections": [item.as_dict(now) for item in self._connections.values()],
        }


def _running_loop() -> asyncio.AbstractEventLoop | None:
    """The loop currently running, or nothing when called outside one."""
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        return None
