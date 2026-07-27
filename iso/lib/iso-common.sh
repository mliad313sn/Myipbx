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

# The console is served over a secured transport. This second port answers only
# by sending a browser to the secured one, so that an operator who types the
# address without a scheme is redirected rather than left at a refused
# connection. It serves no content and issues no session.
APPLIANCE_REDIRECT_PORT="${APPLIANCE_REDIRECT_PORT:-8080}"

# The name the appliance answers to.  The live boot machinery reads its own
# settings out of the boot image rather than out of the root filesystem, and
# this build deliberately does not rebuild the boot image, so a name written
# into the root filesystem alone is discarded at boot.  Passing it as a boot
# argument reaches the live boot machinery at the only moment it is listening,
# which is why the name lives here rather than in the configuration stage.
APPLIANCE_HOST_NAME="${APPLIANCE_HOST_NAME:-myipbx}"

# The marker the firmware bootloader searches for to find the image.  It is the
# appliance's own file rather than the package disc marker a distribution would
# use, because this image is an appliance and not a package disc: claiming to
# be one makes the live boot machinery try to read package indexes off it and
# report, correctly, that there are none.
APPLIANCE_IMAGE_MARKER="${APPLIANCE_IMAGE_MARKER:-/.disk/appliance-image}"

# Arguments every boot entry carries.  Both the legacy and the firmware boot
# paths are generated from this one value so that the two cannot drift apart.
#
# The host name is passed and the live session's account name deliberately is
# not. The appliance already carries a system account named after itself, which
# owns the control plane, and asking the live boot to create a login account of
# the same name makes it stop and report that the user already exists. The live
# session account keeps the machinery's own default, which collides with
# nothing.
APPLIANCE_KERNEL_ARGUMENTS="${APPLIANCE_KERNEL_ARGUMENTS:-boot=casper hostname=${APPLIANCE_HOST_NAME} console=tty0 console=ttyS0,115200n8}"

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

# Everything a package installation needs in place before it can run.
#
# The stages exist to be run again on their own -- that is what the receipts
# are for -- but the configuring stage deliberately removes two things a
# package installation depends on: the package index, so that a stale index
# does not ship, and the restriction that stops a package from starting the
# service it installed.  Re-entering an earlier stage after that point would
# otherwise install against no index, and start daemons inside a filesystem
# that is not a running system.
#
# Both are restored here rather than in the stage that removed them, because
# this is the only code that needs them and it can tell whether they are
# missing.  Neither restoration does anything on a build that runs straight
# through.
prepare_image_for_installation() {
    if [[ ! -f "${CHROOT_DIR}/usr/sbin/policy-rc.d" ]]; then
        log_info "restoring the restriction that stops services starting inside the image"
        write_into_chroot /usr/sbin/policy-rc.d 0755 <<'EOF'
#!/bin/sh
# During the image build nothing may start. The appliance decides what runs.
exit 101
EOF
    fi

    if ! compgen -G "${CHROOT_DIR}/var/lib/apt/lists/*Packages*" >/dev/null 2>&1; then
        log_info "the package index inside the image is absent; refreshing it"
        in_chroot apt-get update >/dev/null 2>&1 \
            || log_warn "the package index inside the image could not be refreshed"
    fi
}

# Install packages inside the image.
install_in_chroot() {
    prepare_image_for_installation
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
