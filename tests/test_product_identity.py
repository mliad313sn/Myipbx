"""The name, and the places the console and the appliance have to agree.

The product name was stated as a literal in seven places, twice of them in this
suite, so renaming it meant finding all seven. It is one constant now, and this
is what keeps it one.

The second half is about a different kind of drift: a list the console holds and
the appliance also holds. Where the two are written separately -- and they are,
because the console offers report periods in the order a person reaches for them
and the appliance has no opinion about that order -- nothing but a test stops
one gaining an entry the other has never heard of.
"""

from __future__ import annotations

import re
import unittest

from appliance import PRODUCT_FULL_NAME, PRODUCT_MAKER, PRODUCT_NAME, reports
from support import REPOSITORY_ROOT

#: Where the brand is allowed to appear as a literal, and why.
_MAY_STATE_THE_NAME = {
    # The definition itself.
    "appliance/__init__.py",
    # The shell half of the product, which cannot import a Python constant.
    "scripts/lib/common.sh",
    # Prose about the product, which is written for people.
    "README.md",
    "Makefile",
}

#: Spelled in two halves so that this file is not itself a place the old name
#: survives, which is exactly what the first run of the test below found.
_OLD_NAME = "my" + "ipbx"


class WhereTheNameLivesTests(unittest.TestCase):
    def test_the_name_and_the_maker_are_separate_and_join_up(self) -> None:
        self.assertEqual(PRODUCT_FULL_NAME, f"{PRODUCT_NAME} {PRODUCT_MAKER}")
        self.assertTrue(PRODUCT_MAKER.startswith("by "))

    def test_the_control_plane_states_the_name_exactly_once(self) -> None:
        """Everything else imports it.

        Seven copies is seven places to miss, and missing one leaves an
        appliance whose backup archive claims a different product from the one
        that wrote it.
        """
        offenders = []
        for path in sorted((REPOSITORY_ROOT / "appliance").rglob("*.py")):
            relative = path.relative_to(REPOSITORY_ROOT).as_posix()
            if relative in _MAY_STATE_THE_NAME:
                continue
            source = path.read_text(encoding="utf-8")
            if re.search(rf'["\']{re.escape(PRODUCT_NAME)}\b', source):
                offenders.append(relative)
        self.assertEqual(
            offenders, [],
            "these modules write the product name out rather than importing "
            "PRODUCT_NAME or PRODUCT_FULL_NAME, so the next rename will miss them",
        )

    def test_the_old_name_is_gone_from_everything_that_ships(self) -> None:
        """Except the captures, which are photographs of what shipped before."""
        offenders = []
        for pattern in ("*.py", "*.sh", "*.js", "*.css", "*.html", "*.md", "*.json"):
            for path in sorted(REPOSITORY_ROOT.rglob(pattern)):
                relative = path.relative_to(REPOSITORY_ROOT).as_posix()
                if relative.startswith(("captures/", ".git/")) or "__pycache__" in relative:
                    continue
                if _OLD_NAME in path.read_text(encoding="utf-8", errors="replace").lower():
                    offenders.append(relative)
        self.assertEqual(offenders, [], "the old name survives in these files")

    def test_no_shipped_file_is_still_named_after_the_old_product(self) -> None:
        named = [
            path.relative_to(REPOSITORY_ROOT).as_posix()
            for path in REPOSITORY_ROOT.rglob(f"*{_OLD_NAME}*")
            if ".git/" not in path.as_posix()
        ]
        self.assertEqual(named, [])


class WhereTheConsoleAndTheApplianceHaveToAgreeTests(unittest.TestCase):
    DASHBOARD = REPOSITORY_ROOT / "web" / "js" / "dashboard.js"

    def setUp(self) -> None:
        self.source = self.DASHBOARD.read_text(encoding="utf-8")

    def _console_windows(self) -> list[str]:
        block = self.source.split("var REPORT_WINDOWS = [", 1)[1].split("];", 1)[0]
        return re.findall(r"name:\s*'([^']+)'", block)

    def test_the_console_offers_exactly_the_windows_the_appliance_resolves(self) -> None:
        self.assertEqual(
            sorted(self._console_windows()),
            sorted(name for name, _ in reports.WINDOWS),
            "the console offers a report period the appliance does not know, or "
            "the appliance grew one the console never shows",
        )

    def test_every_window_the_console_offers_actually_resolves(self) -> None:
        for name in self._console_windows():
            with self.subTest(window=name):
                if name == "between":
                    window = reports.resolve_window(name, "2026-01-01", "2026-01-31")
                else:
                    window = reports.resolve_window(name)
                self.assertLessEqual(window["from"], window["to"])

    def test_the_console_draws_only_breakdowns_the_appliance_produces(self) -> None:
        block = self.source.split("var REPORT_BREAKDOWNS = [", 1)[1].split("\n    ];", 1)[0]
        drawn = re.findall(r"key:\s*'([^']+)'", block)
        self.assertTrue(drawn)

        produced = reports.build_report(
            [], reports.resolve_window("today")
        )["breakdowns"]
        for key in drawn:
            self.assertIn(key, produced,
                          f"the console draws {key}, which no report contains")

    def test_the_console_reads_only_summary_figures_the_appliance_carries(self) -> None:
        block = self.source.split("var REPORT_TILES = [", 1)[1].split("\n    ];", 1)[0]
        wanted = re.findall(r"key:\s*'([^']+)'", block)
        self.assertTrue(wanted)

        # With a rate table, so that the cost figures -- which exist only when
        # one is configured -- are present to be checked. A console tile the
        # appliance never produces is the defect this looks for; a tile it
        # produces conditionally is not.
        summary = reports.build_report(
            [], reports.resolve_window("today"),
            tariffs=[{
                "name": "national", "prefix": "0", "currency": "pounds",
                "connection_fee": "0", "per_minute": "0.01",
                "increment_seconds": 60, "minimum_seconds": 0, "enabled": True,
            }],
        )["summary"]
        for key in wanted:
            self.assertIn(key, summary,
                          f"the console shows a tile for {key}, which no report carries")

    def test_the_queue_tiles_and_breakdowns_agree_with_the_queue_report(self) -> None:
        """The same guarantee for the panel drawn from the other file."""
        produced = reports.build_queue_report([], reports.resolve_window("today"))

        tiles = re.findall(
            r"key:\s*'([^']+)'",
            self.source.split("var QUEUE_TILES = [", 1)[1].split("\n    ];", 1)[0],
        )
        self.assertTrue(tiles)
        for key in tiles:
            self.assertIn(key, produced["summary"],
                          f"the console shows a queue tile for {key}")

        drawn = re.findall(
            r"key:\s*'([^']+)'",
            self.source.split("var QUEUE_BREAKDOWNS = [", 1)[1].split("\n    ];", 1)[0],
        )
        self.assertTrue(drawn)
        for key in drawn:
            self.assertIn(key, produced["breakdowns"],
                          f"the console draws a queue breakdown for {key}")


if __name__ == "__main__":
    unittest.main()
