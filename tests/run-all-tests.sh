#!/usr/bin/env bash
# Runs the complete appliance test suite.
#
# The suite needs nothing that is not already on the appliance: the standard
# library interpreter, and optionally a browser scripting runtime for the test
# that proves the two numeral spelling implementations agree.  When that
# runtime is absent, the agreement test reports itself as skipped and the rest
# of the suite runs normally.

set -o errexit
set -o nounset
set -o pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPOSITORY_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

export PYTHONPATH="${REPOSITORY_ROOT}:${SCRIPT_DIR}${PYTHONPATH:+:${PYTHONPATH}}"
export PYTHONDONTWRITEBYTECODE=1

PATTERN="${1:-test_*.py}"
VERBOSITY="${VERBOSITY:-1}"

printf '\n'
printf '  Legacy-to-Modern IPBX Appliance -- quality assurance suite\n'
printf '  running the pattern %s\n' "${PATTERN}"
printf '\n'

# -- syntax gates ----------------------------------------------------------
# A syntax fault in a shell script would otherwise only be discovered by an
# operator midway through an installation, so it is caught here first.

printf '  checking the shell scripts\n'
for script in "${REPOSITORY_ROOT}"/scripts/*.sh "${REPOSITORY_ROOT}"/scripts/lib/*.sh \
            "${REPOSITORY_ROOT}"/iso/*.sh "${REPOSITORY_ROOT}"/iso/lib/*.sh "${REPOSITORY_ROOT}"/iso/stages/*.sh; do
    [[ -f "${script}" ]] || continue
    bash -n "${script}" || { printf '  the script at %s has a syntax fault\n' "${script}"; exit 1; }
done

printf '  checking the control plane sources\n'
python3 -m compileall -q "${REPOSITORY_ROOT}/appliance" >/dev/null

if command -v node >/dev/null 2>&1; then
    printf '  checking the dashboard sources\n'
    for asset in "${REPOSITORY_ROOT}"/web/js/*.js; do
        [[ -f "${asset}" ]] || continue
        node --check "${asset}" || { printf '  the file at %s has a syntax fault\n' "${asset}"; exit 1; }
    done
else
    printf '  the browser scripting runtime is absent; the dashboard syntax check was skipped\n'
fi

# -- the suite --------------------------------------------------------------

printf '\n'
cd "${REPOSITORY_ROOT}"
python3 -m unittest discover --start-directory tests --pattern "${PATTERN}" \
    --verbose 2>&1 | tail -n "${TAIL_LINES:-40}"

printf '\n  the suite completed\n\n'
