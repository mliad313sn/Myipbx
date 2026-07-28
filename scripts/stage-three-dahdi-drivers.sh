#!/usr/bin/env bash
# Stage three -- the legacy Digium hardware bridging layer.
#
# Compiles the interface card kernel drivers directly against the running
# kernel, installs them, loads them, and persists the module configuration so
# the cards come back after a restart.
#
# This is the stage the market benchmark identified as the unguided manual
# ritual.  Every step here is announced before it runs, is idempotent, reports
# its failure reason in plain language rather than as a bare module load error,
# and can be rehearsed without mutating the machine.
#
# Sources are expected to be present locally, because these appliances are
# frequently air gapped.  Set APPLIANCE_SOURCE_DIR to the directory holding the
# driver and tools archives, or set the download addresses to fetch them.

set -o errexit
set -o nounset
set -o pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/lib/common.sh
source "${SCRIPT_DIR}/lib/common.sh"

STAGE="stage-three-dahdi-drivers"

APPLIANCE_SOURCE_DIR="${APPLIANCE_SOURCE_DIR:-/usr/local/src/crossbar}"
APPLIANCE_BUILD_DIR="${APPLIANCE_BUILD_DIR:-/usr/local/src/crossbar/build}"
DRIVER_ARCHIVE="${DRIVER_ARCHIVE:-}"
TOOLS_ARCHIVE="${TOOLS_ARCHIVE:-}"
DRIVER_ARCHIVE_URL="${DRIVER_ARCHIVE_URL:-}"
TOOLS_ARCHIVE_URL="${TOOLS_ARCHIVE_URL:-}"

# Building from the development tree rather than from the last released
# archive is the default on a recent kernel, and the reason is specific.
#
# The legacy interface card drivers this appliance exists to support were
# removed from the driver project in twenty eighteen and restored after
# community pressure, the last of them landing in the release numbered three
# point four point zero in April of twenty twenty-four. No release has been
# tagged since. The fixes that let the drivers compile against kernels from
# six point ten onward -- the timer interface changes in particular -- exist
# only in the development tree.
#
# So: an appliance on an older kernel may use the released archive, and an
# appliance on a newer kernel must use the development tree, or the
# compilation in this stage will fail on a kernel interface that the release
# predates. The stage chooses for itself and says which it chose and why.
DRIVER_SOURCE_MODE="${DRIVER_SOURCE_MODE:-auto}"
DRIVER_GIT_URL="${DRIVER_GIT_URL:-https://github.com/asterisk/dahdi-linux.git}"
TOOLS_GIT_URL="${TOOLS_GIT_URL:-https://github.com/asterisk/dahdi-tools.git}"
DRIVER_GIT_REF="${DRIVER_GIT_REF:-master}"

#: The kernel release from which the released archive is known to fail.
_TREE_REQUIRED_MAJOR=6
_TREE_REQUIRED_MINOR=10

# The peripheral bus vendor identifier assigned to Digium.
DIGIUM_VENDOR="d161"

