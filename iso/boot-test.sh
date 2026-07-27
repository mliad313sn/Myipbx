#!/usr/bin/env bash
# Start the finished image in an emulator and prove that it boots.
#
# An image that masters cleanly is not an image that boots. The only way to
# know is to start it, so this does: it runs the image in an emulator with a
# serial console, watches what comes out, and looks for evidence that the
# machine reached userspace with the appliance's own identity on it.
#
# Exit status: zero when the image booted and was recognised, one when it did
# not, two when no emulator is available to try.

set -o errexit
set -o nounset
set -o pipefail

TEST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=iso/lib/iso-common.sh
source "${TEST_DIR}/lib/iso-common.sh"

IMAGE="${IMAGE:-${OUTPUT_DIR}/${IMAGE_NAME}.iso}"
BOOT_TIMEOUT="${BOOT_TIMEOUT:-600}"
BOOT_MEMORY="${BOOT_MEMORY:-2048}"
TRANSCRIPT="${TRANSCRIPT:-${BUILD_ROOT}/boot-transcript.txt}"

# What the transcript must contain for the boot to count as a success. Each is
# evidence of a different layer having worked.
# The default boot is deliberately quiet, so the kernel banner is not evidence
# of anything. What proves a boot is the appliance's own text appearing and a
# login prompt being offered, because both come from userspace.
declare -a REQUIRED_MARKERS=(
    "Legacy-to-Modern IPBX Appliance"   # the appliance's own message reached the console
    "login:"                            # userspace started and offered a session
)

# Any of these means the boot failed in a way worth naming.
declare -a FAILURE_MARKERS=(
    "Kernel panic"
    "Unable to mount root"
    "No such device"
    "Boot failed"
)

check_emulator() {
    if ! have_command qemu-system-x86_64; then
        log_warn "no emulator is installed on this machine, so the image cannot be started here"
        log_warn "the image itself was still verified for its boot record and its contents"
        exit 2
    fi
    if [[ ! -e /dev/kvm ]]; then
        log_warn "hardware assisted virtualisation is not available; the boot will be emulated and slow"
    fi
}

start_image() {
    log_step "starting the image in an emulator"
    log_info "the image being started is ${IMAGE}"
    log_info "the boot will be allowed $(spell_integer "${BOOT_TIMEOUT}") seconds"

    [[ -f "${IMAGE}" ]] || fail "there is no image at ${IMAGE} to start"

    mkdir -p "$(dirname "${TRANSCRIPT}")"
    : >"${TRANSCRIPT}"

    # No network is attached. The appliance neither requests nor offers an
    # address, so a boot test does not need one, and attaching one would let a
    # failure hide behind a timeout waiting for it.
    set +o errexit
    timeout --signal=KILL "${BOOT_TIMEOUT}" \
        qemu-system-x86_64 \
            -m "${BOOT_MEMORY}" \
            -smp 2 \
            -cdrom "${IMAGE}" \
            -boot d \
            -nographic \
            -serial mon:stdio \
            -display none \
            -net none \
            -no-reboot \
            >"${TRANSCRIPT}" 2>&1
    local status=$?
    set -o errexit

    # The emulator is killed by the timeout once the boot has been observed,
    # so a terminated status is the expected outcome rather than a failure.
    log_info "the emulator stopped with the status $(spell_integer "${status}")"
    return 0
}

examine_transcript() {
    log_step "examining what the image said while it started"

    [[ -s "${TRANSCRIPT}" ]] || fail "the image produced no output at all when started"

    local lines
    lines="$(wc -l <"${TRANSCRIPT}")"
    log_info "the image produced $(spell_integer "${lines}") lines of console output"

    local findings=0
    local marker

    for marker in "${FAILURE_MARKERS[@]}"; do
        if grep -qi "${marker}" "${TRANSCRIPT}"; then
            log_error "the boot transcript contains the failure marker: ${marker}"
            grep -i -m 3 "${marker}" "${TRANSCRIPT}" | while read -r line; do
                log_error "  ${line}"
            done
            findings=$(( findings + 1 ))
        fi
    done

    for marker in "${REQUIRED_MARKERS[@]}"; do
        if grep -qi "${marker}" "${TRANSCRIPT}"; then
            log_info "the boot transcript shows the expected marker: ${marker}"
        else
            log_error "the boot transcript never showed the expected marker: ${marker}"
            findings=$(( findings + 1 ))
        fi
    done

    # Constraint One, observed rather than asserted: nothing in a boot of this
    # appliance may mention obtaining an address.
    if grep -qiE "dhcpd|dhcp server|offering lease" "${TRANSCRIPT}"; then
        log_error "the boot transcript mentions address allocation, which this appliance forbids"
        findings=$(( findings + 1 ))
    else
        log_info "the boot transcript mentions no address allocation of any kind"
    fi

    local observed
    observed="$(grep -aoE "[A-Za-z0-9-]+ login:" "${TRANSCRIPT}" | head -n 1 | sed 's/ login://')"
    if [[ -n "${observed}" ]]; then
        log_info "the machine offered a session as the host named ${observed}"
    fi

    if (( findings > 0 )); then
        log_error "the boot examination made $(spell_integer "${findings}") finding or findings"
        log_error "the full transcript is at ${TRANSCRIPT}"
        return 1
    fi

    log_info "the image booted and was recognised"
    log_info "the full transcript is at ${TRANSCRIPT}"
    return 0
}

main() {
    banner "verifying the image by starting it"
    check_emulator
    start_image
    examine_transcript
}

main "$@"
