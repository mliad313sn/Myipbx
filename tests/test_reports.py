"""The questions an owner asks, and whether the answers add up.

A report is the one screen in a telephony product that somebody acts on
financially, and it is the one screen whose defects are invisible: a call
history that drops a row is obviously wrong, and an answer rate that is quietly
two points high is not. So the tests here are mostly arithmetic checked against
records constructed so that the right answer is known by hand.
"""

from __future__ import annotations

import unittest
from datetime import date, datetime, timedelta

from appliance import numerals, reports
from appliance.diagnostics import CallRecordReader

NOW = datetime(2026, 7, 28, 12, 0, 0)


def call(
    started: datetime,
    source: str = "201",
    destination: str = "+441632960111",
    disposition: str = "ANSWERED",
    duration: int = 60,
    talk: int = 45,
    context: str = "internal",
    application: str = "Dial",
) -> dict:
    return {
        "account_code": "",
        "source": source,
        "destination": destination,
        "context": context,
        "caller_identity": "",
        "application": application,
        "channel": "",
        "destination_channel": "",
        "started_at": started.strftime("%Y-%m-%d %H:%M:%S"),
        "answered_at": "",
        "ended_at": "",
        "duration": duration,
        "billable_seconds": talk,
        "disposition": disposition,
        "unique_identifier": f"u{started.timestamp()}-{source}-{destination}",
    }


EXTENSIONS = [
    {"number": "201", "name": "reception"},
    {"number": "202", "name": "workshop"},
]


def report_over(records, window_name="last-thirty-days", **kwargs):
    window = reports.resolve_window(window_name, now=NOW, **kwargs)
    return reports.build_report(
        records, window, extensions=EXTENSIONS,
        inbound_context="from-trunk", internal_context="internal",
    )


class WindowTests(unittest.TestCase):
    def test_every_named_window_resolves(self) -> None:
        for name, _ in reports.WINDOWS:
            if name == "between":
                continue
            window = reports.resolve_window(name, now=NOW)
            self.assertLessEqual(window["from"], window["to"], name)

    def test_a_window_that_does_not_exist_is_refused_by_name(self) -> None:
        with self.assertRaises(reports.ReportRefused) as caught:
            reports.resolve_window("last-fortnight", now=NOW)
        self.assertIn("last-fortnight", str(caught.exception))
        self.assertIn("last-seven-days", str(caught.exception))

    def test_both_ends_of_a_named_window_are_included(self) -> None:
        """An exclusive end silently drops a day out of every report."""
        window = reports.resolve_window("last-seven-days", now=NOW)
        self.assertEqual(window["to"], NOW.date())
        self.assertEqual(window["from"], NOW.date() - timedelta(days=6))
        self.assertEqual((window["to"] - window["from"]).days + 1, 7)

    def test_today_is_one_day_and_yesterday_is_the_one_before(self) -> None:
        today = reports.resolve_window("today", now=NOW)
        self.assertEqual(today["from"], today["to"])
        self.assertEqual(today["from"], NOW.date())
        yesterday = reports.resolve_window("yesterday", now=NOW)
        self.assertEqual(yesterday["from"], NOW.date() - timedelta(days=1))

    def test_last_month_is_the_whole_of_the_month_before(self) -> None:
        window = reports.resolve_window("last-month", now=NOW)
        self.assertEqual(window["from"], date(2026, 6, 1))
        self.assertEqual(window["to"], date(2026, 6, 30))

    def test_a_window_running_backwards_is_refused(self) -> None:
        with self.assertRaises(reports.ReportRefused) as caught:
            reports.resolve_window("between", "2026-07-10", "2026-07-01", now=NOW)
        self.assertIn("backwards", str(caught.exception))

    def test_a_date_that_is_not_one_is_refused_by_what_was_typed(self) -> None:
        with self.assertRaises(reports.ReportRefused) as caught:
            reports.resolve_window("between", "the tenth", "2026-07-11", now=NOW)
        self.assertIn("the tenth", str(caught.exception))

    def test_an_absent_date_says_which_end_is_missing(self) -> None:
        with self.assertRaises(reports.ReportRefused) as caught:
            reports.resolve_window("between", "", "2026-07-11", now=NOW)
        self.assertIn("first day", str(caught.exception))


