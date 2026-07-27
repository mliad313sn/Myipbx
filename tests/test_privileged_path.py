"""The privileged path, exercised for real rather than mocked.

Every other test of system operations replaces the thing that actually performs
them.  That is why the product could pass its whole suite while being unable to
perform a single privileged operation on a real machine: the control plane
escalated with ``sudo`` under a service unit that sets ``NoNewPrivileges``,
which disables the setuid mechanism outright, and no test ever ran the real
path to find out.

These tests create a real daemon on a real Unix socket, connect to it with the
real client, and run a real script.  Nothing on the path between the control
plane's ``run`` and the helper being executed is replaced.
"""

from __future__ import annotations

import ast
import asyncio
import json
import os
import socket
import struct
import tempfile
import threading
import time
import unittest
from pathlib import Path

from appliance.helperd import (
    STATUS_REFUSED,
    HelperDaemon,
    read_frame,
    write_frame,
)
from appliance.sysops import OPERATIONS, PrivilegedOperations

_LENGTH_PREFIX = struct.Struct("!I")


class _RealHelperFixture(unittest.TestCase):
    """A daemon, a socket and a stand in helper script that all really exist."""

    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        root = Path(self._directory.name)
        self.addCleanup(self._directory.cleanup)

        self.socket_path = root / "helper.sock"
        self.helper_path = root / "helper.sh"

        # A stand in for the real helper script.  It reports the vector it was
        # given so that a test can prove what actually reached the process, and
        # it exits non zero for one particular verb so that the failure path is
        # exercised as well as the success path.
        self.helper_path.write_text(
            "#!/bin/sh\n"
            'printf "verb=%s args=%s\\n" "$1" "$*"\n'
            'if [ "$1" = "firewall-clear" ]; then exit 3; fi\n'
            'if [ "$1" = "driver-rebuild" ]; then sleep 2; fi\n'
            "exit 0\n",
            encoding="utf-8",
        )
        self.helper_path.chmod(0o755)

        self.daemon = HelperDaemon(
            socket_path=self.socket_path,
            helper_path=self.helper_path,
            account="myipbx",
            # The test runs as whoever runs the suite, so that identity is what
            # the daemon must accept.  The refusal path is proved separately by
            # constructing a daemon that does not admit this account.
            permitted_users=(os.getuid(),),
        )
        self.daemon.start()
        self.addCleanup(self.daemon.stop)

        self.operations = PrivilegedOperations(
            helper_path=self.helper_path, socket_path=self.socket_path
        )

    def run_operation(self, verb: str, arguments: dict[str, str] | None = None):
        return asyncio.run(self.operations.run(verb, arguments))


class RealSocketOperationTests(_RealHelperFixture):
    """The whole path, end to end, with nothing replaced."""

    def test_the_daemon_is_reported_available_once_it_is_listening(self) -> None:
        self.assertTrue(self.operations.available())

    def test_an_operation_reaches_the_helper_and_reports_success(self) -> None:
        outcome = self.run_operation("service-status", {"service": "asterisk"})

        self.assertTrue(outcome.succeeded, outcome.output)
        self.assertEqual(outcome.exit_status, 0)
        self.assertIn("verb=service-status", outcome.output)
        self.assertIn("asterisk", outcome.output)

    def test_the_helper_receives_every_argument_in_order(self) -> None:
        outcome = self.run_operation(
            "network-apply",
            {
                "interface": "eth0",
                "address": "192.168.100.10",
                "prefix": "24",
                "gateway": "192.168.100.1",
            },
        )

        self.assertTrue(outcome.succeeded, outcome.output)
        self.assertIn(
            "args=network-apply eth0 192.168.100.10 24 192.168.100.1", outcome.output
        )

    def test_a_failing_helper_is_reported_as_a_failure(self) -> None:
        outcome = self.run_operation("firewall-clear")

        self.assertFalse(outcome.succeeded)
        self.assertEqual(outcome.exit_status, 3)
        self.assertEqual(outcome.detail, "the helper reported a failure")

    def test_every_verb_in_the_vocabulary_can_actually_be_invoked(self) -> None:
        """The vocabulary and the path agree, verb by verb.

        A verb offered by the interface that the daemon would refuse is a
        promise the product cannot keep, and this is the only test that would
        notice one appearing.
        """
        placeholders = {
            "service": "asterisk",
            "interface": "eth0",
            "address": "192.168.100.10",
            "prefix": "24",
            "gateway": "",
            "hostname": "myipbx",
            "timezone": "UTC",
        }

        for verb, operation in OPERATIONS.items():
            with self.subTest(verb=verb):
                arguments = {name: placeholders[name] for name in operation.parameters}
                outcome = self.run_operation(verb, arguments)
                self.assertNotEqual(
                    outcome.exit_status,
                    STATUS_REFUSED,
                    f"the daemon refused the verb named {verb}, which the "
                    f"interface offers: {outcome.output}",
                )


