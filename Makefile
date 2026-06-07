.DEFAULT_GOAL := all

.PHONY: help all ci nox nox-all validate clean-dist dev build test coverage coverage-unit docs docs-html doctest typecheck clean changelog release publish-test publish stability stability-all stability-embedded repeat vm-health qemu-restart

# Bump component for `make release`. Override on the command line:
#   make release BUMP=minor
BUMP ?= patch

# Coverage target invoked by `validate`. `ci` overrides this to
# `coverage-unit` because GitHub Actions doesn't have the Vagrant VMs
# that integration/hops tests require.
COVERAGE_TARGET ?= coverage

COVERAGE_THRESHOLD := 88
# CI runs unit tests only (integration/hops markers need Vagrant VMs that
# don't exist in GitHub Actions), so the achievable threshold is lower.
CI_COVERAGE_THRESHOLD := 80

# Iteration count for `make repeat`. Override on the command line:
#   make repeat COUNT=50
COUNT ?= 10

# Iteration count for `make nox` / `make nox-all`. The shared COUNT default
# (10) is wrong for nox, so honor COUNT only when set explicitly on the
# command line; otherwise run the matrix once.
#   make nox-all COUNT=5
NOX_COUNT := $(if $(filter command line,$(origin COUNT)),$(COUNT),1)

# Iteration count for `make stability-embedded`. Default is 1 (a single pass)
# so a standalone embedded run doesn't hammer the Zephyr board. When driven
# from `make stability-all` the parent explicitly passes COUNT=10 (or whatever
# the user set on the command line), so this resolves to the right value then.
EMBEDDED_COUNT := $(if $(filter command line,$(origin COUNT)),$(COUNT),1)

# Iteration count for `make stability`. Default is 50 (soak run); honor COUNT
# only when explicitly passed on the command line so that the global COUNT ?= 10
# default never silently overrides the documented 50-iteration contract.
STABILITY_COUNT := $(if $(filter command line,$(origin COUNT)),$(COUNT),50)

# Iteration count for the tier-2 integration leg of `make stability-all`.
# Default is 10; honor COUNT only when explicitly passed on the command line.
INTEGRATION_COUNT := $(if $(filter command line,$(origin COUNT)),$(COUNT),10)

# Hard ceiling on the pytest invocation so a hung test (e.g. an integration
# test waiting on an unreachable VM) can't stall the pipeline indefinitely.
# Two things dominate wall time: Docker integration tests are pinned to one
# xdist worker (xdist_group) because they share /tmp/otto-docker/repo1/ on the
# parent and can't safely parallelize compose_up's `rm -rf` of the staging
# dir; and the embedded Zephyr tests are serialized per-device (one telnet
# client per console — see tests/integration/host/conftest.py). The heavy
# stability/soak tests are excluded from `coverage` (the `stability` marker)
# and run only via `make stability-all` / `stability-embedded`, so 6 min
# leaves comfortable headroom for slower runners.
# --kill-after escalates SIGTERM → SIGKILL if xdist workers don't drain.
PYTEST_TIMEOUT := 360s
TIMEOUT_CMD := timeout --foreground --kill-after=10s $(PYTEST_TIMEOUT)

all: ## Run full pipeline against the dev VM (includes integration tests)
	@$(MAKE) validate \
		&& $(MAKE) build

ci: ## Run pipeline without VM-dependent tests (used by GitHub Actions)
	@$(MAKE) validate COVERAGE_TARGET=coverage-unit \
		&& $(MAKE) build

changelog: ## Regenerate CHANGELOG.md from conventional commit history (Unreleased only — does not touch released sections)
	git-cliff -o CHANGELOG.md

release: ## Validate (typecheck + docs + FULL nox matrix across all Pythons, requires dev VM), regenerate changelog at the new version, bump version, then build dist (BUMP=patch|minor|major, default patch; or NEW_VERSION=X.Y.Z[rcN] for prereleases)
	@$(MAKE) clean-dist \
		&& $(MAKE) typecheck \
		&& $(MAKE) docs \
		&& OTTO_DETECT_ASYNCIO_LEAKS=1 $(MAKE) nox-all \
		&& NEW_VERSION="$${NEW_VERSION:-$$(bump-my-version show new_version --increment $(BUMP))}" \
		&& echo "Targeting v$$NEW_VERSION" \
		&& git-cliff --tag "v$$NEW_VERSION" -o CHANGELOG.md \
		&& git add CHANGELOG.md \
		&& bump-my-version bump --verbose --allow-dirty --new-version "$$NEW_VERSION" $(BUMP) \
		&& $(MAKE) build \
		&& echo \
		&& echo "Regenerated CHANGELOG.md, bumped version, tagged, and built dist/." \
		&& echo "Pushing the tag fires .github/workflows/release.yml, which" \
		&& echo "publishes to PyPI via OIDC (gated by the 'pypi' environment)." \
		&& echo "Push with:" \
		&& echo "    git push --follow-tags" \
		&& echo \
		&& echo "Manual fallbacks (require UV_PUBLISH_TOKEN in env):" \
		&& echo "    make publish-test    # upload dist/ to TestPyPI" \
		&& echo "    make publish         # upload dist/ to PyPI"

publish-test: ## Manual fallback: upload dist/ to TestPyPI (prefer dispatching release-testpypi.yml; requires UV_PUBLISH_TOKEN)
	uv publish \
		--publish-url https://test.pypi.org/legacy/ \
		--check-url   https://test.pypi.org/simple/

publish: ## Manual fallback: upload dist/ to PyPI — permanent (prefer pushing a v* tag to fire release.yml; requires UV_PUBLISH_TOKEN)
	uv publish \
		--check-url https://pypi.org/simple/

