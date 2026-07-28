"""The configuration store with drift detection, and hardware enumeration.

These cover Benchmark Defect One (an ambiguous configuration source of truth)
and Benchmark Defect Three (unguided legacy interface hardware provisioning).
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from support import build_hardware_fixture

from appliance.confstore import (
    GENERATED_MARKER,
    ConfigurationStore,
    DriftDetected,
    STATUS_CURRENT,
    STATUS_MISSING,
    STATUS_MODIFIED,
    STATUS_UNMANAGED,
)
from appliance.hardware import (
    DIGIUM_VENDOR_IDENTIFIER,
    HardwareInventory,
    describe_card,
    parse_span_document,
)


DOCUMENT = {
    "revision": 1,
    "trunks": [
        {
            "name": "carrier-primary",
            "technology": "PJSIP",
            "host": "sip.example.net",
            "username": "example-account",
            "enabled": True,
        }
    ],
    "extensions": [
        {"number": "201", "target": "PJSIP/201"},
        {"number": "202", "target": "PJSIP/202"},
    ],
    "dialplan": {
        "internal_context": "internal",
        "inbound_context": "from-trunk",
        "inbound_target": "201",
    },
    "hardware": {"spans": [{"number": 1, "signalling": "fxo_ks", "group": 1, "channels": "1-4"}]},
}


class ConfigurationStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory(prefix="crossbar-store-")
        base = Path(self.directory.name)
        self.output = base / "asterisk"
        self.store = ConfigurationStore(
            document_path=base / "appliance.json",
            output_directory=self.output,
            digest_path=base / "digests.json",
        )

    def tearDown(self) -> None:
        self.directory.cleanup()

    def test_an_absent_document_yields_a_working_default(self) -> None:
        document = self.store.load()
        self.assertEqual(document["trunks"], [])
        self.assertIn("dialplan", document)

    def test_saving_advances_the_revision_and_persists_atomically(self) -> None:
        saved = self.store.save(DOCUMENT)
        self.assertEqual(saved["revision"], 2)

        reloaded = self.store.load()
        self.assertEqual(reloaded["revision"], 2)
        self.assertEqual(len(reloaded["trunks"]), 1)
        # No temporary file may survive an atomic write.
        self.assertEqual(list(Path(self.directory.name).glob("*.partial")), [])

    def test_every_artefact_is_rendered_and_marked_as_generated(self) -> None:
        outcome = self.store.render(DOCUMENT)
        expected = {"pjsip.conf", "extensions.conf", "voicemail.conf",
                    "queues.conf", "confbridge.conf", "chan_dahdi.conf",
                    "manager.conf"}
        self.assertEqual(set(outcome["written"]), expected)

        for name in expected:
            content = (self.output / name).read_text(encoding="utf-8")
            with self.subTest(name=name):
                self.assertIn(GENERATED_MARKER, content)

    def test_the_rendered_trunk_configuration_reflects_the_document(self) -> None:
        self.store.render(DOCUMENT)
        content = (self.output / "pjsip.conf").read_text(encoding="utf-8")
        self.assertIn("[carrier-primary]", content)
        self.assertIn("sip:sip.example.net", content)
        self.assertIn("example-account", content)

    def test_an_unset_secret_is_never_invented(self) -> None:
        """With no secret stored, no credential may appear in the artefact."""
        self.store.render(DOCUMENT)
        content = (self.output / "pjsip.conf").read_text(encoding="utf-8")
        self.assertIn("no password has been set for this trunk yet", content)
        self.assertNotIn("password = ", content)

    def test_a_secret_reaches_the_artefact_only_from_the_secret_store(self) -> None:
        """The secret is held apart and is read only at the moment of render."""
        from appliance.entities import SecretStore

        secrets = SecretStore(Path(self.directory.name) / "secrets.json")
        secrets.set("trunks", "carrier-primary", "secret", "a-carrier-password")
        self.store.secrets = secrets

        self.store.render(DOCUMENT, force=True)
        content = (self.output / "pjsip.conf").read_text(encoding="utf-8")
        self.assertIn("password = a-carrier-password", content)

        # The source of truth itself must never carry it.
        saved = self.store.save(DOCUMENT)
        self.assertNotIn(
            "a-carrier-password",
            json.dumps(saved),
            "the secret leaked into the source of truth document",
        )
        self.assertEqual(
            (Path(self.directory.name) / "secrets.json").stat().st_mode & 0o777,
            0o600,
        )

    def test_the_rendered_dialplan_contains_every_declared_extension(self) -> None:
        self.store.render(DOCUMENT)
        content = (self.output / "extensions.conf").read_text(encoding="utf-8")
        self.assertIn("[internal]", content)
        self.assertIn("exten => 201,1", content)
        self.assertIn("exten => 202,1", content)

    def test_the_manager_interface_is_bound_to_the_loopback_address_only(self) -> None:
        self.store.render(DOCUMENT)
        content = (self.output / "manager.conf").read_text(encoding="utf-8")
        self.assertIn("bindaddr = 127.0.0.1", content)
        self.assertIn("deny = 0.0.0.0/0.0.0.0", content)
        self.assertIn("permit = 127.0.0.1", content)

    def test_an_unchanged_render_rewrites_nothing(self) -> None:
        self.store.render(DOCUMENT)
        second = self.store.render(DOCUMENT)
        self.assertEqual(second["written"], [])
        self.assertEqual(len(second["unchanged"]), 7)

    # -- Benchmark Defect One ------------------------------------------------

    def test_an_untouched_artefact_reports_itself_current(self) -> None:
        self.store.render(DOCUMENT)
        statuses = {report.name: report.status for report in self.store.inspect(DOCUMENT)}
        self.assertTrue(all(status == STATUS_CURRENT for status in statuses.values()))

    def test_an_absent_artefact_reports_itself_missing(self) -> None:
        reports = self.store.inspect(DOCUMENT)
        self.assertTrue(all(report.status == STATUS_MISSING for report in reports))

    def test_a_file_the_appliance_never_wrote_is_never_overwritten(self) -> None:
        self.output.mkdir(parents=True, exist_ok=True)
        (self.output / "pjsip.conf").write_text("; written by a human\n", encoding="utf-8")

        reports = {report.name: report for report in self.store.inspect(DOCUMENT)}
        self.assertEqual(reports["pjsip.conf"].status, STATUS_UNMANAGED)

        with self.assertRaises(DriftDetected):
            self.store.render(DOCUMENT)
        self.assertEqual(
            (self.output / "pjsip.conf").read_text(encoding="utf-8"),
            "; written by a human\n",
        )

    def test_an_edited_artefact_is_detected_and_refused(self) -> None:
        """The defining test for Benchmark Defect One."""
        self.store.render(DOCUMENT)

        emergency_fix = "; an emergency fix applied at three o'clock in the morning\n"
        (self.output / "pjsip.conf").write_text(emergency_fix, encoding="utf-8")

        reports = {report.name: report for report in self.store.inspect(DOCUMENT)}
        self.assertEqual(reports["pjsip.conf"].status, STATUS_MODIFIED)
        self.assertTrue(reports["pjsip.conf"].diverged)

        with self.assertRaises(DriftDetected) as caught:
            self.store.render(DOCUMENT)
        self.assertEqual([r.name for r in caught.exception.reports], ["pjsip.conf"])

        # The administrator's edit must still be on disk, untouched.
        self.assertEqual(
            (self.output / "pjsip.conf").read_text(encoding="utf-8"), emergency_fix
        )

    def test_a_divergence_blocks_only_after_an_explicit_decision(self) -> None:
        self.store.render(DOCUMENT)
        (self.output / "pjsip.conf").write_text("; edited\n", encoding="utf-8")

        # Decision one: regenerate over the edit, deliberately.
        outcome = self.store.render(DOCUMENT, force=True)
        self.assertTrue(outcome["forced"])
        self.assertIn(GENERATED_MARKER, (self.output / "pjsip.conf").read_text(encoding="utf-8"))

    def test_adopting_an_edit_records_it_as_authoritative(self) -> None:
        self.store.render(DOCUMENT)
        edit = "; adopted by the administrator\n"
        (self.output / "pjsip.conf").write_text(edit, encoding="utf-8")

        # Decision two: adopt the file on disk.
        report = self.store.adopt("pjsip.conf")
        self.assertEqual(report.status, STATUS_CURRENT)

        # The divergence is no longer reported, and the edit survives.
        statuses = {r.name: r.status for r in self.store.inspect(DOCUMENT)}
        self.assertEqual(statuses["pjsip.conf"], STATUS_CURRENT)
        self.assertEqual((self.output / "pjsip.conf").read_text(encoding="utf-8"), edit)

    def test_adopting_a_file_that_does_not_exist_is_refused(self) -> None:
        with self.assertRaises(FileNotFoundError):
            self.store.adopt("pjsip.conf")

    def test_a_corrupt_digest_record_treats_artefacts_as_unmanaged(self) -> None:
        self.store.render(DOCUMENT)
        self.store.digest_path.write_text("this is not valid", encoding="utf-8")
        statuses = {r.name: r.status for r in self.store.inspect(DOCUMENT)}
        self.assertTrue(all(status == STATUS_UNMANAGED for status in statuses.values()))

    def test_a_corrupt_source_document_is_raised_rather_than_defaulted(self) -> None:
        self.store.document_path.write_text("{ not valid json", encoding="utf-8")
        with self.assertRaises(DriftDetected):
            self.store.load()

    def test_a_document_with_no_spans_explains_itself_in_the_artefact(self) -> None:
        document = dict(DOCUMENT)
        document["hardware"] = {"spans": []}
        self.store.render(document)
        content = (self.output / "chan_dahdi.conf").read_text(encoding="utf-8")
        self.assertIn("no span has been mapped", content)


class SpanDocumentParsingTests(unittest.TestCase):
    def test_a_span_document_is_parsed_into_channels(self) -> None:
        span = parse_span_document(
            'Span 1: WCTDM/0 "Wildcard A4A" (MASTER)\n'
            "\n"
            "           1 WCTDM/0/0 FXOKS (In use)\n"
            "           2 WCTDM/0/1 FXOKS\n"
        )
        assert span is not None
        self.assertEqual(span.number, 1)
        self.assertEqual(span.identifier, "WCTDM/0")
        self.assertEqual(span.description, "Wildcard A4A")
        self.assertEqual(len(span.channels), 2)
        self.assertEqual(span.channels[0].signalling, "FXOKS")
        self.assertTrue(span.channels[0].in_use if hasattr(span.channels[0], "in_use") else True)
        self.assertEqual(span.channels[0].as_dict()["in_use"], True)
        self.assertEqual(span.channels[1].as_dict()["in_use"], False)

    def test_an_alarmed_span_is_reported_as_unhealthy(self) -> None:
        # The driver writes the alarm bare, after the signalling description,
        # not inside parentheses. A fixture that parenthesises it agrees with a
        # defect this suite once had: the parser read only inside parentheses,
        # so a span in RED alarm reported "no alarm" and a technician fitting a
        # card saw every span green on a dead line.
        span = parse_span_document(
            'Span 2: TE2/0/1 "T2XXP (PCI) Card 0 Span 1" (MASTER) HDB3/CCS/CRC4 RED\n'
        )
        assert span is not None
        self.assertFalse(span.healthy)
        self.assertEqual(span.alarm, "RED")

    def test_a_span_with_no_annotation_is_healthy(self) -> None:
        span = parse_span_document('Span 1: WCTDM/0 "A board"\n')
        assert span is not None
        self.assertTrue(span.healthy)

    def test_a_bare_alarm_after_the_signalling_description_is_seen(self) -> None:
        """The shape the driver actually emits, for each alarm it can raise.

        This is the case the parser used to miss entirely, so each token is
        asserted rather than trusting one example to stand for the rest.
        """
        for token in ("RED", "YELLOW", "BLUE", "LOS", "LFA", "NOTOPEN", "RECOVERING"):
            with self.subTest(alarm=token):
                span = parse_span_document(
                    f'Span 1: TE4/0/1 "T4XXP (PCI) Card 0 Span 1" (MASTER) HDB3/CCS/CRC4 {token}\n'
                )
                self.assertIn(token, span.alarm)
                self.assertFalse(
                    span.healthy,
                    f"a span reporting {token} was treated as healthy",
                )

    def test_the_signalling_description_alone_is_not_an_alarm(self) -> None:
        """Every healthy span carries one, so reading it as a fault alarms all of them."""
        for signalling in ("HDB3/CCS/CRC4", "B8ZS/ESF", "AMI/D4", "CCS/HDB3"):
            with self.subTest(signalling=signalling):
                span = parse_span_document(
                    f'Span 1: TE4/0/1 "A board" (MASTER) {signalling}\n'
                )
                self.assertEqual(span.alarm, "no alarm")
                self.assertTrue(span.healthy)

    def test_several_bare_alarms_are_all_reported(self) -> None:
        span = parse_span_document(
            'Span 1: TE4/0/1 "A board" (MASTER) B8ZS/ESF LOS LFA\n'
        )
        self.assertIn("LOS", span.alarm)
        self.assertIn("LFA", span.alarm)
        self.assertFalse(span.healthy)

    def test_a_role_annotation_is_not_mistaken_for_an_alarm(self) -> None:
        """The master span of every machine carries a role annotation."""
        for annotation in ("(MASTER)", "(SLAVE)", "(OK)"):
            with self.subTest(annotation=annotation):
                span = parse_span_document(f'Span 1: WCTDM/0 "A board" {annotation}\n')
                assert span is not None
                self.assertTrue(
                    span.healthy,
                    f"the role annotation {annotation} was mistaken for an alarm",
                )
                self.assertEqual(span.alarm, "no alarm")

    def test_an_alarm_beside_a_role_annotation_is_still_reported(self) -> None:
        span = parse_span_document('Span 1: TE2/0/1 "A board" (MASTER) (RED)\n')
        assert span is not None
        self.assertFalse(span.healthy)
        self.assertEqual(span.alarm, "RED")

    def test_unparseable_text_yields_nothing_rather_than_raising(self) -> None:
        self.assertIsNone(parse_span_document("this is not a span document"))
        self.assertIsNone(parse_span_document(""))


class HardwareInventoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory(prefix="crossbar-hardware-")
        self.base = Path(self.directory.name)

    def tearDown(self) -> None:
        self.directory.cleanup()

    def test_a_digium_card_is_detected_and_named_in_plain_language(self) -> None:
        root = build_hardware_fixture(self.base, with_card=True)
        inventory = HardwareInventory(root).scan()

        self.assertTrue(inventory["hardware_present"])
        self.assertEqual(inventory["card_count"], 1)
        card = inventory["cards"][0]
        self.assertEqual(card["model"], "Wildcard A4A")
        self.assertEqual(card["driver_module"], "wcaxx")
        self.assertTrue(card["recognised"])

    def test_the_spans_and_channels_are_enumerated(self) -> None:
        root = build_hardware_fixture(self.base, with_card=True)
        inventory = HardwareInventory(root).scan()
        self.assertEqual(inventory["span_count"], 1)
        self.assertEqual(inventory["channel_count"], 4)
        self.assertEqual(inventory["alarmed_span_count"], 0)
        self.assertTrue(inventory["driver_loaded"])

    def test_a_machine_with_no_card_reports_absence_as_a_normal_condition(self) -> None:
        root = build_hardware_fixture(self.base, with_card=False)
        inventory = HardwareInventory(root).scan()

        self.assertFalse(inventory["hardware_present"])
        self.assertEqual(inventory["card_count"], 0)
        self.assertEqual(inventory["spans"], [])
        self.assertIn("no legacy Digium interface card was detected", inventory["summary"])
        self.assertIsNone(inventory["last_error"])

    def test_a_machine_with_no_peripheral_bus_at_all_does_not_raise(self) -> None:
        inventory = HardwareInventory(self.base / "absent").scan()
        self.assertEqual(inventory["card_count"], 0)
        self.assertIsNotNone(inventory["last_error"])

    def test_an_unrecognised_digium_card_is_still_reported(self) -> None:
        model, module, description = describe_card(0xFFFF)
        self.assertIn("unrecognised Digium interface card", model)
        self.assertEqual(module, "dahdi")
        self.assertTrue(description)

    def test_the_vendor_identifier_is_the_digium_one(self) -> None:
        self.assertEqual(DIGIUM_VENDOR_IDENTIFIER, 0xD161)


if __name__ == "__main__":
    unittest.main()
