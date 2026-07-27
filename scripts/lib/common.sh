#!/usr/bin/env bash
# Shared library for the appliance staging scripts.
#
# Two obligations are discharged here so that no individual stage has to
# remember them.
#
# Constraint Two: every line these scripts emit passes through spell_all, which
# replaces each run of digit characters with its value in words.  A stage
# author cannot leak a digit into a log by forgetting the rule.
#
# Idempotence: each stage records a receipt when it completes.  Re-running a
# stage detects its receipt and reports the work as already satisfied rather
# than repeating it, so the installer is safe to run again after a failure.

set -o errexit
set -o nounset
set -o pipefail

APPLIANCE_NAME="Legacy-to-Modern IPBX Appliance"
APPLIANCE_USER="${APPLIANCE_USER:-myipbx}"
APPLIANCE_PREFIX="${APPLIANCE_PREFIX:-/opt/myipbx}"
APPLIANCE_STATE_DIR="${APPLIANCE_STATE_DIR:-/var/lib/myipbx}"
APPLIANCE_CONFIG_DIR="${APPLIANCE_CONFIG_DIR:-/etc/myipbx}"
APPLIANCE_LOG_DIR="${APPLIANCE_LOG_DIR:-/var/log/myipbx}"
APPLIANCE_RECEIPT_DIR="${APPLIANCE_RECEIPT_DIR:-${APPLIANCE_STATE_DIR}/receipts}"
APPLIANCE_INSTALL_LOG="${APPLIANCE_INSTALL_LOG:-${APPLIANCE_LOG_DIR}/installation.log}"

# Rehearsal mode performs no mutation.  Every stage honours it.
REHEARSAL="${REHEARSAL:-no}"

# ---------------------------------------------------------------------------
# Constraint Two -- numeral spelling
# ---------------------------------------------------------------------------

_ONES=(zero one two three four five six seven eight nine ten eleven twelve
       thirteen fourteen fifteen sixteen seventeen eighteen nineteen)
_TENS=("" "" twenty thirty forty fifty sixty seventy eighty ninety)
_SCALES=("" thousand million billion trillion)

spell_group() {
    local value="$1"
    local -a words=()

    if (( value >= 100 )); then
        words+=("${_ONES[value / 100]}" hundred)
        value=$(( value % 100 ))
    fi
    if (( value >= 20 )); then
        local tens_word="${_TENS[value / 10]}"
        local remainder=$(( value % 10 ))
        if (( remainder > 0 )); then
            words+=("${tens_word}-${_ONES[remainder]}")
        else
            words+=("${tens_word}")
        fi
    elif (( value > 0 )); then
        words+=("${_ONES[value]}")
    fi

    printf '%s' "${words[*]}"
}

