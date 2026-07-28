#!/usr/bin/env bash
# The privileged helper.
#
# The control plane runs unprivileged and never performs a system operation
# itself. It asks this helper, which is the only thing the service account is
# permitted to run with elevated privilege, and which accepts only the fixed
# vocabulary below.
#
# Every argument is validated here as well as in the control plane. That
# duplication is deliberate: this script must be safe when invoked by anything,
# not merely safe when invoked by a caller that already checked. Nothing here
# is ever passed to a shell for interpretation, and no argument is ever placed
# inside a command that is then evaluated.
#
# Exit status: zero when the operation succeeded, one when it failed, two when
# the request itself was refused.

set -o errexit
set -o nounset
set -o pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# The library lives beside this script once installed, and one directory up in
# the repository. Either is acceptable; neither is fatal if absent.
if [[ -f "${SCRIPT_DIR}/lib/common.sh" ]]; then
    # shellcheck source=scripts/lib/common.sh
    source "${SCRIPT_DIR}/lib/common.sh"
elif [[ -f "${SCRIPT_DIR}/../lib/common.sh" ]]; then
    # shellcheck source=scripts/lib/common.sh
    source "${SCRIPT_DIR}/../lib/common.sh"
else
    printf 'the shared library could not be found\n' >&2
    exit 2
fi

MANAGED_SERVICES=(asterisk crossbar dahdi)

refuse() {
    log_error "the request was refused: $*"
    exit 2
}

# -- validation --------------------------------------------------------------

validate_service() {
    local candidate="$1"
    local permitted
    for permitted in "${MANAGED_SERVICES[@]}"; do
        if [[ "${candidate}" == "${permitted}" ]]; then
            return 0
        fi
    done
    refuse "the service named ${candidate} is not one this appliance manages"
}

validate_interface() {
    [[ "$1" =~ ^[a-zA-Z][a-zA-Z0-9._-]{0,15}$ ]] || refuse "the interface name is not acceptable"
}

