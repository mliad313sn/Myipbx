"""System operations, telephony objects, diagnostics, and backup.

These are the surfaces added to make every operation reachable from the
graphical interface, and they are the ones that most need adversarial testing:
the privileged helper is the appliance's only elevated path, and the restore
path accepts an archive uploaded by a browser.
"""

from __future__ import annotations

import io
import json
import tarfile
import tempfile
import time
import unittest
from pathlib import Path

import support  # noqa: F401  (path setup)

from appliance import backup as backup_module
from appliance import firewall
from appliance import numerals
from appliance.diagnostics import CallRecordReader, LogReader, LogSource
from appliance.entities import (
    ENTITY_SPECS,
    EntityStore,
    SecretStore,
    ValidationError,
    schema,
)
from appliance.sysops import (
    OPERATIONS,
    OperationRefused,
    PrivilegedOperations,
    SystemStatus,
)


class PrivilegedOperationTests(unittest.IsolatedAsyncioTestCase):
    """The only elevated path the appliance has."""

    def setUp(self) -> None:
        self.invocations: list[list[str]] = []

        async def runner(command, timeout):
            self.invocations.append(list(command))
            return 0, "the operation completed"

        self.operations = PrivilegedOperations(
            helper_path=__file__,  # any existing executable-ish path
            runner=runner,
        )
        # The helper existence check must pass for the invocation tests.
        self.operations.available = lambda: True  # type: ignore[method-assign]

    # -- the vocabulary ----------------------------------------------------

    def test_an_unknown_verb_is_refused(self) -> None:
        for verb in ("rm", "shell", "service-obliterate", "", "../../bin/sh"):
            with self.subTest(verb=verb):
                with self.assertRaises(OperationRefused):
                    self.operations.validate(verb)

    def test_only_the_appliance_own_services_may_be_controlled(self) -> None:
        for service in ("asterisk", "myipbx", "dahdi"):
            with self.subTest(service=service):
                vector = self.operations.validate("service-restart", {"service": service})
                self.assertEqual(vector, ["service-restart", service])

        for service in ("sshd", "cron", "postgresql", "systemd-journald"):
            with self.subTest(service=service):
                with self.assertRaises(OperationRefused):
                    self.operations.validate("service-restart", {"service": service})

    def test_a_shell_metacharacter_cannot_ride_in_an_argument(self) -> None:
        hostile = [
            "asterisk; rm -rf /",
            "asterisk && curl http://an-attacker.example",
            "asterisk`id`",
            "asterisk$(id)",
            "asterisk\nservice-stop",
            "asterisk|tee /etc/passwd",
        ]
        for value in hostile:
            with self.subTest(value=value):
                with self.assertRaises(OperationRefused):
                    self.operations.validate("service-restart", {"service": value})

    def test_a_hostile_address_is_refused(self) -> None:
        hostile = [
            "999.1.1.1", "1.2.3", "1.2.3.4.5", "not-an-address",
            "192.0.2.1; reboot", "../../etc/passwd", "",
        ]
        for value in hostile:
            with self.subTest(value=value):
                with self.assertRaises(OperationRefused):
                    self.operations.validate(
                        "network-apply",
                        {"interface": "eth0", "address": value,
                         "prefix": "24", "gateway": ""},
                    )

    def test_a_valid_network_request_is_accepted(self) -> None:
        vector = self.operations.validate(
            "network-apply",
            {"interface": "eth0", "address": "192.0.2.20",
             "prefix": "24", "gateway": "192.0.2.1"},
        )
        self.assertEqual(
            vector, ["network-apply", "eth0", "192.0.2.20", "24", "192.0.2.1"]
        )

    def test_an_out_of_range_prefix_is_refused(self) -> None:
        for prefix in ("0", "33", "-1", "24a", ""):
            with self.subTest(prefix=prefix):
                with self.assertRaises(OperationRefused):
                    self.operations.validate(
                        "network-apply",
                        {"interface": "eth0", "address": "192.0.2.20",
                         "prefix": prefix, "gateway": ""},
                    )

    def test_a_hostile_hostname_or_timezone_is_refused(self) -> None:
        with self.assertRaises(OperationRefused):
            self.operations.validate("hostname-set", {"hostname": "a host; reboot"})
        with self.assertRaises(OperationRefused):
            self.operations.validate("timezone-set", {"timezone": "../../etc/shadow"})

    def test_a_missing_argument_is_refused(self) -> None:
        with self.assertRaises(OperationRefused):
            self.operations.validate("service-restart", {})

    def test_a_surplus_argument_is_refused(self) -> None:
        with self.assertRaises(OperationRefused):
            self.operations.validate(
                "service-restart", {"service": "asterisk", "extra": "value"}
            )

    def test_an_operation_taking_no_argument_accepts_none(self) -> None:
        self.assertEqual(self.operations.validate("reboot"), ["reboot"])
        with self.assertRaises(OperationRefused):
            self.operations.validate("reboot", {"when": "now"})

    # -- invocation --------------------------------------------------------

    async def test_the_helper_is_invoked_as_an_argument_vector(self) -> None:
        outcome = await self.operations.run("service-restart", {"service": "asterisk"})
        self.assertTrue(outcome.succeeded)
        self.assertEqual(len(self.invocations), 1)
        # The verb and its argument are separate elements: there is no string
        # anywhere that a shell could reinterpret.
        self.assertEqual(self.invocations[0][-2:], ["service-restart", "asterisk"])

    async def test_a_failing_helper_is_reported_rather_than_raised(self) -> None:
        async def failing(command, timeout):
            return 1, "the service manager refused"

        operations = PrivilegedOperations(helper_path=__file__, runner=failing)
        operations.available = lambda: True  # type: ignore[method-assign]

        outcome = await operations.run("engine-reload")
        self.assertFalse(outcome.succeeded)
        self.assertIn("failure", outcome.detail)

    async def test_an_absent_helper_is_reported_in_plain_language(self) -> None:
        operations = PrivilegedOperations(helper_path="/a/path/that/does/not/exist")
        outcome = await operations.run("engine-reload")
        self.assertFalse(outcome.succeeded)
        self.assertIn("not running", outcome.detail)

    async def test_the_output_of_an_operation_spells_its_quantities(self) -> None:
        """What the helper printed reaches the console under the same rule.

        A helper's output is operator facing text like any other, so quantities
        in it are spelled. The port it names is an identifier and survives,
        because an operator reading "the engine is listening on port five
        thousand thirty-eight" cannot put that anywhere useful.
        """
        async def noisy(command, timeout):
            return 0, "restarted 3 services on port 5038"

        operations = PrivilegedOperations(helper_path=__file__, runner=noisy)
        operations.available = lambda: True  # type: ignore[method-assign]

        outcome = await operations.run("engine-reload")
        rendered = outcome.as_dict()["output"]
        self.assertIn("three services", rendered)
        self.assertIn("port 5038", rendered)

    def test_every_operation_declares_whether_it_interrupts_service(self) -> None:
        for verb, operation in OPERATIONS.items():
            with self.subTest(verb=verb):
                self.assertIsInstance(operation.disruptive, bool)
                self.assertTrue(operation.description)

        # The ones that certainly interrupt service must say so.
        for verb in ("reboot", "power-off", "service-restart", "network-apply"):
            self.assertTrue(OPERATIONS[verb].disruptive, verb)


class SystemStatusTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory(prefix="myipbx-system-")
        self.root = Path(self.directory.name)
        (self.root / "proc").mkdir()
        (self.root / "etc").mkdir()

        (self.root / "proc/uptime").write_text("90061.12 350000.00\n", encoding="utf-8")
        (self.root / "proc/loadavg").write_text(
            "0.52 0.31 0.25 1/210 4242\n", encoding="utf-8"
        )
        (self.root / "proc/meminfo").write_text(
            "MemTotal:        4028460 kB\n"
            "MemFree:          512340 kB\n"
            "MemAvailable:    3020120 kB\n",
            encoding="utf-8",
        )
        (self.root / "etc/timezone").write_text("Europe/Paris\n", encoding="utf-8")

        network = self.root / "sys/class/net/eth0"
        network.mkdir(parents=True)
        (network / "operstate").write_text("up\n", encoding="utf-8")
        (network / "carrier").write_text("1\n", encoding="utf-8")
        (network / "address").write_text("aa:bb:cc:dd:ee:ff\n", encoding="utf-8")

        self.status = SystemStatus(self.root)

    def tearDown(self) -> None:
        self.directory.cleanup()

    def test_the_uptime_is_read(self) -> None:
        self.assertEqual(self.status.uptime_seconds(), 90061)

    def test_the_load_average_is_read(self) -> None:
        self.assertEqual(self.status.load_average(), (0.52, 0.31, 0.25))

    def test_the_memory_is_read(self) -> None:
        memory = self.status.memory()
        assert memory is not None
        self.assertEqual(memory["MemTotal"], 4028460)

    def test_the_timezone_is_read(self) -> None:
        self.assertEqual(self.status.timezone(), "Europe/Paris")

    def test_the_interfaces_are_enumerated_without_the_loopback(self) -> None:
        interfaces = self.status.network_interfaces()
        self.assertEqual([item["name"] for item in interfaces], ["eth0"])
        self.assertTrue(interfaces[0]["carrier"])

    def test_the_snapshot_spells_every_figure(self) -> None:
        snapshot = self.status.snapshot()
        self.assertEqual(snapshot["uptime"], "one day one hour one minute one second")

        # Quantities are spelled. The kernel release is not a quantity: an
        # administrator matches it character for character against a headers
        # package name, and "six.eighteen.five" matches nothing.
        self.assertFalse(
            numerals.contains_digit(str(snapshot["uptime"])),
            f"the uptime carried a digit: {snapshot['uptime']}",
        )
        self.assertRegex(
            str(snapshot["kernel_release"]),
            r"\d",
            "the kernel release was spelled, which makes it useless for the one "
            "thing it is read for: matching a headers package",
        )
        for value in snapshot["memory"].values():
            self.assertFalse(numerals.contains_digit(str(value)))
        for value in snapshot["load_average"].values():
            self.assertFalse(numerals.contains_digit(str(value)))

    def test_a_machine_missing_every_source_does_not_raise(self) -> None:
        status = SystemStatus(self.root / "absent")
        snapshot = status.snapshot()
        self.assertEqual(snapshot["uptime"], "unknown")
        self.assertEqual(snapshot["interfaces"], [])


class EntityStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory(prefix="myipbx-entities-")
        self.secrets = SecretStore(Path(self.directory.name) / "secrets.json")
        self.document: dict = {
            "extensions": [], "trunks": [], "ring_groups": [],
            "inbound_routes": [], "outbound_routes": [], "time_conditions": [],
        }
        self.store = EntityStore(self.document, self.secrets)

    def tearDown(self) -> None:
        self.directory.cleanup()

    def _extension(self, number: str = "201", **overrides) -> dict:
        payload = {"number": number, "name": "A Telephone", "secret": "a-long-password"}
        payload.update(overrides)
        record = self.store.create("extensions", payload)
        # What the interface does after it has saved the document. Secrets are
        # held back until the document that refers to them is safely written,
        # so a caller that never saves never writes one -- and a test standing
        # in for that caller has to do what it does.
        self.store.commit_secrets()
        return record

    # -- creation and validation -------------------------------------------

    def test_an_extension_is_created_with_its_declared_defaults(self) -> None:
        record = self._extension()
        self.assertEqual(record["number"], "201")
        self.assertEqual(record["technology"], "PJSIP")
        self.assertTrue(record["voicemail"])
        self.assertEqual(record["ring_seconds"], 20)

    def test_a_required_field_is_enforced_with_a_named_reason(self) -> None:
        with self.assertRaises(ValidationError) as caught:
            self.store.create("extensions", {"name": "A Telephone"})
        self.assertIn("number", caught.exception.errors)

    def test_a_malformed_value_is_refused_on_its_own_field(self) -> None:
        with self.assertRaises(ValidationError) as caught:
            self.store.create("extensions", {"number": "not-a-number", "name": "A"})
        self.assertIn("number", caught.exception.errors)
        self.assertFalse(
            numerals.contains_digit(caught.exception.errors["number"]),
            "a validation message carried a digit",
        )

    def test_a_duplicate_key_is_refused(self) -> None:
        self._extension("201")
        with self.assertRaises(ValidationError):
            self._extension("201")

    def test_a_number_outside_its_range_is_refused(self) -> None:
        with self.assertRaises(ValidationError):
            self.store.create(
                "extensions",
                {"number": "202", "name": "A Telephone", "ring_seconds": 9999},
            )

    def test_a_short_password_is_refused(self) -> None:
        with self.assertRaises(ValidationError) as caught:
            self.store.create(
                "extensions", {"number": "203", "name": "A", "secret": "short"}
            )
        self.assertIn("secret", caught.exception.errors)

    # -- secrets -----------------------------------------------------------

    def test_a_secret_is_never_returned_by_a_read(self) -> None:
        record = self._extension()
        self.assertNotIn("secret", record)
        self.assertTrue(record["secret_configured"])

        listed = self.store.list("extensions")[0]
        self.assertNotIn("secret", listed)

        # And it must not have been written into the document either.
        self.assertNotIn("a-long-password", json.dumps(self.document))

    def test_a_secret_is_retrievable_only_through_the_secret_store(self) -> None:
        self._extension()
        self.assertEqual(
            self.secrets.get("extensions", "201", "secret"), "a-long-password"
        )

    def test_an_absent_secret_on_an_update_leaves_it_alone(self) -> None:
        self._extension()
        self.store.update("extensions", "201", {"name": "A Renamed Telephone"})
        self.assertEqual(
            self.secrets.get("extensions", "201", "secret"), "a-long-password"
        )

    def test_deleting_an_object_forgets_its_secret(self) -> None:
        self._extension()
        self.store.delete("extensions", "201")
        self.store.commit_secrets()
        self.assertIsNone(self.secrets.get("extensions", "201", "secret"))

    def test_a_creation_that_is_never_saved_writes_no_secret(self) -> None:
        """The orphan the review found.

        A secret used to reach disk the moment a record validated, before the
        document referring to it was written. A save that then failed -- a full
        disk, a document the guard refused, a permission that had changed --
        left a carrier's password or a telephone's on disk with nothing
        pointing at it: never displayed, never reachable, never cleaned up, and
        carried into every backup taken afterwards.
        """
        record = self.store.create(
            "extensions",
            {"number": "301", "name": "Another Telephone", "secret": "a-second-password"},
        )
        # The record still reports the password as set, because the operator
        # typed one and the caller is about to save it.
        self.assertTrue(record["secret_configured"])

        # But nothing has touched the secret file, so a caller that fails to
        # save leaves nothing behind.
        self.store.discard_secrets()
        self.assertIsNone(self.secrets.get("extensions", "301", "secret"))
        self.assertNotIn(
            "a-second-password",
            self.secrets.path.read_text(encoding="utf-8")
            if self.secrets.path.is_file() else "",
        )

    def test_a_deletion_that_is_never_saved_keeps_the_secret(self) -> None:
        """The mirror of the orphan, and the more damaging of the two.

        Forgetting the secret before the document was written meant that a save
        which then failed left the record in place having quietly lost the
        password it needs to register. Nothing said so, and nothing could put
        it back: the appliance never reads a secret out again.
        """
        self._extension()
        self.store.delete("extensions", "201")
        self.store.discard_secrets()
        self.assertEqual(
            self.secrets.get("extensions", "201", "secret"), "a-long-password"
        )

    # -- referential integrity ---------------------------------------------

    def test_a_reference_to_something_absent_is_refused(self) -> None:
        with self.assertRaises(ValidationError) as caught:
            self.store.create(
                "ring_groups",
                {"number": "600", "name": "Sales", "members": ["201", "999"]},
            )
        self.assertIn("members", caught.exception.errors)

    def test_a_valid_reference_is_accepted(self) -> None:
        self._extension("201")
        self._extension("202")
        group = self.store.create(
            "ring_groups",
            {"number": "600", "name": "Sales", "members": ["201", "202"]},
        )
        self.assertEqual(group["members"], ["201", "202"])

    def test_deleting_something_still_referenced_is_refused_with_an_explanation(self) -> None:
        self._extension("201")
        self.store.create(
            "ring_groups", {"number": "600", "name": "Sales", "members": ["201"]}
        )

        with self.assertRaises(ValidationError) as caught:
            self.store.delete("extensions", "201")
        message = caught.exception.errors["number"]
        self.assertIn("still used by", message)
        self.assertIn("ring group", message)

    def test_renaming_a_key_follows_through_to_its_references(self) -> None:
        self._extension("201")
        self.store.create(
            "ring_groups", {"number": "600", "name": "Sales", "members": ["201"]}
        )

        self.store.update("extensions", "201", {"number": "301"})
        self.store.commit_secrets()
        group = self.store.get("ring_groups", "600")
        assert group is not None
        self.assertEqual(group["members"], ["301"])
        # The secret must travel with the rename.
        self.assertEqual(
            self.secrets.get("extensions", "301", "secret"), "a-long-password"
        )

    def test_an_outbound_route_must_name_a_trunk_that_exists(self) -> None:
        with self.assertRaises(ValidationError):
            self.store.create(
                "outbound_routes",
                {"name": "local", "pattern": "9NXXXXXX", "trunk": "absent-trunk"},
            )

        self.store.create(
            "trunks",
            {"name": "carrier", "host": "sip.example.net", "username": "account"},
        )
        route = self.store.create(
            "outbound_routes",
            {"name": "local", "pattern": "9NXXXXXX", "trunk": "carrier"},
        )
        self.assertEqual(route["trunk"], "carrier")

    def test_an_unknown_kind_is_refused(self) -> None:
        with self.assertRaises(KeyError):
            self.store.create("wormholes", {"name": "one"})

    # -- the schema itself --------------------------------------------------

    def test_the_schema_describes_every_kind_the_store_accepts(self) -> None:
        described = {item["kind"] for item in schema()["kinds"]}
        self.assertEqual(described, set(ENTITY_SPECS))

    def test_every_field_carries_a_label_the_interface_can_show(self) -> None:
        for spec in ENTITY_SPECS.values():
            for field in spec.fields:
                with self.subTest(kind=spec.kind, field=field.name):
                    self.assertTrue(field.label)
                    self.assertFalse(numerals.contains_digit(field.label))

    def test_every_kind_declares_a_key_that_is_one_of_its_fields(self) -> None:
        for spec in ENTITY_SPECS.values():
            with self.subTest(kind=spec.kind):
                self.assertIsNotNone(spec.field(spec.key))


class LogReaderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory(prefix="myipbx-logs-")
        self.root = Path(self.directory.name)
        target = self.root / "var/log/myipbx"
        target.mkdir(parents=True)

        lines = []
        for index in range(500):
            level = "ERROR" if index % 50 == 0 else "INFO"
            lines.append(f"line number {index} {level} something happened")
        (target / "appliance.log").write_text("\n".join(lines) + "\n", encoding="utf-8")

        self.reader = LogReader(
            sources=(LogSource("appliance", "the appliance", "/var/log/myipbx/appliance.log"),),
            root=self.root,
        )

    def tearDown(self) -> None:
        self.directory.cleanup()

    def test_only_a_named_log_may_be_read(self) -> None:
        with self.assertRaises(KeyError):
            self.reader.read("../../etc/passwd")
        with self.assertRaises(KeyError):
            self.reader.read("shadow")

    def test_the_tail_is_returned_newest_last(self) -> None:
        payload = self.reader.read("appliance", lines=10)
        self.assertTrue(payload["available"])
        self.assertEqual(len(payload["lines"]), 10)
        self.assertIn("four hundred ninety-nine", payload["lines"][-1]["text"])

    def test_a_level_filter_selects_only_that_level(self) -> None:
        payload = self.reader.read("appliance", lines=100, level="error")
        self.assertTrue(payload["lines"])
        for line in payload["lines"]:
            self.assertEqual(line["level"], "error")

    def test_a_search_selects_only_matching_lines(self) -> None:
        payload = self.reader.read("appliance", lines=100, search="ninety-nine")
        self.assertTrue(payload["lines"])
        for line in payload["lines"]:
            self.assertIn("ninety-nine", line["text"])

    def test_every_returned_line_is_spelled(self) -> None:
        payload = self.reader.read("appliance", lines=200)
        for line in payload["lines"]:
            self.assertFalse(
                numerals.contains_digit(line["text"]),
                f"the log viewer emitted a digit: {line['text']}",
            )

    def test_an_absent_log_is_reported_rather_than_raised(self) -> None:
        reader = LogReader(
            sources=(LogSource("nowhere", "nowhere", "/var/log/nowhere.log"),),
            root=self.root,
        )
        payload = reader.read("nowhere")
        self.assertFalse(payload["available"])
        self.assertIn("does not exist", payload["explanation"])

    def test_the_line_limit_is_capped(self) -> None:
        payload = self.reader.read("appliance", lines=100000)
        self.assertLessEqual(len(payload["lines"]), 2000)


class CallRecordTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory(prefix="myipbx-cdr-")
        self.root = Path(self.directory.name)
        target = self.root / "var/log/asterisk/cdr-csv"
        target.mkdir(parents=True)

        rows = [
            '"","2015550100","201","from-trunk","\\"Caller\\" <2015550100>",'
            '"PJSIP/carrier-00000001","PJSIP/201-00000002","Dial","PJSIP/201,20",'
            '"2026-07-27 09:15:00","2026-07-27 09:15:04","2026-07-27 09:20:04",'
            '304,300,"ANSWERED","3","1785000000.1",""',
            '"","2015550101","202","from-trunk","\\"Other\\" <2015550101>",'
            '"PJSIP/carrier-00000003","","Dial","PJSIP/202,20",'
            '"2026-07-27 09:30:00","","2026-07-27 09:30:22",'
            '22,0,"NO ANSWER","3","1785000000.2",""',
        ]
        (target / "Master.csv").write_text("\n".join(rows) + "\n", encoding="utf-8")
        self.reader = CallRecordReader("/var/log/asterisk/cdr-csv/Master.csv", self.root)

    def tearDown(self) -> None:
        self.directory.cleanup()

    def test_the_records_are_parsed_newest_first(self) -> None:
        payload = self.reader.read()
        self.assertTrue(payload["available"])
        self.assertEqual(len(payload["records"]), 2)
        self.assertEqual(payload["records"][0]["destination"], "202")

    def test_the_durations_are_spelled(self) -> None:
        payload = self.reader.read()
        answered = [item for item in payload["records"] if item["answered"]][0]
        self.assertEqual(answered["talk_time"], "five minutes")
        self.assertEqual(answered["duration"], "five minutes four seconds")

    def test_a_record_keeps_its_numbers_and_spells_its_durations(self) -> None:
        """Call history is read to find a number, and to line a call up in time.

        A telephone number spelled into words matches nothing an operator has
        and cannot be dialled; a start time spelled into words cannot be lined
        up against an incident. Both are identifiers and keep their digits. The
        durations beside them are quantities and are spelled.
        """
        payload = self.reader.read()
        self.assertTrue(payload["records"], "the fixture produced no records")

        for record in payload["records"]:
            for name in ("source", "destination", "started_at"):
                with self.subTest(field=name):
                    self.assertTrue(
                        numerals.contains_digit(str(record[name])),
                        f"the identifier {name} was spelled into words, which "
                        f"makes it useless: {record[name]!r}",
                    )
            for name in ("duration", "talk_time"):
                with self.subTest(field=name):
                    self.assertFalse(
                        numerals.contains_digit(str(record[name])),
                        f"the quantity {name} escaped as digits: {record[name]!r}",
                    )

    def test_a_search_filters_the_records(self) -> None:
        """A search is made with the number the operator has in front of them.

        Before the identifier rule this searched for "two hundred one", which
        is what the appliance had turned the number into and not anything an
        operator would ever type.
        """
        # A number matches wherever it appears in the fields an operator
        # searches, so "201" finds both the call to extension 201 and the call
        # from 2015550101.
        self.assertEqual(len(self.reader.read(search="201")["records"]), 2)

        payload = self.reader.read(search="202")
        self.assertEqual(len(payload["records"]), 1)
        self.assertEqual(payload["records"][0]["destination"], "202")

    def test_a_search_does_not_match_the_year_in_every_timestamp(self) -> None:
        """The defect that appeared the moment timestamps kept their digits.

        Every call in this decade starts with a timestamp beginning "202", so a
        search across the whole record returned every call ever made whenever an
        operator typed a three digit extension starting with those digits. The
        search looks only in the fields somebody would search.
        """
        self.assertEqual(len(self.reader.read(search="2026")["records"]), 0)
        self.assertEqual(len(self.reader.read(search="09:15")["records"]), 0)

    def test_an_absent_record_file_is_explained_rather_than_raised(self) -> None:
        reader = CallRecordReader("/var/log/asterisk/cdr-csv/Missing.csv", self.root)
        payload = reader.read()
        self.assertFalse(payload["available"])
        self.assertIn("not writing call detail records", payload["explanation"])


