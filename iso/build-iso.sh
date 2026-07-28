#!/usr/bin/env bash
# Build the appliance image.
#
# Runs the five image stages in order and produces one bootable file. Every
# stage is individually re-runnable and records a receipt when it completes, so
# a build interrupted after twenty minutes of compressing does not start again
# from an empty directory.
#
# This must be run with administrative privilege, because building a root
# filesystem means creating device nodes and files owned by other accounts.

set -o errexit
set -o nounset
set -o pipefail

BUILD_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=iso/lib/iso-common.sh
source "${BUILD_DIR}/lib/iso-common.sh"

STAGES=(
    "one-bootstrap.sh"
    "two-payload.sh"
    "three-configure.sh"
    "four-compress.sh"
    "five-master.sh"
)

STAGE_NAMES=(one two three four five)

FROM_STAGE=1
TO_STAGE=5
export FORCE_RERUN="${FORCE_RERUN:-no}"
RUN_BOOT_TEST="${RUN_BOOT_TEST:-no}"

usage() {
    cat <<'USAGE'
usage: build-iso.sh [options]

  --rehearse            perform no mutation; report what each stage would do
  --from-stage NUMBER   begin at this stage, from one to five
  --to-stage NUMBER     stop after this stage, from one to five
  --force               re-run stages that already recorded a receipt
  --boot-test           boot the finished image in an emulator and verify it
  --clean               remove the whole build tree and start again
  --help                show this message

the five image stages are:

  one    bootstrap the base operating system
  two    the payload: kernel, telephony engine, interface drivers, appliance
  three  configure the appliance: account, static address, services, messages
  four   compress the root filesystem and lay out the image tree
  five   install both bootloaders and master the image

the finished image, and a checksum beside it, are written to the output
directory inside the build tree.
USAGE
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --rehearse|--rehearsal|--dry-run) export REHEARSAL="yes"; shift ;;
        --from-stage) FROM_STAGE="${2:-1}"; shift 2 ;;
        --to-stage) TO_STAGE="${2:-5}"; shift 2 ;;
        --force) export FORCE_RERUN="yes"; shift ;;
        --boot-test) RUN_BOOT_TEST="yes"; shift ;;
        --clean) rm -rf "${BUILD_ROOT}"; printf 'the build tree was removed\n'; shift ;;
        --help|-h) usage; exit 0 ;;
        *) printf 'the argument %s is not recognised\n' "$1" >&2; usage; exit 2 ;;
    esac
done

validate_range() {
    [[ "${FROM_STAGE}" =~ ^[1-5]$ ]] || fail "the starting stage must be a value from one to five"
    [[ "${TO_STAGE}" =~ ^[1-5]$ ]] || fail "the ending stage must be a value from one to five"
    (( FROM_STAGE <= TO_STAGE )) || fail "the starting stage cannot come after the ending stage"
}

preflight() {
    log_step "preflight"
    require_root

    require_build_tools debootstrap mksquashfs xorriso chroot \
        || fail "the image build cannot proceed without its tools"

    local available
    available="$(df --output=avail --block-size=1G "$(dirname "${BUILD_ROOT}")" 2>/dev/null | tail -n 1 | tr -d ' ')"
    if [[ "${available}" =~ ^[0-9]+$ ]]; then
        log_info "the build tree has $(spell_integer "${available}") gibibytes available"
        if (( available < 8 )); then
            log_warn "an image build usually needs at least eight gibibytes of free space"
        fi
    fi

    log_info "the base will be ${BASE_DISTRIBUTION} at the suite named ${BASE_SUITE}"
    log_info "the image will answer on ${APPLIANCE_DEFAULT_ADDRESS} at port number ${APPLIANCE_CONSOLE_PORT}"

    if is_rehearsal; then
        log_warn "rehearsal mode is active; no image will be produced"
    fi

    mkdir -p "${BUILD_ROOT}" "${RECEIPT_DIR}" "${OUTPUT_DIR}"
}

run_stage() {
    local index="$1"
    local script="${BUILD_DIR}/stages/${STAGES[index - 1]}"
    local name="${STAGE_NAMES[index - 1]}"

    [[ -f "${script}" ]] || fail "the script for image stage ${name} is missing"

    log_step "beginning image stage ${name}"
    local started
    started="$(date +%s)"

    if bash "${script}"; then
        local elapsed=$(( $(date +%s) - started ))
        log_info "image stage ${name} finished in $(spell_duration_seconds "${elapsed}")"
        return 0
    fi

    log_error "image stage ${name} failed"
    log_error "correct the reported cause and run this build again; completed stages will not be repeated"
    return 1
}

# The staging library spells integers; a duration reads better in units.
#
# The unit agrees with the number in front of it. Sidestepping that with
# "minute or minutes" made every build report read "one minute or minutes and
# thirty-five seconds", which is not a sentence anybody would say out loud, and
# these lines are read aloud down a telephone by technicians on site.
spell_duration_seconds() {
    local total="$1"
    local minutes=$(( total / 60 ))
    local seconds=$(( total % 60 ))

    local minute_unit="minutes"
    local second_unit="seconds"
    (( minutes == 1 )) && minute_unit="minute"
    (( seconds == 1 )) && second_unit="second"

    if (( minutes > 0 )); then
        printf '%s %s and %s %s' \
            "$(spell_integer "${minutes}")" "${minute_unit}" \
            "$(spell_integer "${seconds}")" "${second_unit}"
    else
        printf '%s %s' "$(spell_integer "${seconds}")" "${second_unit}"
    fi
}

summarise() {
    printf '\n'
    log_step "build summary"

    local index
    for index in 1 2 3 4 5; do
        local receipt_name="image-stage-${STAGE_NAMES[index - 1]}"
        local stage_file="${STAGES[index - 1]}"
        receipt_name="image-stage-${stage_file%.sh}"
        if image_stage_completed "${receipt_name}"; then
            log_info "image stage ${STAGE_NAMES[index - 1]} is satisfied"
        else
            log_warn "image stage ${STAGE_NAMES[index - 1]} is not satisfied"
        fi
    done

    local target="${OUTPUT_DIR}/${IMAGE_NAME}.iso"
    if [[ -f "${target}" ]]; then
        report_size "${target}" "the finished image"
        log_info "the image is at ${target}"
        log_info "write it to a flash device, or burn it, and start the machine from it"
    fi

    log_info "this appliance assigns no addresses; it spells quantities and keeps identifiers"
}

main() {
    banner "building the appliance image"
    validate_range
    preflight

    local index
    for (( index = FROM_STAGE; index <= TO_STAGE; index++ )); do
        run_stage "${index}" || exit 1
    done

    summarise

    if [[ "${RUN_BOOT_TEST}" == "yes" ]] && ! is_rehearsal; then
        log_step "verifying the image by starting it"
        bash "${BUILD_DIR}/boot-test.sh" || fail "the finished image did not boot"
    fi

    log_info "the image build is complete"
}

main "$@"
