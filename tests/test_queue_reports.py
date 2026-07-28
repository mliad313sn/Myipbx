"""What happened to the people waiting.

A call that waited four minutes in a queue and then gave up appears in the call
detail records as one unanswered call, and nothing in that record says it
waited. Everything here comes out of the engine's second file, and the
arithmetic is checked against events constructed so the right answer is known
by hand.
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from appliance import reports
from appliance.queuelog import QueueLogReader

NOW = datetime(2026, 7, 28, 12, 0, 0)
YESTERDAY = NOW - timedelta(days=1)

QUEUES = [
    {"number": "700", "name": "the switchboard", "service_level_seconds": 20},
    {"number": "701", "name": "out of hours", "service_level_seconds": 60},
]


def event(
    name: str,
    at: datetime = YESTERDAY,
    queue: str = "700",
    member: str = "",
    waited: int = 0,
    talked: int = 0,
    identifier: str = "1753600000.1",
) -> dict:
    return {
        "at": at, "identifier": identifier, "queue": queue, "member": member,
        "event": name, "waited": waited, "talked": talked, "position": 0,
    }


def report_over(events, window_name="last-thirty-days"):
    window = reports.resolve_window(window_name, now=NOW)
    return reports.build_queue_report(events, window, queues=QUEUES)


class WhatTheQueueLogSaysTests(unittest.TestCase):
    """Reading the file, before anything is counted from it."""

    def setUp(self) -> None:
        self.workspace = tempfile.TemporaryDirectory()
        self.addCleanup(self.workspace.cleanup)

    def _reader(self, rows: list[str]) -> QueueLogReader:
        path = Path(self.workspace.name) / "queue_log"
        path.write_text("\n".join(rows) + "\n", encoding="utf-8")
        return QueueLogReader(path=path.name, root=self.workspace.name)

    def test_a_join_is_read_with_its_queue(self) -> None:
        reader = self._reader(["1784160000|1.1|700|NONE|ENTERQUEUE||441632960111|1"])
        events, truncated = reader.sweep()
        self.assertFalse(truncated)
        self.assertEqual(events[0]["event"], "ENTERQUEUE")
        self.assertEqual(events[0]["queue"], "700")

    def test_a_connect_carries_how_long_the_caller_waited(self) -> None:
        """The first field, which is the hold time, not the ring time."""
        reader = self._reader(["1784160000|1.1|700|PJSIP/201|CONNECT|14|1.2|3"])
        event_read = reader.sweep()[0][0]
        self.assertEqual(event_read["waited"], 14)
        self.assertEqual(event_read["member"], "PJSIP/201")

    def test_an_abandon_carries_the_wait_in_its_third_field(self) -> None:
        """Which is a different position from the one CONNECT uses.

        Read positionally at each call site this is exactly the kind of thing
        that comes out as a position number reported as a duration.
        """
        reader = self._reader(["1784160000|1.1|700|NONE|ABANDON|1|1|97"])
        event_read = reader.sweep()[0][0]
        self.assertEqual(event_read["waited"], 97)
        self.assertEqual(event_read["position"], 1)

    def test_a_completion_carries_both_the_wait_and_the_conversation(self) -> None:
        reader = self._reader(["1784160000|1.1|700|PJSIP/201|COMPLETEAGENT|12|340|1"])
        event_read = reader.sweep()[0][0]
        self.assertEqual(event_read["waited"], 12)
        self.assertEqual(event_read["talked"], 340)

    def test_a_line_that_is_not_an_event_is_left_out(self) -> None:
        reader = self._reader(["not a queue log line", "1784160000|1.1|700|NONE|ENTERQUEUE"])
        events, _ = reader.sweep()
        self.assertEqual(len(events), 1)

    def test_a_line_with_an_unreadable_time_is_left_out(self) -> None:
        reader = self._reader(["whenever|1.1|700|NONE|ENTERQUEUE||"])
        self.assertEqual(reader.sweep()[0], [])

    def test_a_sweep_that_hit_its_ceiling_says_so(self) -> None:
        reader = self._reader(["1784160000|1.1|700|NONE|ENTERQUEUE||"] * 12)
        reader.SWEEP_LIMIT = 4
        events, truncated = reader.sweep()
        self.assertEqual(len(events), 4)
        self.assertTrue(truncated)

    def test_an_absent_file_reads_as_nothing_rather_than_raising(self) -> None:
        reader = QueueLogReader(path="nowhere", root=self.workspace.name)
        self.assertFalse(reader.available())
        self.assertEqual(reader.sweep(), ([], False))


class WhatTheQueueReportSaysTests(unittest.TestCase):
    def setUp(self) -> None:
        # Ten callers joined. Six answered -- four inside twenty seconds, two
        # after. Three gave up, one timed out. Every figure below is known.
        events = []
        for index in range(10):
            events.append(event("ENTERQUEUE", at=YESTERDAY + timedelta(minutes=index)))
        for index in range(4):
            events.append(event("CONNECT", member="PJSIP/201", waited=10))
            events.append(event("COMPLETEAGENT", member="PJSIP/201", waited=10, talked=120))
        for index in range(2):
            events.append(event("CONNECT", member="PJSIP/202", waited=40))
            events.append(event("COMPLETECALLER", member="PJSIP/202", waited=40, talked=60))
        for index in range(3):
            events.append(event("ABANDON", waited=75))
        events.append(event("EXITWITHTIMEOUT", waited=90))
        events.append(event("RINGNOANSWER", member="PJSIP/202"))
        self.report = report_over(events)

    def figure(self, key: str) -> float:
        return self.report["summary"][key]["count"]

    def test_offered_is_everybody_who_joined(self) -> None:
        self.assertEqual(self.figure("offered"), 10)
        self.assertEqual(self.report["considered"]["count"], 10)

    def test_answered_and_abandoned_account_for_everybody(self) -> None:
        self.assertEqual(self.figure("answered"), 6)
        self.assertEqual(self.figure("abandoned"), 4)
        self.assertEqual(self.figure("answered") + self.figure("abandoned"),
                         self.figure("offered"))

    def test_the_service_level_counts_only_the_answered_calls(self) -> None:
        """Four of six answered inside the queue's own twenty seconds.

        Measured against the answered calls, and the heading says so, because
        against everything offered the same queue reads forty rather than
        sixty-seven and both numbers are defensible.
        """
        self.assertEqual(self.figure("service_level"), round(4 / 6 * 100, 1))

    def test_the_answer_rate_is_measured_against_everybody_offered(self) -> None:
        self.assertEqual(self.figure("answer_ratio"), 60.0)

    def test_the_average_wait_counts_the_people_who_gave_up(self) -> None:
        """They waited longest, and leaving them out flatters the queue."""
        total = 4 * 10 + 2 * 40 + 3 * 75 + 90
        self.assertEqual(self.figure("average_wait"), round(total / 10))

    def test_the_longest_wait_is_the_longest_anybody_waited(self) -> None:
        self.assertEqual(self.figure("longest_wait"), 90)

    def test_the_average_conversation_is_over_the_answered_calls(self) -> None:
        self.assertEqual(self.figure("average_conversation"),
                         round((4 * 120 + 2 * 60) / 6))

    def test_a_member_is_named_by_extension_not_by_channel(self) -> None:
        """Which is what the rest of the console calls that person."""
        members = {row["key"] for row in self.report["breakdowns"]["by_member"]}
        self.assertEqual(members, {"201", "202"})

    def test_a_member_who_did_not_pick_up_is_counted_separately(self) -> None:
        rows = {row["key"]: row for row in self.report["breakdowns"]["by_member"]}
        self.assertEqual(rows["202"]["figures"]["no_answer"]["count"], 1)
        self.assertEqual(rows["202"]["figures"]["answered"]["count"], 2)
        self.assertEqual(rows["201"]["figures"]["no_answer"]["count"], 0)

    def test_the_reasons_people_stopped_waiting_are_kept_apart(self) -> None:
        """A queue nobody staffs and a queue people give up on differ."""
        reasons = {row["key"]: row for row in self.report["breakdowns"]["by_reason"]}
        self.assertEqual(reasons["ABANDON"]["figures"]["calls"]["count"], 3)
        self.assertEqual(reasons["EXITWITHTIMEOUT"]["figures"]["calls"]["count"], 1)
        self.assertIn("hung up", reasons["ABANDON"]["label"])

    def test_the_queue_carries_the_name_it_was_given(self) -> None:
        rows = {row["key"]: row for row in self.report["breakdowns"]["by_queue"]}
        self.assertEqual(rows["700"]["label"], "the switchboard")


class WhatTheServiceLevelIsMeasuredAgainstTests(unittest.TestCase):
    def test_each_queue_is_held_to_its_own_promise(self) -> None:
        """A switchboard and an out-of-hours line are not the same promise."""
        events = [
            event("ENTERQUEUE", queue="700"),
            event("CONNECT", queue="700", member="PJSIP/201", waited=45),
            event("ENTERQUEUE", queue="701"),
            event("CONNECT", queue="701", member="PJSIP/201", waited=45),
        ]
        rows = {row["key"]: row for row in report_over(events)["breakdowns"]["by_queue"]}
        # Forty-five seconds misses the switchboard's twenty and meets the
        # out-of-hours line's sixty.
        self.assertEqual(rows["700"]["figures"]["service_level"]["count"], 0.0)
        self.assertEqual(rows["701"]["figures"]["service_level"]["count"], 100.0)

    def test_a_queue_with_no_configured_promise_falls_back_to_the_default(self) -> None:
        events = [
            event("ENTERQUEUE", queue="999"),
            event("CONNECT", queue="999", member="PJSIP/201", waited=5),
        ]
        rows = {row["key"]: row for row in report_over(events)["breakdowns"]["by_queue"]}
        self.assertEqual(rows["999"]["figures"]["service_level"]["count"], 100.0)


class WhenThereAreNoQueuesTests(unittest.TestCase):
    def test_an_empty_period_produces_every_shape_the_console_draws(self) -> None:
        report = report_over([])
        self.assertTrue(report["available"])
        self.assertEqual(report["summary"]["offered"]["count"], 0)
        self.assertEqual(report["breakdowns"]["by_queue"], [])
        self.assertEqual(report["breakdowns"]["by_member"], [])

    def test_an_unavailable_report_says_why(self) -> None:
        report = reports.queue_unavailable("the engine writes no queue log")
        self.assertFalse(report["available"])
        self.assertIn("queue log", report["explanation"])

    def test_an_event_outside_the_window_is_not_counted(self) -> None:
        stale = event("ENTERQUEUE", at=NOW - timedelta(days=400))
        self.assertEqual(report_over([stale])["summary"]["offered"]["count"], 0)
        self.assertEqual(
            report_over([stale], "everything")["summary"]["offered"]["count"], 1
        )


class WhatAQueueExportCarriesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.report = report_over([
            event("ENTERQUEUE"),
            event("CONNECT", member="PJSIP/201", waited=8),
            event("COMPLETEAGENT", member="PJSIP/201", waited=8, talked=95),
        ])

    def test_a_queue_breakdown_exports_in_digits(self) -> None:
        payload, name = reports.export_queue_csv(self.report, "by_queue")
        self.assertIn("700", payload)
        self.assertIn("the switchboard", payload)
        self.assertNotIn("one hundred", payload)
        self.assertIn("crossbar-queues-by-queue", name)

    def test_a_member_breakdown_exports_with_its_own_columns(self) -> None:
        payload, _ = reports.export_queue_csv(self.report, "by_member")
        for column in self.report["columns"]["member"]:
            self.assertIn(column["heading"], payload)

    def test_a_breakdown_that_does_not_exist_is_refused_by_name(self) -> None:
        with self.assertRaises(reports.ReportRefused) as caught:
            reports.export_queue_csv(self.report, "by_astrologer")
        self.assertIn("by_astrologer", str(caught.exception))


class WhereTheEngineAndTheReportHaveToAgreeTests(unittest.TestCase):
    def test_the_queue_configuration_carries_the_promise_the_report_measures(self) -> None:
        """Otherwise each would have its own idea of what "in time" means."""
        from appliance import confstore
        rendered = confstore.render_queues({
            "queues": [{
                "number": "700", "name": "the switchboard", "members": ["201"],
                "strategy": "ring all", "ring_seconds": 20,
                "service_level_seconds": 45, "enabled": True,
            }]
        })
        self.assertIn("servicelevel = 45", rendered)

    def test_the_queue_entity_offers_the_field_at_all(self) -> None:
        from appliance import entities
        field = entities.ENTITY_SPECS["queues"].field("service_level_seconds")
        self.assertIsNotNone(field)
        self.assertEqual(field.default, 20)


if __name__ == "__main__":
    unittest.main()
