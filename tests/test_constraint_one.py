"""Constraint One — total exclusion of address allocation service.

The product specification forbids any Dynamic Host Configuration Protocol
server allocation routine anywhere in the deployment manifests or the network
architecture, and forbids the system from ever assigning an Internet Protocol
address.

These tests verify the exclusion at all four enforcement points: the run time
audit, the repository source tree, the deployment manifests, and the appliance's
refusal to start beside an allocation service.
"""

from __future__ import annotations

import re
import subprocess
import tempfile
import unittest

from support import quantities_left_as_digits
from pathlib import Path

from support import REPOSITORY_ROOT, ApplianceHarness, build_fixture_root

from appliance.netaudit import (
    AddressAllocationAudit,
    AddressAllocationDetected,
    SEVERITY_CRITICAL,
)
from appliance.server import Appliance


class RunTimeAuditTests(unittest.TestCase):
    """The run time arm: the audit that runs at startup and every health sweep."""

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory(prefix="crossbar-audit-")
        self.base = Path(self.directory.name)

    def tearDown(self) -> None:
        self.directory.cleanup()

    def test_a_clean_machine_produces_no_finding(self) -> None:
        root = build_fixture_root(self.base, allocation_service=False)
        findings = AddressAllocationAudit(root).scan()
        self.assertEqual(
            findings, [], f"a clean machine produced findings: {findings}"
        )

    def test_a_running_allocation_process_is_detected(self) -> None:
        root = build_fixture_root(self.base, allocation_service=True)
        findings = AddressAllocationAudit(root).scan()
        sources = {finding.source for finding in findings}
        self.assertIn("running processes", sources)

    def test_a_bound_allocation_port_is_detected(self) -> None:
        root = build_fixture_root(self.base, allocation_service=True)
        findings = AddressAllocationAudit(root).scan()
        socket_findings = [f for f in findings if f.source == "listening sockets"]
        self.assertTrue(socket_findings, "the bound allocation port was not detected")
        self.assertEqual(socket_findings[0].severity, SEVERITY_CRITICAL)

    def test_an_enabled_allocation_service_unit_is_detected(self) -> None:
        root = build_fixture_root(self.base, allocation_service=True)
        findings = AddressAllocationAudit(root).scan()
        unit_findings = [f for f in findings if f.source == "service units"]
        self.assertTrue(unit_findings, "the allocation service unit was not detected")
        self.assertEqual(unit_findings[0].severity, SEVERITY_CRITICAL)

    def test_an_allocation_range_in_a_configuration_file_is_detected(self) -> None:
        root = build_fixture_root(self.base, allocation_service=True)
        findings = AddressAllocationAudit(root).scan()
        file_findings = [f for f in findings if f.source == "configuration files"]
        self.assertTrue(file_findings, "the allocation configuration was not detected")
        self.assertEqual(file_findings[0].severity, SEVERITY_CRITICAL)

    def test_all_four_detections_fire_independently(self) -> None:
        """No single evasion can defeat the exclusion."""
        root = build_fixture_root(self.base, allocation_service=True)
        findings = AddressAllocationAudit(root).scan()
        sources = {finding.source for finding in findings}
        self.assertEqual(
            sources,
            {"running processes", "listening sockets", "service units", "configuration files"},
        )

    def test_the_audit_refuses_to_continue_on_a_critical_finding(self) -> None:
        root = build_fixture_root(self.base, allocation_service=True)
        with self.assertRaises(AddressAllocationDetected):
            AddressAllocationAudit(root).enforce(fatal=True)

    def test_the_audit_can_report_without_blocking_when_asked(self) -> None:
        root = build_fixture_root(self.base, allocation_service=True)
        findings = AddressAllocationAudit(root).enforce(fatal=False)
        self.assertTrue(findings)

    def test_an_unreadable_source_degrades_to_a_warning_not_a_crash(self) -> None:
        # A root that does not exist at all makes every source unreadable.
        findings = AddressAllocationAudit(self.base / "a-path-that-is-absent").scan()
        self.assertEqual(findings, [])


