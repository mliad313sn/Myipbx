"""The generated firewall is checked by the thing that will have to load it.

Every other test of the firewall asserts that the generated text contains what
it should. That proves the generator's intent and not the result: a ruleset can
contain every rule an operator declared and still be refused by the kernel for
a syntax fault, and the appliance would only discover that at the moment an
operator pressed apply — which is the worst possible moment, because the verb
that applies a firewall is also the verb that can shut an administrator out of
the appliance they are applying it from.

So here the ruleset is handed to the real rule parser, in the mode that parses
and validates without loading anything. If the parser is not installed these
tests say so and skip, rather than passing quietly and claiming a check that
never happened.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from appliance import firewall


NFT = shutil.which("nft")

_SKIP_REASON = (
    "the rule parser is not installed on this machine, so the generated "
    "ruleset could not be checked against it"
)


@unittest.skipIf(NFT is None, _SKIP_REASON)
class GeneratedRulesetLoadsTests(unittest.TestCase):
    """Every shape the console can produce is a ruleset the kernel accepts."""

    def check(self, ruleset: str) -> None:
        """Parse and validate without loading, and report the parser's own words."""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ruleset.nft"
            path.write_text(ruleset, encoding="utf-8")
            completed = subprocess.run(  # noqa: S603 - an argument list, no shell
                [str(NFT), "--check", "--file", str(path)],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=30,
                check=False,
            )
            self.assertEqual(
                completed.returncode,
                0,
                "the generated ruleset was refused by the rule parser:\n"
                + completed.stdout.decode("utf-8", "replace"),
            )

    def test_a_ruleset_with_nothing_declared_still_loads(self) -> None:
        """The deny by default skeleton is what a fresh appliance carries."""
        self.check(firewall.render_ruleset([], management_port=8088))

    def test_every_offerable_service_loads(self) -> None:
        """A rule for each service the console offers, all at once.

        This is the test that notices a service being added to the catalogue
        with a port expression the parser will not accept.
        """
        rules = [
            {
                "name": f"rule-{index}",
                "service": service,
                "sources": ["192.0.2.0/24"],
                "enabled": True,
            }
            for index, service in enumerate(firewall.SERVICES)
        ]
        self.check(firewall.render_ruleset(rules, management_port=8088))

    def test_each_service_loads_on_its_own(self) -> None:
        """Named individually, so a failure names the service that caused it."""
        for service in firewall.SERVICES:
            with self.subTest(service=service):
                self.check(
                    firewall.render_ruleset(
                        [
                            {
                                "name": "single",
                                "service": service,
                                "sources": ["198.51.100.7"],
                                "enabled": True,
                            }
                        ],
                        management_port=8088,
                    )
                )

    def test_a_rule_open_to_everywhere_loads(self) -> None:
        self.check(
            firewall.render_ruleset(
                [
                    {
                        "name": "open",
                        "service": "session protocol",
                        "sources": ["any"],
                        "enabled": True,
                    }
                ],
                management_port=8088,
            )
        )

    def test_several_sources_on_one_rule_load(self) -> None:
        self.check(
            firewall.render_ruleset(
                [
                    {
                        "name": "several",
                        "service": "media",
                        "sources": ["192.0.2.0/24", "198.51.100.0/25", "203.0.113.9"],
                        "enabled": True,
                    }
                ],
                management_port=8088,
            )
        )

    def test_a_disabled_rule_leaves_a_loadable_ruleset(self) -> None:
        self.check(
            firewall.render_ruleset(
                [
                    {
                        "name": "off",
                        "service": "secure shell",
                        "sources": ["192.0.2.0/24"],
                        "enabled": False,
                    }
                ],
                management_port=8088,
            )
        )

    def test_the_console_and_its_redirect_port_both_load(self) -> None:
        """Transport security added a second port, and it goes in the ruleset too."""
        self.check(
            firewall.render_ruleset(
                [],
                management_port=8443,
                management_sources=["192.0.2.0/24"],
                redirect_port=8080,
            )
        )

    def test_a_console_reachable_from_named_sources_loads(self) -> None:
        self.check(
            firewall.render_ruleset(
                [],
                management_port=8088,
                management_sources=["192.0.2.10", "198.51.100.0/24"],
            )
        )

    def test_many_rules_at_once_load(self) -> None:
        """A site with a long list is still a ruleset, not a pile of text."""
        rules = [
            {
                "name": f"site-{index}",
                "service": "session protocol",
                "sources": [f"192.0.2.{index}"],
                "enabled": True,
            }
            for index in range(1, 40)
        ]
        self.check(firewall.render_ruleset(rules, management_port=8088))


class TheCheckIsHonestTests(unittest.TestCase):
    """The skip above must not be able to hide a broken generator."""

    def test_the_parser_would_reject_a_ruleset_that_is_wrong(self) -> None:
        """Prove the check can fail, or it proves nothing.

        A validation that accepts everything is worse than none, because it
        reads as assurance. A deliberately malformed ruleset is offered to the
        same parser in the same mode, and it has to be refused.
        """
        if NFT is None:
            self.skipTest(_SKIP_REASON)

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "broken.nft"
            path.write_text(
                "table inet myipbx {\n"
                "    chain input {\n"
                "        this is not a rule at all\n"
                "    }\n"
                "}\n",
                encoding="utf-8",
            )
            completed = subprocess.run(  # noqa: S603 - an argument list, no shell
                [str(NFT), "--check", "--file", str(path)],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=30,
                check=False,
            )
            self.assertNotEqual(
                completed.returncode,
                0,
                "the rule parser accepted a ruleset that is plainly malformed, "
                "so the checks above prove nothing",
            )


if __name__ == "__main__":
    unittest.main()
