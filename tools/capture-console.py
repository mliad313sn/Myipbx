"""Start a real appliance and photograph an operator setting it up.

Nothing here is staged. The appliance is the one the suite runs, started the
same way, and the browser drives it through the interface a person uses. What
comes out is what the console looks like on a machine with no interface card
and no telephony engine reachable -- which is stated on the screens that depend
on either, rather than dressed up.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from tests.support import ApplianceHarness, TEST_PASSWORD, TEST_USERNAME  # noqa: E402

SCRIPT = ROOT / "tests" / "browser" / "capture-console.js"
GLOBAL_MODULES = "/opt/node22/lib/node_modules"

#: An appliance that has just been installed: nothing configured, because the
#: point is to watch it being configured.
DOCUMENT = {
    "revision": 1,
    "site": {"name": "an example site", "timezone": "UTC"},
    "trunks": [],
    "extensions": [],
    "ring_groups": [],
    "inbound_routes": [],
    "outbound_routes": [],
    "time_conditions": [],
    "ivr_menus": [],
    "queues": [],
    "conferences": [],
    "firewall_rules": [
        {
            "name": "session-protocol",
            "service": "session protocol",
            "source": "192.0.2.0/24",
            "enabled": True,
        }
    ],
    "dialplan": {"inbound_context": "from-trunk", "internal_context": "internal"},
    "hardware": {"spans": []},
}


async def main() -> int:
    destination = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "captures" / "console"
    destination.mkdir(parents=True, exist_ok=True)

    harness = ApplianceHarness()
    harness.write_document(DOCUMENT)
    await harness.start()
    try:
        environment = dict(os.environ)
        environment.setdefault("NODE_PATH", GLOBAL_MODULES)
        process = await asyncio.create_subprocess_exec(
            "node", str(SCRIPT),
            f"http://127.0.0.1:{harness.port}/",
            TEST_USERNAME, TEST_PASSWORD, str(destination),
            stdout=None, stderr=None, env=environment, cwd=str(ROOT),
        )
        await asyncio.wait_for(process.wait(), timeout=600)
        return process.returncode or 0
    finally:
        await harness.stop()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
