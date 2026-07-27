"""The preflight check — the gate that runs before the installer changes anything.

The check is exercised against fixture roots rather than against real hardware,
because the properties that matter are all observable without a card fitted:
that it is well formed, that it spells every numeral, that it separates a
blocking failure from a warning, that it exits non zero only on the former, and
that every failing line tells the technician what to do about it.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from support import IDENTIFIER_SHAPES, REPOSITORY_ROOT

PREFLIGHT = REPOSITORY_ROOT / "scripts/preflight-check.sh"
INSTALLER = REPOSITORY_ROOT / "scripts/install-appliance.sh"

#: The room the disk checks ask for, in mebibytes, plus a little headroom.  A
#: fixture root is a real directory on a real filesystem, so a machine that is
#: itself nearly full cannot host the passing scenario and says so rather than
#: reporting a failure it did not cause.
REQUIRED_FIXTURE_MEBIBYTES = 2048 + 256

#: A failing line has to end in something the technician can do.  These are the
#: verbs the script's remedies are written with.
REMEDY_VERBS = re.compile(
    r"\b(install|installed|set|run|free|mount|restore|check|replace|reseat|"
    r"point|stop|remove|choose|correct|fit|fitted|mark)\b"
)


def run_preflight(
    arguments: list[str] | None = None,
    environment: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run the check and return the finished process, output combined."""
    prepared = dict(os.environ)
    prepared.pop("REHEARSAL", None)
    prepared.pop("APPLIANCE_INTERFACE", None)
    prepared.pop("ROOT_PREFIX", None)
    prepared.update(environment or {})
    return subprocess.run(
        ["bash", str(PREFLIGHT), *(arguments or [])],
        capture_output=True,
        text=True,
        timeout=120,
        env=prepared,
    )


def combined(completed: subprocess.CompletedProcess[str]) -> str:
    return completed.stdout + completed.stderr


def kernel_release() -> str:
    return os.uname().release


def build_satisfied_root(base: Path, with_card: bool = True) -> Path:
    """A fixture root on which every condition the check inspects is met."""
    root = base / "satisfied-machine"
    (root / "lib/modules" / kernel_release() / "build").mkdir(parents=True, exist_ok=True)
    (root / "sys/class/net/eth0").mkdir(parents=True, exist_ok=True)
    (root / "usr/src").mkdir(parents=True, exist_ok=True)
    (root / "opt").mkdir(parents=True, exist_ok=True)
    (root / "var").mkdir(parents=True, exist_ok=True)

    devices = root / "sys/bus/pci/devices"
    devices.mkdir(parents=True, exist_ok=True)
    if with_card:
        # The identifier the repository's own hardware fixture uses: the
        # Wildcard A four A, a four port analogue card, which is what the
        # analogue express driver claims for this identifier.
        slot = devices / "0000:02:0a.0"
        slot.mkdir(parents=True, exist_ok=True)
        (slot / "vendor").write_text("0xd161\n", encoding="utf-8")
        (slot / "device").write_text("0x800f\n", encoding="utf-8")
    else:
        slot = devices / "0000:00:02.0"
        slot.mkdir(parents=True, exist_ok=True)
        (slot / "vendor").write_text("0x8086\n", encoding="utf-8")
        (slot / "device").write_text("0x100e\n", encoding="utf-8")
    return root


def build_bare_root(base: Path) -> Path:
    """A fixture root with no headers, no card and no interface of that name."""
    root = base / "bare-machine"
    (root / "sys/class/net/lo").mkdir(parents=True, exist_ok=True)
    (root / "usr/src").mkdir(parents=True, exist_ok=True)
    (root / "opt").mkdir(parents=True, exist_ok=True)
    (root / "var").mkdir(parents=True, exist_ok=True)
    return root


def has_room(path: Path) -> bool:
    usage = shutil.disk_usage(path)
    return usage.free >= REQUIRED_FIXTURE_MEBIBYTES * 1024 * 1024


