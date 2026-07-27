"""Who changed what, and from where.

The appliance recorded that a trunk had been disabled. It did not record who
disabled it, or from where, so an operator finding it down on a Monday could
not tell a colleague's Friday change from somebody else's, and an operator
accused of the change had nothing to point at. The record below serves both of
them, which is why it names the account and the address rather than only the
change.

Two things are being pinned here, and the second one is the reason the record
is made where it is. The first is that the journal writes, reads back, rolls
over and survives a bad line. The second is that the recording happens in the
guard every write route already passes through, rather than inside the
handlers: a record each handler has to remember to make is
complete on the day it is written and incomplete the following year. The test
that matters most in this file is therefore the one that walks the router and
asserts that no write route is silent.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from appliance.audit import AuditJournal
from support import TEST_USERNAME, ApplianceHarness


class JournalFileTests(unittest.TestCase):
    """The file itself."""

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory(prefix="myipbx-journal-")
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "operations.jsonl"
        self.journal = AuditJournal(self.path)

    def test_an_entry_is_written_and_read_back(self) -> None:
        self.journal.record(
            actor="administrator",
            source="192.0.2.5",
            action="DELETE",
            target="/api/entities/trunks/carrier-primary",
            outcome="accepted",
        )
        entries = self.journal.recent()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["actor"], "administrator")
        self.assertEqual(entries[0]["source"], "192.0.2.5")
        self.assertEqual(entries[0]["target"], "/api/entities/trunks/carrier-primary")

    def test_the_newest_entry_is_first(self) -> None:
        for index in ("one", "two", "three"):
            self.journal.record("administrator", "192.0.2.5", "POST", index, "accepted")
        entries = self.journal.recent()
        self.assertEqual([entry["target"] for entry in entries], ["three", "two", "one"])

    def test_the_file_is_readable_only_by_its_owner(self) -> None:
        """It names accounts and the addresses they worked from."""
        self.journal.record("administrator", "192.0.2.5", "POST", "a", "accepted")
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_a_line_that_is_not_readable_does_not_hide_the_rest(self) -> None:
        """A truncated write during a power cut must not blind the record."""
        self.journal.record("administrator", "192.0.2.5", "POST", "before", "accepted")
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write("{this is not json\n")
        self.journal.record("administrator", "192.0.2.5", "POST", "after", "accepted")

        targets = [entry["target"] for entry in self.journal.recent()]
        self.assertIn("before", targets)
        self.assertIn("after", targets)

    def test_the_journal_rolls_over_rather_than_filling_the_partition(self) -> None:
        """The state partition also holds voicemail."""
        self.path.write_text("x" * (3 * 1024 * 1024), encoding="utf-8")
        self.journal.record("administrator", "192.0.2.5", "POST", "after-roll", "accepted")

        self.assertTrue(self.journal.previous_path.is_file())
        self.assertLess(self.path.stat().st_size, 1024)
        self.assertEqual(self.journal.recent()[0]["target"], "after-roll")

    def test_a_journal_that_cannot_be_written_does_not_stop_the_operation(self) -> None:
        """A record that could refuse a change would be a way of stopping the
        appliance being administered by filling a disk."""
        unwritable = AuditJournal(Path("/proc/nowhere/operations.jsonl"))
        unwritable.record("administrator", "192.0.2.5", "POST", "a", "accepted")
        self.assertEqual(unwritable.recent(), [])

    def test_nothing_is_recorded_from_a_request_body(self) -> None:
        """The bodies passing through the write routes carry telephone
        passwords, carrier credentials and private keys."""
        self.journal.record(
            actor="administrator",
            source="192.0.2.5",
            action="POST",
            target="/api/entities/extensions",
            outcome="accepted",
        )
        raw = self.path.read_text(encoding="utf-8")
        recorded = json.loads(raw.splitlines()[0])
        self.assertEqual(
            set(recorded),
            {"at", "actor", "source", "action", "target", "outcome", "detail"},
            "the journal line carries a field that could hold a secret",
        )


class RecordedThroughTheGuardTests(unittest.IsolatedAsyncioTestCase):
    """The record is made where it cannot be forgotten."""

    async def asyncSetUp(self) -> None:
        self.harness = ApplianceHarness()
        self.appliance = await self.harness.start()

    async def asyncTearDown(self) -> None:
        await self.harness.stop()

    async def test_a_change_names_the_account_and_the_address(self) -> None:
        await self.harness.sign_in()
        await self.harness.request(
            "POST",
            "/api/entities/extensions",
            body=json.dumps({
                "number": "241", "name": "Reception", "technology": "PJSIP",
                "voicemail": True, "ring_seconds": 20, "enabled": True,
            }),
        )
        entries = self.appliance.journal.recent()
        changes = [
            entry for entry in entries
            if entry["target"] == "/api/entities/extensions"
        ]
        self.assertTrue(changes, f"the change was not recorded: {entries}")
        self.assertEqual(changes[0]["actor"], TEST_USERNAME)
        self.assertTrue(changes[0]["source"], "no address was recorded")
        self.assertEqual(changes[0]["action"], "POST")
        self.assertEqual(changes[0]["outcome"], "accepted")

    async def test_a_refused_change_is_recorded_as_refused(self) -> None:
        await self.harness.sign_in()
        await self.harness.request(
            "POST",
            "/api/entities/extensions",
            body=json.dumps({"number": "not-a-number", "name": "Reception"}),
        )
        entries = self.appliance.journal.recent()
        changes = [
            entry for entry in entries
            if entry["target"] == "/api/entities/extensions"
        ]
        self.assertTrue(changes)
        self.assertIn("refused", changes[0]["outcome"])

    async def test_a_refused_sign_in_names_the_account_that_was_tried(self) -> None:
        """The first entry an operator investigating an intrusion looks for."""
        await self.harness.request(
            "POST", "/api/session",
            body=json.dumps({"username": "intruder", "password": "a-guess"}),
        )
        entries = self.appliance.journal.recent()
        attempts = [entry for entry in entries if entry["target"] == "/api/session"]
        self.assertTrue(attempts, "a refused sign in was not recorded")
        self.assertEqual(attempts[0]["actor"], "intruder")
        self.assertIn("refused", attempts[0]["outcome"])

    async def test_the_password_that_was_tried_is_never_recorded(self) -> None:
        await self.harness.request(
            "POST", "/api/session",
            body=json.dumps({"username": "intruder", "password": "a-very-distinctive-guess"}),
        )
        raw = self.appliance.config.journal_path.read_text(encoding="utf-8")
        self.assertNotIn("a-very-distinctive-guess", raw)

    async def test_producing_a_backup_is_recorded_even_though_it_changes_nothing(self) -> None:
        """A read that emits the whole site is worth an entry.

        The guard records writes, because a write changes the machine. These
        two change nothing and are recorded anyway: they put the entire
        configuration -- and, when asked, every password on the appliance --
        into a file that somebody then sends somewhere. That an archive was
        produced, by whom and from where, is exactly the entry wanted
        afterwards, and no write route would ever have carried it.
        """
        await self.harness.sign_in()
        await self.harness.request("GET", "/api/backup?include_secrets=yes")

        entries = self.appliance.journal.recent()
        exports = [entry for entry in entries if entry["target"] == "/api/backup"]
        self.assertTrue(exports, f"a backup left no trace: {entries}")
        self.assertEqual(exports[0]["action"], "EXPORT")
        self.assertEqual(exports[0]["actor"], TEST_USERNAME)
        self.assertIn("every password in the clear", exports[0]["outcome"])

    async def test_a_backup_without_secrets_is_recorded_as_such(self) -> None:
        await self.harness.sign_in()
        await self.harness.request("GET", "/api/backup?include_secrets=no")
        exports = [
            entry for entry in self.appliance.journal.recent()
            if entry["target"] == "/api/backup"
        ]
        self.assertTrue(exports)
        self.assertIn("no password", exports[0]["outcome"])

    async def test_producing_a_support_bundle_is_recorded(self) -> None:
        await self.harness.sign_in()
        await self.harness.request("GET", "/api/support-bundle")
        exports = [
            entry for entry in self.appliance.journal.recent()
            if entry["target"] == "/api/support-bundle"
        ]
        self.assertTrue(exports, "a support bundle left no trace")
        self.assertEqual(exports[0]["action"], "EXPORT")

    async def test_an_ordinary_read_is_not_recorded(self) -> None:
        """The journal is a record of consequence, not a request log.

        An entry for every dashboard poll would bury the entries that matter
        under thousands that do not, and the console polls.
        """
        await self.harness.sign_in()
        for _ in range(3):
            await self.harness.request("GET", "/api/state")
        entries = self.appliance.journal.recent()
        self.assertEqual(
            [entry for entry in entries if entry["target"] == "/api/state"], []
        )

    async def test_no_write_route_is_silent(self) -> None:
        """The reason the recording lives in the guard rather than in handlers.

        Every write route is wrapped by one function. This walks the router and
        asserts that each of them is that function, so a route added next year
        is recorded whether or not anybody remembered to record it.
        """
        router = self.appliance.router
        write_methods = {"POST", "PUT", "PATCH", "DELETE"}

        # Signing in is the one write that cannot use the recording guard,
        # because the guard runs before there is a session and so cannot know
        # which account was tried -- which is the whole value of the entry. Its
        # handler records both outcomes itself, and the two tests above prove
        # it. Every other write route must go through the guard.
        RECORDED_BY_ITS_OWN_HANDLER = {("POST", "/api/session")}

        found = 0
        unguarded = []
        for method, path, handler in _routes(router):
            if method not in write_methods or not path.startswith("/api/"):
                continue
            found += 1
            if (method, path) in RECORDED_BY_ITS_OWN_HANDLER:
                continue
            closure = getattr(handler, "__qualname__", "")
            if "_Guard.write." not in closure:
                unguarded.append(f"{method} {path}")

        self.assertGreater(
            found, 5,
            "no write routes were found, so this test proved nothing",
        )
        self.assertEqual(
            unguarded, [],
            "these write routes do not pass through the guard that records "
            "them, so a change made through them leaves no trace of who made it",
        )


def _routes(router: object) -> list[tuple[str, str, object]]:
    """Every registered route: the exact ones and the parameterised ones."""
    found: list[tuple[str, str, object]] = []
    for (method, path), handler in getattr(router, "_routes", {}).items():
        found.append((str(method).upper(), str(path), handler))
    for method, segments, handler in getattr(router, "_patterns", []):
        found.append((str(method).upper(), "/" + "/".join(segments), handler))
    return found


if __name__ == "__main__":
    unittest.main()
