#!/usr/bin/env bash
# Image stage one -- bootstrap the base operating system.
#
# Produces a minimal root filesystem for the appliance image. Nothing
# telephony related is installed here; this stage is only concerned with
# producing a base that the later stages can build on, and with making certain
# that the base itself carries no address allocation service.

set -o errexit
set -o nounset
set -o pipefail

STAGE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=iso/lib/iso-common.sh
source "${STAGE_DIR}/../lib/iso-common.sh"

STAGE="image-stage-one-bootstrap"

bootstrap_base() {
    log_step "bootstrapping the base operating system"
    log_info "the base is ${BASE_DISTRIBUTION} at the suite named ${BASE_SUITE}"
    log_info "the architecture is ${BASE_ARCHITECTURE}"

    if is_rehearsal; then
        log_info "rehearsal: the base would be bootstrapped into ${CHROOT_DIR}"
        return 0
    fi

    rm -rf "${CHROOT_DIR}"
    mkdir -p "${CHROOT_DIR}"

    # A variant carrying only the essential set keeps the image small; every
    # further package is named deliberately by a later stage.
    debootstrap \
        --arch="${BASE_ARCHITECTURE}" \
        --components="${BASE_COMPONENTS}" \
        --variant=minbase \
        "${BASE_SUITE}" \
        "${CHROOT_DIR}" \
        "${BASE_MIRROR}" \
        || fail "the base operating system could not be bootstrapped"

    log_info "the base root filesystem was created"
}

configure_package_sources() {
    log_step "configuring the package sources inside the image"

    if is_rehearsal; then
        log_info "rehearsal: the package sources would be written"
        return 0
    fi

    # Name resolution inside the image, for the length of the build only.
    # It is replaced by the appliance's own configuration in stage three.
    if [[ -f /etc/resolv.conf ]]; then
        mkdir -p "${CHROOT_DIR}/etc"
        cp /etc/resolv.conf "${CHROOT_DIR}/etc/resolv.conf"
    fi

    write_into_chroot /etc/apt/sources.list 0644 <<EOF
# ${APPLIANCE_NAME} -- package sources
deb ${BASE_MIRROR} ${BASE_SUITE} main universe
deb ${BASE_MIRROR} ${BASE_SUITE}-updates main universe
deb ${BASE_MIRROR} ${BASE_SUITE}-security main universe
EOF

    # An appliance must never have a package installation ask a question, and
    # must never start a service merely because a package was unpacked.
    write_into_chroot /usr/sbin/policy-rc.d 0755 <<'EOF'
#!/bin/sh
# During the image build nothing may start. The appliance decides what runs.
exit 101
EOF

    write_into_chroot /etc/apt/apt.conf.d/99myipbx 0644 <<'EOF'
APT::Install-Recommends "false";
APT::Install-Suggests "false";
Acquire::Languages "none";
EOF

    in_chroot apt-get update || fail "the package index could not be read inside the image"
}

install_base_system() {
    log_step "installing the base system packages"

    if is_rehearsal; then
        log_info "rehearsal: the base system packages would be installed"
        return 0
    fi

    # Deliberately absent from this list, and from every other list in this
    # build: any address allocation service. The appliance assigns no
    # addresses, so nothing that could is ever placed in the image.
    install_in_chroot \
        systemd systemd-sysv dbus \
        udev kmod \
        iproute2 iputils-ping \
        nftables \
        ca-certificates \
        locales tzdata \
        less nano \
        openssh-server \
        python3 python3-minimal \
        sudo \
        || fail "the base system packages could not be installed"
}

verify_no_allocation_service() {
    log_step "verifying that the base carries no address allocation service"

    if is_rehearsal; then
        log_info "rehearsal: the base would be audited"
        return 0
    fi

    local offender found=0
    for offender in isc-dhcp-server dnsmasq-base kea-dhcp4-server udhcpd; do
        if in_chroot dpkg-query -W -f='${Status}' "${offender}" 2>/dev/null \
            | grep -q "install ok installed"; then
            log_error "the base carries the package named ${offender}"
            found=$(( found + 1 ))
        fi
    done

    if (( found > 0 )); then
        fail "the base image carries an address allocation service, which the specification forbids"
    fi
    log_info "the base carries no address allocation service"
}

main() {
    banner "image stage one -- the base operating system"
    require_root
    require_build_tools debootstrap chroot

    if skip_image_stage_if_completed "${STAGE}"; then
        return 0
    fi

    trap 'unmount_chroot' EXIT

    bootstrap_base
    configure_package_sources
    install_base_system
    verify_no_allocation_service

    report_size "${CHROOT_DIR}" "the base root filesystem"
    mark_image_stage_completed "${STAGE}"
    log_info "image stage one is complete"
}

main "$@"
