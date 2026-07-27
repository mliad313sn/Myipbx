"""The privileged helper daemon.

This is the only part of the appliance that runs as root, and it exists so that
nothing else has to.

The control plane runs as an unprivileged account under a service unit that
sets ``NoNewPrivileges``.  That directive sets the kernel's ``no_new_privs``
flag, which permanently disables the setuid mechanism for the process and
everything it spawns.  A control plane that tried to gain privilege by calling
``sudo`` would therefore fail on every single operation, and would fail at the
moment an operator asked for one rather than at start up where it would be
noticed.  Reaching privilege through a socket instead of through a setuid
binary is what allows the control plane to keep that hardening and still do its
job.

The daemon accepts a request only from the appliance's own service account,
proved by asking the kernel who is on the other end of the socket rather than
by believing anything the request says.  It then accepts only a verb from the
same fixed vocabulary the control plane knows, with every argument checked
against the same patterns, and passes the argument vector directly to the
helper script as an argument list.  There is no shell anywhere on that path, so
a value that came from a browser cannot become a command.

The validation here is deliberately a repetition of the validation the control
plane already performed.  The control plane's copy protects the operator from
mistakes; this copy protects the machine from the control plane.
"""

from __future__ import annotations

import json
import os
import pwd
import signal
import socket
import socketserver
import struct
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

from .logging_setup import get_logger
from .sysops import OPERATIONS, OperationRefused, PrivilegedOperations

__all__ = [
    "HelperDaemon",
    "DEFAULT_SOCKET_PATH",
    "DEFAULT_HELPER_PATH",
    "DEFAULT_ACCOUNT",
    "read_frame",
    "write_frame",
]

_LOG = get_logger("helperd")

DEFAULT_SOCKET_PATH = "/run/myipbx/helper.sock"
DEFAULT_HELPER_PATH = "/opt/myipbx/bin/myipbx-privileged-helper.sh"
DEFAULT_ACCOUNT = "myipbx"

#: A request is a small JSON object.  The cap exists so that a caller who has
#: reached the socket still cannot make the daemon allocate without bound.
MAXIMUM_FRAME_BYTES = 64 * 1024

#: How long the daemon waits for a caller to finish sending before giving up.
#: A caller that has connected and then gone quiet must not hold a thread.
REQUEST_TIMEOUT_SECONDS = 30.0

_LENGTH_PREFIX = struct.Struct("!I")
_PEER_CREDENTIALS = struct.Struct("3i")

#: Statuses the daemon itself reports, as distinct from a status the helper
#: script returned.  They are negative so that no helper exit status can be
#: mistaken for one of them.
STATUS_REFUSED = -1
STATUS_FAULT = -2


def read_frame(connection: socket.socket) -> bytes:
    """Read one length prefixed frame, or raise if the peer stops early."""
    header = _read_exactly(connection, _LENGTH_PREFIX.size)
    (length,) = _LENGTH_PREFIX.unpack(header)
    if length > MAXIMUM_FRAME_BYTES:
        raise ValueError("the request was larger than the daemon accepts")
    return _read_exactly(connection, length)


def write_frame(connection: socket.socket, payload: bytes) -> None:
    """Write one length prefixed frame."""
    connection.sendall(_LENGTH_PREFIX.pack(len(payload)) + payload)


def _read_exactly(connection: socket.socket, count: int) -> bytes:
    chunks: list[bytes] = []
    remaining = count
    while remaining > 0:
        chunk = connection.recv(remaining)
        if not chunk:
            raise ConnectionError("the peer closed before the frame was complete")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def peer_identity(connection: socket.socket) -> tuple[int, int, int]:
    """Ask the kernel which process, user and group is on the other end.

    The answer comes from the kernel rather than from the request, so a caller
    cannot claim to be somebody else.
    """
    raw = connection.getsockopt(
        socket.SOL_SOCKET, socket.SO_PEERCRED, _PEER_CREDENTIALS.size
    )
    process, user, group = _PEER_CREDENTIALS.unpack(raw)
    return process, user, group


