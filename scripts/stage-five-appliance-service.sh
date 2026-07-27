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
LISTEN_ADDRESS="${APPLIANCE_LISTEN_ADDRESS:-0.0.0.0}"
LISTEN_PORT="${APPLIANCE_LISTEN_PORT:-8088}"

install_control_plane() {
    log_step "installing the control plane package"

    ensure_directory "${APPLIANCE_PREFIX}" 0755
    ensure_directory "${APPLIANCE_PREFIX}/appliance" 0755

    local module
    for module in "${REPOSITORY_ROOT}"/appliance/*.py; do
        [[ -f "${module}" ]] || continue
        install_file "${module}" "${APPLIANCE_PREFIX}/appliance/$(basename "${module}")" 0644
    done

    log_info "the control plane package was installed to ${APPLIANCE_PREFIX}"
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

install_service_unit() {
    log_step "installing the service unit"

    local source="${REPOSITORY_ROOT}/config/systemd/myipbx.service"
    [[ -f "${source}" ]] || fail "the service unit template is missing from the repository"

    if [[ -d /etc/systemd/system ]]; then
        install_file "${source}" /etc/systemd/system/myipbx.service 0644
        run_command systemctl daemon-reload
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
    log_info "the dashboard is reachable on the address ${LISTEN_ADDRESS} at port number ${LISTEN_PORT}"
    log_info "the initial administrator password was printed by the control plane on its first start; recover it from the service journal if it was missed"
    log_info "this appliance assigns no addresses"
    printf '\n'
}

main() {
    banner "stage five -- the control plane and the dashboard"
    require_root

    if skip_if_completed "${STAGE}"; then
        return 0
    fi

    install_control_plane
    install_dashboard
    write_configuration_document
    install_service_unit
    start_appliance
    report_access_details

    mark_stage_completed "${STAGE}" "the control plane is installed and running"
    log_info "stage five is complete"
}

main "$@"
