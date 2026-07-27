#!/usr/bin/env bash
# Stage five -- the control plane and the dashboard.
#
# Installs the control plane package and the browser dashboard, writes the
# configuration document, installs the service unit, and starts the appliance.
#
# The control plane imports nothing outside the standard library, so this stage
# installs no packages and needs no package index.

set -o errexit
set -o nounset
set -o pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/lib/common.sh
source "${SCRIPT_DIR}/lib/common.sh"

STAGE="stage-five-appliance-service"

REPOSITORY_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

# The console binds one address, not every address. An appliance that can
# reboot the machine, rewrite its firewall and rebuild kernel modules should
# appear on the network somebody chose for it and on no other. When the
# installation was given no management address, the console binds the loopback
# address and this stage says so at the end in as many words, because an
# appliance nobody can reach is a problem an operator can fix in a minute and
# an appliance on every interface is one they will not discover for months.
LISTEN_ADDRESS="${APPLIANCE_LISTEN_ADDRESS:-}"
MANAGEMENT_ADDRESS_WAS_GIVEN="yes"
if [[ -z "${LISTEN_ADDRESS}" ]]; then
    LISTEN_ADDRESS="127.0.0.1"
    MANAGEMENT_ADDRESS_WAS_GIVEN="no"
fi
LISTEN_PORT="${APPLIANCE_LISTEN_PORT:-8088}"
REDIRECT_PORT="${APPLIANCE_REDIRECT_PORT:-8080}"
TLS_DIR="${APPLIANCE_CONFIG_DIR}/tls"