nox: ## Run the default nox session matrix (unit tests across all supported Pythons + typecheck + docs). Override iterations with COUNT=N (default 1); JUnit XML lands in reports/junit/.
	uv run nox -- --count=$(NOX_COUNT) --repeat-scope=session

nox-all: ## Run the FULL test suite across all supported Pythons. Requires dev VM with Vagrant hosts up. Not used by CI. Override iterations with COUNT=N (default 1); JUnit XML lands in reports/junit/.
	uv run nox -s tests_all -- --count=$(NOX_COUNT) --repeat-scope=session

validate: ## Run validation (clean-dist, typecheck, coverage, docs) without building dist
	@$(MAKE) clean-dist \
		&& $(MAKE) typecheck \
		&& $(MAKE) $(COVERAGE_TARGET) \
		&& $(MAKE) docs

clean-dist:
	@rm -rf dist

dev:
	uv sync
	git config core.hooksPath .githooks
	@echo "Dev environment ready"

build: ## Build the project with uv
	uv build

test: ## Run tests (use TESTS= to filter)
	uv run pytest -k '$(TESTS)'

coverage: ## Run tests and enforce coverage threshold (excludes heavy `stability` tests — those run via `make stability-all`)
	$(TIMEOUT_CMD) uv run pytest -m "not stability" --cov-fail-under=$(COVERAGE_THRESHOLD)

coverage-unit: ## Run unit tests only (no Vagrant VMs needed) and enforce CI threshold
	$(TIMEOUT_CMD) uv run pytest tests/unit -m "not integration and not hops" --cov-fail-under=$(CI_COVERAGE_THRESHOLD)

stability: ## Run no-VM SessionManager concurrency/soak tests by marker. Override iterations with COUNT=N (default 50).
	OTTO_DETECT_ASYNCIO_LEAKS=1 uv run pytest \
	    -m concurrency \
	    --count=$(STABILITY_COUNT) \
	    -p no:cacheprovider

stability-all: ## Real telnet/SSH against Vagrant VMs. Runs all tests, even if unit-level tests are RED. Override iterations with COUNT=N (default 10).
	@echo "── Tier 1 (unit-level concurrency) ──"
	-@$(MAKE) stability COUNT=$(or $(COUNT),50)
	@echo
	@echo "── Tier 2 (real telnet/SSH) ──"
	@if command -v jq >/dev/null 2>&1; then \
	    reachable=0; total=0; \
	    for ip in $$(jq -r '.[].ip' tests/lab_data/tech1/hosts.json); do \
	        total=$$((total+1)); \
	        if ping -c 1 -W 1 $$ip >/dev/null 2>&1; then \
	            reachable=$$((reachable+1)); \
	        fi; \
	    done; \
	    if [ $$reachable -eq 0 ]; then \
	        echo "  WARNING: 0/$$total test VMs responded — run 'vagrant up' in the lab if tests fail at fixture connect."; \
	    else \
	        echo "  Reachable: $$reachable/$$total test VM(s)."; \
	    fi; \
	else \
	    echo "  jq not installed; skipping ping check (tests will fail fast at fixture connect if VMs are down)."; \
	fi
	OTTO_DETECT_ASYNCIO_LEAKS=1 uv run pytest \
	    -m "stability and integration and not embedded" \
	    --count=$(INTEGRATION_COUNT) \
	    -p no:cacheprovider \
	    -n0
	@echo
	@echo "── Tier 3 (cross-OS stability contract — includes embedded) ──"
	@$(MAKE) stability-embedded COUNT=$(or $(COUNT),10)

stability-embedded: ## Cross-OS stability contract against real telnet/SSH targets (unix + Zephyr). Requires Vagrant lab up. JUnit XML lands in reports/junit/. Override iterations with COUNT=N (default 1).
	@mkdir -p reports/junit
	OTTO_DETECT_ASYNCIO_LEAKS=1 uv run pytest \
	    -m "stability and embedded" \
	    -p no:cacheprovider \
	    -n0 \
	    --count=$(EMBEDDED_COUNT) \
	    --junitxml=reports/junit/stability-embedded.xml

repeat: ## Run the full unit suite (including integration) under pytest-repeat. Local only; requires VMs. Override COUNT=N (default 10).
	OTTO_DETECT_ASYNCIO_LEAKS=1 uv run pytest tests/unit \
	    --count=$(COUNT) \
	    -p no:cacheprovider

vm-health: ## Probe every lab VM + Zephyr QEMU instance; prints per-host timestamps + clock drift. Requires the Vagrant lab up.
	uv run python scripts/lab_health.py

qemu-restart: ## Restart the Zephyr QEMU + SNMP-relay units on the hop VM(s), then health-check. Use to recover a wedged embedded bed.
	uv run python scripts/lab_health.py --restart-qemu

typecheck: ## Run ty type checker (advisory during trial; not wired into `all`)
	uv run ty check

SPHINX_SRCS :=  docs/conf.py                        \
                $(shell find docs -name '*.rst')    \
                $(shell find docs -name '*.md')    \
                $(shell find src/otto -name '*.py') \

docs: docs-html doctest ## Build HTML docs and run doctests

docs-html: docs/_build/html/index.html ## Build HTML docs only (warnings are errors)

docs/_build/html/index.html: $(SPHINX_SRCS)
	uv run sphinx-build -W -b html docs/ docs/_build/html

doctest: ## Run Sphinx doctests
	uv run sphinx-build -b doctest docs/ docs/_build/doctest

clean: ## Remove all generated artifacts
	@rm -rf dist
	@rm -rf reports
	@rm -rf docs/_build

help: ## Show this help message
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2}'
