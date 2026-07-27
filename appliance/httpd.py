"""HyperText Transfer Protocol server with socket upgrade dispatch.

The appliance listens once.  A request carrying a socket upgrade header is
promoted to the persistent bidirectional protocol; everything else is served as
an ordinary request.  One port to firewall, one port to document, and the
dashboard is same origin with its own socket by construction.

Request head parsing is a pure function so that malformed input — oversized
headers, absent separators, a body length that disagrees with the body — is
exercised by unit tests rather than discovered in production.
"""

from __future__ import annotations

import asyncio
import json
import mimetypes
import posixpath
import time
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping

from . import wsproto
from .logging_setup import get_logger

__all__ = [
    "Request",
    "Response",
    "Router",
    "HttpServer",
    "parse_request_head",
    "RequestTooLarge",
    "MalformedRequest",
]

_LOG = get_logger("httpd")

_MAXIMUM_HEAD_BYTES = 16384
_MAXIMUM_BODY_BYTES = 1048576
_HEAD_TERMINATOR = b"\r\n\r\n"

_STATUS_TEXT = {
    200: "OK",
    204: "No Content",
    301: "Moved Permanently",
    304: "Not Modified",
    400: "Bad Request",
    401: "Unauthorized",
    403: "Forbidden",
    404: "Not Found",
    405: "Method Not Allowed",
    409: "Conflict",
    413: "Payload Too Large",
    426: "Upgrade Required",
    429: "Too Many Requests",
    500: "Internal Server Error",
    503: "Service Unavailable",
}

#: Applied to every response.  The dashboard loads nothing from anywhere else,
#: so the policy can be maximally restrictive without breaking it.
_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
        "connect-src 'self' ws: wss:; frame-ancestors 'none'; base-uri 'none'; "
        "form-action 'self'"
    ),
    "Cache-Control": "no-store",
}


class MalformedRequest(ValueError):
    """The request head could not be understood."""


class RequestTooLarge(ValueError):
    """The request head or body exceeded the permitted size."""


