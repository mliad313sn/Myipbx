"""HyperText Transfer Protocol server with socket upgrade dispatch.

The appliance listens once.  A request carrying a socket upgrade header is
promoted to the persistent bidirectional protocol; everything else is served as
an ordinary request.  One port to firewall, one port to document, and the
dashboard is same origin with its own socket by construction.

Request head parsing is a pure function so that malformed input — oversized
headers, absent separators, a body length that disagrees with the body — is
exercised by unit tests rather than discovered in production.

The listener is secured.  Everything the console carries — the administrator's
password on the way in, the session cookie on every request afterwards, and the
telephone numbers of an entire site in between — would otherwise be readable by
anything sharing the network with the appliance.  The secured listener is built
from the standard library alone, because the appliances this runs on are old
and air gapped and cannot be asked to acquire a package to be safe.
"""

from __future__ import annotations

import asyncio
import json
import mimetypes
import posixpath
import ssl
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
    "RedirectServer",
    "build_tls_context",
    "certificate_fingerprint",
    "validate_certificate_pair",
    "parse_request_head",
    "RequestTooLarge",
    "MalformedRequest",
    "TlsConfigurationError",
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
    308: "Permanent Redirect",
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


class TlsConfigurationError(RuntimeError):
    """The secured listener could not be built from what was configured."""


#: The floor is a negotiated version, not a cipher list.  Naming ciphers here
#: would freeze this appliance's idea of which ones are sound at the moment it
#: shipped, and these machines are not updated often; the library's own default
#: selection for the version floor ages better than a list written once.
_TLS_VERSIONS = {
    "TLSv1.2": ssl.TLSVersion.TLSv1_2,
    "TLSv1.3": ssl.TLSVersion.TLSv1_3,
}


def build_tls_context(
    certificate: str | Path,
    private_key: str | Path,
    minimum_version: str = "TLSv1.2",
) -> ssl.SSLContext:
    """Build the secured listener's context, or explain why it cannot be built.

    Every refusal names the file it was looking at and what to do about it.  An
    appliance that will not start is an appliance somebody is standing in front
    of at an inconvenient hour, and "certificate error" would tell them nothing
    they could act on.
    """
    certificate_path = Path(certificate)
    key_path = Path(private_key)

    if minimum_version not in _TLS_VERSIONS:
        raise TlsConfigurationError(
            f"the minimum transport security version {minimum_version} is not one "
            "this appliance will negotiate; name either TLSv1.2 or TLSv1.3"
        )

    for description, path in (
        ("certificate", certificate_path),
        ("private key", key_path),
    ):
        if not path.exists():
            raise TlsConfigurationError(
                f"the transport security {description} at {path} does not exist; "
                "generate one by running the script named "
                "crossbar-generate-certificate.sh, or name an existing file in the "
                "configuration document"
            )
        if not path.is_file():
            raise TlsConfigurationError(
                f"the transport security {description} at {path} is not a file"
            )
        try:
            with path.open("rb"):
                pass
        except OSError as error:
            raise TlsConfigurationError(
                f"the transport security {description} at {path} could not be read "
                f"by this appliance: {error}; the private key is expected to be "
                "readable by the group named crossbar and by nobody else"
            ) from error

    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = _TLS_VERSIONS[minimum_version]
    try:
        context.load_cert_chain(certfile=str(certificate_path), keyfile=str(key_path))
    except ssl.SSLError as error:
        # This is the case where both files are present and readable but do not
        # belong together, which is what a half finished manual installation
        # leaves behind.
        raise TlsConfigurationError(
            f"the certificate at {certificate_path} and the private key at "
            f"{key_path} could not be loaded together: {error}; they are most "
            "likely from different generations and must be replaced as a pair"
        ) from error
    except OSError as error:
        raise TlsConfigurationError(
            f"the transport security material could not be read: {error}"
        ) from error
    return context


def validate_certificate_pair(certificate_pem: str, private_key_pem: str) -> str:
    """Check that a certificate and a private key belong together.

    The check is a real load of the pair, because that is the only thing that
    answers the question the appliance actually has: will the secured listener
    come up on these two files?  Comparing them by inspection would accept
    material that the library then refused, and the refusal would arrive after
    the restart, with the console down and the operator holding a browser tab
    that no longer answers.

    Returns the certificate's fingerprint so that a caller can show an operator
    what they have just installed.
    """
    import tempfile

    if not certificate_pem.strip():
        raise TlsConfigurationError("no certificate was supplied")
    if not private_key_pem.strip():
        raise TlsConfigurationError("no private key was supplied")

    # Written with owner only permissions into a directory that is removed
    # whatever happens, so an uploaded key is never left on disk by a
    # validation that failed halfway.
    with tempfile.TemporaryDirectory(prefix="crossbar-certificate-") as workspace:
        directory = Path(workspace)
        certificate_path = directory / "candidate.crt"
        key_path = directory / "candidate.key"
        certificate_path.write_text(certificate_pem, encoding="utf-8")
        key_path.write_text(private_key_pem, encoding="utf-8")
        key_path.chmod(0o600)

        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        try:
            context.load_cert_chain(certfile=str(certificate_path), keyfile=str(key_path))
        except ssl.SSLError as error:
            raise TlsConfigurationError(
                "the certificate and the private key do not match, or one of "
                f"them is not in the expected form: {error}"
            ) from error
        except OSError as error:
            raise TlsConfigurationError(
                f"the supplied material could not be read: {error}"
            ) from error

        return certificate_fingerprint(certificate_path)


