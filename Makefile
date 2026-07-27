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
check-shell: ## check every shell script in the repository for a syntax fault
	@# Found rather than listed. A list of directories has to be extended by
	@# whoever adds a new one, and the disk installer was written into a new
	@# directory and went unchecked until somebody noticed.
	@find scripts iso tests -type f -name '*.sh' -print | sort | while read -r script; do \
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

.PHONY: lint
lint: ## static analysis of the console sources
	@if command -v eslint >/dev/null 2>&1; then \
		NODE_PATH=$${NODE_PATH:-/opt/node22/lib/node_modules} eslint web/js tests/browser; \
		printf '  the console sources pass static analysis\n'; \
	else \
		printf '  the static analysis tool is not installed; this check was skipped\n'; \
	fi

.PHONY: test-browser
test-browser: ## drive the real console in a real browser
	@$(PYTHON) -m unittest test_browser --verbose

.PHONY: image
image: ## build the bootable appliance image, requires administrative privilege
	@bash iso/build-iso.sh

.PHONY: image-rehearse
image-rehearse: ## report what the image build would do, changing nothing
	@bash iso/build-iso.sh --rehearse

.PHONY: image-boot-test
image-boot-test: ## start the finished image in an emulator and prove it boots
	@bash iso/boot-test.sh

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
