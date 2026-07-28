"""The one script in this product that destroys data, and what stops it.

Everything else here can be undone. This partitions a disk, and a disk that has
been partitioned does not remember what was on it. The script had no tests at
all, which is the wrong way round: the further a thing is from recoverable, the
more it is worth pinning.

The tests run it in rehearsal, where every refusal is reported rather than
enforced, so a single run reveals every guard the arguments would trip. That is
the only way to see them all without a machine to sacrifice.

The finding that prompted this file is the one about the live medium. The check
that stops the script repartitioning the disk it is itself running from was
written as "if the live disk is known and the target is it, refuse" -- so a
medium whose device could not be named, which happens on an overlay, a loop
device or a network mount, silently turned the check off. The script logged a
warning among thirty other lines and carried on to destroy the medium it was
reading from, partway through reading it.
"""

from __future__ import annotations

import os
import re
import subprocess
import unittest
from pathlib import Path

from support import IDENTIFIER_SHAPES, REPOSITORY_ROOT

INSTALLER = REPOSITORY_ROOT / "iso" / "installer" / "crossbar-install-to-disk.sh"

#: A refusal that does not say what to do about it leaves the technician
#: exactly where they were. The same rule the pre-flight check is held to.
REMEDY_VERBS = re.compile(
    r"\b(install|installed|set|run|free|mount|unmount|name|choose|remove|"
    r"stop|correct|replace|use|point)\b"
)


def _is_a_command(line: str) -> bool:
    """A line showing a command line, as against a line of prose about one."""
    return "would run" in line or " running: " in line


def rehearse(disk: str | None = "/dev/sda", **environment: str) -> str:
    """Run the installer in rehearsal and return everything it said."""
    arguments = [str(INSTALLER), "--dry-run", "--assume-yes"]
    if disk is not None:
        arguments += ["--disk", disk]
    completed = subprocess.run(
        arguments,
        capture_output=True,
        text=True,
        timeout=120,
        env={**os.environ, **environment},
        cwd=str(REPOSITORY_ROOT),
    )
    return completed.stdout + completed.stderr


class TheLiveMediumTests(unittest.TestCase):
    """The check that stops the script eating the disk it is running from."""

    def test_an_unidentifiable_live_disk_refuses_rather_than_carrying_on(self) -> None:
        """This machine is not a live medium, so the disk cannot be named.

        Before the fix this produced a warning and the installation continued
        with the safety check disabled. It now refuses.
        """
        report = rehearse()
        self.assertIn(
            "the live medium's disk is not known", report,
            "an unidentifiable live medium no longer stops the installation, "
            "so nothing prevents it repartitioning the disk it is running from",
        )

    def test_the_refusal_explains_what_it_could_not_prove(self) -> None:
        report = rehearse()
        self.assertIn("cannot be shown to be a different disk", report)
        self.assertIn("would destroy the medium partway through", report)

    def test_the_refusal_names_the_way_past_it(self) -> None:
        """An operator who knows the target is a different disk has to be able
        to say so, or the script is unusable on the media it cannot name."""
        report = rehearse()
        self.assertIn("CROSSBAR_LIVE_DISK", report)

    def test_naming_the_live_disk_gets_past_the_check(self) -> None:
        report = rehearse(CROSSBAR_LIVE_DISK="/dev/sdz")
        self.assertIn("proceeding on your word", report)
        self.assertNotIn("the live medium's disk is not known", report)

    def test_naming_the_target_as_the_live_disk_still_refuses(self) -> None:
        """The override says where the medium is; it does not say the target is
        safe. Pointing both at one disk must still be refused."""
        report = rehearse(disk="/dev/sdz", CROSSBAR_LIVE_DISK="/dev/sdz")
        self.assertIn(
            "is the one the live medium is on", report,
            "the override was taken as permission to install onto the live disk",
        )