spell_integer() {
    local value="$1"
    local sign=""

    if [[ "${value}" == -* ]]; then
        sign="negative "
        value="${value#-}"
    fi
    if (( value == 0 )); then
        printf '%szero' "${sign}"
        return 0
    fi

    local -a groups=()
    while (( value > 0 )); do
        groups+=( $(( value % 1000 )) )
        value=$(( value / 1000 ))
    done

    local -a rendered=()
    local index
    for (( index = ${#groups[@]} - 1; index >= 0; index-- )); do
        local group="${groups[index]}"
        (( group == 0 )) && continue
        rendered+=( "$(spell_group "${group}")" )
        if (( index > 0 )); then
            rendered+=( "${_SCALES[index]}" )
        fi
    done

    printf '%s%s' "${sign}" "${rendered[*]}"
}

# Replace every run of digit characters in the argument with its words.
spell_all() {
    local text="$1"
    local rendered=""
    local prefix run remainder stripped leading

    while [[ "${text}" =~ ^([^0-9]*)([0-9]+)(.*)$ ]]; do
        prefix="${BASH_REMATCH[1]}"
        run="${BASH_REMATCH[2]}"
        remainder="${BASH_REMATCH[3]}"

        stripped="${run#"${run%%[!0]*}"}"
        leading=""
        local zeros=$(( ${#run} - ${#stripped} ))
        local counter
        for (( counter = 0; counter < zeros; counter++ )); do
            leading+="zero "
        done

        if [[ -n "${stripped}" ]]; then
            rendered+="${prefix}${leading}$(spell_integer "${stripped}")"
        else
            # The run was entirely zeros; the leading words already cover it.
            rendered+="${prefix}${leading% }"
        fi
        text="${remainder}"
    done

    printf '%s%s' "${rendered}" "${text}"
}

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

_emit() {
    local level="$1"
    shift
    local message
    message="$(spell_all "$*")"
    local stamp
    stamp="$(spell_all "$(date -u '+%Y-%m-%d %H:%M:%S')")"
    local line="${stamp} ${level} ${message}"

    printf '%s\n' "${line}"
    if [[ -w "$(dirname "${APPLIANCE_INSTALL_LOG}")" ]] 2>/dev/null; then
        printf '%s\n' "${line}" >>"${APPLIANCE_INSTALL_LOG}" 2>/dev/null || true
    fi
}

log_info()  { _emit "information" "$@"; }
log_warn()  { _emit "warning" "$@"; }
log_error() { _emit "error" "$@" >&2; }

log_step() {
    printf '\n'
    _emit "stage" "$*"
    _emit "stage" "------------------------------------------------------------"
}

fail() {
    log_error "$@"
    exit 1
}

# ---------------------------------------------------------------------------
# Guards
# ---------------------------------------------------------------------------

require_root() {
    if [[ "${EUID}" -ne 0 ]]; then
        fail "this stage must be run with administrative privilege"
    fi
}

have_command() {
    command -v "$1" >/dev/null 2>&1
}

require_command() {
    local name="$1"
    have_command "${name}" || fail "the required command named ${name} is not available"
}

# Detect the package manager without assuming a distribution family.
detect_package_manager() {
    if have_command apt-get; then
        printf 'apt'
    elif have_command dnf; then
        printf 'dnf'
    elif have_command yum; then
        printf 'yum'
    elif have_command zypper; then
        printf 'zypper'
    else
        printf 'unknown'
    fi
}

# ---------------------------------------------------------------------------
# Mutation helpers -- every one of these honours rehearsal mode
# ---------------------------------------------------------------------------

is_rehearsal() {
    [[ "${REHEARSAL}" == "yes" || "${REHEARSAL}" == "true" || "${REHEARSAL}" == "1" ]]
}

run_command() {
    if is_rehearsal; then
        log_info "rehearsal: would run: $*"
        return 0
    fi
    log_info "running: $*"
    "$@"
}

ensure_directory() {
    local path="$1"
    local mode="${2:-0755}"
    local owner="${3:-}"

    if [[ -d "${path}" ]]; then
        return 0
    fi
    run_command mkdir -p "${path}"
    run_command chmod "${mode}" "${path}"
    if [[ -n "${owner}" ]]; then
        run_command chown "${owner}" "${path}"
    fi
}

install_file() {
    local source="$1"
    local destination="$2"
    local mode="${3:-0644}"

    [[ -f "${source}" ]] || fail "the source file at ${source} does not exist"
    ensure_directory "$(dirname "${destination}")"
    run_command install -m "${mode}" "${source}" "${destination}"
}

# ---------------------------------------------------------------------------
# Idempotence receipts
# ---------------------------------------------------------------------------

receipt_path() {
    printf '%s/%s.receipt' "${APPLIANCE_RECEIPT_DIR}" "$1"
}

stage_completed() {
    [[ -f "$(receipt_path "$1")" ]]
}

mark_stage_completed() {
    local stage="$1"
    local detail="${2:-completed}"

    if is_rehearsal; then
        log_info "rehearsal: would record a receipt for the stage named ${stage}"
        return 0
    fi
    ensure_directory "${APPLIANCE_RECEIPT_DIR}"
    {
        printf 'stage: %s\n' "${stage}"
        printf 'detail: %s\n' "$(spell_all "${detail}")"
        printf 'recorded at: %s\n' "$(spell_all "$(date -u '+%Y-%m-%d %H:%M:%S')")"
    } >"$(receipt_path "${stage}")"
}

clear_stage_receipt() {
    local stage="$1"
    run_command rm -f "$(receipt_path "${stage}")"
}

# Skip a stage that has already completed, unless a re-run was demanded.
skip_if_completed() {
    local stage="$1"
    if [[ "${FORCE_RERUN:-no}" == "yes" ]]; then
        return 1
    fi
    if stage_completed "${stage}"; then
        log_info "the stage named ${stage} is already satisfied and will not be repeated"
        return 0
    fi
    return 1
}

# ---------------------------------------------------------------------------
# Constraint One -- the address allocation exclusion
# ---------------------------------------------------------------------------
#
# Every stage that touches networking calls this before it proceeds.  The
# staging scripts install no address allocation service; this guard catches an
# allocation service that arrived on the machine by some other route.

assert_no_address_allocation_service() {
    local audit
    audit="$(dirname "${BASH_SOURCE[0]}")/../verify-no-dhcp.sh"

    if [[ -x "${audit}" ]]; then
        if ! "${audit}" --quiet; then
            fail "an address allocation service is present on this machine, which the appliance specification forbids"
        fi
        return 0
    fi

    log_warn "the address allocation audit script was not found; performing the inline check instead"
    local offender
    for offender in dhcpd dhcpd6 udhcpd kea-dhcp4 kea-dhcp6 dhcrelay; do
        if pgrep -x "${offender}" >/dev/null 2>&1; then
            fail "the address allocation service named ${offender} is running, which the appliance specification forbids"
        fi
    done
    return 0
}

# ---------------------------------------------------------------------------
# Presentation
# ---------------------------------------------------------------------------

banner() {
    printf '\n'
    printf '  %s\n' "${APPLIANCE_NAME}"
    printf '  %s\n' "$(spell_all "$1")"
    printf '  this appliance assigns no addresses and spells every numeral in full letters\n'
    printf '\n'
}