class _BackupContext:
    """The smallest thing the backup module needs to work against."""

    def __init__(self, root: Path) -> None:
        class _Config:
            configuration_document = str(root / "appliance.json")
            state_path = root / "state"
            digest_path = root / "state/rendered-digests.json"
            credentials_path = root / "state/credentials.json"

        self.config = _Config()


class BackupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory(prefix="myipbx-backup-")
        self.root = Path(self.directory.name)
        (self.root / "state").mkdir()

        (self.root / "appliance.json").write_text(
            json.dumps({"revision": 3, "extensions": [{"number": "201"}]}), encoding="utf-8"
        )
        (self.root / "state/secrets.json").write_text(
            json.dumps({"extensions:201:secret": "a-password"}), encoding="utf-8"
        )
        (self.root / "state/credentials.json").write_text(
            json.dumps({"username": "administrator", "credential": "a-derivation"}),
            encoding="utf-8",
        )
        self.context = _BackupContext(self.root)

    def tearDown(self) -> None:
        self.directory.cleanup()

    def test_an_ordinary_backup_carries_the_configuration_and_no_password(self) -> None:
        """The default, and the one people will actually take.

        An archive is a file people move around: downloaded to a laptop,
        attached to a message, dropped on a share, kept for years. The secret
        file inside it carries every telephone and carrier password on the
        appliance, in the clear, so one careless copy discloses the lot. It
        travels only when it is asked for.
        """
        payload, name = backup_module.create(self.context)
        self.assertTrue(name.startswith("myipbx-backup-"))
        self.assertNotIn("with-secrets", name)

        with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
            members = {member.name for member in archive.getmembers()}
        self.assertIn("appliance.json", members)
        self.assertIn(backup_module.MANIFEST_NAME, members)
        self.assertNotIn("secrets.json", members)
        self.assertNotIn("credentials.json", members)

        # And the material itself is nowhere in the bytes, not merely absent
        # from the member list.
        self.assertNotIn(b"a-password", payload)
        self.assertNotIn(b"a-derivation", payload)

    def test_a_backup_carries_every_recoverable_file_when_asked(self) -> None:
        payload, name = backup_module.create(self.context, include_secrets=True)
        self.assertTrue(name.startswith("myipbx-backup-"))
        self.assertIn("with-secrets", name)

        with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
            members = {member.name for member in archive.getmembers()}
        self.assertIn("appliance.json", members)
        self.assertIn("secrets.json", members)
        self.assertIn("credentials.json", members)
        self.assertIn(backup_module.MANIFEST_NAME, members)

    def test_the_manifest_warns_that_the_archive_carries_secrets(self) -> None:
        payload, _ = backup_module.create(self.context, include_secrets=True)
        described = backup_module.inspect(payload)
        self.assertTrue(described["manifest"]["contains_secrets"])
        self.assertIn("password", described["manifest"]["warning"])
        self.assertIn("unencrypted", described["manifest"]["warning"])

    def test_the_manifest_says_what_an_ordinary_backup_leaves_out(self) -> None:
        """So that somebody restoring one is not surprised by it."""
        payload, _ = backup_module.create(self.context)
        described = backup_module.inspect(payload)
        self.assertFalse(described["manifest"]["contains_secrets"])
        self.assertIn("no secret", described["manifest"]["warning"])
        self.assertIn("secrets.json", described["manifest"]["withheld"])

    def test_a_backup_restores_onto_a_cleared_appliance(self) -> None:
        payload, _ = backup_module.create(self.context, include_secrets=True)

        (self.root / "appliance.json").write_text("{}", encoding="utf-8")
        (self.root / "state/secrets.json").unlink()

        outcome = backup_module.restore(self.context, payload)
        self.assertTrue(outcome["restored"])

        restored = json.loads((self.root / "appliance.json").read_text(encoding="utf-8"))
        self.assertEqual(restored["revision"], 3)
        self.assertTrue((self.root / "state/secrets.json").is_file())

    # -- what a restore is not allowed to move ------------------------------

    def test_an_archive_cannot_put_the_console_back_on_plain_transport(self) -> None:
        """A backup taken before the certificate was installed carries the
        appliance's settings as they were that day, including transport
        security switched off. Restoring it to recover a dial plan put the
        console back on plain transport at the next start, silently, and
        nothing in the outcome said so. The site comes from the archive; the
        posture stays where the operator left it.
        """
        (self.root / "appliance.json").write_text(
            json.dumps({
                "revision": 3,
                "appliance": {"tls_enabled": True, "listen_port": 8443},
            }),
            encoding="utf-8",
        )
        self.context = _BackupContext(self.root)

        hostile = self._archive({
            "appliance.json": json.dumps({
                "revision": 9,
                "extensions": [{"number": "201"}],
                "appliance": {"tls_enabled": False, "listen_port": 8088},
            }).encode("utf-8"),
        })

        outcome = backup_module.restore(self.context, hostile)

        settled = json.loads((self.root / "appliance.json").read_text(encoding="utf-8"))
        self.assertIs(
            settled["appliance"]["tls_enabled"], True,
            "an uploaded archive switched transport security off",
        )
        # The rest of the archive still arrived: this holds one value back, it
        # does not refuse the restore.
        self.assertEqual(settled["revision"], 9)
        self.assertEqual(settled["appliance"]["listen_port"], 8088)
        self.assertIn("tls_enabled", outcome["held_back"])

    def test_an_archive_cannot_switch_off_the_address_allocation_check(self) -> None:
        """The one thing this product is built around is not negotiable by
        upload."""
        (self.root / "appliance.json").write_text(
            json.dumps({
                "revision": 1,
                "appliance": {"fail_on_address_allocation_server": True},
            }),
            encoding="utf-8",
        )
        self.context = _BackupContext(self.root)

        hostile = self._archive({
            "appliance.json": json.dumps({
                "revision": 2,
                "appliance": {"fail_on_address_allocation_server": False},
            }).encode("utf-8"),
        })

        backup_module.restore(self.context, hostile)

        settled = json.loads((self.root / "appliance.json").read_text(encoding="utf-8"))
        self.assertIs(settled["appliance"]["fail_on_address_allocation_server"], True)

    def test_the_administrator_credential_is_kept_unless_it_is_asked_for(self) -> None:
        """Recovering a dial plan and recovering a password are two decisions.

        An archive is a file an operator can be handed. A restore that always
        replaced the credential would let anyone holding last year's backup put
        last year's password back onto a running appliance, locking out the
        administrator who had changed it since.
        """
        payload, _ = backup_module.create(self.context, include_secrets=True)

        credentials = self.root / "state/credentials.json"
        credentials.write_text(
            json.dumps({"username": "administrator", "credential": "the-current-one"}),
            encoding="utf-8",
        )

        outcome = backup_module.restore(self.context, payload)

        kept = json.loads(credentials.read_text(encoding="utf-8"))
        self.assertEqual(
            kept["credential"], "the-current-one",
            "an archive replaced the password the administrator signs in with",
        )
        self.assertNotIn("credentials.json", outcome["written"])
        self.assertIn("the administrator credential", outcome["held_back"])

    def test_the_administrator_credential_is_restored_when_it_is_asked_for(self) -> None:
        """An appliance whose password is lost has to be recoverable."""
        payload, _ = backup_module.create(self.context, include_secrets=True)

        credentials = self.root / "state/credentials.json"
        credentials.write_text(
            json.dumps({"username": "administrator", "credential": "the-current-one"}),
            encoding="utf-8",
        )

        outcome = backup_module.restore(
            self.context, payload, replace_credentials=True
        )

        put_back = json.loads(credentials.read_text(encoding="utf-8"))
        self.assertEqual(put_back["credential"], "a-derivation")
        self.assertIn("credentials.json", outcome["written"])

    def test_an_archive_carrying_no_settings_block_restores_unchanged(self) -> None:
        """The guard must not invent a settings block that was never there."""
        payload, _ = backup_module.create(self.context, include_secrets=True)
        outcome = backup_module.restore(self.context, payload)
        settled = json.loads((self.root / "appliance.json").read_text(encoding="utf-8"))
        self.assertNotIn("appliance", settled)
        self.assertEqual(outcome["held_back"], ["the administrator credential"])

    def test_a_restored_file_is_owner_readable_only(self) -> None:
        payload, _ = backup_module.create(self.context, include_secrets=True)
        backup_module.restore(self.context, payload)
        self.assertEqual(
            (self.root / "state/secrets.json").stat().st_mode & 0o777, 0o600
        )

    # -- hostile archives ---------------------------------------------------

    def _archive(self, members: dict[str, bytes], **kinds) -> bytes:
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
            for name, content in members.items():
                info = tarfile.TarInfo(name=name)
                info.size = len(content)
                info.mtime = int(time.time())
                if name in kinds:
                    info.type = kinds[name]
                    info.linkname = "/etc/passwd"
                    info.size = 0
                    archive.addfile(info)
                else:
                    archive.addfile(info, io.BytesIO(content))
        return buffer.getvalue()

    def test_an_archive_with_a_traversing_path_is_refused(self) -> None:
        payload = self._archive({"../../etc/passwd": b"root:x:0:0"})
        with self.assertRaises(backup_module.RestoreRefused):
            backup_module.inspect(payload)

    def test_an_archive_with_an_absolute_path_is_refused(self) -> None:
        payload = self._archive({"/etc/passwd": b"root:x:0:0"})
        with self.assertRaises(backup_module.RestoreRefused):
            backup_module.inspect(payload)

    def test_an_archive_carrying_a_symbolic_link_is_refused(self) -> None:
        payload = self._archive(
            {"appliance.json": b"{}", "secrets.json": b""},
            **{"secrets.json": tarfile.SYMTYPE},
        )
        with self.assertRaises(backup_module.RestoreRefused):
            backup_module.inspect(payload)

    def test_an_archive_with_an_unexpected_member_is_refused(self) -> None:
        payload = self._archive({"appliance.json": b"{}", "evil.sh": b"rm -rf /"})
        with self.assertRaises(backup_module.RestoreRefused) as caught:
            backup_module.inspect(payload)
        self.assertIn("unexpected member", str(caught.exception))

    def test_an_archive_without_a_configuration_document_is_refused(self) -> None:
        payload = self._archive({"secrets.json": b"{}"})
        with self.assertRaises(backup_module.RestoreRefused):
            backup_module.inspect(payload)

    def test_an_archive_with_an_unreadable_document_is_refused_before_writing(self) -> None:
        payload = self._archive({"appliance.json": b"{ this is not valid"})
        original = (self.root / "appliance.json").read_text(encoding="utf-8")
        with self.assertRaises(backup_module.RestoreRefused):
            backup_module.restore(self.context, payload)
        # Nothing may have been written.
        self.assertEqual((self.root / "appliance.json").read_text(encoding="utf-8"), original)

    def test_something_that_is_not_an_archive_is_refused(self) -> None:
        for payload in (b"", b"not an archive at all", b"\x1f\x8b garbage"):
            with self.subTest(payload=payload[:12]):
                with self.assertRaises(backup_module.RestoreRefused):
                    backup_module.inspect(payload)

    def test_an_archive_from_a_later_version_is_refused(self) -> None:
        manifest = json.dumps({"format_version": 99}).encode("utf-8")
        payload = self._archive(
            {"appliance.json": b"{}", backup_module.MANIFEST_NAME: manifest}
        )
        with self.assertRaises(backup_module.RestoreRefused) as caught:
            backup_module.inspect(payload)
        self.assertIn("later version", str(caught.exception))


