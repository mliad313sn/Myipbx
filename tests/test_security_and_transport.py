"""Authentication, session handling, request parsing, and task execution."""

from __future__ import annotations

import asyncio
import tempfile
import time
import unittest
from pathlib import Path

import support  # noqa: F401  (path setup)

from appliance.httpd import (
    MalformedRequest,
    RequestTooLarge,
    Response,
    Router,
    parse_request_head,
)
from appliance.security import (
    CredentialStore,
    LoginThrottle,
    PasswordHasher,
    SessionStore,
    generate_password,
)
from appliance.tasks import (
    STATUS_FAILED,
    STATUS_REJECTED,
    STATUS_SUCCEEDED,
    STATUS_TIMED_OUT,
    TaskError,
    TaskScheduler,
)


class PasswordHasherTests(unittest.TestCase):
    def setUp(self) -> None:
        self.hasher = PasswordHasher(100000)

    def test_a_password_verifies_against_its_own_derivation(self) -> None:
        encoded = self.hasher.hash("a correct horse battery staple")
        self.assertTrue(self.hasher.verify("a correct horse battery staple", encoded))
        self.assertFalse(self.hasher.verify("the wrong password", encoded))

    def test_the_same_password_produces_a_different_derivation_each_time(self) -> None:
        first = self.hasher.hash("the same password")
        second = self.hasher.hash("the same password")
        self.assertNotEqual(first, second, "the salt is not being applied")

    def test_the_plaintext_never_appears_in_the_stored_value(self) -> None:
        encoded = self.hasher.hash("a memorable secret")
        self.assertNotIn("a memorable secret", encoded)

    def test_a_malformed_stored_value_fails_rather_than_raising(self) -> None:
        for candidate in ("", "nonsense", "pbkdf2_sha256$notanumber$aa$bb", "a$b$c"):
            with self.subTest(candidate=candidate):
                self.assertFalse(self.hasher.verify("anything", candidate))

    def test_an_empty_password_cannot_be_hashed(self) -> None:
        with self.assertRaises(ValueError):
            self.hasher.hash("")

    def test_an_iteration_count_below_policy_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            PasswordHasher(1000)

    def test_a_weaker_stored_derivation_is_flagged_for_rehashing(self) -> None:
        weak = PasswordHasher(100000).hash("a password")
        strong = PasswordHasher(240000)
        self.assertTrue(strong.needs_rehash(weak))
        self.assertFalse(strong.needs_rehash(strong.hash("a password")))

    def test_the_shipped_iteration_count_meets_policy(self) -> None:
        from appliance.config import ApplianceConfig

        self.assertGreaterEqual(ApplianceConfig().password_iterations, 240000)


class GeneratedPasswordTests(unittest.TestCase):
    def test_a_generated_password_avoids_ambiguous_characters(self) -> None:
        for _ in range(50):
            password = generate_password()
            self.assertEqual(len(password), 20)
            for character in "0O1lI":
                self.assertNotIn(character, password)

    def test_generated_passwords_do_not_repeat(self) -> None:
        produced = {generate_password() for _ in range(100)}
        self.assertEqual(len(produced), 100)

    def test_a_short_password_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            generate_password(8)


class SessionStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.moment = 1000.0
        self.store = SessionStore(
            idle_seconds=60, lifetime_seconds=600, maximum=3,
            clock=lambda: self.moment,
        )

    def test_a_created_session_validates_and_carries_its_owner(self) -> None:
        token = self.store.create("administrator", "192.0.2.10")
        session = self.store.validate(token)
        assert session is not None
        self.assertEqual(session.username, "administrator")
        self.assertEqual(session.source_address, "192.0.2.10")

    def test_an_unknown_token_never_validates(self) -> None:
        self.store.create("administrator")
        self.assertIsNone(self.store.validate("a token we never issued"))
        self.assertIsNone(self.store.validate(""))
        self.assertIsNone(self.store.validate(None))

    def test_a_session_expires_after_its_idle_period(self) -> None:
        token = self.store.create("administrator")
        self.moment += 61
        self.assertIsNone(self.store.validate(token))
        self.assertEqual(len(self.store), 0)

    def test_activity_extends_a_session(self) -> None:
        token = self.store.create("administrator")
        for _ in range(10):
            self.moment += 30
            self.assertIsNotNone(self.store.validate(token))

    def test_activity_cannot_extend_a_session_past_its_lifetime(self) -> None:
        """The ceiling being busy cannot lift.

        Idle expiry alone never ends a session that keeps being used. A console
        left open on a wall display renews itself every time the page polls,
        and so does a token an attacker has taken and is polling: both live
        until the appliance restarts. This is the other half of the rule.
        """
        token = self.store.create("administrator")
        for _ in range(20):
            self.moment += 30
            self.assertIsNotNone(
                self.store.validate(token),
                "a session ended before its lifetime while in constant use",
            )
        # Exactly ten minutes in, and still live: the boundary belongs to the
        # session, the same way the idle boundary does.

        self.moment += 1
        self.assertIsNone(
            self.store.validate(token),
            "a session in constant use outlived its absolute lifetime",
        )
        self.assertEqual(len(self.store), 0)

    def test_a_session_past_its_lifetime_is_purged_without_being_asked_for(self) -> None:
        """The sweep must find it too, or a dead session holds a place in the
        population ceiling until somebody happens to present its token."""
        self.store.create("administrator")
        self.moment += 601
        self.assertEqual(self.store.purge_expired(), 1)
        self.assertEqual(len(self.store), 0)

    def test_a_lifetime_shorter_than_the_idle_expiry_is_refused(self) -> None:
        """It would end every session at the moment it began."""
        with self.assertRaises(ValueError):
            SessionStore(idle_seconds=600, lifetime_seconds=60)

    def test_a_revoked_session_stops_validating(self) -> None:
        token = self.store.create("administrator")
        self.assertTrue(self.store.revoke(token))
        self.assertIsNone(self.store.validate(token))
        self.assertFalse(self.store.revoke(token))

    def test_the_population_ceiling_evicts_the_least_recently_used(self) -> None:
        first = self.store.create("one")
        self.moment += 1
        second = self.store.create("two")
        self.moment += 1
        third = self.store.create("three")
        self.moment += 1

        # Touch the first so the second becomes least recently used.
        self.store.validate(first)
        fourth = self.store.create("four")

        self.assertEqual(len(self.store), 3)
        self.assertIsNotNone(self.store.validate(first))
        self.assertIsNone(self.store.validate(second))
        self.assertIsNotNone(self.store.validate(third))
        self.assertIsNotNone(self.store.validate(fourth))

    def test_the_plaintext_token_is_not_retained_by_the_store(self) -> None:
        token = self.store.create("administrator")
        held = [session.identifier for session in self.store.active()]
        for identifier in held:
            self.assertNotIn(token, identifier)

    def test_revoking_everything_clears_the_population(self) -> None:
        for index in range(3):
            self.store.create(f"user-{index}")
        self.assertEqual(self.store.revoke_all(), 3)
        self.assertEqual(len(self.store), 0)

    def test_an_idle_expiry_below_the_floor_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            SessionStore(idle_seconds=10)


class LoginThrottleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.moment = 1000.0
        self.throttle = LoginThrottle(
            attempt_limit=3, lockout_seconds=300, clock=lambda: self.moment
        )

    def test_a_source_locks_out_at_the_attempt_limit(self) -> None:
        self.assertFalse(self.throttle.record_failure("192.0.2.10"))
        self.assertFalse(self.throttle.record_failure("192.0.2.10"))
        self.assertTrue(self.throttle.record_failure("192.0.2.10"))
        self.assertTrue(self.throttle.is_locked("192.0.2.10"))

    def test_the_lockout_expires_on_its_own(self) -> None:
        for _ in range(3):
            self.throttle.record_failure("192.0.2.10")
        self.assertTrue(self.throttle.is_locked("192.0.2.10"))
        self.moment += 301
        self.assertFalse(self.throttle.is_locked("192.0.2.10"))

    def test_one_source_locking_out_does_not_affect_another(self) -> None:
        for _ in range(3):
            self.throttle.record_failure("192.0.2.10")
        self.assertTrue(self.throttle.is_locked("192.0.2.10"))
        self.assertFalse(self.throttle.is_locked("192.0.2.11"))

    def test_a_success_clears_the_accumulated_failures(self) -> None:
        self.throttle.record_failure("192.0.2.10")
        self.throttle.record_failure("192.0.2.10")
        self.throttle.record_success("192.0.2.10")
        self.assertFalse(self.throttle.record_failure("192.0.2.10"))
        self.assertFalse(self.throttle.is_locked("192.0.2.10"))

    def test_failures_outside_the_window_do_not_accumulate(self) -> None:
        self.throttle.record_failure("192.0.2.10")
        self.moment += 400
        self.throttle.record_failure("192.0.2.10")
        self.assertFalse(self.throttle.is_locked("192.0.2.10"))

    def test_the_remaining_lockout_is_reported(self) -> None:
        for _ in range(3):
            self.throttle.record_failure("192.0.2.10")
        self.assertEqual(self.throttle.remaining_lockout_seconds("192.0.2.10"), 300)
        self.assertEqual(self.throttle.remaining_lockout_seconds("192.0.2.11"), 0)


class CredentialStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory(prefix="crossbar-credential-")
        self.path = Path(self.directory.name) / "credentials.json"
        self.store = CredentialStore(self.path, PasswordHasher(100000))

    def tearDown(self) -> None:
        self.directory.cleanup()

    def test_a_saved_credential_verifies(self) -> None:
        self.store.save("administrator", "a chosen password")
        self.assertTrue(self.store.verify("administrator", "a chosen password"))
        self.assertFalse(self.store.verify("administrator", "the wrong password"))
        self.assertFalse(self.store.verify("someone-else", "a chosen password"))

    def test_the_credential_file_is_owner_readable_only(self) -> None:
        self.store.save("administrator", "a chosen password")
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_the_plaintext_is_never_written_to_disk(self) -> None:
        self.store.save("administrator", "a memorable secret")
        content = self.path.read_text(encoding="utf-8")
        self.assertNotIn("a memorable secret", content)

    def test_an_absent_credential_store_refuses_every_attempt(self) -> None:
        self.assertFalse(self.store.exists())
        self.assertFalse(self.store.verify("administrator", "anything"))

    def test_a_corrupt_credential_store_refuses_every_attempt(self) -> None:
        self.path.write_text("this is not valid", encoding="utf-8")
        self.assertFalse(self.store.verify("administrator", "anything"))

    def test_no_partial_file_survives_a_save(self) -> None:
        self.store.save("administrator", "a chosen password")
        self.assertEqual(list(self.path.parent.glob("*.partial")), [])


class RequestParsingTests(unittest.TestCase):
    def test_a_well_formed_request_is_parsed(self) -> None:
        request = parse_request_head(
            b"GET /api/state?verbose=yes HTTP/1.1\r\n"
            b"Host: appliance.example\r\n"
            b"Cookie: crossbar_session=abc123\r\n"
        )
        self.assertEqual(request.method, "GET")
        self.assertEqual(request.path, "/api/state")
        self.assertEqual(request.query["verbose"], "yes")
        self.assertEqual(request.header("host"), "appliance.example")
        self.assertEqual(request.cookie("crossbar_session"), "abc123")

    def test_header_names_are_matched_without_regard_to_case(self) -> None:
        request = parse_request_head(b"GET / HTTP/1.1\r\nCONTENT-LENGTH: 42\r\n")
        self.assertEqual(request.content_length, 42)

    def test_a_repeated_header_is_joined_rather_than_dropped(self) -> None:
        request = parse_request_head(
            b"GET / HTTP/1.1\r\nAccept: text/html\r\nAccept: application/json\r\n"
        )
        self.assertEqual(request.header("accept"), "text/html, application/json")

    def test_an_upgrade_request_is_recognised(self) -> None:
        request = parse_request_head(
            b"GET /socket HTTP/1.1\r\nConnection: keep-alive, Upgrade\r\nUpgrade: websocket\r\n"
        )
        self.assertTrue(request.is_upgrade)

    def test_an_ordinary_request_is_not_mistaken_for_an_upgrade(self) -> None:
        request = parse_request_head(b"GET / HTTP/1.1\r\nConnection: keep-alive\r\n")
        self.assertFalse(request.is_upgrade)

    def test_a_malformed_request_line_is_refused(self) -> None:
        for raw in (b"", b"GET\r\n", b"GET / SPDY/3\r\n", b"GET / HTTP/1.1 extra\r\n"):
            with self.subTest(raw=raw):
                with self.assertRaises(MalformedRequest):
                    parse_request_head(raw)

    def test_a_header_line_with_no_separator_is_refused(self) -> None:
        with self.assertRaises(MalformedRequest):
            parse_request_head(b"GET / HTTP/1.1\r\nthis line has no separator\r\n")

    def test_an_oversized_head_is_refused(self) -> None:
        with self.assertRaises(RequestTooLarge):
            parse_request_head(b"GET / HTTP/1.1\r\nX: " + b"a" * 20000 + b"\r\n")

    def test_a_percent_encoded_path_is_decoded(self) -> None:
        request = parse_request_head(b"GET /api/a%20path HTTP/1.1\r\n")
        self.assertEqual(request.path, "/api/a path")

    def test_a_body_that_is_not_valid_encoding_is_refused(self) -> None:
        request = parse_request_head(b"POST /api/session HTTP/1.1\r\n")
        request.body = b"{ not valid json"
        with self.assertRaises(MalformedRequest):
            request.json()

    def test_keep_alive_follows_the_protocol_version_default(self) -> None:
        modern = parse_request_head(b"GET / HTTP/1.1\r\n")
        self.assertTrue(modern.keep_alive)
        closing = parse_request_head(b"GET / HTTP/1.1\r\nConnection: close\r\n")
        self.assertFalse(closing.keep_alive)
        legacy = parse_request_head(b"GET / HTTP/1.0\r\n")
        self.assertFalse(legacy.keep_alive)


