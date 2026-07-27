#!/usr/bin/env bash
# Image stage three -- configure the appliance inside the image.
#
# Turns a root filesystem that merely contains the right software into an
# appliance: a service account, a static address, the services that should run
# and the ones that should not, and a boot screen that tells the operator where
# to point a browser.
#
# The static address matters more than it looks. This appliance never requests
# an address and never offers one, so it has to arrive already reachable or
# nobody could ever open its console the first time.

set -o errexit
set -o nounset
set -o pipefail

STAGE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=iso/lib/iso-common.sh
source "${STAGE_DIR}/../lib/iso-common.sh"

STAGE="image-stage-three-configure"

configure_identity() {
    log_step "configuring the appliance identity"

    if is_rehearsal; then
        return 0
    fi

    write_into_chroot /etc/hostname 0644 <<<"${APPLIANCE_HOST_NAME}"
    write_into_chroot /etc/hosts 0644 <<EOF
127.0.0.1   localhost
127.0.1.1   ${APPLIANCE_HOST_NAME}
::1         localhost ip6-localhost ip6-loopback
EOF

    in_chroot bash -c 'echo "en_US.UTF-8 UTF-8" > /etc/locale.gen' || true
    in_chroot locale-gen en_US.UTF-8 >/dev/null 2>&1 || true
    write_into_chroot /etc/default/locale 0644 <<'EOF'
LANG=en_US.UTF-8
EOF
}

configure_service_account() {
    log_step "creating the unprivileged service account"

    if is_rehearsal; then
        return 0
    fi

    in_chroot bash -c '
        id -u myipbx >/dev/null 2>&1 || \
            useradd --system --home-dir /opt/myipbx --shell /usr/sbin/nologin \
                    --comment "appliance control plane" myipbx
        mkdir -p /var/lib/myipbx/receipts /var/lib/myipbx/backups /var/log/myipbx /etc/myipbx
        chown -R myipbx:myipbx /var/lib/myipbx /var/log/myipbx
        chmod 0750 /var/lib/myipbx /var/log/myipbx /etc/myipbx
    ' || fail "the service account could not be created"
}

configure_live_boot() {
    log_step "configuring the live boot machinery for an appliance"

    if is_rehearsal; then
        return 0
    fi

    # This file is not what names the appliance at boot, and it is worth being
    # clear about why, because the obvious reading is wrong.
    #
    # The live boot machinery reads its settings from the copy of this file
    # inside the boot image, not from the copy in this filesystem, and the boot
    # image is produced when the kernel package is installed -- before this
    # stage runs.  A name written here therefore never reaches the code that
    # would act on it.  The name is passed as a boot argument instead, which is
    # generated once in the shared settings and used by both boot paths.
    #
    # What this copy is good for is the appliance after installation to a fixed
    # disk, where the boot image is rebuilt and this file is read normally.
    write_into_chroot /etc/casper.conf 0644 <<EOF
# ${APPLIANCE_NAME} -- live boot settings
#
# The boot argument is authoritative while the appliance runs from the image.
# These values apply once the appliance has been installed to a disk and its
# boot image rebuilt.
export USERNAME="${APPLIANCE_HOST_NAME}"
export USERFULLNAME="appliance operator"
export HOST="${APPLIANCE_HOST_NAME}"
export BUILD_SYSTEM="${APPLIANCE_HOST_NAME}"
export FLAVOUR="${APPLIANCE_HOST_NAME}"
EOF

    # The module lists below are written for whoever later rebuilds the boot
    # image on this appliance -- after a kernel upgrade, for instance. They are
    # deliberately NOT acted on during this build, and the reason is worth
    # recording rather than discovering again.
    #
    # The boot image that ships is the one the kernel package produced. That
    # one is known to boot, because this build starts the finished image and
    # watches it reach a login prompt. Rebuilding it here was tried, and the
    # result could not create the writable layer over its own read only root:
    # the boot stopped at a rescue shell reporting that it found no support for
    # its layering format. The modules were present in the rebuilt archive and
    # it failed anyway, so the rebuild was removed rather than papered over.
    #
    # An appliance that boots matters more than an appliance whose boot
    # messages are tidy. The desktop hooks the live boot machinery ships are
    # therefore left in place too: they are noisy on a machine with no desktop,
    # and noise is a smaller fault than a rescue shell.
    write_into_chroot /etc/initramfs-tools/modules 0644 <<'EOF'
# Legacy-to-Modern IPBX Appliance -- modules any rebuilt boot image must carry
#
# These are not applied during the image build. They are here so that a rebuild
# performed later, on the appliance itself, carries what the boot depends on.

# The writable layer over the read only image. Without these the live boot
# reports that it found no support for its own format and stops at a shell.
overlay
squashfs
loop

# The media the appliance is booted from.
isofs
sr_mod
cdrom
usb_storage
uas

# Controllers for those media, old and new alike.
ehci_pci
ohci_pci
uhci_hcd
xhci_pci
ahci
ata_piix
pata_acpi
sd_mod
EOF

    # Any rebuilt boot image should carry the wide driver set rather than only
    # the modules of the machine that happened to rebuild it.
    write_into_chroot /etc/initramfs-tools/conf.d/myipbx.conf 0644 <<'EOF'
# Carry the wide driver set: this image boots machines nobody has inspected.
MODULES=most
EOF

    log_info "the boot image that ships is the one the kernel package produced, which is the one this build proves boots"
}

