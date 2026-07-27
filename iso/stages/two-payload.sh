#!/usr/bin/env bash
# Image stage two -- the payload.
#
# Installs the kernel, the live boot machinery, the telephony engine, the
# legacy interface card drivers, and the appliance control plane itself.
#
# The interface card drivers are built into the image against the kernel the
# image ships, so a card works on first boot with nothing to compile. The
# driver source and the kernel headers stay in the image as well, so that the
# appliance can rebuild them from its own console after a kernel upgrade --
# which is the case the released driver archives no longer cover.

set -o errexit
set -o nounset
set -o pipefail

STAGE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=iso/lib/iso-common.sh
source "${STAGE_DIR}/../lib/iso-common.sh"

STAGE="image-stage-two-payload"

install_kernel_and_live_boot() {
    log_step "installing the kernel and the live boot machinery"

    if is_rehearsal; then
        log_info "rehearsal: the kernel and live boot machinery would be installed"
        return 0
    fi

    # The generic kernel carries the widest driver set, which is what an
    # appliance meant for unknown older hardware needs. A smaller virtual
    # kernel would boot faster and support less.
    install_in_chroot \
        linux-image-generic \
        linux-headers-generic \
        initramfs-tools \
        casper \
        || fail "the kernel or the live boot machinery could not be installed"

    freeze_boot_image

    log_info "the kernel and the live boot machinery are installed"
}

freeze_boot_image() {
    # From here on, no package may regenerate the boot image.
    #
    # The boot image that ships is the one the kernel package just produced,
    # and it is the one this build proves boots by starting the finished image
    # and watching it reach a login prompt.  A regenerated boot image was tried
    # and could not create the writable layer over its own read only root: the
    # boot stopped at a rescue shell reporting that it found no support for its
    # layering format, with the required modules demonstrably present in the
    # archive.
    #
    # The danger is that regeneration is not something this build asks for.
    # Several ordinary packages install a trigger that rebuilds the boot image
    # as a side effect of being installed, so every package added to the image
    # after this point is a chance to silently replace a boot image that works
    # with one that does not.  Turning the trigger off closes that whole class
    # of accident rather than the one instance of it that was noticed.
    #
    # The setting is deliberately left in place in the shipped image.  An
    # appliance installed to a fixed disk needs a boot image built for that
    # disk, so the installer turns this back on before it regenerates one.
    write_into_chroot /etc/initramfs-tools/update-initramfs.conf 0644 <<'EOF'
# The boot image this appliance ships with is the one proved to boot, and no
# package installation may replace it as a side effect. The disk installer
# turns this back on when it builds a boot image for an installed system.
update_initramfs=no
EOF

    log_info "the boot image is frozen; no package may replace it as a side effect"
}

install_telephony_engine() {
    log_step "installing the telephony engine"

    if is_rehearsal; then
        log_info "rehearsal: the telephony engine would be installed"
        return 0
    fi

    if ! install_in_chroot asterisk asterisk-modules asterisk-config; then
        log_warn "the full engine package set was not available; attempting the engine alone"
        install_in_chroot asterisk || fail "the telephony engine could not be installed"
    fi

    local version
    version="$(in_chroot dpkg-query -W -f='${Version}' asterisk 2>/dev/null || printf 'unknown')"
    log_info "the telephony engine installed into the image is version ${version}"
}

