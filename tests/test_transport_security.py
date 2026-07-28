"""Transport security: the secured listener, and what it refuses.

The console carries the administrator's password on the way in and the session
cookie on every request afterwards, and the operations it authorises reach as
far as rebooting the machine and recompiling kernel modules.  Before this, all
of it crossed the site network in the clear.

These tests are deliberately end to end where they can be.  A unit test that
asserted an SSL context was constructed would pass on an appliance that never
completed a handshake with anything, so the central test here opens a real
secured connection to a real appliance and signs in over it.

Where a certificate is needed it is generated at test time with the openssl
command line tool, which is the same tool the appliance itself uses.  A machine
without it reports these tests as skipped rather than failing them, because the
absence of a build tool is not a defect in the appliance.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import ssl
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from appliance import firewall, httpd  # noqa: E402
from appliance.config import ApplianceConfig, ConfigError  # noqa: E402
from appliance.httpd import TlsConfigurationError  # noqa: E402

from support import (  # noqa: E402
    TEST_PASSWORD,
    TEST_USERNAME,
    ApplianceHarness,
)

SOCKET_SCHEME_SCRIPT = REPOSITORY_ROOT / "tests/browser/socket-scheme-check.js"


def _openssl_available() -> bool:
    return shutil.which("openssl") is not None


def _require_openssl() -> None:
    if not _openssl_available():
        raise unittest.SkipTest(
            "the openssl command line tool is not installed, so no certificate "
            "can be generated for this test"
        )


def generate_certificate(
    directory: Path,
    common_name: str = "127.0.0.1",
    stem: str = "appliance",
) -> tuple[Path, Path]:
    """Generate a self signed pair the way the appliance generates its own.

    The names it carries matter: the secured listener tests verify the
    certificate properly rather than switching verification off, so the
    address the test connects to has to appear in it.
    """
    certificate = directory / f"{stem}.crt"
    private_key = directory / f"{stem}.key"
    configuration = directory / f"{stem}.cnf"

    configuration.write_text(
        "[req]\n"
        "distinguished_name = appliance_name\n"
        "x509_extensions = appliance_extensions\n"
        "prompt = no\n"
        "\n"
        "[appliance_name]\n"
        "O = Crossbar\n"
        f"CN = {common_name}\n"
        "\n"
        "[appliance_extensions]\n"
        "basicConstraints = critical, CA:FALSE\n"
        "keyUsage = critical, digitalSignature, keyEncipherment\n"
        "extendedKeyUsage = serverAuth\n"
        "subjectAltName = @appliance_names\n"
        "\n"
        "[appliance_names]\n"
        "DNS.1 = localhost\n"
        "IP.1 = 127.0.0.1\n",
        encoding="utf-8",
    )

    completed = subprocess.run(
        [
            "openssl", "req", "-x509", "-nodes",
            "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1",
            "-keyout", str(private_key),
            "-out", str(certificate),
            "-days", "3652",
            "-sha256",
            "-config", str(configuration),
            "-extensions", "appliance_extensions",
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if completed.returncode != 0:
        raise unittest.SkipTest(
            "this machine's openssl could not generate a certificate: "
            + completed.stderr[-500:]
        )
    return certificate, private_key


def free_port() -> int:
    """Reserve a port number by binding it and letting it go.

    The redirect listener is bound to a named port rather than an ephemeral
    one, because the appliance treats a redirect port of zero as a request not
    to run one at all.
    """
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


class ShippedDefaultTests(unittest.TestCase):
    """What an appliance does when nobody has configured it."""

    def test_the_console_does_not_bind_every_interface(self) -> None:
        config = ApplianceConfig()
        self.assertNotEqual(
            config.listen_address, "0.0.0.0",
            "the console binds every interface by default, which puts a console "
            "that can reboot this machine on networks nobody chose",
        )
        self.assertEqual(config.listen_address, "127.0.0.1")

    def test_no_source_file_still_defaults_the_console_to_every_interface(self) -> None:
        """The grep a reviewer would run, run here so it stays true."""
        offenders: list[str] = []
        for path in (REPOSITORY_ROOT / "appliance").rglob("*.py"):
            for number, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(), start=1
            ):
                if "listen_address" in line and "0.0.0.0" in line:
                    offenders.append(f"{path.name} line {number}: {line.strip()}")
        self.assertEqual(
            offenders, [],
            "the console is still defaulted to every interface: " + "; ".join(offenders),
        )

    def test_transport_security_is_on_by_default(self) -> None:
        self.assertTrue(ApplianceConfig().tls_enabled)

    def test_the_session_cookie_is_secure_by_default(self) -> None:
        self.assertTrue(
            ApplianceConfig().session_cookie_secure,
            "the session cookie may be sent over plain transport by default",
        )

    def test_the_transport_security_floor_is_not_below_the_published_one(self) -> None:
        self.assertIn(ApplianceConfig().tls_minimum_version, ("TLSv1.2", "TLSv1.3"))

    def test_a_transport_security_version_below_the_floor_is_refused(self) -> None:
        with self.assertRaises(ConfigError):
            ApplianceConfig.from_mapping({"tls_minimum_version": "TLSv1"})

    def test_a_secured_listener_with_no_certificate_named_is_refused(self) -> None:
        with self.assertRaises(ConfigError):
            ApplianceConfig.from_mapping({"tls_enabled": True, "tls_certificate": ""})

    def test_the_redirect_port_may_not_collide_with_the_console_port(self) -> None:
        with self.assertRaises(ConfigError):
            ApplianceConfig.from_mapping(
                {"listen_port": 8088, "plain_http_redirect_port": 8088}
            )


class TlsContextTests(unittest.TestCase):
    """What the appliance says when the material it was given is wrong."""

    @classmethod
    def setUpClass(cls) -> None:
        _require_openssl()

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory(prefix="crossbar-tls-")
        self.root = Path(self.directory.name)
        self.addCleanup(self.directory.cleanup)

    def test_a_generated_pair_builds_a_context_at_the_expected_floor(self) -> None:
        certificate, private_key = generate_certificate(self.root)
        context = httpd.build_tls_context(certificate, private_key, "TLSv1.2")
        self.assertEqual(context.minimum_version, ssl.TLSVersion.TLSv1_2)

    def test_a_missing_certificate_is_refused_with_an_actionable_reason(self) -> None:
        _, private_key = generate_certificate(self.root)
        absent = self.root / "not-here.crt"

        with self.assertRaises(TlsConfigurationError) as raised:
            httpd.build_tls_context(absent, private_key)

        reason = str(raised.exception)
        self.assertIn(str(absent), reason, "the refusal does not name the file")
        self.assertIn(
            "crossbar-generate-certificate.sh", reason,
            "the refusal does not tell the operator what to run",
        )

    def test_a_missing_private_key_is_refused_and_named(self) -> None:
        certificate, _ = generate_certificate(self.root)
        absent = self.root / "not-here.key"

        with self.assertRaises(TlsConfigurationError) as raised:
            httpd.build_tls_context(certificate, absent)
        self.assertIn(str(absent), str(raised.exception))

    def test_a_directory_where_a_certificate_should_be_is_refused(self) -> None:
        _, private_key = generate_certificate(self.root)
        directory = self.root / "a-directory.crt"
        directory.mkdir()

        with self.assertRaises(TlsConfigurationError) as raised:
            httpd.build_tls_context(directory, private_key)
        self.assertIn("is not a file", str(raised.exception))

    def test_an_unreadable_certificate_is_refused_with_an_actionable_reason(self) -> None:
        certificate, private_key = generate_certificate(self.root)
        certificate.chmod(0o000)
        self.addCleanup(certificate.chmod, 0o644)

        try:
            with certificate.open("rb"):
                pass
        except OSError:
            pass
        else:
            raise unittest.SkipTest(
                "this test runs with a privilege that can read any file, so an "
                "unreadable certificate cannot be produced here"
            )

        with self.assertRaises(TlsConfigurationError) as raised:
            httpd.build_tls_context(certificate, private_key)
        self.assertIn(str(certificate), str(raised.exception))

    def test_a_certificate_and_key_from_different_generations_are_refused(self) -> None:
        certificate, _ = generate_certificate(self.root, stem="first")
        _, other_key = generate_certificate(self.root, stem="second")

        with self.assertRaises(TlsConfigurationError) as raised:
            httpd.build_tls_context(certificate, other_key)
        self.assertIn("replaced as a pair", str(raised.exception))

    def test_a_fingerprint_is_reported_in_the_form_a_browser_shows(self) -> None:
        certificate, _ = generate_certificate(self.root)
        fingerprint = httpd.certificate_fingerprint(certificate)

        # Thirty two bytes rendered as pairs of hexadecimal characters joined
        # by colons, which is what every browser displays.
        parts = fingerprint.split(":")
        self.assertEqual(len(parts), 32)
        self.assertTrue(all(len(part) == 2 for part in parts))

        expected = subprocess.run(
            ["openssl", "x509", "-in", str(certificate), "-noout",
             "-fingerprint", "-sha256"],
            capture_output=True, text=True, timeout=60,
        ).stdout.strip().split("=")[-1]
        self.assertEqual(fingerprint, expected)


class CertificatePairValidationTests(unittest.TestCase):
    """The check that stands between an upload and an unreachable console."""

    @classmethod
    def setUpClass(cls) -> None:
        _require_openssl()

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory(prefix="crossbar-pair-")
        self.root = Path(self.directory.name)
        self.addCleanup(self.directory.cleanup)

    def test_a_matching_pair_validates_and_reports_its_fingerprint(self) -> None:
        certificate, private_key = generate_certificate(self.root)
        fingerprint = httpd.validate_certificate_pair(
            certificate.read_text(encoding="utf-8"),
            private_key.read_text(encoding="utf-8"),
        )
        self.assertEqual(fingerprint, httpd.certificate_fingerprint(certificate))

    def test_a_key_that_does_not_match_its_certificate_is_refused(self) -> None:
        certificate, _ = generate_certificate(self.root, stem="first")
        _, other_key = generate_certificate(self.root, stem="second")

        with self.assertRaises(TlsConfigurationError) as raised:
            httpd.validate_certificate_pair(
                certificate.read_text(encoding="utf-8"),
                other_key.read_text(encoding="utf-8"),
            )
        self.assertIn("do not match", str(raised.exception))

    def test_material_that_is_not_a_certificate_at_all_is_refused(self) -> None:
        _, private_key = generate_certificate(self.root)
        with self.assertRaises(TlsConfigurationError):
            httpd.validate_certificate_pair(
                "this is not a certificate",
                private_key.read_text(encoding="utf-8"),
            )

    def test_an_absent_certificate_or_key_is_refused(self) -> None:
        certificate, private_key = generate_certificate(self.root)
        with self.assertRaises(TlsConfigurationError):
            httpd.validate_certificate_pair("", private_key.read_text(encoding="utf-8"))
        with self.assertRaises(TlsConfigurationError):
            httpd.validate_certificate_pair(certificate.read_text(encoding="utf-8"), "")


class SecuredApplianceTestCase(unittest.IsolatedAsyncioTestCase):
    """Base for tests that need an appliance actually serving over a secured port."""

    @classmethod
    def setUpClass(cls) -> None:
        _require_openssl()

    async def asyncSetUp(self) -> None:
        self.material = tempfile.TemporaryDirectory(prefix="crossbar-serving-")
        self.addCleanup(self.material.cleanup)
        certificate, private_key = generate_certificate(Path(self.material.name))
        self.certificate = certificate

        self.harness = ApplianceHarness(
            tls_enabled=True,
            tls_certificate=str(certificate),
            tls_private_key=str(private_key),
            **self.extra_settings(),
        )
        self.harness.write_document({"revision": 1, "trunks": []})
        self.appliance = await self.harness.start()
        self.addAsyncCleanup(self.harness.stop)

    def extra_settings(self) -> dict:
        return {}

    def client_context(self) -> ssl.SSLContext:
        """A client that verifies the appliance's certificate properly.

        Verification is left on deliberately.  Switching it off would let this
        suite pass against an appliance presenting a certificate for somebody
        else entirely, which is most of what the secured listener is for.
        """
        context = ssl.create_default_context(cafile=str(self.certificate))
        return context

    async def secured_request(
        self,
        method: str,
        path: str,
        body: str | None = None,
        cookie: str | None = None,
        port: int | None = None,
    ) -> tuple[int, dict[str, str], object]:
        reader, writer = await asyncio.open_connection(
            "127.0.0.1",
            port if port is not None else self.harness.port,
            ssl=self.client_context(),
            server_hostname="127.0.0.1",
        )
        encoded = (body or "").encode("utf-8")
        lines = [
            f"{method} {path} HTTP/1.1",
            f"Host: 127.0.0.1:{self.harness.port}",
            "Connection: close",
            f"Content-Length: {len(encoded)}",
        ]
        if encoded:
            lines.append("Content-Type: application/json")
        if cookie:
            lines.append(f"Cookie: {cookie}")

        writer.write(("\r\n".join(lines) + "\r\n\r\n").encode("ascii") + encoded)
        await writer.drain()

        raw = await asyncio.wait_for(reader.read(-1), timeout=15.0)
        writer.close()
        try:
            await writer.wait_closed()
        except (OSError, ConnectionError):
            pass

        head, _, payload_bytes = raw.partition(b"\r\n\r\n")
        head_text = head.decode("latin-1")
        status = int(head_text.split("\r\n")[0].split()[1])
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


class SecuredListenerTests(SecuredApplianceTestCase):
    """A real handshake against a real appliance."""

    async def test_a_real_handshake_completes_a_sign_in(self) -> None:
        status, headers, payload = await self.secured_request(
            "POST",
            "/api/session",
            body=json.dumps({"username": TEST_USERNAME, "password": TEST_PASSWORD}),
        )
        self.assertEqual(status, 200, f"signing in over a secured connection failed: {payload}")
        self.assertTrue(payload["signed_in"])
        self.assertIn("set-cookie", headers)

    async def test_the_session_cookie_carries_the_secure_attribute(self) -> None:
        _, headers, _ = await self.secured_request(
            "POST",
            "/api/session",
            body=json.dumps({"username": TEST_USERNAME, "password": TEST_PASSWORD}),
        )
        cookie = headers["set-cookie"]
        self.assertIn(
            "Secure", cookie,
            "the session cookie may be replayed over plain transport: " + cookie,
        )
        # The attributes it already carried must survive the change.
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)

    async def test_the_session_cookie_works_over_the_secured_connection(self) -> None:
        _, headers, _ = await self.secured_request(
            "POST",
            "/api/session",
            body=json.dumps({"username": TEST_USERNAME, "password": TEST_PASSWORD}),
        )
        cookie = headers["set-cookie"].split(";")[0]

        status, _, payload = await self.secured_request("GET", "/api/state", cookie=cookie)
        self.assertEqual(status, 200, f"a secured session could not read the state: {payload}")

    async def test_the_appliance_reports_its_own_fingerprint(self) -> None:
        _, headers, _ = await self.secured_request(
            "POST",
            "/api/session",
            body=json.dumps({"username": TEST_USERNAME, "password": TEST_PASSWORD}),
        )
        cookie = headers["set-cookie"].split(";")[0]

        status, _, payload = await self.secured_request("GET", "/api/tls", cookie=cookie)
        self.assertEqual(status, 200)
        self.assertTrue(payload["secured"])
        self.assertEqual(
            payload["fingerprint"],
            httpd.certificate_fingerprint(self.certificate),
            "the fingerprint the console reports is not the one it is serving",
        )

    async def test_a_client_that_verifies_a_different_authority_is_refused(self) -> None:
        """The handshake is real, so a wrong certificate has to fail it."""
        other = tempfile.TemporaryDirectory(prefix="crossbar-other-")
        self.addCleanup(other.cleanup)
        unrelated, _ = generate_certificate(Path(other.name), stem="unrelated")

        context = ssl.create_default_context(cafile=str(unrelated))

        # The appliance's side of this refusal is reported by the event loop as
        # a failed connection, which is correct and which this test causes on
        # purpose.  Quietened so that a passing suite does not print a
        # traceback an operator would reasonably read as a fault.
        loop = asyncio.get_running_loop()
        previous = loop.get_exception_handler()
        loop.set_exception_handler(lambda one_loop, context_: None)
        self.addCleanup(loop.set_exception_handler, previous)

        logger = logging.getLogger("asyncio")
        was_disabled = logger.disabled
        logger.disabled = True
        self.addCleanup(setattr, logger, "disabled", was_disabled)

        with self.assertRaises(ssl.SSLError):
            reader, writer = await asyncio.open_connection(
                "127.0.0.1", self.harness.port,
                ssl=context, server_hostname="127.0.0.1",
            )
            writer.close()

        # The appliance is still serving afterwards: a refused handshake must
        # not take the listener down with it.
        status, _, _ = await self.secured_request("GET", "/api/health")
        self.assertEqual(status, 200)


class RefusalToServeWithoutACertificateTests(unittest.IsolatedAsyncioTestCase):
    """The appliance must not quietly fall back to plain transport."""

    @classmethod
    def setUpClass(cls) -> None:
        _require_openssl()

    async def test_the_appliance_refuses_to_start_without_its_certificate(self) -> None:
        directory = tempfile.TemporaryDirectory(prefix="crossbar-absent-")
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)

        harness = ApplianceHarness(
            tls_enabled=True,
            tls_certificate=str(root / "absent.crt"),
            tls_private_key=str(root / "absent.key"),
        )
        harness.write_document({"revision": 1, "trunks": []})
        self.addAsyncCleanup(harness.stop)

        with self.assertRaises(TlsConfigurationError) as raised:
            await harness.start()

        reason = str(raised.exception)
        self.assertIn("absent.crt", reason)
        self.assertIn("crossbar-generate-certificate.sh", reason)

    async def test_the_appliance_refuses_to_start_on_a_mismatched_pair(self) -> None:
        directory = tempfile.TemporaryDirectory(prefix="crossbar-mismatch-")
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        certificate, _ = generate_certificate(root, stem="first")
        _, other_key = generate_certificate(root, stem="second")

        harness = ApplianceHarness(
            tls_enabled=True,
            tls_certificate=str(certificate),
            tls_private_key=str(other_key),
        )
        harness.write_document({"revision": 1, "trunks": []})
        self.addAsyncCleanup(harness.stop)

        with self.assertRaises(TlsConfigurationError):
            await harness.start()

    async def test_nothing_is_left_listening_after_a_refusal(self) -> None:
        """A refusal must not leave a half started appliance behind it.

        The certificate is read before any socket is bound, so a refused start
        leaves nothing serving.  If that ordering were ever reversed, an
        appliance would answer on an unsecured port while reporting that it had
        refused to start.
        """
        directory = tempfile.TemporaryDirectory(prefix="crossbar-partial-")
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)

        harness = ApplianceHarness(
            tls_enabled=True,
            tls_certificate=str(root / "absent.crt"),
            tls_private_key=str(root / "absent.key"),
        )
        harness.write_document({"revision": 1, "trunks": []})
        self.addAsyncCleanup(harness.stop)

        with self.assertRaises(TlsConfigurationError):
            await harness.start()

        self.assertEqual(
            harness.appliance.http.bound_port, 0,
            "the appliance bound a listening socket despite refusing to start",
        )
        self.assertIsNone(harness.appliance.redirect)


class PlainPortRedirectTests(SecuredApplianceTestCase):
    """The plain port answers, and answers with one thing only."""

    def extra_settings(self) -> dict:
        self._redirect_port = free_port()
        return {"plain_http_redirect_port": self._redirect_port}

    async def plain_request(self, method: str, path: str) -> tuple[int, dict[str, str], bytes]:
        reader, writer = await asyncio.open_connection(
            "127.0.0.1", self.appliance.redirect.bound_port
        )
        writer.write(
            (
                f"{method} {path} HTTP/1.1\r\n"
                f"Host: 127.0.0.1:{self.appliance.redirect.bound_port}\r\n"
                "Connection: close\r\n\r\n"
            ).encode("ascii")
        )
        await writer.drain()
        raw = await asyncio.wait_for(reader.read(-1), timeout=15.0)
        writer.close()
        try:
            await writer.wait_closed()
        except (OSError, ConnectionError):
            pass

        head, _, body = raw.partition(b"\r\n\r\n")
        head_text = head.decode("latin-1")
        status = int(head_text.split("\r\n")[0].split()[1])
        headers: dict[str, str] = {}
        for line in head_text.split("\r\n")[1:]:
            name, separator, value = line.partition(":")
            if separator:
                headers[name.strip().lower()] = value.strip()
        return status, headers, body

    async def test_the_plain_port_is_listening(self) -> None:
        self.assertIsNotNone(self.appliance.redirect)
        self.assertEqual(self.appliance.redirect.bound_port, self._redirect_port)

    async def test_the_root_is_answered_with_a_redirect_to_the_secured_listener(self) -> None:
        status, headers, body = await self.plain_request("GET", "/")
        self.assertIn(status, (301, 308))
        self.assertTrue(
            headers["location"].startswith("https://"),
            f"the plain port redirected to an unsecured address: {headers['location']}",
        )
        self.assertIn(str(self.harness.port), headers["location"])
        self.assertEqual(body, b"", "the plain port served content")

    async def test_the_plain_port_never_issues_a_cookie(self) -> None:
        """The one thing this port must never do."""
        for method, path in (
            ("GET", "/"),
            ("GET", "/index.html"),
            ("POST", "/api/session"),
            ("GET", "/api/state"),
        ):
            with self.subTest(method=method, path=path):
                status, headers, body = await self.plain_request(method, path)
                self.assertNotIn(
                    "set-cookie", headers,
                    f"the plain port issued a cookie for {method} {path}",
                )
                self.assertIn(status, (301, 308))
                self.assertEqual(body, b"", f"the plain port served content for {path}")

    async def test_the_plain_port_serves_no_dashboard_file(self) -> None:
        status, headers, body = await self.plain_request("GET", "/js/socket.js")
        self.assertIn(status, (301, 308))
        self.assertEqual(body, b"")
        self.assertNotIn("function", body.decode("latin-1", "ignore"))
        self.assertTrue(headers["location"].startswith("https://"))

    async def test_the_requested_path_is_carried_into_the_redirect(self) -> None:
        _, headers, _ = await self.plain_request("GET", "/api/state?verbose=yes")
        self.assertTrue(headers["location"].endswith("/api/state?verbose=yes"))

    async def test_a_malformed_request_is_still_answered_with_a_redirect(self) -> None:
        reader, writer = await asyncio.open_connection(
            "127.0.0.1", self.appliance.redirect.bound_port
        )
        writer.write(b"this is not a request line\r\n\r\n")
        await writer.drain()
        raw = await asyncio.wait_for(reader.read(-1), timeout=15.0)
        writer.close()
        try:
            await writer.wait_closed()
        except (OSError, ConnectionError):
            pass

        head, _, body = raw.partition(b"\r\n\r\n")
        self.assertIn(int(head.decode("latin-1").split("\r\n")[0].split()[1]), (301, 308))
        self.assertEqual(body, b"")


class CertificateUploadTests(SecuredApplianceTestCase):
    """Installing a site's own certificate without reaching for a terminal."""

    async def signed_in_cookie(self) -> str:
        _, headers, _ = await self.secured_request(
            "POST",
            "/api/session",
            body=json.dumps({"username": TEST_USERNAME, "password": TEST_PASSWORD}),
        )
        return headers["set-cookie"].split(";")[0]

    async def test_a_key_that_does_not_match_its_certificate_is_refused(self) -> None:
        cookie = await self.signed_in_cookie()
        directory = tempfile.TemporaryDirectory(prefix="crossbar-upload-")
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        certificate, _ = generate_certificate(root, stem="first")
        _, other_key = generate_certificate(root, stem="second")

        status, _, payload = await self.secured_request(
            "POST",
            "/api/tls/certificate",
            body=json.dumps(
                {
                    "certificate": certificate.read_text(encoding="utf-8"),
                    "private_key": other_key.read_text(encoding="utf-8"),
                }
            ),
            cookie=cookie,
        )
        self.assertEqual(status, 400, f"a mismatched pair was accepted: {payload}")
        self.assertIn("do not match", payload["error"])

        # And nothing may have been written by a refused upload.
        staged = self.harness.config.state_path / "tls-staged"
        self.assertFalse(
            (staged / "appliance.key").exists(),
            "a refused certificate left a private key on the appliance",
        )

    async def test_a_matching_pair_is_accepted_and_staged_but_not_applied(self) -> None:
        cookie = await self.signed_in_cookie()
        directory = tempfile.TemporaryDirectory(prefix="crossbar-upload-")
        self.addCleanup(directory.cleanup)
        certificate, private_key = generate_certificate(Path(directory.name), stem="site")

        status, _, payload = await self.secured_request(
            "POST",
            "/api/tls/certificate",
            body=json.dumps(
                {
                    "certificate": certificate.read_text(encoding="utf-8"),
                    "private_key": private_key.read_text(encoding="utf-8"),
                }
            ),
            cookie=cookie,
        )
        self.assertEqual(status, 200, f"a matching pair was refused: {payload}")
        self.assertTrue(payload["staged"])
        self.assertFalse(
            payload["applied"],
            "the upload restarted the console inside the request that uploaded it",
        )
        self.assertEqual(
            payload["fingerprint"], httpd.certificate_fingerprint(certificate)
        )

        staged = self.harness.config.state_path / "tls-staged"
        self.assertTrue((staged / "appliance.crt").is_file())
        self.assertEqual(
            (staged / "appliance.key").stat().st_mode & 0o777, 0o600,
            "a staged private key is readable by more than its owner",
        )

    async def test_the_response_warns_that_applying_ends_every_session(self) -> None:
        cookie = await self.signed_in_cookie()
        directory = tempfile.TemporaryDirectory(prefix="crossbar-upload-")
        self.addCleanup(directory.cleanup)
        certificate, private_key = generate_certificate(Path(directory.name), stem="site")

        _, _, payload = await self.secured_request(
            "POST",
            "/api/tls/certificate",
            body=json.dumps(
                {
                    "certificate": certificate.read_text(encoding="utf-8"),
                    "private_key": private_key.read_text(encoding="utf-8"),
                }
            ),
            cookie=cookie,
        )
        warning = payload["warning"]
        self.assertIn("restarts the console", warning)
        self.assertIn("signed out", warning)
        self.assertIn("certificate-apply", payload["next_step"])

        # Constraint Two: the warning is operator facing, so its counts are
        # spelled.  The fingerprint beside it is deliberately not.
        self.assertNotRegex(
            warning, r"\d",
            f"a digit reached an operator facing warning: {warning}",
        )

    async def test_an_unauthenticated_caller_cannot_upload_a_certificate(self) -> None:
        directory = tempfile.TemporaryDirectory(prefix="crossbar-upload-")
        self.addCleanup(directory.cleanup)
        certificate, private_key = generate_certificate(Path(directory.name), stem="site")

        status, _, _ = await self.secured_request(
            "POST",
            "/api/tls/certificate",
            body=json.dumps(
                {
                    "certificate": certificate.read_text(encoding="utf-8"),
                    "private_key": private_key.read_text(encoding="utf-8"),
                }
            ),
        )
        self.assertEqual(status, 401)


