.DEFAULT_GOAL := all

.PHONY: help all ci nox nox-unit nox-integration nox-unix nox-embedded nox-hostless validate validate-python validate-ts clean-dist dev build coverage coverage-python coverage-unit coverage-integration coverage-unix coverage-embedded coverage-hostless coverage-ts coverage-ts-unit docs docs-lint docs-html docs-inventories docs-media doctest doctest-src typecheck typecheck-python typecheck-ts lint lint-python lint-ts check check-python check-ts format format-python format-ts schema monitor-fixtures clean changelog release stability stability-unit stability-unix stability-tunnel stability-embedded repeat vm-health qemu-restart import-snapshot hyperfine profile browsers dashboard dashboard-all dashboard-soak web-install web web-dev test-ts web-clean wheel-check

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

# Coverage target invoked by `validate-python`. Defaults to the full Python
# gate (coverage-python); `ci` overrides this to `coverage-hostless` because
# GitHub Actions doesn't have the Vagrant VMs that integration/hops tests
# require. TS coverage (coverage-ts) is validated separately by validate-ts,
# so this variable is Python-only.
COVERAGE_TARGET ?= coverage-python

COVERAGE_THRESHOLD := 95
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

# Iteration count for `make stability-tunnel`. Default is 1 (the tests loop
# internally via OTTO_TUNNEL_SOAK_CYCLES); honor COUNT only when explicitly
# passed on the command line.
STABILITY_TUNNEL_COUNT := $(if $(filter command line,$(origin COUNT)),$(COUNT),1)

# Internal soak depth for `make stability-tunnel` (cycles per test). Default 5;
# override with CYCLES=N (a smoke run: CYCLES=2).
STABILITY_TUNNEL_CYCLES := $(if $(filter command line,$(origin CYCLES)),$(CYCLES),5)

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

# ═══ Build & Release pipeline ═══════════════════════════════════════════════

all: ## (Build & Release) Run full pipeline against the dev VM (includes integration tests)
	@$(MAKE) web-install \
		&& $(MAKE) validate \
		&& $(MAKE) build

ci: ## (Build & Release) Run pipeline without VM-dependent tests (used by GitHub Actions)
	@$(MAKE) web-install \
		&& $(MAKE) validate COVERAGE_TARGET=coverage-hostless \
		&& $(MAKE) build

changelog: export PATH := $(VENV_BIN):$(PATH)
changelog: ## (Build & Release) Regenerate CHANGELOG.md from conventional commit history (Unreleased only — does not touch released sections)
	git-cliff -o CHANGELOG.md

# WARNING: `make -n release` is NOT side-effect-free — the recipe is one
# backslash-continued line containing $(MAKE), so GNU make executes it under
# -n; the $(MAKE) sub-calls inherit -n and no-op, but the plain
# git-cliff/git-add/bump-my-version commands run for real (version bump +
# CHANGELOG staged). Never dry-run this target.
release: export PATH := $(VENV_BIN):$(PATH)
release: ## (Build & Release) npm ci web/, Python static checks (check-python), docs, nox, build web dist, all-browser dashboard e2e, full TS gate (validate-ts, incl. merged coverage), profile, then changelog, bump, build dist (BUMP=patch|minor|major, default patch; or NEW_VERSION=X.Y.Z[rcN] for prereleases)
	@$(MAKE) clean-dist \
		&& $(MAKE) web-install \
		&& $(MAKE) check-python \
		&& $(MAKE) docs \
		&& OTTO_DETECT_ASYNCIO_LEAKS=1 $(MAKE) nox \
		&& $(MAKE) web \
		&& $(MAKE) dashboard-all \
		&& $(MAKE) validate-ts \
		&& $(MAKE) profile \
		&& NEW_VERSION="$${NEW_VERSION:-$$(bump-my-version show new_version --increment $(BUMP))}" \
		&& echo "Targeting v$$NEW_VERSION" \
		&& git-cliff --tag "v$$NEW_VERSION" -o CHANGELOG.md \
		&& git add CHANGELOG.md \
		&& bump-my-version bump --verbose --allow-dirty --new-version "$$NEW_VERSION" $(BUMP) \
		&& $(MAKE) wheel-check \
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

