#!/usr/bin/env bash
# Generates this appliance's own transport security certificate.
#
# The certificate is generated on the appliance and never travels with the
# image. An image is one file that many machines boot, so a certificate baked
# into one would hand every appliance built from it the same private key, and
# any operator holding the image could then read the console traffic of every
# site running it. That is a worse position than the plain transport this
# replaces, because it would look secured.
#
# So each appliance generates its own on first start, and this script is what
# does it. It is idempotent: an appliance that already holds a usable
# certificate and its matching key keeps them, which is what makes it safe to
# run from a service unit on every boot.
#
# Nothing here needs a package. The certificate is produced by the openssl
# command line tool, which is already present because the engine and the
# secured transport both depend on the library it belongs to.

set -o errexit
set -o nounset
set -o pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/lib/common.sh
source "${SCRIPT_DIR}/lib/common.sh"

APPLIANCE_TLS_DIR="${APPLIANCE_TLS_DIR:-${APPLIANCE_CONFIG_DIR}/tls}"
CERTIFICATE="${APPLIANCE_TLS_CERTIFICATE:-${APPLIANCE_TLS_DIR}/appliance.crt}"
PRIVATE_KEY="${APPLIANCE_TLS_PRIVATE_KEY:-${APPLIANCE_TLS_DIR}/appliance.key}"

# Ten years. This appliance is fitted once and then left alone for the life of
# the telephone system around it, and a certificate that expires during that
# life would take the console down on a morning nobody had planned for.
VALIDITY_DAYS="${APPLIANCE_TLS_VALIDITY_DAYS:-3652}"

# A certificate inside this window of its expiry is replaced rather than kept,
# so the renewal happens on a boot rather than on the day it lapses.
RENEW_WITHIN_SECONDS="${APPLIANCE_TLS_RENEW_WITHIN_SECONDS:-2592000}"

FORCE="no"

usage() {
    cat <<'EOF'
Usage: crossbar-generate-certificate.sh [--force]

Generates the appliance's transport security certificate if it does not
already hold a usable one. With --force, replaces whatever is there.

Applying a new certificate does not restart the control plane; the appliance
reads its certificate when it starts.
EOF
}

# ---------------------------------------------------------------------------
# What this appliance calls itself
# ---------------------------------------------------------------------------

configured_address() {
    # The address an operator types into a browser is the one the console is
    # bound to, so that is the name the certificate has to carry. It is read
    # from the configuration document rather than from the interface, because
    # a machine may hold several addresses and only one of them is the console.
    local document="${APPLIANCE_CONFIG_DIR}/appliance.json"
    local address=""

    if [[ -n "${APPLIANCE_LISTEN_ADDRESS:-}" ]]; then
        address="${APPLIANCE_LISTEN_ADDRESS}"
    elif [[ -r "${document}" ]]; then
        # Deliberately a text extraction rather than an interpreter. This runs
        # before the control plane on a cold boot, and the fewer things that
        # have to be working for the console to come up secured, the better.
        address="$(sed -n 's/.*"listen_address"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' \
            "${document}" | head -n 1)"
    fi

    # An appliance bound to every interface has no single address to be named
    # after, so the certificate is issued for the one it actually answers on.
    if [[ -z "${address}" || "${address}" == "0.0.0.0" || "${address}" == "::" ]]; then
        address="$(hostname -I 2>/dev/null | awk '{ print $1 }')"
    fi
    if [[ -z "${address}" ]]; then
        address="127.0.0.1"
    fi
    printf '%s' "${address}"
}

configured_hostname() {
    local name=""
    name="$(hostname 2>/dev/null || true)"
    if [[ -z "${name}" ]]; then
        name="crossbar"
    fi
    printf '%s' "${name}"
}