class _RequestHandler(socketserver.BaseRequestHandler):
    """Serves one request on one connection, then closes it."""

    server: "_Server"

    def handle(self) -> None:
        connection: socket.socket = self.request
        connection.settimeout(REQUEST_TIMEOUT_SECONDS)

        try:
            _, user, _ = peer_identity(connection)
        except OSError as error:
            _LOG.warning("the identity of a caller could not be established: %s", error)
            return

        if user not in self.server.permitted_users:
            # Nothing about the request is read.  A caller who is not permitted
            # is told so and disconnected before anything it sent is parsed, so
            # an unauthorised caller cannot reach the parser at all.
            _LOG.warning(
                "a request from the user numbered %s was refused; that account "
                "is not permitted to ask for privileged operations",
                user,
            )
            self._reply(
                connection,
                STATUS_REFUSED,
                "",
                "this account may not ask for privileged operations",
            )
            return

        try:
            raw = read_frame(connection)
        except (OSError, ValueError, ConnectionError) as error:
            _LOG.warning("a request could not be read: %s", error)
            return

        try:
            request = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._reply(connection, STATUS_REFUSED, "", "the request was not readable")
            return

        if not isinstance(request, dict):
            self._reply(connection, STATUS_REFUSED, "", "the request was not an object")
            return

        verb = request.get("verb")
        arguments = request.get("arguments", [])
        if not isinstance(verb, str) or not isinstance(arguments, list):
            self._reply(connection, STATUS_REFUSED, "", "the request was malformed")
            return

        try:
            vector = self.server.vocabulary.validate_vector([verb, *(str(a) for a in arguments)])
        except OperationRefused as refusal:
            _LOG.warning("a request was refused: %s", refusal)
            self._reply(connection, STATUS_REFUSED, "", str(refusal))
            return

        status, output = self.server.perform(vector)
        self._reply(connection, status, output, "" if status == 0 else "the helper reported a failure")

    def _reply(self, connection: socket.socket, status: int, output: str, detail: str) -> None:
        payload = json.dumps(
            {"status": status, "output": output, "detail": detail}
        ).encode("utf-8")
        try:
            write_frame(connection, payload)
        except OSError:
            # The caller gave up while the operation was running.  The operation
            # itself already happened, so there is nothing to undo and nothing
            # useful to say.
            pass


class _Server(socketserver.ThreadingUnixStreamServer):
    """A thread per connection, because operations are long and few.

    Rebuilding the interface card drivers takes minutes.  Serving that on a
    thread keeps a second operator's request for something quick from waiting
    behind it, and the vocabulary is small enough that the number of threads
    cannot get out of hand.
    """

    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 16

    def __init__(self, path: str, daemon: "HelperDaemon") -> None:
        self.daemon_reference = daemon
        self.permitted_users = daemon.permitted_users
        self.vocabulary = daemon
        super().__init__(path, _RequestHandler)

    def perform(self, vector: list[str]) -> tuple[int, str]:
        return self.daemon_reference.perform(vector)


