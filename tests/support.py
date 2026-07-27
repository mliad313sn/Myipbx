"""Shared test support: a socket client, an appliance harness, and fixtures.

The client here speaks the real protocol over a real socket against the real
appliance.  Nothing about the transport is mocked, because the transport is
exactly what the concurrency and heartbeat tests exist to exercise.
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import secrets
import sys
import tempfile
from pathlib import Path
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from appliance import wsproto  # noqa: E402
from appliance.ami import ManagerMessage  # noqa: E402
from appliance.config import ApplianceConfig  # noqa: E402
from appliance.logging_setup import configure_logging  # noqa: E402
from appliance.security import CredentialStore, PasswordHasher  # noqa: E402
from appliance.server import Appliance  # noqa: E402

TEST_USERNAME = "administrator"
TEST_PASSWORD = "an-example-appliance-password"

#: Key derivation is deliberately expensive in production.  The tests lower it
#: so that a suite exercising hundreds of sessions finishes in seconds; the
#: production iteration count is asserted separately in the security tests.
TEST_ITERATIONS = 100000


def engine_event(name: str, **fields: Any) -> ManagerMessage:
    """Build a manager message as the engine would deliver it."""
    parsed: dict[str, list[str]] = {"event": [name]}
    for key, value in fields.items():
        parsed[key.lower()] = [str(value)]
    return ManagerMessage(parsed)


class SocketTestClient:
    """A minimal client for the appliance's persistent bidirectional socket."""

    def __init__(self, host: str, port: int, cookie: str) -> None:
        self.host = host
        self.port = port
        self.cookie = cookie
        self.reader: asyncio.StreamReader | None = None
        self.writer: asyncio.StreamWriter | None = None
        self.decoder = wsproto.FrameDecoder(require_mask=False)
        self.received: list[dict[str, Any]] = []
        self.pings_received = 0

    async def connect(self, path: str = "/socket") -> bool:
        self.reader, self.writer = await asyncio.open_connection(self.host, self.port)
        key = base64.b64encode(secrets.token_bytes(16)).decode("ascii")

        request = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {self.host}:{self.port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            f"Cookie: {self.cookie}\r\n"
            "\r\n"
        )
        self.writer.write(request.encode("ascii"))
        await self.writer.drain()

        head = await asyncio.wait_for(
            self.reader.readuntil(b"\r\n\r\n"), timeout=10.0
        )
        text = head.decode("latin-1")
        if "101" not in text.split("\r\n")[0]:
            return False
        return wsproto.compute_accept_token(key) in text

    async def send(self, payload: dict[str, Any]) -> None:
        assert self.writer is not None
        frame = wsproto.build_frame(
            wsproto.OPCODE_TEXT, json.dumps(payload).encode("utf-8"), mask=True
        )
        self.writer.write(frame)
        await self.writer.drain()

    async def pump(self, timeout: float = 1.0) -> list[dict[str, Any]]:
        """Read whatever has arrived, answering protocol pings as a browser would."""
        assert self.reader is not None and self.writer is not None
        collected: list[dict[str, Any]] = []
        try:
            while True:
                chunk = await asyncio.wait_for(self.reader.read(65536), timeout=timeout)
                if not chunk:
                    break
                for event in self.decoder.feed(chunk):
                    if isinstance(event, wsproto.TextMessage):
                        envelope = json.loads(event.text)
                        collected.append(envelope)
                        self.received.append(envelope)
                    elif isinstance(event, wsproto.PingReceived):
                        self.pings_received += 1
                        self.writer.write(
                            wsproto.build_frame(
                                wsproto.OPCODE_PONG, event.payload, mask=True
                            )
                        )
                        await self.writer.drain()
                if collected:
                    break
        except asyncio.TimeoutError:
            pass
        except (asyncio.IncompleteReadError, ConnectionResetError):
            pass
        return collected

    async def close(self) -> None:
        if self.writer is None:
            return
        try:
            self.writer.write(wsproto.build_close_frame(wsproto.CLOSE_NORMAL, "done"))
            await self.writer.drain()
        except (OSError, ConnectionError):
            pass
        try:
            self.writer.close()
            await self.writer.wait_closed()
        except (OSError, ConnectionError, RuntimeError):
            pass


