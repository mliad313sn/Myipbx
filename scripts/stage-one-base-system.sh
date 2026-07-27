#!/usr/bin/env bash
# Stage one -- the Linux root filesystem layer.
#
# Prepares the base system the remaining stages depend on: the build toolchain,
# kernel headers matched to the running kernel, the unprivileged service
# account, the directory tree, and the file permissions.
#
# Idempotent.  A completed stage records a receipt and is not repeated unless a
# re-run is demanded.

set -o errexit
set -o nounset
set -o pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/lib/common.sh
source "${SCRIPT_DIR}/lib/common.sh"

STAGE="stage-one-base-system"

BUILD_PACKAGES_APT=(build-essential linux-headers-"$(uname -r)" libnewt-dev libssl-dev
                    libncurses5-dev libsqlite3-dev libjansson-dev uuid-dev
                    libxml2-dev libedit-dev pkg-config wget curl python3)
BUILD_PACKAGES_RPM=(gcc gcc-c++ make kernel-devel newt-devel openssl-devel
                    ncurses-devel sqlite-devel jansson-devel libuuid-devel
                    libxml2-devel libedit-devel pkgconfig wget curl python3)

install_build_toolchain() {
    local manager
    manager="$(detect_package_manager)"
    log_info "the detected package manager is ${manager}"

    case "${manager}" in
        apt)
            run_command apt-get update
            run_command apt-get install --yes --no-install-recommends "${BUILD_PACKAGES_APT[@]}"
            ;;
        dnf)
            run_command dnf install -y "${BUILD_PACKAGES_RPM[@]}"
            ;;
        yum)
            run_command yum install -y "${BUILD_PACKAGES_RPM[@]}"
            ;;
        zypper)
            run_command zypper --non-interactive install "${BUILD_PACKAGES_RPM[@]}"
            ;;
        *)
            log_warn "the package manager was not recognised; the build toolchain must be installed by hand"
            log_warn "the required components are a compiler, the make utility, and the kernel headers for the running kernel"
            ;;
    esac
}

verify_kernel_headers() {
    local release
    release="$(uname -r)"
    local candidates=("/lib/modules/${release}/build" "/usr/src/linux-headers-${release}" "/usr/src/kernels/${release}")

    local candidate
    for candidate in "${candidates[@]}"; do
        if [[ -d "${candidate}" ]]; then
            log_info "kernel headers for the running kernel were found at ${candidate}"
            return 0
        fi
    done

    if is_rehearsal; then
        log_warn "rehearsal: kernel headers were not found, which would block the interface driver compilation"
        return 0
    fi
    fail "kernel headers matching the running kernel release ${release} were not found; the legacy interface card drivers cannot be compiled without them"
}

create_service_account() {
    if id -u "${APPLIANCE_USER}" >/dev/null 2>&1; then
        log_info "the service account named ${APPLIANCE_USER} already exists"
        return 0
    fi
    log_info "creating the unprivileged service account named ${APPLIANCE_USER}"
    run_command useradd --system --home-dir "${APPLIANCE_PREFIX}" \
        --shell /usr/sbin/nologin --comment "${APPLIANCE_NAME} control plane" \
        "${APPLIANCE_USER}"
}

create_directory_tree() {
    ensure_directory "${APPLIANCE_PREFIX}" 0755
    ensure_directory "${APPLIANCE_PREFIX}/web" 0755
    ensure_directory "${APPLIANCE_CONFIG_DIR}" 0750
    ensure_directory "${APPLIANCE_STATE_DIR}" 0750
    ensure_directory "${APPLIANCE_RECEIPT_DIR}" 0750
    ensure_directory "${APPLIANCE_STATE_DIR}/backups" 0750
    ensure_directory "${APPLIANCE_LOG_DIR}" 0750

    if ! is_rehearsal && id -u "${APPLIANCE_USER}" >/dev/null 2>&1; then
        run_command chown -R "${APPLIANCE_USER}:${APPLIANCE_USER}" \
            "${APPLIANCE_STATE_DIR}" "${APPLIANCE_LOG_DIR}" "${APPLIANCE_CONFIG_DIR}"
    fi
}

verify_interpreter() {
    if ! have_command python3; then
        fail "the control plane requires the python three interpreter, which is not installed"
    fi
    local version
    version="$(python3 -c 'import sys; print(str(sys.version_info[0]) + "." + str(sys.version_info[1]))')"
    log_info "the interpreter version present on this machine is ${version}"

    if ! python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 8) else 1)'; then
        fail "the control plane requires the interpreter at version three point eight or later"
    fi
}

main() {
    banner "stage one -- the base system"
    require_root

    if skip_if_completed "${STAGE}"; then
        return 0
    fi

    log_step "installing the build toolchain"
    install_build_toolchain

    log_step "verifying the kernel headers"
    verify_kernel_headers

    log_step "verifying the interpreter"
    verify_interpreter

    log_step "creating the service account"
    create_service_account

    log_step "creating the directory tree"
    create_directory_tree

    mark_stage_completed "${STAGE}" "the base system is prepared"
    log_info "stage one is complete"
}

main "$@"
