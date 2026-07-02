.DEFAULT_GOAL := all

.PHONY: help all ci nox nox-unit nox-integration nox-unix nox-embedded nox-hostless validate clean-dist dev build coverage coverage-unit coverage-integration coverage-unix coverage-embedded coverage-hostless docs docs-lint docs-html docs-inventories doctest doctest-src typecheck lint format schema clean changelog release stability stability-unit stability-unix stability-embedded repeat vm-health qemu-restart import-snapshot hyperfine profile browsers dashboard

# Bump component for `make release`. Override on the command line:
#   make release BUMP=minor
BUMP ?= patch

HYPERFINE_VERSION := 1.20.0

# Release-flow tools (git-cliff, bump-my-version) live in the project venv and
# are invoked DIRECTLY, never via `uv run` — `uv run` would sync and dirty
# uv.lock, which blocks the bump. The catch: when the venv isn't activated,
# .venv/bin isn't on PATH, so git-cliff fails with "git-cliff: not found"
# (bump-my-version happens to survive because `uv tool` also drops it in
# ~/.local/bin, but git-cliff has no such fallback). Prepend the venv's bin dir
# for the `changelog`/`release` recipes so the tools resolve either way. Honor
# an already-active venv ($VIRTUAL_ENV); otherwise fall back to ./.venv.
VENV_BIN := $(if $(VIRTUAL_ENV),$(VIRTUAL_ENV)/bin,$(CURDIR)/.venv/bin)

# Coverage target invoked by `validate`. `ci` overrides this to
# `coverage-unit` because GitHub Actions doesn't have the Vagrant VMs
# that integration/hops tests require.
COVERAGE_TARGET ?= coverage

COVERAGE_THRESHOLD := 94
# CI runs unit tests only (integration/hops markers need Vagrant VMs that
# don't exist in GitHub Actions), so the achievable threshold is lower.
CI_COVERAGE_THRESHOLD := 90

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

# Two axes of test selection (see docs/contributing.md → Regression-test
# categories). Keep these in sync with noxfile.py.
#   Level (directory, cumulative) — selected by PATH, in the coverage-*/nox-*
#   targets below:
#     unit        — tests/unit
#     integration — tests/unit + tests/integration
#     (bare)      — all three tiers (tests/unit + tests/integration + tests/e2e)
#   Resource (marker, orthogonal) — selected by MARKER:
#     unix     — real telnet/SSH against the Linux Vagrant VMs (incl. multi-hop)
#     embedded — Zephyr/QEMU under the zephyr VM
#     hostless — needs no testbed at all (what CI gates on): tests/unit + the
#                no-VM e2e tests. Mirrors noxfile.py tests_hostless.
M_UNIX := integration and not embedded
M_EMBEDDED := embedded
M_HOSTLESS := not integration and not embedded and not stability and not browser

# `browser` (Playwright) tests always run as their own pytest process — sync
# Playwright keeps an event loop running in the worker main thread for the
# whole session, which breaks pytest-asyncio tests that share the process
# (see tests/e2e/monitor/dashboard's `browser` marker). `make dashboard` is
# that dedicated process; every multi-tier selection below that would
# otherwise co-select browser + async tests in one pytest invocation
# (`coverage`, `repeat`) excludes `browser` and, for `coverage`, chains
# `make dashboard` separately so the gate still runs those tests overall.

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

all: ## (Build & Release) Run full pipeline against the dev VM (includes integration tests)
	@$(MAKE) validate \
		&& $(MAKE) build

ci: ## (Build & Release) Run pipeline without VM-dependent tests (used by GitHub Actions)
	@$(MAKE) validate COVERAGE_TARGET=coverage-hostless \
		&& $(MAKE) build

changelog: export PATH := $(VENV_BIN):$(PATH)
changelog: ## (Build & Release) Regenerate CHANGELOG.md from conventional commit history (Unreleased only — does not touch released sections)
	git-cliff -o CHANGELOG.md