nox-unit-repeat: ## Repeat the whole tests/unit tree twice in one process — the test-isolation leak guard (registry/tmp-import/module-identity) that also runs in CI. No VMs. JUnit XML lands in reports/junit/nox-unit-repeat/. (Count is fixed at 2; the check is pass/fail, not a soak.)
	uv run nox -s tests_unit_repeat

nox: ## Run the FULL test suite (all environments) across all supported Pythons. Requires dev VM with Vagrant hosts up. Not used by CI. Override COUNT=N (default 1); JUnit XML in reports/junit/nox/.
	uv run nox -s tests_all -- --count=$(NOX_COUNT) --repeat-scope=session

validate: validate-python validate-ts ## (Build & Release) Validate ALL code (Python + TS): sub-targets validate-python + validate-ts

validate-python: ## (Build & Release) Python validation (clean-dist, static checks, coverage, docs) without building dist
	@$(MAKE) clean-dist \
		&& $(MAKE) check-python \
		&& $(MAKE) $(COVERAGE_TARGET) \
		&& $(MAKE) docs

validate-ts: check-ts coverage-ts ## (Build & Release) TypeScript validation: Biome+knip, tsc, merged coverage gate (unit floor runs inside it via test:coverage; CI's browserless slice is check-ts + coverage-ts-unit)

clean-dist:
	@rm -rf dist

# ═══ Dev environment ════════════════════════════════════════════════════════

dev: ## (Dev) Set up the dev environment (uv sync, git hooks, hyperfine, Chromium, web/ deps)
	uv sync
	git config core.hooksPath .githooks
	$(MAKE) hyperfine
	$(MAKE) browsers
	$(MAKE) web-install
	@echo "Dev environment ready"

hyperfine:
	@if [ -x "$(VENV_BIN)/hyperfine" ] && "$(VENV_BIN)/hyperfine" --version | grep -qF "$(HYPERFINE_VERSION)"; then \
		echo "hyperfine $(HYPERFINE_VERSION) already installed"; \
	else \
		bash scripts/install_hyperfine.sh "$(HYPERFINE_VERSION)" "$(VENV_BIN)"; \
	fi

browsers: ## (Setup) Install the Playwright Chromium + Firefox + WebKit binaries: the dashboard e2e suite runs on all three engines, and the docs media pipeline uses Chromium. On a box missing a browser's system libs, run `uv run playwright install-deps <chromium|firefox|webkit>` once — the Vagrantfile's dev-root provisioner carries the exact apt package list + how to regenerate it.
	uv run playwright install chromium firefox webkit

# web/ (React+TS monitor dashboard) build lanes. `make web` produces the
# dist/ that MonitorServer requires (see server.py's _dist_index_path()) —
# the legacy static dashboard was deleted at the Task 9 cutover, so dist/ is
# now the ONLY frontend and stays in place once built; the browser pin suite
# (tests/e2e/monitor/dashboard) runs against it. (Pre-cutover, a stray dist/
# left behind by a smoke build used to shadow the legacy dashboard.html —
# that's why `make web-clean` exists, but it's no longer required after
# every build.)
web-install: ## (Dev) Install web/'s npm dependencies from the committed lockfile (npm ci)
	# npm ci occasionally hits a transient registry ECONNRESET mid-download in CI
	# (issue #107) that npm's own fetch-retries don't catch. Retry ONLY on
	# network-class failures (up to 3 attempts, 5s/10s backoff); a deterministic
	# error such as a package.json/lockfile drift still fails fast on attempt 1.
	cd web && n=1; while :; do \
	  log=$$(mktemp); \
	  npm ci >"$$log" 2>&1; rc=$$?; cat "$$log"; \
	  if [ $$rc -eq 0 ]; then rm -f "$$log"; break; fi; \
	  if ! grep -qiE 'ECONNRESET|ETIMEDOUT|EAI_AGAIN|ENOTFOUND|ECONNREFUSED|socket hang up|npm (error|ERR!) network' "$$log"; then \
	    rm -f "$$log"; echo "web-install: npm ci failed (exit $$rc), not a network error - failing fast" >&2; exit $$rc; fi; \
	  rm -f "$$log"; \
	  if [ $$n -ge 3 ]; then echo "web-install: npm ci still failing after $$n network-error attempts (exit $$rc)" >&2; exit $$rc; fi; \
	  echo "web-install: npm ci hit a network error (attempt $$n); retrying in $$((n * 5))s" >&2; \
	  sleep $$((n * 5)); n=$$((n + 1)); \
	done