report_detected_cards() {
    log_step "enumerating the peripheral bus for legacy Digium interface cards"

    local found=0
    local device
    for device in /sys/bus/pci/devices/*; do
        [[ -r "${device}/vendor" ]] || continue
        local vendor
        vendor="$(tr -d '\n' <"${device}/vendor")"
        if [[ "${vendor}" == "0x${DIGIUM_VENDOR}" ]]; then
            found=$(( found + 1 ))
            local model
            model="$(tr -d '\n' <"${device}/device" 2>/dev/null || printf 'unknown')"
            log_info "a Digium interface card was detected in the slot named $(basename "${device}") bearing the device identifier ${model}"
        fi
    done

    if (( found == 0 )); then
        log_warn "no legacy Digium interface card was detected on the peripheral bus"
        log_warn "the drivers will still be built and installed so that a card added later is supported immediately"
    else
        log_info "$(spell_integer "${found}") Digium interface card or cards were detected"
    fi
    return 0
}

# Decide, and explain, whether to build from the development tree.
choose_source_mode() {
    local release major minor
    release="$(uname -r)"
    major="${release%%.*}"
    minor="${release#*.}"
    minor="${minor%%.*}"

    case "${DRIVER_SOURCE_MODE}" in
        git|tree)
            log_info "the development tree was requested explicitly"
            printf 'git'
            return 0
            ;;
        archive|release)
            log_info "the released archive was requested explicitly"
            printf 'archive'
            return 0
            ;;
    esac

    if ! [[ "${major}" =~ ^[0-9]+$ ]] || ! [[ "${minor}" =~ ^[0-9]+$ ]]; then
        log_warn "the kernel release could not be interpreted; the released archive will be used"
        printf 'archive'
        return 0
    fi

    if (( major > _TREE_REQUIRED_MAJOR )) \
        || (( major == _TREE_REQUIRED_MAJOR && minor >= _TREE_REQUIRED_MINOR )); then
        log_info "the running kernel is newer than the last tagged driver release"
        log_info "the development tree will be used, because the released archive predates the kernel interface changes this kernel requires"
        printf 'git'
        return 0
    fi

    log_info "the running kernel predates the kernel interface changes, so the released archive is sufficient"
    printf 'archive'
}

clone_source() {
    local url="$1"
    local destination="$2"
    local description="$3"

    require_command git
    ensure_directory "$(dirname "${destination}")"

    if is_rehearsal; then
        log_info "rehearsal: the ${description} tree would be fetched from ${url}"
        printf '%s' "${destination}"
        return 0
    fi

    if [[ -d "${destination}/.git" ]]; then
        log_info "updating the existing ${description} tree"
        ( cd "${destination}" && git fetch --depth 1 origin "${DRIVER_GIT_REF}" \
            && git checkout --force FETCH_HEAD ) \
            || fail "the ${description} tree could not be updated"
    else
        log_info "fetching the ${description} tree at the reference ${DRIVER_GIT_REF}"
        git clone --depth 1 --branch "${DRIVER_GIT_REF}" "${url}" "${destination}" \
            || fail "the ${description} tree could not be fetched; supply an archive instead if this machine has no route to it"
    fi

    printf '%s' "${destination}"
}

resolve_archive() {
    local explicit="$1"
    local url="$2"
    local pattern="$3"
    local description="$4"

    if [[ -n "${explicit}" ]]; then
        [[ -f "${explicit}" ]] || fail "the ${description} archive at ${explicit} does not exist"
        printf '%s' "${explicit}"
        return 0
    fi

    local candidate
    candidate="$(find "${APPLIANCE_SOURCE_DIR}" -maxdepth 1 -name "${pattern}" -type f 2>/dev/null | sort | tail -n 1)"
    if [[ -n "${candidate}" ]]; then
        printf '%s' "${candidate}"
        return 0
    fi

    if [[ -n "${url}" ]]; then
        ensure_directory "${APPLIANCE_SOURCE_DIR}"
        local target="${APPLIANCE_SOURCE_DIR}/$(basename "${url}")"
        log_info "fetching the ${description} archive"
        run_command curl --location --fail --output "${target}" "${url}"
        printf '%s' "${target}"
        return 0
    fi

    fail "the ${description} archive was not found in ${APPLIANCE_SOURCE_DIR} and no download address was supplied"
}

extract_archive() {
    local archive="$1"
    local destination="$2"

    ensure_directory "${destination}"
    log_info "extracting the archive at ${archive}"
    run_command tar --extract --file "${archive}" --directory "${destination}"

    if is_rehearsal; then
        printf '%s/rehearsal' "${destination}"
        return 0
    fi

    local extracted
    extracted="$(find "${destination}" -maxdepth 1 -mindepth 1 -type d -newer "${archive}" 2>/dev/null | sort | tail -n 1)"
    if [[ -z "${extracted}" ]]; then
        extracted="$(find "${destination}" -maxdepth 1 -mindepth 1 -type d | sort | tail -n 1)"
    fi
    [[ -n "${extracted}" ]] || fail "the archive at ${archive} did not extract into a directory"
    printf '%s' "${extracted}"
}

build_drivers() {
    local source_tree="$1"
    local release
    release="$(uname -r)"

    log_step "compiling the interface card drivers against the running kernel"
    log_info "the running kernel release is ${release}"

    if is_rehearsal; then
        log_info "rehearsal: the drivers would be compiled and installed from ${source_tree}"
        return 0
    fi

    local parallelism
    parallelism="$(getconf _NPROCESSORS_ONLN 2>/dev/null || printf '1')"
    log_info "building with $(spell_integer "${parallelism}") parallel job or jobs"

    # A compilation failure here is the single most common bring up failure, so
    # it is caught and explained rather than left as a wall of compiler output.
    if ! ( cd "${source_tree}" && make KVERS="${release}" -j"${parallelism}" ); then
        log_error "the interface card drivers did not compile against the running kernel"
        log_error "the usual causes are kernel headers that do not match the running kernel, a driver release that predates this kernel, or a missing compiler"
        fail "the driver compilation failed"
    fi

    ( cd "${source_tree}" && make install )
    ( cd "${source_tree}" && make config ) || log_warn "the driver configuration step reported a problem and was skipped"
}

build_tools() {
    local source_tree="$1"

    log_step "building the interface card tools"
    if is_rehearsal; then
        log_info "rehearsal: the tools would be built and installed from ${source_tree}"
        return 0
    fi

    if ! ( cd "${source_tree}" && ./configure && make && make install ); then
        fail "the interface card tools did not build"
    fi
}

load_and_persist_modules() {
    log_step "loading the interface driver and persisting the module configuration"

    if is_rehearsal; then
        log_info "rehearsal: the interface driver would be loaded and persisted"
        return 0
    fi

    if ! modprobe dahdi; then
        log_error "the interface driver refused to load"
        log_error "inspect the kernel message buffer for the reason; a driver built against a different kernel release is the most frequent cause"
        fail "the interface driver could not be loaded"
    fi
    log_info "the interface driver is loaded"

    local persist="/etc/modules-load.d/crossbar-dahdi.conf"
    ensure_directory "$(dirname "${persist}")"
    {
        printf '# %s -- interface driver modules loaded at start up\n' "${APPLIANCE_NAME}"
        printf 'dahdi\n'
        printf 'dahdi_transcode\n'
    } >"${persist}"
    log_info "the module configuration was persisted to ${persist}"
}

generate_span_configuration() {
    log_step "generating the span configuration from the detected hardware"

    if is_rehearsal; then
        log_info "rehearsal: the span configuration would be generated and applied"
        return 0
    fi

    if ! have_command dahdi_genconf; then
        log_warn "the span configuration generator is not available; map the spans from the dashboard instead"
        return 0
    fi

    if ! dahdi_genconf; then
        log_warn "the span configuration generator reported a problem"
        log_warn "this is expected on a machine with no interface card fitted"
        return 0
    fi

    if have_command dahdi_cfg; then
        dahdi_cfg -vv || log_warn "the span configuration could not be applied; inspect the generated configuration"
    fi

    if [[ -d /proc/dahdi ]]; then
        local span_count
        span_count="$(find /proc/dahdi -maxdepth 1 -type f 2>/dev/null | wc -l)"
        log_info "the interface driver now exports $(spell_integer "${span_count}") span or spans"
    fi
}

main() {
    banner "stage three -- the legacy Digium hardware bridging layer"
    require_root

    if skip_if_completed "${STAGE}"; then
        return 0
    fi

    report_detected_cards
    ensure_directory "${APPLIANCE_BUILD_DIR}"

    local mode driver_tree tools_tree
    mode="$(choose_source_mode)"

    if [[ "${mode}" == "git" ]]; then
        driver_tree="$(clone_source "${DRIVER_GIT_URL}" "${APPLIANCE_BUILD_DIR}/dahdi-linux" 'interface driver')"
        tools_tree="$(clone_source "${TOOLS_GIT_URL}" "${APPLIANCE_BUILD_DIR}/dahdi-tools" 'interface tools')"
    else
        local driver_archive tools_archive
        driver_archive="$(resolve_archive "${DRIVER_ARCHIVE}" "${DRIVER_ARCHIVE_URL}" 'dahdi-linux*.tar.gz' 'interface driver')"
        tools_archive="$(resolve_archive "${TOOLS_ARCHIVE}" "${TOOLS_ARCHIVE_URL}" 'dahdi-tools*.tar.gz' 'interface tools')"
        driver_tree="$(extract_archive "${driver_archive}" "${APPLIANCE_BUILD_DIR}")"
        tools_tree="$(extract_archive "${tools_archive}" "${APPLIANCE_BUILD_DIR}")"
    fi

    build_drivers "${driver_tree}"
    build_tools "${tools_tree}"

    load_and_persist_modules
    generate_span_configuration

    mark_stage_completed "${STAGE}" "the interface drivers are compiled, installed, and loaded"
    log_info "stage three is complete"
}

main "$@"