release: export PATH := $(VENV_BIN):$(PATH)
release: ## (Build & Release) lint, typecheck, docs, nox, profile, then changelog, bump, build dist (BUMP=patch|minor|major, default patch; or NEW_VERSION=X.Y.Z[rcN] for prereleases)
	@$(MAKE) clean-dist \
		&& $(MAKE) lint \
		&& $(MAKE) typecheck \
		&& $(MAKE) docs \
		&& OTTO_DETECT_ASYNCIO_LEAKS=1 $(MAKE) nox \
		&& $(MAKE) profile \
		&& NEW_VERSION="$${NEW_VERSION:-$$(bump-my-version show new_version --increment $(BUMP))}" \
		&& echo "Targeting v$$NEW_VERSION" \
		&& git-cliff --tag "v$$NEW_VERSION" -o CHANGELOG.md \
		&& git add CHANGELOG.md \
		&& bump-my-version bump --verbose --allow-dirty --new-version "$$NEW_VERSION" $(BUMP) \
		&& $(MAKE) build \
		&& echo \
		&& echo "Regenerated CHANGELOG.md, bumped version, tagged, and built dist/." \
		&& echo "Pushing the tag fires .github/workflows/release.yml, which builds," \
		&& echo "publishes to PyPI via OIDC (gated by the 'pypi' environment), and" \
		&& echo "creates the GitHub Release." \
		&& echo "Push with:" \
		&& echo "    git push --follow-tags" \
		&& echo \
		&& echo "To rehearse first, dispatch release-testpypi.yml from the Actions tab."

nox-unit: ## Run the unit suite across all supported Pythons (no VMs). Fastest safe test. Override iterations with COUNT=N (default 1); JUnit XML lands in reports/junit/nox-unit/.
	uv run nox -s tests_unit -- --count=$(NOX_COUNT) --repeat-scope=session

nox-integration: ## Run the unit + integration level tiers across all supported Pythons. Requires the full lab. Override COUNT=N (default 1); JUnit XML lands in reports/junit/nox-integration/.
	uv run nox -s tests_integration -- --count=$(NOX_COUNT) --repeat-scope=session

nox-unix: ## Run the Unix-VM integration suite (incl. multi-hop) across all supported Pythons. Requires dev VM with Vagrant hosts up. Override COUNT=N (default 1); JUnit XML in reports/junit/nox-unix/.
	uv run nox -s tests_unix -- --count=$(NOX_COUNT) --repeat-scope=session

nox-embedded: ## Run the embedded (Zephyr) suite across all supported Pythons. Requires Vagrant lab up. Override COUNT=N (default 1); JUnit XML in reports/junit/nox-embedded/.
	uv run nox -s tests_embedded -- --count=$(NOX_COUNT) --repeat-scope=session

nox-hostless: ## Run the no-testbed CI gate (tests/unit + no-VM e2e) across all supported Pythons. No VMs. Override COUNT=N (default 1); JUnit XML lands in reports/junit/nox-hostless/.
	uv run nox -s tests_hostless -- --count=$(NOX_COUNT) --repeat-scope=session

nox: ## Run the FULL test suite (all environments) across all supported Pythons. Requires dev VM with Vagrant hosts up. Not used by CI. Override COUNT=N (default 1); JUnit XML in reports/junit/nox/.
	uv run nox -s tests_all -- --count=$(NOX_COUNT) --repeat-scope=session

validate: ## (Build & Release) Run validation (clean-dist, lint, typecheck, coverage, docs) without building dist
	@$(MAKE) clean-dist \
		&& $(MAKE) lint \
		&& $(MAKE) typecheck \
		&& $(MAKE) $(COVERAGE_TARGET) \
		&& $(MAKE) docs

clean-dist:
	@rm -rf dist

dev: ## (Dev) Set up the dev environment (uv sync, git hooks, hyperfine)
	uv sync
	git config core.hooksPath .githooks
	$(MAKE) hyperfine
	@echo "Dev environment ready"

hyperfine:
	@if [ -x "$(VENV_BIN)/hyperfine" ] && "$(VENV_BIN)/hyperfine" --version | grep -qF "$(HYPERFINE_VERSION)"; then \
		echo "hyperfine $(HYPERFINE_VERSION) already installed"; \
	else \
		bash scripts/install_hyperfine.sh "$(HYPERFINE_VERSION)" "$(VENV_BIN)"; \
	fi

