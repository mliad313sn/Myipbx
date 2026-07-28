#!/usr/bin/env bash
# Removes the appliance control plane.
#
# The telephony engine and the interface card drivers are deliberately left in
# place: they are the customer's telephony service, and removing them would
# take the site's telephones down.  This script removes only what the appliance
# installer added on top of them.
#
# State and configuration are preserved unless removal is demanded explicitly,
# so that an uninstall followed by a reinstall does not lose a site's dialplan.

set -o errexit
set -o nounset
set -o pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/lib/common.sh
source "${SCRIPT_DIR}/lib/common.sh"

REMOVE_STATE="no"
REMOVE_ACCOUNT="no"

usage() {
    cat <<'USAGE'
usage: uninstall-appliance.sh [options]

  --remove-state     also remove the configuration, the state, and the backups
  --remove-account   also remove the unprivileged service account
  --rehearse         report what would be removed without removing anything
  --help             show this message

the telephony engine and the interface card drivers are never removed by this
script.  they are the site's telephony service, not appliance scaffolding.
USAGE
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --remove-state) REMOVE_STATE="yes"; shift ;;
        --remove-account) REMOVE_ACCOUNT="yes"; shift ;;
        --rehearse|--dry-run) export REHEARSAL="yes"; shift ;;
        --help|-h) usage; exit 0 ;;
        *) printf 'the argument %s is not recognised\n' "$1" >&2; usage; exit 2 ;;
    esac
done

stop_service() {
    log_step "stopping the appliance service"

    if ! have_command systemctl; then
        log_warn "the service manager is not available; stop the control plane by hand"
        return 0
    fi
    if systemctl list-unit-files 2>/dev/null | grep -q '^crossbar.service'; then
        run_command systemctl disable --now crossbar.service || log_warn "the service could not be disabled cleanly"
        run_command rm -f /etc/systemd/system/crossbar.service
        run_command systemctl daemon-reload
        log_info "the appliance service unit was removed"
    else
        log_info "no appliance service unit is installed"
    fi
}

remove_program_files() {
    log_step "removing the control plane and the dashboard"

    if [[ -d "${APPLIANCE_PREFIX}" ]]; then
        run_command rm -rf "${APPLIANCE_PREFIX}"
        log_info "the directory at ${APPLIANCE_PREFIX} was removed"
    else
        log_info "no control plane directory is present"
    fi

    if [[ -f /etc/modules-load.d/crossbar-dahdi.conf ]]; then
        log_info "the interface driver module configuration is left in place so the cards continue to work"
    fi
}

remove_state() {
    log_step "removing the configuration and the state"

    if [[ "${REMOVE_STATE}" != "yes" ]]; then
        log_info "the configuration and the state are preserved; pass the remove state option to delete them"
        return 0
    fi

    local path
    for path in "${APPLIANCE_CONFIG_DIR}" "${APPLIANCE_STATE_DIR}" "${APPLIANCE_LOG_DIR}"; do
        if [[ -d "${path}" ]]; then
            run_command rm -rf "${path}"
            log_info "the directory at ${path} was removed"
        fi
    done
}

remove_account() {
    log_step "removing the service account"

    if [[ "${REMOVE_ACCOUNT}" != "yes" ]]; then
        log_info "the service account named ${APPLIANCE_USER} is preserved; pass the remove account option to delete it"
        return 0
    fi
    if id -u "${APPLIANCE_USER}" >/dev/null 2>&1; then
        run_command userdel "${APPLIANCE_USER}" || log_warn "the service account could not be removed"
    else
        log_info "no service account named ${APPLIANCE_USER} exists"
    fi
}

main() {
    banner "removal of the appliance control plane"
    require_root

    stop_service
    remove_program_files
    remove_state
    remove_account

    log_info "the removal is complete"
    log_info "the telephony engine and the interface card drivers were left in place"
}

main "$@"