class FirewallConsoleReachabilityTests(unittest.TestCase):
    """Applying a firewall must not lock the administrator out of either port."""

    def test_both_console_ports_appear_in_a_generated_ruleset(self) -> None:
        ruleset = firewall.render_ruleset([], management_port=8088, redirect_port=8080)
        self.assertIn("tcp dport 8088 accept", ruleset)
        self.assertIn("tcp dport 8080 accept", ruleset)

    def test_both_console_ports_survive_a_ruleset_full_of_declared_rules(self) -> None:
        rules = [
            {"name": "signalling", "service": "session protocol", "source": "any"},
            {"name": "audio", "service": "media", "source": "192.0.2.0/24"},
            {"name": "shell", "service": "secure shell", "source": "192.0.2.10/32"},
        ]
        ruleset = firewall.render_ruleset(rules, management_port=8088, redirect_port=8080)
        self.assertIn("tcp dport 8088 accept", ruleset)
        self.assertIn("tcp dport 8080 accept", ruleset)

    def test_both_console_ports_are_opened_before_any_declared_rule(self) -> None:
        """Ordering is what makes the lockout impossible rather than unlikely."""
        rules = [{"name": "signalling", "service": "session protocol", "source": "any"}]
        ruleset = firewall.render_ruleset(rules, management_port=8088, redirect_port=8080)

        secure_at = ruleset.index("tcp dport 8088 accept")
        redirect_at = ruleset.index("tcp dport 8080 accept")
        declared_at = ruleset.index('comment "signalling"')
        self.assertLess(secure_at, declared_at)
        self.assertLess(redirect_at, declared_at)

    def test_the_console_ports_are_opened_from_every_declared_source(self) -> None:
        ruleset = firewall.render_ruleset(
            [],
            management_port=8088,
            management_sources=["192.0.2.0/24", "198.51.100.0/24"],
            redirect_port=8080,
        )
        for source in ("192.0.2.0/24", "198.51.100.0/24"):
            for port in ("8088", "8080"):
                self.assertIn(f"ip saddr {source} tcp dport {port} accept", ruleset)

    def test_an_appliance_without_a_redirect_port_opens_only_the_console(self) -> None:
        ruleset = firewall.render_ruleset([], management_port=8088, redirect_port=0)
        self.assertIn("tcp dport 8088 accept", ruleset)
        self.assertNotIn("tcp dport 8080 accept", ruleset)

    def test_a_redirect_port_equal_to_the_console_port_is_not_emitted_twice(self) -> None:
        ruleset = firewall.render_ruleset([], management_port=8088, redirect_port=8088)
        self.assertEqual(ruleset.count("tcp dport 8088 accept"), 1)

    def test_an_invalid_redirect_port_is_refused(self) -> None:
        with self.assertRaises(firewall.FirewallError):
            firewall.render_ruleset([], management_port=8088, redirect_port=70000)