browsers: ## (Setup) Install the Playwright Chromium binary used by the dashboard e2e tests
	uv run playwright install chromium

profile: hyperfine ## (Dev) Enforce the import budget (module-count caps + snapshots + denylist) + hyperfine wall-clock
	uv run python scripts/import_budget.py --check --hyperfine

build: ## (Build & Release) Build the project with uv
	uv build

coverage: dashboard ## Run the full suite (all tiers, pinned Python) and enforce the coverage gate (excludes heavy `stability`; browser (Playwright) suite runs first, as its own process, via the `dashboard` prerequisite). Requires lab VMs (+ `make browsers` once). JUnit XML lands in reports/junit/coverage/ and reports/junit/dashboard/.
	$(TIMEOUT_CMD) uv run pytest -m "not stability and not browser" --cov-fail-under=$(COVERAGE_THRESHOLD) $(call junitxml,coverage)

coverage-unit: ## Run the unit level tier (tests/unit only; no testbed) with a coverage report (no gate — one tier can't meet the whole-repo floor). JUnit XML lands in reports/junit/coverage-unit/.
	$(TIMEOUT_CMD) uv run pytest tests/unit -m "not stability" $(call junitxml,coverage-unit)

coverage-integration: ## Run the unit + integration level tiers (tests/unit + tests/integration) with a coverage report (no gate). Requires the full lab. JUnit XML in reports/junit/coverage-integration/.
	$(TIMEOUT_CMD) uv run pytest tests/unit tests/integration -m "not stability" $(call junitxml,coverage-integration)

coverage-hostless: ## Run the no-testbed CI gate suite (tests/unit + no-VM e2e) and enforce the CI coverage gate. No VMs. JUnit XML lands in reports/junit/coverage-hostless/.
	$(TIMEOUT_CMD) uv run pytest tests/unit tests/e2e -m "$(M_HOSTLESS)" --cov-fail-under=$(CI_COVERAGE_THRESHOLD) $(call junitxml,coverage-hostless)

coverage-unix: ## Run the Unix-VM resource slice (incl. multi-hop) with a coverage report (no gate). Requires lab VMs. JUnit XML in reports/junit/coverage-unix/.
	$(TIMEOUT_CMD) uv run pytest -m "$(M_UNIX)" $(call junitxml,coverage-unix)

coverage-embedded: ## Run the embedded (Zephyr) resource slice with a coverage report (no gate). Requires Vagrant lab up. JUnit XML in reports/junit/coverage-embedded/.
	$(TIMEOUT_CMD) uv run pytest -m "$(M_EMBEDDED)" $(call junitxml,coverage-embedded)

dashboard: ## Run the browser e2e suite for the monitor dashboard (needs `make browsers` once). JUnit XML in reports/junit/dashboard/.
	$(TIMEOUT_CMD) uv run pytest tests/e2e/monitor/dashboard -m browser --screenshot only-on-failure --output reports/playwright $(call junitxml,dashboard)

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
	    for ip in $$(jq -r '.[].ip' tests/_fixtures/lab_data/tech1/hosts.json); do \
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

repeat: ## Run the full local suite (unit + integration + e2e) under pytest-repeat (excludes `browser` — see note above M_HOSTLESS; run its soak separately). Local only; requires VMs. JUnit XML in reports/junit/repeat/. Override COUNT=N (default 10).
	OTTO_DETECT_ASYNCIO_LEAKS=1 uv run pytest \
	    -m "not browser" \
	    --count=$(COUNT) \
	    -p no:cacheprovider \
	    --no-cov \
	    $(call junitxml,repeat)

vm-health: ## (Lab) Probe every lab VM + Zephyr QEMU instance; prints per-host timestamps + clock drift. Requires the Vagrant lab up.
	uv run python scripts/lab_health.py

qemu-restart: ## (Lab) Restart the Zephyr QEMU + SNMP-relay units on the hop VM(s), then health-check. Use to recover a wedged embedded bed.
	uv run python scripts/lab_health.py --restart-qemu

lint: ## (Quality) Run ruff lint + format checks (part of validate/ci/all)
	uv run ruff check .
	uv run ruff format --check .