class WellFormedTests(unittest.TestCase):
    """The script has to be sound before anything it says can be trusted."""

    def test_the_script_exists_and_is_executable(self) -> None:
        self.assertTrue(PREFLIGHT.is_file(), "the preflight check is missing")
        self.assertTrue(os.access(PREFLIGHT, os.X_OK), "the preflight check is not executable")

    def test_the_script_is_well_formed(self) -> None:
        completed = subprocess.run(
            ["bash", "-n", str(PREFLIGHT)], capture_output=True, text=True, timeout=60
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_the_script_arms_every_shell_guard(self) -> None:
        source = PREFLIGHT.read_text(encoding="utf-8")
        for guard in ("set -o errexit", "set -o nounset", "set -o pipefail"):
            with self.subTest(guard=guard):
                self.assertIn(guard, source)

    def test_the_script_uses_the_shared_library_rather_than_its_own_logging(self) -> None:
        source = PREFLIGHT.read_text(encoding="utf-8")
        self.assertIn("lib/common.sh", source)
        self.assertIn("spell_integer", source)

    def test_the_allocation_audit_is_reused_rather_than_reimplemented(self) -> None:
        source = PREFLIGHT.read_text(encoding="utf-8")
        self.assertIn("verify-no-dhcp.sh", source)

    def test_every_pipeline_result_is_guarded_before_it_is_used(self) -> None:
        """A pipeline that fails under pipefail once aborted a build here."""
        source = PREFLIGHT.read_text(encoding="utf-8")
        unguarded: list[str] = []
        for number, line in enumerate(source.splitlines(), start=1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            # An assignment from a command substitution containing a pipe.
            if re.search(r'^\w+="\$\([^)]*\|', stripped) and "||" not in stripped:
                unguarded.append(f"line {number}: {stripped}")
        self.assertEqual(unguarded, [], "; ".join(unguarded))

    def test_the_installer_runs_the_check_before_it_changes_anything(self) -> None:
        source = INSTALLER.read_text(encoding="utf-8")
        self.assertIn("preflight-check.sh", source)
        # The call must be able to stop the installation, not merely report.
        self.assertRegex(source, r"if ! bash \"\$\{SCRIPT_DIR\}/preflight-check\.sh\"")

    def test_the_usage_message_is_offered_and_costs_nothing(self) -> None:
        completed = run_preflight(["--help"])
        self.assertEqual(completed.returncode, 0)
        self.assertIn("preflight-check.sh", combined(completed))

    def test_an_unrecognised_argument_is_refused(self) -> None:
        completed = run_preflight(["--allocate-addresses"])
        self.assertEqual(completed.returncode, 2)


class NumeralSpellingTests(unittest.TestCase):
    """Constraint Two: no digit character may reach an operator's eye."""

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory(prefix="myipbx-preflight-")
        self.base = Path(self.directory.name)

    def tearDown(self) -> None:
        self.directory.cleanup()

    #: Shapes the report is allowed to print as digits, because a technician
    #: uses them rather than counts them. A kernel release is compared against
    #: a headers package, an interface name against what the machine calls it,
    #: a path against what is on disk. This is the rule the appliance follows
    #: everywhere; the preflight check is where its absence was noticed, since
    #: it once told a technician to configure an interface named "ethzero".
    #: The shapes come from the suite's shared list rather than from a copy
    #: kept here. There was a copy kept here, and it drifted: it did not know
    #: the shapes for an error number, a socket address pair, or the analogue
    #: express card family, so a card model printed by the pre-flight check
    #: read as a quantity that had escaped spelling. Four copies of one rule is
    #: three too many.
    _IDENTIFIER_SHAPES = IDENTIFIER_SHAPES

    def _assert_no_digit(self, text: str) -> None:
        """Every quantity is spelled; identifiers keep their digits.

        Each line is stripped of what is legitimately an identifier, and any
        digit still standing is a quantity that escaped.
        """
        offenders = []
        for line in text.splitlines():
            remainder = line
            for shape in self._IDENTIFIER_SHAPES:
                remainder = re.sub(shape, " ", remainder)
            if re.search(r"\d", remainder):
                offenders.append(line)
        self.assertEqual(
            offenders, [], f"a quantity was emitted as digits: {offenders}"
        )

    def test_the_passing_report_spells_every_numeral(self) -> None:
        root = build_satisfied_root(self.base)
        completed = run_preflight(
            environment={"ROOT_PREFIX": str(root), "APPLIANCE_INTERFACE": "eth0"}
        )
        self._assert_no_digit(combined(completed))

    def test_the_failing_report_spells_every_numeral(self) -> None:
        root = build_bare_root(self.base)
        completed = run_preflight(
            environment={"ROOT_PREFIX": str(root), "APPLIANCE_INTERFACE": "eth0"}
        )
        self._assert_no_digit(combined(completed))

    def test_the_report_on_this_machine_spells_every_numeral(self) -> None:
        self._assert_no_digit(combined(run_preflight()))

    def test_the_usage_message_spells_every_numeral(self) -> None:
        self._assert_no_digit(combined(run_preflight(["--help"])))

    def test_the_summary_counts_are_spelled_not_printed(self) -> None:
        completed = run_preflight()
        self.assertRegex(
            combined(completed),
            r"the preflight check reports [a-z -]+ satisfied, [a-z -]+ worth knowing, "
            r"and [a-z -]+ blocking",
        )


class OutcomeTests(unittest.TestCase):
    """A blocking failure, a warning and a pass must be told apart."""

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory(prefix="myipbx-preflight-")
        self.base = Path(self.directory.name)

    def tearDown(self) -> None:
        self.directory.cleanup()

    def test_a_satisfied_machine_exits_zero(self) -> None:
        if not has_room(self.base):
            self.skipTest("this machine has too little free space to host the fixture")
        if shutil.which("cc") is None and shutil.which("gcc") is None:
            self.skipTest("this machine has no compiler, so the toolchain check cannot pass")
        if shutil.which("make") is None:
            self.skipTest("this machine has no make utility")

        root = build_satisfied_root(self.base)
        completed = run_preflight(
            environment={"ROOT_PREFIX": str(root), "APPLIANCE_INTERFACE": "eth0"}
        )
        self.assertEqual(
            completed.returncode, 0,
            f"a satisfied machine was refused: {combined(completed)}",
        )
        self.assertIn("the installation may proceed", combined(completed))

    def test_a_missing_kernel_header_tree_blocks_the_installation(self) -> None:
        root = build_bare_root(self.base)
        completed = run_preflight(environment={"ROOT_PREFIX": str(root)})
        self.assertEqual(completed.returncode, 1)
        self.assertRegex(combined(completed), r"blocking: kernel headers .* are missing")

    def test_an_absent_interface_card_is_a_warning_and_not_a_refusal(self) -> None:
        """An appliance is often prepared before the card is fitted."""
        if not has_room(self.base):
            self.skipTest("this machine has too little free space to host the fixture")

        root = build_satisfied_root(self.base, with_card=False)
        completed = run_preflight(
            environment={"ROOT_PREFIX": str(root), "APPLIANCE_INTERFACE": "eth0"}
        )
        report = combined(completed)
        self.assertIn("no Digium interface card is visible", report)
        self.assertNotIn("blocking: no Digium interface card", report)
        self.assertNotEqual(
            completed.returncode, 1,
            "an absent card refused an installation that should have proceeded",
        )

    def test_a_fitted_card_is_named_and_not_merely_counted(self) -> None:
        root = build_satisfied_root(self.base, with_card=True)
        completed = run_preflight(environment={"ROOT_PREFIX": str(root)})
        report = combined(completed)
        self.assertIn("a legacy interface card is fitted", report)
        # The model, spoken as a technician would read it off the card.
        self.assertIn("Wildcard A4A", report)

    def test_an_unknown_card_is_reported_as_unknown_rather_than_guessed(self) -> None:
        root = build_bare_root(self.base)
        slot = root / "sys/bus/pci/devices/0000:ff:1f.7"
        slot.mkdir(parents=True, exist_ok=True)
        (slot / "vendor").write_text("0xd161\n", encoding="utf-8")
        (slot / "device").write_text("0x7777\n", encoding="utf-8")

        completed = run_preflight(environment={"ROOT_PREFIX": str(root)})
        report = combined(completed)
        self.assertIn("a legacy interface card is fitted", report)
        self.assertIn("seven seven seven seven", report)

    def test_an_interface_that_does_not_exist_blocks_the_installation(self) -> None:
        root = build_bare_root(self.base)
        completed = run_preflight(
            environment={"ROOT_PREFIX": str(root), "APPLIANCE_INTERFACE": "wan-nine"}
        )
        report = combined(completed)
        self.assertEqual(completed.returncode, 1)
        self.assertIn("does not exist on this machine", report)
        # The names that are present are offered, so the operator can correct it.
        self.assertRegex(report, r"which are: [^;]*lo")

    def test_an_unnamed_interface_is_a_warning_and_not_a_refusal(self) -> None:
        if not has_room(self.base):
            self.skipTest("this machine has too little free space to host the fixture")

        root = build_satisfied_root(self.base)
        completed = run_preflight(environment={"ROOT_PREFIX": str(root)})
        report = combined(completed)
        self.assertIn("the target network interface has not been named", report)
        self.assertNotEqual(completed.returncode, 1)

    def test_every_item_is_reported_on_one_line(self) -> None:
        root = build_bare_root(self.base)
        report = combined(run_preflight(environment={"ROOT_PREFIX": str(root)}))
        items = [
            line for line in report.splitlines()
            if "satisfied: " in line or "worth knowing: " in line or "blocking: " in line
        ]
        self.assertGreaterEqual(len(items), 8, f"items were not all reported: {report}")

    def test_the_check_reports_on_every_inspection_it_promises(self) -> None:
        root = build_bare_root(self.base)
        report = combined(run_preflight(environment={"ROOT_PREFIX": str(root)}))
        for subject in (
            "interpreter", "kernel headers", "toolchain", "interface card",
            "address allocation", "/usr/src", "/opt", "/var", "clock",
            "network interface",
        ):
            with self.subTest(subject=subject):
                self.assertIn(subject, report)


class DriverSourceTests(unittest.TestCase):
    """Whether stage three can get the source it will need, said beforehand.

    On a kernel from six point ten onward the released driver archive does not
    compile, so the stage clones the development tree instead. At a site with
    no route off the premises -- which is most of the sites this appliance is
    built for -- that clone fails in the middle of an installation, after the
    base system is down and the packages are in, and the operator is left
    holding a half built machine and a network error.
    """

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory(prefix="myipbx-driver-")
        self.base = Path(self.directory.name)

    def tearDown(self) -> None:
        self.directory.cleanup()

    def _run(self, **environment: str) -> str:
        root = build_satisfied_root(self.base)
        settings = {"ROOT_PREFIX": str(root), "APPLIANCE_INTERFACE": "eth0"}
        settings.update(environment)
        return combined(run_preflight(environment=settings))

    def test_a_local_archive_settles_the_question(self) -> None:
        """If the source is already here, nothing has to be reached."""
        archive = self.base / "dahdi-linux-3.4.0.tar.gz"
        archive.write_bytes(b"not really an archive, but it is present")
        report = self._run(DRIVER_ARCHIVE=str(archive))
        self.assertIn("already on this machine", report)
        self.assertIn("needs to reach nothing", report)

    def test_the_development_tree_is_named_as_needing_the_network(self) -> None:
        report = self._run(DRIVER_SOURCE_MODE="git")
        self.assertIn("clone the driver development tree from the network", report)
        self.assertIn("DRIVER_ARCHIVE", report)

    def test_asking_for_the_released_archive_raises_nothing(self) -> None:
        report = self._run(DRIVER_SOURCE_MODE="archive")
        self.assertIn("can build from the released archive", report)

    def test_the_warning_names_the_way_out(self) -> None:
        """A warning that says only "this may fail" is not worth printing."""
        report = self._run(DRIVER_SOURCE_MODE="git")
        line = [
            item for item in report.splitlines()
            if "development tree" in item and "worth knowing" in item
        ]
        self.assertTrue(line, f"the finding was not reported: {report}")
        self.assertIn("set the variable named DRIVER_ARCHIVE", line[0])


class RemedyTests(unittest.TestCase):
    """A finding without a remedy leaves the technician exactly where he was."""

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory(prefix="myipbx-preflight-")
        self.base = Path(self.directory.name)

    def tearDown(self) -> None:
        self.directory.cleanup()

    def _assert_remedied(self, line: str) -> None:
        # What is wrong and what to do about it, separated: the script writes
        # the fault, a semicolon, then the action.
        self.assertIn(";", line, f"this line states no remedy at all: {line}")
        self.assertRegex(
            line, REMEDY_VERBS,
            f"this line names no action the technician can take: {line}",
        )

    def test_every_failing_line_emitted_names_a_remedy(self) -> None:
        root = build_bare_root(self.base)
        report = combined(
            run_preflight(environment={"ROOT_PREFIX": str(root), "APPLIANCE_INTERFACE": "wan-nine"})
        )
        failing = [
            line for line in report.splitlines()
            if "blocking: " in line or "worth knowing: " in line
        ]
        self.assertTrue(failing, f"no failing line was produced to inspect: {report}")
        for line in failing:
            with self.subTest(line=line[:72]):
                self._assert_remedied(line)

    def test_every_failing_line_in_the_source_names_a_remedy(self) -> None:
        """Including the ones this machine cannot be made to produce."""
        source = PREFLIGHT.read_text(encoding="utf-8")
        messages = re.findall(r'report_(?:blocking|warning) "([^"]+)"', source)
        self.assertGreaterEqual(
            len(messages), 12, "the failing messages could not be extracted from the source"
        )
        for message in messages:
            with self.subTest(message=message[:72]):
                self._assert_remedied(message)

    def test_the_kernel_header_remedy_names_the_command_to_match_against(self) -> None:
        root = build_bare_root(self.base)
        report = combined(run_preflight(environment={"ROOT_PREFIX": str(root)}))
        self.assertIn("uname -r", report)
        self.assertIn("linux-headers", report)
        self.assertIn("kernel-devel", report)

    def test_the_allocation_remedy_names_the_audit_rather_than_repeating_it(self) -> None:
        source = PREFLIGHT.read_text(encoding="utf-8")
        self.assertIn("scripts/verify-no-dhcp.sh to see which finding was made", source)


class RehearsalTests(unittest.TestCase):
    """Rehearsal changes nothing, so nothing can be left half finished."""

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory(prefix="myipbx-preflight-")
        self.base = Path(self.directory.name)

    def tearDown(self) -> None:
        self.directory.cleanup()

    def test_rehearsal_reports_the_blocking_finding_but_does_not_fail(self) -> None:
        root = build_bare_root(self.base)
        environment = {"ROOT_PREFIX": str(root), "APPLIANCE_INTERFACE": "wan-nine"}

        blocked = run_preflight(environment=environment)
        self.assertEqual(blocked.returncode, 1)

        rehearsed = run_preflight(["--rehearse"], environment=environment)
        self.assertEqual(rehearsed.returncode, 0)
        report = combined(rehearsed)
        self.assertIn("blocking: kernel headers", report)
        self.assertIn("a real installation would stop", report)

    def test_the_rehearsal_flag_is_accepted_in_the_forms_the_other_scripts_accept(self) -> None:
        for flag in ("--rehearse", "--rehearsal", "--dry-run"):
            with self.subTest(flag=flag):
                completed = run_preflight([flag], environment={"ROOT_PREFIX": str(self.base)})
                self.assertEqual(completed.returncode, 0)
                self.assertIn("rehearsal mode is active", combined(completed))

    def test_the_environment_variable_arms_rehearsal_as_it_does_elsewhere(self) -> None:
        completed = run_preflight(
            environment={"ROOT_PREFIX": str(self.base), "REHEARSAL": "yes"}
        )
        self.assertEqual(completed.returncode, 0)


class UnprivilegedTests(unittest.TestCase):
    """A technician runs this before committing to anything, as himself."""

    def test_the_check_never_demands_administrative_privilege(self) -> None:
        source = PREFLIGHT.read_text(encoding="utf-8")
        self.assertNotIn("require_root", source)

    def test_the_check_mutates_nothing(self) -> None:
        source = PREFLIGHT.read_text(encoding="utf-8")
        for mutation in ("run_command", "mkdir ", "install -m", "systemctl start",
                         "systemctl enable", "mark_stage_completed"):
            with self.subTest(mutation=mutation):
                self.assertNotIn(mutation, source)

    def test_the_check_runs_to_completion_on_this_machine(self) -> None:
        completed = run_preflight()
        self.assertIn(completed.returncode, (0, 1), combined(completed))
        self.assertIn("the preflight check reports", combined(completed))


if __name__ == "__main__":
    unittest.main()
