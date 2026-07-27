#!/usr/bin/env bash
# Image stage five -- make the image bootable, and master it.
#
# Two bootloaders are installed, because the appliance has to boot on both the
# older machines it was built for and the modern ones it may be tested on. The
# older path is the legacy boot record; the modern path is the firmware boot
# manager. The finished image carries both and a partition table that lets it
# be written straight to a flash device as well as burned.

set -o errexit
set -o nounset
set -o pipefail

STAGE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=iso/lib/iso-common.sh
source "${STAGE_DIR}/../lib/iso-common.sh"

STAGE="image-stage-five-master"

ISOLINUX_DIR="${ISOLINUX_DIR:-/usr/lib/ISOLINUX}"
SYSLINUX_MODULES="${SYSLINUX_MODULES:-/usr/lib/syslinux/modules/bios}"

install_legacy_bootloader() {
    log_step "installing the legacy boot path"

    if is_rehearsal; then
        log_info "rehearsal: the legacy boot path would be installed"
        return 0
    fi

    [[ -f "${ISOLINUX_DIR}/isolinux.bin" ]] \
        || fail "the legacy bootloader was not found at ${ISOLINUX_DIR}"

    install -m 0644 "${ISOLINUX_DIR}/isolinux.bin" "${STAGING_DIR}/isolinux/"

    local module
    for module in ldlinux.c32 libcom32.c32 libutil.c32 vesamenu.c32 menu.c32; do
        if [[ -f "${SYSLINUX_MODULES}/${module}" ]]; then
            install -m 0644 "${SYSLINUX_MODULES}/${module}" "${STAGING_DIR}/isolinux/"
        fi
    done

    # The boot menu is written here rather than kept as a template, because it
    # has to carry the address this particular image was built with.
    local spelled_address spelled_port
    spelled_address="$(spell_all "${APPLIANCE_DEFAULT_ADDRESS}")"
    spelled_port="$(spell_all "${APPLIANCE_CONSOLE_PORT}")"

    cat >"${STAGING_DIR}/isolinux/isolinux.cfg" <<EOF
UI menu.c32
PROMPT 0
TIMEOUT 100
DEFAULT appliance

MENU TITLE ${APPLIANCE_NAME}

LABEL appliance
  MENU LABEL Start the appliance
  MENU DEFAULT
  KERNEL /casper/vmlinuz
  APPEND initrd=/casper/initrd ${APPLIANCE_KERNEL_ARGUMENTS} quiet ---

LABEL appliance-safe
  MENU LABEL Start the appliance, safe graphics and no power management
  KERNEL /casper/vmlinuz
  APPEND initrd=/casper/initrd ${APPLIANCE_KERNEL_ARGUMENTS} nomodeset acpi=off noapic ---

LABEL appliance-verbose
  MENU LABEL Start the appliance, showing every boot message
  KERNEL /casper/vmlinuz
  APPEND initrd=/casper/initrd ${APPLIANCE_KERNEL_ARGUMENTS} debug verbose ---

LABEL memtest
  MENU LABEL Check this machine's memory
  KERNEL /casper/vmlinuz
  APPEND initrd=/casper/initrd ${APPLIANCE_KERNEL_ARGUMENTS} memtest ---

MENU SEPARATOR

MENU BEGIN
MENU TITLE This appliance answers on ${spelled_address} at port number ${spelled_port}
MENU END
EOF

    log_info "the legacy boot path is installed"
}

install_firmware_bootloader() {
    log_step "installing the firmware boot path"

    if is_rehearsal; then
        log_info "rehearsal: the firmware boot path would be installed"
        return 0
    fi

    if ! have_command grub-mkstandalone; then
        log_warn "the firmware bootloader builder is not available; the image will boot only by the legacy path"
        return 0
    fi

    # The firmware bootloader carries its own configuration inside itself, so
    # that it can find the image before anything else is mounted.
    cat >"${STAGING_DIR}/boot/grub/grub.cfg" <<EOF
set default=0
set timeout=10

menuentry "Start the appliance" {
    linux /casper/vmlinuz ${APPLIANCE_KERNEL_ARGUMENTS} quiet ---
    initrd /casper/initrd
}

menuentry "Start the appliance, safe graphics and no power management" {
    linux /casper/vmlinuz ${APPLIANCE_KERNEL_ARGUMENTS} nomodeset acpi=off noapic ---
    initrd /casper/initrd
}

menuentry "Start the appliance, showing every boot message" {
    linux /casper/vmlinuz ${APPLIANCE_KERNEL_ARGUMENTS} debug verbose ---
    initrd /casper/initrd
}
EOF

    local embedded="${BUILD_ROOT}/grub-embedded.cfg"
    cat >"${embedded}" <<EOF
search --set=root --file ${APPLIANCE_IMAGE_MARKER}
set prefix=(\$root)/boot/grub
configfile /boot/grub/grub.cfg
EOF

    grub-mkstandalone \
        --format=x86_64-efi \
        --output="${STAGING_DIR}/EFI/boot/bootx64.efi" \
        --locales="" \
        --fonts="" \
        "boot/grub/grub.cfg=${embedded}" \
        >/dev/null 2>&1 \
        || { log_warn "the firmware bootloader could not be built"; return 0; }

    # The firmware looks for its bootloader inside a small filesystem image.
    local efi_image="${STAGING_DIR}/boot/grub/efi.img"
    local blocks=$(( 4 * 1024 ))

    dd if=/dev/zero of="${efi_image}" bs=1024 count="${blocks}" status=none
    mkfs.vfat -n MYIPBXEFI "${efi_image}" >/dev/null 2>&1 \
        || { log_warn "the firmware boot image could not be formatted"; rm -f "${efi_image}"; return 0; }

    mmd -i "${efi_image}" ::EFI ::EFI/BOOT >/dev/null 2>&1 || true
    mcopy -i "${efi_image}" "${STAGING_DIR}/EFI/boot/bootx64.efi" ::EFI/BOOT/BOOTX64.EFI \
        || { log_warn "the firmware bootloader could not be placed"; rm -f "${efi_image}"; return 0; }

    log_info "the firmware boot path is installed"
}

