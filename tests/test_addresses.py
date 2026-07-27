"""What this appliance thinks an address is, and that it thinks it once.

There were three readings. The system operations module matched four groups of
digits and checked each was between zero and two hundred fifty-five, reading
each group with the interpreter's own integer conversion. The firewall renderer
handed the text to the standard library. The telephony schema matched four
groups of digits and checked nothing about them at all.

Two consequences, and the second one is a security finding rather than a tidy
one.

A source of nine hundred ninety-nine dot one dot one dot one passed the schema,
saved into the source of truth, and was refused later at render time by a
different component with a different message -- so an operator learned their
firewall rule was wrong at the moment they tried to apply the firewall, which
on a remote appliance is the worst possible moment.

And the leading zero. In this language ``int("010")`` is ten. In the C library
every other program on the machine uses, ``010`` is octal and means eight. A
rule written to admit ``010.0.0.1`` named one host to the appliance's own check
and a different host to the kernel that loaded the ruleset. The tests below
assert that all three readings now refuse it, rather than that two of them
happen to agree.
"""

from __future__ import annotations

import unittest

from appliance import addresses, firewall, sysops
from appliance.entities import ENTITY_SPECS


class LeadingZeroTests(unittest.TestCase):
    """The disagreement that mattered."""

    OCTAL_TRAPS = (
        "010.0.0.1",
        "192.168.01.1",
        "0177.0.0.1",
        "192.0.2.010",
    )

    def test_the_shared_reading_refuses_a_leading_zero(self) -> None:
        for text in self.OCTAL_TRAPS:
            with self.subTest(text=text):
                self.assertFalse(addresses.is_address(text))
                self.assertFalse(addresses.is_network(text))

    def test_the_refusal_says_why_rather_than_only_that(self) -> None:
        """An operator who typed a leading zero has to be told what is wrong
        with it, because to them it looks like the address they meant."""
        with self.assertRaises(addresses.AddressRefused) as refusal:
            addresses.parse_address("010.0.0.1")
        message = str(refusal.exception)
        self.assertIn("leading zero", message)
        self.assertIn("reads as one number", message)

    def test_the_system_operations_reading_refuses_it(self) -> None:
        for text in self.OCTAL_TRAPS:
            with self.subTest(text=text):
                self.assertFalse(sysops._is_address(text))

    def test_the_firewall_reading_refuses_it(self) -> None:
        for text in self.OCTAL_TRAPS:
            with self.subTest(text=text):
                with self.assertRaises(firewall.FirewallError):
                    firewall._validate_source(text)

    def test_a_rule_naming_an_octal_trap_never_reaches_the_ruleset(self) -> None:
        """Prove the outcome, not only the refusal."""
        rules = [
            {"name": "sip", "service": "session protocol",
             "sources": ["010.0.0.1"], "enabled": True}
        ]
        try:
            ruleset = firewall.render_ruleset(rules, management_port=8088)
        except Exception:
            return  # refusing to render at all is an acceptable outcome
        self.assertNotIn("010.0.0.1", ruleset)


class OneReadingTests(unittest.TestCase):
    """Whatever one component accepts, the others must accept, and no more."""

    SAMPLES = (
        "192.0.2.10",
        "10.0.0.1",
        "255.255.255.255",
        "0.0.0.0",
        "999.1.1.1",
        "1.2.3",
        "1.2.3.4.5",
        "256.0.0.1",
        "010.0.0.1",
        "192.0.2.10 ",
        "not an address",
        "",
        "::1",
        "192.0.2.10/24",
    )

    def test_the_operations_reading_and_the_shared_one_agree_exactly(self) -> None:
        for text in self.SAMPLES:
            with self.subTest(text=text):
                self.assertEqual(sysops._is_address(text), addresses.is_address(text))

    def test_the_firewall_reading_and_the_shared_one_agree_exactly(self) -> None:
        for text in self.SAMPLES:
            with self.subTest(text=text):
                accepted_by_firewall = True
                try:
                    firewall._validate_source(text)
                except firewall.FirewallError:
                    accepted_by_firewall = False
                self.assertEqual(accepted_by_firewall, addresses.is_network(text))

    def test_the_schema_names_the_shared_reading_for_a_firewall_source(self) -> None:
        """Without this the schema is back to checking nothing."""
        field = ENTITY_SPECS["firewall_rules"].field("source")
        assert field is not None
        self.assertEqual(field.reading, "network")


class WhatIsAcceptedTests(unittest.TestCase):
    """The guard must not refuse the addresses people actually write."""

    def test_ordinary_addresses_and_networks_are_accepted(self) -> None:
        for text in ("192.0.2.10", "10.0.0.1", "172.16.0.1", "0.0.0.0"):
            with self.subTest(text=text):
                self.assertTrue(addresses.is_address(text))
        for text in ("192.0.2.0/24", "10.0.0.0/8", "192.0.2.10", "0.0.0.0/0"):
            with self.subTest(text=text):
                self.assertTrue(addresses.is_network(text))

    def test_a_single_zero_group_is_not_a_leading_zero(self) -> None:
        """Refusing "10.0.0.1" would refuse most private networks in use."""
        self.assertTrue(addresses.is_address("10.0.0.1"))
        self.assertTrue(addresses.is_address("0.0.0.0"))

    def test_the_words_for_everywhere_are_still_understood(self) -> None:
        for text in ("any", "ANY", "anywhere", "", "  "):
            with self.subTest(text=text):
                self.assertEqual(addresses.parse_network(text), "0.0.0.0/0")

    def test_a_bare_address_is_read_as_a_network_of_one_host(self) -> None:
        """Which is how an operator naming one machine expects it to be read."""
        self.assertEqual(addresses.parse_network("192.0.2.10"), "192.0.2.10/32")

    def test_a_network_is_returned_in_one_spelling(self) -> None:
        """So that two rules naming the same network compare equal."""
        self.assertEqual(addresses.parse_network("192.0.2.7/24"), "192.0.2.0/24")

    def test_version_six_is_refused_by_name(self) -> None:
        """This appliance renders a version four ruleset. Silently dropping a
        version six source would open nothing and say nothing."""
        with self.assertRaises(addresses.AddressRefused) as refusal:
            addresses.parse_network("2001:db8::/32")
        self.assertIn("version four", str(refusal.exception))


if __name__ == "__main__":
    unittest.main()