@dataclass
class Request:
    """One parsed request."""

    method: str
    target: str
    path: str
    version: str
    headers: Mapping[str, str]
    query: Mapping[str, str] = field(default_factory=dict)
    body: bytes = b""
    peer: str = "unknown"
    #: Bound by the router when a parameterised route matches.
    path_parameters: dict[str, str] = field(default_factory=dict)

    def parameter(self, name: str, default: str = "") -> str:
        return self.path_parameters.get(name, default)

    def header(self, name: str, default: str = "") -> str:
        return self.headers.get(name.lower(), default)

    @property
    def content_length(self) -> int:
        try:
            return int(self.header("content-length", "0") or 0)
        except ValueError:
            return 0

    @property
    def is_upgrade(self) -> bool:
        return (
            "upgrade" in self.header("connection").lower()
            and self.header("upgrade").lower() == "websocket"
        )

    @property
    def keep_alive(self) -> bool:
        connection = self.header("connection").lower()
        if self.version == "HTTP/1.0":
            return "keep-alive" in connection
        return "close" not in connection

    def cookie(self, name: str) -> str | None:
        raw = self.header("cookie")
        if not raw:
            return None
        for part in raw.split(";"):
            key, separator, value = part.strip().partition("=")
            if separator and key == name:
                return value
        return None

    def json(self) -> Any:
        if not self.body:
            return None
        try:
            return json.loads(self.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise MalformedRequest(f"the request body is not valid: {error}") from error


@dataclass
class Response:
    """One response to serialise."""

    status: int = 200
    body: bytes = b""
    content_type: str = "application/json; charset=utf-8"
    headers: dict[str, str] = field(default_factory=dict)

    @classmethod
    def json(cls, payload: Any, status: int = 200, headers: dict[str, str] | None = None) -> "Response":
        body = json.dumps(payload, default=str).encode("utf-8")
        return cls(status=status, body=body, headers=headers or {})

    @classmethod
    def text(cls, message: str, status: int = 200) -> "Response":
        return cls(
            status=status,
            body=message.encode("utf-8"),
            content_type="text/plain; charset=utf-8",
        )

    @classmethod
    def error(cls, status: int, reason: str) -> "Response":
        return cls.json({"error": reason, "status": status}, status=status)

    def serialise(self, keep_alive: bool = True) -> bytes:
        reason = _STATUS_TEXT.get(self.status, "Unknown")
        headers = {
            "Content-Type": self.content_type,
            "Content-Length": str(len(self.body)),
            "Connection": "keep-alive" if keep_alive else "close",
            "Date": time.strftime("%a, %d %b %Y %H:%M:%S GMT", time.gmtime()),
            **_SECURITY_HEADERS,
            **self.headers,
        }
        head = f"HTTP/1.1 {self.status} {reason}\r\n"
        head += "".join(f"{name}: {value}\r\n" for name, value in headers.items())
        return head.encode("latin-1") + b"\r\n" + self.body


Handler = Callable[[Request], Awaitable[Response] | Response]


class Router:
    """A route table with parameterised paths and a static file fallback.

    A path segment written as ``{name}`` matches any single segment and binds
    it as a path parameter on the request.  Exact routes are always preferred
    over parameterised ones, so a specific path can never be shadowed by a
    general one registered earlier.
    """

    def __init__(self) -> None:
        self._routes: dict[tuple[str, str], Handler] = {}
        self._patterns: list[tuple[str, tuple[str, ...], Handler]] = []
        self._static_root: Path | None = None

    def add(self, method: str, path: str, handler: Handler) -> None:
        if "{" in path:
            segments = tuple(path.strip("/").split("/"))
            self._patterns.append((method.upper(), segments, handler))
        else:
            self._routes[(method.upper(), path)] = handler

    def get(self, path: str, handler: Handler) -> None:
        self.add("GET", path, handler)

    def post(self, path: str, handler: Handler) -> None:
        self.add("POST", path, handler)

    def put(self, path: str, handler: Handler) -> None:
        self.add("PUT", path, handler)

    def delete(self, path: str, handler: Handler) -> None:
        self.add("DELETE", path, handler)

    def serve_static(self, root: str | Path) -> None:
        self._static_root = Path(root)

    def resolve(
        self, method: str, path: str
    ) -> tuple[Handler, dict[str, str]] | None:
        handler = self._routes.get((method.upper(), path))
        if handler is not None:
            return handler, {}

        wanted = tuple(segment for segment in path.strip("/").split("/") if segment != "")
        for route_method, segments, candidate in self._patterns:
            if route_method != method.upper() or len(segments) != len(wanted):
                continue
            parameters: dict[str, str] = {}
            for declared, actual in zip(segments, wanted):
                if declared.startswith("{") and declared.endswith("}"):
                    parameters[declared[1:-1]] = actual
                elif declared != actual:
                    break
            else:
                return candidate, parameters
        return None

    def path_exists(self, path: str) -> bool:
        """Report whether any method serves this path.

        Used to distinguish a wrong method from a missing route, so that a
        client defect is not misdiagnosed as a deployment problem.
        """
        if any(route_path == path for _, route_path in self._routes):
            return True

        wanted = tuple(segment for segment in path.strip("/").split("/") if segment != "")
        for _, segments, _ in self._patterns:
            if len(segments) != len(wanted):
                continue
            for declared, actual in zip(segments, wanted):
                if not (declared.startswith("{") and declared.endswith("}")) and declared != actual:
                    break
            else:
                return True
        return False

    async def dispatch(self, request: Request) -> Response:
        resolved = self.resolve(request.method, request.path)
        if resolved is not None:
            handler, parameters = resolved
            request.path_parameters = parameters
            outcome = handler(request)
            if asyncio.iscoroutine(outcome):
                return await outcome
            return outcome  # type: ignore[return-value]

        if self.path_exists(request.path):
            return Response.error(405, f"the method {request.method} is not accepted here")

        if request.method in ("GET", "HEAD") and self._static_root is not None:
            return self._serve_file(request)

        return Response.error(404, "there is no resource at that path")

    def _serve_file(self, request: Request) -> Response:
        assert self._static_root is not None
        relative = request.path.lstrip("/") or "index.html"

        # Normalise before joining so that a traversal attempt is defeated by
        # construction rather than by inspection of the resulting path.
        normalised = posixpath.normpath("/" + relative).lstrip("/")
        if normalised.startswith("..") or normalised.startswith("/"):
            return Response.error(403, "that path may not be requested")

        candidate = (self._static_root / normalised).resolve()
        try:
            root = self._static_root.resolve()
        except OSError:
            return Response.error(503, "the interface files are not available")

        if root != candidate and root not in candidate.parents:
            return Response.error(403, "that path may not be requested")
        if candidate.is_dir():
            candidate = candidate / "index.html"
        if not candidate.is_file():
            return Response.error(404, "there is no interface file at that path")

        try:
            payload = candidate.read_bytes()
        except OSError as error:
            _LOG.error("an interface file could not be read: %s", error)
            return Response.error(500, "the interface file could not be read")

        guessed, _ = mimetypes.guess_type(candidate.name)
        body = b"" if request.method == "HEAD" else payload
        response = Response(
            status=200,
            body=body,
            content_type=guessed or "application/octet-stream",
        )
        if request.method == "HEAD":
            response.headers["Content-Length"] = str(len(payload))
        return response


def parse_request_head(raw: bytes, peer: str = "unknown") -> Request:
    """Parse a request head into a request object.

    The head is the request line plus headers, without the terminating blank
    line.  Every failure mode raises rather than returning a partially
    populated request, so a caller cannot accidentally act on a malformed one.
    """
    if len(raw) > _MAXIMUM_HEAD_BYTES:
        raise RequestTooLarge("the request head exceeds the permitted size")

    try:
        text = raw.decode("latin-1")
    except UnicodeDecodeError as error:
        raise MalformedRequest("the request head is not decodable") from error

    lines = text.split("\r\n")
    if not lines or not lines[0].strip():
        raise MalformedRequest("the request line is absent")

    parts = lines[0].split()
    if len(parts) != 3:
        raise MalformedRequest("the request line is not well formed")
    method, target, version = parts
    if not version.startswith("HTTP/"):
        raise MalformedRequest("the protocol version is not recognised")

    headers: dict[str, str] = {}
    for line in lines[1:]:
        if not line.strip():
            continue
        name, separator, value = line.partition(":")
        if not separator:
            raise MalformedRequest("a header line carried no separator")
        lowered = name.strip().lower()
        stripped = value.strip()
        # A repeated header is joined rather than silently dropped, which is
        # what the specification requires for list valued headers.
        headers[lowered] = f"{headers[lowered]}, {stripped}" if lowered in headers else stripped

    split = urllib.parse.urlsplit(target)
    query = {
        key: values[0]
        for key, values in urllib.parse.parse_qs(split.query, keep_blank_values=True).items()
    }

    return Request(
        method=method.upper(),
        target=target,
        path=urllib.parse.unquote(split.path) or "/",
        version=version,
        headers=headers,
        query=query,
        peer=peer,
    )


UpgradeHandler = Callable[
    [Request, asyncio.StreamReader, asyncio.StreamWriter], Awaitable[None]
]


class HttpServer:
    """The appliance listener."""

    def __init__(
        self,
        router: Router,
        host: str = "0.0.0.0",
        port: int = 8088,
        upgrade_handler: UpgradeHandler | None = None,
        maximum_body_bytes: int = _MAXIMUM_BODY_BYTES,
        idle_timeout_seconds: float = 60.0,
    ) -> None:
        self.router = router
        self.host = host
        self.port = port
        self.upgrade_handler = upgrade_handler
        self.maximum_body_bytes = maximum_body_bytes
        self.idle_timeout_seconds = idle_timeout_seconds
        self._server: asyncio.AbstractServer | None = None
        self.requests_served = 0
        self.upgrades_accepted = 0

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, self.host, self.port)
        _LOG.info(
            "the appliance interface is listening on the address %s at port number %d",
            self.host,
            self.port,
        )

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    @property
    def sockets_bound(self) -> int:
        return len(self._server.sockets) if self._server is not None else 0

    @property
    def bound_port(self) -> int:
        """The port actually bound, which differs from the requested port when
        the appliance is asked to bind an ephemeral one."""
        if self._server is None or not self._server.sockets:
            return 0
        return int(self._server.sockets[0].getsockname()[1])

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        peer = _describe_peer(writer)
        try:
            while True:
                request = await self._read_request(reader, peer)
                if request is None:
                    return

                if request.is_upgrade:
                    if self.upgrade_handler is None:
                        await _write(writer, Response.error(426, "upgrades are not accepted"))
                        return
                    self.upgrades_accepted += 1
                    # Ownership of the stream transfers to the upgrade handler.
                    await self.upgrade_handler(request, reader, writer)
                    return

                response = await self._safe_dispatch(request)
                self.requests_served += 1
                keep_alive = request.keep_alive and response.status < 500
                await _write(writer, response, keep_alive)
                if not keep_alive:
                    return
        except (asyncio.IncompleteReadError, ConnectionResetError, BrokenPipeError):
            return
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 - one connection must not kill the server
            _LOG.error("a connection handler failed: %s", error)
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except (OSError, ConnectionError, RuntimeError):
                pass

    async def _read_request(
        self, reader: asyncio.StreamReader, peer: str
    ) -> Request | None:
        buffer = bytearray()
        while _HEAD_TERMINATOR not in buffer:
            try:
                chunk = await asyncio.wait_for(
                    reader.read(4096), timeout=self.idle_timeout_seconds
                )
            except asyncio.TimeoutError:
                return None
            if not chunk:
                return None
            buffer.extend(chunk)
            if len(buffer) > _MAXIMUM_HEAD_BYTES:
                raise RequestTooLarge("the request head exceeds the permitted size")

        head, _, remainder = bytes(buffer).partition(_HEAD_TERMINATOR)
        request = parse_request_head(head, peer)

        length = request.content_length
        if length > self.maximum_body_bytes:
            raise RequestTooLarge("the request body exceeds the permitted size")
        if length:
            body = bytearray(remainder[:length])
            while len(body) < length:
                chunk = await asyncio.wait_for(
                    reader.read(length - len(body)), timeout=self.idle_timeout_seconds
                )
                if not chunk:
                    raise MalformedRequest("the request body ended before it was complete")
                body.extend(chunk)
            request.body = bytes(body)

        return request

    async def _safe_dispatch(self, request: Request) -> Response:
        try:
            return await self.router.dispatch(request)
        except MalformedRequest as error:
            return Response.error(400, str(error))
        except RequestTooLarge as error:
            return Response.error(413, str(error))
        except Exception as error:  # noqa: BLE001 - a handler defect is a server error
            _LOG.error(
                "the handler for the path %s failed: %s", request.path, error, exc_info=True
            )
            return Response.error(500, "the appliance could not complete the request")