class RefusalTests(_RealHelperFixture):
    """What the daemon will not do, proved against the daemon itself."""

    def _speak_directly(self, request: dict[str, object]) -> dict[str, object]:
        """Bypass the client, so the daemon's own checks are what is tested."""
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(10.0)
            connection.connect(str(self.socket_path))
            write_frame(connection, json.dumps(request).encode("utf-8"))
            return json.loads(read_frame(connection).decode("utf-8"))

    def test_a_verb_outside_the_vocabulary_is_refused(self) -> None:
        reply = self._speak_directly({"verb": "rm", "arguments": ["-rf", "/"]})

        self.assertEqual(reply["status"], STATUS_REFUSED)
        self.assertIn("not permitted", str(reply["detail"]))

    def test_a_service_outside_the_managed_set_is_refused(self) -> None:
        reply = self._speak_directly(
            {"verb": "service-stop", "arguments": ["systemd-logind"]}
        )

        self.assertEqual(reply["status"], STATUS_REFUSED)

    def test_an_argument_that_is_not_a_plain_value_cannot_become_a_command(self) -> None:
        """Shell metacharacters are refused, and could not act even if they were.

        The vector is passed to the process as an argument list, so there is no
        shell to interpret them.  The pattern check refuses them anyway, so that
        the defence does not rest on a single mechanism.
        """
        for hostile in ("asterisk; reboot", "asterisk && reboot", "$(reboot)", "a|b"):
            with self.subTest(value=hostile):
                reply = self._speak_directly(
                    {"verb": "service-restart", "arguments": [hostile]}
                )
                self.assertEqual(reply["status"], STATUS_REFUSED)

    def test_the_wrong_number_of_values_is_refused(self) -> None:
        reply = self._speak_directly(
            {"verb": "service-status", "arguments": ["asterisk", "extra"]}
        )

        self.assertEqual(reply["status"], STATUS_REFUSED)
        self.assertIn("wrong number", str(reply["detail"]))

    def test_a_malformed_request_is_refused_rather_than_crashing_the_daemon(self) -> None:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(10.0)
            connection.connect(str(self.socket_path))
            write_frame(connection, b"this is not an object")
            reply = json.loads(read_frame(connection).decode("utf-8"))

        self.assertEqual(reply["status"], STATUS_REFUSED)

        # And the daemon is still serving afterwards.
        outcome = self.run_operation("service-status", {"service": "asterisk"})
        self.assertTrue(outcome.succeeded)

    def test_an_oversized_frame_is_refused_rather_than_allocated(self) -> None:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(10.0)
            connection.connect(str(self.socket_path))
            connection.sendall(_LENGTH_PREFIX.pack(64 * 1024 * 1024))
            connection.shutdown(socket.SHUT_WR)
            self.assertEqual(connection.recv(4096), b"")

        outcome = self.run_operation("service-status", {"service": "asterisk"})
        self.assertTrue(outcome.succeeded)