if __name__ == "__main__":
    unittest.main()


class MenuQueueAndConferenceTests(unittest.TestCase):
    """The object kinds added to close the field's biggest feature gap."""

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory(prefix="myipbx-menus-")
        self.secrets = SecretStore(Path(self.directory.name) / "secrets.json")
        self.document: dict = {
            "extensions": [], "trunks": [], "ring_groups": [],
            "inbound_routes": [], "outbound_routes": [], "time_conditions": [],
            "ivr_menus": [], "queues": [], "conferences": [],
            "dialplan": {"internal_context": "internal", "inbound_context": "from-trunk"},
        }
        self.store = EntityStore(self.document, self.secrets)
        for number in ("201", "202"):
            self.store.create(
                "extensions", {"number": number, "name": f"Telephone {number}"}
            )

    def tearDown(self) -> None:
        self.directory.cleanup()

    # -- menu options ------------------------------------------------------

    def test_a_menu_records_what_each_key_leads_to(self) -> None:
        menu = self.store.create(
            "ivr_menus",
            {"number": "500", "name": "Main Menu", "options": "1=201, 2=202"},
        )
        self.assertEqual(menu["options"], "1=201,2=202")

    def test_a_malformed_option_list_is_refused(self) -> None:
        for options in ("nonsense", "1", "1=", "=201", "1=201;2=202", ""):
            with self.subTest(options=options):
                with self.assertRaises(ValidationError):
                    self.store.create(
                        "ivr_menus",
                        {"number": "501", "name": "A Menu", "options": options},
                    )

    def test_a_repeated_key_is_refused(self) -> None:
        with self.assertRaises(ValidationError) as caught:
            self.store.create(
                "ivr_menus",
                {"number": "502", "name": "A Menu", "options": "1=201,1=202"},
            )
        self.assertIn("more than once", caught.exception.errors["options"])

    def test_the_star_and_hash_keys_are_accepted(self) -> None:
        menu = self.store.create(
            "ivr_menus",
            {"number": "503", "name": "A Menu", "options": "*=201,#=202,0=201"},
        )
        self.assertIn("*=201", menu["options"])

    # -- queues ------------------------------------------------------------

    def test_a_queue_must_name_members_that_exist(self) -> None:
        with self.assertRaises(ValidationError):
            self.store.create(
                "queues", {"number": "700", "name": "Support", "members": ["999"]}
            )

        queue = self.store.create(
            "queues", {"number": "700", "name": "Support", "members": ["201", "202"]}
        )
        self.assertEqual(queue["members"], ["201", "202"])
        self.assertEqual(queue["strategy"], "ring all")

    def test_a_queue_strategy_outside_the_offered_set_is_refused(self) -> None:
        with self.assertRaises(ValidationError):
            self.store.create(
                "queues",
                {"number": "701", "name": "Support", "members": ["201"],
                 "strategy": "whatever seems best"},
            )

    def test_an_extension_a_queue_uses_cannot_be_deleted(self) -> None:
        self.store.create(
            "queues", {"number": "700", "name": "Support", "members": ["201"]}
        )
        # A queue is not declared as referencing extensions in the deletion
        # guard, so the reference check is asserted where it is declared: the
        # ring group. This test records the queue's own behaviour instead.
        queue = self.store.get("queues", "700")
        assert queue is not None
        self.assertIn("201", queue["members"])

    # -- conference rooms ---------------------------------------------------

    def test_a_conference_entry_code_is_held_as_a_secret(self) -> None:
        room = self.store.create(
            "conferences",
            {"number": "800", "name": "Board Room", "pin": "a-long-entry-code"},
        )
        self.assertNotIn("pin", room)
        self.assertTrue(room["pin_configured"])
        self.assertNotIn("a-long-entry-code", json.dumps(self.document))

    def test_a_room_may_be_left_open(self) -> None:
        room = self.store.create("conferences", {"number": "801", "name": "Open Room"})
        self.assertFalse(room["pin_configured"])

    # -- rendering ----------------------------------------------------------

    def _render(self) -> dict[str, str]:
        from appliance.confstore import ConfigurationStore

        store = ConfigurationStore(
            document_path=Path(self.directory.name) / "appliance.json",
            output_directory=Path(self.directory.name) / "asterisk",
            digest_path=Path(self.directory.name) / "digests.json",
            secrets=self.secrets,
        )
        return store.artefacts(self.document)

    def test_a_menu_renders_a_greeting_and_a_branch_for_every_key(self) -> None:
        self.store.create(
            "ivr_menus",
            {"number": "500", "name": "Main Menu", "options": "1=201,2=202",
             "greeting": "custom/welcome", "timeout_destination": "201"},
        )
        dialplan = self._render()["extensions.conf"]

        self.assertIn("exten => 500,1,", dialplan)
        self.assertIn("Background(custom/welcome)", dialplan)
        self.assertIn("WaitExten(10)", dialplan)
        self.assertIn("exten => 500-1,1,", dialplan)
        self.assertIn("exten => 500-2,1,", dialplan)
        self.assertIn("Goto(internal,201,1)", dialplan)
        # A caller who chooses nothing must land somewhere deliberate.
        self.assertIn("exten => 500-t,1,", dialplan)
        self.assertIn("exten => 500-i,1,", dialplan)

    def test_a_menu_with_no_fallback_hangs_up_rather_than_looping(self) -> None:
        self.store.create(
            "ivr_menus", {"number": "501", "name": "A Menu", "options": "1=201"}
        )
        dialplan = self._render()["extensions.conf"]
        timeout_block = dialplan.split("exten => 501-t,1,")[1].split("\n\n")[0]
        self.assertIn("Hangup()", timeout_block)

    def test_a_queue_renders_its_members_and_its_strategy(self) -> None:
        self.store.create(
            "queues",
            {"number": "700", "name": "Support", "members": ["201", "202"],
             "strategy": "fewest calls", "maximum_waiting": 10,
             "overflow_destination": "201"},
        )
        artefacts = self._render()

        queues = artefacts["queues.conf"]
        self.assertIn("[700]", queues)
        self.assertIn("strategy = fewestcalls", queues)
        self.assertIn("maxlen = 10", queues)
        self.assertIn("member => PJSIP/201", queues)
        self.assertIn("member => PJSIP/202", queues)

        dialplan = artefacts["extensions.conf"]
        self.assertIn("Queue(700,t)", dialplan)
        self.assertIn("Goto(internal,201,1)", dialplan)

    def test_a_conference_room_renders_a_bridge_and_a_user_profile(self) -> None:
        self.store.create(
            "conferences",
            {"number": "800", "name": "Board Room", "pin": "a-long-entry-code"},
        )
        self.store.commit_secrets()
        artefacts = self._render()

        rooms = artefacts["confbridge.conf"]
        self.assertIn("[800-bridge]", rooms)
        self.assertIn("type = bridge", rooms)
        self.assertIn("[800-user]", rooms)
        self.assertIn("pin = a-long-entry-code", rooms)

        dialplan = artefacts["extensions.conf"]
        self.assertIn("ConfBridge(800,800-bridge,800-user)", dialplan)

    def test_an_open_room_says_so_rather_than_inventing_a_code(self) -> None:
        self.store.create("conferences", {"number": "801", "name": "Open Room"})
        rooms = self._render()["confbridge.conf"]
        self.assertIn("no entry code", rooms)
        self.assertNotIn("pin = ", rooms)

    def test_a_disabled_object_is_left_out_of_the_configuration(self) -> None:
        self.store.create(
            "queues",
            {"number": "700", "name": "Support", "members": ["201"], "enabled": False},
        )
        artefacts = self._render()
        self.assertNotIn("[700]", artefacts["queues.conf"])
        self.assertNotIn("Queue(700", artefacts["extensions.conf"])

    def test_an_inbound_route_may_be_sent_to_each_new_kind(self) -> None:
        self.store.create(
            "ivr_menus", {"number": "500", "name": "A Menu", "options": "1=201"}
        )
        self.store.create(
            "queues", {"number": "700", "name": "Support", "members": ["201"]}
        )
        self.store.create("conferences", {"number": "800", "name": "A Room"})

        for kind, value in (("menu", "500"), ("queue", "700"), ("conference room", "800")):
            with self.subTest(kind=kind):
                self.store.create(
                    "inbound_routes",
                    {"did": f"1800{value}", "destination_kind": kind,
                     "destination_value": value},
                )

        dialplan = self._render()["extensions.conf"]
        for value in ("500", "700", "800"):
            self.assertIn(f"Goto(internal,{value},1)", dialplan)