class GeneratedRulesetReachabilityTests(unittest.IsolatedAsyncioTestCase):
    """The ruleset the appliance actually writes, not one a test composed."""

    async def test_the_appliance_writes_a_ruleset_that_keeps_its_console_open(self) -> None:
        harness = ApplianceHarness()
        harness.write_document({"revision": 1, "trunks": [], "firewall_rules": []})
        appliance = await harness.start()
        self.addAsyncCleanup(harness.stop)

        run = await appliance.tasks.run("render-firewall")
        self.assertTrue(run.succeeded, f"the firewall task failed: {run.as_dict()}")

        ruleset = (harness.config.state_path / "firewall.nft").read_text(encoding="utf-8")
        self.assertIn(
            f"tcp dport {appliance.http.bound_port} accept", ruleset,
            "the generated ruleset does not keep the port the console is on open",
        )


class CertificateGeneratorScriptTests(unittest.TestCase):
    """The script each appliance runs to give itself a certificate."""

    SCRIPT = REPOSITORY_ROOT / "scripts/crossbar-generate-certificate.sh"

    def test_the_generator_is_present_and_executable(self) -> None:
        self.assertTrue(self.SCRIPT.is_file(), "the certificate generator is absent")
        self.assertTrue(
            self.SCRIPT.stat().st_mode & 0o111,
            "the certificate generator is not executable, so no service unit could run it",
        )

    def test_the_generator_asks_for_a_ten_year_certificate(self) -> None:
        text = self.SCRIPT.read_text(encoding="utf-8")
        self.assertIn("3652", text, "the certificate does not last ten years")
        self.assertIn("-x509", text)

    def test_the_generator_asks_for_a_key_of_adequate_strength(self) -> None:
        text = self.SCRIPT.read_text(encoding="utf-8")
        self.assertTrue(
            "prime256v1" in text or "rsa:4096" in text,
            "the generated key is neither a P-256 curve key nor a four thousand "
            "ninety six bit key",
        )

    def test_the_generated_private_key_is_not_world_readable(self) -> None:
        text = self.SCRIPT.read_text(encoding="utf-8")
        self.assertIn("chmod 0640", text)
        self.assertNotIn("chmod 0644 \"${temporary_key}\"", text)

    def test_the_generator_names_both_the_address_and_the_host_name(self) -> None:
        text = self.SCRIPT.read_text(encoding="utf-8")
        self.assertIn("subjectAltName", text)
        self.assertIn("configured_address", text)
        self.assertIn("configured_hostname", text)

    def test_the_generator_is_idempotent_by_construction(self) -> None:
        """A unit that runs on every boot must do nothing on almost all of them."""
        text = self.SCRIPT.read_text(encoding="utf-8")
        self.assertIn("certificate_is_usable", text)
        self.assertIn("-checkend", text, "an expiring certificate is never renewed")
        self.assertIn("already holds a usable certificate", text)

    @unittest.skipUnless(_openssl_available(), "the openssl command line tool is absent")
    def test_the_generator_produces_a_usable_pair_and_repeats_itself_safely(self) -> None:
        """Run for real, then run again and confirm it changed nothing."""
        import os

        if os.geteuid() != 0:
            raise unittest.SkipTest(
                "the certificate generator requires administrative privilege, which "
                "this test run does not hold"
            )

        directory = tempfile.TemporaryDirectory(prefix="crossbar-generator-")
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        (root / "etc/crossbar").mkdir(parents=True)
        (root / "var/log/crossbar").mkdir(parents=True)
        (root / "var/lib/crossbar").mkdir(parents=True)
        (root / "etc/crossbar/appliance.json").write_text(
            json.dumps({"appliance": {"listen_address": "192.0.2.15"}}), encoding="utf-8"
        )

        environment = dict(os.environ)
        environment.update(
            {
                "APPLIANCE_CONFIG_DIR": str(root / "etc/crossbar"),
                "APPLIANCE_LOG_DIR": str(root / "var/log/crossbar"),
                "APPLIANCE_STATE_DIR": str(root / "var/lib/crossbar"),
            }
        )

        first = subprocess.run(
            ["bash", str(self.SCRIPT)],
            capture_output=True, text=True, timeout=300, env=environment,
        )
        self.assertEqual(
            first.returncode, 0,
            f"the generator failed: {first.stdout[-2000:]} {first.stderr[-2000:]}",
        )

        certificate = root / "etc/crossbar/tls/appliance.crt"
        private_key = root / "etc/crossbar/tls/appliance.key"
        self.assertTrue(certificate.is_file(), "no certificate was generated")
        self.assertTrue(private_key.is_file(), "no private key was generated")

        # The permissions the appliance depends on to be able to read its own
        # key, and that nobody else may.
        self.assertEqual(private_key.stat().st_mode & 0o777, 0o640)
        self.assertEqual(certificate.stat().st_mode & 0o777, 0o644)

        # The pair must actually build a listener context.
        httpd.build_tls_context(certificate, private_key)

        # It must name the address from the configuration document.
        described = subprocess.run(
            ["openssl", "x509", "-in", str(certificate), "-noout", "-text"],
            capture_output=True, text=True, timeout=60,
        ).stdout
        self.assertIn("192.0.2.15", described, "the certificate does not name the console's address")
        self.assertIn("Subject Alternative Name", described)

        # And a second run must leave the first run's material exactly alone,
        # which is what makes it safe on a unit that fires on every boot.
        before = certificate.read_bytes(), private_key.read_bytes()
        second = subprocess.run(
            ["bash", str(self.SCRIPT)],
            capture_output=True, text=True, timeout=300, env=environment,
        )
        self.assertEqual(second.returncode, 0)
        self.assertEqual(
            (certificate.read_bytes(), private_key.read_bytes()), before,
            "running the generator again replaced a certificate that was still good",
        )

    @unittest.skipUnless(_openssl_available(), "the openssl command line tool is absent")
    def test_no_digit_reaches_the_operator_except_in_the_fingerprint(self) -> None:
        """Constraint Two, on this script's own output."""
        import os
        import re as regular_expressions

        if os.geteuid() != 0:
            raise unittest.SkipTest(
                "the certificate generator requires administrative privilege"
            )

        directory = tempfile.TemporaryDirectory(prefix="crossbar-generator-words-")
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        (root / "etc/crossbar").mkdir(parents=True)
        (root / "var/log/crossbar").mkdir(parents=True)
        (root / "var/lib/crossbar").mkdir(parents=True)

        environment = dict(os.environ)
        environment.update(
            {
                "APPLIANCE_CONFIG_DIR": str(root / "etc/crossbar"),
                "APPLIANCE_LOG_DIR": str(root / "var/log/crossbar"),
                "APPLIANCE_STATE_DIR": str(root / "var/lib/crossbar"),
            }
        )
        completed = subprocess.run(
            ["bash", str(self.SCRIPT)],
            capture_output=True, text=True, timeout=300, env=environment,
        )
        self.assertEqual(completed.returncode, 0)

        # Every quantity this script reports is spelled. Identifiers are not,
        # and the difference is the rule: an operator compares a fingerprint
        # against a browser, types an address into one, and reads a timestamp
        # to know when something happened. Words serve none of those.
        #
        # So the line is stripped of everything that is legitimately an
        # identifier, and whatever digits remain are quantities that escaped.
        for line in completed.stdout.splitlines():
            # The temporary directory this test runs in carries digits only
            # because the operating system put them there.
            if str(root) in line or root.name in line:
                continue

            remainder = line
            for shape in (
                r"([0-9A-Fa-f]{2}:)+[0-9A-Fa-f]{2}",          # a fingerprint
                r"\d{4}-\d{2}-\d{2}([ T]\d{2}:\d{2}(:\d{2})?)?",  # a date
                r"\d{2}:\d{2}(:\d{2})?",                      # a time
                r"(\d{1,3}\.){3}\d{1,3}(/\d{1,2})?",           # an address
                r"v?\d+\.\d+(\.\d+)*",                        # a version
                r"(/[A-Za-z0-9._-]*\d[A-Za-z0-9._-]*)+",      # a path
                r"\bport(\s+number)?\s+\d{1,5}\b",           # a port
            ):
                remainder = regular_expressions.sub(shape, " ", remainder)

            with self.subTest(line=line):
                self.assertNotRegex(
                    remainder, r"\d",
                    f"a quantity reached the operator as digits: {line}",
                )