class ResponseTests(unittest.TestCase):
    def test_every_response_carries_the_security_headers(self) -> None:
        serialised = Response.json({"ok": True}).serialise().decode("latin-1")
        for header in (
            "X-Content-Type-Options: nosniff",
            "X-Frame-Options: DENY",
            "Content-Security-Policy:",
            "Referrer-Policy: no-referrer",
        ):
            self.assertIn(header, serialised)

    def test_the_content_policy_forbids_remote_sources(self) -> None:
        serialised = Response.json({}).serialise().decode("latin-1")
        self.assertIn("default-src 'self'", serialised)
        self.assertIn("frame-ancestors 'none'", serialised)

    def test_the_declared_length_matches_the_body(self) -> None:
        response = Response.json({"message": "a payload"})
        serialised = response.serialise().decode("latin-1")
        head, _, body = serialised.partition("\r\n\r\n")
        self.assertIn(f"Content-Length: {len(body.encode('utf-8'))}", head)


class StaticFileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory(prefix="crossbar-static-")
        self.root = Path(self.directory.name)
        (self.root / "index.html").write_text("<p>the dashboard</p>", encoding="utf-8")
        (self.root / "js").mkdir()
        (self.root / "js/app.js").write_text("// script", encoding="utf-8")

        # A file outside the served root, which must never be reachable.
        self.secret = self.root.parent / "a-secret-outside-the-root"
        self.secret.write_text("this must never be served", encoding="utf-8")

        self.router = Router()
        self.router.serve_static(self.root)

    def tearDown(self) -> None:
        self.secret.unlink(missing_ok=True)
        self.directory.cleanup()

    def _get(self, path: str) -> Response:
        request = parse_request_head(f"GET {path} HTTP/1.1\r\n".encode("latin-1"))
        return asyncio.run(self.router.dispatch(request))

    def test_the_index_is_served_at_the_root(self) -> None:
        response = self._get("/")
        self.assertEqual(response.status, 200)
        self.assertIn(b"the dashboard", response.body)

    def test_a_nested_file_is_served_with_its_media_type(self) -> None:
        response = self._get("/js/app.js")
        self.assertEqual(response.status, 200)
        self.assertIn("javascript", response.content_type)

    def test_a_traversal_attempt_is_refused(self) -> None:
        for path in (
            "/../a-secret-outside-the-root",
            "/js/../../a-secret-outside-the-root",
            "/./../../etc/passwd",
        ):
            with self.subTest(path=path):
                response = self._get(path)
                self.assertIn(response.status, (403, 404))
                self.assertNotIn(b"this must never be served", response.body)

    def test_an_absent_file_yields_a_plain_language_refusal(self) -> None:
        response = self._get("/not-here.html")
        self.assertEqual(response.status, 404)

    def test_a_wrong_method_is_distinguished_from_a_missing_route(self) -> None:
        self.router.post("/api/session", lambda request: Response.json({}))
        request = parse_request_head(b"GET /api/session HTTP/1.1\r\n")
        response = asyncio.run(self.router.dispatch(request))
        self.assertEqual(response.status, 405)


class TaskSchedulerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.published: list[tuple[str, dict]] = []
        self.scheduler = TaskScheduler(
            publisher=lambda topic, payload: self.published.append((topic, payload)),
            worker_pool_size=2,
            default_timeout_seconds=1.0,
        )

    async def asyncTearDown(self) -> None:
        await self.scheduler.stop()

    async def test_a_task_runs_and_reports_its_result(self) -> None:
        self.scheduler.register("probe", lambda: {"value": "produced"})
        run = await self.scheduler.run("probe")

        self.assertEqual(run.status, STATUS_SUCCEEDED)
        self.assertTrue(run.succeeded)
        self.assertEqual(run.result, {"value": "produced"})

    async def test_an_asynchronous_task_is_awaited(self) -> None:
        async def handler() -> str:
            await asyncio.sleep(0.01)
            return "produced asynchronously"

        self.scheduler.register("asynchronous", handler)
        run = await self.scheduler.run("asynchronous")
        self.assertEqual(run.result, "produced asynchronously")

    async def test_a_blocking_task_runs_on_the_worker_pool(self) -> None:
        def handler() -> str:
            time.sleep(0.05)
            return "produced on a worker"

        self.scheduler.register("blocking", handler, blocking=True)

        # The event loop must remain responsive while the worker is busy.
        loop = asyncio.get_running_loop()
        started = loop.time()
        ticks = 0

        async def tick() -> None:
            nonlocal ticks
            while loop.time() - started < 0.05:
                ticks += 1
                await asyncio.sleep(0.005)

        ticker = asyncio.create_task(tick())
        run = await self.scheduler.run("blocking")
        await ticker

        self.assertEqual(run.result, "produced on a worker")
        self.assertGreater(ticks, 3, "the event loop was blocked by the worker")

    async def test_a_failing_task_is_recorded_rather_than_raising(self) -> None:
        def handler() -> None:
            raise RuntimeError("the task could not complete")

        self.scheduler.register("failing", handler)
        run = await self.scheduler.run("failing")

        self.assertEqual(run.status, STATUS_FAILED)
        self.assertIn("could not complete", run.detail)

    async def test_a_wedged_task_is_abandoned_at_its_timeout(self) -> None:
        async def handler() -> None:
            await asyncio.sleep(30)

        self.scheduler.register("wedged", handler, timeout_seconds=0.05)
        run = await self.scheduler.run("wedged")
        self.assertEqual(run.status, STATUS_TIMED_OUT)

    async def test_a_task_cannot_be_stacked_on_itself(self) -> None:
        release = asyncio.Event()

        async def handler() -> str:
            await release.wait()
            return "released"

        self.scheduler.register("single-flight", handler, timeout_seconds=5.0)
        first = asyncio.create_task(self.scheduler.run("single-flight"))
        await asyncio.sleep(0.02)

        second = await self.scheduler.run("single-flight")
        self.assertEqual(second.status, STATUS_REJECTED)

        release.set()
        outcome = await first
        self.assertEqual(outcome.status, STATUS_SUCCEEDED)

    async def test_an_unknown_task_is_refused(self) -> None:
        with self.assertRaises(TaskError):
            await self.scheduler.run("a task that does not exist")

    async def test_a_duplicate_registration_is_refused(self) -> None:
        self.scheduler.register("probe", lambda: None)
        with self.assertRaises(TaskError):
            self.scheduler.register("probe", lambda: None)

    async def test_the_start_and_finish_of_a_task_are_published(self) -> None:
        self.scheduler.register("probe", lambda: "done")
        await self.scheduler.run("probe")
        topics = [topic for topic, _ in self.published]
        self.assertIn("task.started", topics)
        self.assertIn("task.finished", topics)

    async def test_the_history_stays_bounded(self) -> None:
        scheduler = TaskScheduler(history_limit=5)
        scheduler.register("probe", lambda: None)
        for _ in range(20):
            await scheduler.run("probe")
        self.assertLessEqual(len(scheduler.history(limit=100)), 5)
        await scheduler.stop()

    async def test_a_scheduled_task_fires_on_its_interval(self) -> None:
        calls = 0

        def handler() -> None:
            nonlocal calls
            calls += 1

        self.scheduler.register("periodic", handler, interval_seconds=0.05)
        started = self.scheduler.start()
        self.assertEqual(started, 1)

        await asyncio.sleep(0.28)
        await self.scheduler.stop()
        self.assertGreaterEqual(calls, 2, "the scheduled task did not fire repeatedly")

    async def test_the_snapshot_reports_every_registered_task(self) -> None:
        self.scheduler.register("one", lambda: None, "the first task")
        self.scheduler.register("two", lambda: None, "the second task")
        snapshot = self.scheduler.snapshot()
        self.assertEqual([task["name"] for task in snapshot["tasks"]], ["one", "two"])


if __name__ == "__main__":
    unittest.main()
