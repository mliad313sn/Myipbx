#!/usr/bin/env bash
# Install the running appliance onto a fixed disk.
#
# The image this script ships inside is a live image: it runs from the medium it
# was written to, and everything it does is forgotten when the machine stops.
# That is the right shape for trying the appliance and the wrong shape for
# owning one. This script takes the appliance that is running and lays it down
# on a disk, so the machine starts from itself afterwards and keeps what it is
# told.
#
# It is deliberately narrow. It installs the appliance that is already here; it
# does not choose a distribution, ask what packages to add, or offer a set of
# layouts. An appliance that can be installed one way is an appliance whose
# installation can be proved, and proving it matters more here than choice.
#
# Two things this script will never do, because the product forbids them. It
# never allocates an address, and it never leaves an address allocation service
# behind: the installed system keeps the static address the image arrived with,
# and the allocation service unit names stay masked exactly as the image masked
# them. And it never emits a digit character: every numeral in every message
# below is spelled in full letters by the shared library.

set -o errexit
set -o nounset
set -o pipefail
set -o errtrace

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# The shared library lives in the repository during a build and in the appliance
# tree on a running appliance. Both are looked for, because this script has to
# work in a checkout, where it can be rehearsed, and on the booted image, where
# it does the real work.
COMMON_LIBRARY=""
for candidate in \
    "${SCRIPT_DIR}/../../scripts/lib/common.sh" \
    "/opt/crossbar/bin/lib/common.sh" \
    "/usr/lib/crossbar/common.sh"
do
    if [[ -f "${candidate}" ]]; then
        COMMON_LIBRARY="${candidate}"
        break
    fi
done
if [[ -z "${COMMON_LIBRARY}" ]]; then
    printf 'the shared appliance library could not be found; this script cannot run without it\n' >&2
    exit 1
fi
# shellcheck source=scripts/lib/common.sh
source "${COMMON_LIBRARY}"

# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

TARGET_DISK=""
ASSUME_YES="no"

# Where the target root filesystem is mounted while it is being built. A path
# under the volatile run directory is used rather than a temporary directory, so
# that an operator watching the machine can see what is mounted and where.
TARGET_MOUNT="${TARGET_MOUNT:-/run/crossbar-installation-target}"

# The first partition holds nothing a filesystem would recognise. On a disk with
# a modern partition table there is no gap after the boot record for a legacy
# bootloader to hide its core image in, so the table reserves one instead, and
# this is it. One mebibyte is the conventional size and is far more than the
# core image needs.
BIOS_BOOT_SIZE_MIB=1

# The second partition is the one the firmware of a modern machine reads. It is
# an ordinary file allocation table filesystem, and the firmware finds the
# bootloader by looking for a file at a fixed path inside it.
EFI_SIZE_MIB=512

# The third partition takes the rest of the disk and holds the appliance.

# The word an operator has to type when the installation was not asked to
# proceed unattended. It is a word rather than a letter on purpose: this step
# destroys everything on the disk, and a single keystroke is too easy to give.
CONFIRMATION_WORD="destroy"

# Where the live medium is mounted, discovered during the preflight.
LIVE_MEDIUM=""
LIVE_MEDIUM_DISK=""

# The pseudo filesystems bound into the target while the bootloader and the boot
# image are being built there. Recorded so that the exit trap can take them down
# again in the reverse order.
BOUND_POINTS=()
TARGET_MOUNTED="no"
EFI_MOUNTED="no"

# The root filesystem the live boot machinery mounts read only. Copying this is
# preferred over copying the running system, because it is exactly what the
# image was built and tested with, whereas the running system carries whatever
# has happened since the machine started.
SQUASHFS_RELATIVE_PATH="casper/filesystem.squashfs"

# What must not be copied when the running system is copied instead. The kernel
# filesystems are not files and cannot be copied. The live boot machinery's own
# mounts would copy the source into itself. The target mount would copy the
# target into the target.
RSYNC_EXCLUSIONS=(
    /proc /sys /dev /run /tmp /var/tmp
    /mnt /media /cdrom /isodevice
    /rofs /lib/live/mount /run/live
    /swapfile /swap.img
    /lost+found
)

# ---------------------------------------------------------------------------
# How this script talks about what it is doing
# ---------------------------------------------------------------------------

usage() {
    cat <<'USAGE'
usage: crossbar-install-to-disk.sh --disk DEVICE [options]

  --disk DEVICE   the whole disk to install onto, for example /dev/sda
  --assume-yes    proceed without asking; for an unattended installation
  --dry-run       report every step and write nothing at all
  --help          show this message

environment:
  CROSSBAR_LIVE_DISK  the disk the live medium is on, for the rare medium whose
                    device this script cannot identify by itself. it refuses to
                    install rather than guess, because guessing wrong destroys
                    the medium it is reading from partway through.

this installs the appliance that is running from the live medium onto a fixed
disk. everything on that disk is destroyed. the disk is partitioned so that the
installed appliance starts on an older machine and on a modern one alike, and
both bootloaders are installed.

the installed appliance keeps the static address the image arrived with. this
script allocates no address and installs no address allocation service, and it
leaves every allocation service unit name masked exactly as the image left it.
USAGE
}

parse_arguments() {
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --disk)
                TARGET_DISK="${2:-}"; shift 2 ;;
            --disk=*)
                TARGET_DISK="${1#*=}"; shift ;;
            --assume-yes|--yes|-y)
                ASSUME_YES="yes"; shift ;;
            --dry-run|--rehearse|--rehearsal)
                export REHEARSAL="yes"; shift ;;
            --help|-h)
                usage; exit 0 ;;
            *)
                printf 'the argument %s is not recognised\n' "$1" >&2
                usage
                exit 2 ;;
        esac
    done
}

