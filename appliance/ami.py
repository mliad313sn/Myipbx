"""Asynchronous client for the telephony engine manager interface.

The engine exposes a line oriented control and event channel.  This client
maintains one long lived connection to it, authenticates, consumes the event
stream, and correlates outgoing actions to their responses by a generated
action identifier.

Every operation is non blocking.  An action that receives no response within
its timeout fails that one action; it never stalls the event loop and never
delays an unrelated action.  Loss of the connection triggers reconnection with
exponential backoff and jitter, and dependent state is marked unknown rather
than being left to look healthy.
"""

from __future__ import annotations

import asyncio
import itertools
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Iterable, Mapping

from .logging_setup import get_logger
from .retry import BackoffPolicy

__all__ = [
    "ManagerMessage",
    "parse_message",
    "encode_action",
    "ManagerClient",
    "ManagerError",
    "ManagerNotConnected",
]

_LOG = get_logger("ami")

_TERMINATOR = b"\r\n\r\n"
_LINE_ENDING = "\r\n"

EventHandler = Callable[["ManagerMessage"], Any]
Connector = Callable[[], Awaitable[tuple[asyncio.StreamReader, asyncio.StreamWriter]]]


class ManagerError(RuntimeError):
    """The engine rejected an action or returned an error response."""


class ManagerNotConnected(ManagerError):
    """An action was attempted while no manager connection was established."""


@dataclass(frozen=True)
class ManagerMessage:
    """One parsed manager message.

    Field names are matched without regard to case, because the engine is not
    consistent about capitalisation across versions.  A field that appears more
    than once — channel variables, for instance — retains every occurrence.
    """

    fields: Mapping[str, list[str]] = field(default_factory=dict)

    def get(self, name: str, default: str | None = None) -> str | None:
        values = self.fields.get(name.lower())
        return values[0] if values else default

    def get_all(self, name: str) -> list[str]:
        return list(self.fields.get(name.lower(), ()))

    def has(self, name: str) -> bool:
        return name.lower() in self.fields

    @property
    def event(self) -> str | None:
        return self.get("event")

    @property
    def response(self) -> str | None:
        return self.get("response")

    @property
    def action_id(self) -> str | None:
        return self.get("actionid")

    @property
    def is_success(self) -> bool:
        return (self.response or "").lower() in {"success", "goodbye", "follows"}

    def as_dict(self) -> dict[str, str]:
        return {name: values[0] for name, values in self.fields.items() if values}


def parse_message(block: str) -> ManagerMessage:
    """Parse one manager message block into a message object.

    Lines that carry no separator are preserved under a synthetic field so
    that command output, which the engine returns as unstructured text, is not
    silently discarded.
    """
    parsed: dict[str, list[str]] = {}
    for line in block.split(_LINE_ENDING):
        if not line.strip():
            continue
        name, separator, value = line.partition(":")
        if not separator:
            parsed.setdefault("output", []).append(line.strip())
            continue
        parsed.setdefault(name.strip().lower(), []).append(value.strip())
    return ManagerMessage(parsed)


def encode_action(action: str, fields: Mapping[str, Any] | None = None) -> bytes:
    """Serialise an action into the manager wire format.

    Field values are stringified and stripped of line endings, so that a value
    taken from configuration or from an interface request cannot inject an
    additional manager field.
    """
    lines = [f"Action: {_clean(action)}"]
    for name, value in (fields or {}).items():
        if value is None:
            continue
        if isinstance(value, (list, tuple)):
            for item in value:
                lines.append(f"{_clean(name)}: {_clean(item)}")
        else:
            lines.append(f"{_clean(name)}: {_clean(value)}")
    return (_LINE_ENDING.join(lines) + _TERMINATOR.decode("ascii")).encode("utf-8")


def _clean(value: Any) -> str:
    return str(value).replace("\r", " ").replace("\n", " ")


