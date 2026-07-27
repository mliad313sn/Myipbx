"""The dashboard, driven in a real browser against a real appliance.

Every other suite exercises the appliance from the server side.  This one loads
the served page in Chromium, signs in through the form, and watches the page
update itself while the appliance pushes events at it.

A dashboard that throws in the browser is a dashboard that has silently stopped
updating while still looking alive, so any error the page raises is collected
and fails the test.

When no browser runtime is available the suite reports itself skipped rather
than passing vacuously.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import unittest
from pathlib import Path

from support import (
    REPOSITORY_ROOT,
    ApplianceHarness,
    TEST_PASSWORD,
    TEST_USERNAME,
    engine_event,
)

BROWSER_SCRIPT = REPOSITORY_ROOT / "tests/browser/dashboard-check.js"
SOCKET_LOGIC_SCRIPT = REPOSITORY_ROOT / "tests/browser/socket-logic-check.js"

#: Playwright is installed globally in this environment rather than beside the
#: repository, so the module search path is supplied explicitly.
_GLOBAL_MODULES = "/opt/node22/lib/node_modules"


def _browser_available() -> tuple[bool, str]:
    if shutil.which("node") is None:
        return False, "no browser scripting runtime is installed"
    if not BROWSER_SCRIPT.is_file():
        return False, "the browser harness script is absent"

    probe = (
        "try { require('playwright'); console.log('yes'); } "
        "catch (error) { console.log('no'); }"
    )
    environment = dict(os.environ)
    environment.setdefault("NODE_PATH", _GLOBAL_MODULES)
    import subprocess

    try:
        completed = subprocess.run(
            ["node", "-e", probe],
            capture_output=True,
            text=True,
            timeout=60,
            env=environment,
        )
    except (OSError, subprocess.SubprocessError):
        return False, "the browser automation module could not be probed"

    if "yes" not in completed.stdout:
        return False, "the browser automation module is not installed"
    return True, ""


class SocketClientLogicTests(unittest.TestCase):
    """The dashboard socket client's own classification, without a browser.

    Inducing genuine staleness against a live appliance would mean wedging its
    event loop, so the three way classification is exercised directly instead.
    This is the browser half of Benchmark Defect Two: a dashboard that cannot
    tell stale from quiet is the failure the product exists to remove.
    """

    @classmethod
    def setUpClass(cls) -> None:
        if shutil.which("node") is None:
            raise unittest.SkipTest("no browser scripting runtime is installed")
        if not SOCKET_LOGIC_SCRIPT.is_file():
            raise unittest.SkipTest("the socket logic harness script is absent")

    def test_the_socket_client_classifies_its_link_correctly(self) -> None:
        import subprocess

        completed = subprocess.run(
            ["node", str(SOCKET_LOGIC_SCRIPT)],
            capture_output=True,
            text=True,
            timeout=120,
            cwd=str(REPOSITORY_ROOT),
        )
        self.assertEqual(
            completed.returncode, 0,
            f"the socket logic harness failed: {completed.stderr[-2000:]}",
        )

        report = None
        for line in completed.stdout.splitlines():
            if line.startswith("RESULT "):
                report = json.loads(line[len("RESULT "):])
        self.assertIsNotNone(report, f"no result was produced: {completed.stdout[-2000:]}")

        failed = [item for item in report["results"] if not item["passed"]]
        self.assertEqual(
            failed, [],
            "the dashboard socket client misclassified its link: "
            + "; ".join(f"{item['name']} ({item['detail']})" for item in failed),
        )
        self.assertGreaterEqual(
            len(report["results"]), 15,
            "the socket logic harness ran fewer checks than expected",
        )


class BrowserDashboardTests(unittest.IsolatedAsyncioTestCase):
    """The vanilla dashboard, executed rather than merely syntax checked."""

    @classmethod
    def setUpClass(cls) -> None:
        available, reason = _browser_available()
        if not available:
            raise unittest.SkipTest(reason)

    async def asyncSetUp(self) -> None:
        self.harness = ApplianceHarness()
        self.harness.write_document(
            {
                "revision": 1,
                "trunks": [
                    {"name": "carrier-primary", "technology": "PJSIP",
                     "host": "sip.example.net", "username": "primary-account",
                     "enabled": True},
                ],
                "extensions": [
                    {"number": "201", "name": "Reception", "technology": "PJSIP",
                     "voicemail": True, "ring_seconds": 20, "enabled": True},
                ],
                "dialplan": {"internal_context": "internal",
                             "inbound_context": "from-trunk"},
                "hardware": {"spans": []},
            }
        )
        self.appliance = await self.harness.start()

    async def asyncTearDown(self) -> None:
        await self.harness.stop()

    async def _run_browser(self) -> dict:
        """Drive the browser while the appliance keeps serving."""
        # A call is already in progress when the page loads.
        self.appliance._on_engine_event(
            engine_event(
                "Newchannel", uniqueid="existing-call", channel="PJSIP/existing",
                channelstate="6", calleridnum="2015550100", exten="201",
            )
        )

        # A second call arrives while the page is open.  Nothing asks for it;
        # the appliance pushes it and the page must render it.
        async def push_later() -> None:
            await asyncio.sleep(4)
            self.appliance._on_engine_event(
                engine_event(
                    "Newchannel", uniqueid="live-call", channel="PJSIP/live-probe",
                    channelstate="4", calleridnum="2015550199", exten="201",
                )
            )
            await asyncio.sleep(0.5)
            self.appliance._on_engine_event(
                engine_event("Newstate", uniqueid="live-call", channelstatedesc="Up")
            )

        pusher = asyncio.create_task(push_later())

        environment = dict(os.environ)
        environment.setdefault("NODE_PATH", _GLOBAL_MODULES)

        process = await asyncio.create_subprocess_exec(
            "node",
            str(BROWSER_SCRIPT),
            f"http://127.0.0.1:{self.harness.port}/",
            TEST_USERNAME,
            TEST_PASSWORD,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=environment,
            cwd=str(REPOSITORY_ROOT),
        )

        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=180
            )
        except asyncio.TimeoutError:
            process.kill()
            self.fail("the browser run did not finish within its timeout")
        finally:
            pusher.cancel()
            try:
                await pusher
            except asyncio.CancelledError:
                pass

        output = stdout.decode("utf-8", "replace")
        for line in output.splitlines():
            if line.startswith("RESULT "):
                return json.loads(line[len("RESULT "):])

        self.fail(
            "the browser harness produced no result.\n"
            f"standard output: {output[-2000:]}\n"
            f"standard error: {stderr.decode('utf-8', 'replace')[-2000:]}"
        )

    async def test_the_dashboard_works_end_to_end_in_a_browser(self) -> None:
        report = await self._run_browser()

        self.assertIsNone(
            report.get("failure"),
            f"the browser reported a failure: {report.get('failure')}",
        )

        # -- the page must not have thrown ---------------------------------
        self.assertEqual(
            report["pageErrors"], [],
            f"the dashboard raised an error in the browser: {report['pageErrors']}",
        )
        # The run deliberately submits an invalid extension to prove the
        # validation reaches the operator, and the browser logs the refusal
        # that follows.  That one entry is expected; anything else is not.
        unexpected = [
            message
            for message in report["consoleErrors"]
            if "422" not in message
        ]
        self.assertEqual(
            unexpected, [],
            f"the console logged an unexpected error in the browser: {unexpected}",
        )
        self.assertEqual(
            report["failedRequests"], [],
            f"a request from the dashboard failed: {report['failedRequests']}",
        )

        # -- authentication -------------------------------------------------
        self.assertTrue(
            report["consoleHiddenBeforeSignIn"],
            "the console was visible before anyone signed in",
        )
        self.assertTrue(report["signedIn"], "the sign in form did not admit the browser")
        self.assertTrue(report["signedOut"], "signing out did not hide the console")

        # -- every section of the console renders ---------------------------
        expected_views = {
            "overview", "calls", "history", "extensions", "trunks",
            "ring_groups", "inbound_routes", "outbound_routes",
            "time_conditions", "hardware", "system", "configuration",
            "tasks", "logs", "backup", "constraints",
        }
        self.assertEqual(
            set(report["views"]), expected_views,
            "not every section of the console was reached",
        )
        for name, text in report["views"].items():
            with self.subTest(view=name):
                self.assertTrue(
                    text, f"the section named {name} rendered nothing at all"
                )

        # A few sections must show specific evidence that they really loaded.
        self.assertIn("privileged operations", report["views"]["constraints"].lower())
        self.assertIn("guided bring up", report["views"]["hardware"].lower())
        self.assertIn("health-sweep", report["views"]["tasks"])

        # -- a telephony object created and deleted through the interface ---
        self.assertTrue(
            report["entityValidationRefused"],
            "an invalid extension was accepted, or its refusal never reached the form",
        )
        self.assertFalse(
            re.search(r"\d", report.get("validationMessage", "")),
            f"the validation message carried a digit: {report.get('validationMessage')}",
        )
        self.assertTrue(
            report["entityCreated"],
            f"the extension was not created through the interface: "
            f"{report.get('extensionsAfterCreate')}",
        )
        self.assertTrue(
            report["entityDeleted"],
            "the extension was not deleted through the interface",
        )

        # -- the persistent socket reached the live condition ---------------
        self.assertEqual(
            report["linkState"], "live",
            "the dashboard never reported its link as live",
        )

        # -- the existing call was rendered ---------------------------------
        rows = " ".join(report["initialChannelRows"])
        self.assertIn(
            "PJSIP/existing", rows,
            f"the call already in progress was not rendered: {report['initialChannelRows']}",
        )

        # -- a pushed update was rendered without being asked for -----------
        self.assertTrue(
            report["liveChannelSeen"],
            "a call pushed while the page was open never appeared on it",
        )
        self.assertIsNotNone(report["liveChannelRow"])

        # -- Constraint Two, as a person actually sees it -------------------
        self.assertEqual(
            report["digitsOnPage"], [],
            f"a digit character was rendered on the dashboard: {report['digitsOnPage']}",
        )
        self.assertEqual(
            report["tiles"]["activeCalls"], "one",
            "the tile did not show the call that was already in progress",
        )
        self.assertIn("one", report["tiles"]["trunksCaption"])
        self.assertFalse(re.search(r"\d", report["tiles"]["uptime"] or ""))

        # The headline tile must follow the pushed state, not just the table.
        self.assertEqual(
            report.get("tilesAfterPush", {}).get("activeCalls"), "two",
            "the active call tile did not follow the pushed update",
        )

        # -- the constraint panel reports the exclusion as satisfied --------
        # -- the constraint section states the exclusion --------------------
        constraints = report["views"]["constraints"]
        self.assertIn("never assigns", constraints)
        self.assertIn("no address allocation service exists", constraints)

        # -- the trunk table was populated on the overview ------------------
        self.assertIn("carrier-primary", report["views"]["overview"])

        # -- the machine section reached the appliance ----------------------
        system = report["views"]["system"].lower()
        self.assertIn("host name", system)
        self.assertIn("network interfaces", system)


if __name__ == "__main__":
    unittest.main()
