#!/usr/bin/env bash
# Image stage four -- compress the root filesystem and lay out the image tree.
#
# The root filesystem becomes one compressed file that the live boot machinery
# mounts read only. The kernel and its initial ram filesystem are lifted out
# beside it, because the bootloader has to reach them before anything is
# mounted.

set -o errexit
set -o nounset
set -o pipefail

STAGE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=iso/lib/iso-common.sh
source "${STAGE_DIR}/../lib/iso-common.sh"

STAGE="image-stage-four-compress"

# Compression is a real trade for this product. The appliance targets older
# machines, where the processor that decompresses is slow but the optical or
# flash medium it reads from is slower still. A smaller image wins.
SQUASH_COMPRESSION="${SQUASH_COMPRESSION:-xz}"
SQUASH_BLOCK_SIZE="${SQUASH_BLOCK_SIZE:-1048576}"

prepare_tree() {
    log_step "laying out the image tree"

    if is_rehearsal; then
        log_info "rehearsal: the image tree would be laid out at ${STAGING_DIR}"
        return 0
    fi

    rm -rf "${STAGING_DIR}"
    mkdir -p "${STAGING_DIR}/casper" \
             "${STAGING_DIR}/isolinux" \
             "${STAGING_DIR}/boot/grub" \
             "${STAGING_DIR}/EFI/boot" \
             "${STAGING_DIR}/.disk"

    # The marker the firmware bootloader searches for, and the only claim this
    # image makes about what it is.
    #
    # A distribution would name this file so that the live boot machinery reads
    # it and treats the disc as a package disc: it would then derive the host
    # name from the first word of the description, and try to register the disc
    # as a source of packages.  Neither is true here.  This image carries an
    # appliance, not a package archive, so the description goes in a file of the
    # appliance's own naming and the disc makes no claim it cannot honour.
    printf '%s\n' "${APPLIANCE_NAME}" >"${STAGING_DIR}${APPLIANCE_IMAGE_MARKER}"
    printf 'full_cd/single\n' >"${STAGING_DIR}/.disk/cd_type"
}

extract_kernel() {
    log_step "lifting the kernel and its initial ram filesystem out of the image"

    if is_rehearsal; then
        log_info "rehearsal: the kernel would be lifted out"
        return 0
    fi

    local kernel initrd
    kernel="$(find "${CHROOT_DIR}/boot" -name 'vmlinuz-*' -type f | sort | tail -n 1)"
    initrd="$(find "${CHROOT_DIR}/boot" -name 'initrd.img-*' -type f | sort | tail -n 1)"

    [[ -n "${kernel}" ]] || fail "no kernel was found in the image"
    if [[ -z "${initrd}" ]]; then
        log_warn "no initial ram filesystem was found; generating one inside the image"
        local release
        release="$(basename "${kernel}" | sed 's/^vmlinuz-//')"
        in_chroot update-initramfs -c -k "${release}" \
            || fail "an initial ram filesystem could not be generated"
        initrd="$(find "${CHROOT_DIR}/boot" -name 'initrd.img-*' -type f | sort | tail -n 1)"
        [[ -n "${initrd}" ]] || fail "no initial ram filesystem could be produced"
    fi

    install -m 0644 "${kernel}" "${STAGING_DIR}/casper/vmlinuz"
    install -m 0644 "${initrd}" "${STAGING_DIR}/casper/initrd"

    log_info "the kernel taken from the image is $(basename "${kernel}")"
}

compress_root_filesystem() {
    log_step "compressing the root filesystem"

    if is_rehearsal; then
        log_info "rehearsal: the root filesystem would be compressed"
        return 0
    fi

    # The record of what is inside travels with the image, so an operator can
    # see what they have without unpacking anything.
    in_chroot dpkg-query -W --showformat='${Package} ${Version}\n' \
        >"${STAGING_DIR}/casper/filesystem.manifest" 2>/dev/null || true

    du -sx --block-size=1 "${CHROOT_DIR}" 2>/dev/null | cut -f1 \
        >"${STAGING_DIR}/casper/filesystem.size" || true

    # The kernel filesystems must not be inside the compressed copy.
    unmount_chroot

    log_info "compressing with the method named ${SQUASH_COMPRESSION}; this is the slow part"
    mksquashfs "${CHROOT_DIR}" "${STAGING_DIR}/casper/filesystem.squashfs" \
        -comp "${SQUASH_COMPRESSION}" \
        -b "${SQUASH_BLOCK_SIZE}" \
        -noappend \
        -no-progress \
        -e boot/vmlinuz-\* boot/initrd.img-\* \
        || fail "the root filesystem could not be compressed"

    report_size "${STAGING_DIR}/casper/filesystem.squashfs" "the compressed root filesystem"
}

main() {
    banner "image stage four -- compressing the root filesystem"
    require_root
    require_build_tools mksquashfs

    if [[ ! -d "${CHROOT_DIR}" ]]; then
        is_rehearsal || fail "no root filesystem was found; run the earlier stages first"
        log_warn "rehearsal: no root filesystem exists, so this stage only reports what it would do"
    fi

    if skip_image_stage_if_completed "${STAGE}"; then
        return 0
    fi

    prepare_tree
    extract_kernel
    compress_root_filesystem

    mark_image_stage_completed "${STAGE}"
    log_info "image stage four is complete"
}

main "$@"
