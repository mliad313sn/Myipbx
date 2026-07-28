"""The reporting routes, over real transport, against a real record file.

The arithmetic is tested in ``test_reports``. What is tested here is the part
that only breaks once it is wired up: that the routes exist, that they refuse an
anonymous request like every other route, that the export arrives as a file with
a name on it, and that the export route does not get swallowed by the route next
to it -- which is exactly what happened the first time it was declared.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from support import ApplianceHarness, TEST_PASSWORD, TEST_USERNAME  # noqa: F401

DOCUMENT = {
    "revision": 1,
    "site": {"name": "an example site", "timezone": "UTC"},
    "trunks": [],
    "extensions": [
        {"number": "201", "name": "reception", "technology": "PJSIP",
         "context": "internal", "ring_seconds": 20},
        {"number": "202", "name": "workshop", "technology": "PJSIP",
         "context": "internal", "ring_seconds": 20},
    ],
    "ring_groups": [],
    "inbound_routes": [],
    "outbound_routes": [],
    "time_conditions": [],
    "ivr_menus": [],
    "queues": [],
    "conferences": [],
    "firewall_rules": [],
    "dialplan": {"inbound_context": "from-trunk", "internal_context": "internal"},
    "hardware": {"spans": []},
}


def record(
    started: str,
    source: str = "+441632960111",
    destination: str = "201",
    disposition: str = "ANSWERED",
    context: str = "from-trunk",
    duration: int = 60,
    talk: int = 45,
) -> str:
    return (
        f'"","{source}","{destination}","{context}","{source}",'
        f'"PJSIP/{source}","PJSIP/{destination}","Dial","",'
        f'"{started}","{started}","{started}",'
        f'"{duration}","{talk}","{disposition}","3","1753600000.1",""'
    )


class ReportingRouteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.harness = ApplianceHarness()
        self.harness.write_document(DOCUMENT)

        # A record file the appliance can actually read, holding calls whose
        # figures are known by hand.
        self.records_path = Path(self.harness.root) / "Master.csv"
        self.records_path.write_text("\n".join([
            record("2026-07-20 09:15:00"),
            record("2026-07-20 09:40:00", destination="202"),
            record("2026-07-20 14:05:00", disposition="NO ANSWER", talk=0),
            record("2026-07-20 14:10:00", source="201",
                   destination="+441632960999", context="outbound"),
        ]) + "\n", encoding="utf-8")
        self.harness.config.call_record_file = str(self.records_path)

        await self.harness.start()
        await self.harness.sign_in()

    async def asyncTearDown(self) -> None:
        await self.harness.stop()

    async def report(self, query: str = "window=everything"):
        status, _, payload = await self.harness.request("GET", f"/api/reports?{query}")
        self.assertEqual(status, 200, payload)
        return payload

    # -- the report --------------------------------------------------------

    async def test_a_report_is_served_over_the_whole_record_file(self) -> None:
        payload = await self.report()
        self.assertTrue(payload["available"])
        self.assertEqual(payload["summary"]["calls"]["count"], 4)
        self.assertEqual(payload["summary"]["answered"]["count"], 3)
        self.assertEqual(payload["considered"]["count"], 4)

    async def test_the_window_is_named_back_in_the_response(self) -> None:
        """The console prints this, so it has to say what was actually asked."""
        payload = await self.report("window=today")
        self.assertIn("today", payload["window"]["label"])
        self.assertEqual(payload["window"]["name"], "today")

    async def test_a_narrow_window_leaves_the_older_calls_out(self) -> None:
        payload = await self.report("window=between&from=2026-07-20&to=2026-07-20")
        self.assertEqual(payload["summary"]["calls"]["count"], 4)
        payload = await self.report("window=between&from=2026-07-21&to=2026-07-22")
        self.assertEqual(payload["summary"]["calls"]["count"], 0)

    async def test_the_configured_extensions_are_the_ones_reported_on(self) -> None:
        payload = await self.report()
        keys = {row["key"] for row in payload["breakdowns"]["by_extension"]}
        self.assertEqual(keys, {"201", "202"})
        names = {row["key"]: row["label"] for row in payload["breakdowns"]["by_extension"]}
        self.assertEqual(names["201"], "reception")

    async def test_the_dialplan_decides_direction(self) -> None:
        """Read from the document the appliance wrote, not guessed from numbers."""
        payload = await self.report()
        self.assertEqual(payload["summary"]["inbound"]["count"], 3)
        self.assertEqual(payload["summary"]["outbound"]["count"], 1)

    async def test_a_window_that_does_not_exist_is_refused_with_a_reason(self) -> None:
        status, _, payload = await self.harness.request(
            "GET", "/api/reports?window=last-fortnight"
        )
        self.assertEqual(status, 422)
        self.assertIn("last-fortnight", payload["error"])

    async def test_a_backwards_window_is_refused_rather_than_reported_as_empty(self) -> None:
        status, _, payload = await self.harness.request(
            "GET", "/api/reports?window=between&from=2026-07-20&to=2026-07-01"
        )
        self.assertEqual(status, 422)
        self.assertIn("backwards", payload["error"])

    # -- the exports -------------------------------------------------------

    async def test_a_breakdown_downloads_as_a_named_file(self) -> None:
        status, headers, payload = await self.harness.request(
            "GET", "/api/reports/export?window=everything&breakdown=by_extension"
        )
        self.assertEqual(status, 200)
        self.assertIn("text/csv", headers["content-type"])
        self.assertIn("attachment", headers["content-disposition"])
        self.assertIn("crossbar-by-extension", headers["content-disposition"])
        body = payload.decode("utf-8")
        self.assertIn("201", body)
        self.assertIn("reception", body)

    async def test_the_calls_themselves_download_when_no_breakdown_is_named(self) -> None:
        status, headers, payload = await self.harness.request(
            "GET", "/api/reports/export?window=everything"
        )
        self.assertEqual(status, 200)
        self.assertIn("crossbar-calls-", headers["content-disposition"])
        self.assertIn(b"ANSWERED", payload)

    async def test_an_exported_report_carries_digits(self) -> None:
        """The one place the spelling rule is deliberately not applied."""
        _, _, payload = await self.harness.request(
            "GET", "/api/reports/export?window=everything&breakdown=by_disposition"
        )
        self.assertRegex(payload.decode("utf-8"), r"ANSWERED,,3\b")

    async def test_a_breakdown_that_does_not_exist_is_refused(self) -> None:
        status, _, payload = await self.harness.request(
            "GET", "/api/reports/export?window=everything&breakdown=by_astrologer"
        )
        self.assertEqual(status, 422)
        self.assertIn("by_astrologer", payload["error"])

    async def test_an_entity_list_downloads_as_a_file(self) -> None:
        """And is not swallowed by the route that reads one record.

        ``/api/entities/{kind}/{key}`` was declared first and matched this,
        which arrived at the single record reader as a request for the record
        named "export".
        """
        status, headers, payload = await self.harness.request(
            "GET", "/api/entities/extensions/export"
        )
        self.assertEqual(status, 200)
        self.assertIn("text/csv", headers["content-type"])
        self.assertIn("crossbar-extensions.csv", headers["content-disposition"])
        body = payload.decode("utf-8")
        self.assertIn("201", body)
        self.assertIn("reception", body)

    async def test_reading_one_record_still_works_beside_the_export(self) -> None:
        status, _, payload = await self.harness.request(
            "GET", "/api/entities/extensions/201"
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["record"]["number"], "201")

    async def test_an_exported_list_never_carries_a_password(self) -> None:
        document = json.loads(json.dumps(DOCUMENT))
        document["extensions"][0]["password"] = "a-secret-nobody-should-export"
        self.harness.write_document(document)

        status, _, payload = await self.harness.request(
            "GET", "/api/entities/extensions/export"
        )
        self.assertEqual(status, 200)
        self.assertNotIn(b"a-secret-nobody-should-export", payload)

    async def test_an_unknown_kind_is_a_missing_route_not_an_empty_file(self) -> None:
        status, _, _ = await self.harness.request(
            "GET", "/api/entities/astrologers/export"
        )
        self.assertEqual(status, 404)

    # -- the guard ---------------------------------------------------------

    async def test_every_reporting_route_refuses_an_anonymous_request(self) -> None:
        for path in (
            "/api/reports?window=today",
            "/api/reports/export?window=today",
            "/api/entities/extensions/export",
        ):
            with self.subTest(path=path):
                status, _, _ = await self.harness.request("GET", path, cookie="")
                self.assertEqual(status, 401)

    async def test_producing_an_export_is_recorded_in_the_journal(self) -> None:
        """A read that leaves the appliance carrying the site with it."""
        await self.harness.request(
            "GET", "/api/reports/export?window=everything&breakdown=by_extension"
        )
        status, _, payload = await self.harness.request("GET", "/api/journal?limit=50")
        self.assertEqual(status, 200)
        targets = [entry.get("target", "") for entry in payload["entries"]]
        self.assertIn("/api/reports/export", targets)


class WhenTheEngineWritesNoRecordsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.harness = ApplianceHarness()
        self.harness.write_document(DOCUMENT)
        self.harness.config.call_record_file = str(
            Path(self.harness.root) / "there-is-no-such-file.csv"
        )
        await self.harness.start()
        await self.harness.sign_in()

    async def asyncTearDown(self) -> None:
        await self.harness.stop()

    async def test_the_report_says_why_rather_than_reporting_zero(self) -> None:
        """Zero calls and no records are different facts about a telephone system."""
        status, _, payload = await self.harness.request("GET", "/api/reports?window=today")
        self.assertEqual(status, 200)
        self.assertFalse(payload["available"])
        self.assertIn("call detail records", payload["explanation"])

    async def test_an_export_with_nothing_to_export_is_refused(self) -> None:
        status, _, payload = await self.harness.request(
            "GET", "/api/reports/export?window=today"
        )
        self.assertEqual(status, 409)
        self.assertIn("no call records", payload["error"])


class QueueReportingRouteTests(unittest.IsolatedAsyncioTestCase):
    """The queue report is a separate file and so a separate availability.

    A site with queues and no call record file still has a queue report, and a
    site with records and no queue has the rest; folding the two together would
    make each unavailable whenever the other was.
    """

    async def asyncSetUp(self) -> None:
        self.harness = ApplianceHarness()
        document = json.loads(json.dumps(DOCUMENT))
        document["queues"] = [{
            "number": "700", "name": "the switchboard", "members": ["201", "202"],
            "strategy": "ring all", "ring_seconds": 20,
            "service_level_seconds": 20, "enabled": True,
        }]
        self.harness.write_document(document)

        self.queue_log = Path(self.harness.root) / "queue_log"
        self.queue_log.write_text("\n".join([
            "1784160000|1.1|700|NONE|ENTERQUEUE||441632960111|1",
            "1784160010|1.1|700|PJSIP/201|CONNECT|10|1.2|3",
            "1784160130|1.1|700|PJSIP/201|COMPLETEAGENT|10|120|1",
            "1784160200|1.2|700|NONE|ENTERQUEUE||441632960222|1",
            "1784160290|1.2|700|NONE|ABANDON|1|1|90",
        ]) + "\n", encoding="utf-8")
        self.harness.config.queue_log_file = str(self.queue_log)

        # No call record file at all, deliberately.
        self.harness.config.call_record_file = str(
            Path(self.harness.root) / "there-is-no-such-file.csv"
        )

        await self.harness.start()
        await self.harness.sign_in()

    async def asyncTearDown(self) -> None:
        await self.harness.stop()

    async def test_the_queues_report_even_with_no_call_records(self) -> None:
        status, _, payload = await self.harness.request(
            "GET", "/api/reports/queues?window=everything"
        )
        self.assertEqual(status, 200)
        self.assertTrue(payload["available"])
        self.assertEqual(payload["summary"]["offered"]["count"], 2)
        self.assertEqual(payload["summary"]["answered"]["count"], 1)
        self.assertEqual(payload["summary"]["abandoned"]["count"], 1)

    async def test_the_call_report_is_unavailable_at_the_same_moment(self) -> None:
        _, _, payload = await self.harness.request("GET", "/api/reports?window=everything")
        self.assertFalse(payload["available"])

    async def test_the_queue_carries_the_name_and_promise_it_was_configured_with(self) -> None:
        _, _, payload = await self.harness.request(
            "GET", "/api/reports/queues?window=everything"
        )
        rows = {row["key"]: row for row in payload["breakdowns"]["by_queue"]}
        self.assertEqual(rows["700"]["label"], "the switchboard")
        # Answered in ten seconds, inside the queue's twenty.
        self.assertEqual(rows["700"]["figures"]["service_level"]["count"], 100.0)

    async def test_a_queue_breakdown_downloads_as_a_file(self) -> None:
        status, headers, payload = await self.harness.request(
            "GET", "/api/reports/export?window=everything&breakdown=queue:by_queue"
        )
        self.assertEqual(status, 200)
        self.assertIn("crossbar-queues-by-queue", headers["content-disposition"])
        self.assertIn(b"700", payload)

    async def test_a_bad_window_is_refused_here_too(self) -> None:
        status, _, payload = await self.harness.request(
            "GET", "/api/reports/queues?window=last-fortnight"
        )
        self.assertEqual(status, 422)
        self.assertIn("last-fortnight", payload["error"])

    async def test_the_route_refuses_an_anonymous_request(self) -> None:
        status, _, _ = await self.harness.request(
            "GET", "/api/reports/queues?window=today", cookie=""
        )
        self.assertEqual(status, 401)


class WhenThereIsNoQueueLogTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.harness = ApplianceHarness()
        self.harness.write_document(DOCUMENT)
        self.harness.config.queue_log_file = str(
            Path(self.harness.root) / "no-queue-log-here"
        )
        await self.harness.start()
        await self.harness.sign_in()

    async def asyncTearDown(self) -> None:
        await self.harness.stop()

    async def test_it_says_what_the_call_records_cannot_answer(self) -> None:
        status, _, payload = await self.harness.request(
            "GET", "/api/reports/queues?window=today"
        )
        self.assertEqual(status, 200)
        self.assertFalse(payload["available"])
        self.assertIn("queue log", payload["explanation"])
        self.assertIn("gave up", payload["explanation"])


class RecordingRouteTests(unittest.IsolatedAsyncioTestCase):
    """The one route that hands a file off the disk, over real transport."""

    GOOD = "20260727-091500_441632960111_201_1753600000.123456.wav"

    async def asyncSetUp(self) -> None:
        self.harness = ApplianceHarness()
        self.harness.write_document(DOCUMENT)

        self.directory = Path(self.harness.root) / "monitor"
        self.directory.mkdir()
        (self.directory / self.GOOD).write_bytes(b"RIFF....WAVEfmt ")
        self.secret = Path(self.harness.root) / "a-secret-outside-the-directory"
        self.secret.write_bytes(b"this must never be served")
        self.harness.config.recording_directory = str(self.directory)

        await self.harness.start()
        await self.harness.sign_in()

    async def asyncTearDown(self) -> None:
        await self.harness.stop()

    async def test_a_recording_is_listed_from_its_name_alone(self) -> None:
        status, _, payload = await self.harness.request("GET", "/api/recordings")
        self.assertEqual(status, 200)
        self.assertTrue(payload["available"])
        self.assertEqual(payload["records"][0]["destination"], "201")

    async def test_a_recording_is_served_as_audio(self) -> None:
        status, headers, payload = await self.harness.request(
            "GET", f"/api/recordings/{self.GOOD}"
        )
        self.assertEqual(status, 200)
        self.assertEqual(headers["content-type"], "audio/wav")
        self.assertIn("inline", headers["content-disposition"])
        self.assertEqual(payload, b"RIFF....WAVEfmt ")

    async def test_nothing_outside_the_directory_is_ever_served(self) -> None:
        for name in (
            "..%2Fa-secret-outside-the-directory",
            "..%2F..%2Fetc%2Fpasswd",
            "..%2F..%2F..%2Fetc%2Fshadow",
            "20260727-091500_1_2_1.1.conf",
            "20260727-091500_1_2_1.1.wav.php",
        ):
            with self.subTest(name=name):
                status, _, payload = await self.harness.request(
                    "GET", f"/api/recordings/{name}"
                )
                self.assertEqual(status, 404)
                self.assertNotIn(b"must never be served", str(payload).encode())
                self.assertNotIn(b"root:", str(payload).encode())

    async def test_every_name_this_route_refuses_is_refused_the_same_way(self) -> None:
        """A name that failed the pattern and a name naming a file that is not
        there must not be distinguishable from outside."""
        refusals = set()
        for name in (
            "20260727-091500_1_2_1.1.conf",     # not a shape this appliance writes
            "20260727-091500_1_2_9.9.wav",      # the right shape, no such file
            "notes.txt",                        # not a recording at all
        ):
            status, _, payload = await self.harness.request(
                "GET", f"/api/recordings/{name}"
            )
            self.assertEqual(status, 404, name)
            refusals.add(payload.get("error"))
        self.assertEqual(len(refusals), 1, refusals)

    async def test_the_route_refuses_an_anonymous_request(self) -> None:
        for path in ("/api/recordings", f"/api/recordings/{self.GOOD}"):
            with self.subTest(path=path):
                status, _, _ = await self.harness.request("GET", path, cookie="")
                self.assertEqual(status, 401)

    async def test_playing_a_recording_is_recorded_in_the_journal(self) -> None:
        """Somebody listened to a conversation. That is worth an entry."""
        await self.harness.request("GET", f"/api/recordings/{self.GOOD}")
        _, _, payload = await self.harness.request("GET", "/api/journal?limit=50")
        targets = [entry.get("target", "") for entry in payload["entries"]]
        self.assertIn("/api/recordings", targets)


class ScheduledReportTaskTests(unittest.IsolatedAsyncioTestCase):
    """The task that draws a report for itself, keeps it, and would send it."""

    async def asyncSetUp(self) -> None:
        self.harness = ApplianceHarness()
        document = json.loads(json.dumps(DOCUMENT))
        document["scheduled_reports"] = [{
            "name": "every-morning", "report": "by_extension",
            "period": "last-thirty-days", "frequency": "daily",
            "destination": "", "enabled": True,
        }, {
            "name": "goes-nowhere", "report": "by_day",
            "period": "last-thirty-days", "frequency": "daily",
            "destination": "a-destination-that-does-not-exist", "enabled": True,
        }, {
            "name": "paused", "report": "by_trunk",
            "period": "today", "frequency": "daily",
            "destination": "", "enabled": False,
        }]
        self.harness.write_document(document)

        self.records_path = Path(self.harness.root) / "Master.csv"
        self.records_path.write_text("\n".join([
            record("2026-07-20 09:15:00"),
            record("2026-07-20 09:40:00", destination="202"),
        ]) + "\n", encoding="utf-8")
        self.harness.config.call_record_file = str(self.records_path)

        await self.harness.start()
        await self.harness.sign_in()

    async def asyncTearDown(self) -> None:
        await self.harness.stop()

    async def run_task(self):
        status, _, payload = await self.harness.request(
            "POST", "/api/tasks/run", body=json.dumps({"name": "scheduled-reports"}),
            extra_headers={"Origin": f"http://127.0.0.1:{self.harness.port}"},
        )
        self.assertEqual(status, 200, payload)
        return payload

    async def test_a_due_report_is_drawn_and_kept(self) -> None:
        await self.run_task()
        _, _, payload = await self.harness.request("GET", "/api/reports/scheduled")
        names = [snapshot["name"] for snapshot in payload["snapshots"]]
        self.assertTrue(any(name.startswith("every-morning_") for name in names), names)

    async def test_a_report_is_kept_even_though_it_could_not_be_sent(self) -> None:
        """A mail server that is down costs a delivery, not the report."""
        await self.run_task()
        _, _, payload = await self.harness.request("GET", "/api/reports/scheduled")
        names = [snapshot["name"] for snapshot in payload["snapshots"]]
        self.assertTrue(any(name.startswith("goes-nowhere_") for name in names), names)

    async def test_a_disabled_report_is_not_drawn(self) -> None:
        await self.run_task()
        _, _, payload = await self.harness.request("GET", "/api/reports/scheduled")
        names = [snapshot["name"] for snapshot in payload["snapshots"]]
        self.assertFalse(any(name.startswith("paused_") for name in names), names)

    async def test_running_it_twice_in_a_day_draws_nothing_the_second_time(self) -> None:
        """Restarting the appliance must not produce a second copy."""
        await self.run_task()
        _, _, first = await self.harness.request("GET", "/api/reports/scheduled")
        await self.run_task()
        _, _, second = await self.harness.request("GET", "/api/reports/scheduled")
        self.assertEqual(
            [snapshot["name"] for snapshot in first["snapshots"]],
            [snapshot["name"] for snapshot in second["snapshots"]],
        )

    async def test_the_kept_report_carries_the_figures_in_digits(self) -> None:
        await self.run_task()
        _, _, listing = await self.harness.request("GET", "/api/reports/scheduled")
        name = next(snapshot["name"] for snapshot in listing["snapshots"]
                    if snapshot["name"].startswith("every-morning_"))
        status, headers, payload = await self.harness.request(
            "GET", f"/api/reports/scheduled/{name}"
        )
        self.assertEqual(status, 200)
        self.assertIn("text/csv", headers["content-type"])
        body = payload.decode("utf-8")
        self.assertIn("201", body)
        self.assertIn("reception", body)

    async def test_a_snapshot_name_that_escapes_the_directory_is_refused(self) -> None:
        for name in ("..%2F..%2Fetc%2Fpasswd", "nothing-here.csv", "secrets.json"):
            with self.subTest(name=name):
                status, _, _ = await self.harness.request(
                    "GET", f"/api/reports/scheduled/{name}"
                )
                self.assertEqual(status, 404)

    async def test_the_routes_refuse_an_anonymous_request(self) -> None:
        for path in ("/api/reports/scheduled", "/api/reports/scheduled/x.csv"):
            with self.subTest(path=path):
                status, _, _ = await self.harness.request("GET", path, cookie="")
                self.assertEqual(status, 401)


if __name__ == "__main__":
    unittest.main()
