"""Constraint One — structural exclusion of address allocation service.

The appliance must never assign Internet Protocol addresses.  This module is
the run time arm of that exclusion.  It inspects the machine for any Dynamic
Host Configuration Protocol server and reports what it finds; the control
plane treats a critical finding as a fatal startup condition and raises a
dashboard alarm if one appears while the appliance is running.

Four independent detections are performed, so that an allocation server that
evades one is still caught by another:

* running processes, matched by executable name and by allocation arguments,
* listening sockets, matched by the allocation service port numbers,
* installed service units, matched by name and by enablement,
* configuration files, matched by path and by the presence of an allocation
  range directive.

Every filesystem read is rooted at an injectable path so that the audit can be
exercised against fixture trees by the test suite.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Iterable, Sequence

from .logging_setup import get_logger

__all__ = [
    "Finding",
    "AddressAllocationAudit",
    "AddressAllocationDetected",
    "ALLOCATION_SERVER_BINARIES",
    "ALLOCATION_SERVICE_PORTS",
]

_LOG = get_logger("netaudit")

SEVERITY_CRITICAL = "critical"
SEVERITY_WARNING = "warning"

#: Executable names that are unambiguously address allocation servers.
ALLOCATION_SERVER_BINARIES = frozenset(
    {
        "dhcpd",
        "dhcpd6",
        "udhcpd",
        "dhcrelay",
        "kea-dhcp4",
        "kea-dhcp6",
        "kea-dhcp-ddns",
        "isc-dhcp-server",
        "dhcp-server",
        "dhcpy6d",
        "atftpd-dhcp",
    }
)

#: Executables that can allocate addresses but usually do not.  These are
#: reported as critical only when an allocation range argument is present.
_CONDITIONAL_BINARIES = frozenset({"dnsmasq", "systemd-networkd", "NetworkManager", "connmand"})

_ALLOCATION_ARGUMENT = re.compile(
    r"--?(dhcp-range|dhcp-host|dhcp-option|dhcp-boot|enable-dhcp)\b"
)

#: Allocation server ports.  The client ports are deliberately absent: the
#: appliance is expected to be a client of the network's addressing.
ALLOCATION_SERVICE_PORTS = {
    67: "the address allocation server port",
    547: "the version six address allocation server port",
}

_SERVICE_UNIT_NAMES = frozenset(
    {
        "isc-dhcp-server",
        "isc-dhcp-server6",
        "dhcpd",
        "dhcpd6",
        "dhcp4",
        "dhcp6",
        "udhcpd",
        "kea-dhcp4-server",
        "kea-dhcp6-server",
        "dnsmasq",
    }
)

_UNIT_DIRECTORIES = (
    "etc/systemd/system",
    "lib/systemd/system",
    "usr/lib/systemd/system",
)

_ENABLEMENT_DIRECTORIES = (
    "etc/systemd/system/multi-user.target.wants",
    "etc/systemd/system/network.target.wants",
    "etc/rc2.d",
    "etc/rc3.d",
    "etc/rc5.d",
)

_CONFIGURATION_PATHS = (
    "etc/dhcp/dhcpd.conf",
    "etc/dhcp/dhcpd6.conf",
    "etc/dhcpd.conf",
    "etc/dhcp3/dhcpd.conf",
    "etc/kea/kea-dhcp4.conf",
    "etc/kea/kea-dhcp6.conf",
    "etc/udhcpd.conf",
)

_RANGE_DIRECTIVE = re.compile(
    r"^\s*(range\b|dhcp-range\b|\"pools\"|start\s|end\s)", re.IGNORECASE | re.MULTILINE
)


class AddressAllocationDetected(RuntimeError):
    """Raised when the appliance refuses to run beside an allocation server."""

    def __init__(self, findings: Sequence["Finding"]) -> None:
        self.findings = tuple(findings)
        summary = "; ".join(f"{item.source}: {item.subject}" for item in self.findings)
        super().__init__(
            "an address allocation server is present on this machine, which the "
            f"appliance specification forbids -- {summary}"
        )


@dataclass(frozen=True)
class Finding:
    """One detection produced by the audit."""

    source: str
    subject: str
    detail: str
    severity: str = SEVERITY_CRITICAL

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class AddressAllocationAudit:
    """Inspect a machine for any address allocation server."""

    def __init__(self, root: str | Path = "/") -> None:
        self.root = Path(root)

    # -- public surface ----------------------------------------------------

    def scan(self) -> list[Finding]:
        """Run every detection and return the combined findings."""
        findings: list[Finding] = []
        for detection in (
            self._scan_processes,
            self._scan_listening_sockets,
            self._scan_service_units,
            self._scan_configuration_files,
        ):
            try:
                findings.extend(detection())
            except OSError as error:
                # An unreadable source is reported rather than swallowed, but
                # it must not abort the remaining detections.
                findings.append(
                    Finding(
                        source=detection.__name__.lstrip("_"),
                        subject="the audit source could not be read",
                        detail=str(error),
                        severity=SEVERITY_WARNING,
                    )
                )
        return findings

    def critical_findings(self) -> list[Finding]:
        return [item for item in self.scan() if item.severity == SEVERITY_CRITICAL]

    def enforce(self, fatal: bool = True) -> list[Finding]:
        """Scan, log, and optionally refuse to continue.

        Returns every finding so that warnings can be surfaced on the
        dashboard even when the audit does not block startup.
        """
        findings = self.scan()
        critical = [item for item in findings if item.severity == SEVERITY_CRITICAL]

        for item in findings:
            record = _LOG.error if item.severity == SEVERITY_CRITICAL else _LOG.warning
            record(
                "address allocation audit finding from %s -- %s (%s)",
                item.source,
                item.subject,
                item.detail,
            )

        if critical and fatal:
            raise AddressAllocationDetected(critical)
        if not findings:
            _LOG.info(
                "address allocation audit passed with no findings; this appliance "
                "assigns no addresses"
            )
        return findings

    # -- detections --------------------------------------------------------

    def _scan_processes(self) -> Iterable[Finding]:
        procedure_root = self.root / "proc"
        if not procedure_root.is_dir():
            return []

        findings: list[Finding] = []
        for entry in sorted(procedure_root.iterdir()):
            if not entry.name.isdigit():
                continue
            command_line = entry / "cmdline"
            try:
                raw = command_line.read_bytes()
            except OSError:
                # Processes disappear mid scan; that is normal, not a finding.
                continue
            if not raw:
                continue

            arguments = [part for part in raw.decode("utf-8", "replace").split("\0") if part]
            if not arguments:
                continue
            executable = Path(arguments[0]).name

            if executable in ALLOCATION_SERVER_BINARIES:
                findings.append(
                    Finding(
                        source="running processes",
                        subject=f"the process named {executable}",
                        detail=(
                            "an address allocation server is running as process "
                            f"number {entry.name}"
                        ),
                    )
                )
            elif executable in _CONDITIONAL_BINARIES:
                joined = " ".join(arguments)
                if _ALLOCATION_ARGUMENT.search(joined):
                    findings.append(
                        Finding(
                            source="running processes",
                            subject=f"the process named {executable}",
                            detail=(
                                "the process is running with address allocation "
                                "arguments, which makes it an allocation server"
                            ),
                        )
                    )
        return findings

    def _scan_listening_sockets(self) -> Iterable[Finding]:
        findings: list[Finding] = []
        for relative in ("proc/net/udp", "proc/net/udp6"):
            table = self.root / relative
            if not table.is_file():
                continue
            try:
                lines = table.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue

            for line in lines[1:]:
                columns = line.split()
                if len(columns) < 2:
                    continue
                local = columns[1]
                _, _, port_hex = local.partition(":")
                try:
                    port = int(port_hex, 16)
                except ValueError:
                    continue
                if port in ALLOCATION_SERVICE_PORTS:
                    findings.append(
                        Finding(
                            source="listening sockets",
                            subject=f"a socket bound to port number {port}",
                            detail=(
                                f"{ALLOCATION_SERVICE_PORTS[port]} is bound, which "
                                "indicates an active allocation server"
                            ),
                        )
                    )
        return findings

    def _scan_service_units(self) -> Iterable[Finding]:
        findings: list[Finding] = []
        seen: set[str] = set()

        for relative in _UNIT_DIRECTORIES:
            directory = self.root / relative
            if not directory.is_dir():
                continue
            for unit in sorted(directory.iterdir()):
                stem = unit.name.split(".service")[0]
                if stem not in _SERVICE_UNIT_NAMES or stem in seen:
                    continue
                seen.add(stem)
                findings.append(
                    Finding(
                        source="service units",
                        subject=f"the service unit named {stem}",
                        detail=(
                            "an address allocation service unit is installed on this "
                            "machine and must be removed before the appliance may run"
                        ),
                        severity=SEVERITY_CRITICAL
                        if self._unit_is_enabled(stem)
                        else SEVERITY_WARNING,
                    )
                )
        return findings

    def _unit_is_enabled(self, stem: str) -> bool:
        for relative in _ENABLEMENT_DIRECTORIES:
            directory = self.root / relative
            if not directory.is_dir():
                continue
            for link in directory.iterdir():
                if stem in link.name:
                    return True
        return False

    def _scan_configuration_files(self) -> Iterable[Finding]:
        findings: list[Finding] = []
        for relative in _CONFIGURATION_PATHS:
            path = self.root / relative
            if not path.is_file():
                continue
            try:
                content = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue

            # A file that exists but declares no allocation range cannot hand
            # out an address, so it is reported as a warning to be cleaned up
            # rather than as a blocking fault.
            severity = (
                SEVERITY_CRITICAL if _RANGE_DIRECTIVE.search(content) else SEVERITY_WARNING
            )
            findings.append(
                Finding(
                    source="configuration files",
                    subject=f"the file at {relative}",
                    detail=(
                        "an address allocation server configuration file is present"
                        + (
                            " and declares an allocation range"
                            if severity == SEVERITY_CRITICAL
                            else " but declares no allocation range"
                        )
                    ),
                    severity=severity,
                )
            )
        return findings
