.DEFAULT_GOAL := all

.PHONY: help all ci nox nox-unit nox-unix nox-embedded validate clean-dist dev build test coverage coverage-unit coverage-unix coverage-embedded docs docs-html doctest typecheck clean changelog release publish-test publish stability stability-unit stability-unix stability-embedded repeat vm-health qemu-restart

# Bump component for `make release`. Override on the command line:
#   make release BUMP=minor
BUMP ?= patch

# Coverage target invoked by `validate`. `ci` overrides this to
# `coverage-unit` because GitHub Actions doesn't have the Vagrant VMs
# that integration/hops tests require.
COVERAGE_TARGET ?= coverage

COVERAGE_THRESHOLD := 90
# CI runs unit tests only (integration/hops markers need Vagrant VMs that
# don't exist in GitHub Actions), so the achievable threshold is lower.
CI_COVERAGE_THRESHOLD := 80

# Iteration count for `make repeat`. Override on the command line:
#   make repeat COUNT=50
COUNT ?= 10

# Iteration count for the `nox-*` targets. The shared COUNT default (10) is
# wrong for nox, so honor COUNT only when set explicitly on the command line;
# otherwise run the matrix once.
#   make nox-unit COUNT=5
NOX_COUNT := $(if $(filter command line,$(origin COUNT)),$(COUNT),1)

# Iteration count for `make stability-embedded`. Default is 1 (a single pass)
# so a standalone embedded run doesn't hammer the Zephyr board. When driven
# from `make stability` the parent explicitly passes COUNT=10 (or whatever the
# user set on the command line), so this resolves to the right value then.
STABILITY_EMBEDDED_COUNT := $(if $(filter command line,$(origin COUNT)),$(COUNT),1)

# Iteration count for `make stability-unit`. Default is 50 (soak run); honor
# COUNT only when explicitly passed on the command line so that the global
# COUNT ?= 10 default never silently overrides the documented 50-iteration
# contract.
STABILITY_UNIT_COUNT := $(if $(filter command line,$(origin COUNT)),$(COUNT),50)

# Iteration count for the Unix-VM leg `make stability-unix`. Default is 10;
# honor COUNT only when explicitly passed on the command line.
STABILITY_UNIX_COUNT := $(if $(filter command line,$(origin COUNT)),$(COUNT),10)

# Shared pytest marker expressions for the test "environment" axis, reused by
# the coverage-* targets (the nox-* sessions encode the same expressions in
# noxfile.py). Keep the two in sync.
#   unit     — no VM (mocked transports)
#   unix     — real telnet/SSH against the Linux Vagrant VMs (incl. multi-hop)
#   embedded — Zephyr/QEMU under the zephyr VM
M_UNIT := not integration
M_UNIX := integration and not embedded
M_EMBEDDED := embedded

# Hard ceiling on the pytest invocation so a hung test (e.g. an integration
# test waiting on an unreachable VM) can't stall the pipeline indefinitely.
# Two things dominate wall time: Docker integration tests are pinned to one
# xdist worker (xdist_group) because they share /tmp/otto-docker/repo1/ on the
# parent and can't safely parallelize compose_up's `rm -rf` of the staging
# dir; and the embedded Zephyr tests are serialized per-device (one telnet
# client per console — see tests/integration/host/conftest.py). The heavy
# stability/soak tests are excluded from `coverage` (the `stability` marker)
# and run only via `make stability` / `stability-embedded`, so 6 min
# leaves comfortable headroom for slower runners.
# --kill-after escalates SIGTERM → SIGKILL if xdist workers don't drain.
PYTEST_TIMEOUT := 360s
TIMEOUT_CMD := timeout --foreground --kill-after=10s $(PYTEST_TIMEOUT)

# JUnit XML output. Every test target writes into its own subdirectory of
# reports/junit/ named after the target, so runs never clobber each other and
# `make clean` (rm -rf reports) removes them all. pytest creates the parent
# directory for --junitxml, so no mkdir is needed. The nox-* targets encode the
# same layout in noxfile.py (_junitxml). Usage: $(call junitxml,coverage-unit)
JUNIT_DIR := reports/junit
junitxml = --junitxml=$(JUNIT_DIR)/$(1)/$(1).xml

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
		&& OTTO_DETECT_ASYNCIO_LEAKS=1 $(MAKE) nox \
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

