"""User acceptance, run against a real appliance in a real browser.

The rest of the suite proves the appliance behaves. This asks whether a person
could operate it: whether the console can be driven with the keyboard alone,
whether a rejected form tells the person what is wrong in a way a screen reader
conveys, whether anything is reachable on a telephone, and whether the text can
actually be read at the contrast it is drawn with.

Findings are graded the way an acceptance tester would grade them. Critical and
high findings fail the run, because they are the ones that stop somebody
working. Lower findings are reported so they are visible without blocking.

When no browser runtime is available the suite reports itself skipped rather
than passing vacuously, which is the only honest thing an untested check can do.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import unittest
from pathlib import Path

from support import (
    REPOSITORY_ROOT,
    ApplianceHarness,
    TEST_PASSWORD,
    TEST_USERNAME,
)

UAT_SCRIPT = REPOSITORY_ROOT / "tests/browser/uat-check.js"

#: Playwright is installed globally in this environment rather than beside the
#: repository, so the module search path is supplied explicitly.
_GLOBAL_MODULES = "/opt/node22/lib/node_modules"

#: Severities that fail the run.  A person stopped is a defect; a person
#: inconvenienced is a finding to carry.
BLOCKING = ("critical", "high")


def _browser_available() -> tuple[bool, str]:
    if shutil.which("node") is None:
        return False, "no browser scripting runtime is installed"
    if not UAT_SCRIPT.is_file():
        return False, "the acceptance harness script is absent"

    environment = dict(os.environ)
    environment.setdefault("NODE_PATH", _GLOBAL_MODULES)
    probe = (
        "try { require('playwright'); console.log('yes'); } "
        "catch (error) { console.log('no'); }"
    )
    try:
        completed = subprocess.run(
            ["node", "-e", probe],
            capture_output=True,
            text=True,
            timeout=60,
            env=environment,
        )
    except (OSError, subprocess.SubprocessError) as error:
        return False, f"the browser runtime could not be probed: {error}"

    if completed.stdout.strip() != "yes":
        return False, "the browser automation library is not installed"
    return True, ""


_AVAILABLE, _REASON = _browser_available()

DOCUMENT = {
    "revision": 1,
    "trunks": [
        {
            "name": "carrier-primary",
            "technology": "PJSIP",
            "host": "sip.example.net",
            "username": "primary-account",
            "enabled": True,
        }
    ],
    "extensions": [{"number": "201", "target": "PJSIP/201"}],
    "dialplan": {"internal_context": "internal", "inbound_context": "from-trunk"},
    "hardware": {"spans": []},
}


@unittest.skipUnless(_AVAILABLE, _REASON)
class UserAcceptanceTests(unittest.IsolatedAsyncioTestCase):
    """Could somebody actually operate this appliance?"""

    maxDiff = None

    async def asyncSetUp(self) -> None:
        self.harness = ApplianceHarness()
        self.harness.write_document(DOCUMENT)
        self.appliance = await self.harness.start()

    async def asyncTearDown(self) -> None:
        await self.harness.stop()

    async def _run_acceptance(self) -> dict:
        """Drive the console, without starving the appliance being driven.

        The appliance under test runs on this test's own event loop. A
        synchronous subprocess call would block that loop for the whole run, so
        the appliance could never answer the browser and every navigation would
        time out. It looks like a product fault and is entirely a harness one.
        """
        environment = dict(os.environ)
        environment.setdefault("NODE_PATH", _GLOBAL_MODULES)

        process = await asyncio.create_subprocess_exec(
            "node",
            str(UAT_SCRIPT),
            f"http://127.0.0.1:{self.harness.port}/",
            TEST_USERNAME,
            TEST_PASSWORD,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=environment,
            cwd=str(REPOSITORY_ROOT),
        )
        try:
            raw_out, raw_err = await asyncio.wait_for(process.communicate(), timeout=420)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            self.fail("the acceptance run did not finish within its time")

        stdout = raw_out.decode("utf-8", "replace")
        stderr = raw_err.decode("utf-8", "replace")

        line = ""
        for candidate in reversed(stdout.splitlines()):
            if candidate.startswith("RESULT "):
                line = candidate[len("RESULT ") :]
                break
        self.assertTrue(
            line,
            "the acceptance harness produced no result:\n"
            f"stdout: {stdout[-2000:]}\nstderr: {stderr[-2000:]}",
        )
        return json.loads(line)

    async def test_the_console_can_be_operated_by_a_person(self) -> None:
        report = await self._run_acceptance()

        self.assertIsNone(
            report.get("failure"),
            f"the acceptance run stopped at {report.get('stage')}: {report.get('failure')}",
        )
        self.assertEqual(
            report.get("stage"),
            "complete",
            f"the acceptance run did not finish; it stopped at {report.get('stage')}",
        )

        findings = report.get("findings", [])
        blocking = [f for f in findings if f["severity"] in BLOCKING]
        carried = [f for f in findings if f["severity"] not in BLOCKING]

        if carried:
            # Reported, not failed: worth knowing, and they stop nobody working.
            print("\n  acceptance findings carried forward:")
            for item in carried:
                print(f"    [{item['severity']}] {item['title']}: {item['detail']}")

        self.assertEqual(
            blocking,
            [],
            "the console cannot be operated as it stands:\n"
            + "\n".join(
                f"  [{f['severity']}] {f['title']}\n      {f['detail']}" for f in blocking
            ),
        )

    async def test_the_console_signs_in_with_the_keyboard_alone(self) -> None:
        """The narrower claim, asserted separately so a failure names itself."""
        report = await self._run_acceptance()
        self.assertTrue(
            report.get("keyboard", {}).get("signedInWithKeyboard"),
            "the sign in form could not be completed without a mouse",
        )


if __name__ == "__main__":
    unittest.main()