class WhatTheSummarySaysTests(unittest.TestCase):
    def setUp(self) -> None:
        base = NOW - timedelta(days=1)
        # Ten calls: six answered with ninety seconds of talk each, two no
        # answer, one busy, one failed. Every figure below is known by hand.
        self.records = (
            [call(base + timedelta(minutes=i), talk=90, duration=100) for i in range(6)]
            + [call(base + timedelta(minutes=10 + i), disposition="NO ANSWER",
                    talk=0, duration=20) for i in range(2)]
            + [call(base + timedelta(minutes=20), disposition="BUSY", talk=0, duration=5)]
            + [call(base + timedelta(minutes=21), disposition="FAILED", talk=0, duration=3)]
        )
        self.report = report_over(self.records)

    def figure(self, key: str) -> int:
        return self.report["summary"][key]["count"]

    def test_the_counts_are_what_was_put_in(self) -> None:
        self.assertEqual(self.figure("calls"), 10)
        self.assertEqual(self.figure("answered"), 6)
        self.assertEqual(self.figure("no_answer"), 2)
        self.assertEqual(self.figure("busy"), 1)
        self.assertEqual(self.figure("failed"), 1)

    def test_missed_is_everything_that_was_not_answered(self) -> None:
        """However it failed to be, so the two always sum to the calls."""
        self.assertEqual(self.figure("missed"), 4)
        self.assertEqual(self.figure("answered") + self.figure("missed"),
                         self.figure("calls"))

    def test_the_answer_rate_is_a_share_of_a_hundred(self) -> None:
        self.assertEqual(self.figure("answer_ratio"), 60.0)
        self.assertIn("in every hundred",
                      self.report["summary"]["answer_ratio"]["text"])

    def test_the_average_conversation_counts_only_answered_calls(self) -> None:
        """The whole point of the measure.

        Six answered calls of ninety seconds is ninety seconds each. Dividing
        the same talk time across all ten calls would give fifty-four, which is
        a number about a system nobody is running.
        """
        self.assertEqual(self.figure("conversation"), 6 * 90)
        self.assertEqual(self.figure("average_conversation"), 90)
        self.assertNotEqual(self.figure("average_conversation"), 54)

    def test_the_longest_call_is_the_longest_conversation(self) -> None:
        self.assertEqual(self.figure("longest_call"), 90)

    def test_ringing_is_what_is_left_of_a_call_after_the_talking(self) -> None:
        # Six answered: ten seconds of ring each. Two no answer at twenty, one
        # busy at five, one failed at three.
        self.assertEqual(self.figure("ringing"), 6 * 10 + 2 * 20 + 5 + 3)

    def test_every_figure_is_carried_as_a_number_and_as_words(self) -> None:
        for key, figure in self.report["summary"].items():
            self.assertIn("count", figure, key)
            self.assertIn("text", figure, key)
            self.assertTrue(figure["text"], key)

    def test_the_words_are_the_ones_the_appliance_spells_elsewhere(self) -> None:
        self.assertEqual(
            self.report["summary"]["calls"]["text"], numerals.spell_integer(10)
        )
        self.assertEqual(
            self.report["summary"]["average_conversation"]["text"],
            numerals.spell_duration(90),
        )

    def test_a_call_outside_the_window_is_not_counted(self) -> None:
        stale = call(NOW - timedelta(days=400))
        widened = report_over(self.records + [stale])
        self.assertEqual(widened["summary"]["calls"]["count"], 10)
        everything = report_over(self.records + [stale], "everything")
        self.assertEqual(everything["summary"]["calls"]["count"], 11)


