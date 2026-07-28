"""Recorded calls, and the one route that hands a file off the disk.

Most of this file is about that route. It is the only place in the appliance
where a name arriving from outside chooses a file, and the failure mode is not
a wrong answer on a screen -- it is serving somebody a file they were never
meant to have.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from appliance import confstore, entities
from appliance.recordings import RECORDING_PATTERN, RecordingStore

GOOD = "20260727-091500_441632960111_201_1753600000.123456.wav"
ALSO_GOOD = "20260726-140000_201_441632960999_1753500000.1.wav"


class WhatCountsAsARecordingTests(unittest.TestCase):
    def test_the_name_the_appliance_writes_is_accepted(self) -> None:
        match = RECORDING_PATTERN.match(GOOD)
        self.assertIsNotNone(match)
        self.assertEqual(match.group("source"), "441632960111")
        self.assertEqual(match.group("destination"), "201")

    def test_a_name_carrying_a_separator_is_refused(self) -> None:
        """The first of the two rules that stop a name naming another file."""
        for name in (
            "../../etc/shadow",
            "..%2f..%2fetc%2fshadow",
            "20260727-091500_1_2_1.1.wav/../../etc/shadow",
            "/etc/shadow",
            "subdirectory/20260727-091500_1_2_1.1.wav",
        ):
            with self.subTest(name=name):
                self.assertIsNone(RECORDING_PATTERN.match(name))

    def test_a_name_with_another_extension_is_refused(self) -> None:
        for name in (
            "20260727-091500_1_2_1.1.conf",
            "20260727-091500_1_2_1.1.wav.php",
            "20260727-091500_1_2_1.1",
        ):
            with self.subTest(name=name):
                self.assertIsNone(RECORDING_PATTERN.match(name))

    def test_a_name_with_letters_where_a_number_belongs_is_refused(self) -> None:
        self.assertIsNone(RECORDING_PATTERN.match(
            "20260727-091500_shadow_201_1753600000.1.wav"
        ))


class WhatIsServedTests(unittest.TestCase):
    def setUp(self) -> None:
        self.workspace = tempfile.TemporaryDirectory()
        self.addCleanup(self.workspace.cleanup)
        self.root = Path(self.workspace.name)
        self.directory = self.root / "var" / "spool" / "asterisk" / "monitor"
        self.directory.mkdir(parents=True)
        (self.directory / GOOD).write_bytes(b"RIFF....WAVEfmt ")
        self.store = RecordingStore(
            directory="/var/spool/asterisk/monitor", root=self.root
        )

        # Something outside the directory that must never be reachable.
        self.secret = self.root / "a-secret-outside-the-directory"
        self.secret.write_bytes(b"this must never be served")

    def test_a_recording_this_appliance_wrote_is_served(self) -> None:
        found = self.store.read(GOOD)
        self.assertIsNotNone(found)
        payload, media_type = found
        self.assertEqual(payload, b"RIFF....WAVEfmt ")
        self.assertEqual(media_type, "audio/wav")

    def test_a_traversal_is_refused(self) -> None:
        for name in (
            "../a-secret-outside-the-directory",
            "../../a-secret-outside-the-directory",
            "./../a-secret-outside-the-directory",
            "/etc/passwd",
        ):
            with self.subTest(name=name):
                self.assertIsNone(self.store.resolve(name))
                self.assertIsNone(self.store.read(name))

    def test_a_symbolic_link_out_of_the_directory_is_refused(self) -> None:
        """The second rule, and the reason the first is not enough.

        A link whose name is a perfectly ordinary recording name passes every
        check on the name. Only resolving it and looking at where it actually
        landed catches this.
        """
        link = self.directory / ALSO_GOOD
        try:
            os.symlink(self.secret, link)
        except (OSError, NotImplementedError):
            self.skipTest("this filesystem does not support symbolic links")

        self.assertIsNotNone(RECORDING_PATTERN.match(link.name))
        self.assertIsNone(self.store.resolve(link.name))
        self.assertIsNone(self.store.read(link.name))

    def test_a_file_that_is_not_a_recording_is_not_served(self) -> None:
        (self.directory / "notes.txt").write_text("hello", encoding="utf-8")
        self.assertIsNone(self.store.read("notes.txt"))

    def test_a_recording_that_does_not_exist_is_not_served(self) -> None:
        self.assertIsNone(self.store.read(ALSO_GOOD))

    def test_a_recording_past_the_ceiling_is_refused_rather_than_read(self) -> None:
        from appliance import recordings
        original = recordings.MAXIMUM_BYTES
        recordings.MAXIMUM_BYTES = 4
        self.addCleanup(setattr, recordings, "MAXIMUM_BYTES", original)
        self.assertIsNone(self.store.read(GOOD))


class WhatTheListingSaysTests(unittest.TestCase):
    def setUp(self) -> None:
        self.workspace = tempfile.TemporaryDirectory()
        self.addCleanup(self.workspace.cleanup)
        self.root = Path(self.workspace.name)
        self.directory = self.root / "monitor"
        self.directory.mkdir()
        for name in (GOOD, ALSO_GOOD):
            (self.directory / name).write_bytes(b"x" * 2048)
        self.store = RecordingStore(directory="/monitor", root=self.root)

    def test_recordings_are_listed_newest_first(self) -> None:
        listing = self.store.list()
        self.assertTrue(listing["available"])
        self.assertEqual([record["name"] for record in listing["records"]],
                         [GOOD, ALSO_GOOD])

    def test_everything_a_row_shows_comes_out_of_the_name(self) -> None:
        """No second record of the same thing to come to disagree with it."""
        record = self.store.list()["records"][0]
        self.assertEqual(record["at"], "2026-07-27 09:15:00")
        self.assertEqual(record["source"], "441632960111")
        self.assertEqual(record["destination"], "201")

    def test_a_search_looks_at_the_numbers_and_the_time(self) -> None:
        self.assertEqual(len(self.store.list(search="201")["records"]), 2)
        self.assertEqual(len(self.store.list(search="2026-07-26")["records"]), 1)
        self.assertEqual(len(self.store.list(search="nothing")["records"]), 0)

    def test_a_window_narrows_the_listing_at_both_ends(self) -> None:
        self.assertEqual(
            len(self.store.list(since="2026-07-27")["records"]), 1
        )
        self.assertEqual(
            len(self.store.list(until="2026-07-26")["records"]), 1
        )

    def test_a_file_the_appliance_did_not_write_is_reported_rather_than_ignored(self) -> None:
        """An operator who copied recordings in should be told they are not listed."""
        (self.directory / "somebody-elses-recording.wav").write_bytes(b"x")
        listing = self.store.list()
        self.assertEqual(len(listing["records"]), 2)
        self.assertEqual(listing["unrecognised_count"], "one")
        self.assertIn("not named the way this appliance names", listing["explanation"])

    def test_an_absent_directory_says_what_to_do_rather_than_failing(self) -> None:
        store = RecordingStore(directory="/nowhere", root=self.root)
        listing = store.list()
        self.assertFalse(listing["available"])
        self.assertIn("recording is off on every extension", listing["explanation"])
        self.assertIn("must be told", listing["explanation"])


class WhatRetentionRemovesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.workspace = tempfile.TemporaryDirectory()
        self.addCleanup(self.workspace.cleanup)
        self.root = Path(self.workspace.name)
        self.directory = self.root / "monitor"
        self.directory.mkdir()
        self.store = RecordingStore(directory="/monitor", root=self.root)

        self.old = self.directory / GOOD
        self.old.write_bytes(b"x")
        stale = (datetime.now() - timedelta(days=90)).timestamp()
        os.utime(self.old, (stale, stale))

        self.fresh = self.directory / ALSO_GOOD
        self.fresh.write_bytes(b"x")

    def test_a_recording_past_its_retention_is_removed(self) -> None:
        outcome = self.store.prune(keep_days=30)
        self.assertEqual(outcome["removed"], 1)
        self.assertEqual(outcome["kept"], 1)
        self.assertFalse(self.old.exists())
        self.assertTrue(self.fresh.exists())

    def test_a_retention_of_zero_keeps_everything_and_says_so(self) -> None:
        """A decision somebody made, not a default meaning "delete it all"."""
        outcome = self.store.prune(keep_days=0)
        self.assertEqual(outcome["removed"], 0)
        self.assertIn("indefinitely", outcome["explanation"])
        self.assertTrue(self.old.exists())

    def test_a_file_the_appliance_did_not_write_is_never_deleted(self) -> None:
        stranger = self.directory / "somebody-elses-file.wav"
        stranger.write_bytes(b"x")
        stale = (datetime.now() - timedelta(days=900)).timestamp()
        os.utime(stranger, (stale, stale))
        self.store.prune(keep_days=1)
        self.assertTrue(stranger.exists())


class WhatTurnsRecordingOnTests(unittest.TestCase):
    def dialplan(self, mode: str) -> str:
        return confstore.render_dialplan({
            "extensions": [{
                "number": "201", "name": "reception", "technology": "PJSIP",
                "ring_seconds": 20, "voicemail": True,
                "record_calls": mode, "enabled": True,
            }],
            "dialplan": {"internal_context": "internal",
                         "inbound_context": "from-trunk"},
        })

    def test_nothing_is_recorded_until_somebody_turns_it_on(self) -> None:
        self.assertNotIn("MixMonitor", self.dialplan("never"))

    def test_recording_is_off_by_default_on_a_new_extension(self) -> None:
        field = entities.ENTITY_SPECS["extensions"].field("record_calls")
        self.assertEqual(field.default, "never")

    def test_the_setting_says_that_a_caller_has_to_be_told(self) -> None:
        """Where the decision is made, not in a manual nobody opens."""
        field = entities.ENTITY_SPECS["extensions"].field("record_calls")
        self.assertIn("must be told", field.help)

    def test_recording_starts_when_the_call_is_answered(self) -> None:
        """Not when it starts ringing, or every recording opens with ringback."""
        rendered = self.dialplan("always")
        self.assertIn("MixMonitor(", rendered)
        # The argument itself contains brackets, so the line is checked by its
        # ending rather than by matching balanced ones.
        line = next(part for part in rendered.splitlines() if "MixMonitor(" in part)
        self.assertTrue(line.rstrip().endswith(",b)"), line)

    def test_the_recording_is_started_before_the_call_is_dialled(self) -> None:
        rendered = self.dialplan("always")
        self.assertLess(rendered.index("MixMonitor("), rendered.index("Dial(PJSIP/201"))

    def test_the_name_the_engine_writes_is_the_name_the_reader_expects(self) -> None:
        """The two halves of this feature agree, or nothing is ever listed."""
        rendered = self.dialplan("always")
        self.assertIn("%Y%m%d-%H%M%S", rendered)
        self.assertIn("${CALLERID(num)}_${EXTEN}_${UNIQUEID}.wav", rendered)

        # And a name of exactly that shape is one the reader accepts.
        self.assertIsNotNone(RECORDING_PATTERN.match(GOOD))

    def test_calls_in_records_a_call_arriving_at_the_extension(self) -> None:
        self.assertIn("MixMonitor", self.dialplan("calls in"))

    def test_calls_out_does_not_record_a_call_arriving(self) -> None:
        """The extension's own leg is the inbound one; outbound is the route."""
        self.assertNotIn("MixMonitor", self.dialplan("calls out"))


if __name__ == "__main__":
    unittest.main()