install_interface_drivers() {
    log_step "installing the legacy interface card drivers"

    if is_rehearsal; then
        log_info "rehearsal: the interface card drivers would be built into the image"
        return 0
    fi

    # The driver source plus the module build system, so the modules can be
    # rebuilt from the appliance console against any later kernel.
    if ! install_in_chroot dahdi-source dahdi dahdi-dkms dkms build-essential; then
        log_warn "the complete driver package set was not available"
        install_in_chroot dkms build-essential || true
        install_in_chroot dahdi-source || log_warn "the driver source package was not available"
        install_in_chroot dahdi || log_warn "the driver tools package was not available"
    fi

    # Build the modules against the kernel the image actually ships, so a card
    # works on first boot with nothing to compile.
    local release
    release="$(in_chroot bash -c 'ls /lib/modules | head -n 1' 2>/dev/null || printf '')"

    if [[ -z "${release}" ]]; then
        log_warn "no kernel module tree was found in the image; the drivers cannot be prebuilt"
        return 0
    fi
    log_info "building the interface card drivers against the kernel release ${release}"

    if in_chroot bash -c "dkms autoinstall -k ${release}" 2>/dev/null; then
        log_info "the interface card drivers were built into the image"
    else
        log_warn "the drivers could not be prebuilt in this environment"
        log_warn "the image still carries the source and the build system, so the appliance can build them from its console on first boot"
    fi

    # Persist the module load configuration either way.
    write_into_chroot /etc/modules-load.d/myipbx-dahdi.conf 0644 <<'EOF'
# Legacy-to-Modern IPBX Appliance -- interface driver modules loaded at start up
dahdi
EOF
}

install_supplementary_services() {
    log_step "installing the supplementary services"

    if is_rehearsal; then
        log_info "rehearsal: the supplementary services would be installed"
        return 0
    fi

    # Everything the appliance's own console offers must be present, or the
    # console would offer an operation the image cannot perform.
    install_in_chroot \
        nftables \
        chrony \
        logrotate \
        rsyslog \
        pciutils usbutils \
        ethtool \
        curl \
        git \
        console-setup \
        || log_warn "one or more supplementary services could not be installed"

    # What the appliance needs in order to install itself onto a fixed disk.
    # The image can be run from the medium indefinitely, but an appliance in a
    # rack should be running from its own disk, and it can only put itself
    # there if it is carrying the tools to do so before it leaves the factory.
    #
    # Both bootloader flavours are carried deliberately.  The installer writes
    # both a legacy boot record and a firmware one, because the machine it will
    # be installed on is not known at the time this image is built.
    install_in_chroot \
        squashfs-tools \
        rsync \
        dosfstools \
        gdisk parted \
        grub-common grub-pc-bin grub-efi-amd64-bin grub2-common \
        || log_warn "one or more of the disk installation tools could not be installed"
}

