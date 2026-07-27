#!/usr/bin/env bash
# Stage four -- the telephony engine.
#
# Builds and installs the engine against the interface drivers installed by
# stage three, installs the generated configuration, and binds the manager
# interface to the loopback address only.
#
# The manager interface is the control plane's sole channel to the engine.  The
# control plane runs on this same machine, so exposing that interface to the
# network would add risk and no capability.

set -o errexit
set -o nounset
set -o pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/lib/common.sh
source "${SCRIPT_DIR}/lib/common.sh"

STAGE="stage-four-asterisk"

APPLIANCE_SOURCE_DIR="${APPLIANCE_SOURCE_DIR:-/usr/local/src/myipbx}"
APPLIANCE_BUILD_DIR="${APPLIANCE_BUILD_DIR:-/usr/local/src/myipbx/build}"
ENGINE_ARCHIVE="${ENGINE_ARCHIVE:-}"
ENGINE_ARCHIVE_URL="${ENGINE_ARCHIVE_URL:-}"
ASTERISK_CONFIG_DIR="${ASTERISK_CONFIG_DIR:-/etc/asterisk}"
TEMPLATE_DIR="${SCRIPT_DIR}/../config/asterisk"

resolve_engine_archive() {
    if [[ -n "${ENGINE_ARCHIVE}" ]]; then
        [[ -f "${ENGINE_ARCHIVE}" ]] || fail "the engine archive at ${ENGINE_ARCHIVE} does not exist"
        printf '%s' "${ENGINE_ARCHIVE}"
        return 0
    fi

    local candidate
    candidate="$(find "${APPLIANCE_SOURCE_DIR}" -maxdepth 1 -name 'asterisk*.tar.gz' -type f 2>/dev/null | sort | tail -n 1)"
    if [[ -n "${candidate}" ]]; then
        printf '%s' "${candidate}"
        return 0
    fi

    if [[ -n "${ENGINE_ARCHIVE_URL}" ]]; then
        ensure_directory "${APPLIANCE_SOURCE_DIR}"
        local target="${APPLIANCE_SOURCE_DIR}/$(basename "${ENGINE_ARCHIVE_URL}")"
        log_info "fetching the telephony engine archive"
        run_command curl --location --fail --output "${target}" "${ENGINE_ARCHIVE_URL}"
        printf '%s' "${target}"
        return 0
    fi

    fail "the telephony engine archive was not found in ${APPLIANCE_SOURCE_DIR} and no download address was supplied"
}

verify_driver_present() {
    if [[ -e /dev/dahdi/ctl ]] || lsmod 2>/dev/null | grep -q '^dahdi'; then
        log_info "the interface driver is present, so the engine will be built with legacy interface card support"
        return 0
    fi
    log_warn "the interface driver is not loaded; the engine will be built without legacy interface card support"
    log_warn "run stage three first if this machine has interface cards fitted"
    return 0
}

build_engine() {
    local archive="$1"
    ensure_directory "${APPLIANCE_BUILD_DIR}"

    log_step "extracting the telephony engine sources"
    run_command tar --extract --file "${archive}" --directory "${APPLIANCE_BUILD_DIR}"

    if is_rehearsal; then
        log_info "rehearsal: the telephony engine would be configured, built, and installed"
        return 0
    fi

    local tree
    tree="$(find "${APPLIANCE_BUILD_DIR}" -maxdepth 1 -mindepth 1 -type d -name 'asterisk-*' | sort | tail -n 1)"
    [[ -n "${tree}" ]] || fail "the engine archive did not extract into a source directory"

    local parallelism
    parallelism="$(getconf _NPROCESSORS_ONLN 2>/dev/null || printf '1')"

    log_step "configuring the telephony engine"
    ( cd "${tree}" && ./configure --with-pjproject-bundled --with-jansson-bundled ) \
        || fail "the telephony engine could not be configured; a development library is most likely missing"

    log_step "selecting the modules to build"
    ( cd "${tree}" && make menuselect.makeopts \
        && menuselect/menuselect --enable chan_dahdi --enable res_pjsip \
             --enable app_voicemail --enable format_wav menuselect.makeopts ) \
        || log_warn "the module selection step reported a problem; the default selection will be used"

    log_step "building the telephony engine with $(spell_integer "${parallelism}") parallel job or jobs"
    ( cd "${tree}" && make -j"${parallelism}" ) || fail "the telephony engine did not build"

    log_step "installing the telephony engine"
    ( cd "${tree}" && make install ) || fail "the telephony engine could not be installed"
}

install_configuration_templates() {
    log_step "installing the engine configuration"
    ensure_directory "${ASTERISK_CONFIG_DIR}" 0750

    if [[ ! -d "${TEMPLATE_DIR}" ]]; then
        log_warn "no configuration templates were found at ${TEMPLATE_DIR}; the control plane will render the configuration on first run"
        return 0
    fi

    local template
    for template in "${TEMPLATE_DIR}"/*.conf; do
        [[ -f "${template}" ]] || continue
        local name
        name="$(basename "${template}")"
        if [[ -f "${ASTERISK_CONFIG_DIR}/${name}" ]]; then
            log_info "the file named ${name} already exists and will not be replaced; the control plane reconciles it instead"
            continue
        fi
        install_file "${template}" "${ASTERISK_CONFIG_DIR}/${name}" 0640
    done
}

configure_manager_secret() {
    log_step "generating the manager interface credential"

    local secret_file="${APPLIANCE_CONFIG_DIR}/manager.secret"
    if [[ -f "${secret_file}" ]]; then
        log_info "a manager interface credential already exists and will be reused"
        return 0
    fi

    if is_rehearsal; then
        log_info "rehearsal: a manager interface credential would be generated"
        return 0
    fi

    ensure_directory "${APPLIANCE_CONFIG_DIR}" 0750
    local secret
    secret="$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')"
    printf '%s\n' "${secret}" >"${secret_file}"
    run_command chmod 0600 "${secret_file}"
    log_info "a manager interface credential was generated and stored with owner only permissions"
}

main() {
    banner "stage four -- the telephony engine"
    require_root

    if skip_if_completed "${STAGE}"; then
        return 0
    fi

    verify_driver_present

    local archive
    archive="$(resolve_engine_archive)"
    build_engine "${archive}"

    install_configuration_templates
    configure_manager_secret

    mark_stage_completed "${STAGE}" "the telephony engine is built, installed, and configured"
    log_info "stage four is complete"
}

main "$@"