class WhatTheBreakdownsSayTests(unittest.TestCase):
    def setUp(self) -> None:
        base = NOW.replace(hour=9, minute=0) - timedelta(days=1)
        self.records = [
            # Two answered calls in from the trunk to extension 201.
            call(base, source="+441632960111", destination="201", context="from-trunk"),
            call(base + timedelta(minutes=1), source="+441632960111",
                 destination="201", context="from-trunk"),
            # One missed call in to 202, in a different hour.
            call(base + timedelta(hours=3), source="+441632960222",
                 destination="202", context="from-trunk",
                 disposition="NO ANSWER", talk=0),
            # One call out from 201.
            call(base + timedelta(hours=3, minutes=5), source="201",
                 destination="+441632960999", context="outbound"),
        ]
        self.report = report_over(self.records)

    def rows(self, breakdown: str) -> dict[str, dict]:
        return {row["key"]: row for row in self.report["breakdowns"][breakdown]}

    def test_an_extension_is_counted_on_whichever_end_it_was(self) -> None:
        rows = self.rows("by_extension")
        self.assertEqual(rows["201"]["figures"]["calls"]["count"], 3)
        self.assertEqual(rows["202"]["figures"]["calls"]["count"], 1)

    def test_an_extension_carries_the_name_it_was_given(self) -> None:
        self.assertEqual(self.rows("by_extension")["201"]["label"], "reception")

    def test_direction_comes_from_the_dialplan_context(self) -> None:
        rows = self.rows("by_extension")
        self.assertEqual(rows["201"]["figures"]["inbound"]["count"], 2)
        self.assertEqual(rows["201"]["figures"]["outbound"]["count"], 1)

    def test_an_extension_is_never_listed_as_a_destination(self) -> None:
        """That table is for numbers off this machine; the extensions have one."""
        self.assertNotIn("201", self.rows("by_destination"))
        self.assertIn("+441632960999", self.rows("by_destination"))

    def test_every_hour_of_the_day_has_a_row_even_when_it_carried_nothing(self) -> None:
        """A chart with gaps in it is a chart nobody can read an hour off."""
        hours = self.report["breakdowns"]["by_hour"]
        self.assertEqual(len(hours), 24)
        self.assertEqual([row["key"] for row in hours],
                         [f"{hour:02d}" for hour in range(24)])
        self.assertEqual(self.rows("by_hour")["09"]["figures"]["calls"]["count"], 2)
        self.assertEqual(self.rows("by_hour")["12"]["figures"]["calls"]["count"], 2)

    def test_a_day_that_carried_nothing_is_not_invented(self) -> None:
        days = self.report["breakdowns"]["by_day"]
        self.assertEqual(len(days), 1)

    def test_the_outcomes_add_up_to_the_calls(self) -> None:
        total = sum(
            row["figures"]["calls"]["count"]
            for row in self.report["breakdowns"]["by_disposition"]
        )
        self.assertEqual(total, self.report["summary"]["calls"]["count"])

    def test_the_hours_add_up_to_the_calls(self) -> None:
        total = sum(
            row["figures"]["calls"]["count"]
            for row in self.report["breakdowns"]["by_hour"]
        )
        self.assertEqual(total, self.report["summary"]["calls"]["count"])

    def test_the_days_add_up_to_the_calls(self) -> None:
        total = sum(
            row["figures"]["calls"]["count"]
            for row in self.report["breakdowns"]["by_day"]
        )
        self.assertEqual(total, self.report["summary"]["calls"]["count"])

    def test_every_row_carries_every_column_the_report_declares(self) -> None:
        """A heading with no figure under it is an empty cell nobody can explain."""
        for breakdown, rows in self.report["breakdowns"].items():
            which = "extension" if breakdown == "by_extension" else "standard"
            for column in self.report["columns"][which]:
                for row in rows:
                    self.assertIn(column["key"], row["figures"],
                                  f"{breakdown} is missing {column['key']}")

    def test_a_busiest_hour_can_be_found_from_the_hours_alone(self) -> None:
        """What the chart's own sentence is drawn from."""
        busiest = max(self.report["breakdowns"]["by_hour"],
                      key=lambda row: row["figures"]["calls"]["count"])
        self.assertIn(busiest["key"], ("09", "12"))
        self.assertTrue(busiest["label"])


class WhatAnExportCarriesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.records = [
            call(NOW - timedelta(days=1), source="201", destination="202",
                 context="internal"),
        ]
        self.report = report_over(self.records)

    def test_an_exported_report_carries_digits_rather_than_words(self) -> None:
        """A spreadsheet cannot sum a word, which is the point of exporting."""
        payload, _ = reports.export_csv(self.report, "by_extension")
        self.assertIn(",1,", payload)
        self.assertNotIn("one hundred", payload)

    def test_the_export_names_the_period_it_covers(self) -> None:
        payload, name = reports.export_csv(self.report, "by_extension")
        self.assertIn("window:", payload)
        self.assertIn(self.report["window"]["from"], name)
        self.assertTrue(name.endswith(".csv"))
        self.assertTrue(name.startswith("crossbar-"))

    def test_the_export_headings_are_the_report_headings(self) -> None:
        payload, _ = reports.export_csv(self.report, "by_extension")
        for column in self.report["columns"]["extension"]:
            self.assertIn(column["heading"], payload)

    def test_a_breakdown_that_does_not_exist_is_refused_by_name(self) -> None:
        with self.assertRaises(reports.ReportRefused) as caught:
            reports.export_csv(self.report, "by_astrologer")
        self.assertIn("by_astrologer", str(caught.exception))
        self.assertIn("by_extension", str(caught.exception))

    def test_the_calls_themselves_export_as_the_engine_recorded_them(self) -> None:
        window = reports.resolve_window("last-thirty-days", now=NOW)
        payload, name = reports.export_records_csv(self.records, window)
        self.assertIn("201", payload)
        self.assertIn("202", payload)
        self.assertIn("ANSWERED", payload)
        self.assertIn("crossbar-calls-", name)

    def test_an_exported_call_outside_the_window_is_left_out(self) -> None:
        window = reports.resolve_window("today", now=NOW)
        payload, _ = reports.export_records_csv(self.records, window)
        self.assertNotIn("ANSWERED", payload)


class WhenThereIsNothingToReportOnTests(unittest.TestCase):
    def test_an_empty_period_still_produces_every_shape_the_console_draws(self) -> None:
        """A console that has to guard every field is a console with holes in it."""
        report = report_over([])
        self.assertTrue(report["available"])
        self.assertEqual(report["summary"]["calls"]["count"], 0)
        self.assertEqual(report["summary"]["answer_ratio"]["count"], 0)
        self.assertEqual(len(report["breakdowns"]["by_hour"]), 24)
        self.assertEqual(report["breakdowns"]["by_day"], [])

    def test_an_unavailable_report_says_why_and_draws_nothing(self) -> None:
        report = reports.unavailable("the engine writes no call records")
        self.assertFalse(report["available"])
        self.assertIn("no call records", report["explanation"])
        self.assertEqual(report["breakdowns"], {})

    def test_a_record_with_an_unreadable_time_is_left_out_rather_than_guessed(self) -> None:
        broken = call(NOW - timedelta(days=1))
        broken["started_at"] = "sometime on Tuesday"
        report = report_over([broken])
        self.assertEqual(report["summary"]["calls"]["count"], 0)

    def test_a_disposition_the_engine_invented_is_still_counted(self) -> None:
        """Under its own name, rather than dropped into a bucket that hides it."""
        odd = call(NOW - timedelta(days=1), disposition="TRANSFERRED")
        report = report_over([odd])
        keys = [row["key"] for row in report["breakdowns"]["by_disposition"]]
        self.assertIn("TRANSFERRED", keys)
        self.assertEqual(report["summary"]["calls"]["count"], 1)


