"""The two screens a technician reads before there is a browser to read one in.

The bootloader menu and the login screen are the only places this appliance
tells somebody standing in front of the machine where to find its console. Both
name an address and a port, and the port is the thing that has to be typed.

Both got it wrong in the same way, and the way is instructive: they spelled
each value on its own rather than spelling the sentence it sits in. Constraint
Two says quantities are written in words; the split this product is held to
adds that identifiers keep their digits. A port is an identifier -- but only
when something says it is a port. Handed the bare string ``8088`` with no
context, the spelling routine can only see a quantity, and dutifully wrote
"eight thousand eighty-eight" onto the one screen whose whole purpose is to
give a technician something to type into a browser.

It was also forty characters longer, and the bootloader truncates its menu line
to the width of its box, so the real image cut it off at "at port numbe" -- the
number itself did not survive at all. Photographed off the emulated machine
before it was fixed.
"""

from __future__ import annotations

import re
import subprocess
import unittest

from support import REPOSITORY_ROOT

LIBRARY = REPOSITORY_ROOT / "scripts" / "lib" / "common.sh"
SETTINGS = REPOSITORY_ROOT / "iso" / "lib" / "iso-common.sh"
BOOT_STAGE = REPOSITORY_ROOT / "iso" / "stages" / "five-master.sh"
CONSOLE_STAGE = REPOSITORY_ROOT / "iso" / "stages" / "three-configure.sh"

#: What the shipped image is built with.
ADDRESS = "192.168.100.10"
PORT = "8088"

#: The bootloader draws its menu inside a box, and a line longer than the box
#: is cut at the end -- which is where the port is. The two widths below are
#: read from the build settings rather than restated here, so that changing the
#: geometry changes what this measures.
def _width(function: str) -> int:
    completed = subprocess.run(
        ["bash", "-c", f'source "{SETTINGS}"\n{function}'],
        capture_output=True, text=True, timeout=60,
    )
    if completed.returncode != 0:
        raise AssertionError(f"the width could not be read: {completed.stderr}")
    return int(completed.stdout.strip())


def spell(text: str) -> str:
    """Run the installer's own spelling routine over one line."""
    completed = subprocess.run(
        ["bash", "-c", f'source "{LIBRARY}"\nspell_all "$1"', "--", text],
        capture_output=True, text=True, timeout=60,
    )
    if completed.returncode != 0:
        raise AssertionError(f"the spelling routine failed: {completed.stderr}")
    return completed.stdout


class WhatTheFirstScreensSayTests(unittest.TestCase):
    def test_a_port_named_as_one_keeps_its_digits(self) -> None:
        """The rule, before the screens that depend on it."""
        self.assertIn(PORT, spell(f"port number {PORT}"))
        self.assertIn(ADDRESS, spell(f"the address {ADDRESS}"))

    def test_a_bare_port_cannot_be_recognised_and_is_spelled(self) -> None:
        """Not a defect in the routine -- the reason the screens must not do it.

        With no context there is nothing to distinguish this from a count, and
        a routine that guessed would strip the digits from real quantities
        instead. The caller has to hand it the sentence.
        """
        self.assertNotIn(PORT, spell(PORT))
        self.assertIn("eight thousand", spell(PORT))

    def test_the_bootloader_menu_gives_a_port_that_can_be_typed(self) -> None:
        source = BOOT_STAGE.read_text(encoding="utf-8")
        self.assertNotRegex(
            source, r'spell_all "\$\{APPLIANCE_CONSOLE_PORT\}"',
            "the boot menu spells the port out of context, so it reads as a "
            "quantity and cannot be typed into a browser",
        )
        self.assertIn("at port number ${APPLIANCE_CONSOLE_PORT}", source)

    def test_the_login_screen_gives_a_port_that_can_be_typed(self) -> None:
        source = CONSOLE_STAGE.read_text(encoding="utf-8")
        self.assertNotRegex(
            source, r'spell_all "\$\{APPLIANCE_CONSOLE_PORT\}"',
            "the login screen spells the port out of context",
        )
        self.assertIn('spell_all "port number ${APPLIANCE_CONSOLE_PORT}"', source)

    def test_the_boot_menu_line_fits_inside_the_menu(self) -> None:
        """A line the bootloader truncates loses its end, and the port is at
        the end. Measured against the real values the image is built with."""
        width = _width("isolinux_nested_title_width")
        line = "The console " + spell(
            f"answers on {ADDRESS} at port number {PORT}"
        ).strip()
        self.assertLessEqual(
            len(line), width,
            f"the boot menu line is {len(line)} characters and a nested title "
            f"holds {width}, so it is cut off before the port: {line!r}",
        )
        self.assertTrue(
            line.rstrip().endswith(PORT),
            f"the port is not the readable end of the line: {line!r}",
        )

    def test_every_boot_menu_entry_fits_inside_the_menu(self) -> None:
        """The longest label was arriving as "no power manageme"."""
        width = _width("isolinux_entry_width")
        source = BOOT_STAGE.read_text(encoding="utf-8")
        labels = re.findall(r"^\s*MENU LABEL (.+)$", source, re.MULTILINE)
        self.assertGreaterEqual(len(labels), 4, "the menu lost its entries")
        for label in labels:
            self.assertLessEqual(
                len(label), width,
                f"the entry {label!r} is {len(label)} characters and an entry "
                f"holds {width}, so it is cut off",
            )

    def test_the_menu_geometry_is_written_into_the_menu(self) -> None:
        """The widths above only mean anything if the stage sets them."""
        source = BOOT_STAGE.read_text(encoding="utf-8")
        self.assertIn("MENU WIDTH ${ISOLINUX_MENU_WIDTH}", source)
        self.assertIn("MENU MARGIN ${ISOLINUX_MENU_MARGIN}", source)