nox-unit: ## Run the unit suite across all supported Pythons (no VMs). Fastest safe test. Override iterations with COUNT=N (default 1); JUnit XML lands in reports/junit/nox-unit/.
	uv run nox -s tests_unit -- --count=$(NOX_COUNT) --repeat-scope=session

nox-unix: ## Run the Unix-VM integration suite (incl. multi-hop) across all supported Pythons. Requires dev VM with Vagrant hosts up. Override COUNT=N (default 1); JUnit XML in reports/junit/nox-unix/.
	uv run nox -s tests_unix -- --count=$(NOX_COUNT) --repeat-scope=session

nox-embedded: ## Run the embedded (Zephyr) suite across all supported Pythons. Requires Vagrant lab up. Override COUNT=N (default 1); JUnit XML in reports/junit/nox-embedded/.
	uv run nox -s tests_embedded -- --count=$(NOX_COUNT) --repeat-scope=session

nox: ## Run the FULL test suite (all environments) across all supported Pythons. Requires dev VM with Vagrant hosts up. Not used by CI. Override COUNT=N (default 1); JUnit XML in reports/junit/nox/.
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

test: ## Run tests (use TESTS= to filter). JUnit XML lands in reports/junit/test/.
	uv run pytest -k '$(TESTS)' $(call junitxml,test)

coverage: ## Run the pinned-Python suite and enforce the coverage gate (excludes heavy `stability` tests — those run via `make stability`). JUnit XML lands in reports/junit/coverage/.
	$(TIMEOUT_CMD) uv run pytest -m "not stability" --cov-fail-under=$(COVERAGE_THRESHOLD) $(call junitxml,coverage)

coverage-unit: ## Run the pinned-Python unit suite (no Vagrant VMs) and enforce the CI coverage gate. JUnit XML lands in reports/junit/coverage-unit/.
	$(TIMEOUT_CMD) uv run pytest tests/unit -m "$(M_UNIT)" --cov-fail-under=$(CI_COVERAGE_THRESHOLD) $(call junitxml,coverage-unit)

coverage-unix: ## Run the pinned-Python Unix-VM integration suite (incl. multi-hop) with a coverage report (no gate — one env can't meet the whole-repo threshold). Requires lab VMs. JUnit XML in reports/junit/coverage-unix/.
	$(TIMEOUT_CMD) uv run pytest -m "$(M_UNIX)" $(call junitxml,coverage-unix)

coverage-embedded: ## Run the pinned-Python embedded (Zephyr) suite with a coverage report (no gate). Requires Vagrant lab up. JUnit XML in reports/junit/coverage-embedded/.
	$(TIMEOUT_CMD) uv run pytest -m "$(M_EMBEDDED)" $(call junitxml,coverage-embedded)

# Soak/stability + repeat targets disable coverage (--no-cov, overriding the
# --cov in pytest addopts). Per-test `--cov-context=test` tracing adds overhead
# to every one of the COUNT-multiplied iterations and, on slow CI runners under
# xdist, helps push tight per-test timeouts over their wall-clock budget. These
# runs exist to flush flakes, not to measure coverage — that's `make coverage`.
stability-unit: ## Run no-VM SessionManager concurrency/soak tests by marker. JUnit XML lands in reports/junit/stability-unit/. Override iterations with COUNT=N (default 50).
	OTTO_DETECT_ASYNCIO_LEAKS=1 uv run pytest \
	    -m concurrency \
	    --count=$(STABILITY_UNIT_COUNT) \
	    -p no:cacheprovider \
	    --no-cov \
	    $(call junitxml,stability-unit)

stability-unix: ## Real telnet/SSH soak against the Unix Vagrant VMs (incl. multi-hop). Requires lab VMs. JUnit XML in reports/junit/stability-unix/. Override iterations with COUNT=N (default 10).
	OTTO_DETECT_ASYNCIO_LEAKS=1 uv run pytest \
	    -m "stability and integration and not embedded" \
	    --count=$(STABILITY_UNIX_COUNT) \
	    -p no:cacheprovider \
	    --no-cov \
	    $(call junitxml,stability-unix)

stability-embedded: ## Cross-OS stability contract against real telnet/SSH targets (Zephyr). Requires Vagrant lab up. JUnit XML lands in reports/junit/stability-embedded/. Override iterations with COUNT=N (default 1).
	OTTO_DETECT_ASYNCIO_LEAKS=1 uv run pytest \
	    -m "stability and embedded" \
	    -p no:cacheprovider \
	    --no-cov \
	    --count=$(STABILITY_EMBEDDED_COUNT) \
	    $(call junitxml,stability-embedded)