class ApplianceHarness:
    """An appliance built in a temporary directory tree, bound to a free port."""

    def __init__(self, **overrides: Any) -> None:
        self.directory = tempfile.TemporaryDirectory(prefix="myipbx-test-")
        self.root = Path(self.directory.name)
        (self.root / "state").mkdir()
        (self.root / "asterisk").mkdir()

        settings: dict[str, Any] = {
            "listen_address": "127.0.0.1",
            "listen_port": 0,
            "web_root": str(REPOSITORY_ROOT / "web"),
            "state_directory": str(self.root / "state"),
            "configuration_document": str(self.root / "appliance.json"),
            "asterisk_configuration_directory": str(self.root / "asterisk"),
            "log_file": str(self.root / "appliance.log"),
            # The engine is deliberately unreachable: the appliance must serve
            # its dashboard and report the engine as disconnected rather than
            # refusing to start.
            "manager_host": "127.0.0.1",
            "manager_port": 1,
            "manager_connect_timeout_seconds": 0.2,
            # Long by default so that a test which is not about the heartbeat
            # is never disturbed by one; the heartbeat tests drive the sweep
            # directly instead of waiting on wall clock time.
            "heartbeat_interval_seconds": 30.0,
            "health_sweep_interval_seconds": 3600.0,
            "hardware_rescan_interval_seconds": 3600.0,
            "password_iterations": TEST_ITERATIONS,
            # A laboratory machine may legitimately run an allocation service;
            # the exclusion itself is asserted directly in its own tests.
            "fail_on_address_allocation_server": False,
            # The harness serves over plain transport so that the suite can
            # speak to it with an ordinary socket.  This is the one place that
            # decision is made, and it is made here rather than by weakening
            # the shipped default, which stays secured.  The secured listener
            # has its own tests, and they generate a certificate at test time
            # and complete a real handshake against it; a harness built with
            # transport security switched on is what those tests use.
            "tls_enabled": False,
            # Nothing to redirect to when the transport is plain, and binding a
            # fixed second port would collide between tests running together.
            "plain_http_redirect_port": 0,
        }
        settings.update(overrides)

        self.config = ApplianceConfig.from_mapping(settings)
        self.appliance: Appliance | None = None
        self.cookie = ""

    def write_document(self, document: dict[str, Any]) -> None:
        (self.root / "appliance.json").write_text(
            json.dumps(document, indent=2), encoding="utf-8"
        )

    async def start(self) -> Appliance:
        # Pre-seed the credential so that startup does not generate and print
        # one, and so the tests know the password.
        store = CredentialStore(
            self.config.credentials_path, PasswordHasher(TEST_ITERATIONS)
        )
        store.save(TEST_USERNAME, TEST_PASSWORD)

        # Configure logging exactly as the appliance does at run time, with the
        # console diverted so that the suite's own output stays readable.  The
        # log file this produces is what the Constraint Two tests inspect.
        self.log_stream = io.StringIO()
        configure_logging(
            self.config.log_level, self.config.log_file, stream=self.log_stream
        )

        self.appliance = Appliance(self.config)
        await self.appliance.start()
        return self.appliance

    @property
    def port(self) -> int:
        assert self.appliance is not None
        return self.appliance.http.bound_port

    async def sign_in(self) -> str:
        """Sign in over real transport and retain the session cookie."""
        body = json.dumps({"username": TEST_USERNAME, "password": TEST_PASSWORD})
        status, headers, payload = await self.request(
            "POST", "/api/session", body=body
        )
        assert status == 200, f"signing in failed with status {status}: {payload}"
        cookie = headers.get("set-cookie", "")
        self.cookie = cookie.split(";")[0]
        return self.cookie

    async def request(
        self,
        method: str,
        path: str,
        body: str | None = None,
        cookie: str | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> tuple[int, dict[str, str], Any]:
        reader, writer = await asyncio.open_connection("127.0.0.1", self.port)
        encoded = (body or "").encode("utf-8")

        lines = [
            f"{method} {path} HTTP/1.1",
            f"Host: 127.0.0.1:{self.port}",
            "Connection: close",
            f"Content-Length: {len(encoded)}",
        ]
        if encoded:
            lines.append("Content-Type: application/json")
        chosen = cookie if cookie is not None else self.cookie
        if chosen:
            lines.append(f"Cookie: {chosen}")
        for name, value in (extra_headers or {}).items():
            lines.append(f"{name}: {value}")

        writer.write(("\r\n".join(lines) + "\r\n\r\n").encode("ascii") + encoded)
        await writer.drain()

        raw = await asyncio.wait_for(reader.read(-1), timeout=10.0)
        writer.close()
        try:
            await writer.wait_closed()
        except (OSError, ConnectionError):
            pass

        head, _, payload_bytes = raw.partition(b"\r\n\r\n")
        head_text = head.decode("latin-1")
        status_line = head_text.split("\r\n")[0]
        status = int(status_line.split()[1])

        headers: dict[str, str] = {}
        for line in head_text.split("\r\n")[1:]:
            name, separator, value = line.partition(":")
            if separator:
                headers[name.strip().lower()] = value.strip()

        try:
            payload = json.loads(payload_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            payload = payload_bytes

        return status, headers, payload

    async def stop(self) -> None:
        if self.appliance is not None:
            await self.appliance.stop()
            self.appliance = None

        # Release the log file before the temporary tree is removed.
        root_logger = logging.getLogger("myipbx")
        for handler in list(root_logger.handlers):
            root_logger.removeHandler(handler)
            try:
                handler.close()
            except (OSError, ValueError):
                pass

        self.directory.cleanup()


def build_fixture_root(base: Path, allocation_service: bool = False) -> Path:
    """Build a fixture filesystem root for the address allocation audit."""
    root = base / ("machine-with-allocation" if allocation_service else "clean-machine")
    (root / "proc/net").mkdir(parents=True, exist_ok=True)
    (root / "etc/systemd/system").mkdir(parents=True, exist_ok=True)

    header = (
        "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when "
        "retrnsmt   uid  timeout inode ref pointer drops\n"
    )
    if allocation_service:
        # Port number sixty seven in hexadecimal is the allocation server port.
        (root / "proc/net/udp").write_text(
            header + "  1: 00000000:0043 00000000:0000 07 00000000:00000000 "
            "00:00000000 00000000     0        0 12345 2 0000000000000000 0\n",
            encoding="utf-8",
        )
        process = root / "proc/4242"
        process.mkdir(parents=True, exist_ok=True)
        process.joinpath("cmdline").write_bytes(b"/usr/sbin/dhcpd\0-f\0-cf\0/etc/dhcp/dhcpd.conf\0")
        (root / "etc/dhcp").mkdir(parents=True, exist_ok=True)
        (root / "etc/dhcp/dhcpd.conf").write_text(
            "subnet 192.0.2.0 netmask 255.255.255.0 {\n"
            "  range 192.0.2.100 192.0.2.200;\n"
            "}\n",
            encoding="utf-8",
        )
        (root / "etc/systemd/system/isc-dhcp-server.service").write_text(
            "[Service]\nExecStart=/usr/sbin/dhcpd\n", encoding="utf-8"
        )
        wants = root / "etc/systemd/system/multi-user.target.wants"
        wants.mkdir(parents=True, exist_ok=True)
        (wants / "isc-dhcp-server.service").write_text("", encoding="utf-8")
    else:
        # A machine with only ordinary sockets bound: port number one hundred
        # twenty three is the time service, which allocates nothing.
        (root / "proc/net/udp").write_text(
            header + "  1: 00000000:007B 00000000:0000 07 00000000:00000000 "
            "00:00000000 00000000     0        0 54321 2 0000000000000000 0\n",
            encoding="utf-8",
        )
        process = root / "proc/1"
        process.mkdir(parents=True, exist_ok=True)
        process.joinpath("cmdline").write_bytes(b"/sbin/init\0")

    return root


def build_hardware_fixture(base: Path, with_card: bool = True) -> Path:
    """Build a fixture root that presents a legacy Digium interface card."""
    root = base / ("machine-with-card" if with_card else "machine-without-card")
    devices = root / "sys/bus/pci/devices"
    devices.mkdir(parents=True, exist_ok=True)

    if not with_card:
        slot = devices / "0000:00:02.0"
        slot.mkdir(parents=True, exist_ok=True)
        (slot / "vendor").write_text("0x8086\n", encoding="utf-8")
        (slot / "device").write_text("0x100e\n", encoding="utf-8")
        return root

    slot = devices / "0000:02:0a.0"
    slot.mkdir(parents=True, exist_ok=True)
    (slot / "vendor").write_text("0xd161\n", encoding="utf-8")
    (slot / "device").write_text("0x8005\n", encoding="utf-8")

    dahdi = root / "proc/dahdi"
    dahdi.mkdir(parents=True, exist_ok=True)
    (dahdi / "1").write_text(
        'Span 1: WCTDM/0 "Wildcard TDM410P Board 1" (MASTER)\n'
        "\n"
        "           1 WCTDM/0/0 FXOKS (In use)\n"
        "           2 WCTDM/0/1 FXOKS\n"
        "           3 WCTDM/0/2 FXSKS\n"
        "           4 WCTDM/0/3 FXSKS\n",
        encoding="utf-8",
    )
    modules = root / "proc"
    modules.mkdir(parents=True, exist_ok=True)
    (modules / "modules").write_text(
        "dahdi 245760 3 wctdm24xxp, Live 0x0000000000000000\n", encoding="utf-8"
    )
    return root


# ---------------------------------------------------------------------------
# Constraint Two, in the form the appliance now holds it
# ---------------------------------------------------------------------------
#
# Quantities are spelled; identifiers keep their digits.  Several suites need
# to assert that, and each writing out its own list of identifier shapes would
# be four copies of one rule, drifting apart.  The rule lives here once.

IDENTIFIER_SHAPES: tuple[str, ...] = (
    # Most specific first, so a shorter shape cannot eat part of a longer one.
    r"\b[0-9a-fA-F]{4}:[0-9a-fA-F]{2}:[0-9a-fA-F]{2}\.[0-9a-fA-F]\b",
    r"\b([0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}\b",
    r"\b([0-9A-Fa-f]{2}:){7,}[0-9A-Fa-f]{2}\b",
    r"\d{4}-\d{2}-\d{2}([ T]\d{2}:\d{2}(:\d{2})?)?",
    r"\d{2}:\d{2}(:\d{2})?",
    r"(\d{1,3}\.){3}\d{1,3}(:\d{1,5})?(/\d{1,2})?",
    r"\bv?\d+\.\d+(\.\d+)*\b",
    r"\b(eth|en[a-z0-9]*|wl[a-z0-9]*|tty[A-Za-z]*|sd[a-z]|nvme|dahdi|span|zap)\d+\b",
    r"(/[A-Za-z0-9._-]*\d[A-Za-z0-9._-]*)+",
    r"\b(SIP|HTTP|status|code|error)\s+\d{3}\b",
    r"\bport(\s+number)?\s+\d{1,5}\b",
    r"\b(errno|error\s+number)\s+\d+\b",
    r"\(\s*'[^']*'\s*,\s*\d{1,5}\s*\)",
    r"\b(TDM|TE|AEX|HA|HB|B)\d+[A-Z]?\b",
    r"\bextension\s+\d+\b",
)


def strip_identifiers(text: str) -> str:
    """Remove everything that is legitimately an identifier.

    Whatever digits survive are quantities that escaped, which is the thing
    worth failing a test over.
    """
    import re as _re

    remainder = text
    for shape in IDENTIFIER_SHAPES:
        remainder = _re.sub(shape, " ", remainder, flags=_re.IGNORECASE)
    return remainder


def quantities_left_as_digits(text: str) -> list[str]:
    """The lines of ``text`` in which a quantity survived as digits."""
    import re as _re

    return [
        line
        for line in text.splitlines()
        if _re.search(r"\d", strip_identifiers(line))
    ]