install_control_plane() {
    log_step "installing the appliance control plane"

    if is_rehearsal; then
        log_info "rehearsal: the control plane would be installed into the image"
        return 0
    fi

    local prefix="${CHROOT_DIR}/opt/myipbx"
    mkdir -p "${prefix}/appliance" "${prefix}/web/js" "${prefix}/web/css" "${prefix}/bin/lib"

    install -m 0644 "${REPOSITORY_ROOT}"/appliance/*.py "${prefix}/appliance/"
    install -m 0644 "${REPOSITORY_ROOT}"/web/index.html "${prefix}/web/"
    install -m 0644 "${REPOSITORY_ROOT}"/web/js/*.js "${prefix}/web/js/"
    install -m 0644 "${REPOSITORY_ROOT}"/web/css/*.css "${prefix}/web/css/"

    # The privileged helper and the staging scripts it delegates to.
    install -m 0755 "${REPOSITORY_ROOT}/scripts/myipbx-privileged-helper.sh" "${prefix}/bin/"
    install -m 0644 "${REPOSITORY_ROOT}/scripts/lib/common.sh" "${prefix}/bin/lib/"
    local script
    for script in stage-two-network-static.sh stage-three-dahdi-drivers.sh verify-no-dhcp.sh \
                  myipbx-generate-certificate.sh; do
        install -m 0755 "${REPOSITORY_ROOT}/scripts/${script}" "${prefix}/bin/"
    done

    # The documentation travels with the appliance, because an appliance whose
    # manual is somewhere else is an appliance without a manual.
    mkdir -p "${prefix}/docs"
    install -m 0644 "${REPOSITORY_ROOT}"/docs/*.md "${prefix}/docs/"
    install -m 0644 "${REPOSITORY_ROOT}/README.md" "${prefix}/docs/"

    # The control plane's own unit, and the unit for the daemon that holds the
    # privilege the control plane deliberately does not have.
    #
    # The service account is granted nothing.  An earlier design gave it a sudo
    # rule naming the helper script, which could never have worked: the control
    # plane's unit sets NoNewPrivileges, and sudo refuses to run under that
    # flag.  No privilege grant is installed here, and any left behind by an
    # older image is removed.
    #
    # No certificate is installed here, only the unit that generates one. A
    # certificate inside this image would be the same certificate, and the same
    # private key, on every appliance anybody ever booted from it.
    local unit
    for unit in myipbx.service myipbx-helperd.service myipbx-certificate.service; do
        install -m 0644 "${REPOSITORY_ROOT}/config/systemd/${unit}" \
            "${CHROOT_DIR}/etc/systemd/system/${unit}"
    done
    rm -f "${CHROOT_DIR}/etc/sudoers.d/myipbx"

    # The appliance installs itself onto a fixed disk from here, so the script
    # that does it travels inside the image rather than beside it.
    install -m 0755 "${REPOSITORY_ROOT}/iso/installer/myipbx-install-to-disk.sh" \
        "${prefix}/bin/myipbx-install-to-disk.sh"
    ln -sf "${prefix#"${CHROOT_DIR}"}/bin/myipbx-install-to-disk.sh" \
        "${CHROOT_DIR}/usr/local/sbin/myipbx-install-to-disk" 2>/dev/null || true

    log_info "the control plane was installed into the image"
}

verify_payload() {
    log_step "verifying the payload"

    if is_rehearsal; then
        return 0
    fi

    local failures=0

    if ! in_chroot test -x /usr/sbin/asterisk && ! in_chroot test -x /usr/bin/asterisk; then
        log_error "the telephony engine is not present in the image"
        failures=$(( failures + 1 ))
    fi
    if ! in_chroot test -f /opt/myipbx/appliance/server.py; then
        log_error "the control plane is not present in the image"
        failures=$(( failures + 1 ))
    fi
    if ! in_chroot test -f /opt/myipbx/web/index.html; then
        log_error "the console is not present in the image"
        failures=$(( failures + 1 ))
    fi
    if ! in_chroot bash -c 'ls /boot/vmlinuz-* >/dev/null 2>&1'; then
        log_error "no kernel is present in the image"
        failures=$(( failures + 1 ))
    fi

    # The control plane must import cleanly with the interpreter in the image,
    # which is the only way to know the two are compatible before boot.
    if ! in_chroot python3 -c 'import sys; sys.path.insert(0, "/opt/myipbx"); import appliance.server' 2>/dev/null; then
        log_error "the control plane does not import with the interpreter in the image"
        failures=$(( failures + 1 ))
    fi

    if (( failures > 0 )); then
        fail "the payload verification made $(spell_integer "${failures}") finding or findings"
    fi
    log_info "the payload is complete and the control plane imports inside the image"
}

main() {
    banner "image stage two -- the payload"
    require_root

    if [[ ! -d "${CHROOT_DIR}" ]]; then
        # A rehearsal never created one, and that is not a failure.
        is_rehearsal || fail "no base root filesystem was found; run image stage one first"
        log_warn "rehearsal: no root filesystem exists, so this stage only reports what it would do"
    fi

    if skip_image_stage_if_completed "${STAGE}"; then
        return 0
    fi

    trap 'unmount_chroot' EXIT

    install_kernel_and_live_boot
    install_telephony_engine
    install_interface_drivers
    install_supplementary_services
    install_control_plane
    verify_payload

    report_size "${CHROOT_DIR}" "the root filesystem with its payload"
    mark_image_stage_completed "${STAGE}"
    log_info "image stage two is complete"
}

main "$@"