accommodate_live_boot_scripts() {
    log_step "making room for the live boot scripts to succeed"

    if is_rehearsal; then
        return 0
    fi

    # The live boot machinery ships a set of scripts written for a desktop
    # installation disc, and it runs all of them regardless of what the image
    # actually contains.  On an appliance with no desktop several of them reach
    # for directories and files that were never installed and complain, in a
    # burst of errors on the console, while the boot proceeds perfectly well.
    #
    # The scripts cannot be removed, because they live inside the boot image
    # and this build deliberately does not rebuild it.  What can be done is to
    # give each of them the thing it reaches for, so that it does its work
    # quietly instead of failing loudly.  Everything below is created for the
    # sake of one named script, and is inert otherwise.

    in_chroot bash -c '
        # The live boot initialises a crash directory so that a desktop
        # notifier can watch it.  Nothing watches it here, but its absence is
        # reported twice on every boot.
        mkdir -p /var/crash

        # Two scripts replace update checkers with a command that does nothing.
        # Neither checker is installed, so the replacement is harmless; only
        # the missing directory turns it into an error.
        mkdir -p /usr/lib/update-notifier /usr/lib/ubuntu-release-upgrader

        # A desktop accessibility profile is written unconditionally.
        mkdir -p /etc/xdg/autostart
    ' || log_warn "the live boot scripts could not be accommodated"

    # The crash reporter is read to decide whether to switch it on for the
    # installer.  There is no installer and no crash reporter in this image, and
    # the file is written so that the reading succeeds and the answer is no.
    write_into_chroot /etc/default/apport 0644 <<'EOF'
# Crash reporting is not installed on this appliance and nothing here enables
# it.  The file exists because the live boot consults it, and a consultation
# that finds nothing is quieter than one that finds no file at all.
EOF
}

configure_static_network() {
    log_step "configuring the static address the appliance arrives with"
    log_info "the appliance will answer on ${APPLIANCE_DEFAULT_ADDRESS} at port number ${APPLIANCE_CONSOLE_PORT}"

    if is_rehearsal; then
        return 0
    fi

    # Note what is absent: there is no address request directive anywhere in
    # this file, and no allocation service anywhere in the image. Every address
    # this appliance has was written down by somebody.
    write_into_chroot /etc/systemd/network/10-myipbx-static.network 0644 <<EOF
# ${APPLIANCE_NAME} -- the address this appliance arrives with
#
# This appliance requests no address and offers no address. Change this from
# the appliance's own console once you can reach it.

[Match]
Name=en* eth*

[Network]
Address=${APPLIANCE_DEFAULT_ADDRESS}/${APPLIANCE_DEFAULT_PREFIX}
LinkLocalAddressing=no
IPv6AcceptRA=no
DHCP=no
EOF

    in_chroot systemctl enable systemd-networkd.service >/dev/null 2>&1 || true

    # Any address requesting client that arrived as a dependency is masked, so
    # the appliance cannot acquire an address it was not given deliberately.
    local client
    for client in dhclient dhcpcd systemd-networkd-wait-online; do
        in_chroot systemctl mask "${client}.service" >/dev/null 2>&1 || true
    done
}

