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
    # The sentence is spelled, not the values inside it.
    #
    # Spelling each value on its own strips it of the context that makes it an
    # identifier: the port came out as "eight thousand eighty-eight", which is
    # the one thing on this screen a technician has to type into a browser and
    # cannot type from that. In the sentence, "port number 8088" is recognised
    # for what it is and keeps its digits, exactly as it does everywhere else
    # in the appliance. It is also forty characters shorter, which matters
    # because the bootloader truncates this line to the width of its box and
    # was cutting it off at "at port numbe".
    local boot_note
    boot_note="$(spell_all "answers on ${APPLIANCE_DEFAULT_ADDRESS} at port number ${APPLIANCE_CONSOLE_PORT}")"

    # The menu is drawn inside a box, and the box is narrower than the screen.
    # Its width is the menu width less twice the margin, and an entry is four
    # narrower still; at the shipped defaults -- eighty wide, ten of margin --
    # an entry gets fifty-six characters and a nested title fifty-four, which
    # was cutting "no power management" off at "manageme" and taking the port
    # off the end of the note. A margin of two gives an entry seventy-two and a
    # nested title seventy, which every line here fits inside with room left.
    cat >"${STAGING_DIR}/isolinux/isolinux.cfg" <<EOF
UI menu.c32
PROMPT 0
TIMEOUT 100
DEFAULT appliance

MENU WIDTH ${ISOLINUX_MENU_WIDTH}
MENU MARGIN ${ISOLINUX_MENU_MARGIN}
MENU TITLE ${APPLIANCE_NAME} ${APPLIANCE_MAKER}

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
MENU TITLE The console ${boot_note}
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

    # The menu, written once and used twice: it is embedded inside the
    # bootloader itself, which is what actually runs, and left on the image
    # beside it so that a technician can read what the machine will do.
    local menu
    menu="$(
        cat <<EOF
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
    )"
    printf '%s\n' "${menu}" >"${STAGING_DIR}/boot/grub/grub.cfg"

    # What the firmware bootloader carries inside itself.
    #
    # It finds the image by the marker file and then reads the menu it is
    # already holding. Two things it must not do, both learned by photographing
    # a machine that would not start:
    #
    # It must not set its own prefix to a directory on the image. A standalone
    # bootloader keeps its modules in a memory disk inside itself, and the
    # prefix is how it finds them; pointing the prefix at the image sends it
    # looking for those modules in a directory that has never contained any,
    # and the first one it needs -- configfile -- is reported missing and it
    # stops at a bare prompt. Nothing on the screen says what went wrong.
    #
    # And it must not reach for the menu on the image with configfile. The menu
    # is embedded here, so it is read from memory and cannot be missing, cannot
    # be unreadable, and does not depend on the filesystem driver for the image
    # having loaded first.
    local embedded="${BUILD_ROOT}/grub-embedded.cfg"
    {
        printf 'search --no-floppy --set=root --file %s\n\n' "${APPLIANCE_IMAGE_MARKER}"
        printf '%s\n' "${menu}"
    } >"${embedded}"

    # Named rather than left to the default, so that a change in the builder's
    # idea of a sensible default cannot quietly remove something the boot needs.
    local modules="search search_fs_file search_label part_gpt part_msdos"
    modules="${modules} fat iso9660 udf ext2 normal linux echo test configfile"
    modules="${modules} all_video video gfxterm gfxterm_background loadenv"
    modules="${modules} minicmd reboot halt sleep"

    local builder_output="${BUILD_ROOT}/grub-mkstandalone.log"
    if ! grub-mkstandalone \
        --format=x86_64-efi \
        --output="${STAGING_DIR}/EFI/boot/bootx64.efi" \
        --locales="" \
        --fonts="" \
        --modules="${modules}" \
        "boot/grub/grub.cfg=${embedded}" \
        >"${builder_output}" 2>&1
    then
        log_error "the firmware bootloader could not be built, and an image without one does not start on a machine that boots by firmware, which is every machine sold for years; the builder reported:"
        while IFS= read -r builder_line; do
            log_error "  ${builder_line}"
        done <"${builder_output}"
        fail "the firmware boot path could not be installed"
    fi

    # The firmware looks for its bootloader inside a small filesystem image.
    local efi_image="${STAGING_DIR}/boot/grub/efi.img"
    local blocks=$(( 4 * 1024 ))

    dd if=/dev/zero of="${efi_image}" bs=1024 count="${blocks}" status=none
    mkfs.vfat -n CROSSBAREFI "${efi_image}" >/dev/null 2>&1 \
        || fail "the firmware boot image could not be formatted, and without it the image starts only on a machine old enough to boot by the legacy path"

    mmd -i "${efi_image}" ::EFI ::EFI/BOOT >/dev/null 2>&1 || true
    mcopy -i "${efi_image}" "${STAGING_DIR}/EFI/boot/bootx64.efi" ::EFI/BOOT/BOOTX64.EFI \
        || fail "the firmware bootloader could not be placed inside the firmware boot image"

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
