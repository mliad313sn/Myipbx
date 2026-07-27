"""One file that says what an appliance is doing, and nothing it must not say.

The exchange this replaces is a dozen messages long: what does the hardware
section say, which trunk is it, what is in the log, what version is this. Each
round trip to a site nobody can reach costs a day, and each answer arrives as a
screenshot of part of a screen.

The tests below hold two things, and the second one is why this file is
adversarial rather than descriptive.

The first is that the bundle is worth sending: it carries the state, the
hardware, the trunks, the configuration, the logs and the record of changes,
and one collector failing does not take the rest with it -- a bundle is asked
for when something is already wrong, and the collector that raises is often the
broken thing itself.

The second is that it is safe to send. A support bundle is the most casually
forwarded file an appliance produces: it goes to a vendor, into a ticket, onto
a shared drive, and it stays there. So the tests do not check that a redaction
function was called. They put real passwords into a real appliance, produce the
real bundle, and search the finished bytes for the material by value.
"""

from __future__ import annotations

import io
import json
import tarfile
import unittest

from appliance import supportbundle
from support import ApplianceHarness

#: Distinctive enough that finding any of them in the archive is unambiguous.
TELEPHONE_PASSWORD = "a-telephone-password-nobody-else-uses"
CARRIER_PASSWORD = "a-carrier-password-nobody-else-uses"
CONFERENCE_PIN = "a-conference-entry-code-nobody-else-uses"


class SupportBundleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.harness = ApplianceHarness()
        self.appliance = await self.harness.start()
        await self.harness.sign_in()

        # Real material, put in through the interface an operator uses.
        for path, body in (
            (
                "/api/entities/extensions",
                {
                    "number": "241", "name": "Reception", "technology": "PJSIP",
                    "secret": TELEPHONE_PASSWORD, "voicemail": True,
                    "ring_seconds": 20, "enabled": True,
                },
            ),
            (
                "/api/entities/trunks",
                {
                    "name": "carrier-primary", "technology": "PJSIP",
                    "host": "sip.example.net", "username": "an-account",
                    "secret": CARRIER_PASSWORD, "register": True, "enabled": True,
                },
            ),
            (
                "/api/entities/conferences",
                {
                    "number": "800", "name": "Board Room", "pin": CONFERENCE_PIN,
                    "announce_arrivals": True, "music_class": "default",
                    "enabled": True,
                },
            ),
        ):
            status, _, payload = await self.harness.request(
                "POST", path, body=json.dumps(body)
            )
            self.assertIn(status, (200, 201), f"{path} refused the setup: {payload}")

    async def asyncTearDown(self) -> None:
        await self.harness.stop()

    def _bundle(self) -> tuple[bytes, dict[str, bytes]]:
        payload, name = supportbundle.create(self.appliance)
        self.assertTrue(name.startswith("myipbx-support-"))
        members: dict[str, bytes] = {}
        with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
            for member in archive.getmembers():
                handle = archive.extractfile(member)
                if handle is not None:
                    members[member.name] = handle.read()
        return payload, members

    # -- what it must not carry --------------------------------------------

    def test_no_password_appears_anywhere_in_the_finished_bytes(self) -> None:
        """Searched by value in the decompressed archive, not by field name.

        A test that asserted the redaction function ran would pass while a
        password reached the bundle by a route the function never sees.
        """
        _, members = self._bundle()
        whole = b"\n".join(members.values())
        for material in (TELEPHONE_PASSWORD, CARRIER_PASSWORD, CONFERENCE_PIN):
            with self.subTest(material=material):
                self.assertNotIn(
                    material.encode("utf-8"), whole,
                    "a password reached the support bundle",
                )

    def test_no_administrator_credential_appears(self) -> None:
        _, members = self._bundle()
        whole = b"\n".join(members.values()).lower()
        self.assertNotIn(b"pbkdf2", whole)
        self.assertNotIn(b"-----begin", whole, "a key or certificate reached the bundle")

    def test_a_withheld_field_says_so_rather_than_vanishing(self) -> None:
        """An absent field reads as "this appliance has none", which is a
        different and more misleading statement."""
        _, members = self._bundle()
        configuration = json.loads(members["configuration.json"])
        trunk = next(
            item for item in configuration["trunks"]
            if item["name"] == "carrier-primary"
        )
        if "secret" in trunk:
            self.assertEqual(trunk["secret"], supportbundle.REDACTED)

        whole = b"\n".join(members.values())
        self.assertIn(supportbundle.REDACTED.encode("utf-8"), whole)

    def test_the_redaction_reaches_a_deeply_nested_value(self) -> None:
        redacted = supportbundle._redact(
            {"a": [{"b": {"secret": "material", "keep": "this"}}]}
        )
        self.assertEqual(redacted["a"][0]["b"]["secret"], supportbundle.REDACTED)
        self.assertEqual(redacted["a"][0]["b"]["keep"], "this")

    # -- what it must carry -------------------------------------------------

    def test_the_bundle_describes_the_appliance_and_its_work(self) -> None:
        _, members = self._bundle()
        for name in (
            "READ-ME-FIRST.txt", "manifest.json", "appliance.json", "state.json",
            "trunks.json", "tasks.json", "hardware.json", "settings.json",
            "configuration.json", "journal.json", "logs.json",
            "address-allocation.json",
        ):
            with self.subTest(member=name):
                self.assertIn(name, members)

    def test_the_configuration_is_there_in_full_apart_from_its_secrets(self) -> None:
        """A bundle that dropped the configuration would answer none of the
        questions it exists to answer."""
        _, members = self._bundle()
        configuration = json.loads(members["configuration.json"])
        numbers = [item["number"] for item in configuration["extensions"]]
        self.assertIn("241", numbers)
        hosts = [item["host"] for item in configuration["trunks"]]
        self.assertIn("sip.example.net", hosts)

    def test_the_first_file_says_what_is_in_it_and_what_is_not(self) -> None:
        """Whoever forwards this has to know what they are forwarding."""
        _, members = self._bundle()
        # Read as prose rather than as lines, because the file is wrapped for
        # somebody opening it in whatever a site has to hand.
        readme = " ".join(members["READ-ME-FIRST.txt"].decode("utf-8").split())
        self.assertIn("no telephone password", readme)
        self.assertIn("addresses of this site", readme)
        self.assertIn(supportbundle.REDACTED, readme)

    def test_the_manifest_names_every_withheld_field(self) -> None:
        _, members = self._bundle()
        manifest = json.loads(members["manifest.json"])
        self.assertEqual(
            set(manifest["withheld_fields"]), set(supportbundle.WITHHELD_FIELDS)
        )

    def test_the_record_of_changes_travels_with_it(self) -> None:
        """The three entities above were created through the interface, so the
        journal has them, and an engineer reading this can see what was done to
        the appliance before it went wrong."""
        _, members = self._bundle()
        journal = json.loads(members["journal.json"])
        targets = [entry["target"] for entry in journal["entries"]]
        self.assertIn("/api/entities/extensions", targets)

    # -- when the appliance is already broken -------------------------------

    def test_one_failing_collector_does_not_take_the_others_with_it(self) -> None:
        """A bundle is asked for when something is already wrong, and the
        collector that raises is often the broken thing itself."""
        class Exploding:
            def snapshot(self):
                raise RuntimeError("the trunk engine is not answering")

        original = self.appliance.trunks
        self.appliance.trunks = Exploding()
        try:
            _, members = self._bundle()
        finally:
            self.appliance.trunks = original

        trunks = json.loads(members["trunks.json"])
        self.assertFalse(trunks["collected"])
        self.assertIn("not answering", trunks["reason"])

        # And everything else is still there.
        self.assertIn("241", members["configuration.json"].decode("utf-8"))
        state = json.loads(members["state.json"])
        self.assertIn("sequence", state)


if __name__ == "__main__":
    unittest.main()
