#!/usr/bin/env bash
# The preflight check -- everything the installer needs to be true before it
# begins to change the machine.
#
# The installer runs this first, and a technician can run it on its own long
# before committing to an installation.  Nothing here mutates anything, so it
# needs no privilege; it only looks.
#
# The check exists because a half finished installation is far more expensive
# to unpick than a refused one.  Missing kernel headers are discovered at the
# driver compilation in stage three, by which point the toolchain, the service
# account and the directory tree are already on the machine.  Everything that
# stage three, four or five depends on is therefore established here, at the
# point where the answer is still "not yet" rather than "half way".
#
# Three outcomes are distinguished, because they call for different responses:
#
#   blocking   the installation cannot proceed and this script exits non zero
#   warning    the installation can proceed, but the operator should know
#   pass       the condition is satisfied
#
# The absence of an interface card is the archetypal warning: an appliance is
# frequently prepared in an office weeks before the card is fitted in the comms
# room, and refusing to install on that ground would be wrong.

set -o errexit
set -o nounset
set -o pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/lib/common.sh
source "${SCRIPT_DIR}/lib/common.sh"

# Every filesystem inspection is taken relative to this prefix.  It is empty on
# a real machine, and the address allocation audit already offers the same
# lever, so a fixture root exercises both scripts the same way.
ROOT_PREFIX="${ROOT_PREFIX:-}"

# Named by the operator or by the installer.  Stage two deliberately refuses to
# guess it, so this check refuses to guess it either.
APPLIANCE_INTERFACE="${APPLIANCE_INTERFACE:-}"

PASS_COUNT=0
WARNING_COUNT=0
BLOCKING_COUNT=0

MINIMUM_INTERPRETER_MAJOR=3
MINIMUM_INTERPRETER_MINOR=8

# Free space in mebibytes.  The source tree figure is the one that matters: the
# driver tree and the telephony engine are both unpacked and compiled in place,
# and a build that runs out of room part way through leaves an object tree that
# has to be removed by hand before the stage can be retried.
REQUIRED_SPACE_SOURCE=2048
REQUIRED_SPACE_PREFIX=512
REQUIRED_SPACE_STATE=1024

# The plausible window for the system clock, as seconds since the epoch.  The
# floor is the first of January, twenty twenty-five, which is earlier than any
# machine this appliance ships to could legitimately read; the ceiling is the
# first of January, twenty fifty.  A machine whose battery has failed reads
# somewhere in the nineteen seventies or the year two thousand, and a machine
# with a corrupted clock reads far beyond the ceiling.  Both are caught.
CLOCK_FLOOR=1735689600
CLOCK_CEILING=2524608000

# The peripheral bus vendor identifiers assigned to Digium and to the Tiger Jet
# part the earliest Wildcard analogue cards were built around.
DIGIUM_VENDOR="d161"
TIGERJET_VENDOR="e159"

usage() {
    cat <<'USAGE'
usage: preflight-check.sh [--rehearse] [--help]

  --rehearse  report every finding, but exit zero even when a finding blocks
  --help      show this message

the check inspects, and reports one line for each:

  the interpreter, the kernel headers, the build toolchain, the fitted
  interface card, the address allocation exclusion, the free space in the
  source, prefix and state filesystems, the system clock, and the target
  network interface

exit status zero means the installation may proceed; a warning does not stop
it.  exit status one means at least one blocking failure was reported and the
installation would leave the machine half finished if it were attempted.

nothing here changes the machine, so the check needs no privilege.
USAGE
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --rehearse|--rehearsal|--dry-run) export REHEARSAL="yes"; shift ;;
        --help|-h) usage; exit 0 ;;
        *) printf 'the argument %s is not recognised\n' "$1" >&2; usage; exit 2 ;;
    esac
done

# ---------------------------------------------------------------------------
# Reporting -- one line per item, and every failing line names its remedy
# ---------------------------------------------------------------------------

report_pass() {
    PASS_COUNT=$(( PASS_COUNT + 1 ))
    log_info "satisfied: $*"
}

report_warning() {
    WARNING_COUNT=$(( WARNING_COUNT + 1 ))
    log_warn "worth knowing: $*"
}