configure_appliance() {
    log_step "writing the appliance configuration"

    if is_rehearsal; then
        return 0
    fi

    write_into_chroot /etc/myipbx/appliance.json 0640 <<EOF
{
  "appliance": {
    "listen_address": "0.0.0.0",
    "listen_port": ${APPLIANCE_CONSOLE_PORT},
    "web_root": "/opt/myipbx/web",
    "state_directory": "/var/lib/myipbx",
    "asterisk_configuration_directory": "/etc/asterisk",
    "log_file": "/var/log/myipbx/appliance.log",
    "log_level": "INFO",
    "manager_host": "127.0.0.1",
    "manager_port": 5038,
    "manager_username": "myipbx",
    "manager_secret": "",
    "privileged_helper": "/opt/myipbx/bin/myipbx-privileged-helper.sh",
    "fail_on_address_allocation_server": true
  },
  "revision": 1,
  "site": { "name": "an unconfigured appliance", "timezone": "UTC" },
  "trunks": [],
  "extensions": [],
  "ring_groups": [],
  "inbound_routes": [],
  "outbound_routes": [],
  "time_conditions": [],
  "ivr_menus": [],
  "queues": [],
  "conferences": [],
  "firewall_rules": [],
  "dialplan": { "inbound_context": "from-trunk", "internal_context": "internal" },
  "hardware": { "spans": [] }
}
EOF

    in_chroot chown root:myipbx /etc/myipbx/appliance.json || true
}

configure_services() {
    log_step "choosing what runs and what does not"

    if is_rehearsal; then
        return 0
    fi

    # The privileged helper is enabled before the control plane, because the
    # control plane can perform no system operation until the helper is
    # listening and an operator would have no way to tell why.
    in_chroot systemctl enable myipbx-helperd.service >/dev/null 2>&1 \
        || log_warn "the privileged helper service could not be enabled"
    in_chroot systemctl enable myipbx.service >/dev/null 2>&1 \
        || log_warn "the appliance service could not be enabled"
    in_chroot systemctl enable asterisk.service >/dev/null 2>&1 \
        || log_warn "the telephony engine service could not be enabled"
    in_chroot systemctl enable chrony.service >/dev/null 2>&1 || true

    # A serial console, because an appliance in a rack with no monitor still
    # has to be recoverable, and because it is what lets the build verify its
    # own image by booting it.
    in_chroot systemctl enable serial-getty@ttyS0.service >/dev/null 2>&1 || true

    # Administrative access at the command line is present but off. The
    # appliance is operated from its console; a shell is a recovery tool, and a
    # recovery tool that is listening by default is an attack surface.
    in_chroot systemctl disable ssh.service >/dev/null 2>&1 || true

    # Nothing may ever enable an allocation service, so the unit names are
    # masked whether or not anything installed them.
    local forbidden
    for forbidden in isc-dhcp-server isc-dhcp-server6 dnsmasq udhcpd kea-dhcp4-server; do
        in_chroot systemctl mask "${forbidden}.service" >/dev/null 2>&1 || true
    done
    log_info "every address allocation service unit name is masked in the image"
}

configure_boot_message() {
    log_step "writing the message the operator sees on the console"

    if is_rehearsal; then
        return 0
    fi

    # Deliberately spelled, because Constraint Two applies to the appliance's
    # own console as much as to its logs.
    local spelled_address spelled_port
    spelled_address="$(spell_all "${APPLIANCE_DEFAULT_ADDRESS}")"
    spelled_port="$(spell_all "${APPLIANCE_CONSOLE_PORT}")"

    write_into_chroot /etc/issue 0644 <<EOF

  ${APPLIANCE_NAME}

  Open a browser at the address ${spelled_address}
  on port number ${spelled_port}

  This appliance assigns no addresses. It arrived with the address above
  written into it. Change it from the console once you can reach it.

  The administrator password is printed once, on this screen, when the
  appliance first starts.

EOF

    cp "${CHROOT_DIR}/etc/issue" "${CHROOT_DIR}/etc/issue.net"

    write_into_chroot /etc/motd 0644 <<EOF

  ${APPLIANCE_NAME}

  Everything is done from the browser console. This shell is a recovery tool.
  The manual is in the directory /opt/myipbx/docs.

EOF
}