install_control_plane() {
    log_step "installing the control plane package"

    ensure_directory "${APPLIANCE_PREFIX}" 0755
    ensure_directory "${APPLIANCE_PREFIX}/appliance" 0755

    local module
    for module in "${REPOSITORY_ROOT}"/appliance/*.py; do
        [[ -f "${module}" ]] || continue
        install_file "${module}" "${APPLIANCE_PREFIX}/appliance/$(basename "${module}")" 0644
    done

    # The data the package reads, which is not a module and so is not caught by
    # the loop above. It was missed once: the interface card catalogue stayed
    # in the repository, the appliance found nothing, and every fitted card
    # would have been reported as unrecognised on a real machine while the
    # suite -- which runs from the repository -- stayed green.
    ensure_directory "${APPLIANCE_PREFIX}/share" 0755
    local datum
    for datum in "${REPOSITORY_ROOT}"/share/*; do
        [[ -f "${datum}" ]] || continue
        install_file "${datum}" "${APPLIANCE_PREFIX}/share/$(basename "${datum}")" 0644
    done

    log_info "the control plane package was installed to ${APPLIANCE_PREFIX}"
}

install_privileged_helper() {
    log_step "installing the privileged helper and the daemon that holds its privilege"

    ensure_directory "${APPLIANCE_PREFIX}/bin" 0755
    ensure_directory "${APPLIANCE_PREFIX}/bin/lib" 0755

    install_file "${REPOSITORY_ROOT}/scripts/myipbx-privileged-helper.sh" \
        "${APPLIANCE_PREFIX}/bin/myipbx-privileged-helper.sh" 0755
    install_file "${REPOSITORY_ROOT}/scripts/lib/common.sh" \
        "${APPLIANCE_PREFIX}/bin/lib/common.sh" 0644

    # The helper delegates the network and driver work to the staging scripts,
    # so they must be beside it once installed.
    local stage
    for stage in stage-two-network-static.sh stage-three-dahdi-drivers.sh verify-no-dhcp.sh \
                 myipbx-generate-certificate.sh; do
        install_file "${REPOSITORY_ROOT}/scripts/${stage}" \
            "${APPLIANCE_PREFIX}/bin/${stage}" 0755
    done

    local unit="${REPOSITORY_ROOT}/config/systemd/myipbx-helperd.service"
    [[ -f "${unit}" ]] || fail "the privileged helper's service unit is missing from the repository"

    if is_rehearsal; then
        log_info "rehearsal: the privileged helper's service would be installed and started"
        return 0
    fi

    # The service account is granted nothing at all.  It reaches privilege by
    # asking a daemon that already holds it, over a socket the daemon opens
    # only to this account, and the daemon accepts only the same fixed
    # vocabulary of verbs.
    #
    # An earlier design granted the account the right to run this helper under
    # sudo.  That could never have worked: the control plane's service unit
    # sets NoNewPrivileges, which disables the setuid mechanism, and sudo
    # refuses to run at all under it.  Any leftover grant from that design is
    # removed here rather than left to confuse a later reader, and because a
    # privilege grant that is no longer needed should not survive the reason
    # for it.
    if [[ -f /etc/sudoers.d/myipbx ]]; then
        rm -f /etc/sudoers.d/myipbx
        log_info "the obsolete privilege grant was removed; the service account now has none"
    fi

    if ! have_command systemctl; then
        log_warn "the service manager is not available; the privileged helper was installed but not started"
        return 0
    fi

    install_file "${unit}" /etc/systemd/system/myipbx-helperd.service 0644
    run_command systemctl daemon-reload
    run_command systemctl enable myipbx-helperd.service
    run_command systemctl restart myipbx-helperd.service
    log_info "the privileged helper is listening; the service account holds no privilege of its own"
}

install_dashboard() {
    log_step "installing the browser dashboard"

    ensure_directory "${APPLIANCE_PREFIX}/web" 0755
    ensure_directory "${APPLIANCE_PREFIX}/web/js" 0755
    ensure_directory "${APPLIANCE_PREFIX}/web/css" 0755

    install_file "${REPOSITORY_ROOT}/web/index.html" "${APPLIANCE_PREFIX}/web/index.html" 0644

    local asset
    for asset in "${REPOSITORY_ROOT}"/web/js/*.js; do
        [[ -f "${asset}" ]] || continue
        install_file "${asset}" "${APPLIANCE_PREFIX}/web/js/$(basename "${asset}")" 0644
    done
    for asset in "${REPOSITORY_ROOT}"/web/css/*.css; do
        [[ -f "${asset}" ]] || continue
        install_file "${asset}" "${APPLIANCE_PREFIX}/web/css/$(basename "${asset}")" 0644
    done

    log_info "the browser dashboard was installed to ${APPLIANCE_PREFIX}/web"
}

write_configuration_document() {
    log_step "writing the appliance configuration document"

    local document="${APPLIANCE_CONFIG_DIR}/appliance.json"
    if [[ -f "${document}" ]]; then
        log_info "a configuration document already exists and will not be replaced"
        return 0
    fi
    if is_rehearsal; then
        log_info "rehearsal: the configuration document would be written to ${document}"
        return 0
    fi

    ensure_directory "${APPLIANCE_CONFIG_DIR}" 0750

    local manager_secret=""
    if [[ -f "${APPLIANCE_CONFIG_DIR}/manager.secret" ]]; then
        manager_secret="$(cat "${APPLIANCE_CONFIG_DIR}/manager.secret")"
    fi

    # The listening port is written as a number because it is machine read.
    # Every human facing rendering of it is spelled in words by the control
    # plane, which is where Constraint Two applies.
    cat >"${document}" <<EOF
{
  "appliance": {
    "listen_address": "${LISTEN_ADDRESS}",
    "listen_port": ${LISTEN_PORT},
    "tls_enabled": true,
    "tls_certificate": "${TLS_DIR}/appliance.crt",
    "tls_private_key": "${TLS_DIR}/appliance.key",
    "tls_minimum_version": "TLSv1.2",
    "plain_http_redirect_port": ${REDIRECT_PORT},
    "session_cookie_secure": true,
    "web_root": "${APPLIANCE_PREFIX}/web",
    "state_directory": "${APPLIANCE_STATE_DIR}",
    "asterisk_configuration_directory": "/etc/asterisk",
    "log_file": "${APPLIANCE_LOG_DIR}/appliance.log",
    "log_level": "INFO",
    "manager_host": "127.0.0.1",
    "manager_port": 5038,
    "manager_username": "myipbx",
    "manager_secret": "${manager_secret}",
    "fail_on_address_allocation_server": true
  },
  "revision": 1,
  "site": { "name": "an unnamed site", "timezone": "UTC" },
  "trunks": [],
  "extensions": [],
  "dialplan": { "inbound_context": "from-trunk", "internal_context": "internal" },
  "hardware": { "spans": [] }
}
EOF

    run_command chmod 0640 "${document}"
    if id -u "${APPLIANCE_USER}" >/dev/null 2>&1; then
        run_command chown "root:${APPLIANCE_USER}" "${document}"
    fi
    log_info "the configuration document was written to ${document}"
}

generate_certificate() {
    log_step "generating this appliance's own certificate"

    # Generated here, on this machine, and never shipped. A certificate that
    # travelled with the software would be the same certificate on every
    # appliance running it, and one private key shared by every site is worse
    # than the plain transport it would appear to have replaced.
    local generator="${APPLIANCE_PREFIX}/bin/myipbx-generate-certificate.sh"
    [[ -x "${generator}" ]] || fail "the certificate generator was not installed beside the helper"

    if is_rehearsal; then
        log_info "rehearsal: this appliance's certificate would be generated at ${TLS_DIR}"
        return 0
    fi

    APPLIANCE_LISTEN_ADDRESS="${LISTEN_ADDRESS}" "${generator}" \
        || fail "the certificate could not be generated, and the console will not serve without one"
}

install_service_unit() {
    log_step "installing the service unit"

    local source="${REPOSITORY_ROOT}/config/systemd/myipbx.service"
    [[ -f "${source}" ]] || fail "the service unit template is missing from the repository"

    local certificate_unit="${REPOSITORY_ROOT}/config/systemd/myipbx-certificate.service"
    [[ -f "${certificate_unit}" ]] || fail "the certificate service unit is missing from the repository"

    if [[ -d /etc/systemd/system ]]; then
        install_file "${source}" /etc/systemd/system/myipbx.service 0644
        # The generation runs again before every start, and does nothing at all
        # on every start after the first. It is installed even though this
        # stage has already generated one, because an appliance whose
        # certificate is later removed or expires should recover on a reboot
        # rather than wait for somebody to notice.
        install_file "${certificate_unit}" /etc/systemd/system/myipbx-certificate.service 0644
        run_command systemctl daemon-reload
        run_command systemctl enable myipbx-certificate.service
        run_command systemctl enable myipbx.service
        log_info "the appliance service unit is installed and enabled"
    else
        log_warn "this machine has no service unit directory; start the control plane with the module invocation instead"
    fi
}

start_appliance() {
    log_step "starting the appliance"

    if is_rehearsal; then
        log_info "rehearsal: the appliance service would be started"
        return 0
    fi
    if ! have_command systemctl; then
        log_warn "the service manager is not available; start the control plane by hand"
        return 0
    fi

    systemctl restart myipbx.service || fail "the appliance service did not start; inspect the service journal for the reason"

    local attempt
    for attempt in one two three four five; do
        sleep 1
        if systemctl is-active --quiet myipbx.service; then
            log_info "the appliance service is running"
            return 0
        fi
    done

    fail "the appliance service did not reach a running state"
}

report_access_details() {
    printf '\n'
    log_info "the dashboard is reachable over a secured connection on the address ${LISTEN_ADDRESS} at port number ${LISTEN_PORT}"
    log_info "the plain port number ${REDIRECT_PORT} answers only by sending a browser to the secured one; it serves nothing"
    log_info "the initial administrator password was printed by the control plane on its first start; recover it from the service journal if it was missed"
    log_info "this appliance assigns no addresses"

    if [[ "${MANAGEMENT_ADDRESS_WAS_GIVEN}" != "yes" ]]; then
        printf '\n'
        log_warn "this installation was given no management address, so the console is bound to the loopback address and is reachable only from this machine"
        log_warn "it was NOT bound to every interface, because a console that can reboot this machine and rewrite its firewall should not appear on a network nobody chose"
        log_warn "set the management address in ${APPLIANCE_CONFIG_DIR}/appliance.json and restart the appliance service to reach it from elsewhere"
    fi

    # Printed rather than logged, and printed with its numerals intact. Every
    # logged line has them spelled into words to satisfy Constraint Two, and a
    # fingerprint put through that could no longer be compared against the one
    # the browser shows, which is the only thing it is for.
    if [[ -r "${TLS_DIR}/appliance.crt" ]] && have_command openssl; then
        local fingerprint
        fingerprint="$(openssl x509 -in "${TLS_DIR}/appliance.crt" -noout -fingerprint -sha256 2>/dev/null | sed 's/^.*=//')"
        if [[ -n "${fingerprint}" ]]; then
            printf '\n'
            printf '  this appliance signed its own certificate. the first browser to reach\n'
            printf '  the console will warn. compare what it shows against this fingerprint\n'
            printf '  before accepting it:\n\n'
            printf '  %s\n' "${fingerprint}"
        fi
    fi
    printf '\n'
}

main() {
    banner "stage five -- the control plane and the dashboard"
    require_root

    if skip_if_completed "${STAGE}"; then
        return 0
    fi

    install_control_plane
    install_privileged_helper
    install_dashboard
    write_configuration_document
    # Before the unit is installed and long before the service starts: the
    # control plane refuses to serve without a certificate, by design.
    generate_certificate
    install_service_unit
    start_appliance
    report_access_details

    mark_stage_completed "${STAGE}" "the control plane is installed and running"
    log_info "stage five is complete"
}

# Dispatch when run, stay quiet when sourced, by the ordinary shell idiom. The
# suite reaches one function of this stage that way -- laying the package down
# in a temporary prefix -- without a service manager, a certificate, or an
# appliance to start.
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
    main "$@"
fi