class StartupRefusalTests(unittest.IsolatedAsyncioTestCase):
    """The appliance must refuse to serve on a machine that allocates addresses."""

    async def test_the_appliance_refuses_to_start_beside_an_allocation_service(self) -> None:
        with tempfile.TemporaryDirectory(prefix="crossbar-refusal-") as name:
            root = build_fixture_root(Path(name), allocation_service=True)
            harness = ApplianceHarness(fail_on_address_allocation_server=True)

            # Build the appliance, then point its audit at the offending machine.
            appliance = Appliance(harness.config)
            appliance.audit = AddressAllocationAudit(root)

            with self.assertRaises(AddressAllocationDetected):
                await appliance.start()

            # Nothing may be left listening after a refusal.
            self.assertEqual(appliance.http.sockets_bound, 0)
            await appliance.stop()
            harness.directory.cleanup()

    async def test_the_appliance_serves_normally_on_a_clean_machine(self) -> None:
        with tempfile.TemporaryDirectory(prefix="crossbar-clean-") as name:
            root = build_fixture_root(Path(name), allocation_service=False)
            harness = ApplianceHarness(fail_on_address_allocation_server=True)

            appliance = Appliance(harness.config)
            appliance.audit = AddressAllocationAudit(root)

            from appliance.security import CredentialStore, PasswordHasher
            from support import TEST_ITERATIONS, TEST_PASSWORD, TEST_USERNAME

            CredentialStore(
                harness.config.credentials_path, PasswordHasher(TEST_ITERATIONS)
            ).save(TEST_USERNAME, TEST_PASSWORD)

            await appliance.start()
            try:
                self.assertGreater(appliance.http.bound_port, 0)
                self.assertEqual(appliance.audit_findings, [])
            finally:
                await appliance.stop()
                harness.directory.cleanup()


class RepositoryExclusionTests(unittest.TestCase):
    """The repository time arm: no allocation routine anywhere in the tree."""

    #: Files whose entire purpose is to name and detect allocation services.
    EXCLUSION_MACHINERY = {
        "appliance/netaudit.py",
        "scripts/verify-no-dhcp.sh",
        "scripts/lib/common.sh",
        "tests/test_constraint_one.py",
        "tests/support.py",
        "docs/quality-assurance-report.md",
        "docs/product-vision.md",
        "docs/system-architecture.md",
        "docs/operations-runbook.md",
        "docs/market-benchmark.md",
        "README.md",
    }

    #: Directives that would configure a machine to hand out addresses.  Their
    #: presence anywhere outside the exclusion machinery is a specification
    #: breach, not a style problem.
    ALLOCATION_DIRECTIVES = (
        re.compile(r"^\s*range\s+\d", re.MULTILINE),
        re.compile(r"dhcp-range"),
        re.compile(r"default-lease-time"),
        re.compile(r"max-lease-time"),
        # Anchored to a line of its own: this is the allocation server
        # directive, not the ordinary English word used in prose.
        re.compile(r"^\s*authoritative\s*;\s*$", re.MULTILINE),
        re.compile(r"subnet\s+\S+\s+netmask\s+\S+\s*\{"),
    )

    #: Commands that would install or enable an allocation service.
    INSTALLATION_PATTERNS = (
        re.compile(r"(apt-get|apt|dnf|yum|zypper)\s+(-\S+\s+)*install[^\n]*\bisc-dhcp-server\b"),
        re.compile(r"(apt-get|apt|dnf|yum|zypper)\s+(-\S+\s+)*install[^\n]*\bdhcp-server\b"),
        re.compile(r"(apt-get|apt|dnf|yum|zypper)\s+(-\S+\s+)*install[^\n]*\bkea-dhcp"),
        re.compile(r"systemctl\s+enable\s+\S*dhcp"),
        re.compile(r"systemctl\s+start\s+\S*dhcp"),
    )

    def _source_files(self) -> list[Path]:
        collected: list[Path] = []
        for pattern in ("*.py", "*.sh", "*.js", "*.html", "*.css", "*.json", "*.yaml", "*.conf", "*.service", "*.md"):
            for path in REPOSITORY_ROOT.rglob(pattern):
                if ".git" in path.parts or "__pycache__" in path.parts:
                    continue
                collected.append(path)
        return collected

    def test_no_allocation_directive_appears_anywhere_in_the_tree(self) -> None:
        breaches: list[str] = []
        for path in self._source_files():
            relative = path.relative_to(REPOSITORY_ROOT).as_posix()
            if relative in self.EXCLUSION_MACHINERY:
                continue
            try:
                content = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            for directive in self.ALLOCATION_DIRECTIVES:
                if directive.search(content):
                    breaches.append(f"{relative} matches {directive.pattern}")

        self.assertEqual(
            breaches, [],
            "an address allocation directive appears in the source tree: "
            + "; ".join(breaches),
        )

    def test_no_script_installs_or_enables_an_allocation_service(self) -> None:
        breaches: list[str] = []
        for path in REPOSITORY_ROOT.rglob("*.sh"):
            if ".git" in path.parts:
                continue
            content = path.read_text(encoding="utf-8")
            for pattern in self.INSTALLATION_PATTERNS:
                if pattern.search(content):
                    breaches.append(
                        f"{path.relative_to(REPOSITORY_ROOT)} matches {pattern.pattern}"
                    )
        self.assertEqual(breaches, [], "; ".join(breaches))

    def test_the_network_templates_declare_static_addressing_only(self) -> None:
        templates = list((REPOSITORY_ROOT / "config/network").glob("*"))
        self.assertTrue(templates, "no network template was found to inspect")

        for template in templates:
            content = template.read_text(encoding="utf-8")
            with self.subTest(template=template.name):
                # An address request directive would make the appliance a
                # client of allocation; an allocation range would make it a
                # server.  Neither may appear.
                self.assertNotRegex(content, r"dhcp4:\s*true")
                self.assertNotRegex(content, r"dhcp6:\s*true")
                self.assertNotRegex(content, r"\bdhcp-range\b")
                self.assertIn("addresses:", content)

    def test_the_service_unit_does_not_want_or_require_an_allocation_service(self) -> None:
        unit = (REPOSITORY_ROOT / "config/systemd/crossbar.service").read_text(encoding="utf-8")
        for directive in ("Requires=", "Wants=", "After=", "BindsTo="):
            for line in unit.splitlines():
                if line.startswith(directive):
                    self.assertNotIn(
                        "dhcp", line.lower(),
                        f"the service unit references an allocation service: {line}",
                    )

    def test_the_product_declares_that_it_assigns_no_addresses(self) -> None:
        import appliance

        self.assertFalse(appliance.ASSIGNS_ADDRESSES)