class HelperDaemon:
    """Listens on a Unix socket and performs the fixed vocabulary of verbs."""

    def __init__(
        self,
        socket_path: str | Path = DEFAULT_SOCKET_PATH,
        helper_path: str | Path = DEFAULT_HELPER_PATH,
        account: str = DEFAULT_ACCOUNT,
        permitted_users: tuple[int, ...] | None = None,
    ) -> None:
        self.socket_path = Path(socket_path)
        self.helper_path = Path(helper_path)
        self.account = account
        self.permitted_users = (
            tuple(permitted_users)
            if permitted_users is not None
            else self._resolve_permitted_users(account)
        )
        self._server: _Server | None = None
        self._thread: threading.Thread | None = None

    # -- vocabulary --------------------------------------------------------

    @staticmethod
    def validate_vector(vector: list[str]) -> list[str]:
        """Check a whole argument vector against the shared vocabulary.

        The vector arrives already flattened, because that is the form the
        helper script is invoked with and checking the thing that will actually
        be run is worth more than checking a friendlier representation of it.
        """
        if not vector:
            raise OperationRefused("the request named no operation")

        verb, *values = vector
        operation = OPERATIONS.get(verb)
        if operation is None:
            raise OperationRefused(f"the operation named {verb} is not permitted")
        if len(values) != len(operation.parameters):
            raise OperationRefused(
                f"the operation named {verb} was given the wrong number of values"
            )

        # Reusing the control plane's own validation is deliberate.  Were this
        # a second, separately written copy it could drift, and a drift between
        # the two would open exactly the gap this daemon exists to close.
        arguments = dict(zip(operation.parameters, values))
        return PrivilegedOperations.validate_arguments(verb, arguments)

    # -- performing --------------------------------------------------------

    def perform(self, vector: list[str]) -> tuple[int, str]:
        """Run the helper script with the validated vector and nothing else."""
        verb = vector[0]
        operation = OPERATIONS[verb]

        if not (self.helper_path.is_file() and os.access(self.helper_path, os.X_OK)):
            return (
                STATUS_FAULT,
                "the privileged helper script is not installed on this machine",
            )

        _LOG.info("performing the privileged operation named %s", verb)
        try:
            completed = subprocess.run(  # noqa: S603 - an argument list, never a shell
                [str(self.helper_path), *vector],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=operation.timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired:
            _LOG.warning("the operation named %s exceeded its time and was abandoned", verb)
            return STATUS_FAULT, "the operation exceeded its time and was abandoned"
        except OSError as error:
            _LOG.error("the helper script could not be invoked: %s", error)
            return STATUS_FAULT, f"the helper script could not be invoked: {error}"

        return completed.returncode, completed.stdout.decode("utf-8", "replace")

    # -- lifetime ----------------------------------------------------------

    @staticmethod
    def _resolve_permitted_users(account: str) -> tuple[int, ...]:
        """The service account, and root because root already has privilege.

        Admitting root grants nothing that root could not do directly, and it
        is what lets an administrator at the console exercise the same path the
        control plane uses rather than a different one.
        """
        try:
            return (pwd.getpwnam(account).pw_uid, 0)
        except KeyError:
            _LOG.warning(
                "the account named %s does not exist on this machine, so only "
                "the administrator may ask for privileged operations",
                account,
            )
            return (0,)

    def _prepare_socket_directory(self) -> None:
        directory = self.socket_path.parent
        directory.mkdir(parents=True, exist_ok=True)

        # The directory and the socket are readable only by the daemon and the
        # service account, so nothing else on the machine can even see the
        # socket to attempt a connection.
        try:
            group = pwd.getpwnam(self.account).pw_gid
        except KeyError:
            group = -1

        try:
            os.chmod(directory, 0o750)
            if group != -1:
                os.chown(directory, 0, group)
        except OSError as error:
            _LOG.warning("the socket directory could not be secured: %s", error)

        if self.socket_path.exists():
            # A socket left behind by a daemon that was killed would otherwise
            # prevent this one from binding.
            self.socket_path.unlink()

    def _secure_socket(self) -> None:
        try:
            group = pwd.getpwnam(self.account).pw_gid
        except KeyError:
            group = -1
        try:
            if group != -1:
                os.chown(self.socket_path, 0, group)
            os.chmod(self.socket_path, 0o660)
        except OSError as error:
            _LOG.warning("the socket could not be secured: %s", error)

    def start(self) -> None:
        """Bind the socket and begin serving on a background thread."""
        self._prepare_socket_directory()

        # The mode is set with the umask as well as afterwards, so that the
        # socket is never even briefly reachable by anything else.
        previous_umask = os.umask(0o117)
        try:
            self._server = _Server(str(self.socket_path), self)
        finally:
            os.umask(previous_umask)

        self._secure_socket()

        self._thread = threading.Thread(
            target=self._server.serve_forever, name="helper-daemon", daemon=True
        )
        self._thread.start()
        _LOG.info("the privileged helper is listening at %s", self.socket_path)

    def stop(self) -> None:
        """Stop serving and remove the socket."""
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            self._thread = None
        try:
            self.socket_path.unlink()
        except OSError:
            pass

    def serve_until_signalled(self) -> None:
        """Run until the service manager asks the daemon to stop."""
        stopping = threading.Event()

        def _request_stop(_signal: int, _frame: Any) -> None:
            stopping.set()

        signal.signal(signal.SIGTERM, _request_stop)
        signal.signal(signal.SIGINT, _request_stop)

        self.start()
        try:
            stopping.wait()
        finally:
            self.stop()
            _LOG.info("the privileged helper has stopped")


def main(argv: list[str] | None = None) -> int:
    """Entry point for the daemon's service unit."""
    arguments = list(sys.argv[1:] if argv is None else argv)

    socket_path = os.environ.get("MYIPBX_HELPER_SOCKET", DEFAULT_SOCKET_PATH)
    helper_path = os.environ.get("MYIPBX_HELPER_SCRIPT", DEFAULT_HELPER_PATH)
    account = os.environ.get("MYIPBX_SERVICE_ACCOUNT", DEFAULT_ACCOUNT)

    if arguments:
        _LOG.warning("the daemon takes no arguments; it is configured by its environment")

    if os.geteuid() != 0:
        _LOG.error(
            "the privileged helper must be started by the administrator, because "
            "its whole purpose is to hold the privilege the control plane does not"
        )
        return 1

    HelperDaemon(socket_path, helper_path, account).serve_until_signalled()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
