#!/usr/bin/env bash
# Shared library for the appliance image build.
#
# Reuses the staging library so that the image build logs in exactly the same
# spelled form as the installer does, and so that Constraint Two holds through
# the build as well as through the product.

set -o errexit
set -o nounset
set -o pipefail

ISO_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ISO_ROOT="$(cd "${ISO_LIB_DIR}/.." && pwd)"
REPOSITORY_ROOT="$(cd "${ISO_ROOT}/.." && pwd)"

# The appliance staging library carries the numeral spelling and the logging.
# shellcheck source=scripts/lib/common.sh
source "${REPOSITORY_ROOT}/scripts/lib/common.sh"

# ---------------------------------------------------------------------------
# Where the build happens, and what it produces
# ---------------------------------------------------------------------------

BUILD_ROOT="${BUILD_ROOT:-/var/tmp/myipbx-image}"
CHROOT_DIR="${CHROOT_DIR:-${BUILD_ROOT}/rootfs}"
STAGING_DIR="${STAGING_DIR:-${BUILD_ROOT}/staging}"
OUTPUT_DIR="${OUTPUT_DIR:-${BUILD_ROOT}/output}"
RECEIPT_DIR="${RECEIPT_DIR:-${BUILD_ROOT}/receipts}"

IMAGE_NAME="${IMAGE_NAME:-myipbx-appliance}"
IMAGE_LABEL="${IMAGE_LABEL:-MYIPBX_APPLIANCE}"

# ---------------------------------------------------------------------------
# What goes inside the image
# ---------------------------------------------------------------------------
#
# The base distribution is chosen at build time.  The appliance targets older
# machines, so a long term support release is the only sensible default.
BASE_DISTRIBUTION="${BASE_DISTRIBUTION:-ubuntu}"
BASE_SUITE="${BASE_SUITE:-noble}"
BASE_MIRROR="${BASE_MIRROR:-http://archive.ubuntu.com/ubuntu}"
BASE_COMPONENTS="${BASE_COMPONENTS:-main,universe}"
BASE_ARCHITECTURE="${BASE_ARCHITECTURE:-amd64}"

# The appliance's own default address, printed on the boot screen.  The
# appliance never requests an address and never offers one, so it must arrive
# already reachable or an operator could never open its console the first time.
APPLIANCE_DEFAULT_ADDRESS="${APPLIANCE_DEFAULT_ADDRESS:-192.168.100.10}"
APPLIANCE_DEFAULT_PREFIX="${APPLIANCE_DEFAULT_PREFIX:-24}"
APPLIANCE_CONSOLE_PORT="${APPLIANCE_CONSOLE_PORT:-8088}"

# ---------------------------------------------------------------------------
# Build receipts, so a failed build resumes rather than restarts
# ---------------------------------------------------------------------------

image_stage_completed() {
    [[ -f "${RECEIPT_DIR}/$1.receipt" ]]
}

mark_image_stage_completed() {
    mkdir -p "${RECEIPT_DIR}"
    {
        printf 'stage: %s\n' "$1"
        printf 'recorded at: %s\n' "$(spell_all "$(date -u '+%Y-%m-%d %H:%M:%S')")"
    } >"${RECEIPT_DIR}/$1.receipt"
}

skip_image_stage_if_completed() {
    if [[ "${FORCE_RERUN:-no}" == "yes" ]]; then
        return 1
    fi
    if image_stage_completed "$1"; then
        log_info "the image stage named $1 is already satisfied and will not be repeated"
        return 0
    fi
    return 1
}

# ---------------------------------------------------------------------------
# Working inside the image being built
# ---------------------------------------------------------------------------

# Mount the kernel filesystems the package manager needs inside the chroot.
mount_chroot() {
    local target="${1:-${CHROOT_DIR}}"

    mountpoint -q "${target}/proc" || mount -t proc none "${target}/proc"
    mountpoint -q "${target}/sys" || mount -t sysfs none "${target}/sys"
    mountpoint -q "${target}/dev/pts" || mount -t devpts none "${target}/dev/pts" 2>/dev/null || true
}

# Unmount them again.  Called from a trap, so it must never fail the build.
unmount_chroot() {
    local target="${1:-${CHROOT_DIR}}"
    local point

    for point in dev/pts sys proc; do
        if mountpoint -q "${target}/${point}" 2>/dev/null; then
            umount --lazy "${target}/${point}" 2>/dev/null || true
        fi
    done
}

# Run a command inside the image.  The package manager must never ask a
# question, because there is nobody to answer it.
in_chroot() {
    mount_chroot
    DEBIAN_FRONTEND=noninteractive \
    LC_ALL=C \
    LANGUAGE=C \
    LANG=C \
        chroot "${CHROOT_DIR}" /usr/bin/env \
            DEBIAN_FRONTEND=noninteractive \
            LC_ALL=C LANG=C \
            PATH=/usr/sbin:/usr/bin:/sbin:/bin \
            "$@"
}

# Install packages inside the image.
install_in_chroot() {
    log_info "installing into the image: $*"
    in_chroot apt-get install --yes --no-install-recommends "$@"
}

# Write a file into the image with a mode, creating its directory.
write_into_chroot() {
    local destination="$1"
    local mode="${2:-0644}"

    mkdir -p "$(dirname "${CHROOT_DIR}${destination}")"
    cat >"${CHROOT_DIR}${destination}"
    chmod "${mode}" "${CHROOT_DIR}${destination}"
}

# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

report_size() {
    local path="$1"
    local description="$2"

    if [[ ! -e "${path}" ]]; then
        return 0
    fi

    # Measuring a root filesystem being built will meet a directory it cannot
    # read, and the measuring tool reports that by its exit status. Reporting a
    # size must never be able to fail a build, so every failure here is
    # swallowed deliberately rather than allowed to reach the shell's error
    # handling.
    local bytes=""
    bytes="$(du -sb "${path}" 2>/dev/null | cut -f1 || true)"

    if ! [[ "${bytes}" =~ ^[0-9]+$ ]]; then
        log_info "${description} could not be measured"
        return 0
    fi

    local mebibytes=$(( bytes / 1048576 ))
    log_info "${description} occupies $(spell_integer "${mebibytes}") mebibytes"
    return 0
}

require_build_tools() {
    local missing=()
    local tool
    for tool in "$@"; do
        have_command "${tool}" || missing+=("${tool}")
    done
    if (( ${#missing[@]} > 0 )); then
        log_error "the image build needs tools this machine does not have: ${missing[*]}"
        log_error "install them and run the build again"
        return 1
    fi
    return 0
}