validate_address() {
    local candidate="$1"
    [[ "${candidate}" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]] || refuse "the address is not acceptable"
    local part
    for part in ${candidate//./ }; do
        (( part >= 0 && part <= 255 )) || refuse "the address is not acceptable"
    done
}

validate_prefix() {
    [[ "$1" =~ ^([1-9]|[12][0-9]|3[0-2])$ ]] || refuse "the prefix length is not acceptable"
}

validate_hostname() {
    [[ "$1" =~ ^[a-zA-Z0-9]([a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?$ ]] \
        || refuse "the host name is not acceptable"
}

validate_timezone() {
    [[ "$1" =~ ^[A-Za-z][A-Za-z0-9_+-]*(/[A-Za-z0-9_+-]+){0,2}$ ]] \
        || refuse "the time zone is not acceptable"
    if [[ -d /usr/share/zoneinfo ]] && [[ ! -e "/usr/share/zoneinfo/$1" ]]; then
        refuse "the time zone named $1 is not known to this machine"
    fi
}

require_arguments() {
    local expected="$1"
    local received="$2"
    if (( received != expected )); then
        refuse "the operation expected $(spell_integer "${expected}") argument or arguments and received $(spell_integer "${received}")"
    fi
}

# -- operations --------------------------------------------------------------

operation_service_status() {
    require_arguments 1 $#
    validate_service "$1"
    if systemctl is-active --quiet "$1"; then
        log_info "the service named $1 is running"
        return 0
    fi
    log_info "the service named $1 is not running"
    return 1
}

operation_service_control() {
    local action="$1"
    shift
    require_arguments 1 $#
    validate_service "$1"

    log_info "asking the service manager to ${action} the service named $1"
    systemctl "${action}" "$1" || {
        log_error "the service manager could not ${action} the service named $1"
        return 1
    }
    log_info "the service named $1 was asked to ${action}"
}

operation_engine_reload() {
    require_arguments 0 $#
    if ! have_command asterisk; then
        log_error "the telephony engine is not installed on this machine"
        return 1
    fi
    log_info "asking the telephony engine to reload its configuration"
    asterisk -rx "core reload" || {
        log_error "the telephony engine did not accept the reload"
        return 1
    }
}

operation_network_apply() {
    require_arguments 4 $#
    local interface="$1" address="$2" prefix="$3" gateway="$4"

    validate_interface "${interface}"
    validate_address "${address}"
    validate_prefix "${prefix}"
    if [[ -n "${gateway}" ]]; then
        validate_address "${gateway}"
    fi

    # Constraint One is re-asserted here, because this is the one operation
    # that touches addressing. The appliance writes a static address and never
    # requests or offers one.
    log_info "applying a static configuration to the interface named ${interface}"
    log_info "this appliance requests no address and offers no address"

    APPLIANCE_INTERFACE="${interface}" \
    APPLIANCE_ADDRESS="${address}" \
    APPLIANCE_PREFIX_LENGTH="${prefix}" \
    APPLIANCE_GATEWAY="${gateway}" \
    FORCE_RERUN=yes \
        bash "${SCRIPT_DIR}/stage-two-network-static.sh" || {
        log_error "the static network configuration could not be applied"
        return 1
    }
}

operation_driver_rebuild() {
    require_arguments 0 $#
    log_info "recompiling the interface card drivers against the running kernel"
    FORCE_RERUN=yes bash "${SCRIPT_DIR}/stage-three-dahdi-drivers.sh" || {
        log_error "the interface card drivers could not be rebuilt"
        return 1
    }
}

operation_span_generate() {
    require_arguments 0 $#
    if ! have_command dahdi_genconf; then
        log_error "the span configuration generator is not installed on this machine"
        return 1
    fi

    log_info "regenerating the interface card span configuration"
    dahdi_genconf || {
        log_error "the span configuration could not be generated"
        return 1
    }
    if have_command dahdi_cfg; then
        dahdi_cfg -vv || log_warn "the span configuration was generated but could not be applied"
    fi
}

# The ruleset is generated by the control plane and written to the state
# directory. This helper only loads what is already there, so nothing an
# operator typed is ever assembled into a rule here.
FIREWALL_RULESET="${FIREWALL_RULESET:-/var/lib/crossbar/firewall.nft}"

operation_firewall_apply() {
    require_arguments 0 $#

    if ! have_command nft; then
        log_error "the firewall tool is not installed on this machine"
        return 1
    fi
    if [[ ! -f "${FIREWALL_RULESET}" ]]; then
        log_error "no ruleset has been generated yet; render it from the console first"
        return 1
    fi

    # A ruleset that does not parse must never be loaded, because a partially
    # applied firewall is worse than none.
    if ! nft --check --file "${FIREWALL_RULESET}"; then
        log_error "the generated ruleset did not parse and was not loaded"
        return 1
    fi

    log_info "loading the appliance ruleset"
    nft --file "${FIREWALL_RULESET}" || {
        log_error "the ruleset could not be loaded"
        return 1
    }
    log_info "the appliance ruleset is loaded; everything not named in it is dropped"
}

# The certificate and its key are validated by the control plane and written
# to the state directory. This helper installs what is already there, and
# checks the pair again itself before it does, because a helper that trusted
# its caller would be a helper that could be made to install anything.
STAGED_TLS_DIR="${STAGED_TLS_DIR:-/var/lib/crossbar/tls-staged}"
INSTALLED_TLS_DIR="${INSTALLED_TLS_DIR:-/etc/crossbar/tls}"

operation_certificate_apply() {
    require_arguments 0 $#

    local staged_certificate="${STAGED_TLS_DIR}/appliance.crt"
    local staged_key="${STAGED_TLS_DIR}/appliance.key"

    if ! have_command openssl; then
        log_error "the openssl tool is not installed on this machine"
        return 1
    fi
    if [[ ! -s "${staged_certificate}" || ! -s "${staged_key}" ]]; then
        log_error "no certificate has been staged from the console yet; upload one there first"
        return 1
    fi

    # The staging directory is written by the unprivileged control plane and
    # read by this helper, which runs as root. That is a shared directory
    # between two privilege levels, and everything below exists because of it.
    #
    # The directory itself must be a directory. Were it replaced by a link, the
    # two paths above would name whatever it pointed at, and this helper would
    # be reading its inputs from a location the caller chose.
    if [[ -L "${STAGED_TLS_DIR}" || ! -d "${STAGED_TLS_DIR}" ]]; then
        log_error "the staging directory is not a directory; nothing was installed"
        return 1
    fi

    # Take a private copy before looking at anything, and look only at the
    # copy from here on.
    #
    # Validating the staged path and then installing from it are two separate
    # reads of a path the caller can still write. Between them the caller can
    # replace a validated certificate with a link to any file root can read,
    # and the install that follows would copy that file out under a mode the
    # console serves. Copying first collapses the two reads into one: whatever
    # this copy captured is what gets checked and what gets installed, and a
    # substitution after it changes nothing.
    #
    # The copy is taken without following links, so a link that was already in
    # place arrives here as a link rather than as its target, and is refused
    # below by looking at what was copied rather than at what was asked for.
    local snapshot
    snapshot="$(mktemp -d "${TMPDIR:-/tmp}/crossbar-tls-XXXXXXXX")" || {
        log_error "a private working directory could not be created; nothing was installed"
        return 1
    }
    chmod 0700 "${snapshot}"
    # shellcheck disable=SC2064
    trap "rm -rf -- '${snapshot}'" RETURN

    cp --no-dereference --preserve=mode "${staged_certificate}" "${snapshot}/appliance.crt" 2>/dev/null || {
        log_error "the staged certificate could not be read; nothing was installed"
        return 1
    }
    cp --no-dereference --preserve=mode "${staged_key}" "${snapshot}/appliance.key" 2>/dev/null || {
        log_error "the staged key could not be read; nothing was installed"
        return 1
    }

    local snapshot_certificate="${snapshot}/appliance.crt"
    local snapshot_key="${snapshot}/appliance.key"

    if [[ -L "${snapshot_certificate}" || -L "${snapshot_key}" ]]; then
        log_error "the staged material is a link rather than a file; nothing was installed"
        return 1
    fi
    if [[ ! -f "${snapshot_certificate}" || ! -f "${snapshot_key}" ]]; then
        log_error "the staged material is not a plain file; nothing was installed"
        return 1
    fi

    # And it has to have been written by the account that stages it. A file
    # this helper found here owned by root was not put here by the console,
    # and copying one out would disclose it.
    if id -u crossbar >/dev/null 2>&1; then
        local certificate_owner key_owner
        certificate_owner="$(stat -c '%U' "${staged_certificate}" 2>/dev/null || echo unknown)"
        key_owner="$(stat -c '%U' "${staged_key}" 2>/dev/null || echo unknown)"
        if [[ "${certificate_owner}" != "crossbar" || "${key_owner}" != "crossbar" ]]; then
            log_error "the staged material was not written by the console; nothing was installed"
            return 1
        fi
    fi

    # Checked here as well as in the control plane. A pair that does not belong
    # together would leave the console unable to start, and the console is the
    # only way an operator has to correct it.
    local certificate_public key_public
    certificate_public="$(openssl x509 -in "${snapshot_certificate}" -noout -pubkey 2>/dev/null || true)"
    key_public="$(openssl pkey -in "${snapshot_key}" -pubout 2>/dev/null || true)"
    if [[ -z "${certificate_public}" || "${certificate_public}" != "${key_public}" ]]; then
        log_error "the staged certificate and key do not match and were not installed"
        return 1
    fi

    # The material already in place is kept. An operator who installs a
    # certificate that turns out to be wrong for their site has something to
    # put back without a terminal, which is the whole premise of this console.
    mkdir -p "${INSTALLED_TLS_DIR}"
    chmod 0750 "${INSTALLED_TLS_DIR}"
    if [[ -f "${INSTALLED_TLS_DIR}/appliance.crt" ]]; then
        cp -f "${INSTALLED_TLS_DIR}/appliance.crt" "${INSTALLED_TLS_DIR}/appliance.crt.previous"
        cp -f "${INSTALLED_TLS_DIR}/appliance.key" "${INSTALLED_TLS_DIR}/appliance.key.previous" 2>/dev/null || true
        chmod 0640 "${INSTALLED_TLS_DIR}/appliance.key.previous" 2>/dev/null || true
    fi

    # From the private copy, never from the staged path. This is the half of
    # the fix that matters: the path the caller can still write is not read
    # again after the snapshot was taken.
    install -m 0644 "${snapshot_certificate}" "${INSTALLED_TLS_DIR}/appliance.crt"
    install -m 0640 "${snapshot_key}" "${INSTALLED_TLS_DIR}/appliance.key"
    if id -u crossbar >/dev/null 2>&1; then
        chown "root:crossbar" "${INSTALLED_TLS_DIR}/appliance.crt" "${INSTALLED_TLS_DIR}/appliance.key"
    fi

    # The staged copy of a private key is removed once it is installed. There
    # is no reason for a second copy of it to exist on this machine.
    rm -f "${staged_certificate}" "${staged_key}"

    log_info "the certificate was installed; restarting the console, which ends every session on this appliance"
    if have_command systemctl; then
        systemctl restart crossbar.service || {
            log_error "the certificate was installed but the console did not restart"
            return 1
        }
    else
        log_warn "this machine has no service manager; restart the control plane by hand for the certificate to take effect"
    fi
    log_info "the console is serving with the newly installed certificate"
}

operation_firewall_status() {
    require_arguments 0 $#

    if ! have_command nft; then
        log_error "the firewall tool is not installed on this machine"
        return 1
    fi
    if nft list table inet crossbar >/dev/null 2>&1; then
        log_info "the appliance ruleset is loaded"
        nft list table inet crossbar
        return 0
    fi
    log_info "the appliance ruleset is not loaded; this machine is unfiltered by it"
    return 1
}

operation_firewall_clear() {
    require_arguments 0 $#

    if ! have_command nft; then
        log_error "the firewall tool is not installed on this machine"
        return 1
    fi
    log_warn "unloading the appliance ruleset; this machine will be unfiltered by it"
    # Only the appliance's own table is removed. A site with its own rules
    # keeps them.
    nft delete table inet crossbar 2>/dev/null || true
    log_info "the appliance ruleset was unloaded"
}

operation_hostname_set() {
    require_arguments 1 $#
    validate_hostname "$1"
    log_info "setting the host name to $1"
    if have_command hostnamectl; then
        hostnamectl set-hostname "$1" || return 1
    else
        hostname "$1" || return 1
        printf '%s\n' "$1" >/etc/hostname
    fi
}

operation_timezone_set() {
    require_arguments 1 $#
    validate_timezone "$1"
    log_info "setting the time zone to $1"
    if have_command timedatectl; then
        timedatectl set-timezone "$1" || return 1
    else
        ln -sf "/usr/share/zoneinfo/$1" /etc/localtime || return 1
        printf '%s\n' "$1" >/etc/timezone
    fi
}

operation_time_synchronise() {
    require_arguments 0 $#
    log_info "synchronising the clock with the configured source"
    if have_command timedatectl; then
        timedatectl set-ntp true || log_warn "the clock synchronisation service could not be enabled"
    fi
    if have_command chronyc; then
        chronyc makestep || log_warn "the clock could not be stepped"
    elif have_command ntpd; then
        ntpd -gq || log_warn "the clock could not be stepped"
    else
        log_warn "no clock synchronisation client is installed on this machine"
        return 1
    fi
}

operation_reboot() {
    require_arguments 0 $#
    log_warn "the machine will restart; every call in progress will be lost"
    # The delay gives the interface time to acknowledge before the machine goes.
    ( sleep 2; systemctl reboot ) >/dev/null 2>&1 &
    log_info "the restart was scheduled"
}

operation_power_off() {
    require_arguments 0 $#
    log_warn "the machine will shut down; every call in progress will be lost"
    ( sleep 2; systemctl poweroff ) >/dev/null 2>&1 &
    log_info "the shut down was scheduled"
}

# -- dispatch ----------------------------------------------------------------

usage() {
    cat <<'USAGE'
usage: crossbar-privileged-helper.sh VERB [ARGUMENT ...]

  service-status SERVICE      report whether a service is running
  service-start SERVICE       start a service
  service-stop SERVICE        stop a service
  service-restart SERVICE     restart a service
  engine-reload               ask the telephony engine to reload
  network-apply IFACE ADDRESS PREFIX GATEWAY
                              write and apply a static network configuration
  driver-rebuild              recompile the interface card drivers
  span-generate               regenerate and apply the span configuration
  firewall-apply              load the ruleset the appliance generated
  certificate-apply           install the certificate staged from the console
                              and restart the console, ending every session
  firewall-status             report whether that ruleset is loaded
  firewall-clear              unload it, leaving the machine unfiltered
  hostname-set NAME           set the machine's host name
  timezone-set ZONE           set the machine's time zone
  time-synchronise            synchronise the clock
  reboot                      restart the machine
  power-off                   shut the machine down

this helper is the only elevated path the appliance has. it accepts no verb
outside this list and validates every argument before acting on it.
USAGE
}

main() {
    if [[ $# -lt 1 ]]; then
        usage
        exit 2
    fi

    local verb="$1"
    shift

    if [[ "${EUID}" -ne 0 ]]; then
        refuse "this helper must be run with administrative privilege"
    fi

    case "${verb}" in
        service-status)   operation_service_status "$@" ;;
        service-start)    operation_service_control start "$@" ;;
        service-stop)     operation_service_control stop "$@" ;;
        service-restart)  operation_service_control restart "$@" ;;
        engine-reload)    operation_engine_reload "$@" ;;
        network-apply)    operation_network_apply "$@" ;;
        driver-rebuild)   operation_driver_rebuild "$@" ;;
        span-generate)    operation_span_generate "$@" ;;
        firewall-apply)   operation_firewall_apply "$@" ;;
        certificate-apply) operation_certificate_apply "$@" ;;
        firewall-status)  operation_firewall_status "$@" ;;
        firewall-clear)   operation_firewall_clear "$@" ;;
        hostname-set)     operation_hostname_set "$@" ;;
        timezone-set)     operation_timezone_set "$@" ;;
        time-synchronise) operation_time_synchronise "$@" ;;
        reboot)           operation_reboot "$@" ;;
        power-off)        operation_power_off "$@" ;;
        --help|-h|help)   usage; exit 0 ;;
        *)                refuse "the verb named ${verb} is not permitted" ;;
    esac
}

# Dispatch when run, stay quiet when sourced. Sourcing is how the suite gets at
# one operation at a time without a service manager, a real interface card, or
# an appliance underneath it; running is how the daemon uses it. The two are
# told apart by the ordinary shell idiom rather than by a switch, so there is
# nothing here that exists only for the tests.
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
    main "$@"
fi