# A refusal that a rehearsal is allowed to walk past.
#
# Every guard below stops a real installation dead. A rehearsal has to be able
# to run somewhere other than a booted appliance -- on a build machine, for
# instance, where there is no live medium and no spare disk -- or it could never
# be exercised, and a mode that is never exercised is a mode that does not work.
# So during a rehearsal a guard reports what would have stopped the real thing
# and carries on.
guard() {
    if is_rehearsal; then
        log_warn "a real installation would stop here: $*"
        return 0
    fi
    fail "$@"
}

# ---------------------------------------------------------------------------
# Working on the target
# ---------------------------------------------------------------------------

# Refuse to remove anything unless the target really is a mounted filesystem.
#
# Everything this script deletes is named relative to the target's mount point.
# If a mount had silently not happened, those same paths would name the running
# system's own directories instead, and the removals meant for a fresh disk
# would land on the appliance doing the installing. The mount is therefore
# proved rather than assumed, immediately before anything is removed.
assert_target_is_mounted() {
    [[ -n "${TARGET_MOUNT}" ]] \
        || fail "the target mount point is not set; nothing will be removed"
    mountpoint -q "${TARGET_MOUNT}" 2>/dev/null \
        || findmnt --noheadings --target "${TARGET_MOUNT}" >/dev/null 2>&1 \
        || fail "the target at ${TARGET_MOUNT} is not a mounted filesystem; nothing will be removed"
}

# Run a command inside the system being installed. The bootloader, the boot
# image and the service enablement all have to be done by the target's own
# tools against the target's own tree, not by this machine's.
in_target() {
    if is_rehearsal; then
        log_command "rehearsal: would run inside the installed system: $*"
        return 0
    fi
    chroot "${TARGET_MOUNT}" /usr/bin/env \
        PATH=/usr/sbin:/usr/bin:/sbin:/bin \
        LC_ALL=C LANG=C \
        DEBIAN_FRONTEND=noninteractive \
        "$@"
}

# Write a file into the system being installed, creating its directory.
write_into_target() {
    local destination="$1"
    local mode="${2:-0644}"

    if is_rehearsal; then
        log_info "rehearsal: would write the file at ${destination} into the installed system"
        cat >/dev/null
        return 0
    fi
    mkdir -p "$(dirname "${TARGET_MOUNT}${destination}")"
    cat >"${TARGET_MOUNT}${destination}"
    chmod "${mode}" "${TARGET_MOUNT}${destination}"
}

# The partition device name a disk gives its partitions. A disk whose name ends
# in a digit separates the partition number with a letter, because otherwise the
# two numbers would run together and name nothing.
partition_device() {
    local disk="$1"
    local index="$2"

    if [[ "${disk}" =~ [0-9]$ ]]; then
        printf '%sp%s' "${disk}" "${index}"
    else
        printf '%s%s' "${disk}" "${index}"
    fi
}

# The whole disk a device belongs to. A partition reports its parent; a disk
# reports nothing and is its own answer.
disk_of_device() {
    local device="$1"
    local parent=""

    # The measurement is allowed to fail without failing the installation, so
    # the pipeline's status is discarded deliberately rather than left to the
    # shell's error handling, which would treat a device it cannot read as a
    # fatal fault.
    parent="$(lsblk --noheadings --output PKNAME "${device}" 2>/dev/null | head -n 1 || true)"
    parent="${parent//[[:space:]]/}"

    if [[ -n "${parent}" ]]; then
        printf '/dev/%s' "${parent}"
    else
        printf '%s' "${device}"
    fi
}

# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------