class WhatTheFirmwareBootloaderDoesTests(unittest.TestCase):
    """The path every machine sold in the last decade takes.

    The shipped image dropped to a bare ``grub>`` prompt on this path and never
    started at all, reporting only ``configfile.mod not found``. A standalone
    bootloader keeps its modules in a memory disk inside itself and finds them
    through its prefix; the configuration it carried reset that prefix to a
    directory on the image, which has never held a module, so the first module
    it needed was missing and it stopped. Photographed off the emulated machine
    before it was fixed.
    """

    def setUp(self) -> None:
        self.source = BOOT_STAGE.read_text(encoding="utf-8")

    def test_the_bootloader_keeps_the_prefix_that_finds_its_own_modules(self) -> None:
        self.assertNotIn(
            "set prefix=", self.source,
            "the firmware bootloader resets its prefix, which sends it looking "
            "for its modules on the image, where there are none, and it stops "
            "at a prompt with the machine unstarted",
        )

    def test_the_menu_is_carried_inside_the_bootloader(self) -> None:
        """Not fetched from the image once it is running."""
        self.assertNotRegex(
            self.source, r"^\s*configfile /boot/grub/grub\.cfg\s*$",
            "the firmware bootloader reads its menu off the image rather than "
            "carrying it, which is one more thing that can be missing",
        )
        self.assertIn('printf \'%s\\n\' "${menu}"', self.source)

    def test_the_modules_the_boot_needs_are_named(self) -> None:
        """Left to the builder's default, they can change underneath us."""
        self.assertIn('--modules="${modules}"', self.source)
        for module in ("search", "iso9660", "linux", "normal", "all_video"):
            self.assertRegex(
                self.source, rf"modules=.*\b{module}\b",
                f"the boot needs the {module} module and does not ask for it",
            )

    def test_a_firmware_boot_path_that_cannot_be_built_stops_the_build(self) -> None:
        """It used to warn and carry on, and the image shipped without one.

        An image that starts only on a machine old enough to boot the legacy
        way is not the image this product means to ship, and a warning in the
        middle of a long build is not how anybody finds that out.
        """
        block = self.source.split("install_firmware_bootloader()", 1)[1]
        block = block.split("\nmaster_image()", 1)[0]
        self.assertNotIn(
            'log_warn "the firmware bootloader could not be built"', block,
            "a firmware bootloader that could not be built is only warned about",
        )
        self.assertIn("fail \"the firmware boot path could not be installed\"", block)
        self.assertNotIn("rm -f \"${efi_image}\"; return 0", block)

    def test_both_screens_still_spell_their_quantities(self) -> None:
        """The fix must not have turned Constraint Two off for these screens."""
        spelled = spell("this stage installed 3 packages and took 45 seconds")
        self.assertNotRegex(
            spelled, r"\b3\b|\b45\b",
            "a quantity survived as digits on a screen a technician reads",
        )
        self.assertIn("three", spelled)
        self.assertIn("forty-five", spelled)


class WhatTheBuildReportsAboutItselfTests(unittest.TestCase):
    """These lines are read aloud down a telephone. They have to be sentences."""

    def duration(self, seconds: int) -> str:
        completed = subprocess.run(
            [
                "bash", "-c",
                f'source "{LIBRARY}"\n'
                f'source "{REPOSITORY_ROOT / "iso" / "lib" / "iso-common.sh"}"\n'
                f'eval "$(sed -n "/^spell_duration_seconds()/,/^}}/p" '
                f'"{REPOSITORY_ROOT / "iso" / "build-iso.sh"}")"\n'
                f"spell_duration_seconds {seconds}",
            ],
            capture_output=True, text=True, timeout=60,
        )
        if completed.returncode != 0:
            raise AssertionError(completed.stderr)
        return completed.stdout.strip()

    def test_the_unit_agrees_with_the_number_in_front_of_it(self) -> None:
        self.assertNotIn("minute or minutes", self.duration(95))
        self.assertEqual(self.duration(95), "one minute and thirty-five seconds")
        self.assertEqual(self.duration(61), "one minute and one second")
        self.assertEqual(self.duration(125), "two minutes and five seconds")
        self.assertEqual(self.duration(1), "one second")
        self.assertEqual(self.duration(45), "forty-five seconds")


if __name__ == "__main__":
    unittest.main()