class WhatTheReaderHandsTheReportTests(unittest.TestCase):
    """The sweep, which is the half of reporting that touches the disk."""

    def setUp(self) -> None:
        import tempfile
        self.workspace = tempfile.TemporaryDirectory()
        self.addCleanup(self.workspace.cleanup)

    def _reader(self, rows: list[str]) -> CallRecordReader:
        from pathlib import Path
        path = Path(self.workspace.name) / "Master.csv"
        path.write_text("\n".join(rows) + "\n", encoding="utf-8")
        return CallRecordReader(path=path.name, root=self.workspace.name)

    @staticmethod
    def _row(source: str = "201", destination: str = "202",
             disposition: str = "ANSWERED") -> str:
        return (
            f'"","{source}","{destination}","internal","{source}",'
            f'"SIP/{source}","SIP/{destination}","Dial","",'
            f'"2026-07-27 09:00:00","2026-07-27 09:00:05","2026-07-27 09:01:00",'
            f'"60","55","{disposition}","3","1753600000.1",""'
        )

    def test_a_swept_record_carries_numbers_rather_than_words(self) -> None:
        reader = self._reader([self._row()])
        records, truncated = reader.sweep()
        self.assertFalse(truncated)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["duration"], 60)
        self.assertEqual(records[0]["billable_seconds"], 55)
        self.assertEqual(records[0]["source"], "201")
        self.assertEqual(records[0]["context"], "internal")

    def test_the_sweep_and_the_history_agree_about_the_same_line(self) -> None:
        """One parsing, so a figure and a row can never come from two."""
        reader = self._reader([self._row()])
        swept = reader.sweep()[0][0]
        shown = reader.read(limit=10)["records"][0]
        self.assertEqual(swept["source"], shown["source"])
        self.assertEqual(swept["destination"], shown["destination"])
        self.assertEqual(shown["duration"], numerals.spell_duration(swept["duration"]))

    def test_a_sweep_that_hit_its_ceiling_says_so(self) -> None:
        reader = self._reader([self._row() for _ in range(20)])
        reader.SWEEP_LIMIT = 5
        records, truncated = reader.sweep()
        self.assertEqual(len(records), 5)
        self.assertTrue(truncated)

    def test_a_report_drawn_from_a_truncated_sweep_carries_the_warning(self) -> None:
        """Silently reporting on part of a period is the worst failure here."""
        window = reports.resolve_window("everything", now=NOW)
        report = reports.build_report([], window, truncated=True)
        self.assertTrue(report["truncated"])
        self.assertIn("narrow the window", report["truncation_note"])

    def test_an_absent_file_sweeps_to_nothing_rather_than_raising(self) -> None:
        reader = CallRecordReader(path="nowhere.csv", root=self.workspace.name)
        self.assertEqual(reader.sweep(), ([], False))


class WhatEndedInAMailboxTests(unittest.TestCase):
    """A call that rang out and a call that left a message are both unanswered.

    They are not the same thing -- one reached somebody's attention and the
    other did not -- and the only place the difference survives is the last
    application the engine ran.
    """

    def setUp(self) -> None:
        base = NOW - timedelta(days=1)
        self.records = [
            call(base, source="+441632960111", destination="201",
                 context="from-trunk", disposition="NO ANSWER", talk=0,
                 application="VoiceMail"),
            call(base + timedelta(minutes=1), source="+441632960111",
                 destination="201", context="from-trunk",
                 disposition="NO ANSWER", talk=0, application="Dial"),
        ]
        self.report = report_over(self.records)

    def test_only_the_call_that_reached_a_mailbox_is_counted(self) -> None:
        self.assertEqual(self.report["summary"]["voicemail"]["count"], 1)
        self.assertEqual(self.report["summary"]["missed"]["count"], 2)

    def test_the_extension_carries_its_own_count(self) -> None:
        rows = {row["key"]: row for row in self.report["breakdowns"]["by_extension"]}
        self.assertEqual(rows["201"]["figures"]["voicemail"]["count"], 1)

    def test_the_application_is_matched_however_the_engine_capitalised_it(self) -> None:
        odd = call(NOW - timedelta(days=1), destination="201",
                   context="from-trunk", disposition="NO ANSWER", talk=0,
                   application="voicemail")
        self.assertEqual(report_over([odd])["summary"]["voicemail"]["count"], 1)

    def test_an_answered_call_is_never_counted_as_a_message(self) -> None:
        answered = call(NOW - timedelta(days=1), destination="201",
                        context="from-trunk", application="Dial")
        self.assertEqual(report_over([answered])["summary"]["voicemail"]["count"], 0)

    def test_the_reader_carries_the_application_through_from_the_file(self) -> None:
        """Which is the field this whole count rests on."""
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as workspace:
            path = Path(workspace) / "Master.csv"
            path.write_text(
                '"","+441632960111","201","from-trunk","+441632960111",'
                '"PJSIP/carrier","PJSIP/201","VoiceMail","201@default,u",'
                '"2026-07-27 09:00:00","","2026-07-27 09:00:30",'
                '"30","0","NO ANSWER","3","1753600000.1",""\n',
                encoding="utf-8",
            )
            reader = CallRecordReader(path="Master.csv", root=workspace)
            self.assertEqual(reader.sweep()[0][0]["application"], "VoiceMail")


if __name__ == "__main__":
    unittest.main()