class CertificateServiceUnitTests(unittest.TestCase):
    """The oneshot unit that gives an image booted appliance its certificate.

    This unit is held to a deliberately suspicious standard.  A predecessor
    that set the appliance's host name failed on every boot, survived in a
    reused build tree after the code that wrote it was deleted, and shipped in
    images that reported it failing.  A unit that runs before the console can
    serve is exactly the kind that must not become that.
    """

    UNIT = REPOSITORY_ROOT / "config/systemd/crossbar-certificate.service"
    APPLIANCE_UNIT = REPOSITORY_ROOT / "config/systemd/crossbar.service"

    def setUp(self) -> None:
        self.assertTrue(self.UNIT.is_file(), "the certificate service unit is absent")
        self.text = self.UNIT.read_text(encoding="utf-8")

    def test_the_unit_is_a_oneshot_that_stays_satisfied(self) -> None:
        self.assertIn("Type=oneshot", self.text)
        self.assertIn(
            "RemainAfterExit=yes", self.text,
            "the unit would be re-run and reported as inactive rather than done",
        )

    def test_the_unit_runs_before_the_console(self) -> None:
        self.assertIn("Before=crossbar.service", self.text)

    def test_the_console_orders_itself_after_the_certificate(self) -> None:
        appliance_unit = self.APPLIANCE_UNIT.read_text(encoding="utf-8")
        self.assertIn("After=crossbar-certificate.service", appliance_unit)
        self.assertIn(
            "Wants=crossbar-certificate.service", appliance_unit,
            "the console requires the certificate unit, so a failure there would "
            "stop the console from starting and reporting why",
        )

    def test_the_unit_depends_on_nothing_that_is_not_up_early(self) -> None:
        """The defect that shipped last time, expressed as a test."""
        forbidden = ("dbus", "network-online", "basic.target", "graphical.target")
        for token in forbidden:
            with self.subTest(dependency=token):
                self.assertNotIn(
                    token, self.text.lower(),
                    f"the certificate unit waits on {token}, which is how a unit "
                    "comes to fail on every boot",
                )

    def test_the_unit_does_not_restart_itself_in_a_loop(self) -> None:
        self.assertIn("Restart=no", self.text)

    def test_the_unit_runs_the_generator_that_is_actually_installed(self) -> None:
        self.assertIn("ExecStart=/opt/crossbar/bin/crossbar-generate-certificate.sh", self.text)
        payload = (REPOSITORY_ROOT / "iso/stages/two-payload.sh").read_text(encoding="utf-8")
        self.assertIn(
            "crossbar-generate-certificate.sh", payload,
            "the unit runs a script the image never installs",
        )

    def test_the_image_enables_the_unit(self) -> None:
        configure = (REPOSITORY_ROOT / "iso/stages/three-configure.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn("systemctl enable crossbar-certificate.service", configure)

    def test_the_image_installs_the_unit(self) -> None:
        payload = (REPOSITORY_ROOT / "iso/stages/two-payload.sh").read_text(encoding="utf-8")
        self.assertIn("crossbar-certificate.service", payload)


class NoCertificateTravelsInTheImageTests(unittest.TestCase):
    """One private key shared by every appliance would be worse than none."""

    CONFIGURE = REPOSITORY_ROOT / "iso/stages/three-configure.sh"

    def test_the_image_audit_refuses_a_baked_in_certificate(self) -> None:
        text = self.CONFIGURE.read_text(encoding="utf-8")
        self.assertIn("transport security material was baked into the image", text)
        self.assertIn("appliance.key", text)

    def test_the_image_is_tidied_of_any_certificate_a_build_left_behind(self) -> None:
        """The reused build tree is how the last superseded artefact shipped."""
        text = self.CONFIGURE.read_text(encoding="utf-8")
        self.assertIn("rm -f \"${CHROOT_DIR}\"/etc/crossbar/tls/appliance.*", text)

    def test_no_certificate_or_key_is_committed_to_the_repository(self) -> None:
        offenders: list[str] = []
        for pattern in ("*.key", "*.crt", "*.pem"):
            for path in REPOSITORY_ROOT.rglob(pattern):
                if ".git" in path.parts:
                    continue
                offenders.append(path.relative_to(REPOSITORY_ROOT).as_posix())
        self.assertEqual(
            offenders, [],
            "transport security material is committed to the repository, which "
            "would give every appliance built from it the same private key: "
            + "; ".join(offenders),
        )


class SocketSchemeTests(unittest.TestCase):
    """The dashboard must open a secured socket from a secured page.

    A page loaded over a secured transport is forbidden by every browser from
    opening an unsecured socket.  The dashboard already derives its scheme from
    the page's own, and this holds that still: were it ever hard coded, the
    console would load, look healthy, and never receive a single update.
    """

    def test_the_socket_client_derives_its_scheme_from_the_page(self) -> None:
        if shutil.which("node") is None:
            raise unittest.SkipTest("no browser scripting runtime is installed")
        if not SOCKET_SCHEME_SCRIPT.is_file():
            raise unittest.SkipTest("the socket scheme harness script is absent")

        completed = subprocess.run(
            ["node", str(SOCKET_SCHEME_SCRIPT)],
            capture_output=True, text=True, timeout=120,
            cwd=str(REPOSITORY_ROOT),
        )

        report = None
        for line in completed.stdout.splitlines():
            if line.startswith("RESULT "):
                report = json.loads(line[len("RESULT "):])
        self.assertIsNotNone(report, f"no result was produced: {completed.stdout[-2000:]}")

        failed = [item for item in report["results"] if not item["passed"]]
        self.assertEqual(
            failed, [],
            "the dashboard chose the wrong socket scheme: "
            + "; ".join(f"{item['name']} ({item['detail']})" for item in failed),
        )
        self.assertGreaterEqual(len(report["results"]), 5)


if __name__ == "__main__":
    unittest.main()