def certificate_fingerprint(certificate: str | Path) -> str:
    """The certificate's own digest, in the form an operator can compare.

    This is what a browser shows when somebody asks it to explain the warning
    on an appliance that signed its own certificate, so it is what the console
    prints and what the interface reports.
    """
    import hashlib

    text = Path(certificate).read_text(encoding="utf-8")
    try:
        der = ssl.PEM_cert_to_DER_cert(text)
    except ValueError as error:
        raise TlsConfigurationError(
            f"the file at {certificate} is not a certificate in the expected form"
        ) from error
    digest = hashlib.sha256(der).hexdigest().upper()
    return ":".join(digest[index : index + 2] for index in range(0, len(digest), 2))


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
        host: str = "127.0.0.1",
        port: int = 8088,
        upgrade_handler: UpgradeHandler | None = None,
        maximum_body_bytes: int = _MAXIMUM_BODY_BYTES,
        idle_timeout_seconds: float = 60.0,
        tls_context: ssl.SSLContext | None = None,
    ) -> None:
        self.router = router
        self.host = host
        self.port = port
        self.upgrade_handler = upgrade_handler
        self.maximum_body_bytes = maximum_body_bytes
        self.idle_timeout_seconds = idle_timeout_seconds
        self.tls_context = tls_context
        self._server: asyncio.AbstractServer | None = None
        self.requests_served = 0
        self.upgrades_accepted = 0

    @property
    def secured(self) -> bool:
        return self.tls_context is not None

    async def start(self) -> None:
        self._server = await asyncio.start_server(
            self._handle, self.host, self.port, ssl=self.tls_context
        )
        _LOG.info(
            "the appliance interface is listening on the address %s at port number %d, "
            "over %s transport",
            self.host,
            self.port,
            "secured" if self.secured else "plain",
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


class RedirectServer:
    """A plain listener that answers every request with the secured address.

    An operator who types this appliance's address without a scheme gets plain
    transport, and a port that refuses the connection tells them only that
    something is wrong.  So a second port answers, and it answers with one
    thing: go to the secured listener instead.

    It serves no content, consults no router, and reads no request body.  It
    never issues a cookie, because a cookie issued here would be exactly the
    plain transport disclosure the secured listener exists to prevent — and the
    session cookie now carries the attribute that would make a browser discard
    it anyway.
    """

    def __init__(
        self,
        host: str,
        port: int,
        secure_port: int,
        idle_timeout_seconds: float = 10.0,
    ) -> None:
        self.host = host
        self.port = port
        self.secure_port = secure_port
        self.idle_timeout_seconds = idle_timeout_seconds
        self._server: asyncio.AbstractServer | None = None
        self.redirects_issued = 0

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, self.host, self.port)
        _LOG.info(
            "the plain listener on the address %s at port number %d answers only with "
            "the secured address and serves nothing",
            self.host,
            self.bound_port,
        )

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    @property
    def bound_port(self) -> int:
        if self._server is None or not self._server.sockets:
            return 0
        return int(self._server.sockets[0].getsockname()[1])

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            buffer = bytearray()
            while _HEAD_TERMINATOR not in buffer:
                chunk = await asyncio.wait_for(
                    reader.read(4096), timeout=self.idle_timeout_seconds
                )
                if not chunk:
                    return
                buffer.extend(chunk)
                if len(buffer) > _MAXIMUM_HEAD_BYTES:
                    # Even the refusal is a redirect, so that this port has no
                    # second behaviour anybody could come to depend on.
                    break

            head, _, _ = bytes(buffer).partition(_HEAD_TERMINATOR)
            response = self._redirect(head)
            self.redirects_issued += 1
            writer.write(response.serialise(keep_alive=False))
            await writer.drain()
        except (
            asyncio.TimeoutError,
            asyncio.IncompleteReadError,
            ConnectionResetError,
            BrokenPipeError,
        ):
            return
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 - one connection must not kill the port
            _LOG.error("the plain listener failed to answer a connection: %s", error)
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except (OSError, ConnectionError, RuntimeError):
                pass

    def _redirect(self, head: bytes) -> Response:
        """Build the redirect for one request head.

        A head that cannot be parsed still gets a redirect, to the root of the
        secured listener.  There is nothing this port could usefully say about
        a malformed request that would not amount to serving content.
        """
        target = "/"
        host = self.host
        try:
            request = parse_request_head(head)
        except (MalformedRequest, RequestTooLarge):
            pass
        else:
            target = request.target or "/"
            host = _host_without_port(request.header("host")) or self.host

        location = f"https://{host}"
        if self.secure_port and self.secure_port != 443:
            location += f":{self.secure_port}"
        location += target if target.startswith("/") else f"/{target}"

        # Permanent rather than temporary, and the variant that keeps the
        # method, so that a client repeating a write does not silently have it
        # turned into a read.
        return Response(
            status=308,
            body=b"",
            content_type="text/plain; charset=utf-8",
            headers={"Location": location},
        )


def _host_without_port(value: str) -> str:
    """Strip the port from a host header, leaving bracketed literals intact."""
    text = (value or "").strip()
    if not text:
        return ""
    if text.startswith("["):
        closing = text.find("]")
        return text[: closing + 1] if closing != -1 else ""
    name, separator, port = text.rpartition(":")
    if separator and port.isdigit():
        return name
    return text


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