stability: ## Run the full stability/soak suite: no-VM concurrency, then real telnet/SSH (Unix + embedded). Runs all tiers even if an earlier one is RED. Requires lab VMs for tiers 2-3. Override iterations with COUNT=N.
	@echo "── Tier 1 (unit-level concurrency) ──"
	-@$(MAKE) stability-unit COUNT=$(COUNT)
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
	@$(MAKE) stability-unix COUNT=$(COUNT)
	@echo
	@echo "── Tier 3 (cross-OS stability contract — includes embedded) ──"
	@$(MAKE) stability-embedded COUNT=$(COUNT)

repeat: ## Run the full unit suite (including integration) under pytest-repeat. Local only; requires VMs. JUnit XML in reports/junit/repeat/. Override COUNT=N (default 10).
	OTTO_DETECT_ASYNCIO_LEAKS=1 uv run pytest tests/unit \
	    --count=$(COUNT) \
	    -p no:cacheprovider \
	    --no-cov \
	    $(call junitxml,repeat)

vm-health: ## Probe every lab VM + Zephyr QEMU instance; prints per-host timestamps + clock drift. Requires the Vagrant lab up.
	uv run python scripts/lab_health.py

qemu-restart: ## Restart the Zephyr QEMU + SNMP-relay units on the hop VM(s), then health-check. Use to recover a wedged embedded bed.
	uv run python scripts/lab_health.py --restart-qemu

typecheck: ## Run ty type checker (advisory during trial; not wired into `all`)
	uv run ty check

schema: ## Generate JSON Schema for hosts.json / settings.toml / reservations into schemas/ (git-ignored; for editor autocomplete)
	uv run otto schema export --out schemas

SPHINX_SRCS :=  docs/conf.py                        \
                $(shell find docs -name '*.rst')    \
                $(shell find docs -name '*.md')    \
                $(shell find src/otto -name '*.py') \

docs: docs-lint docs-html doctest ## Build HTML docs and run doctests

docs-lint: ## Fast RST structural lint (doc8) — catches title/underline desync without a full sphinx build
	uv run doc8 docs/

docs-html: docs/_build/html/index.html ## Build HTML docs only (warnings are errors)

# -E (fresh env, no stale doctrees) + -a (write all) make a local build match
# CI's clean build, so incremental state can't mask or invent a warning.
docs/_build/html/index.html: $(SPHINX_SRCS)
	uv run sphinx-build -E -a -W -b html docs/ docs/_build/html

doctest: ## Run Sphinx doctests
	uv run sphinx-build -E -b doctest docs/ docs/_build/doctest

clean: ## Remove all generated artifacts
	@rm -rf dist
	@rm -rf reports
	@rm -rf docs/_build
	@# Reset the embedded-gcov submodule(s) to pristine. This discards the
	@# gcc-12+ patch that product/build.sh applies; that patch is tracked
	@# (tests/repo3/third_party/patches/) and re-applied idempotently on the
	@# next build, so resetting here keeps the submodule from drifting between
	@# builds (a stale patch/build is what desyncs .gcno and trips gcov's
	@# "stamp mismatch").
	@git submodule foreach --recursive 'git reset --hard && git clean -fdx'

help: ## Show this help message
	@printf '\n\033[1mTesting\033[0m  (COUNT=N overrides iterations; omit the scope to run all environments)\n'
	@printf '  unit = no VMs (fast)  ·  unix = Linux VMs (incl. hops)  ·  embedded = Zephyr\n'
	@printf '  \033[36m%-31s\033[0m %s\n' 'nox-{unit,unix,embedded}'       'multi-Python matrix        (nox = all envs)'
	@printf '  \033[36m%-31s\033[0m %s\n' 'coverage-{unit,unix,embedded}'  'pinned Python + coverage   (coverage = all, gated)'
	@printf '  \033[36m%-31s\033[0m %s\n' 'stability-{unit,unix,embedded}' 'pinned pytest-repeat soak  (stability = all tiers)'
	@printf '  \033[36m%-31s\033[0m %s\n' 'test TESTS=<kw>' 'filter any run by keyword'
	@printf '  \033[36m%-31s\033[0m %s\n' 'repeat'          'soak the full unit suite (pytest-repeat)'
	@printf '\n\033[1mOther targets\033[0m\n'
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| grep -vE '^(nox|coverage|stability)(-(unit|unix|embedded))?:|^(test|repeat):' \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2}'