# npm ci writes node_modules/.package-lock.json, so it doubles as the install
# stamp: gating on it re-runs `npm ci` when (and only when) the lockfile moves.
# That is what a checkout predating a new dependency needs — @xyflow landing
# with the topology work left `make web` dying on an unresolved import until
# node_modules caught up. Depending on the phony `web-install` directly would
# instead pay a full wipe-and-reinstall on every single build.
# (Defined here, ahead of its first use as a prerequisite: GNU Make expands
# prerequisites when the rule is READ, so a later definition would expand to
# nothing here and silently drop the dependency.)
WEB_NODE_MODULES := web/node_modules/.package-lock.json

$(WEB_NODE_MODULES): web/package.json web/package-lock.json
	$(MAKE) web-install

web: $(WEB_NODE_MODULES) ## (Build & Release) Build the web/ React dashboard + the covreport bundle (vite) into their static dist dirs, then gate both against absolute http(s) URLs (air-gap requirement — labs have no network access, see scripts/check_airgap.sh) and the dashboard against a resolved-brand-color regression (scripts/check_brand_tokens.sh)
	# Regenerate web/src/api/types.gen.ts and web/src/api/export.gen.ts from
	# the live pydantic models and fail BEFORE the vite build if either
	# committed file has drifted — a stale wire contract should be caught by
	# its own diff, not surface later as a build or runtime type error with
	# no clue which model changed.
	scripts/gen_web_types.sh
	git diff --exit-code web/src/api/types.gen.ts web/src/api/export.gen.ts
	cd web && npm run build
	cd web && npm run build:covreport
	scripts/check_airgap.sh
	scripts/check_airgap.sh src/otto/coverage/renderer/static/dist
	scripts/check_brand_tokens.sh

web-dev: $(WEB_NODE_MODULES) ## (Dev) Run the web/ Vite dev server with hot reload; proxies /api to a running otto monitor (default target http://127.0.0.1:8080, override with VITE_OTTO_TARGET=http://host:port)
	cd web && npm run dev

# web/ quality lanes moved to the language-parity family (lint-ts /
# typecheck-ts / coverage-ts-unit / test-ts) in the Quality section below —
# one name per aspect, no web-* aliases. web-install/web/web-dev/web-clean
# stay here: they are artifact/dev targets, not language-parity gates.

test-ts: $(WEB_NODE_MODULES) ## (Dev) Run the web/ vitest suite once — no coverage, the fast TS loop. (Deliberately no test-python twin and no bare `test`: the fast Python lane is `coverage-unit`.)
	cd web && npm run test

web-clean: ## (Dev) Remove the built web/ dist outputs (monitor dashboard + covreport)
	rm -rf src/otto/monitor/static/dist
	rm -rf src/otto/coverage/renderer/static/dist

