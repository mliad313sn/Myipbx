"""An account scoped to one extension, and the boundary that keeps it there.

This is an authorisation boundary rather than a hidden menu, so most of what is
below is about what a scoped account is refused rather than what it is shown. A
portal that merely does not draw a link to the rest of the appliance is not a
portal; it is a suggestion.

The rule this file exists to hold: **the scope comes from the session, never
from the request.** A portal that asked which extension to show would be a
portal that showed any of them, and it would pass every test that only checked
the happy path.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from support import ApplianceHarness, TEST_PASSWORD, TEST_USERNAME

PORTAL_PASSWORD = "a-long-enough-portal-password"

DOCUMENT = {
    "revision": 1,
    "site": {"name": "an example site", "timezone": "UTC"},
    "trunks": [],
    "extensions": [
        {"number": "201", "name": "reception", "technology": "PJSIP",
         "ring_seconds": 20},
        {"number": "202", "name": "workshop", "technology": "PJSIP",
         "ring_seconds": 20},
    ],
    "ring_groups": [], "inbound_routes": [], "outbound_routes": [],
    "time_conditions": [], "ivr_menus": [], "queues": [], "conferences": [],
    "firewall_rules": [],
    "dialplan": {"inbound_context": "from-trunk", "internal_context": "internal"},
    "hardware": {"spans": []},
}

MINE = "20260727-091500_441632960111_201_1753600000.1.wav"
THEIRS = "20260727-101500_441632960222_202_1753600000.2.wav"


def record(started: str, source: str, destination: str,
           disposition: str = "ANSWERED") -> str:
    return (
        f'"","{source}","{destination}","from-trunk","{source}",'
        f'"PJSIP/carrier","PJSIP/{destination}","Dial","",'
        f'"{started}","{started}","{started}","60","45","{disposition}",'
        f'"3","1753600000.1",""'
    )


class PortalTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.harness = ApplianceHarness()
        self.harness.write_document(DOCUMENT)

        self.records = Path(self.harness.root) / "Master.csv"
        self.records.write_text("\n".join([
            record("2026-07-27 09:15:00", "+441632960111", "201"),
            record("2026-07-27 09:20:00", "201", "+441632960999"),
            record("2026-07-27 10:15:00", "+441632960222", "202"),
        ]) + "\n", encoding="utf-8")
        self.harness.config.call_record_file = str(self.records)

        self.monitor = Path(self.harness.root) / "monitor"
        self.monitor.mkdir()
        (self.monitor / MINE).write_bytes(b"mine")
        (self.monitor / THEIRS).write_bytes(b"theirs")
        self.harness.config.recording_directory = str(self.monitor)

        await self.harness.start()
        await self.harness.sign_in()
        status, _, payload = await self.harness.request(
            "POST", "/api/accounts",
            body=json.dumps({
                "username": "reception", "scope": "201", "password": PORTAL_PASSWORD,
            }),
            extra_headers={"Origin": f"http://127.0.0.1:{self.harness.port}"},
        )
        self.assertEqual(status, 200, payload)
        self.administrator = self.harness.cookie

    async def asyncTearDown(self) -> None:
        await self.harness.stop()

    async def as_portal(self) -> str:
        status, headers, payload = await self.harness.request(
            "POST", "/api/session",
            body=json.dumps({"username": "reception", "password": PORTAL_PASSWORD}),
            cookie="",
        )
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["role"], "extension")
        self.assertEqual(payload["scope"], "201")
        return headers["set-cookie"].split(";")[0]

    # -- what a scoped account is refused ----------------------------------

    async def test_a_scoped_account_is_refused_every_administrator_read(self) -> None:
        cookie = await self.as_portal()
        for path in (
            "/api/state", "/api/hardware", "/api/trunks", "/api/system",
            "/api/configuration", "/api/journal", "/api/logs", "/api/calls",
            "/api/entities/extensions", "/api/entities/extensions/202",
            "/api/reports?window=today", "/api/recordings", "/api/backup",
            "/api/support-bundle", "/api/accounts", "/api/schema",
            "/api/firewall", "/api/tls", "/api/constraints",
            "/api/reports/scheduled",
        ):
            with self.subTest(path=path):
                status, _, payload = await self.harness.request(
                    "GET", path, cookie=cookie
                )
                self.assertEqual(status, 403, path)
                self.assertIn("scoped to one extension", payload["error"])

    async def test_a_scoped_account_is_refused_every_write(self) -> None:
        cookie = await self.as_portal()
        origin = {"Origin": f"http://127.0.0.1:{self.harness.port}"}
        for method, path, body in (
            ("POST", "/api/entities/extensions",
             json.dumps({"number": "999", "name": "mine"})),
            ("DELETE", "/api/entities/extensions/202", None),
            ("POST", "/api/tasks/run", json.dumps({"name": "health-sweep"})),
            ("POST", "/api/accounts",
             json.dumps({"username": "x", "scope": "202", "password": "x" * 12})),
            ("POST", "/api/configuration/render", "{}"),
        ):
            with self.subTest(path=path):
                status, _, _ = await self.harness.request(
                    method, path, body=body, cookie=cookie, extra_headers=origin
                )
                self.assertEqual(status, 403, path)

    async def test_a_scoped_account_cannot_create_another_account(self) -> None:
        """Which would be a way to widen its own reach."""
        cookie = await self.as_portal()
        status, _, _ = await self.harness.request(
            "POST", "/api/accounts",
            body=json.dumps({"username": "mine", "scope": "202",
                             "password": PORTAL_PASSWORD}),
            cookie=cookie,
            extra_headers={"Origin": f"http://127.0.0.1:{self.harness.port}"},
        )
        self.assertEqual(status, 403)

    # -- what a scoped account is shown ------------------------------------

    async def test_the_portal_names_the_extension_it_is_scoped_to(self) -> None:
        cookie = await self.as_portal()
        status, _, payload = await self.harness.request(
            "GET", "/api/portal", cookie=cookie
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["extension"], "201")
        self.assertEqual(payload["name"], "reception")

    async def test_only_this_extension_s_calls_are_returned(self) -> None:
        cookie = await self.as_portal()
        _, _, payload = await self.harness.request(
            "GET", "/api/portal/calls", cookie=cookie
        )
        self.assertTrue(payload["available"])
        self.assertEqual(len(payload["records"]), 2)
        for entry in payload["records"]:
            self.assertIn("201", (entry["source"], entry["destination"]))
            # And nothing of the other extension's. Checked field by field
            # rather than against the whole response, because every timestamp
            # in this decade contains the digits of the year.
            self.assertNotIn("202", (entry["source"], entry["destination"]))

    async def test_only_this_extension_s_recordings_are_listed(self) -> None:
        cookie = await self.as_portal()
        _, _, payload = await self.harness.request(
            "GET", "/api/portal/recordings", cookie=cookie
        )
        self.assertEqual([entry["name"] for entry in payload["records"]], [MINE])

    async def test_this_extension_s_own_recording_is_served(self) -> None:
        cookie = await self.as_portal()
        status, _, payload = await self.harness.request(
            "GET", f"/api/portal/recordings/{MINE}", cookie=cookie
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload, b"mine")

    async def test_another_extension_s_recording_is_refused(self) -> None:
        """And refused the same way a recording that does not exist is.

        Otherwise the portal becomes a way of learning who spoke to whom.
        """
        cookie = await self.as_portal()
        refusals = set()
        for name in (THEIRS, "20260727-101500_1_2_9.9.wav"):
            status, _, payload = await self.harness.request(
                "GET", f"/api/portal/recordings/{name}", cookie=cookie
            )
            self.assertEqual(status, 404, name)
            self.assertNotIn(b"theirs", str(payload).encode())
            refusals.add(payload["error"])
        self.assertEqual(len(refusals), 1, refusals)

    async def test_the_scope_cannot_be_chosen_by_the_request(self) -> None:
        """The rule this whole file exists to hold."""
        cookie = await self.as_portal()
        for query in ("?extension=202", "?scope=202", "?number=202"):
            with self.subTest(query=query):
                _, _, payload = await self.harness.request(
                    "GET", "/api/portal/calls" + query, cookie=cookie
                )
                self.assertEqual(len(payload["records"]), 2)
                for entry in payload["records"]:
                    self.assertNotIn(
                        "202", (entry["source"], entry["destination"])
                    )

    # -- the administrator -------------------------------------------------

    async def test_the_administrator_still_reaches_everything(self) -> None:
        for path in ("/api/state", "/api/entities/extensions", "/api/accounts"):
            with self.subTest(path=path):
                status, _, _ = await self.harness.request(
                    "GET", path, cookie=self.administrator
                )
                self.assertEqual(status, 200)

    async def test_the_administrator_sees_no_portal_of_their_own(self) -> None:
        """Not a second way into everybody's calls."""
        status, _, payload = await self.harness.request(
            "GET", "/api/portal", cookie=self.administrator
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["extension"], "")

        _, _, calls = await self.harness.request(
            "GET", "/api/portal/calls", cookie=self.administrator
        )
        self.assertFalse(calls["available"])

    async def test_the_account_is_listed_without_any_credential(self) -> None:
        _, _, payload = await self.harness.request(
            "GET", "/api/accounts", cookie=self.administrator
        )
        names = {entry["username"]: entry for entry in payload["accounts"]}
        self.assertIn("reception", names)
        self.assertEqual(names["reception"]["scope"], "201")
        self.assertNotIn("credential", json.dumps(payload))
        self.assertNotIn(PORTAL_PASSWORD, json.dumps(payload))

    async def test_an_account_for_an_extension_that_does_not_exist_is_refused(self) -> None:
        status, _, payload = await self.harness.request(
            "POST", "/api/accounts",
            body=json.dumps({"username": "nobody", "scope": "999",
                             "password": PORTAL_PASSWORD}),
            cookie=self.administrator,
            extra_headers={"Origin": f"http://127.0.0.1:{self.harness.port}"},
        )
        self.assertEqual(status, 422)
        self.assertIn("999", payload["error"])

    async def test_a_short_password_is_refused_with_the_reason(self) -> None:
        status, _, payload = await self.harness.request(
            "POST", "/api/accounts",
            body=json.dumps({"username": "brief", "scope": "202", "password": "short"}),
            cookie=self.administrator,
            extra_headers={"Origin": f"http://127.0.0.1:{self.harness.port}"},
        )
        self.assertEqual(status, 422)
        self.assertIn("twelve characters", payload["error"])

    async def test_an_account_cannot_take_the_administrator_s_name(self) -> None:
        status, _, payload = await self.harness.request(
            "POST", "/api/accounts",
            body=json.dumps({"username": TEST_USERNAME, "scope": "202",
                             "password": PORTAL_PASSWORD}),
            cookie=self.administrator,
            extra_headers={"Origin": f"http://127.0.0.1:{self.harness.port}"},
        )
        self.assertEqual(status, 422)
        self.assertIn("administrator", payload["error"])

    async def test_deleting_an_account_stops_it_signing_in(self) -> None:
        status, _, _ = await self.harness.request(
            "DELETE", "/api/accounts/reception", cookie=self.administrator,
            extra_headers={"Origin": f"http://127.0.0.1:{self.harness.port}"},
        )
        self.assertEqual(status, 200)
        status, _, _ = await self.harness.request(
            "POST", "/api/session",
            body=json.dumps({"username": "reception", "password": PORTAL_PASSWORD}),
            cookie="",
        )
        self.assertEqual(status, 401)

    async def test_the_administrator_password_still_works_beside_the_accounts(self) -> None:
        """An appliance upgraded in place must still sign its administrator in."""
        status, _, payload = await self.harness.request(
            "POST", "/api/session",
            body=json.dumps({"username": TEST_USERNAME, "password": TEST_PASSWORD}),
            cookie="",
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["role"], "administrator")

    async def test_a_refused_sign_in_names_no_account_that_exists(self) -> None:
        status, _, payload = await self.harness.request(
            "POST", "/api/session",
            body=json.dumps({"username": "reception", "password": "not-the-password"}),
            cookie="",
        )
        self.assertEqual(status, 401)
        self.assertNotIn("201", payload["error"])


if __name__ == "__main__":
    unittest.main()