format: ## (Quality) Apply ruff autoformat to the tree
	uv run ruff format .

typecheck: ## (Quality) Run ty type checker (advisory during trial; not wired into all)
	uv run ty check

schema: ## (Dev) Generate JSON Schema for hosts.json / settings.toml / reservations into schemas/ (git-ignored; for editor autocomplete)
	uv run otto schema export --out schemas

import-snapshot: ## (Dev) Regenerate import-budget golden snapshots + print per-surface counts (run after an intentional import change, then review the diff and update caps)
	uv run python scripts/import_budget.py --update

SPHINX_SRCS :=  docs/conf.py                        \
                $(shell find docs -name '*.rst')    \
                $(shell find docs -name '*.md')    \
                $(shell find src/otto -name '*.py') \

docs: docs-lint docs-html doctest doctest-src ## (Docs) Build HTML docs + Sphinx & src doctests (sub-targets: docs-lint, docs-html, doctest, doctest-src, docs-inventories)

docs-lint:
	uv run doc8 docs/
	uv run python scripts/lint_markdown_doctests.py docs/

docs-html: docs/_build/html/index.html

docs-inventories:
	mkdir -p docs/_inventories
	curl -sSL --retry 3 -o docs/_inventories/python.inv     https://docs.python.org/3/objects.inv
	curl -sSL --retry 3 -o docs/_inventories/typer.inv      https://typer.tiangolo.com/objects.inv
	curl -sSL --retry 3 -o docs/_inventories/rich.inv       https://rich.readthedocs.io/en/stable/objects.inv
	curl -sSL --retry 3 -o docs/_inventories/pydantic.inv   https://docs.pydantic.dev/latest/objects.inv
	curl -sSL --retry 3 -o docs/_inventories/asyncssh.inv   https://asyncssh.readthedocs.io/en/stable/objects.inv
	curl -sSL --retry 3 -o docs/_inventories/pytest.inv     https://docs.pytest.org/en/stable/objects.inv
	curl -sSL --retry 3 -o docs/_inventories/telnetlib3.inv https://telnetlib3.readthedocs.io/en/latest/objects.inv

# -E (fresh env, no stale doctrees) + -a (write all) make a local build match
# CI's clean build, so incremental state can't mask or invent a warning.
docs/_build/html/index.html: $(SPHINX_SRCS)
	uv run sphinx-build -E -a -W -b html docs/ docs/_build/html

doctest:
	uv run sphinx-build -E -b doctest docs/ docs/_build/doctest

doctest-src:
	uv run pytest -p no:cacheprovider -o addopts="--doctest-modules" src/otto

clean: ## (Dev) Remove all generated artifacts
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
	@printf '\n\033[1mTesting\033[0m  (COUNT=N overrides iterations; omit the suffix to run all tiers)\n'
	@printf '  scope:  unit < integration < (all)   ·   unix · embedded   ·   hostless = no-VM CI gate\n'
	@printf '  \033[36m%-30s\033[0m %s\n' 'coverage-*'   'pinned Python + coverage    (bare coverage = all tiers, gated 94; hostless gated 90)'
	@printf '  \033[36m%-30s\033[0m %s\n' 'nox-*'        'every suffix, all Pythons   (bare nox = full matrix)'
	@printf '  \033[36m%-30s\033[0m %s\n' 'stability-*'  'pytest-repeat soak          (unit · unix · embedded; bare stability = all tiers)'
	@printf '  \033[36m%-30s\033[0m %s\n' 'repeat'       'soak the full unit suite (pytest-repeat)'
	@awk 'BEGIN { FS=":.*?## "; n=split("Build & Release|Quality|Docs|Lab|Dev",order,"|") } /^[a-zA-Z_-]+:.*## \(/ { d=$$2; s=d; sub(/\).*/,"",s); sub(/^\(/,"",s); sub(/^\([^)]*\) */,"",d); items[s]=items[s] sprintf("  \033[36m%-16s\033[0m %s\n",$$1,d) } END { for(i=1;i<=n;i++) if(order[i] in items) printf "\n\033[1m%s\033[0m\n%s",order[i],items[order[i]] }' \
		$(MAKEFILE_LIST)
