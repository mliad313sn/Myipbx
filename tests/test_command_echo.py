"""A command the installer says it will run has to be the command it will run.

The rehearsal exists so that somebody can read what is about to happen to their
disk before it happens. It printed

    sgdisk --new=1:0:+1M --change-name=1:crossbar legacy boot /dev/vda

for a command that was given ``crossbar legacy boot`` as one argument. The
command itself was correct -- the script quoted it properly -- but the line
reporting it did not, and pasted back it names two extra disks. Found by
running the rehearsal and reading what came out.
"""

from __future__ import annotations

import shlex
import subprocess
import unittest

from support import REPOSITORY_ROOT

LIBRARY = REPOSITORY_ROOT / "scripts" / "lib" / "common.sh"
INSTALLER = REPOSITORY_ROOT / "iso" / "installer" / "crossbar-install-to-disk.sh"


def quote(*arguments: str) -> str:
    """Run the library's own quoting over one argument list."""
    script = f'source "{LIBRARY}"\nquote_arguments "$@"'
    completed = subprocess.run(
        ["bash", "-c", script, "--", *arguments],
        capture_output=True, text=True, timeout=60,
    )
    if completed.returncode != 0:
        raise AssertionError(completed.stderr)
    return completed.stdout


class WhatARehearsalPrintsTests(unittest.TestCase):
    def test_an_argument_holding_spaces_stays_one_argument(self) -> None:
        line = quote("sgdisk", "--change-name=1:crossbar legacy boot", "/dev/vda")
        self.assertEqual(
            shlex.split(line),
            ["sgdisk", "--change-name=1:crossbar legacy boot", "/dev/vda"],
        )

    def test_an_ordinary_command_is_left_alone(self) -> None:
        """Quoting everything would make every line harder to read for nothing."""
        self.assertEqual(quote("chmod", "0750", "/etc/crossbar/tls"),
                         "chmod 0750 /etc/crossbar/tls")

    def test_a_quote_inside_an_argument_survives(self) -> None:
        line = quote("printf", "it's here")
        self.assertEqual(shlex.split(line), ["printf", "it's here"])

    def test_the_shell_is_never_handed_something_it_would_act_on(self) -> None:
        for argument in (
            "a; rm -rf /", "a && b", "$(whoami)", "`whoami`", "a|b", "a>b",
            "a\nb", "*", "~root", "a b\tc",
        ):
            with self.subTest(argument=argument):
                self.assertEqual(shlex.split(quote("echo", argument)),
                                 ["echo", argument])

    def test_an_empty_argument_does_not_vanish(self) -> None:
        self.assertEqual(shlex.split(quote("echo", "", "after")), ["echo", "", "after"])

    def test_the_whole_line_survives_a_round_trip(self) -> None:
        """Which is the only property that actually matters."""
        arguments = [
            "sgdisk", "--zap-all", "--new=3:0:0", "--typecode=3:8300",
            "--change-name=3:crossbar root", "/dev/vda",
        ]
        self.assertEqual(shlex.split(quote(*arguments)), arguments)


class WhatTheDiskInstallerRehearsesTests(unittest.TestCase):
    """End to end, against the real installer, against a real disk name."""

    def rehearse(self, disk: str = "/dev/null") -> str:
        completed = subprocess.run(
            ["bash", str(INSTALLER), "--rehearse", "--disk", disk],
            capture_output=True, text=True, timeout=300,
            cwd=str(REPOSITORY_ROOT),
        )
        return completed.stdout + completed.stderr

    def test_every_command_it_says_it_would_run_can_be_read_back(self) -> None:
        output = self.rehearse()
        lines = [
            line.split("would run:", 1)[1].strip()
            for line in output.splitlines()
            if "would run:" in line and "inside the installed system" not in line
        ]
        self.assertTrue(lines, "the rehearsal reported no commands at all")
        for line in lines:
            with self.subTest(line=line):
                # Raises on an unbalanced quote, which is the failure this
                # exists to catch.
                parsed = shlex.split(line)
                self.assertTrue(parsed)

    def test_a_partition_name_reaches_the_disk_tool_as_one_argument(self) -> None:
        output = self.rehearse()
        partitioning = [
            line for line in output.splitlines()
            if "would run: sgdisk" in line and "--change-name" in line
        ]
        self.assertTrue(partitioning, "the rehearsal never reported partitioning")
        for line in partitioning:
            arguments = shlex.split(line.split("would run:", 1)[1].strip())
            names = [
                argument for argument in arguments
                if argument.startswith("--change-name=")
            ]
            self.assertEqual(len(names), 3, arguments)
            for name in names:
                # Everything after the partition number is the name, and the
                # name is allowed to contain spaces -- so long as it arrived as
                # one argument, which is what this checks.
                self.assertIn("crossbar", name)
            # And nothing that is not an option or the disk is left loose.
            loose = [
                argument for argument in arguments[1:]
                if not argument.startswith("--") and not argument.startswith("/dev/")
            ]
            self.assertEqual(loose, [], f"these would be read as extra disks: {loose}")


if __name__ == "__main__":
    unittest.main()