# uv_build embeds the ENTIRE module tree (src/otto/**) into both the sdist and
# the wheel by default — unlike hatchling, it is not VCS-aware, so it doesn't
# care that static/dist/ is .gitignore'd (see the [tool.uv.build-backend]
# comment in pyproject.toml). That makes the embedding implicit rather than
# explicit config, so this target exists to pin it with a real assertion:
# build the dashboard, build the wheel, and fail loudly if the dashboard ever
# stops making it in (e.g. a future wheel-exclude, or a uv_build default
# change). Deliberately NOT wired into `coverage` — it rebuilds the frontend
# and a real wheel, which is release-flow overhead, not a per-commit gate.
# Prerequisite composition (clean-dist web build), not $(MAKE) calls in the
# recipe: `make -n wheel-check` must stay dry-run-safe, and GNU make only
# honors -n for prerequisite recursion, not for $(MAKE) invoked from inside a
# recipe line (see the release: warning above for what happens when that rule
# is violated).
# NOTE: prerequisites assume serial execution; do not run wheel-check under make -j.
wheel-check: clean-dist web build ## (Build & Release) Rebuild the dashboard + wheel and assert the wheel embeds src/otto/monitor/static/dist/ (air-gap requirement)
	@count=$$(unzip -l dist/*.whl | grep -c "otto/monitor/static/dist/" || true); \
	if [ "$$count" -eq 0 ]; then \
		echo "wheel-check: FAIL — no otto/monitor/static/dist/ entries in dist/*.whl; an air-gapped install would ship without the dashboard." >&2; \
		exit 1; \
	fi; \
	if ! unzip -p dist/*.whl otto/monitor/static/dist/index.html > /dev/null; then \
		echo "wheel-check: FAIL — index.html missing from the wheel's static/dist." >&2; exit 1; \
	fi; \
	echo "wheel-check: OK — $$count otto/monitor/static/dist/ entries embedded in the wheel (incl. index.html)."; \
	scripts/check_airgap.sh
	@count=$$(unzip -l dist/*.whl | grep -c "otto/coverage/renderer/static/dist/" || true); \
	if [ "$$count" -eq 0 ]; then \
		echo "wheel-check: FAIL — no otto/coverage/renderer/static/dist/ entries in dist/*.whl; an air-gapped install would ship the coverage report without its frontend." >&2; \
		exit 1; \
	fi; \
	echo "wheel-check: OK — $$count otto/coverage/renderer/static/dist/ entries embedded."

docs-media: ## (Docs) Force-regenerate the build-time GUI media (screenshots, clips, termynal blocks) in docs/_static/generated/
	uv run python scripts/capture_docs_media.py --mode force
	uv run python scripts/capture_docs_termynal.py --mode force

profile: hyperfine ## (Dev) Enforce the import budget (module-count caps + snapshots + denylist) + hyperfine wall-clock
	uv run python scripts/import_budget.py --check --hyperfine

build: ## (Build & Release) Build the project with uv
	uv build

# ═══ Test & Coverage (Python tiers + TS legs) ═══════════════════════════════

# The dashboard lane feeds `coverage-python` (its browser-driven server/
# collector lines) and by default runs on Chromium ONLY: the coverage numbers
# are engine-independent, so one engine keeps the per-task `make coverage`
# gate fast — mirroring how `make coverage-python` pins a single Python while
# `make nox` spans them all. The full cross-engine run is `make dashboard-all`
# (Chromium + Firefox + WebKit), which `make release` invokes; CI runs the
# three engines as a parallel matrix (see the `dashboard` job / noxfile's
# parametrized session). Override ad hoc with
# DASHBOARD_BROWSERS="chromium firefox webkit".
# The one Safari-specific test is `@pytest.mark.only_browser("webkit")`, so it
# only runs when webkit is in the set (a skip, not silently absent, otherwise).
# Runs -n 1 (all browser tests share one xdist_group anyway; extra workers
# would sit idle and emit "No data was collected" coverage warnings) and writes
# coverage DATA only: --cov-report= suppresses the report so a standalone run
# never stomps reports/coverage/html. Running first as `coverage-python`'s
# direct prerequisite (bare `coverage`'s only transitively, via
# coverage-python), its fresh data file is then extended by the main run's
# --cov-append, folding the browser-driven server/collector lines (e.g. the
# dashboard HTML route, UI event round-trips) into the gated report.
#
# Both suites — and the docs build, whose GUI media is photographed from the
# real shell (see docs/_build/html/index.html below) — drive real build
# artifacts: the React dashboard (src/otto/monitor/static/dist/) and the
# coverage-report bundle (src/otto/coverage/renderer/static/dist/covreport.js).
# They exist only once `make web` has run (noxfile.py's `dashboard` session
# docstring documents this as `make dashboard`'s prerequisite). Declaring them
# as real file targets, built on demand by `make web`, lets a fresh checkout or
# worktree self-heal on the first `make coverage`/`make dashboard`/`make docs`
# instead of dying with "run `make web` first".
#
# They gate on the frontend SOURCES, not merely on the dist's existence. An
# existence-only gate keeps repeat runs fast but silently serves a stale
# bundle: every consumer here (browser e2e, coverage, docs media) drives the
# built dist, so a dist older than web/src/ means the gates photograph and
# assert against a frontend that no longer exists. That is not hypothetical —
# a dist five days behind web/src/ sailed through `make clean` (which did not
# remove it) and failed `make docs` in Playwright as a selector that "did not
# exist", when in truth it existed in the source and merely had never been
# built. Source prerequisites keep the fast path intact (unchanged sources =>
# dist is newer than all prereqs => no rebuild, exactly as before) while making
# the stale case impossible. `make web` is not incremental, but it does not
# need to be: it re-emits both bundles only when make has already decided
# something upstream moved.
#
# The `&:` grouped-target form (GNU Make 4.3+) runs the recipe once for both
# outputs together, not once per missing file. Same caveat as `release` above:
# because the recipe line names `$(MAKE)` literally, GNU Make always runs it
# for real even under `make -n`, so a dry run against a checkout with no dist
# yet will actually build it.
coverage-python: dashboard ## Run the full Python suite (all tiers, pinned Python) and enforce the 95 gate; the browser (Playwright) suite runs first as its own process via the `dashboard` prerequisite — its coverage data is folded in via --cov-append. Requires lab VMs (+ `make browsers` once). JUnit XML lands in reports/junit/coverage-python/.
	$(TIMEOUT_CMD) uv run pytest -m "not stability and not browser" --cov-append --cov-fail-under=$(COVERAGE_THRESHOLD) $(call junitxml,coverage-python)

coverage: coverage-python coverage-ts ## Run BOTH language coverage gates: coverage-python (full pytest, 95 floor) + coverage-ts (merged vitest+e2e floor). The dashboard browser lane runs exactly once — coverage-python triggers it, and coverage-ts's artifact stamp sees it fresh.

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

DASHBOARD_BROWSERS ?= chromium
DASHBOARD_DIST := src/otto/monitor/static/dist/index.html
COVREPORT_DIST := src/otto/coverage/renderer/static/dist/covreport.js

# Everything vite feeds into the two bundles: the app sources (including the
# committed api/*.gen.ts, which is the seam through which a pydantic-model
# change reaches the frontend — `make web` regenerates and diff-gates them),
# the html entry, the tsc/vite configs, and the dependency manifests. Biome's
# config and web/fixtures/ are deliberately absent: neither is a build input.
WEB_SRCS := $(shell find web/src -type f) \
            web/index.html               \
            web/tsconfig.json            \
            web/vite.config.ts           \
            web/vite.covreport.config.ts \
            web/package.json             \
            web/package-lock.json

$(DASHBOARD_DIST) $(COVREPORT_DIST) &: $(WEB_SRCS) $(WEB_NODE_MODULES)
	$(MAKE) web

# Merged-TS-coverage inputs. The browser lane (dashboard) dumps raw Chromium
# V8 coverage (tests/_fixtures/_ts_coverage.py); its recipe touches the raw
# stamp. The istanbul artifact is source-stamped like DASHBOARD_DIST: a cold
# or stale `make coverage-ts` re-runs the (chromium) browser lane itself —
# honest, if heavy; the fast no-coverage loop is `make test-ts`. The stamp rule
# calls `$(MAKE) dashboard` for the same reason DASHBOARD_DIST calls
# `$(MAKE) web` (see its note above): `dashboard` is a .PHONY orchestrator, so
# it cannot be a plain freshness-gated prerequisite without re-running on every
# invocation. Same `make -n` caveat as DASHBOARD_DIST — the `$(MAKE)` line runs
# even under -n, but -n rides through MAKEFLAGS so the child `dashboard` also
# dry-runs and executes nothing. That safety holds only while this recipe stays
# a lone `$(MAKE)` line: do NOT chain another shell command onto it (the release
# warning above is the cautionary tale), and do not run this under `make -j`.
TS_E2E_RAW_STAMP := reports/ts-e2e-cov/raw/.stamp
TS_E2E_COV := reports/ts-e2e-cov/istanbul/coverage-final.json
BROWSER_TEST_SRCS := $(shell find tests/e2e/monitor/dashboard tests/e2e/cov/report_browser -name '*.py') tests/_fixtures/_ts_coverage.py

$(TS_E2E_RAW_STAMP): $(WEB_SRCS) $(BROWSER_TEST_SRCS)
	$(MAKE) dashboard

$(TS_E2E_COV): $(TS_E2E_RAW_STAMP) $(WEB_NODE_MODULES) web/scripts/e2e_coverage_report.mjs
	cd web && npm run e2e:coverage-report

# The `-m "browser and not soak"` below MUST match noxfile.py's
# DASHBOARD_MARKER_EXPR (the `dashboard` session's marker, which is what
# CI's `dashboard-e2e` job actually runs via `uv run nox -k <browser>` — NOT
# this target). See that constant's comment for why the two can't share one
# literal source and for the concrete incident (soak ran on every push, on
# every engine, until nox's expression was brought back in line with this
# one) that makes keeping them in step worth a standing comment. If this
# expression changes, change noxfile.py's too.
dashboard: $(DASHBOARD_DIST) $(COVREPORT_DIST) ## Run the browser e2e suites (monitor dashboard + coverage report) on DASHBOARD_BROWSERS (default: chromium — feeds `coverage`). Full matrix: `make dashboard-all`. Needs `make browsers` once; (re)builds web/'s dist bundles when missing or older than web/src/ (see `make web`). Excludes `soak` (see `dashboard-soak`) — minutes of pushing, not a per-task gate.
	@rm -rf reports/ts-e2e-cov/raw
	$(TIMEOUT_CMD) uv run pytest tests/e2e/monitor/dashboard tests/e2e/cov/report_browser -m "browser and not soak" $(foreach b,$(DASHBOARD_BROWSERS),--browser $(b)) -n 1 --cov-report= --screenshot only-on-failure --output reports/playwright $(call junitxml,dashboard)
	@mkdir -p reports/ts-e2e-cov/raw && touch $(TS_E2E_RAW_STAMP)

dashboard-all: ## Run the dashboard e2e on ALL engines (Chromium + Firefox + WebKit); invoked by `make release`. Needs `make browsers` once.
	$(MAKE) dashboard DASHBOARD_BROWSERS="chromium firefox webkit"

# --browser chromium is intentionally hardcoded, not DASHBOARD_BROWSERS:
# measured directly, the soak passes on Chromium in ~15s but WebKit's main
# thread can't answer a single DOM read within Playwright's 60s action
# timeout under the ~180k-point SSE firehose (see test_replay_soak.py's
# module docstring for the measurement). The test itself now skips loudly
# on any non-chromium `browser_name`, so this flag is belt-and-suspenders,
# not the only guard.
dashboard-soak: $(DASHBOARD_DIST) $(COVREPORT_DIST) ## Run the dashboard replay soak (Tier-3, `soak`-marked; NOT part of `make dashboard`/`make coverage`) — drives FakeCollector at max rate in-process, no VM. Chromium only (see comment above). JUnit XML lands in reports/junit/dashboard-soak/.
	$(TIMEOUT_CMD) uv run pytest tests/e2e/monitor/dashboard/test_replay_soak.py -m "browser and soak" --browser chromium -n 1 --no-cov --screenshot only-on-failure --output reports/playwright $(call junitxml,dashboard-soak)

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
	    -m "stability and integration and not embedded and not hops" \
	    --count=$(STABILITY_UNIX_COUNT) \
	    -p no:cacheprovider \
	    --no-cov \
	    $(call junitxml,stability-unix)

stability-tunnel: ## Tunnel soak against the live bed (churn/concurrency/traffic/adversity/health/monitor-loop). Requires lab VMs. JUnit XML in reports/junit/stability-tunnel/. COUNT=N repeats the suite (default 1); CYCLES=N sets internal loop depth (default 5).
	OTTO_DETECT_ASYNCIO_LEAKS=1 OTTO_TUNNEL_SOAK_CYCLES=$(STABILITY_TUNNEL_CYCLES) uv run pytest \
	    tests/e2e/tunnel_stability \
	    -m "stability and hops" \
	    --count=$(STABILITY_TUNNEL_COUNT) \
	    -p no:cacheprovider \
	    --no-cov \
	    $(call junitxml,stability-tunnel)

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
	    for ip in $$(jq -r '.hosts[].ip' tests/_fixtures/lab_data/tech1/lab.json); do \
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
	@echo "── Tier 2b (tunnel soak) ──"
	@$(MAKE) stability-tunnel $(if $(filter command line,$(origin COUNT)),COUNT=$(COUNT)) $(if $(filter command line,$(origin CYCLES)),CYCLES=$(CYCLES))
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

# ═══ Lab ════════════════════════════════════════════════════════════════════

vm-health: ## (Lab) Probe every lab VM + Zephyr QEMU instance; prints per-host timestamps + clock drift. Requires the Vagrant lab up.
	uv run python scripts/lab_health.py

qemu-restart: ## (Lab) Restart the Zephyr QEMU + SNMP-relay units on the hop VM(s), then health-check. Use to recover a wedged embedded bed.
	uv run python scripts/lab_health.py --restart-qemu

# ═══ Quality: static analysis + autofix ═════════════════════════════════════

lint: lint-python lint-ts ## (Quality) Lint ALL code (Python + TS): sub-targets lint-python + lint-ts

lint-python: ## (Quality) Run ruff lint + format checks (part of check-python)
	uv run ruff check .
	uv run ruff format --check .

# `biome check` = lint rules + formatting + ASSIST actions (organize-imports).
# `biome lint` + `biome format` together are STRICTLY WEAKER: neither reports
# an assist action, so unsorted imports pass both and fail `biome check`. That
# gap sat on main undetected while CI hand-listed sub-targets — see
# tests/unit/test_ci_web_gate.py, which pins this chain. This target is the
# single authoritative Biome gate; there is deliberately NO weaker TS lint.
# knip is the project-scope parity for what ruff's dead-code rules do on the
# Python side: unused exports/files/deps across web/src, scoped by
# web/knip.json (vendored Untitled UI source + generated wire types excluded,
# mirroring biome.json's files.includes).
lint-ts: $(WEB_NODE_MODULES) ## (Quality) Lint web/: the authoritative Biome gate (rules + format + assists) + knip (unused exports/files/deps)
	cd web && npm run check
	cd web && npm run knip

format: format-python format-ts ## (Quality) Apply ALL safe autofixes (Python + TS): sub-targets format-python + format-ts

# "format" means: after this, everything auto-fixable that `make lint` gates
# is fixed — not merely reformatted. That is why the Python leg runs ruff's
# safe lint fixes before the formatter (fixes can need reformatting), and the
# TS leg runs `biome check --write` (biome format alone cannot apply assist
# actions like organize-imports, which lint-ts gates).
format-python: ## (Quality) Apply ruff safe lint autofixes + autoformat
	uv run ruff check --fix .
	uv run ruff format .

format-ts: $(WEB_NODE_MODULES) ## (Quality) Apply Biome fixes to web/: rules + format + assists (`biome check --write`)
	cd web && npm run check:fix

typecheck: typecheck-python typecheck-ts ## (Quality) Type-check ALL code (Python + TS): sub-targets typecheck-python + typecheck-ts

typecheck-python: ## (Quality) Run ty type checker
	uv run ty check

typecheck-ts: $(WEB_NODE_MODULES) ## (Quality) Type-check web/ with tsc --noEmit (no build)
	cd web && npm run typecheck

check: check-python check-ts ## (Quality) ALL static analysis (Python + TS): sub-targets check-python + check-ts

check-python: lint-python typecheck-python ## (Quality) All Python static analysis: ruff (lint+format) + ty

check-ts: lint-ts typecheck-ts ## (Quality) All TS static analysis: Biome + knip (lint-ts) + tsc

coverage-ts-unit: $(WEB_NODE_MODULES) ## (Quality) Run the web/ vitest suite with v8 coverage and enforce the UNIT-tier floor (the TS analogue of coverage-hostless's reduced CI gate; the full merged gate is coverage-ts)
	cd web && npm run test:coverage

# The FULL TS coverage gate: vitest (unit) + the Playwright e2e leg, merged
# into ONE istanbul report and gated at the merged floor. The vitest-only
# floor (coverage-ts-unit, enforced inside vite.config.ts) is the reduced
# browserless tier CI runs — the exact analogue of coverage-hostless's 90 vs
# the full gate's 95 on the Python side.
coverage-ts: $(TS_E2E_COV) ## (Quality) Merged TS coverage gate: vitest + browser-e2e legs, one report, one floor (see also coverage-ts-unit)
	cd web && npm run test:coverage
	rm -rf reports/ts-cov/final && mkdir -p reports/ts-cov/final
	cp web/coverage/coverage-final.json reports/ts-cov/final/vitest.json
	cp $(TS_E2E_COV) reports/ts-cov/final/e2e.json
	cd web && npm run coverage:merged

schema: ## (Dev) Generate JSON Schema for lab.json / settings.toml / reservations into schemas/ (git-ignored; for editor autocomplete)
	uv run otto schema export --out schemas

monitor-fixtures: ## (Dev) Regenerate the committed monitor dummy-data fixtures in web/fixtures/ (spec 2026-07-10)
	uv run python scripts/gen_monitor_fixtures.py web/fixtures

import-snapshot: ## (Dev) Regenerate import-budget golden snapshots + print per-surface counts (run after an intentional import change, then review the diff and update caps)
	uv run python scripts/import_budget.py --update

# ═══ Docs ═══════════════════════════════════════════════════════════════════

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
#
# The dist prerequisites are load-bearing, not decorative: docs/conf.py runs
# scripts/capture_docs_media.py, which boots a real MonitorServer and
# photographs the REAL frontend through headless Chromium. That server serves
# the built dist, so without these the docs build happily photographs whatever
# stale bundle is lying around — or, on a fresh worktree, none at all.
docs/_build/html/index.html: $(SPHINX_SRCS) $(DASHBOARD_DIST) $(COVREPORT_DIST)
	uv run sphinx-build -E -a -W -b html docs/ docs/_build/html

doctest:
	uv run sphinx-build -E -b doctest docs/ docs/_build/doctest

doctest-src:
	uv run pytest -p no:cacheprovider -o addopts="--doctest-modules" src/otto

# web-clean is a prerequisite because the built frontend IS a generated
# artifact, and omitting it made this target quietly dishonest: a `make clean`
# followed by `make docs` used to keep serving a stale dashboard dist, because
# nothing in the clean removed it and the dist rule (see DASHBOARD_DIST) only
# rebuilt a MISSING bundle. Source-gated dist prerequisites now catch the stale
# case on their own, but "all generated artifacts" should still mean all of
# them. docs/_static/generated/ is the media capture's own output and is
# stamp-managed (it regenerates when its inputs move), so it stays.
clean: web-clean ## (Dev) Remove all generated artifacts
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
	@printf '  \033[36m%-30s\033[0m %s\n' 'coverage-*'   'pinned Python + coverage    (bare coverage = BOTH languages: coverage-python, gated 95, + coverage-ts merged; hostless gated 90)'
	@printf '  \033[36m%-30s\033[0m %s\n' 'nox-*'        'every suffix, all Pythons   (bare nox = full matrix)'
	@printf '  \033[36m%-30s\033[0m %s\n' 'stability-*'  'pytest-repeat soak          (unit · unix · tunnel · embedded; bare stability = all tiers)'
	@printf '  \033[36m%-30s\033[0m %s\n' 'repeat'       'soak the full unit suite (pytest-repeat)'
	@awk 'BEGIN { FS=":.*?## "; n=split("Build & Release|Quality|Docs|Lab|Dev",order,"|") } /^[a-zA-Z_-]+:.*## \(/ { d=$$2; s=d; sub(/\).*/,"",s); sub(/^\(/,"",s); sub(/^\([^)]*\) */,"",d); items[s]=items[s] sprintf("  \033[36m%-16s\033[0m %s\n",$$1,d) } END { for(i=1;i<=n;i++) if(order[i] in items) printf "\n\033[1m%s\033[0m\n%s",order[i],items[order[i]] }' \
		$(MAKEFILE_LIST)
