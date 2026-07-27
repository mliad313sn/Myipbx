"""System operations and system status.

The product requirement is that every operation can be performed from the
graphical interface — including operations that need privilege, such as
restarting the telephony engine, applying a static network configuration,
rebuilding the interface card drivers, or restarting the machine.

The control plane runs unprivileged and must stay that way, so it never
performs those operations itself. It asks a helper to, and the helper accepts
only a fixed vocabulary of verbs with pattern validated arguments. There is no
shell anywhere on the path: the argument vector is passed directly to the
process, so a value taken from an interface request cannot become a command.

The request reaches the helper over a Unix domain socket rather than by way of
a setuid binary, and the reason is worth stating because the alternative looks
simpler and does not work. The control plane's service unit sets
``NoNewPrivileges``, which sets the kernel's ``no_new_privs`` flag and
permanently disables the setuid mechanism for that process and everything it
spawns. Under that flag ``sudo`` refuses to run at all. A control plane that
escalated through ``sudo`` would therefore fail every privileged operation on a
real installation, and would fail at the moment an operator asked for one
rather than at start up where somebody would notice. Asking a daemon that
already holds privilege is what lets the control plane keep the hardening and
still do its job.

Everything an operator might otherwise reach for a terminal to do is either a
verb here or a reader below. If something is missing from this file, it is
missing from the graphical interface, and that is the test to apply when
judging whether the product's central claim still holds.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import socket
import struct
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping, Sequence

from . import addresses, numerals
from .logging_setup import get_logger

__all__ = [
    "PrivilegedOperations",
    "OperationOutcome",
    "OperationRefused",
    "SystemStatus",
    "OPERATIONS",
    "MANAGED_SERVICES",
]

_LOG = get_logger("sysops")

#: The framing the control plane and the privileged daemon agree on: an
#: unsigned length, then that many bytes of a JSON object.
_LENGTH_PREFIX = struct.Struct("!I")

#: A reply carries the tail of a command's output and nothing larger.  The cap
#: is here so that a helper which somehow produced an enormous answer cannot
#: make the control plane allocate without bound.
_MAXIMUM_REPLY_BYTES = 1024 * 1024

#: How many times a connection to the privileged daemon is attempted, and the
#: first pause between attempts.  Only the connection is retried; see _connect
#: for why nothing after it may be.
_CONNECT_ATTEMPTS = 4
_RETRY_INITIAL_SECONDS = 0.05

#: The services the appliance is permitted to control.  Anything outside this
#: set is refused, so a request cannot reach an unrelated system service.
MANAGED_SERVICES = ("asterisk", "myipbx", "dahdi")

_SERVICE_PATTERN = re.compile(r"^[a-z][a-z0-9-]{1,31}$")
_INTERFACE_PATTERN = re.compile(r"^[a-zA-Z][a-zA-Z0-9._-]{0,15}$")
_ADDRESS_PATTERN = re.compile(r"^(\d{1,3}\.){3}\d{1,3}$")
_PREFIX_PATTERN = re.compile(r"^([1-9]|[12]\d|3[0-2])$")
_HOSTNAME_PATTERN = re.compile(r"^[a-zA-Z0-9]([a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?$")
_TIMEZONE_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_+-]*(/[A-Za-z0-9_+-]+){0,2}$")


class OperationRefused(ValueError):
    """The requested operation, or one of its arguments, was not permitted."""


@dataclass(frozen=True)
class Operation:
    """One permitted privileged verb."""

    verb: str
    description: str
    parameters: tuple[str, ...] = ()
    #: An operation that interrupts service is confirmed in the interface
    #: before it is sent, and is labelled as such wherever it is offered.
    disruptive: bool = False
    timeout_seconds: float = 120.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "verb": self.verb,
            "description": self.description,
            "parameters": list(self.parameters),
            "disruptive": self.disruptive,
        }


#: The complete vocabulary.  A verb absent from here cannot be invoked.
OPERATIONS: dict[str, Operation] = {
    operation.verb: operation
    for operation in (
        Operation("service-status", "report whether a service is running", ("service",)),
        Operation("service-start", "start a service", ("service",), disruptive=False),
        Operation("service-stop", "stop a service", ("service",), disruptive=True),
        Operation("service-restart", "restart a service", ("service",), disruptive=True),
        Operation("engine-reload", "ask the telephony engine to reload its configuration"),
        Operation(
            "network-apply",
            "write and apply a static network configuration",
            ("interface", "address", "prefix", "gateway"),
            disruptive=True,
            timeout_seconds=180.0,
        ),
        Operation(
            "driver-rebuild",
            "recompile the interface card drivers against the running kernel",
            disruptive=True,
            timeout_seconds=1800.0,
        ),
        Operation(
            "span-generate",
            "regenerate and apply the interface card span configuration",
            disruptive=True,
            timeout_seconds=300.0,
        ),
        Operation(
            "firewall-apply",
            "load the firewall ruleset the appliance generated",
            disruptive=False,
            timeout_seconds=60.0,
        ),
        Operation(
            "certificate-apply",
            "install the certificate staged from the console and restart the console",
            # Disruptive, and labelled so wherever it is offered. The console
            # reads its certificate when it starts, so installing one means
            # restarting it, and every session on this appliance ends including
            # the one that asked. No call in progress is affected.
            disruptive=True,
            timeout_seconds=120.0,
        ),
        Operation("firewall-status", "report whether the appliance ruleset is loaded"),
        Operation(
            "firewall-clear",
            "unload the appliance ruleset, leaving the machine unfiltered",
            disruptive=True,
        ),
        Operation("hostname-set", "set the machine's host name", ("hostname",)),
        Operation("timezone-set", "set the machine's time zone", ("timezone",)),
        Operation("time-synchronise", "synchronise the clock with the configured source"),
        Operation("reboot", "restart the machine", disruptive=True),
        Operation("power-off", "shut the machine down", disruptive=True),
    )
}

#: Per parameter validation.  Every argument is checked before it leaves the
#: control plane, so the helper is defended even if it is invoked by something
#: else later.
_VALIDATORS: dict[str, Callable[[str], bool]] = {
    "service": lambda value: bool(_SERVICE_PATTERN.match(value)) and value in MANAGED_SERVICES,
    "interface": lambda value: bool(_INTERFACE_PATTERN.match(value)),
    "address": lambda value: _is_address(value),
    "prefix": lambda value: bool(_PREFIX_PATTERN.match(value)),
    "gateway": lambda value: value == "" or _is_address(value),
    "hostname": lambda value: bool(_HOSTNAME_PATTERN.match(value)),
    "timezone": lambda value: bool(_TIMEZONE_PATTERN.match(value)),
}


def _is_address(value: str) -> bool:
    """One reading of an address, shared with the firewall and the schema.

    This used to read each group with the interpreter's own integer
    conversion, under which ``010`` is ten. The C library that every other
    program on the machine uses reads ``010`` as octal eight, so a rule this
    module accepted named one host and the kernel loaded another.
    """
    return addresses.is_address(value)


@dataclass
class OperationOutcome:
    """What a privileged operation did."""

    verb: str
    succeeded: bool
    exit_status: int
    output: str
    detail: str = ""
    duration_seconds: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "verb": self.verb,
            "succeeded": self.succeeded,
            "output": numerals.sanitize(self.output.strip())[-4000:],
            "detail": self.detail,
            "duration": numerals.spell_duration(int(self.duration_seconds)),
        }


Runner = Callable[[Sequence[str], float], Awaitable[tuple[int, str]]]


class PrivilegedOperations:
    """Invokes the privileged helper, and nothing else."""

    def __init__(
        self,
        helper_path: str | Path = "/opt/myipbx/bin/myipbx-privileged-helper.sh",
        socket_path: str | Path = "/run/myipbx/helper.sock",
        runner: Runner | None = None,
    ) -> None:
        self.helper_path = Path(helper_path)
        self.socket_path = Path(socket_path)
        self._runner = runner or self._default_runner

    # -- availability ------------------------------------------------------

    def available(self) -> bool:
        """Report whether the privileged daemon is listening."""
        try:
            return self.socket_path.is_socket()
        except OSError:
            return False

    def describe(self) -> dict[str, Any]:
        return {
            "available": self.available(),
            "helper_path": str(self.helper_path),
            "socket_path": str(self.socket_path),
            "operations": [operation.as_dict() for operation in OPERATIONS.values()],
            "managed_services": list(MANAGED_SERVICES),
            "explanation": (
                "the control plane runs unprivileged and performs no system "
                "operation itself; it asks a helper that accepts only this fixed "
                "vocabulary, with every argument validated before it is sent, "
                "over a socket the helper opens only to this appliance's own "
                "account"
            ),
        }

    # -- invocation --------------------------------------------------------

    def validate(self, verb: str, arguments: Mapping[str, Any] | None = None) -> list[str]:
        """Validate a request and return the argument vector to send."""
        return self.validate_arguments(verb, arguments)

    @staticmethod
    def validate_arguments(
        verb: str, arguments: Mapping[str, Any] | None = None
    ) -> list[str]:
        """Validate a request and return the argument vector to send.

        Raises rather than returning a partial vector, so a caller cannot act
        on a request that was only partly acceptable.

        This is a static method because the privileged daemon calls it too.
        Both sides checking the same way is deliberate: the control plane's
        check protects the operator from mistakes, and the daemon's check
        protects the machine from the control plane.
        """
        operation = OPERATIONS.get(verb)
        if operation is None:
            raise OperationRefused(f"the operation named {verb} is not permitted")

        supplied = dict(arguments or {})
        vector: list[str] = [verb]

        for parameter in operation.parameters:
            if parameter not in supplied:
                raise OperationRefused(
                    f"the operation named {verb} requires a value for {parameter}"
                )
            value = supplied.pop(parameter)
            text = "" if value is None else str(value)

            validator = _VALIDATORS.get(parameter)
            if validator is None or not validator(text):
                raise OperationRefused(
                    f"the value supplied for {parameter} is not acceptable"
                )
            vector.append(text)

        if supplied:
            # An unexpected argument means the caller and this table disagree,
            # which is a defect rather than something to quietly ignore.
            raise OperationRefused(
                "the request carried arguments the operation does not accept: "
                + ", ".join(sorted(supplied))
            )
        return vector

    async def run(
        self, verb: str, arguments: Mapping[str, Any] | None = None
    ) -> OperationOutcome:
        """Validate and perform a privileged operation."""
        vector = self.validate(verb, arguments)
        operation = OPERATIONS[verb]

        if not self.available():
            return OperationOutcome(
                verb=verb,
                succeeded=False,
                exit_status=-1,
                output="",
                detail=(
                    "the privileged helper is not running on this machine, so "
                    "system operations cannot be performed from the interface"
                ),
            )

        _LOG.info("requesting the privileged operation named %s", verb)
        started = time.monotonic()
        try:
            status, output = await self._runner(vector, operation.timeout_seconds)
        except asyncio.TimeoutError:
            return OperationOutcome(
                verb=verb,
                succeeded=False,
                exit_status=-1,
                output="",
                detail="the operation exceeded its timeout and was abandoned",
                duration_seconds=time.monotonic() - started,
            )
        except OSError as error:
            return OperationOutcome(
                verb=verb,
                succeeded=False,
                exit_status=-1,
                output="",
                detail=f"the helper could not be invoked: {error}",
                duration_seconds=time.monotonic() - started,
            )

        duration = time.monotonic() - started
        succeeded = status == 0
        if not succeeded:
            _LOG.warning(
                "the privileged operation named %s failed with status %d", verb, status
            )
        return OperationOutcome(
            verb=verb,
            succeeded=succeeded,
            exit_status=status,
            output=output,
            detail="" if succeeded else "the helper reported a failure",
            duration_seconds=duration,
        )

    async def _connect(self) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        """Open the connection, retrying only while it is safe to retry.

        A burst of dashboards acting at once can fill the daemon's accept queue
        and have the kernel refuse the rest.  That is transient by nature and
        the obvious answer is to try again — but retrying a privileged
        operation is only safe while it is certain the request has not been
        sent, because the verbs include restarting the engine and restarting
        the machine, and doing either of those twice is not a small matter.

        Connecting is the one moment where that certainty exists, so the retry
        lives here and nowhere else.  A failure after this point is reported,
        never repeated.
        """
        delay = _RETRY_INITIAL_SECONDS
        last: OSError | None = None

        for _ in range(_CONNECT_ATTEMPTS):
            try:
                return await asyncio.open_unix_connection(str(self.socket_path))
            except (ConnectionError, BlockingIOError, TimeoutError) as error:
                # The daemon is there and momentarily full.  Nothing was sent.
                last = error
                await asyncio.sleep(delay)
                delay *= 2
            except OSError:
                # The socket is absent or unusable, which more waiting will not
                # mend, so it is reported at once.
                raise

        raise last if last is not None else ConnectionError(
            "the privileged helper could not be reached"
        )

    async def _default_runner(
        self, vector: Sequence[str], timeout_seconds: float
    ) -> tuple[int, str]:
        """Ask the privileged daemon to perform the already validated vector.

        The whole exchange is one request and one reply on one connection.  A
        connection that carries a single operation and is then closed cannot
        leave a half read request behind for the next one to be confused by,
        which is worth more here than the cost of connecting each time.
        """
        reader, writer = await self._connect()
        try:
            request = json.dumps(
                {"verb": vector[0], "arguments": list(vector[1:])}
            ).encode("utf-8")
            writer.write(_LENGTH_PREFIX.pack(len(request)) + request)
            await writer.drain()

            header = await asyncio.wait_for(
                reader.readexactly(_LENGTH_PREFIX.size), timeout=timeout_seconds
            )
            (length,) = _LENGTH_PREFIX.unpack(header)
            if length > _MAXIMUM_REPLY_BYTES:
                return -1, "the privileged helper replied with more than it should have"
            body = await asyncio.wait_for(
                reader.readexactly(length), timeout=timeout_seconds
            )
        except asyncio.IncompleteReadError:
            return -1, "the privileged helper closed before it answered"
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass

        try:
            reply = json.loads(body.decode("utf-8"))
            return int(reply["status"]), str(reply.get("output", "")) or str(
                reply.get("detail", "")
            )
        except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError, ValueError):
            return -1, "the privileged helper's answer could not be read"


class SystemStatus:
    """Unprivileged readings that describe the machine.

    Everything here is read from the kernel's own exported files, so it needs
    no privilege and cannot disturb anything. The readings are what an operator
    would otherwise open a terminal to see, which is why they belong on the
    dashboard.
    """

    def __init__(self, root: str | Path = "/") -> None:
        self.root = Path(root)

    # -- individual readings ----------------------------------------------

    def uptime_seconds(self) -> int | None:
        try:
            content = (self.root / "proc/uptime").read_text(encoding="utf-8")
            return int(float(content.split()[0]))
        except (OSError, ValueError, IndexError):
            return None

    def load_average(self) -> tuple[float, float, float] | None:
        try:
            parts = (self.root / "proc/loadavg").read_text(encoding="utf-8").split()
            return float(parts[0]), float(parts[1]), float(parts[2])
        except (OSError, ValueError, IndexError):
            return None

    def memory(self) -> dict[str, int] | None:
        """Total and available memory in kibibytes."""
        try:
            content = (self.root / "proc/meminfo").read_text(encoding="utf-8")
        except OSError:
            return None

        readings: dict[str, int] = {}
        for line in content.splitlines():
            name, separator, value = line.partition(":")
            if not separator:
                continue
            if name in ("MemTotal", "MemAvailable", "MemFree", "SwapTotal", "SwapFree"):
                try:
                    readings[name] = int(value.strip().split()[0])
                except (ValueError, IndexError):
                    continue
        return readings or None

    def disk(self, path: str | Path = "/") -> dict[str, int] | None:
        try:
            statistics = os.statvfs(path)
        except OSError:
            return None
        block = statistics.f_frsize
        return {
            "total_bytes": statistics.f_blocks * block,
            "free_bytes": statistics.f_bavail * block,
            "used_bytes": (statistics.f_blocks - statistics.f_bfree) * block,
        }

    def kernel_release(self) -> str:
        try:
            return os.uname().release
        except (OSError, AttributeError):
            return "unknown"

    def hostname(self) -> str:
        try:
            return socket.gethostname()
        except OSError:
            return "unknown"

    def timezone(self) -> str:
        candidate = self.root / "etc/timezone"
        try:
            if candidate.is_file():
                return candidate.read_text(encoding="utf-8").strip() or "unknown"
        except OSError:
            pass
        link = self.root / "etc/localtime"
        try:
            if link.is_symlink():
                target = str(link.readlink())
                marker = "zoneinfo/"
                if marker in target:
                    return target.split(marker, 1)[1]
        except OSError:
            pass
        return time.tzname[0] if time.tzname else "unknown"

    def network_interfaces(self) -> list[dict[str, Any]]:
        """Enumerate interfaces and their operational state.

        Addresses are deliberately not read here — the appliance's addressing
        is whatever the operator wrote down, and is reported from the
        configuration rather than discovered, in keeping with the exclusion.
        """
        directory = self.root / "sys/class/net"
        if not directory.is_dir():
            return []

        interfaces: list[dict[str, Any]] = []
        try:
            entries = sorted(directory.iterdir())
        except OSError:
            return []

        for entry in entries:
            if entry.name == "lo":
                continue
            interfaces.append(
                {
                    "name": entry.name,
                    "state": _read_text(entry / "operstate") or "unknown",
                    "carrier": _read_text(entry / "carrier") == "1",
                    "address": _read_text(entry / "address") or "",
                    "speed": _read_text(entry / "speed") or "",
                }
            )
        return interfaces

    # -- combined ----------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        """The complete machine description, with every figure spelled."""
        uptime = self.uptime_seconds()
        load = self.load_average()
        memory = self.memory()
        disk = self.disk(self.root)

        payload: dict[str, Any] = {
            "hostname": self.hostname(),
            "kernel_release": numerals.sanitize(self.kernel_release()),
            "timezone": self.timezone(),
            "interfaces": self.network_interfaces(),
            "uptime": numerals.spell_duration(uptime) if uptime is not None else "unknown",
        }

        if load is not None:
            payload["load_average"] = {
                "one_minute": numerals.spell_decimal(load[0]),
                "five_minutes": numerals.spell_decimal(load[1]),
                "fifteen_minutes": numerals.spell_decimal(load[2]),
            }

        if memory:
            total = memory.get("MemTotal", 0)
            available = memory.get("MemAvailable", memory.get("MemFree", 0))
            payload["memory"] = {
                "total": _spell_bytes(total * 1024),
                "available": _spell_bytes(available * 1024),
                "used_portion": _spell_portion(total - available, total),
            }

        if disk:
            payload["disk"] = {
                "total": _spell_bytes(disk["total_bytes"]),
                "free": _spell_bytes(disk["free_bytes"]),
                "used_portion": _spell_portion(disk["used_bytes"], disk["total_bytes"]),
            }

        return payload


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


_UNITS = (
    (1024 ** 4, "tebibytes", "tebibyte"),
    (1024 ** 3, "gibibytes", "gibibyte"),
    (1024 ** 2, "mebibytes", "mebibyte"),
    (1024, "kibibytes", "kibibyte"),
)


def _spell_bytes(value: int) -> str:
    """Render a byte count as words in the largest sensible unit."""
    if value <= 0:
        return "zero bytes"
    for size, plural, singular in _UNITS:
        if value >= size:
            amount = value / size
            name = singular if abs(amount - 1.0) < 1e-9 else plural
            return f"{numerals.spell_decimal(amount, 1)} {name}"
    return f"{numerals.spell_integer(value)} bytes"


def _spell_portion(part: int, whole: int) -> str:
    """Render a proportion as spelled percent."""
    if whole <= 0:
        return "an unknown portion"
    portion = max(0.0, min(100.0, (part / whole) * 100.0))
    return f"{numerals.spell_decimal(portion, 1)} percent"