tidy_image() {
    log_step "tidying the image"

    if is_rehearsal; then
        return 0
    fi

    # The build time restriction on starting services must not ship.
    rm -f "${CHROOT_DIR}/usr/sbin/policy-rc.d"

    in_chroot apt-get clean || true
    rm -rf "${CHROOT_DIR}/var/lib/apt/lists"/* \
           "${CHROOT_DIR}/var/cache/apt/archives"/*.deb \
           "${CHROOT_DIR}/tmp"/* \
           "${CHROOT_DIR}/var/tmp"/* 2>/dev/null || true

    # No machine identity may be baked into an image that many machines boot.
    : >"${CHROOT_DIR}/etc/machine-id"
    rm -f "${CHROOT_DIR}/var/lib/dbus/machine-id"

    # Nor may any host key, or every appliance would share one.
    rm -f "${CHROOT_DIR}"/etc/ssh/ssh_host_* 2>/dev/null || true
}

audit_image() {
    log_step "auditing the finished image"

    if is_rehearsal; then
        return 0
    fi

    local findings=0

    # Constraint One, inside the image this time.
    if in_chroot bash -c 'ls /usr/sbin/dhcpd /usr/sbin/kea-dhcp4 /usr/sbin/udhcpd' >/dev/null 2>&1; then
        log_error "an address allocation server binary is present in the image"
        findings=$(( findings + 1 ))
    fi
    if [[ -f "${CHROOT_DIR}/etc/dhcp/dhcpd.conf" ]]; then
        log_error "an address allocation configuration file is present in the image"
        findings=$(( findings + 1 ))
    fi

    # No credential may ship inside an image that anybody can download.
    if [[ -f "${CHROOT_DIR}/var/lib/myipbx/credentials.json" ]]; then
        log_error "a credential was baked into the image"
        findings=$(( findings + 1 ))
    fi

    # The service account must hold no privilege of its own.  It reaches
    # privilege by asking a daemon that holds it, and a grant here would mean
    # the account could act directly instead.
    if [[ -e "${CHROOT_DIR}/etc/sudoers.d/myipbx" ]]; then
        log_error "the service account was granted privilege directly in the image"
        findings=$(( findings + 1 ))
    fi

    # And the daemon that does hold the privilege must actually be there, or
    # every operation offered by the interface would fail on a real machine.
    if [[ ! -f "${CHROOT_DIR}/etc/systemd/system/myipbx-helperd.service" ]]; then
        log_error "the privileged helper's service is missing from the image"
        findings=$(( findings + 1 ))
    fi

    if (( findings > 0 )); then
        fail "the image audit made $(spell_integer "${findings}") finding or findings"
    fi
    log_info "the image audit passed: no allocation service, and no credential shipped"
}

main() {
    banner "image stage three -- configuring the appliance"
    require_root

    if [[ ! -d "${CHROOT_DIR}" ]]; then
        is_rehearsal || fail "no root filesystem was found; run the earlier stages first"
        log_warn "rehearsal: no root filesystem exists, so this stage only reports what it would do"
    fi

    if skip_image_stage_if_completed "${STAGE}"; then
        return 0
    fi

    trap 'unmount_chroot' EXIT

    configure_identity
    configure_live_boot
    accommodate_live_boot_scripts
    configure_service_account
    configure_static_network
    configure_appliance
    configure_services
    configure_boot_message
    tidy_image
    audit_image

    report_size "${CHROOT_DIR}" "the configured root filesystem"
    mark_image_stage_completed "${STAGE}"
    log_info "image stage three is complete"
}

main "$@"