class CallerIdentityTests(unittest.TestCase):
    """A caller the daemon does not admit is refused before it is parsed."""

    def test_a_caller_whose_account_is_not_permitted_is_refused(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)

        helper = root / "helper.sh"
        helper.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        helper.chmod(0o755)

        # The daemon is told to admit an account that this test is definitely
        # not running as, so the identity check is the only thing that can
        # decide the outcome.
        impossible = os.getuid() + 4242
        daemon = HelperDaemon(
            socket_path=root / "helper.sock",
            helper_path=helper,
            account="myipbx",
            permitted_users=(impossible,),
        )
        daemon.start()
        self.addCleanup(daemon.stop)

        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(10.0)
            connection.connect(str(root / "helper.sock"))
            reply = json.loads(read_frame(connection).decode("utf-8"))

        self.assertEqual(reply["status"], STATUS_REFUSED)
        self.assertIn("may not", str(reply["detail"]))

    def test_the_socket_is_not_readable_by_other_accounts(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)

        helper = root / "helper.sh"
        helper.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        helper.chmod(0o755)

        daemon = HelperDaemon(
            socket_path=root / "helper.sock",
            helper_path=helper,
            permitted_users=(os.getuid(),),
        )
        daemon.start()
        self.addCleanup(daemon.stop)

        mode = (root / "helper.sock").stat().st_mode & 0o777
        self.assertEqual(
            mode & 0o007,
            0,
            "an account other than the appliance's own could reach the socket",
        )


class UnavailableDaemonTests(unittest.TestCase):
    """What the control plane says when the daemon is not there."""

    def test_an_absent_socket_is_reported_as_unavailable_not_as_a_crash(self) -> None:
        operations = PrivilegedOperations(
            helper_path="/a/path/that/does/not/exist",
            socket_path="/a/socket/that/does/not/exist",
        )

        self.assertFalse(operations.available())

        outcome = asyncio.run(operations.run("engine-reload"))
        self.assertFalse(outcome.succeeded)
        self.assertIn("not running", outcome.detail)


class BurstTests(_RealHelperFixture):
    """A burst of operators must not have their requests dropped.

    The appliance is specified to carry one hundred dashboards at once, and one
    operator action on each is enough to make them all ask for a privileged
    operation in the same instant. The daemon's accept queue used to hold
    sixteen, so the kernel refused the rest before the daemon saw them and the
    operator was told the connection was lost — true, and nothing they could
    act on.
    """

    def test_a_burst_of_requests_is_answered_rather_than_dropped(self) -> None:
        async def burst() -> list:
            return await asyncio.gather(
                *[self.operations.run("firewall-status") for _ in range(120)],
                return_exceptions=True,
            )

        outcomes = asyncio.run(burst())
        failed = [
            outcome
            for outcome in outcomes
            if isinstance(outcome, BaseException) or not outcome.succeeded
        ]
        self.assertEqual(
            failed,
            [],
            "requests were dropped under a burst: "
            + "; ".join(str(getattr(f, "detail", f)) for f in failed[:3]),
        )

    def test_the_daemon_will_not_allocate_a_thread_for_every_caller(self) -> None:
        """The root process must not scale with the number of connections.

        There was no limit, and the reasoning written beside its absence was
        that the vocabulary is small enough that the number of threads cannot
        get out of hand. That does not follow: the thread count tracks
        connections, not verbs. A control plane that had been taken over --
        which is precisely what this privilege boundary exists to contain --
        could open thousands of connections and make a root process spawn a
        thread and a subprocess for each, on a machine carrying calls.
        """
        import appliance.helperd as helperd

        server = self.daemon._server
        assert server is not None

        in_flight = 0
        highest = 0
        guard = threading.Lock()
        original = helperd.HelperDaemon.perform

        def counting(daemon_self, vector):
            nonlocal in_flight, highest
            with guard:
                in_flight += 1
                highest = max(highest, in_flight)
            try:
                time.sleep(0.15)
                return original(daemon_self, vector)
            finally:
                with guard:
                    in_flight -= 1

        helperd.HelperDaemon.perform = counting
        self.addCleanup(setattr, helperd.HelperDaemon, "perform", original)

        async def burst() -> list:
            return await asyncio.gather(
                *[self.operations.run("firewall-status") for _ in range(60)],
                return_exceptions=True,
            )

        outcomes = asyncio.run(burst())

        self.assertLessEqual(
            highest, helperd.MAXIMUM_CONCURRENT_REQUESTS,
            f"{highest} privileged operations ran at once against a limit of "
            f"{helperd.MAXIMUM_CONCURRENT_REQUESTS}",
        )
        self.assertGreater(
            highest, 1, "nothing ran concurrently, so the limit proved nothing"
        )
        # And every one of them was still served: the limit paces, it does not
        # refuse work an operator legitimately asked for.
        refused = [
            outcome for outcome in outcomes
            if isinstance(outcome, BaseException) or not outcome.succeeded
        ]
        self.assertEqual(
            refused, [],
            "the limit turned away requests instead of pacing them: "
            + "; ".join(str(getattr(item, "detail", item)) for item in refused[:3]),
        )

    def test_a_caller_turned_away_is_told_why(self) -> None:
        """A connection closed without a word reaches the operator as "the
        connection was lost", which is true and useless."""
        import appliance.helperd as helperd

        server = self.daemon._server
        assert server is not None

        # Hold every slot, so the next caller cannot have one.
        held = [server._slots.acquire(timeout=1.0)
                for _ in range(helperd.MAXIMUM_CONCURRENT_REQUESTS)]
        self.assertTrue(all(held), "the slots could not be taken for the test")
        original_wait = helperd.SLOT_WAIT_SECONDS
        helperd.SLOT_WAIT_SECONDS = 0.2
        try:
            outcome = self.run_operation("firewall-status")
        finally:
            helperd.SLOT_WAIT_SECONDS = original_wait
            for _ in held:
                server._slots.release()

        self.assertFalse(outcome.succeeded)
        self.assertIn("as many privileged operations", outcome.detail)
        self.assertIn("ask again", outcome.detail)

    def test_a_slow_operation_does_not_block_a_quick_one(self) -> None:
        """Rebuilding drivers takes minutes and must not hold up a status read."""

        async def both() -> float:
            slow = asyncio.create_task(self.operations.run("driver-rebuild"))
            await asyncio.sleep(0.2)
            started = time.monotonic()
            await self.operations.run("service-status", {"service": "asterisk"})
            elapsed = time.monotonic() - started
            await slow
            return elapsed

        self.assertLess(asyncio.run(both()), 2.0)