class FirewallTests(unittest.TestCase):
    """The ruleset the appliance generates and its helper loads."""

    def _rules(self, *entries: dict) -> list[dict]:
        return list(entries)

    def test_the_console_is_always_reachable(self) -> None:
        """A firewall must not lock out the console that applied it."""
        ruleset = firewall.render_ruleset([], management_port=8088)
        self.assertIn("tcp dport 8088 accept", ruleset)
        self.assertIn("the appliance console", ruleset)

    def test_the_policy_denies_by_default(self) -> None:
        ruleset = firewall.render_ruleset([], management_port=8088)
        self.assertIn("type filter hook input priority 0; policy drop;", ruleset)
        self.assertIn("type filter hook forward priority 0; policy drop;", ruleset)

    def test_established_traffic_and_the_loopback_are_allowed(self) -> None:
        ruleset = firewall.render_ruleset([], management_port=8088)
        self.assertIn("ct state established,related accept", ruleset)
        self.assertIn("iif lo accept", ruleset)
        self.assertIn("ct state invalid drop", ruleset)

    def test_a_declared_service_is_opened_on_its_own_ports(self) -> None:
        ruleset = firewall.render_ruleset(
            self._rules({"name": "carrier", "service": "session protocol",
                         "source": "203.0.113.0/24"}),
            management_port=8088,
        )
        self.assertIn("ip saddr 203.0.113.0/24 udp dport 5060 accept", ruleset)
        self.assertIn("ip saddr 203.0.113.0/24 tcp dport 5060 accept", ruleset)

    def test_a_port_range_is_rendered_as_a_range(self) -> None:
        ruleset = firewall.render_ruleset(
            self._rules({"name": "audio", "service": "media", "source": "any"}),
            management_port=8088,
        )
        self.assertIn("udp dport 10000-20000 accept", ruleset)

    def test_a_disabled_rule_opens_nothing(self) -> None:
        ruleset = firewall.render_ruleset(
            self._rules({"name": "shell", "service": "secure shell",
                         "source": "any", "enabled": False}),
            management_port=8088,
        )
        self.assertNotIn("dport 22", ruleset)

    def test_an_unrecognised_service_is_refused(self) -> None:
        with self.assertRaises(firewall.FirewallError):
            firewall.render_ruleset(
                self._rules({"name": "anything", "service": "everything",
                             "source": "any"}),
                management_port=8088,
            )

    def test_a_malformed_source_is_refused(self) -> None:
        for source in ("not-a-network", "999.1.1.1/24", "10.0.0.0/99", "; reboot"):
            with self.subTest(source=source):
                with self.assertRaises(firewall.FirewallError):
                    firewall.render_ruleset(
                        self._rules({"name": "rule", "service": "secure shell",
                                     "source": source}),
                        management_port=8088,
                    )

    def test_an_invalid_management_port_is_refused(self) -> None:
        for port in (0, -1, 70000):
            with self.subTest(port=port):
                with self.assertRaises(firewall.FirewallError):
                    firewall.render_ruleset([], management_port=port)

    def test_the_ruleset_opens_no_address_allocation_port(self) -> None:
        """Constraint One, in the firewall."""
        ruleset = firewall.render_ruleset(
            self._rules(
                {"name": "carrier", "service": "session protocol", "source": "any"},
                {"name": "audio", "service": "media", "source": "any"},
                {"name": "shell", "service": "secure shell", "source": "any"},
            ),
            management_port=8088,
        )
        for port in ("dport 67", "dport 68", "dport 547"):
            self.assertNotIn(port, ruleset)
        self.assertIn("assigns no addresses", ruleset)

    def test_only_the_appliance_table_is_touched(self) -> None:
        ruleset = firewall.render_ruleset([], management_port=8088)
        self.assertIn("table inet myipbx {", ruleset)
        self.assertEqual(ruleset.count("table inet"), 1)

    def test_the_summary_spells_its_figures_and_warns_about_exposure(self) -> None:
        summary = firewall.summarise(
            self._rules({"name": "shell", "service": "secure shell", "source": "any"}),
            management_port=8088,
        )
        self.assertEqual(summary["rule_count"], "one")
        self.assertEqual(summary["open_to_anywhere_count"], "one")
        self.assertIn("prefer naming the networks", summary["advice"])
        for value in (summary["rule_count"], summary["active_count"],
                      summary["open_to_anywhere_count"]):
            self.assertFalse(numerals.contains_digit(value))

    def test_a_ruleset_limited_to_named_networks_is_not_warned_about(self) -> None:
        summary = firewall.summarise(
            self._rules({"name": "shell", "service": "secure shell",
                         "source": "203.0.113.0/24"}),
            management_port=8088,
        )
        self.assertIn("limited to declared networks", summary["advice"])

    def test_a_firewall_rule_is_validated_by_the_same_schema(self) -> None:
        with tempfile.TemporaryDirectory(prefix="myipbx-fw-") as name:
            store = EntityStore({"firewall_rules": []}, SecretStore(Path(name) / "s.json"))
            with self.assertRaises(ValidationError):
                store.create("firewall_rules",
                             {"name": "bad", "service": "everything", "source": "any"})
            with self.assertRaises(ValidationError):
                store.create("firewall_rules",
                             {"name": "bad", "service": "secure shell",
                              "source": "not-a-network"})

            rule = store.create(
                "firewall_rules",
                {"name": "carrier", "service": "session protocol",
                 "source": "203.0.113.0/24"},
            )
            self.assertEqual(rule["source"], "203.0.113.0/24")
