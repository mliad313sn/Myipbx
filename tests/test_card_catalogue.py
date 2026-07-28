"""One table of interface card identifiers, and only one.

There were two. One lived in the control plane and named the card on the
dashboard; the other lived in the pre-flight script and told a technician what
was in the machine before anything was installed. They disagreed on ten of the
eleven identifiers they shared.

The disagreement was not cosmetic. Device zero two zero five was a four port
analogue card in one table and a dual span digital card in the other. The
driver source this product compiles into its own image says it is the digital
one. A technician told they were holding an analogue card would have been
offered the wrong questions by the wizard, and the wrong driver module named in
the diagnosis when the card did not come up.

Four identifiers between the two tables were claimed by no driver at all, so
they described cards that do not exist.

The tests below hold three things: that there is one file, that both consumers
read it, and that what it says about a card is internally consistent -- a name
in the basic rate family is not described as an analogue card, a four span card
is not described as a single span one. The rows themselves came out of the
driver source mechanically; that provenance is recorded in the file and cannot
be re-checked here, because the driver source is not in this repository.
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

from appliance import hardware
from support import REPOSITORY_ROOT

CATALOGUE = REPOSITORY_ROOT / "share" / "digium-cards.tsv"
PREFLIGHT = REPOSITORY_ROOT / "scripts" / "preflight-check.sh"


def _rows() -> list[tuple[str, str, str, str]]:
    rows = []
    for line in CATALOGUE.read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) != 4 or parts[0] == "device":
            continue
        rows.append(tuple(parts))  # type: ignore[arg-type]
    return rows


class TheFileTests(unittest.TestCase):
    def test_the_catalogue_exists_and_has_rows(self) -> None:
        self.assertTrue(CATALOGUE.is_file())
        self.assertGreater(len(_rows()), 20)

    def test_every_identifier_is_four_hexadecimal_digits_and_appears_once(self) -> None:
        seen: set[str] = set()
        for device, _, _, _ in _rows():
            with self.subTest(device=device):
                self.assertRegex(device, r"^[0-9a-f]{4}$")
                self.assertNotIn(device, seen, "the same identifier is listed twice")
                seen.add(device)

    def test_every_row_names_a_model_a_module_and_a_description(self) -> None:
        for device, model, module, description in _rows():
            with self.subTest(device=device):
                self.assertTrue(model.strip())
                self.assertRegex(module, r"^[a-z0-9_]+$")
                self.assertTrue(description.strip())

    def test_the_file_records_which_driver_source_it_was_read_from(self) -> None:
        """Without this the table is a claim with nothing behind it."""
        text = CATALOGUE.read_text(encoding="utf-8")
        self.assertIn("PROVENANCE", text)
        self.assertRegex(text, r"DAHDI Linux v\d+\.\d+\.\d+")

    def test_the_file_does_not_claim_any_card_has_been_run(self) -> None:
        """That a driver claims an identifier is not that the card works.

        No physical Digium card has been in a machine running this appliance.
        The file must not read as though one had.
        """
        text = CATALOGUE.read_text(encoding="utf-8")
        self.assertIn("has been run against that card", text)


class WhatTheRowsSayTests(unittest.TestCase):
    """A model name and its description must not contradict each other."""

    #: What each family of model name is, drawn from the model name alone.
    FAMILIES = (
        (re.compile(r"\bB\d{3}P\b"), "basic rate"),
        (re.compile(r"\bTE\d"), "digital"),
        (re.compile(r"\bTDM\d|\bAEX\d|\bA[48][AB]\b"), "analogue"),
    )

    def test_a_model_name_and_its_description_agree(self) -> None:
        for device, model, _, description in _rows():
            for pattern, expected in self.FAMILIES:
                if not pattern.search(model):
                    continue
                with self.subTest(device=device, model=model):
                    self.assertIn(
                        expected, description,
                        f"{model} is described as: {description}",
                    )
                break

    #: In a digital card's model number the first digit after TE is the span
    #: count: a TE two zero five P is a dual span card, a TE four one zero P a
    #: four span one, a TE eight two zero an eight span one.
    SPANS_BY_LEADING_DIGIT = {"1": "single", "2": "dual", "4": "four", "8": "eight"}

    def test_the_span_count_in_a_digital_description_matches_its_model(self) -> None:
        """A TE four ten P is a four span card, and must not be called a dual.

        Some identifiers name two models. Where both imply the same count --
        the TE one three one and the TE one three three differ in how they
        attach, not in how many spans they carry -- the description states it.
        Where they disagree, the appliance cannot tell which card it is holding
        from the bus alone, and the row has to say so rather than pick one.
        """
        for device, model, _, description in _rows():
            implied = {
                self.SPANS_BY_LEADING_DIGIT[match.group(1)]
                for match in re.finditer(r"\bTE(\d)\d\d(?!\d)", model)
                # A trailing word boundary would not match TE four zero five P:
                # the digit and the letter after it are both word characters, so
                # there is no boundary between them and every P suffixed model
                # silently escaped this check.
                if match.group(1) in self.SPANS_BY_LEADING_DIGIT
            }
            if not implied:
                continue
            with self.subTest(device=device, model=model):
                if len(implied) > 1:
                    self.assertIn(
                        "serial number", description,
                        f"{model} names cards with different span counts but "
                        f"the description states one: {description}",
                    )
                else:
                    self.assertIn(
                        implied.pop(), description,
                        f"{model} is described as: {description}",
                    )

    def test_the_four_invented_identifiers_are_gone(self) -> None:
        """No driver in the source claims these; they described cards that do
        not exist, and a card that never existed cannot be diagnosed."""
        devices = {device for device, _, _, _ in _rows()}
        for invented in ("8002", "8005", "8006", "0800"):
            with self.subTest(device=invented):
                self.assertNotIn(invented, devices)


class TheInstalledAppliancePathTests(unittest.TestCase):
    """The catalogue has to be on the machine, not only in the repository.

    The first draft resolved one path relative to the module, which found the
    file here -- where every test runs -- and would have found nothing on a
    real appliance, because the installer copies the modules and nothing
    beside them. Every fitted card would have read "an unrecognised Digium
    interface card" in service while this suite stayed green. These two tests
    are the ones that would have caught it.
    """

    INSTALLER = REPOSITORY_ROOT / "scripts" / "stage-five-appliance-service.sh"

    def test_the_installer_lays_the_catalogue_down_beside_the_package(self) -> None:
        """Run the installer's own function against a temporary prefix.

        Reading the script's text would pass on a script that names the right
        path in a branch that never runs. This lays the package down and looks
        at what is on disk afterwards.
        """
        with tempfile.TemporaryDirectory(prefix="crossbar-prefix-") as name:
            prefix = Path(name)
            result = subprocess.run(
                ["bash", "-c",
                 f'source "{self.INSTALLER}" >/dev/null 2>&1 || true; '
                 f'install_control_plane'],
                capture_output=True, text=True, cwd=str(REPOSITORY_ROOT),
                env={**os.environ, "APPLIANCE_PREFIX": str(prefix)},
            )
            self.assertEqual(
                result.returncode, 0,
                f"laying the package down failed:\n{result.stdout}\n{result.stderr}",
            )
            self.assertTrue(
                (prefix / "appliance" / "hardware.py").is_file(),
                "the modules were not installed, so this test proved nothing",
            )
            laid_down = prefix / "share" / "digium-cards.tsv"
            self.assertTrue(
                laid_down.is_file(),
                "the interface card catalogue was not installed, so a real "
                "appliance would report every fitted card as unrecognised",
            )
            self.assertEqual(
                laid_down.read_text(encoding="utf-8"),
                CATALOGUE.read_text(encoding="utf-8"),
            )

    def test_the_module_looks_where_the_installer_puts_it(self) -> None:
        """The two halves have to name the same place."""
        looked_in = {str(path) for path in hardware._CATALOGUE_LOCATIONS}
        self.assertIn("/opt/crossbar/share/digium-cards.tsv", looked_in)

    def test_the_image_carries_it_and_checks_that_it_did(self) -> None:
        """The same gap, in the other place the package is laid down.

        The image build copies the modules and the console by glob. Data files
        are neither, so the catalogue would have been missing from a finished
        image exactly as it was missing from an installation -- and an image is
        harder to inspect afterwards than a machine is.
        """
        payload = (REPOSITORY_ROOT / "iso" / "stages" / "two-payload.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            '"${REPOSITORY_ROOT}"/share/*', payload,
            "the image build does not carry the data the control plane reads",
        )
        self.assertIn(
            "/opt/crossbar/share/digium-cards.tsv", payload,
            "the image build never checks that the catalogue arrived, so a "
            "silent omission would ship",
        )

    def test_the_preflight_script_looks_there_too(self) -> None:
        text = PREFLIGHT.read_text(encoding="utf-8")
        self.assertIn("APPLIANCE_PREFIX:-/opt/crossbar}/share/digium-cards.tsv", text)


class BothConsumersReadTheSameFileTests(unittest.TestCase):
    """The point of the file, asserted rather than assumed."""

    def test_the_control_plane_reads_it(self) -> None:
        rows = _rows()
        self.assertEqual(len(hardware._CARD_CATALOGUE), len(rows))
        for device, model, module, description in rows:
            with self.subTest(device=device):
                self.assertEqual(
                    hardware.describe_card(int(device, 16)),
                    (model, module, description),
                )

    def test_the_preflight_script_reads_it(self) -> None:
        """Run the script's own lookup, so this tests the shipped code path."""
        for device, model, _, _ in _rows():
            with self.subTest(device=device):
                result = subprocess.run(
                    ["bash", "-c",
                     f'source "{PREFLIGHT}" >/dev/null 2>&1 || true; '
                     f'digium_card_name d161 {device}'],
                    capture_output=True, text=True, cwd=str(REPOSITORY_ROOT),
                )
                self.assertIn(
                    model, result.stdout,
                    f"the pre-flight script does not name {device} as {model}",
                )

    def test_the_preflight_script_writes_no_identifiers_of_its_own(self) -> None:
        """Two tables is how the disagreement happened; one is the fix."""
        text = PREFLIGHT.read_text(encoding="utf-8")
        stray = re.findall(r"d161:[0-9a-fA-F]{4}", text)
        self.assertEqual(
            stray, [],
            "the pre-flight script has started keeping its own copy of the "
            "identifiers again",
        )

    def test_an_unknown_identifier_is_still_reported_as_a_digium_card(self) -> None:
        """The table improves a description; it must not gate detection."""
        model, module, description = hardware.describe_card(0x9999)
        self.assertIn("unrecognised", model)
        self.assertIn("Digium", model)
        self.assertTrue(module)
        self.assertTrue(description)

    def test_a_missing_catalogue_degrades_rather_than_fails(self) -> None:
        """An appliance that refused to enumerate its hardware because a text
        file was missing would be worse than one that says so and carries on."""
        empty = hardware._load_catalogue(Path("/nowhere/digium-cards.tsv"))
        self.assertEqual(empty, {})


if __name__ == "__main__":
    unittest.main()