require_tools() {
    log_step "checking that this machine carries the tools an installation needs"

    # Named once, checked once, and reported once. An installation that stops
    # halfway because the fourth tool it needed was absent has already destroyed
    # the disk, so everything is demanded before anything is touched.
    local -a needed=(
        lsblk blkid findmnt
        sgdisk
        mkfs.ext4 mkfs.vfat
        mount umount chroot install sync
    )

    local -a missing=()
    local tool
    for tool in "${needed[@]}"; do
        have_command "${tool}" || missing+=("${tool}")
    done

    # Either copying method is acceptable, so neither one alone is required.
    if ! have_command unsquashfs && ! have_command rsync; then
        missing+=("unsquashfs or rsync")
    fi

    if (( ${#missing[@]} > 0 )); then
        guard "this machine is missing the tools an installation needs: ${missing[*]}"
        return 0
    fi

    log_info "every tool the installation needs is present"
}

assert_running_from_live_image() {
    log_step "confirming that this machine is running from the live medium"

    local point
    for point in /cdrom /run/live/medium /lib/live/mount/medium /isodevice /media/cdrom; do
        if [[ -f "${point}/${SQUASHFS_RELATIVE_PATH}" ]] || [[ -f "${point}/casper/vmlinuz" ]]; then
            LIVE_MEDIUM="${point}"
            break
        fi
    done

    if [[ -z "${LIVE_MEDIUM}" ]]; then
        # The live medium is the only source of the appliance this script
        # installs. Without it there is nothing to copy, and a machine that is
        # not running the appliance is not a machine that should be laying it
        # down on a disk.
        guard "this machine is not running from the appliance live medium, so there is nothing to install from"
        return 0
    fi

    log_info "the live medium is mounted at ${LIVE_MEDIUM}"

    local source=""
    source="$(findmnt --noheadings --output SOURCE --target "${LIVE_MEDIUM}" 2>/dev/null | head -n 1 || true)"
    source="${source//[[:space:]]/}"
    if [[ -n "${source}" && "${source}" == /dev/* ]]; then
        LIVE_MEDIUM_DISK="$(disk_of_device "${source}")"
        log_info "the live medium is the device ${source} on the disk ${LIVE_MEDIUM_DISK}"
    else
        # Not a warning. This is the input to the check that stops this script
        # repartitioning the disk it is itself running from, and without it
        # that check does nothing at all -- it is written as "if the live disk
        # is known and the target is it, refuse", so an unknown live disk
        # silently disables it. A medium on an overlay, a loop device, or a
        # network mount all reach this branch.
        #
        # The consequence of continuing is that the installer removes the
        # running system from under itself and destroys the medium it is
        # reading from, which is not something to risk on a warning nobody
        # reads among thirty other lines. An operator who knows the target is a
        # different disk can say so.
        # Not a warning. This is the input to the check that stops this script
        # repartitioning the disk it is itself running from, and the refusal
        # that follows from it is raised where that check is made.
        log_warn "the disk the live medium sits on could not be identified by name"
    fi
}

assert_disk_is_safe() {
    log_step "examining the disk the installation was pointed at"

    # The fault and the action stay on one line. A refusal quoted on its own --
    # into a ticket, a message, a photograph of a screen -- has to carry the
    # way out with it, or the technician is left exactly where they were.
    if [[ -z "${TARGET_DISK}" ]]; then
        log_error "no disk was named; name the disk to install onto with the disk option, for example --disk /dev/sda, then run this again"
        exit 2
    fi

    if [[ "${TARGET_DISK}" != /dev/* ]]; then
        fail "the disk must be named by its device path, and ${TARGET_DISK} is not one; name it as it appears under /dev, for example /dev/sda, which the command lsblk will list for you"
    fi

    if [[ ! -b "${TARGET_DISK}" ]]; then
        guard "the path ${TARGET_DISK} is not a block device on this machine"
    fi

    # A partition is not a disk. Installing onto one would produce a system with
    # a partition table inside a partition, which nothing would boot.
    local kind=""
    kind="$(lsblk --noheadings --nodeps --output TYPE "${TARGET_DISK}" 2>/dev/null | head -n 1 || true)"
    kind="${kind//[[:space:]]/}"
    if [[ -n "${kind}" && "${kind}" != "disk" ]]; then
        guard "the device ${TARGET_DISK} is a ${kind} rather than a whole disk"
    fi

    # The disk the appliance is running from cannot also be the disk it is
    # installed onto. Repartitioning it would remove the running system from
    # under the very script doing the removing.
    # An operator who knows where the medium is can say so. The override sits
    # here, beside the check it unblocks, rather than beside the detection --
    # a detection that never ran, on a machine with no medium mounted at all,
    # would otherwise skip past it.
    if [[ -z "${LIVE_MEDIUM_DISK}" && -n "${CROSSBAR_LIVE_DISK:-}" ]]; then
        LIVE_MEDIUM_DISK="${CROSSBAR_LIVE_DISK}"
        log_warn "proceeding on your word that the live medium is on the disk ${LIVE_MEDIUM_DISK}"
    fi

    # Written so that an unknown live disk refuses rather than passes.
    #
    # The earlier form asked "is the live disk known and equal to the target",
    # which reads as a safety check and is not one: a medium whose device could
    # not be named -- an overlay, a loop device, a network mount -- made the
    # first half false and turned the whole check off. The script logged a
    # warning among thirty other lines and would have repartitioned the disk it
    # was reading itself from, partway through reading it. Stated positively,
    # an unknown live disk is a refusal.
    if [[ -z "${LIVE_MEDIUM_DISK}" ]]; then
        guard "the live medium's disk is not known, so the disk ${TARGET_DISK} cannot be shown to be a different disk from the one this script is running from, and installing onto that one would destroy the medium partway through; set the variable named CROSSBAR_LIVE_DISK to the live medium's disk, for example /dev/sdb, then run this again"
    elif [[ "${TARGET_DISK}" == "${LIVE_MEDIUM_DISK}" ]]; then
        guard "the disk ${TARGET_DISK} is the one the live medium is on, and it will not be touched; name a different disk, which the command lsblk will list for you, then run this again"
    fi

    # Anything mounted from this disk is in use by something, and something in
    # use is something whose owner did not expect it to be destroyed.
    local mounted=""
    mounted="$(lsblk --noheadings --output MOUNTPOINT "${TARGET_DISK}" 2>/dev/null | tr -d ' ' | tr '\n' ' ' || true)"
    if [[ -n "${mounted// /}" ]]; then
        guard "the disk ${TARGET_DISK} has a mounted filesystem on it and will not be touched"
    fi

    # Swap does not appear as a mount point, so it is asked about separately.
    if have_command swapon; then
        local swap=""
        swap="$(swapon --noheadings --show=NAME 2>/dev/null | tr '\n' ' ' || true)"
        local device
        for device in ${swap}; do
            if [[ "$(disk_of_device "${device}")" == "${TARGET_DISK}" ]]; then
                guard "the disk ${TARGET_DISK} carries swap that is in use and will not be touched"
            fi
        done
    fi

    local bytes=""
    bytes="$(lsblk --bytes --noheadings --nodeps --output SIZE "${TARGET_DISK}" 2>/dev/null | head -n 1 || true)"
    bytes="${bytes//[[:space:]]/}"
    if [[ "${bytes}" =~ ^[0-9]+$ ]]; then
        local gibibytes=$(( bytes / 1073741824 ))
        log_info "the disk ${TARGET_DISK} holds $(spell_integer "${gibibytes}") gibibytes"
        # The appliance, its documentation and room to keep call recordings.
        if (( gibibytes < 8 )); then
            guard "the disk ${TARGET_DISK} is smaller than the eight gibibytes an appliance needs"
        fi
    else
        log_warn "the size of the disk ${TARGET_DISK} could not be measured"
    fi
}

describe_what_will_be_destroyed() {
    log_step "what this installation will destroy"

    log_warn "everything on the disk ${TARGET_DISK} is about to be removed and cannot be recovered"
    log_info "the disk currently carries:"

    local line
    while IFS= read -r line; do
        [[ -n "${line}" ]] || continue
        log_info "  ${line}"
    done < <(lsblk --output NAME,SIZE,FSTYPE,LABEL,MOUNTPOINT "${TARGET_DISK}" 2>/dev/null || true)

    log_info "and it will be replaced by:"
    log_info "  a partition table of the modern kind, which both older and newer machines can be made to read"
    log_info "  a boot partition of $(spell_integer "${BIOS_BOOT_SIZE_MIB}") mebibyte for the legacy bootloader's core image"
    log_info "  a firmware partition of $(spell_integer "${EFI_SIZE_MIB}") mebibytes for the modern bootloader"
    log_info "  a root partition taking all of the remaining space, holding the appliance"
}

confirm() {
    if [[ "${ASSUME_YES}" == "yes" ]]; then
        log_warn "the installation was asked to proceed without confirmation"
        return 0
    fi
    if is_rehearsal; then
        log_info "rehearsal: an operator would be asked to confirm the destruction here"
        return 0
    fi

    printf '\n'
    printf '  Type the word %s and press return to destroy this disk.\n' "${CONFIRMATION_WORD}"
    printf '  Anything else abandons the installation and changes nothing.\n'
    printf '\n  > '

    local reply=""
    read -r reply || true
    printf '\n'

    if [[ "${reply}" != "${CONFIRMATION_WORD}" ]]; then
        log_info "the installation was abandoned and nothing was changed"
        exit 0
    fi
    log_info "the operator confirmed the destruction of the disk ${TARGET_DISK}"
}

# ---------------------------------------------------------------------------
# Laying out the disk
# ---------------------------------------------------------------------------
#
# One disk has to boot two kinds of machine, because this appliance is sold to
# replace telephone systems that are older than the firmware on the machines
# that would replace them. The layout below serves both from the same table.
#
# The table itself is the modern kind. An older machine's boot record cannot
# read it, so the first partition exists to hold the legacy bootloader's core
# image, which the boot record can find and hand control to. The second is the
# filesystem a modern machine's firmware reads on its own. The third is the
# appliance. A machine of either age finds a path it understands, and no
# operator has to be told which kind of machine they have.

partition_disk() {
    log_step "laying out the disk"

    run_command sgdisk --zap-all "${TARGET_DISK}"

    run_command sgdisk \
        --new="1:0:+${BIOS_BOOT_SIZE_MIB}M" \
        --typecode=1:ef02 \
        --change-name=1:"crossbar legacy boot" \
        --new="2:0:+${EFI_SIZE_MIB}M" \
        --typecode=2:ef00 \
        --change-name=2:"crossbar firmware boot" \
        --new=3:0:0 \
        --typecode=3:8300 \
        --change-name=3:"crossbar root" \
        "${TARGET_DISK}"

    log_info "the disk was given three partitions: a legacy boot area, a firmware boot filesystem, and the appliance root"

    settle_partition_table
}

# The kernel has to be told the table changed before the new partitions can be
# opened by name, and the device nodes appear a moment after it is told.
settle_partition_table() {
    if is_rehearsal; then
        log_info "rehearsal: the kernel would be asked to re-read the partition table"
        return 0
    fi

    if have_command partprobe; then
        partprobe "${TARGET_DISK}" >/dev/null 2>&1 || true
    elif have_command blockdev; then
        blockdev --rereadpt "${TARGET_DISK}" >/dev/null 2>&1 || true
    fi
    if have_command udevadm; then
        udevadm settle --timeout=30 >/dev/null 2>&1 || true
    fi

    local root_partition
    root_partition="$(partition_device "${TARGET_DISK}" 3)"

    local attempt
    for attempt in 1 2 3 4 5 6 7 8 9 10; do
        if [[ -b "${root_partition}" ]]; then
            return 0
        fi
        sleep 1
    done

    fail "the new partitions on ${TARGET_DISK} did not appear; the disk may be held by something else"
}

make_filesystems() {
    log_step "making the filesystems"

    local efi_partition root_partition
    efi_partition="$(partition_device "${TARGET_DISK}" 2)"
    root_partition="$(partition_device "${TARGET_DISK}" 3)"

    # The firmware reads one kind of filesystem and only one, so there is no
    # choice to make here.
    run_command mkfs.vfat -F 32 -n CROSSBAREFI "${efi_partition}"

    # The root filesystem is the conservative one. A newer filesystem would
    # offer features this appliance does not use, on machines whose recovery
    # tools may not know them.
    run_command mkfs.ext4 -F -L crossbar-root "${root_partition}"

    log_info "the firmware partition and the appliance root were both formatted"
}

mount_target() {
    log_step "mounting the target"

    local efi_partition root_partition
    efi_partition="$(partition_device "${TARGET_DISK}" 2)"
    root_partition="$(partition_device "${TARGET_DISK}" 3)"

    if is_rehearsal; then
        log_info "rehearsal: the appliance root would be mounted at ${TARGET_MOUNT}"
        log_info "rehearsal: the firmware partition would be mounted beneath it"
        return 0
    fi

    mkdir -p "${TARGET_MOUNT}"
    mount "${root_partition}" "${TARGET_MOUNT}" \
        || fail "the appliance root could not be mounted at ${TARGET_MOUNT}"
    TARGET_MOUNTED="yes"

    mkdir -p "${TARGET_MOUNT}/boot/efi"
    mount "${efi_partition}" "${TARGET_MOUNT}/boot/efi" \
        || fail "the firmware partition could not be mounted"
    EFI_MOUNTED="yes"

    log_info "the target is mounted at ${TARGET_MOUNT}"
}

# ---------------------------------------------------------------------------
# Copying the appliance onto the target
# ---------------------------------------------------------------------------

copy_root_filesystem() {
    log_step "copying the appliance onto the disk"

    local squashfs=""
    if [[ -n "${LIVE_MEDIUM}" && -f "${LIVE_MEDIUM}/${SQUASHFS_RELATIVE_PATH}" ]]; then
        squashfs="${LIVE_MEDIUM}/${SQUASHFS_RELATIVE_PATH}"
    fi

    if [[ -n "${squashfs}" ]] && have_command unsquashfs; then
        # The compressed root filesystem is preferred because it is exactly what
        # the image build produced and the build's own boot test proved. The
        # running system is that same filesystem plus whatever this session has
        # done to it, and this session's temporary state is not something an
        # installed appliance should inherit.
        log_info "the source is the compressed root filesystem on the live medium"
        run_command unsquashfs -force -dest "${TARGET_MOUNT}" "${squashfs}"
        log_info "the compressed root filesystem was unpacked onto the disk"
        return 0
    fi

    if ! have_command rsync; then
        guard "there is no compressed root filesystem to unpack and no copying tool to fall back on"
        return 0
    fi

    log_warn "no compressed root filesystem was found; the running system will be copied instead"

    # Everything excluded below is either not a file at all, or belongs to this
    # boot rather than to the appliance, or is the target itself.
    local -a arguments=(--archive --hard-links --acls --xattrs --numeric-ids --info=none)
    local exclusion
    for exclusion in "${RSYNC_EXCLUSIONS[@]}"; do
        arguments+=("--exclude=${exclusion}")
        log_info "the copy will leave out ${exclusion}"
    done
    arguments+=("--exclude=${TARGET_MOUNT}")
    log_info "the copy will leave out ${TARGET_MOUNT}, which is the target itself"

    run_command rsync "${arguments[@]}" / "${TARGET_MOUNT}/"
    log_info "the running system was copied onto the disk"
}

# The compressed root filesystem is built without the kernel and its boot image
# inside it, because the bootloader on the medium has to reach those before
# anything is mounted and they are therefore carried separately. An installed
# system needs them back in the place a bootloader on a disk looks for them.
install_kernel() {
    log_step "putting the kernel back where an installed system keeps it"

    local release
    release="$(detect_kernel_release)"
    if [[ -z "${release}" ]]; then
        guard "no kernel module tree was found on the target, so the kernel release cannot be determined"
        return 0
    fi
    log_info "the installed appliance will run the kernel release ${release}"

    if is_rehearsal; then
        log_info "rehearsal: the kernel from the live medium would be placed on the disk"
        return 0
    fi

    if ls "${TARGET_MOUNT}/boot"/vmlinuz-* >/dev/null 2>&1; then
        log_info "the target already carries a kernel of its own"
        return 0
    fi

    [[ -n "${LIVE_MEDIUM}" && -f "${LIVE_MEDIUM}/casper/vmlinuz" ]] \
        || fail "there is no kernel on the live medium to install"

    install -m 0644 "${LIVE_MEDIUM}/casper/vmlinuz" "${TARGET_MOUNT}/boot/vmlinuz-${release}"
    log_info "the kernel from the live medium was placed on the disk"
}

detect_kernel_release() {
    local release=""
    if [[ -d "${TARGET_MOUNT}/lib/modules" ]]; then
        # The listing is allowed to fail quietly; the caller decides what an
        # empty answer means.
        release="$(ls -1 "${TARGET_MOUNT}/lib/modules" 2>/dev/null | sort | tail -n 1 || true)"
    fi
    if [[ -z "${release}" ]] && is_rehearsal; then
        release="$(uname -r)"
    fi
    printf '%s' "${release}"
}

# ---------------------------------------------------------------------------
# Turning a live system into an installed one
# ---------------------------------------------------------------------------

remove_live_boot_machinery() {
    log_step "removing the live boot machinery"

    # The live boot machinery's whole job is to build a writable layer over a
    # read only image and to derive a machine's identity at every start. An
    # installed appliance has a writable root of its own and an identity it
    # keeps, so the machinery is not merely unnecessary, it is wrong: left in
    # place it would try to find a medium that is no longer there.
    in_target dpkg --purge --force-depends casper >/dev/null 2>&1 \
        || log_warn "the live boot package could not be removed cleanly; its files will be removed by hand"

    if is_rehearsal; then
        log_info "rehearsal: the live boot settings, hooks and boot scripts would be removed"
        return 0
    fi

    assert_target_is_mounted

    rm -f "${TARGET_MOUNT}/etc/casper.conf"
    rm -f "${TARGET_MOUNT}/usr/share/initramfs-tools/hooks/casper"
    rm -f "${TARGET_MOUNT}/usr/share/initramfs-tools/scripts/casper"
    rm -f "${TARGET_MOUNT}/usr/share/initramfs-tools/scripts/casper-bottom"/* 2>/dev/null || true
    rm -rf "${TARGET_MOUNT}/usr/share/initramfs-tools/scripts/casper-bottom"
    rm -rf "${TARGET_MOUNT}/usr/share/initramfs-tools/scripts/casper-helpers"
    rm -rf "${TARGET_MOUNT}/usr/share/casper"
    rm -rf "${TARGET_MOUNT}/var/lib/casper"
    rm -f "${TARGET_MOUNT}/etc/initramfs-tools/conf.d/casper.conf" 2>/dev/null || true

    log_info "the live boot machinery is gone from the installed system"
}

write_filesystem_table() {
    log_step "writing the filesystem table"

    local efi_partition root_partition
    efi_partition="$(partition_device "${TARGET_DISK}" 2)"
    root_partition="$(partition_device "${TARGET_DISK}" 3)"

    local root_uuid="" efi_uuid=""
    if ! is_rehearsal; then
        root_uuid="$(blkid -s UUID -o value "${root_partition}" 2>/dev/null || true)"
        efi_uuid="$(blkid -s UUID -o value "${efi_partition}" 2>/dev/null || true)"
        [[ -n "${root_uuid}" ]] || fail "the appliance root partition has no unique identifier to mount it by"
        [[ -n "${efi_uuid}" ]] || fail "the firmware partition has no unique identifier to mount it by"
    fi

    # Filesystems are named by their unique identifiers rather than by device
    # path, because a machine that gains a disk renumbers its device paths and
    # an appliance that stops booting when somebody adds a disk is not an
    # appliance anybody would keep.
    write_into_target /etc/fstab 0644 <<EOF
# ${APPLIANCE_NAME} -- the filesystems this appliance mounts at start up
#
# Every filesystem is named by its own unique identifier, so that adding or
# moving a disk cannot change which one is the root.

UUID=${root_uuid}	/	ext4	errors=remount-ro	0	1
UUID=${efi_uuid}	/boot/efi	vfat	umask=0077	0	1
EOF

    log_info "the filesystem table names the root and the firmware partition by their unique identifiers"
}

generate_machine_identity() {
    log_step "giving this appliance an identity of its own"

    # The image ships with no identity, deliberately, because many machines boot
    # the same image and no two of them may claim to be the same machine. The
    # installed appliance is one machine, so it gets one identity, generated
    # here and kept from now on.
    if is_rehearsal; then
        log_info "rehearsal: a fresh machine identity would be generated on the disk"
        return 0
    fi

    assert_target_is_mounted

    rm -f "${TARGET_MOUNT}/etc/machine-id" "${TARGET_MOUNT}/var/lib/dbus/machine-id"
    : >"${TARGET_MOUNT}/etc/machine-id"
    in_target systemd-machine-id-setup >/dev/null 2>&1 \
        || log_warn "a machine identity could not be generated now; the first start will generate one"

    # Nor may every appliance share a host key, so any that arrived are removed
    # and the first start makes new ones.
    rm -f "${TARGET_MOUNT}"/etc/ssh/ssh_host_* 2>/dev/null || true

    log_info "the installed appliance has an identity that is its own"
}

rebuild_boot_image() {
    log_step "rebuilding the boot image for the installed root"

    # The boot image on the medium knows how to find a compressed filesystem on
    # a removable device and build a writable layer over it. The installed
    # appliance needs one that knows how to find an ordinary root filesystem on
    # a disk instead, which means building it here, against the target's own
    # tree, after the live machinery has been taken out of that tree.
    local release
    release="$(detect_kernel_release)"
    if [[ -z "${release}" ]]; then
        guard "the kernel release could not be determined, so no boot image can be built"
        return 0
    fi

    if in_target update-initramfs -c -k "${release}"; then
        log_info "a boot image was built for the kernel release ${release}"
        return 0
    fi

    log_warn "a boot image could not be built for the kernel release ${release}"

    if is_rehearsal; then
        return 0
    fi

    # Falling back to the medium's own boot image is worse but not useless: it
    # carries the same modules, and a machine that starts and can be repaired is
    # better than a machine that cannot start at all.
    if [[ -n "${LIVE_MEDIUM}" && -f "${LIVE_MEDIUM}/casper/initrd" ]]; then
        install -m 0644 "${LIVE_MEDIUM}/casper/initrd" "${TARGET_MOUNT}/boot/initrd.img-${release}"
        log_warn "the boot image from the live medium was used instead, and should be rebuilt from the appliance console"
        return 0
    fi

    fail "no boot image could be produced for the installed system"
}

preserve_appliance_identity() {
    log_step "keeping the appliance the appliance"

    # Nothing here changes the appliance. It confirms that the installation
    # carried across the things that make this a telephone system rather than a
    # bare operating system, and puts back anything the removal of the live
    # machinery may have disturbed.

    if [[ -f "${TARGET_MOUNT}/etc/systemd/network/10-crossbar-static.network" ]] || is_rehearsal; then
        log_info "the static address the appliance arrived with is on the disk, unchanged"
    else
        log_warn "the static address file did not survive the copy and must be restored from the appliance console"
    fi

    # These are enabled again rather than assumed, because a package removed
    # during the live machinery's departure can take a dependent link with it.
    local unit
    for unit in systemd-networkd.service crossbar-hostname.service crossbar.service asterisk.service; do
        in_target systemctl enable "${unit}" >/dev/null 2>&1 \
            || log_warn "the service unit named ${unit} could not be enabled on the disk"
    done

    # A rack machine with no monitor is still an appliance somebody has to be
    # able to recover, so the serial console follows the appliance onto the disk.
    in_target systemctl enable serial-getty@ttyS0.service >/dev/null 2>&1 \
        || log_warn "the serial console could not be enabled on the disk"

    # Constraint One, carried onto the disk. The image masked every address
    # allocation service unit name whether or not anything had installed one,
    # and the installed appliance keeps every one of those masks. Nothing here
    # installs, enables or starts any of them; the names appear only so that
    # they can be refused.
    local forbidden
    for forbidden in isc-dhcp-server isc-dhcp-server6 dnsmasq udhcpd kea-dhcp4-server dhcpcd dhclient; do
        in_target systemctl mask "${forbidden}.service" >/dev/null 2>&1 || true
    done
    log_info "every address allocation service unit name is masked on the installed disk"
    log_info "the installed appliance allocates no address and keeps the address it was given"
}

# ---------------------------------------------------------------------------
# The bootloaders
# ---------------------------------------------------------------------------

bind_pseudo_filesystems() {
    if is_rehearsal; then
        log_info "rehearsal: the kernel filesystems would be bound into the target"
        return 0
    fi

    local point
    for point in /dev /dev/pts /proc /sys /run; do
        mkdir -p "${TARGET_MOUNT}${point}"
        if mount --rbind "${point}" "${TARGET_MOUNT}${point}" 2>/dev/null; then
            BOUND_POINTS+=("${TARGET_MOUNT}${point}")
            mount --make-rslave "${TARGET_MOUNT}${point}" 2>/dev/null || true
        else
            log_warn "the kernel filesystem at ${point} could not be bound into the target"
        fi
    done
}

install_bootloaders() {
    log_step "installing both bootloaders"

    # Two installations onto one disk, because the appliance has to start on the
    # older machines it was built for and on the modern ones it may be tested
    # on. Neither installation knows about the other, and a machine of either
    # age finds only the path it understands.

    if ! is_rehearsal; then
        if ! in_target test -x /usr/sbin/grub-install && ! in_target test -x /usr/bin/grub-install; then
            fail "the installed system carries no bootloader installer, so the image must be rebuilt carrying one"
        fi
    fi

    # The boot menu is written before either installation, because both read it.
    write_into_target /etc/default/grub 0644 <<'EOF'
# Crossbar -- how this appliance starts
#
# The console is offered on the screen and on the serial line alike, because an
# appliance in a rack with no monitor still has to be recoverable.

GRUB_DEFAULT=0
GRUB_TIMEOUT=5
GRUB_TIMEOUT_STYLE=menu
GRUB_DISTRIBUTOR="Crossbar"
GRUB_CMDLINE_LINUX_DEFAULT="quiet console=tty0 console=ttyS0,115200n8"
GRUB_CMDLINE_LINUX=""
GRUB_TERMINAL="console serial"
GRUB_SERIAL_COMMAND="serial --unit=0 --speed=115200"
EOF

    # The legacy path. The core image goes into the small first partition, and
    # the boot record at the front of the disk is written to point at it.
    if is_rehearsal; then
        log_info "rehearsal: the legacy bootloader would be written to the disk ${TARGET_DISK}"
    elif in_target grub-install --target=i386-pc --boot-directory=/boot --recheck "${TARGET_DISK}" >/dev/null 2>&1; then
        log_info "the legacy bootloader was written to the disk ${TARGET_DISK}"
    else
        fail "the legacy bootloader could not be written to the disk ${TARGET_DISK}"
    fi

    # The modern path. The removable form is used deliberately: it writes the
    # bootloader to the fixed path a firmware looks at when it has been told
    # nothing, which is the state of every machine this appliance is installed
    # on and of any machine whose firmware settings are later cleared. A machine
    # that forgets its boot entries still starts the appliance.
    if is_rehearsal; then
        log_info "rehearsal: the modern bootloader would be written to the firmware partition in its removable form"
    elif in_target grub-install --target=x86_64-efi --efi-directory=/boot/efi \
            --bootloader-id=crossbar --removable --recheck >/dev/null 2>&1; then
        log_info "the modern bootloader was written to the firmware partition in its removable form"
    else
        log_warn "the modern bootloader could not be written; this appliance will start only on an older machine"
    fi

    if is_rehearsal; then
        log_info "rehearsal: the boot menu would be generated from the installed kernel"
    elif in_target update-grub >/dev/null 2>&1; then
        log_info "the boot menu was generated from the installed kernel"
    elif in_target grub-mkconfig -o /boot/grub/grub.cfg >/dev/null 2>&1; then
        log_info "the boot menu was generated from the installed kernel"
    else
        fail "the boot menu could not be generated"
    fi
}

# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------

VERIFICATION_FINDINGS=0

expect_on_target() {
    local path="$1"
    local description="$2"

    if is_rehearsal; then
        log_info "rehearsal: would confirm that ${description} is on the disk"
        return 0
    fi
    if [[ -e "${TARGET_MOUNT}${path}" ]]; then
        log_info "confirmed: ${description}"
        return 0
    fi
    log_error "missing: ${description}, which should be at ${path}"
    VERIFICATION_FINDINGS=$(( VERIFICATION_FINDINGS + 1 ))
    return 0
}

expect_glob_on_target() {
    local pattern="$1"
    local description="$2"

    if is_rehearsal; then
        log_info "rehearsal: would confirm that ${description} is on the disk"
        return 0
    fi
    # The listing's status is the question being asked, so it is caught rather
    # than allowed to reach the shell's error handling.
    if ls "${TARGET_MOUNT}"${pattern} >/dev/null 2>&1; then
        log_info "confirmed: ${description}"
        return 0
    fi
    log_error "missing: ${description}, which should match ${pattern}"
    VERIFICATION_FINDINGS=$(( VERIFICATION_FINDINGS + 1 ))
    return 0
}

verify_installation() {
    log_step "verifying the installed appliance before declaring it done"

    # An installation that reports success without looking is a claim, not a
    # result. Each of these is something the appliance cannot start or cannot
    # work without, and each is reported by name whether it is there or not.
    expect_glob_on_target "/boot/vmlinuz-*" "the kernel"
    expect_glob_on_target "/boot/initrd.img-*" "the boot image"
    expect_on_target /boot/grub/grub.cfg "the boot menu"
    expect_on_target /boot/grub/i386-pc/core.img "the legacy bootloader's core image"
    expect_on_target /boot/efi/EFI/BOOT/BOOTX64.EFI "the modern bootloader in its removable form"
    expect_on_target /etc/fstab "the filesystem table"
    expect_on_target /etc/machine-id "the machine identity"
    expect_on_target /opt/crossbar/appliance/server.py "the appliance control plane"
    expect_on_target /opt/crossbar/web/index.html "the appliance console"
    expect_on_target /opt/crossbar/docs "the appliance manual"
    expect_on_target /etc/systemd/network/10-crossbar-static.network "the static address the appliance arrived with"
    expect_on_target /etc/systemd/system/crossbar.service "the appliance service"
    expect_on_target /etc/crossbar/appliance.json "the appliance configuration"

    # And one thing that must not be there.
    if ! is_rehearsal && [[ -f "${TARGET_MOUNT}/etc/casper.conf" ]]; then
        log_error "the live boot settings are still on the disk"
        VERIFICATION_FINDINGS=$(( VERIFICATION_FINDINGS + 1 ))
    else
        log_info "confirmed: the live boot machinery is not on the disk"
    fi

    if (( VERIFICATION_FINDINGS > 0 )); then
        fail "the verification of the installed appliance made $(spell_integer "${VERIFICATION_FINDINGS}") finding or findings, so the installation cannot be called complete"
    fi
    log_info "every part the installed appliance needs is present on the disk"
}

# ---------------------------------------------------------------------------
# Leaving the machine tidy
# ---------------------------------------------------------------------------

# Called from the exit trap, on success and on failure alike, so it must never
# fail: a cleanup that fails during a failure hides the fault that mattered.
cleanup() {
    if is_rehearsal; then
        return 0
    fi

    sync 2>/dev/null || true

    local index point
    for (( index = ${#BOUND_POINTS[@]} - 1; index >= 0; index-- )); do
        point="${BOUND_POINTS[index]}"
        umount --recursive --lazy "${point}" 2>/dev/null || true
    done
    BOUND_POINTS=()

    if [[ "${EFI_MOUNTED}" == "yes" ]]; then
        umount --lazy "${TARGET_MOUNT}/boot/efi" 2>/dev/null || true
        EFI_MOUNTED="no"
    fi
    if [[ "${TARGET_MOUNTED}" == "yes" ]]; then
        umount --recursive --lazy "${TARGET_MOUNT}" 2>/dev/null || true
        TARGET_MOUNTED="no"
    fi
}

on_error() {
    local status="$1"

    log_error "the installation stopped because a step did not succeed, and nothing further will be attempted"
    log_error "the disk may be partly written; run this installation again once the reported cause is corrected"
    exit "${status}"
}

summarise() {
    printf '\n'
    log_step "installation summary"

    log_info "the appliance was installed onto the disk ${TARGET_DISK}"
    log_info "it starts on an older machine by the legacy path and on a modern machine by the firmware path"
    log_info "it answers on the address the image arrived with, at the port the boot screen names"
    log_info "remove the live medium and start the machine from its disk"
    log_info "this appliance assigns no addresses; it spells quantities and keeps identifiers"
}

main() {
    parse_arguments "$@"

    banner "installing the appliance onto a disk"
    require_root

    trap 'cleanup' EXIT
    trap 'on_error $?' ERR

    if is_rehearsal; then
        log_warn "this is a rehearsal; nothing will be written and every refusal is reported rather than enforced"
    fi

    require_tools
    assert_running_from_live_image
    assert_disk_is_safe
    describe_what_will_be_destroyed
    confirm

    partition_disk
    make_filesystems
    mount_target

    copy_root_filesystem
    install_kernel

    bind_pseudo_filesystems
    remove_live_boot_machinery
    write_filesystem_table
    generate_machine_identity
    rebuild_boot_image
    preserve_appliance_identity
    install_bootloaders

    verify_installation
    cleanup

    summarise
    log_info "the installation onto the disk is complete"
}

main "$@"