class TheDiskItWasPointedAtTests(unittest.TestCase):
    """What the script will and will not accept as a target."""

    def test_naming_no_disk_at_all_is_refused(self) -> None:
        report = rehearse(disk=None)
        self.assertIn("no disk was named", report)

    def test_a_path_that_is_not_a_device_is_refused(self) -> None:
        report = rehearse(disk="/home/somebody/a-file")
        self.assertIn("must be named by its device path", report)

    def test_a_path_that_is_not_a_block_device_is_refused(self) -> None:
        report = rehearse(disk="/dev/null")
        self.assertIn("not a block device", report)

    def test_a_partition_is_refused_rather_than_installed_onto(self) -> None:
        """A partition table inside a partition boots nothing."""
        source = INSTALLER.read_text(encoding="utf-8")
        self.assertIn("rather than a whole disk", source)

    def test_a_mounted_disk_is_refused(self) -> None:
        """Something mounted is something whose owner did not expect it to go."""
        source = INSTALLER.read_text(encoding="utf-8")
        self.assertIn("has a mounted filesystem on it and will not be touched", source)

    def test_a_disk_carrying_swap_in_use_is_refused(self) -> None:
        source = INSTALLER.read_text(encoding="utf-8")
        self.assertIn("carries swap that is in use", source)


class WhatItSaysBeforeItActsTests(unittest.TestCase):
    def test_a_rehearsal_writes_nothing_and_says_so(self) -> None:
        report = rehearse()
        self.assertIn("nothing will be written", report)

    def test_it_names_what_will_be_destroyed_before_destroying_it(self) -> None:
        report = rehearse()
        self.assertIn("about to be removed and cannot be recovered", report)

    def test_every_refusal_it_can_be_made_to_produce_names_a_remedy(self) -> None:
        reports = [
            rehearse(),
            rehearse(disk="/dev/null"),
            rehearse(disk="/home/somebody/a-file"),
            rehearse(disk=None),
        ]
        refusals = [
            line
            for report in reports
            for line in report.splitlines()
            if "a real installation would stop here" in line or "error" in line
        ]
        self.assertTrue(refusals, "no refusal was produced to inspect")

        without = [
            line for line in refusals
            if not REMEDY_VERBS.search(line)
            # A line that only restates the fault is acceptable when the line
            # after it carries the action; those are checked by name above.
            and "will not be touched" not in line
            and "cannot be shown to be safe" not in line
            and "is not known" not in line
        ]
        self.assertEqual(
            without, [],
            "these refusals name no action the technician can take: "
            + "; ".join(without[:3]),
        )

    def test_the_report_spells_its_quantities(self) -> None:
        """Constraint Two, on the one output a technician reads at the console
        of a machine with no browser on it."""
        report = rehearse()
        # The shapes come from the suite's shared list rather than a copy kept
        # here. A copy is how the pre-flight suite came to disagree with the
        # rest of the appliance about what an identifier is, and it is how this
        # test first reported a kernel release as an escaped quantity.
        offenders = []
        for line in report.splitlines():
            if _is_a_command(line):
                # Handled by the test below: a command is one identifier from
                # end to end, because it is a line somebody retypes.
                continue
            remainder = line
            for shape in IDENTIFIER_SHAPES:
                remainder = re.sub(shape, " ", remainder)
            if re.search(r"\d", remainder):
                offenders.append(line)
        self.assertEqual(
            offenders, [],
            f"a quantity reached the technician as digits: {offenders[:3]}",
        )

    def test_a_command_is_shown_as_the_thing_a_person_would_retype(self) -> None:
        """The rehearsal exists to be read before a disk is destroyed.

        Every other line the installer prints is prose and spells its
        quantities. A command is not prose. Spelled, the partitioning command
        came out as "sgdisk --new=1:0:+one M --typecode=one:ef zero two", which
        cannot be run and cannot even be checked by eye against what the
        installer means to do -- so the one output whose entire purpose is
        review was the one output nobody could review.
        """
        report = rehearse()
        commands = [line for line in report.splitlines() if _is_a_command(line)]
        self.assertTrue(commands, "the rehearsal named no command to inspect")

        spelled = [
            line for line in commands
            if re.search(r"\b(one|two|three|zero|five hundred|eight thousand)\b", line)
        ]
        self.assertEqual(
            spelled, [],
            "a command was spelled and so cannot be run or checked: "
            + "; ".join(spelled[:2]),
        )

        # The partition type codes are the clearest case: "ef zero two" is not
        # a type code, and a technician checking the plan against what they
        # meant cannot check it against that.
        partitioning = " ".join(line for line in commands if "--typecode" in line)
        self.assertTrue(partitioning, "the partitioning command was not shown")
        self.assertIn("--typecode=1:ef02", partitioning)
        self.assertIn("--typecode=3:8300", partitioning)


if __name__ == "__main__":
    unittest.main()
