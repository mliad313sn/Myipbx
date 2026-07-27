#!/usr/bin/env bash
# The appliance installer.
#
# Runs the five staging scripts in dependency order.  Every stage is
# individually re-runnable, records a receipt when it completes, and honours
# rehearsal mode, so a failed installation is resumed rather than restarted.
#
# Before any stage runs, the address allocation exclusion audit must pass.  An
# appliance that allocates addresses is not an appliance this installer will
# produce.

set -o errexit
set -o nounset
set -o pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/lib/common.sh
source "${SCRIPT_DIR}/lib/common.sh"

STAGES=(
    "stage-one-base-system.sh"
    "stage-two-network-static.sh"
    "stage-three-dahdi-drivers.sh"
    "stage-four-asterisk.sh"
    "stage-five-appliance-service.sh"
)

STAGE_NAMES=(one two three four five)

FROM_STAGE=1
TO_STAGE=5
export FORCE_RERUN="${FORCE_RERUN:-no}"

usage() {
    cat <<'USAGE'
usage: install-appliance.sh [options]

  --rehearse            perform no mutation; report what each stage would do
  --from-stage NUMBER   begin at this stage, from one to five
  --to-stage NUMBER     stop after this stage, from one to five
  --force               re-run stages that have already recorded a receipt
  --audit-only          run the address allocation exclusion audit and exit
  --help                show this message

the five stages are:

  one    the base system: toolchain, kernel headers, service account, tree
  two    the static network configuration and the address allocation audit
  three  the legacy Digium interface card drivers, compiled for this kernel
  four   the telephony engine
  five   the control plane, the dashboard, and the service unit

every stage is idempotent.  a failed installation is resumed by running this
command again; completed stages report themselves as already satisfied.
USAGE
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --rehearse|--rehearsal|--dry-run)
            export REHEARSAL="yes"; shift ;;
        --from-stage)
            FROM_STAGE="${2:-1}"; shift 2 ;;
        --to-stage)
            TO_STAGE="${2:-5}"; shift 2 ;;
        --force)
            export FORCE_RERUN="yes"; shift ;;
        --audit-only)
            exec "${SCRIPT_DIR}/verify-no-dhcp.sh" ;;
        --help|-h)
            usage; exit 0 ;;
        *)
            printf 'the argument %s is not recognised\n' "$1" >&2
            usage
            exit 2 ;;
    esac
done

validate_range() {
    if ! [[ "${FROM_STAGE}" =~ ^[1-5]$ ]]; then
        fail "the starting stage must be a value from one to five"
    fi
    if ! [[ "${TO_STAGE}" =~ ^[1-5]$ ]]; then
        fail "the ending stage must be a value from one to five"
    fi
    if (( FROM_STAGE > TO_STAGE )); then
        fail "the starting stage cannot come after the ending stage"
    fi
}

preflight() {
    log_step "preflight"

    # The standalone preflight check runs before anything else, because every
    # condition it reports is one that would otherwise be discovered midway
    # through a stage, with the machine already part changed.
    if ! bash "${SCRIPT_DIR}/preflight-check.sh"; then
        fail "the preflight check reported a blocking failure; correct the causes it named and run this installer again"
    fi

    require_root
    require_command uname
    require_command install

    log_info "the machine reports the kernel release $(uname -r)"
    log_info "the machine reports the architecture $(uname -m)"

    log_info "running the address allocation exclusion audit"
    if ! "${SCRIPT_DIR}/verify-no-dhcp.sh"; then
        fail "the installation cannot proceed while this machine allocates addresses"
    fi

    if is_rehearsal; then
        log_warn "rehearsal mode is active; no change will be made to this machine"
    fi
}

run_stage() {
    local index="$1"
    local script="${SCRIPT_DIR}/${STAGES[index - 1]}"
    local name="${STAGE_NAMES[index - 1]}"

    [[ -f "${script}" ]] || fail "the script for stage ${name} is missing from the repository"

    log_step "beginning stage ${name}"
    if bash "${script}"; then
        log_info "stage ${name} finished successfully"
        return 0
    fi

    log_error "stage ${name} failed"
    log_error "correct the reported cause and run this installer again; the stages that already completed will not be repeated"
    return 1
}

summarise() {
    printf '\n'
    log_step "installation summary"

    local index
    for index in 1 2 3 4 5; do
        local stage_file="${STAGES[index - 1]}"
        local receipt_name="${stage_file%.sh}"
        local name="${STAGE_NAMES[index - 1]}"
        if stage_completed "${receipt_name}"; then
            log_info "stage ${name} is satisfied"
        else
            log_warn "stage ${name} is not satisfied"
        fi
    done

    log_info "this appliance assigns no addresses and spells every numeral in full letters"
}

main() {
    banner "autonomous installation"
    validate_range
    preflight

    local index
    for (( index = FROM_STAGE; index <= TO_STAGE; index++ )); do
        run_stage "${index}" || exit 1
    done

    summarise
    log_info "the installation is complete"
}

main "$@"