class AuditScriptTests(unittest.TestCase):
    """The install time arm: the standalone audit script an operator can run."""

    def test_the_audit_script_reports_the_state_of_this_machine(self) -> None:
        completed = subprocess.run(
            ["bash", str(REPOSITORY_ROOT / "scripts/verify-no-dhcp.sh")],
            capture_output=True,
            text=True,
            timeout=120,
        )
        # Either outcome is legitimate depending on the host, but the script
        # must run to completion and say which it found.
        self.assertIn(completed.returncode, (0, 1), completed.stderr)
        combined = completed.stdout + completed.stderr
        self.assertTrue(combined.strip(), "the audit script produced no output")

    def test_the_audit_script_output_contains_no_digit_character(self) -> None:
        completed = subprocess.run(
            ["bash", str(REPOSITORY_ROOT / "scripts/verify-no-dhcp.sh")],
            capture_output=True,
            text=True,
            timeout=120,
        )
        combined = completed.stdout + completed.stderr
        # Quantities are spelled; the timestamp each line carries is an
        # identifier and keeps its digits on purpose.
        offenders = quantities_left_as_digits(combined)
        self.assertEqual(
            offenders,
            [],
            f"the audit script emitted a quantity as digits: {offenders!r}",
        )

    def test_the_installer_refuses_an_out_of_range_stage_selection(self) -> None:
        completed = subprocess.run(
            ["bash", str(REPOSITORY_ROOT / "scripts/install-appliance.sh"),
             "--from-stage", "9"],
            capture_output=True,
            text=True,
            timeout=120,
        )
        self.assertNotEqual(completed.returncode, 0)


if __name__ == "__main__":
    unittest.main()


