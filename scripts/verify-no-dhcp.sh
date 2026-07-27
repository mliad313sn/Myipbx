#!/usr/bin/env bash
# Constraint One -- the install time arm of the address allocation exclusion.
#
# The appliance must never assign Internet Protocol addresses.  This script is
# the audit the installer runs before it will provision anything, and the audit
# an operator can run at any time afterwards to prove the property still holds.
#
# It inspects four independent sources, so an allocation service that evades one
# is still caught by another:
#
#   installed packages, service units, running processes, and listening sockets,
#   plus any allocation server configuration file left behind on the filesystem.
#
# Exit status: zero when the machine assigns no addresses, one when a blocking
# finding was made, two when the audit itself could not be completed.

set -o errexit
set -o nounset
set -o pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/lib/common.sh
source "${SCRIPT_DIR}/lib/common.sh"

QUIET="no"
ROOT_PREFIX="${ROOT_PREFIX:-}"

CRITICAL_COUNT=0
WARNING_COUNT=0

ALLOCATION_BINARIES=(dhcpd dhcpd6 udhcpd dhcrelay kea-dhcp4 kea-dhcp6 dhcpy6d)
ALLOCATION_PACKAGES=(isc-dhcp-server isc-dhcp-server6 dhcp-server dhcp kea-dhcp4-server
                     kea-dhcp6-server udhcpd busybox-udhcpd)
ALLOCATION_UNITS=(isc-dhcp-server isc-dhcp-server6 dhcpd dhcpd6 udhcpd
                  kea-dhcp4-server kea-dhcp6-server)
ALLOCATION_CONFIGS=(/etc/dhcp/dhcpd.conf /etc/dhcp/dhcpd6.conf /etc/dhcpd.conf
                    /etc/dhcp3/dhcpd.conf /etc/kea/kea-dhcp4.conf
                    /etc/kea/kea-dhcp6.conf /etc/udhcpd.conf)

usage() {
    cat <<'USAGE'
usage: verify-no-dhcp.sh [--quiet] [--help]

  --quiet   report nothing on success; used by the staging scripts
  --help    show this message

exit status zero means this machine assigns no addresses.
USAGE
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --quiet) QUIET="yes"; shift ;;
        --help|-h) usage; exit 0 ;;
        *) printf 'the argument %s is not recognised\n' "$1" >&2; usage; exit 2 ;;
    esac
done

report_critical() {
    CRITICAL_COUNT=$(( CRITICAL_COUNT + 1 ))
    log_error "critical finding: $*"
}

report_warning() {
    WARNING_COUNT=$(( WARNING_COUNT + 1 ))
    if [[ "${QUIET}" != "yes" ]]; then
        log_warn "advisory finding: $*"
    fi
}

announce() {
    if [[ "${QUIET}" != "yes" ]]; then
        log_info "$@"
    fi
}

# -- detection one: running processes ---------------------------------------

audit_processes() {
    announce "inspecting running processes for an address allocation service"
    local binary
    for binary in "${ALLOCATION_BINARIES[@]}"; do
        if pgrep -x "${binary}" >/dev/null 2>&1; then
            report_critical "the address allocation service named ${binary} is running"
        fi
    done

    # A name resolution daemon is only an allocation service when it is started
    # with an allocation range, so the argument list decides the severity.
    if pgrep -x dnsmasq >/dev/null 2>&1; then
        if pgrep -af dnsmasq 2>/dev/null | grep -q -- '--dhcp-range'; then
            report_critical "the process named dnsmasq is running with an address allocation range"
        else
            report_warning "the process named dnsmasq is running; it allocates no addresses at present but is capable of doing so"
        fi
    fi
}

# -- detection two: listening sockets ---------------------------------------

audit_sockets() {
    announce "inspecting listening sockets for the address allocation service ports"

    local table
    for table in /proc/net/udp /proc/net/udp6; do
        [[ -r "${ROOT_PREFIX}${table}" ]] || continue
        # The second column holds the local address and port in hexadecimal.
        # Port number sixty seven and port number five hundred forty seven are
        # the allocation server ports.
        if awk 'NR > 1 { split($2, parts, ":"); port = strtonum("0x" parts[2]);
                         if (port == 67 || port == 547) { found = 1 } }
                END { exit(found ? 0 : 1) }' "${ROOT_PREFIX}${table}" 2>/dev/null; then
            report_critical "a socket is bound to an address allocation server port in ${table}"
        fi
    done
}

# -- detection three: service units -----------------------------------------

audit_service_units() {
    announce "inspecting service units for an address allocation service"

    local unit
    for unit in "${ALLOCATION_UNITS[@]}"; do
        local found="no"
        local directory
        for directory in /etc/systemd/system /lib/systemd/system /usr/lib/systemd/system; do
            if [[ -e "${ROOT_PREFIX}${directory}/${unit}.service" ]]; then
                found="yes"
            fi
        done
        [[ "${found}" == "yes" ]] || continue

        if have_command systemctl && systemctl is-enabled "${unit}" >/dev/null 2>&1; then
            report_critical "the service unit named ${unit} is installed and enabled"
        else
            report_warning "the service unit named ${unit} is installed but is not enabled; remove it to keep the exclusion unambiguous"
        fi
    done
}

# -- detection four: installed packages -------------------------------------

audit_packages() {
    announce "inspecting installed packages for an address allocation service"

    local manager
    manager="$(detect_package_manager)"
    local package

    case "${manager}" in
        apt)
            have_command dpkg-query || return 0
            for package in "${ALLOCATION_PACKAGES[@]}"; do
                if dpkg-query -W -f='${Status}' "${package}" 2>/dev/null | grep -q "install ok installed"; then
                    report_warning "the package named ${package} is installed; the appliance never enables it, but removing it is advised"
                fi
            done
            ;;
        dnf|yum|zypper)
            have_command rpm || return 0
            for package in "${ALLOCATION_PACKAGES[@]}"; do
                if rpm -q "${package}" >/dev/null 2>&1; then
                    report_warning "the package named ${package} is installed; the appliance never enables it, but removing it is advised"
                fi
            done
            ;;
        *)
            announce "the package manager was not recognised; the package inspection was skipped"
            ;;
    esac
}

# -- detection five: leftover configuration ---------------------------------

audit_configuration_files() {
    announce "inspecting the filesystem for address allocation configuration"

    local path
    for path in "${ALLOCATION_CONFIGS[@]}"; do
        [[ -f "${ROOT_PREFIX}${path}" ]] || continue
        if grep -Eq '^[[:space:]]*(range|dhcp-range|start|end)[[:space:]]' "${ROOT_PREFIX}${path}" 2>/dev/null; then
            report_critical "the file at ${path} declares an address allocation range"
        else
            report_warning "the file at ${path} exists but declares no allocation range"
        fi
    done
}

# -- main --------------------------------------------------------------------

main() {
    if [[ "${QUIET}" != "yes" ]]; then
        banner "address allocation exclusion audit"
    fi

    audit_processes
    audit_sockets
    audit_service_units
    audit_packages
    audit_configuration_files

    if (( CRITICAL_COUNT > 0 )); then
        log_error "the audit made $(spell_integer "${CRITICAL_COUNT}") blocking finding or findings; this machine allocates addresses and may not host the appliance"
        return 1
    fi

    if [[ "${QUIET}" != "yes" ]]; then
        if (( WARNING_COUNT > 0 )); then
            log_info "the audit made no blocking finding and $(spell_integer "${WARNING_COUNT}") advisory finding or findings"
        else
            log_info "the audit made no finding at all; this machine assigns no addresses"
        fi
    fi
    return 0
}

main "$@"