def build_handshake_response(request: Request) -> Response:
    """Build the socket upgrade acceptance response, or the refusal."""
    key = request.header("sec-websocket-key")
    if not key or not wsproto.validate_client_key(key):
        return Response.error(400, "the upgrade request carried no usable key")

    version = request.header("sec-websocket-version")
    if version and version.strip() != "13":
        return Response(
            status=426,
            body=b'{"error": "the requested socket protocol version is not supported"}',
            headers={"Sec-WebSocket-Version": "13"},
        )

    return Response(
        status=101,
        headers={
            "Upgrade": "websocket",
            "Connection": "Upgrade",
            "Sec-WebSocket-Accept": wsproto.compute_accept_token(key),
        },
    )


def serialise_handshake(response: Response) -> bytes:
    """Serialise the upgrade acceptance, which carries no body or length."""
    if response.status != 101:
        return response.serialise(keep_alive=False)
    head = "HTTP/1.1 101 Switching Protocols\r\n"
    head += "".join(f"{name}: {value}\r\n" for name, value in response.headers.items())
    return head.encode("latin-1") + b"\r\n"


async def _write(
    writer: asyncio.StreamWriter, response: Response, keep_alive: bool = False
) -> None:
    writer.write(response.serialise(keep_alive))
    await writer.drain()


def _describe_peer(writer: asyncio.StreamWriter) -> str:
    try:
        info = writer.get_extra_info("peername")
    except (OSError, AttributeError):
        return "unknown"
    if isinstance(info, tuple) and info:
        return str(info[0])
    return "unknown"