class ImageBuildTests(unittest.TestCase):
    """The bootable image is held to the same constraints as the appliance."""

    ISO_ROOT = REPOSITORY_ROOT / "iso"

    def _scripts(self) -> list[Path]:
        return sorted(self.ISO_ROOT.rglob("*.sh"))

    def test_the_image_build_exists_and_is_complete(self) -> None:
        self.assertTrue(self.ISO_ROOT.is_dir(), "there is no image build in the repository")
        for expected in (
            "build-iso.sh", "boot-test.sh", "lib/iso-common.sh",
            "stages/one-bootstrap.sh", "stages/two-payload.sh",
            "stages/three-configure.sh", "stages/four-compress.sh",
            "stages/five-master.sh",
        ):
            with self.subTest(part=expected):
                self.assertTrue(
                    (self.ISO_ROOT / expected).is_file(),
                    f"the image build is missing {expected}",
                )

    def test_every_image_script_is_well_formed(self) -> None:
        import subprocess

        for script in self._scripts():
            with self.subTest(script=script.name):
                completed = subprocess.run(
                    ["bash", "-n", str(script)], capture_output=True, text=True, timeout=60
                )
                self.assertEqual(
                    completed.returncode, 0,
                    f"{script.name} has a syntax fault: {completed.stderr}",
                )

    def test_no_image_stage_installs_an_address_allocation_service(self) -> None:
        """The image may name allocation services only to exclude them."""
        installing = re.compile(
            r"install_in_chroot[^\n]*\b(isc-dhcp-server|dnsmasq|udhcpd|kea-dhcp)",
        )
        for script in self._scripts():
            with self.subTest(script=script.name):
                self.assertIsNone(
                    installing.search(script.read_text(encoding="utf-8")),
                    f"{script.name} installs an address allocation service into the image",
                )

    def test_the_image_build_audits_for_an_allocation_service(self) -> None:
        bootstrap = (self.ISO_ROOT / "stages/one-bootstrap.sh").read_text(encoding="utf-8")
        configure = (self.ISO_ROOT / "stages/three-configure.sh").read_text(encoding="utf-8")

        self.assertIn("verify_no_allocation_service", bootstrap)
        self.assertIn("audit_image", configure)
        # The audit must be able to fail the build, not merely warn.
        self.assertIn("the base image carries an address allocation service", bootstrap)

    def test_the_image_masks_every_allocation_service_unit(self) -> None:
        configure = (self.ISO_ROOT / "stages/three-configure.sh").read_text(encoding="utf-8")
        for unit in ("isc-dhcp-server", "udhcpd", "kea-dhcp4-server"):
            with self.subTest(unit=unit):
                self.assertIn(unit, configure)
        self.assertIn("systemctl mask", configure)

    def test_the_image_network_configuration_requests_no_address(self) -> None:
        configure = (self.ISO_ROOT / "stages/three-configure.sh").read_text(encoding="utf-8")
        self.assertIn("DHCP=no", configure)
        self.assertIn("Address=${APPLIANCE_DEFAULT_ADDRESS}", configure)
        self.assertNotIn("DHCP=yes", configure)
        self.assertNotIn("dhcp4: true", configure)

    def test_the_boot_test_observes_the_exclusion_rather_than_asserting_it(self) -> None:
        boot_test = (self.ISO_ROOT / "boot-test.sh").read_text(encoding="utf-8")
        self.assertIn("mentions address allocation", boot_test)
        self.assertIn("dhcpd", boot_test)

    def test_the_image_ships_no_credential(self) -> None:
        configure = (self.ISO_ROOT / "stages/three-configure.sh").read_text(encoding="utf-8")
        self.assertIn("a credential was baked into the image", configure)
        self.assertIn("credentials.json", configure)

    def test_the_image_carries_both_boot_paths(self) -> None:
        master = (self.ISO_ROOT / "stages/five-master.sh").read_text(encoding="utf-8")
        self.assertIn("isolinux.bin", master)
        self.assertIn("bootx64.efi", master)
        self.assertIn("isohybrid-mbr", master)

    def test_the_image_build_verifies_its_own_payload(self) -> None:
        payload = (self.ISO_ROOT / "stages/two-payload.sh").read_text(encoding="utf-8")
        # The strongest check the build makes: the control plane must import
        # using the interpreter inside the image.
        self.assertIn("import appliance.server", payload)
