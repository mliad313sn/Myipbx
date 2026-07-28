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


if __name__ == "__main__":
    unittest.main()