class ManagerClient:
    """A resilient, non blocking manager interface client."""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 5038,
        username: str = "",
        secret: str = "",
        connect_timeout_seconds: float = 5.0,
        action_timeout_seconds: float = 10.0,
        reconnect_base_seconds: float = 1.0,
        reconnect_ceiling_seconds: float = 60.0,
        connector: Connector | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self.username = username
        self.secret = secret
        self.connect_timeout_seconds = connect_timeout_seconds
        self.action_timeout_seconds = action_timeout_seconds
        self._backoff = BackoffPolicy(reconnect_base_seconds, reconnect_ceiling_seconds)
        self._connector = connector or self._default_connector

        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._pending: dict[str, asyncio.Future[ManagerMessage]] = {}
        self._handlers: list[EventHandler] = []
        self._identifiers = itertools.count(1)
        self._runner: asyncio.Task[None] | None = None
        self._running = False
        self._write_lock = asyncio.Lock()

        self.connected = False
        self.connection_count = 0
        self.last_error: str | None = None
        self.greeting: str | None = None

    # -- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        """Begin maintaining the connection in the background."""
        if self._running:
            return
        self._running = True
        self._runner = asyncio.create_task(self._maintain(), name="manager-connection")

    async def stop(self) -> None:
        """Stop maintaining the connection and release every pending action."""
        self._running = False
        if self._runner is not None:
            self._runner.cancel()
            try:
                await self._runner
            except asyncio.CancelledError:
                pass
            self._runner = None
        await self._teardown("the appliance is shutting down")

    def add_event_handler(self, handler: EventHandler) -> None:
        self._handlers.append(handler)

    # -- connection maintenance -------------------------------------------

    async def _maintain(self) -> None:
        while self._running:
            try:
                await self._connect_and_serve()
            except asyncio.CancelledError:
                raise
            except Exception as error:  # noqa: BLE001 - the loop must survive anything
                self.last_error = str(error)
                _LOG.warning("the manager connection failed: %s", error)

            if not self._running:
                break

            await self._teardown(self.last_error or "the connection closed")
            delay = self._backoff.next_delay()
            _LOG.info(
                "reconnecting to the manager interface after a delay of %.1f seconds "
                "(attempt number %d)",
                delay,
                self._backoff.attempt,
            )
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                raise

    async def _default_connector(self) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        return await asyncio.wait_for(
            asyncio.open_connection(self.host, self.port),
            timeout=self.connect_timeout_seconds,
        )

    async def _connect_and_serve(self) -> None:
        reader, writer = await self._connector()
        self._reader, self._writer = reader, writer

        greeting = await asyncio.wait_for(
            reader.readline(), timeout=self.connect_timeout_seconds
        )
        self.greeting = greeting.decode("utf-8", "replace").strip()
        _LOG.info("the manager interface greeted us with %s", self.greeting or "no banner")

        # The read loop must be running before the login action is sent.  The
        # login response is correlated by the same dispatch path as every other
        # response, so authenticating before the loop exists would leave the
        # login awaiting a reply that nothing is reading.
        reading = asyncio.create_task(self._read_loop(reader), name="manager-read")
        try:
            authenticating = asyncio.create_task(
                self._authenticate(), name="manager-login"
            )
            done, _ = await asyncio.wait(
                {reading, authenticating}, return_when=asyncio.FIRST_COMPLETED
            )

            if authenticating not in done:
                # The connection died while the login was in flight.  Cancel
                # the login and surface the reason the read loop recorded.
                authenticating.cancel()
                try:
                    await authenticating
                except (asyncio.CancelledError, ManagerError):
                    pass
                await reading  # re-raises the underlying failure
                raise ManagerError("the connection closed during authentication")

            # Raises if the engine refused the credentials.
            await authenticating

            self.connected = True
            self.connection_count += 1
            self._backoff.reset()
            self.last_error = None
            await self._publish(ManagerMessage({"event": ["ApplianceManagerConnected"]}))

            await reading
        finally:
            if not reading.done():
                reading.cancel()
                try:
                    await reading
                except (asyncio.CancelledError, Exception):  # noqa: B014
                    pass
            if self.connected:
                self.connected = False
                await self._publish(
                    ManagerMessage({"event": ["ApplianceManagerDisconnected"]})
                )

    async def _authenticate(self) -> None:
        response = await self._send_and_wait(
            "Login",
            {"Username": self.username, "Secret": self.secret, "Events": "on"},
            timeout=self.connect_timeout_seconds,
            require_connection=False,
        )
        if not response.is_success:
            raise ManagerError(
                "the manager interface refused the credentials: "
                f"{response.get('message', 'no reason given')}"
            )

    async def _read_loop(self, reader: asyncio.StreamReader) -> None:
        buffer = bytearray()
        while self._running:
            chunk = await reader.read(65536)
            if not chunk:
                raise ManagerError("the manager interface closed the connection")
            buffer.extend(chunk)

            while _TERMINATOR in buffer:
                block, _, remainder = bytes(buffer).partition(_TERMINATOR)
                buffer = bytearray(remainder)
                message = parse_message(block.decode("utf-8", "replace"))
                await self._dispatch(message)

    async def _dispatch(self, message: ManagerMessage) -> None:
        identifier = message.action_id
        if identifier and identifier in self._pending:
            future = self._pending.pop(identifier)
            if not future.done():
                future.set_result(message)
            # A response may also be of interest to observers, so it is
            # published as well as resolved.
        if message.event:
            await self._publish(message)

    async def _publish(self, message: ManagerMessage) -> None:
        for handler in list(self._handlers):
            try:
                result = handler(message)
                if asyncio.iscoroutine(result):
                    await result
            except Exception as error:  # noqa: BLE001 - one bad observer is not fatal
                _LOG.error("an event handler raised an error and was skipped: %s", error)

    async def _teardown(self, reason: str) -> None:
        self.connected = False
        writer, self._writer = self._writer, None
        self._reader = None

        for identifier, future in list(self._pending.items()):
            if not future.done():
                future.set_exception(ManagerNotConnected(reason))
            self._pending.pop(identifier, None)

        if writer is not None:
            try:
                writer.close()
                await writer.wait_closed()
            except (OSError, RuntimeError, asyncio.CancelledError):
                pass

    # -- actions -----------------------------------------------------------

    async def send_action(
        self,
        action: str,
        fields: Mapping[str, Any] | None = None,
        timeout: float | None = None,
    ) -> ManagerMessage:
        """Send an action and await its correlated response."""
        return await self._send_and_wait(action, fields, timeout=timeout)

    async def _send_and_wait(
        self,
        action: str,
        fields: Mapping[str, Any] | None = None,
        timeout: float | None = None,
        require_connection: bool = True,
    ) -> ManagerMessage:
        if require_connection and not self.connected:
            raise ManagerNotConnected("the manager interface is not connected")
        writer = self._writer
        if writer is None:
            raise ManagerNotConnected("the manager interface is not connected")

        identifier = f"crossbar-{next(self._identifiers)}"
        payload = dict(fields or {})
        payload["ActionID"] = identifier

        loop = asyncio.get_running_loop()
        future: asyncio.Future[ManagerMessage] = loop.create_future()
        self._pending[identifier] = future

        try:
            async with self._write_lock:
                writer.write(encode_action(action, payload))
                await writer.drain()
            return await asyncio.wait_for(
                future, timeout=timeout or self.action_timeout_seconds
            )
        except asyncio.TimeoutError as error:
            raise ManagerError(
                f"the action named {action} received no response within its timeout"
            ) from error
        finally:
            self._pending.pop(identifier, None)

    async def ping(self) -> bool:
        """Probe the manager interface, reporting reachability as a truth value."""
        try:
            response = await self.send_action("Ping", timeout=self.action_timeout_seconds)
        except ManagerError:
            return False
        return response.is_success or response.get("ping") == "Pong"

    # -- introspection -----------------------------------------------------

    def status(self) -> dict[str, Any]:
        return {
            "connected": self.connected,
            "host": self.host,
            "port": self.port,
            "connection_count": self.connection_count,
            "pending_actions": len(self._pending),
            "retry_attempt": self._backoff.attempt,
            "greeting": self.greeting,
            "last_error": self.last_error,
        }

    @property
    def pending_action_count(self) -> int:
        return len(self._pending)

    @property
    def handlers(self) -> Iterable[EventHandler]:
        return tuple(self._handlers)