class RefusedCallerSeesAReasonTests(unittest.TestCase):
    """A refused control plane must report why, not raise.

    The daemon answers an unpermitted caller and closes without reading what
    that caller sent, so the client can meet a reset connection midway through
    its own request. That has to arrive at the operator as a sentence rather
    than as a traceback.
    """

    def test_a_refused_caller_is_told_so_in_plain_language(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)

        helper = root / "helper.sh"
        helper.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        helper.chmod(0o755)

        daemon = HelperDaemon(
            socket_path=root / "helper.sock",
            helper_path=helper,
            permitted_users=(os.getuid() + 4242,),
        )
        daemon.start()
        self.addCleanup(daemon.stop)

        operations = PrivilegedOperations(
            helper_path=helper, socket_path=root / "helper.sock"
        )
        outcome = asyncio.run(operations.run("engine-reload"))

        self.assertFalse(outcome.succeeded)
        self.assertTrue(
            outcome.output or outcome.detail,
            "a refused operation told the operator nothing at all",
        )


class NoSudoAnywhereTests(unittest.TestCase):
    """The path must not reacquire the defect it was built to remove."""

    def test_the_control_plane_never_invokes_the_setuid_escalator(self) -> None:
        """Read the code, not the prose.

        The module explains at length why it must not call the escalator, so a
        search of the text would find those explanations and fail on them. The
        source is parsed instead, and only values the code would actually use
        are examined.
        """
        source = Path(__file__).resolve().parents[1] / "appliance" / "sysops.py"
        tree = ast.parse(source.read_text(encoding="utf-8"))

        docstrings = {
            node.body[0].value
            for node in ast.walk(tree)
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
            and node.body
            and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Constant)
            and isinstance(node.body[0].value.value, str)
        }

        offending = [
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and node not in docstrings
            and "sudo" in node.value
        ]
        self.assertEqual(
            offending,
            [],
            "the control plane escalates through a setuid binary again, which "
            "cannot work under the NoNewPrivileges directive its unit sets",
        )

    def test_the_control_plane_unit_keeps_the_hardening(self) -> None:
        unit = (
            Path(__file__).resolve().parents[1]
            / "config"
            / "systemd"
            / "myipbx.service"
        )
        self.assertIn("NoNewPrivileges=yes", unit.read_text(encoding="utf-8"))

    def test_no_privilege_grant_is_shipped_for_the_service_account(self) -> None:
        grant = Path(__file__).resolve().parents[1] / "config" / "sudoers"
        self.assertFalse(
            grant.exists(),
            "the service account is granted privilege again; it should hold none",
        )


if __name__ == "__main__":
    unittest.main()