is_address() {
    [[ "$1" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]
}

# ---------------------------------------------------------------------------
# Idempotence
# ---------------------------------------------------------------------------

certificate_is_usable() {
    # Three separate questions, because a certificate can fail any one of them
    # on its own and each failure needs the same remedy.
    [[ -s "${CERTIFICATE}" && -s "${PRIVATE_KEY}" ]] || return 1

    openssl x509 -in "${CERTIFICATE}" -noout >/dev/null 2>&1 || return 1
    openssl x509 -in "${CERTIFICATE}" -noout -checkend "${RENEW_WITHIN_SECONDS}" \
        >/dev/null 2>&1 || return 1

    # And the pair has to belong together. A half completed manual
    # installation leaves a certificate beside somebody else's key, and the
    # control plane would refuse to start on it with no explanation of which
    # of the two files was wrong.
    local certificate_public key_public
    certificate_public="$(openssl x509 -in "${CERTIFICATE}" -noout -pubkey 2>/dev/null || true)"
    key_public="$(openssl pkey -in "${PRIVATE_KEY}" -pubout 2>/dev/null || true)"
    [[ -n "${certificate_public}" && "${certificate_public}" == "${key_public}" ]]
}

# ---------------------------------------------------------------------------
# Generation
# ---------------------------------------------------------------------------

write_openssl_configuration() {
    local path="$1"
    local address="$2"
    local host="$3"

    # The extensions are written into a configuration file rather than passed
    # as command line arguments, because the argument form for them arrived in
    # a later openssl than some of the machines this appliance is fitted to.
    {
        printf '[req]\n'
        printf 'distinguished_name = appliance_name\n'
        printf 'x509_extensions = appliance_extensions\n'
        printf 'prompt = no\n'
        printf '\n'
        printf '[appliance_name]\n'
        printf 'O = Crossbar\n'
        printf 'OU = %s\n' "${host}"
        printf 'CN = %s\n' "${address}"
        printf '\n'
        printf '[appliance_extensions]\n'
        printf 'basicConstraints = critical, CA:FALSE\n'
        printf 'keyUsage = critical, digitalSignature, keyEncipherment\n'
        printf 'extendedKeyUsage = serverAuth\n'
        printf 'subjectAltName = @appliance_names\n'
        printf '\n'
        printf '[appliance_names]\n'
        printf 'DNS.1 = %s\n' "${host}"
        printf 'DNS.2 = localhost\n'
        if is_address "${address}"; then
            printf 'IP.1 = %s\n' "${address}"
            printf 'IP.2 = 127.0.0.1\n'
        else
            printf 'DNS.3 = %s\n' "${address}"
            printf 'IP.1 = 127.0.0.1\n'
        fi
    } >"${path}"
}

generate_certificate() {
    local address="$1"
    local host="$2"

    ensure_directory "${APPLIANCE_TLS_DIR}" 0750

    local workspace
    workspace="$(mktemp -d)"
    # shellcheck disable=SC2064
    trap "rm -rf '${workspace}'" RETURN

    local configuration="${workspace}/openssl.cnf"
    write_openssl_configuration "${configuration}" "${address}" "${host}"

    local temporary_key="${workspace}/appliance.key"
    local temporary_certificate="${workspace}/appliance.crt"

    # An elliptic curve key first, because it is smaller and faster to
    # negotiate on hardware of the age this appliance is fitted to. Some older
    # openssl builds cannot generate one from these arguments, and on those the
    # appliance takes the larger key rather than no certificate at all.
    if ! openssl req -x509 -nodes \
            -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 \
            -keyout "${temporary_key}" \
            -out "${temporary_certificate}" \
            -days "${VALIDITY_DAYS}" \
            -sha256 \
            -config "${configuration}" \
            -extensions appliance_extensions >/dev/null 2>&1; then
        log_warn "this machine's openssl could not produce a curve key; falling back to the larger key"
        openssl req -x509 -nodes \
            -newkey rsa:4096 \
            -keyout "${temporary_key}" \
            -out "${temporary_certificate}" \
            -days "${VALIDITY_DAYS}" \
            -sha256 \
            -config "${configuration}" \
            -extensions appliance_extensions >/dev/null 2>&1 \
            || fail "the certificate could not be generated; inspect the openssl installation on this machine"
    fi

    # The permissions are set on the temporary files, so that the key is never
    # readable by anybody at any instant between being written and being moved
    # into place.
    #
    # The key is owned by root and readable by the appliance's group, and by
    # nobody else. It cannot be owner read only: the control plane runs as the
    # unprivileged appliance account and reads this file when it binds the
    # secured listener, and a key that account cannot open is a key that stops
    # the appliance from ever starting. The group holds only that one account,
    # which is the same arrangement the configuration document already uses.
    chmod 0640 "${temporary_key}"
    chmod 0644 "${temporary_certificate}"
    if id -u "${APPLIANCE_USER}" >/dev/null 2>&1; then
        chown "root:${APPLIANCE_USER}" "${temporary_key}"
        chown "root:${APPLIANCE_USER}" "${temporary_certificate}"
    fi

    mv -f "${temporary_key}" "${PRIVATE_KEY}"
    mv -f "${temporary_certificate}" "${CERTIFICATE}"

    if id -u "${APPLIANCE_USER}" >/dev/null 2>&1; then
        chown "root:${APPLIANCE_USER}" "${APPLIANCE_TLS_DIR}" 2>/dev/null || true
    fi
}

report_fingerprint() {
    local fingerprint
    fingerprint="$(openssl x509 -in "${CERTIFICATE}" -noout -fingerprint -sha256 2>/dev/null \
        | sed 's/^.*=//')"
    [[ -n "${fingerprint}" ]] || return 0

    # Printed rather than logged, and printed exactly as generated. Every other
    # line this script emits has its numerals spelled into words to satisfy
    # Constraint Two; a fingerprint put through that would no longer match the
    # one the browser shows, and matching them character by character is the
    # only thing a fingerprint is for. The initial administrator password is
    # printed for the same reason and in the same way.
    printf '\n'
    printf '  this appliance has its own certificate, which it signed itself.\n'
    printf '  the first browser to reach the console will warn. compare what it\n'
    printf '  shows against this fingerprint before accepting it:\n'
    printf '\n'
    printf '  %s\n' "${fingerprint}"
    printf '\n'
}

main() {
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --force) FORCE="yes"; shift ;;
            --help|-h) usage; return 0 ;;
            *) fail "the argument $1 is not recognised" ;;
        esac
    done

    require_root

    if ! have_command openssl; then
        fail "the openssl command line tool is not installed, so no certificate can be generated"
    fi

    if [[ "${FORCE}" != "yes" ]] && certificate_is_usable; then
        log_info "this appliance already holds a usable certificate and its matching key"
        return 0
    fi

    if is_rehearsal; then
        log_info "rehearsal: a certificate would be generated at ${CERTIFICATE}"
        return 0
    fi

    local address host
    address="$(configured_address)"
    host="$(configured_hostname)"

    log_step "generating this appliance's own certificate"
    log_info "the certificate will name the address ${address} and the host name ${host}"

    generate_certificate "${address}" "${host}"

    certificate_is_usable \
        || fail "the generated certificate did not verify against its own key"

    log_info "the certificate was written to ${CERTIFICATE}"
    log_info "the private key was written to ${PRIVATE_KEY} and is readable by root and by the appliance account only"
    report_fingerprint
}

main "$@"
