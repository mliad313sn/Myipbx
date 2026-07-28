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
    # A rate table, so the cost figures in the reports are figures rather than
    # an absence with an explanation beside it.
    "tariffs": [
        {"name": "international", "prefix": "00", "currency": "pounds",
         "connection_fee": "0.05", "per_minute": "0.18",
         "increment_seconds": 60, "minimum_seconds": 60, "enabled": True},
        {"name": "national", "prefix": "0", "currency": "pounds",
         "connection_fee": "0.02", "per_minute": "0.012",
         "increment_seconds": 60, "minimum_seconds": 0, "enabled": True},
        {"name": "everything else", "prefix": "", "currency": "pounds",
         "connection_fee": "0", "per_minute": "0.09",
         "increment_seconds": 1, "minimum_seconds": 0, "enabled": True},
    ],
    "scheduled_reports": [
        {"name": "every-morning", "report": "by_extension",
         "period": "yesterday", "frequency": "daily",
         "destination": "", "enabled": True},
        {"name": "the-switchboard-weekly", "report": "queue:by_queue",
         "period": "last-seven-days", "frequency": "weekly",
         "destination": "", "enabled": True},
    ],
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


#: Calls to report on.
#:
#: The reports view is the one screen that says nothing useful about an
#: appliance which has never carried a call, and a photograph of an empty
#: report says nothing useful about the reports view. These are written into a
#: real record file in the engine's own format and read back through the same
#: reader the console uses, so what is photographed is genuinely aggregated
#: rather than staged.
def _call_records() -> str:
    from datetime import datetime, timedelta
    import random

    start = datetime.now().replace(hour=8, minute=0, second=0, microsecond=0)
    start -= timedelta(days=6)
    # Fixed seed, so re-running the capture produces the same report rather
    # than a differently shaped one every time.
    chance = random.Random(20260728)

    rows = []
    for day in range(7):
        for hour in range(8, 19):
            # Busier late morning and mid afternoon, quiet over lunch, which is
            # the shape a real working day has and the shape the chart exists
            # to show.
            volume = {8: 2, 9: 5, 10: 7, 11: 6, 12: 2, 13: 2,
                      14: 6, 15: 7, 16: 5, 17: 3, 18: 1}[hour]
            for index in range(volume):
                moment = start + timedelta(days=day, hours=hour - 8,
                                           minutes=index * 7)
                inbound = chance.random() < 0.62
                extension = chance.choice(["201", "202", "203"])
                outside = "+4416329601" + str(chance.randint(10, 99))
                answered = chance.random() < 0.78
                talk = chance.randint(25, 480) if answered else 0
                ring = chance.randint(4, 22)
                rows.append(",".join(f'"{value}"' for value in (
                    "",
                    outside if inbound else extension,
                    extension if inbound else outside,
                    "from-trunk" if inbound else "outbound",
                    outside if inbound else extension,
                    "PJSIP/carrier", f"PJSIP/{extension}", "Dial", "",
                    moment.strftime("%Y-%m-%d %H:%M:%S"),
                    moment.strftime("%Y-%m-%d %H:%M:%S"),
                    moment.strftime("%Y-%m-%d %H:%M:%S"),
                    str(talk + ring), str(talk),
                    "ANSWERED" if answered else chance.choice(
                        ["NO ANSWER", "NO ANSWER", "BUSY"]
                    ),
                    "3", f"{moment.timestamp():.6f}", "",
                )))
    return "\n".join(rows) + "\n"


def _queue_events() -> str:
    """A week of a switchboard, in the engine's own queue log format."""
    from datetime import datetime, timedelta
    import random

    start = datetime.now().replace(hour=9, minute=0, second=0, microsecond=0)
    start -= timedelta(days=6)
    chance = random.Random(20260729)

    rows = []
    call = 0
    for day in range(7):
        for index in range(28):
            call += 1
            moment = start + timedelta(days=day, minutes=index * 17)
            identifier = f"{moment.timestamp():.6f}"
            rows.append(f"{int(moment.timestamp())}|{identifier}|700|NONE|"
                        f"ENTERQUEUE||441632960{chance.randint(100, 199)}|1")
            if chance.random() < 0.82:
                waited = chance.randint(2, 55)
                member = chance.choice(["201", "202", "203"])
                talked = chance.randint(40, 400)
                rows.append(f"{int(moment.timestamp()) + waited}|{identifier}|700|"
                            f"PJSIP/{member}|CONNECT|{waited}|{identifier}|3")
                rows.append(f"{int(moment.timestamp()) + waited + talked}|{identifier}|"
                            f"700|PJSIP/{member}|COMPLETEAGENT|{waited}|{talked}|1")
            else:
                waited = chance.randint(30, 180)
                event = chance.choice(["ABANDON", "ABANDON", "EXITWITHTIMEOUT"])
                rows.append(f"{int(moment.timestamp()) + waited}|{identifier}|700|NONE|"
                            f"{event}|1|1|{waited}")
    return "\n".join(rows) + "\n"


async def main() -> int:
    destination = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "captures" / "console"
    destination.mkdir(parents=True, exist_ok=True)

    harness = ApplianceHarness()
    harness.write_document(DOCUMENT)

    records = Path(harness.root) / "Master.csv"
    records.write_text(_call_records(), encoding="utf-8")
    harness.config.call_record_file = str(records)

    # A queue log, so the queue panel shows what a queue report is for rather
    # than what one looks like with nothing in it.
    queue_log = Path(harness.root) / "queue_log"
    queue_log.write_text(_queue_events(), encoding="utf-8")
    harness.config.queue_log_file = str(queue_log)

    # An extension account, so the portal can be photographed being used by
    # somebody who is not the administrator.
    harness_started = await harness.start()

    from appliance.security import CredentialStore, PasswordHasher
    from tests.support import TEST_ITERATIONS

    CredentialStore(
        harness.config.credentials_path, PasswordHasher(TEST_ITERATIONS)
    ).put_account("reception", "a-long-enough-portal-password", "extension", "201")
    del harness_started
    try:
        environment = dict(os.environ)
        environment.setdefault("NODE_PATH", GLOBAL_MODULES)
        process = await asyncio.create_subprocess_exec(
            "node", str(SCRIPT),
            f"http://127.0.0.1:{harness.port}/",
            TEST_USERNAME, TEST_PASSWORD, str(destination),
            "reception", "a-long-enough-portal-password",
            stdout=None, stderr=None, env=environment, cwd=str(ROOT),
        )
        await asyncio.wait_for(process.wait(), timeout=600)
        return process.returncode or 0
    finally:
        await harness.stop()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
