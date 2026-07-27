# Legacy-to-Modern IPBX Appliance
#
# Nothing here fetches anything.  The control plane depends on the standard
# library alone, so there is no dependency step to run and no package index to
# reach, which is what makes the appliance installable on an air gapped machine.

SHELL := /usr/bin/env bash
PYTHON ?= python3
REPOSITORY_ROOT := $(shell pwd)
export PYTHONPATH := $(REPOSITORY_ROOT):$(REPOSITORY_ROOT)/tests
export PYTHONDONTWRITEBYTECODE := 1

.DEFAULT_GOAL := help

.PHONY: help
help: ## show this message
	@printf '\n  Legacy-to-Modern IPBX Appliance\n\n'
	@grep -E '^[a-zA-Z0-9_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  %-22s %s\n", $$1, $$2}'
	@printf '\n  this appliance assigns no addresses and spells every numeral in full letters\n\n'

.PHONY: test
test: ## run the complete quality assurance suite
	@$(PYTHON) -m unittest discover --start-directory tests --pattern 'test_*.py'

.PHONY: test-verbose
test-verbose: ## run the suite naming every test
	@$(PYTHON) -m unittest discover --start-directory tests --pattern 'test_*.py' --verbose

.PHONY: test-constraints
test-constraints: ## run only the two absolute constraint suites
	@$(PYTHON) -m unittest discover --start-directory tests --pattern 'test_constraint_one.py' --verbose
	@$(PYTHON) -m unittest discover --start-directory tests --pattern 'test_numerals.py' --verbose

.PHONY: test-concurrency
test-concurrency: ## run only the one hundred concurrent session scenarios
	@$(PYTHON) -m unittest test_integration.ConcurrencyTests --verbose

.PHONY: check
check: check-shell check-python check-browser ## run every syntax gate

.PHONY: check-shell
check-shell: ## check every staging script for a syntax fault
	@for script in scripts/*.sh scripts/lib/*.sh tests/*.sh; do \
		[ -f "$$script" ] || continue; \
		bash -n "$$script" || exit 1; \
		printf '  the script at %s is well formed\n' "$$script"; \
	done

.PHONY: check-python
check-python: ## compile every control plane source
	@$(PYTHON) -m compileall -q appliance tests
	@printf '  the control plane sources compile\n'

.PHONY: check-browser
check-browser: ## check every dashboard source for a syntax fault
	@if command -v node >/dev/null 2>&1; then \
		for asset in web/js/*.js; do \
			node --check "$$asset" || exit 1; \
			printf '  the file at %s is well formed\n' "$$asset"; \
		done; \
	else \
		printf '  the browser scripting runtime is absent; this check was skipped\n'; \
	fi

.PHONY: audit
audit: ## run the address allocation exclusion audit against this machine
	@bash scripts/verify-no-dhcp.sh

.PHONY: rehearse
rehearse: ## rehearse the installation without changing anything
	@bash scripts/install-appliance.sh --rehearse

.PHONY: install
install: ## install the appliance on this machine, requires administrative privilege
	@bash scripts/install-appliance.sh

.PHONY: run
run: ## run the control plane in the foreground from the repository
	@$(PYTHON) -m appliance --web-root ./web --log-level DEBUG

.PHONY: clean
clean: ## remove compiled artefacts
	@find . -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
	@find . -name '*.pyc' -delete 2>/dev/null || true
	@printf '  the compiled artefacts were removed\n'