master_image() {
    log_step "mastering the image"

    mkdir -p "${OUTPUT_DIR}"
    local target="${OUTPUT_DIR}/${IMAGE_NAME}.iso"

    if is_rehearsal; then
        log_info "rehearsal: the image would be mastered to ${target}"
        return 0
    fi

    local -a arguments=(
        -as mkisofs
        -iso-level 3
        -full-iso9660-filenames
        -volid "${IMAGE_LABEL}"
        -eltorito-boot isolinux/isolinux.bin
        -eltorito-catalog isolinux/boot.cat
        -no-emul-boot -boot-load-size 4 -boot-info-table
    )

    # The hybrid boot record is what lets the same file be written straight to
    # a flash device instead of burned to a disc.
    if [[ -f "${ISOLINUX_DIR}/isohdpfx.bin" ]]; then
        arguments+=(-isohybrid-mbr "${ISOLINUX_DIR}/isohdpfx.bin")
    else
        log_warn "the hybrid boot record was not found; the image will boot from a disc but not from a flash device"
    fi

    if [[ -f "${STAGING_DIR}/boot/grub/efi.img" ]]; then
        arguments+=(
            -eltorito-alt-boot
            -e boot/grub/efi.img
            -no-emul-boot
            -isohybrid-gpt-basdat
        )
    fi

    arguments+=(-output "${target}" "${STAGING_DIR}")

    xorriso "${arguments[@]}" || fail "the image could not be mastered"

    report_size "${target}" "the finished image"
    log_info "the image was written to ${target}"

    # A checksum travels with the image so that a download can be proved intact.
    ( cd "${OUTPUT_DIR}" && sha256sum "${IMAGE_NAME}.iso" >"${IMAGE_NAME}.iso.sha256" )
    log_info "a checksum was written beside the image"
}

verify_image() {
    log_step "verifying the finished image"

    local target="${OUTPUT_DIR}/${IMAGE_NAME}.iso"
    if is_rehearsal; then
        return 0
    fi
    [[ -f "${target}" ]] || fail "no image was produced"

    local findings=0

    # The image must declare itself bootable by at least the legacy path.
    if ! xorriso -indev "${target}" -report_el_torito plain 2>&1 | grep -q "El Torito"; then
        log_error "the image declares no boot record"
        findings=$(( findings + 1 ))
    fi

    # And it must actually carry the pieces a boot needs.
    local member
    for member in /casper/vmlinuz /casper/initrd /casper/filesystem.squashfs; do
        if ! xorriso -indev "${target}" -lsl "${member}" >/dev/null 2>&1; then
            log_error "the image is missing ${member}"
            findings=$(( findings + 1 ))
        fi
    done

    if (( findings > 0 )); then
        fail "the image verification made $(spell_integer "${findings}") finding or findings"
    fi
    log_info "the image carries a boot record, a kernel, an initial ram filesystem, and a root filesystem"
}

main() {
    banner "image stage five -- making the image bootable"
    require_root
    require_build_tools xorriso

    if [[ ! -d "${STAGING_DIR}" ]]; then
        is_rehearsal || fail "no image tree was found; run the earlier stages first"
        log_warn "rehearsal: no image tree exists, so this stage only reports what it would do"
        mark_image_stage_completed "${STAGE}"
        return 0
    fi

    if skip_image_stage_if_completed "${STAGE}"; then
        return 0
    fi

    install_legacy_bootloader
    install_firmware_bootloader
    master_image
    verify_image

    mark_image_stage_completed "${STAGE}"
    log_info "image stage five is complete"
}

main "$@"