report_blocking() {
    BLOCKING_COUNT=$(( BLOCKING_COUNT + 1 ))
    log_error "blocking: $*"
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Separate the characters of an identifier so that Constraint Two renders it as
# a sequence a technician can read off against the silkscreen on the card,
# rather than as one improbable cardinal number.
spell_identifier() {
    local text="${1#0x}"
    local rendered="" index
    for (( index = 0; index < ${#text}; index++ )); do
        rendered+="${text:index:1} "
    done
    printf '%s' "${rendered% }"
}

# Walk upwards until an existing directory is found.  A filesystem question
# about a directory that has not been created yet is really a question about
# the filesystem that will hold it.
nearest_existing_directory() {
    local path="$1"
    while [[ ! -d "${path}" ]]; do
        local parent
        parent="$(dirname "${path}")"
        [[ "${parent}" == "${path}" ]] && break
        path="${parent}"
    done
    printf '%s' "${path}"
}

# The free space in mebibytes, or an empty string when it could not be read.
#
# The pipeline is guarded and its result is validated before use.  A pipeline
# whose first command fails aborts the script under pipefail, and this
# repository has already lost one build to exactly that shape.
free_mebibytes() {
    local path="$1"
    local blocks=""

    blocks="$(df -P -k -- "${path}" 2>/dev/null | awk 'NR == 2 { print $4 }')" || blocks=""
    [[ "${blocks}" =~ ^[0-9]+$ ]] || return 0
    printf '%s' "$(( blocks / 1024 ))"
}

# The names of the network interfaces this machine presents, as one phrase.
present_interface_names() {
    local directory="${ROOT_PREFIX}/sys/class/net"
    local -a names=()
    local entry

    [[ -d "${directory}" ]] || { printf 'none'; return 0; }
    for entry in "${directory}"/*; do
        [[ -e "${entry}" ]] || continue
        names+=( "$(basename "${entry}")" )
    done
    if (( ${#names[@]} == 0 )); then
        printf 'none'
        return 0
    fi
    printf '%s' "${names[*]}"
}

# The model name for a peripheral bus identifier.
#
# The table is deliberately carried here rather than read from the system's
# peripheral identifier database, because these appliances are frequently air
# gapped and that database is frequently absent or stale.  An identifier the
# table does not know is reported as unknown rather than guessed at, and the
# system database is consulted only as a second opinion in that case.
#
# The identifiers themselves are not written here. They live in one file,
# share/digium-cards.tsv, which the control plane reads as well.
#
# They used to be written twice -- once there and once here -- and the two
# copies disagreed on ten of the eleven identifiers they shared. Device zero
# two zero five was a four port analogue card in one and a dual span digital
# card in the other. A technician reads this output against the silkscreen on a
# card in their hand, and a name that is nearly right is worse than no name at
# all; two names that contradict each other are worse again. The file records
# which driver source its rows were read out of.
digium_card_catalogue() {
    local here
    here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    local candidate
    for candidate in \
        "${MYIPBX_CARD_CATALOGUE:-}" \
        "${here}/../share/digium-cards.tsv" \
        "${here}/../../share/digium-cards.tsv" \
        "${APPLIANCE_PREFIX:-/opt/myipbx}/share/digium-cards.tsv" \
        "/usr/share/myipbx/digium-cards.tsv"
    do
        if [[ -n "${candidate}" && -f "${candidate}" ]]; then
            printf '%s' "${candidate}"
            return 0
        fi
    done
    return 1
}

digium_card_name() {
    local vendor="$1"
    local model="$2"

    # One vendor that is not Digium's own, kept here rather than in the table
    # because it is not a device identifier: these cards all report the same
    # one and are told apart by their subsystem, which this check does not
    # read. Naming the family is the most that can honestly be said.
    if [[ "${vendor}:${model}" == "e159:0001" ]]; then
        printf 'an early Tiger Jet based Wildcard, of the X100P or TDM400P family'
        return 0
    fi

    [[ "${vendor}" == "d161" ]] || { printf ''; return 0; }

    local catalogue
    catalogue="$(digium_card_catalogue)" || { printf ''; return 0; }

    local device name module description
    while IFS=$'\t' read -r device name module description; do
        [[ "${device}" == "#"* || -z "${device}" || "${device}" == "device" ]] && continue
        if [[ "${device}" == "${model}" ]]; then
            printf '%s, %s, driven by %s' "${name}" "${description}" "${module}"
            return 0
        fi
    done < "${catalogue}"

    printf ''
}

# The system's own description of a slot, used only for a card the table above
# does not name.  Its output carries digits, which Constraint Two renders, and
# it is collapsed to a single line so that the one line per item rule holds.
system_card_description() {
    local slot="$1"
    local description=""

    have_command lspci || return 0
    description="$(lspci -s "${slot}" 2>/dev/null | tr '\n' ' ')" || description=""
    printf '%s' "${description}"
}

# ---------------------------------------------------------------------------
# The checks
# ---------------------------------------------------------------------------

check_interpreter() {
    if ! have_command python3; then
        report_blocking "no python three interpreter is installed, and the control plane is written for one; install the distribution package that provides the command named python three, at version three point eight or later, then run this check again"
        return 0
    fi

    local version=""
    version="$(python3 -c 'import sys; print(str(sys.version_info[0]) + "." + str(sys.version_info[1]))' 2>/dev/null)" || version=""
    if [[ -z "${version}" ]]; then
        report_blocking "a command named python three is on the path but it could not be asked for its version, so it is not a usable interpreter; check what that command actually is with the command type python3, replace it with a genuine interpreter, then run this check again"
        return 0
    fi

    if python3 -c "import sys; raise SystemExit(0 if sys.version_info >= (${MINIMUM_INTERPRETER_MAJOR}, ${MINIMUM_INTERPRETER_MINOR}) else 1)" 2>/dev/null; then
        report_pass "the python interpreter is present at version ${version}, which meets the required version three point eight or later"
    else
        report_blocking "the python interpreter is at version ${version} and the control plane requires version three point eight or later; install a newer interpreter package and make it the command named python three, then run this check again"
    fi
}

check_kernel_headers() {
    local release
    release="$(uname -r)"

    local candidate
    for candidate in "/lib/modules/${release}/build" \
                     "/usr/src/linux-headers-${release}" \
                     "/usr/src/kernels/${release}"; do
        if [[ -d "${ROOT_PREFIX}${candidate}" ]]; then
            report_pass "kernel headers matching the running kernel release ${release} are installed at ${candidate}"
            return 0
        fi
    done

    report_blocking "kernel headers for the running kernel release ${release} are missing, and the interface card drivers are compiled against them in stage three; install the headers package whose version matches the output of the command uname -r exactly -- the package named linux-headers followed by that release on a Debian derived system, or kernel-devel on a Red Hat derived system -- then run this check again"
}

#: The kernel release from which the released driver archive is known to fail
#: to compile. Kept in step with the same two values in
#: scripts/stage-three-dahdi-drivers.sh, which is where the choice is made.
_DRIVER_TREE_REQUIRED_MAJOR=6
_DRIVER_TREE_REQUIRED_MINOR=10

# Can stage three actually get the driver source it will need?
#
# On a kernel from six point ten onward the released archive does not compile,
# so the stage clones the development tree instead. At a site with no route off
# the premises -- which is most of the sites this appliance is built for -- that
# clone fails, and it fails in the middle of an installation, after the base
# system has been laid down and the packages installed. The operator is then
# holding a half built machine and a network error.
#
# This is the check that turns that into a sentence read beforehand.
check_driver_source() {
    local release major minor
    release="$(uname -r)"
    major="${release%%.*}"
    minor="${release#*.}"
    minor="${minor%%.*}"

    # A local archive satisfies every path. If one is named and present, the
    # question of reaching anything does not arise.
    local candidate
    for candidate in "${DRIVER_ARCHIVE:-}" "${MYIPBX_DRIVER_ARCHIVE:-}"; do
        if [[ -n "${candidate}" && -f "${candidate}" ]]; then
            report_pass "the interface driver source is already on this machine at ${candidate}, so stage three needs to reach nothing"
            return 0
        fi
    done

    local needs_tree=0
    if [[ "${DRIVER_SOURCE_MODE:-auto}" == "git" || "${DRIVER_SOURCE_MODE:-auto}" == "tree" ]]; then
        needs_tree=1
    elif [[ "${DRIVER_SOURCE_MODE:-auto}" == "archive" || "${DRIVER_SOURCE_MODE:-auto}" == "release" ]]; then
        needs_tree=0
    elif [[ "${major}" =~ ^[0-9]+$ && "${minor}" =~ ^[0-9]+$ ]]; then
        if (( major > _DRIVER_TREE_REQUIRED_MAJOR )) \
            || (( major == _DRIVER_TREE_REQUIRED_MAJOR && minor >= _DRIVER_TREE_REQUIRED_MINOR )); then
            needs_tree=1
        fi
    fi

    if (( needs_tree )); then
        if ! have_command git; then
            report_blocking "the running kernel release ${release} is newer than the last tagged driver release, so stage three must build from the development tree, and the command named git is not installed on this machine; install the git package, or download the driver source elsewhere and name the file in the variable DRIVER_ARCHIVE, then run this check again"
            return 0
        fi
        report_warning "the running kernel release ${release} is newer than the last tagged driver release, so stage three will clone the driver development tree from the network, and on a site with no route off the premises that clone fails partway through the installation and leaves a half built machine; if this machine has no such route, set the variable named DRIVER_ARCHIVE to a copy of the driver source fetched on a machine that has one, then run this check again"
        return 0
    fi

    report_pass "the running kernel release ${release} predates the driver interface changes, so stage three can build from the released archive"
}

check_build_toolchain() {
    local -a missing=()

    if ! have_command cc && ! have_command gcc; then
        missing+=("a compiler")
    fi
    if ! have_command make; then
        missing+=("the make utility")
    fi

    if (( ${#missing[@]} == 0 )); then
        report_pass "the build toolchain is present, with both a compiler and the make utility on the path"
        return 0
    fi

    local absent="${missing[0]}"
    if (( ${#missing[@]} > 1 )); then
        absent="${missing[0]} and ${missing[1]}"
    fi

    report_blocking "the build toolchain is incomplete, because ${absent} could not be found, and stages three and four compile from source; install the toolchain package group -- build-essential on a Debian derived system, or the development tools group on a Red Hat derived system -- then run this check again"
}

check_interface_card() {
    local devices="${ROOT_PREFIX}/sys/bus/pci/devices"

    if [[ ! -d "${devices}" ]]; then
        report_warning "the peripheral bus could not be enumerated because the directory at /sys/bus/pci/devices is absent, so nothing can be said about a fitted interface card; run this check on the appliance itself rather than inside a container, or fit and check the card by hand before stage three"
        return 0
    fi

    local found=0
    local -a described=()
    local device vendor model slot name

    for device in "${devices}"/*; do
        [[ -r "${device}/vendor" ]] || continue
        vendor="$(tr -d '[:space:]' <"${device}/vendor" 2>/dev/null)" || vendor=""
        case "${vendor}" in
            "0x${DIGIUM_VENDOR}"|"0x${TIGERJET_VENDOR}") ;;
            *) continue ;;
        esac

        found=$(( found + 1 ))
        slot="$(basename "${device}")"
        model=""
        if [[ -r "${device}/device" ]]; then
            model="$(tr -d '[:space:]' <"${device}/device" 2>/dev/null)" || model=""
        fi

        name="$(digium_card_name "${vendor#0x}" "${model#0x}")"
        if [[ -z "${name}" ]]; then
            name="$(system_card_description "${slot}")"
        fi
        if [[ -z "${name}" ]]; then
            name="an unrecognised Digium card bearing the device identifier $(spell_identifier "${model}")"
        fi
        described+=("${name} in the slot named ${slot}")
    done

    if (( found == 0 )); then
        report_warning "no Digium interface card is visible on the peripheral bus; this is legitimate when the appliance is being prepared before the card arrives, and the drivers are built regardless so that a card fitted later works at once, but if a card is already fitted then reseat it and check the slot with the command lspci before relying on stage three"
        return 0
    fi

    # Joined by hand rather than by the field separator, because two cards read
    # as one run-on sentence otherwise and the operator has to guess where the
    # first card's description ended.
    local listed="${described[0]}"
    local index
    for (( index = 1; index < ${#described[@]}; index++ )); do
        listed+="; and ${described[index]}"
    done

    report_pass "a legacy interface card is fitted: ${listed}"
}

check_address_allocation() {
    local audit="${SCRIPT_DIR}/verify-no-dhcp.sh"

    if [[ ! -f "${audit}" ]]; then
        report_blocking "the address allocation exclusion audit script is missing from this installation, and the appliance may not be installed without it; restore the file at scripts/verify-no-dhcp.sh from the repository, then run this check again"
        return 0
    fi

    # The audit's own findings are suppressed so that this report keeps to one
    # line per item; the remedy names the audit so the detail is one command
    # away.
    local status=0
    ROOT_PREFIX="${ROOT_PREFIX}" bash "${audit}" --quiet >/dev/null 2>&1 || status=$?

    case "${status}" in
        0)
            report_pass "this machine runs no address allocation service, so the exclusion the specification demands still holds"
            ;;
        1)
            report_blocking "this machine carries an address allocation service, which the appliance specification forbids absolutely; run the command scripts/verify-no-dhcp.sh to see which finding was made, stop and remove the offending service, then run this check again"
            ;;
        *)
            report_warning "the address allocation exclusion audit could not be completed on this machine, so the exclusion is unproven rather than broken; run the command scripts/verify-no-dhcp.sh on its own to see why it could not finish"
            ;;
    esac
}

check_free_space() {
    local path="$1"
    local required="$2"
    local purpose="$3"

    local target
    target="$(nearest_existing_directory "${ROOT_PREFIX}${path}")"

    local available
    available="$(free_mebibytes "${target}")"

    if [[ -z "${available}" ]]; then
        report_warning "the free space at ${path} could not be measured, so the room for ${purpose} is unproven; check the filesystem is mounted with the command df -h ${path} before starting the installation"
        return 0
    fi

    if (( available < required )); then
        report_blocking "the filesystem holding ${path} has only ${available} mebibytes free, and ${required} are needed for ${purpose}; free the difference, or mount a larger filesystem at ${path}, then run this check again"
        return 0
    fi

    report_pass "the filesystem holding ${path} has ${available} mebibytes free, and ${required} are needed for ${purpose}"
}

check_disk_space() {
    check_free_space "/usr/src" "${REQUIRED_SPACE_SOURCE}" "the driver and telephony engine source trees"
    check_free_space "/opt" "${REQUIRED_SPACE_PREFIX}" "the appliance prefix"
    check_free_space "/var" "${REQUIRED_SPACE_STATE}" "the appliance state, logs and package cache"
}

check_clock() {
    local now=""
    now="$(date -u +%s 2>/dev/null)" || now=""

    if ! [[ "${now}" =~ ^[0-9]+$ ]]; then
        report_warning "the system clock could not be read, so its plausibility is unproven; check it by hand with the command date before generating any certificate"
        return 0
    fi

    local reading
    reading="$(date -u '+%Y-%m-%d %H:%M:%S' 2>/dev/null || printf 'unreadable')"

    # A clock outside the plausible window is treated as blocking because the
    # failure it causes is silent and arrives much later: the transport
    # security certificate is generated with a validity period that has already
    # expired, or has not begun, and every browser rejects it for a reason that
    # never mentions the clock.
    if (( now < CLOCK_FLOOR )); then
        report_blocking "the system clock reads ${reading} in coordinated universal time, which is earlier than this appliance could possibly have been installed and usually means the board battery has failed; set the clock with the command timedatectl set-time, replace the battery if it will not hold, then run this check again -- a certificate generated against this clock is rejected by every browser from the moment it is made"
        return 0
    fi

    if (( now > CLOCK_CEILING )); then
        report_blocking "the system clock reads ${reading} in coordinated universal time, which is far beyond any plausible installation date; set the clock with the command timedatectl set-time, then run this check again -- a certificate generated against this clock is not valid until long after the appliance is in service"
        return 0
    fi

    local synchronised=""
    if have_command timedatectl; then
        synchronised="$(timedatectl show --property=NTPSynchronized --value 2>/dev/null)" || synchronised=""
    fi

    if [[ "${synchronised}" == "no" ]]; then
        report_warning "the system clock reads ${reading} in coordinated universal time, which is plausible, but it is not synchronised against any time source and will drift; point the machine at the time server this site already uses, or accept the drift knowingly"
        return 0
    fi

    report_pass "the system clock reads ${reading} in coordinated universal time, which is plausible"
}

check_network_interface() {
    local present
    present="$(present_interface_names)"

    if [[ -z "${APPLIANCE_INTERFACE}" ]]; then
        report_warning "the target network interface has not been named, and the appliance never guesses one; set the variable named APPLIANCE_INTERFACE to the interface the appliance will serve on, chosen from those present on this machine, which are: ${present}"
        return 0
    fi

    if [[ ! -e "${ROOT_PREFIX}/sys/class/net/${APPLIANCE_INTERFACE}" ]]; then
        report_blocking "the target network interface named ${APPLIANCE_INTERFACE} does not exist on this machine, so stage two would write a static configuration for nothing; set the variable named APPLIANCE_INTERFACE to one of the interfaces that are present, which are: ${present}, then run this check again"
        return 0
    fi

    # A fixture root presents interfaces that no running daemon knows about, so
    # the daemons are interrogated only when the real machine is being read.
    if [[ -n "${ROOT_PREFIX}" ]]; then
        report_pass "the target network interface named ${APPLIANCE_INTERFACE} exists"
        return 0
    fi

    local claimant=""

    if have_command nmcli; then
        local nm_state=""
        nm_state="$(nmcli --terse --fields DEVICE,STATE device status 2>/dev/null \
            | awk -F: -v want="${APPLIANCE_INTERFACE}" '$1 == want { print $2 }')" || nm_state=""
        if [[ -n "${nm_state}" && "${nm_state}" != "unmanaged" ]]; then
            claimant="the network management daemon"
        fi
    fi

    if [[ -z "${claimant}" ]] && have_command networkctl; then
        local link_state=""
        link_state="$(networkctl list --no-legend 2>/dev/null \
            | awk -v want="${APPLIANCE_INTERFACE}" '$2 == want { print $NF }')" || link_state=""
        if [[ "${link_state}" == "configured" || "${link_state}" == "configuring" ]]; then
            claimant="the system network daemon"
        fi
    fi

    if [[ -n "${claimant}" ]]; then
        report_warning "the target network interface named ${APPLIANCE_INTERFACE} exists but ${claimant} is already configuring it, and it will contend with the static configuration stage two writes; either mark the interface unmanaged in that daemon's configuration, or stop and disable the daemon, before the appliance is put into service"
        return 0
    fi

    report_pass "the target network interface named ${APPLIANCE_INTERFACE} exists and no competing configuration daemon claims it"
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

summarise() {
    log_info "the preflight check reports $(spell_integer "${PASS_COUNT}") satisfied, $(spell_integer "${WARNING_COUNT}") worth knowing, and $(spell_integer "${BLOCKING_COUNT}") blocking"
}

main() {
    banner "preflight check"

    if [[ "${EUID}" -ne 0 ]]; then
        log_info "this check is running without administrative privilege, which is deliberate; every inspection below only reads, and anything that cannot be read is reported rather than assumed"
    fi
    if is_rehearsal; then
        log_info "rehearsal mode is active; the findings below are reported in full but no blocking failure will fail this command"
    fi

    check_interpreter
    check_kernel_headers
    check_build_toolchain
    check_driver_source
    check_interface_card
    check_address_allocation
    check_disk_space
    check_clock
    check_network_interface

    summarise

    if (( BLOCKING_COUNT > 0 )); then
        if is_rehearsal; then
            log_warn "a real installation would stop at the blocking failure or failures above; rehearsal reports a passing status because nothing was changed and nothing can be left half finished"
            return 0
        fi
        log_error "the installation may not proceed until every blocking failure above is corrected; correct them and run this check again"
        return 1
    fi

    if (( WARNING_COUNT > 0 )); then
        log_info "no blocking failure was found; the installation may proceed with the warnings above understood"
    else
        log_info "every condition is satisfied; the installation may proceed"
    fi
    return 0
}

main "$@"
