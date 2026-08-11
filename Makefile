.DEFAULT_GOAL := all

# Not -j safe: several recipes share scratch dirs under reports/ — `dashboard`
# does `rm -rf reports/ts-e2e-cov/raw` while `coverage-ts` reads it, and
# coverage-python/coverage-ts both write reports/junit/ — and parallelism here
# lives INSIDE recipes (pytest -n, npm), not in `make -j`. Force serial so
# `make -j coverage` can't race the rm against the read. Global because GNU
# Make 4.3 has no target-scoped .NOTPARALLEL; harmless, as no target relies
# on -j.
.NOTPARALLEL:

.PHONY: help all ci nox nox-full nox-unit nox-integration nox-unix nox-embedded nox-hostless validate validate-python validate-ts clean-dist dev build coverage coverage-python coverage-unit coverage-integration coverage-unix coverage-embedded coverage-hostless coverage-ts coverage-ts-unit docs docs-lint docs-html docs-inventories docs-media doctest doctest-src typecheck typecheck-python typecheck-ts lint lint-python lint-ts lint-arch check check-python gate-fresh check-ts format format-python format-ts schema monitor-fixtures clean changelog release stability stability-unit stability-unix stability-tunnel stability-embedded chaos chaos-embedded repeat vm-health qemu-restart import-snapshot hyperfine profile browsers dashboard dashboard-all dashboard-soak web-install web web-dev test-ts web-clean wheel-check

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
# Both resource legs share a marker (`integration`/`embedded`) with the
# tier-3 chaos lane (tests/e2e/chaos/ is stamped `chaos` + `stability` + one
# of these two), and both are bare POSITIVE selectors — no catch-all's
# `not stability` protects them. Without `not stability and not chaos` here,
# `make coverage-unix` / `coverage-embedded` (and their nox twins) co-select
# chaos scenarios that soft-reboot the leased host and blackhole SSH
# mid-suite. `not stability` alone was the pre-existing gap (already true of
# the plain stability soak); `not chaos` closes the same hole for the newer
# chaos lane in the same change. See
# tests/unit/test_tier_marker_invariants.py's G7.
M_UNIX := integration and not embedded and not stability and not chaos
M_EMBEDDED := embedded and not stability and not chaos
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

# Arms the asyncio transport-leak detector (tests/conftest.py +
# tests/_fixtures/_transport_leaks.py). One token so "which lanes are armed"
# is a single grep, and so a lane can never be armed by accident of copy-paste
# while reading as if it were not. Costs nothing when nothing leaked: the scan
# walks only live tracked transports and never calls into gc.
#
# It reports a leak by attributing it to the test that CREATED the transport,
# then forcing the ResourceWarning out at that boundary so pytest's unraisable
# plugin errors the leaking test rather than an innocent later one.
#
# STATED BLIND SPOT, measured 2026-08-09: it only sees transports still tracked
# at the test boundary, and the registry holds them weakly. A transport that
# becomes unreachable when its loop is dropped is collected BEFORE the scan, so
# it vanishes from the registry and is reported by nobody — while its
# ResourceWarning still fires at some later gc point, which is the very flake
# this is meant to attribute. Verified twice: the LocalHost exec-timeout leak
# (fixed in dab13a7b) and a synthetic leak both went unreported here. So a
# clean run under this flag is NOT proof that nothing leaked.
#
# NOTHING here covers that gap, and an earlier version of this comment claimed
# the FD-watermark bracket (tests/_fixtures/fd_watermark.py) did. It does not:
# measured 2026-08-09 by mutating dab13a7b's fix back out behind a probe test
# that asserts nothing, the bracket stayed GREEN and only the unraisable plugin
# went red. A leaked transport is COLLECTABLE, and every bracket path collects
# before its verdict — deliberately, so collector timing cannot manufacture a
# red build — which closes the pipe before the descriptors are counted.
# Tightening the tolerance does not help; zero fails the same way.
#
# So the two boundary instruments cover DIFFERENT halves and neither covers
# this one. The registry attributes leaks whose transports stay REFERENCED; the
# FD bracket catches descriptors held by something still ALIVE at teardown, and
# now runs over all of tests/unit/host at tolerance 0 for no measurable cost
# (it collects only once a raw count already looks wrong). What actually pins
# the collectable class is a test that counts descriptors IN-TEST with the loop
# still open and no collect in between —
# test_timed_out_exec_does_not_leak_its_pipe_fds — plus the ast-grep rule that
# bans the API whose Process wrapper makes the leak unfixable. A per-transport
# finalizer would close the hole here too; not attempted.
LEAK_DETECT := OTTO_DETECT_ASYNCIO_LEAKS=1

# JUnit XML output. Every test target writes into its own subdirectory of
# reports/junit/ named after the target, so runs never clobber each other and
# `make clean` (rm -rf reports) removes them all. pytest creates the parent
# directory for --junitxml, so no mkdir is needed. The nox-* targets encode the
# same layout in noxfile.py (_junitxml). Usage: $(call junitxml,coverage-unit)
JUNIT_DIR := reports/junit
junitxml = --junitxml=$(JUNIT_DIR)/$(1)/$(1).xml

# ═══ Recipe output convention ═══════════════════════════════════════════════
#
# Every recipe line is @-silenced, and each unit of work is announced by one
# $(SAY) banner. What reaches the terminal is therefore the banner plus the
# tool's OWN output — never make's echo of the shell plumbing behind it. The
# rules, so new targets stay in step:
#
#   1. @-silence every recipe line. No exceptions: an echoed `rm -rf`, `cp`, or
#      backslash-continued pytest invocation is noise the banner already covers.
#   2. Announce each distinct unit of work with exactly one $(SAY). A target
#      that runs two different tools (lint-ts = biome + knip) gets two; a target
#      that runs one tool in several invocations (lint-python = ruff check +
#      ruff format) gets one.
#   3. Say what is about to happen and any detail that varies at runtime
#      (browser set, worker count, iteration count, gate threshold) — those are
#      exactly what the suppressed command line would otherwise have shown.
#   4. Two kinds of line are NOT banners and stay plain: results/verdicts
#      (`wheel-check: OK — ...`) and the end-of-release instructions. A banner
#      is an action, and blurring that makes both harder to scan.
#   5. Pure-delegation recipes (a lone `$(MAKE) foo`) get no banner — the child
#      announces itself, and a second banner would just double every line.
#      Likewise, prerequisite-only aggregators (`validate`, `check`, `docs`)
#      have no recipe at all: a banner there would print AFTER the work it
#      claims to introduce, which is worse than silence.
#
# The exact command a target runs stays one keystroke away: `make -n <target>`.
# Colour matches what `make help` already emits.
SAY := printf '\033[1;36m==>\033[0m %s\n'

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
changelog: ## (Build & Release) Regenerate the WHOLE of CHANGELOG.md from conventional commit history (released sections included — the file is generated, never hand-edited)
	@$(SAY) "git-cliff → CHANGELOG.md (whole file, released sections included)"
	@git-cliff -o CHANGELOG.md

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
		&& $(LEAK_DETECT) $(MAKE) nox \
		&& $(MAKE) web \
		&& $(MAKE) dashboard-all \
		&& $(MAKE) validate-ts \
		&& $(MAKE) profile \
		&& NEW_VERSION="$${NEW_VERSION:-$$(bump-my-version show new_version --increment $(BUMP))}" \
		&& $(SAY) "targeting v$$NEW_VERSION" \
		&& $(SAY) "git-cliff → CHANGELOG.md (tagged v$$NEW_VERSION)" \
		&& git-cliff --tag "v$$NEW_VERSION" -o CHANGELOG.md \
		&& git add CHANGELOG.md \
		&& $(SAY) "bump-my-version → v$$NEW_VERSION" \
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
	@$(SAY) "nox: unit suite, all Pythons (x$(NOX_COUNT))"
	@uv run nox -s tests_unit -- --count=$(NOX_COUNT) --repeat-scope=session

nox-integration: ## Run the unit + integration level tiers across all supported Pythons. Requires the full lab. Override COUNT=N (default 1); JUnit XML lands in reports/junit/nox-integration/.
	@$(SAY) "nox: unit + integration tiers, all Pythons (x$(NOX_COUNT))"
	@uv run nox -s tests_integration -- --count=$(NOX_COUNT) --repeat-scope=session

nox-unix: ## Run the Unix-VM integration suite (incl. multi-hop) across all supported Pythons. Requires dev VM with Vagrant hosts up. Override COUNT=N (default 1); JUnit XML in reports/junit/nox-unix/.
	@$(SAY) "nox: Unix-VM suite, all Pythons (x$(NOX_COUNT))"
	@uv run nox -s tests_unix -- --count=$(NOX_COUNT) --repeat-scope=session

nox-embedded: ## Run the embedded (Zephyr) suite across all supported Pythons. Requires Vagrant lab up. Override COUNT=N (default 1); JUnit XML in reports/junit/nox-embedded/.
	@$(SAY) "nox: embedded/Zephyr suite, all Pythons (x$(NOX_COUNT))"
	@uv run nox -s tests_embedded -- --count=$(NOX_COUNT) --repeat-scope=session

nox-hostless: ## Run the no-testbed CI gate (tests/unit + no-VM e2e) across all supported Pythons. No VMs. Override COUNT=N (default 1); JUnit XML lands in reports/junit/nox-hostless/.
	@$(SAY) "nox: hostless CI gate, all Pythons (x$(NOX_COUNT))"
	@uv run nox -s tests_hostless -- --count=$(NOX_COUNT) --repeat-scope=session

nox-unit-repeat: ## Repeat the whole tests/unit tree twice in one process — the test-isolation leak guard (registry/tmp-import/module-identity) that also runs in CI. No VMs. JUnit XML lands in reports/junit/nox-unit-repeat/. (Count is fixed at 2; the check is pass/fail, not a soak.)
	@$(SAY) "nox: tests/unit twice in one process (isolation-leak guard)"
	@uv run nox -s tests_unit_repeat

# Interpreters for the tiered `nox` lane. PRIMARY (mirrors noxfile.py's
# PRIMARY_PYTHON — hand-kept pair) is the floor: the oldest supported
# interpreter, which the dev venv and release/build jobs run — its FULL-suite
# leg catches "requires newer Python" breaks everywhere. CANARY is the
# NEWEST interpreter, and also runs the FULL suite: with pytest's
# filterwarnings=error, a warning only fails on versions that actually RUN
# the affected tier. Import-time DeprecationWarnings are already caught by
# every version's unit/hostless legs; the canary exists for RUNTIME warnings
# in VM-backed code paths, which are version-specific and surface on the
# newest interpreter first (for a while only 3.14 emitted the asyncio
# resource-leak warnings) — so the newest keeps full VM-backed coverage as
# the early-warning leg. The MIDDLE versions run the hostless
# selection (the exact slice CI gates on, per push, on all five versions
# already): interpreter-sensitive regressions live overwhelmingly in the
# unit/hostless code paths, while the VM-backed tiers exercise otto↔testbed
# behavior that does not vary across interior versions — and cross-version
# parallelism is not an option here because xdist_group pins are
# process-local while the lab testbed is machine-global (two concurrent
# sessions would race the fixed tunnel/impair topologies). The complete
# cross-version matrix stays available as `make nox-full`; nightly cannot
# absorb it (hostless-only, no lab VMs), so run nox-full on demand when a
# release touches interpreter-sensitive integration surface.
NOX_PRIMARY := 3.10
NOX_CANARY := 3.14
NOX_MIDDLE := 3.11 3.12 3.13

nox: ## Run the full suite on the PRIMARY (3.10, floor) + CANARY (3.14, newest — version-specific warnings) Pythons, and the hostless CI-gate slice on the middle versions. Requires dev VM with Vagrant hosts up. Not used by CI. Full cross-version matrix: `make nox-full`. Override COUNT=N (default 1); JUnit XML in reports/junit/nox/ + reports/junit/nox-hostless/.
	@$(SAY) "nox: full suite on $(NOX_PRIMARY) + $(NOX_CANARY), hostless on $(NOX_MIDDLE) (x$(NOX_COUNT))"
	@uv run nox -s tests_all-$(NOX_PRIMARY) tests_all-$(NOX_CANARY) $(foreach v,$(NOX_MIDDLE),tests_hostless-$(v)) -- --count=$(NOX_COUNT) --repeat-scope=session

nox-full: ## Run the FULL test suite (all environments) across ALL supported Pythons — the pre-tiering `make nox` (~5× its wall-clock). Requires dev VM with Vagrant hosts up. Override COUNT=N (default 1); JUnit XML in reports/junit/nox/.
	@$(SAY) "nox: FULL matrix — every suite on every Python (x$(NOX_COUNT))"
	@uv run nox -s tests_all -- --count=$(NOX_COUNT) --repeat-scope=session

validate: validate-python validate-ts ## (Build & Release) Validate ALL code (Python + TS): sub-targets validate-python + validate-ts

validate-python: ## (Build & Release) Python validation (clean-dist, static checks, coverage, docs) without building dist
	@$(MAKE) clean-dist \
		&& $(MAKE) check-python \
		&& $(MAKE) $(COVERAGE_TARGET) \
		&& $(MAKE) docs

validate-ts: check-ts coverage-ts ## (Build & Release) TypeScript validation: Biome+knip, tsc, merged coverage gate (unit floor runs inside it via test:coverage; CI's browserless slice is check-ts + coverage-ts-unit)

clean-dist:
	@$(SAY) "removing dist/"
	@rm -rf dist

# ═══ Dev environment ════════════════════════════════════════════════════════

dev: ## (Dev) Set up the dev environment (uv sync, git hooks, hyperfine, Chromium, web/ deps)
	@$(SAY) "uv sync"
	@uv sync
	@$(SAY) "git hooks → .githooks"
	@git config core.hooksPath .githooks
	@$(MAKE) hyperfine
	@$(MAKE) browsers
	@$(MAKE) web-install
	@$(SAY) "dev environment ready"

hyperfine:
	@if [ -x "$(VENV_BIN)/hyperfine" ] && "$(VENV_BIN)/hyperfine" --version | grep -qF "$(HYPERFINE_VERSION)"; then \
		$(SAY) "hyperfine $(HYPERFINE_VERSION) already installed"; \
	else \
		$(SAY) "installing hyperfine $(HYPERFINE_VERSION)"; \
		bash scripts/install_hyperfine.sh "$(HYPERFINE_VERSION)" "$(VENV_BIN)"; \
	fi

browsers: ## (Setup) Install the Playwright Chromium + Firefox + WebKit binaries: the dashboard e2e suite runs on all three engines, and the docs media pipeline uses Chromium. On a box missing a browser's system libs, run `uv run playwright install-deps <chromium|firefox|webkit>` once — the Vagrantfile's dev-root provisioner carries the exact apt package list + how to regenerate it.
	@$(SAY) "playwright install: chromium firefox webkit"
	@uv run playwright install chromium firefox webkit

# web/ (React+TS monitor dashboard) build lanes. `make web` produces the
# dist/ that MonitorServer requires (see server.py's _dist_index_path()) —
# the legacy static dashboard was deleted at the Task 9 cutover, so dist/ is
# now the ONLY frontend and stays in place once built; the browser pin suite
# (tests/e2e/monitor/dashboard) runs against it. (Pre-cutover, a stray dist/
# left behind by a smoke build used to shadow the legacy dashboard.html —
# that's why `make web-clean` exists, but it's no longer required after
# every build.)
# npm ci occasionally hits a transient registry ECONNRESET mid-download in CI
# (issue #107) that npm's own fetch-retries don't catch. Retry ONLY on
# network-class failures (up to 3 attempts, 5s/10s backoff); a deterministic
# error such as a package.json/lockfile drift still fails fast on attempt 1.
# The whole loop is @-silenced: echoing it is 11 lines of shell noise. npm's own
# log is buffered and replayed IN FULL on failure (that's the diagnostic); on
# success only its one-line tally is shown, so the routine case stays quiet.
web-install: ## (Dev) Install web/'s npm dependencies from the committed lockfile (npm ci)
	@$(SAY) "npm ci (web/)"
	@cd web && n=1 && while :; do \
	  log=$$(mktemp); \
	  npm ci >"$$log" 2>&1; rc=$$?; \
	  if [ $$rc -eq 0 ]; then \
	    grep -E '^(added|removed|changed|up to date)' "$$log" || cat "$$log"; \
	    rm -f "$$log"; break; fi; \
	  cat "$$log" >&2; \
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

# Pure delegation, so no banner of its own (rule 5) — and the recipe must stay
# a lone `$(MAKE)` line: make runs `$(MAKE)` lines even under -n, and -n rides
# through MAKEFLAGS so the child dry-runs too. Adding any non-$(MAKE) command
# here (a $(SAY) included) would execute for real during `make -n`.
$(WEB_NODE_MODULES): web/package.json web/package-lock.json
	@$(MAKE) web-install

web: $(WEB_NODE_MODULES) ## (Build & Release) Build the web/ React dashboard + the covapp SPA (vite) into their static dist dirs, then gate both against absolute http(s) URLs (air-gap requirement — labs have no network access, see scripts/check_airgap.sh) and against a resolved-brand-color regression (scripts/check_brand_tokens.sh)
# Regenerate web/src/api/types.gen.ts and web/src/api/export.gen.ts from
# the live pydantic models and fail BEFORE the vite build if either
# committed file has drifted — a stale wire contract should be caught by
# its own diff, not surface later as a build or runtime type error with
# no clue which model changed.
	@$(SAY) "regenerating web/ API types from the live pydantic models"
	@scripts/gen_web_types.sh
	@git diff --exit-code web/src/api/types.gen.ts web/src/api/export.gen.ts
# build_web_no_warnings.sh = vite with warnings-as-errors: any "(!)"
# build warning (chunk budget overrun, rollup notices) fails the build
# instead of scrolling past. The chunk budget itself lives in
# web/vite.config.ts (chunkSizeWarningLimit) / web/vite.covapp.config.ts.
	@$(SAY) "vite build: monitor dashboard (warnings are errors)"
	@scripts/build_web_no_warnings.sh build
	@$(SAY) "vite build: covapp (warnings are errors)"
	@scripts/build_web_no_warnings.sh build:covapp
	@$(SAY) "gating both bundles: air-gap + brand tokens"
	@scripts/check_airgap.sh
	@scripts/check_airgap.sh src/otto/_webassets/covapp
	@scripts/check_brand_tokens.sh
	@scripts/check_brand_tokens.sh src/otto/_webassets/covapp

web-dev: $(WEB_NODE_MODULES) ## (Dev) Run the web/ Vite dev server with hot reload; proxies /api to a running otto monitor (default target http://127.0.0.1:8080, override with VITE_OTTO_TARGET=http://host:port)
	@$(SAY) "vite dev server (web/) — proxying /api to $${VITE_OTTO_TARGET:-http://127.0.0.1:8080}"
	@cd web && npm run dev

# web/ quality lanes moved to the language-parity family (lint-ts /
# typecheck-ts / coverage-ts-unit / test-ts) in the Quality section below —
# one name per aspect, no web-* aliases. web-install/web/web-dev/web-clean
# stay here: they are artifact/dev targets, not language-parity gates.

test-ts: $(WEB_NODE_MODULES) ## (Dev) Run the web/ vitest suite once — no coverage, the fast TS loop. (Deliberately no test-python twin and no bare `test`: the fast Python lane is `coverage-unit`.)
	@$(SAY) "vitest (web/) — no coverage"
	@cd web && npm run test

web-clean: ## (Dev) Remove the built web/ dist outputs (monitor dashboard + covapp) from src/otto/_webassets/
	@$(SAY) "removing built web/ dist (monitor + covapp)"
	@rm -rf src/otto/_webassets/monitor
	@rm -rf src/otto/_webassets/covapp

# uv_build embeds the ENTIRE module tree (src/otto/**) into both the sdist and
# the wheel by default — unlike hatchling, it is not VCS-aware, so it doesn't
# care that _webassets/*/ is .gitignore'd (see the [tool.uv.build-backend]
# comment in pyproject.toml). That makes the embedding implicit rather than
# explicit config, so this target exists to pin it with a real assertion:
# build the dashboard, build the wheel, and fail loudly if the dashboard ever
# stops making it in (e.g. an overbroad wheel-exclude pattern — one narrow
# `**/*.map` exclude exists deliberately, see pyproject.toml — or a uv_build
# default change). Deliberately NOT wired into `coverage` — it rebuilds the frontend
# and a real wheel, which is release-flow overhead, not a per-commit gate.
# Prerequisite composition (clean-dist web build), not $(MAKE) calls in the
# recipe: `make -n wheel-check` must stay dry-run-safe, and GNU make only
# honors -n for prerequisite recursion, not for $(MAKE) invoked from inside a
# recipe line (see the release: warning above for what happens when that rule
# is violated).
# NOTE: prerequisites assume serial execution; do not run wheel-check under make -j.
wheel-check: clean-dist web build ## (Build & Release) Rebuild the dashboard + wheel and assert the wheel embeds both src/otto/_webassets/{monitor/dist,covapp}/ artifacts (air-gap requirement)
	@$(SAY) "asserting dist/*.whl embeds the web assets"
	@for entry in monitor/dist/index.html covapp/index.html; do \
		dir="otto/_webassets/$${entry%%/*}/"; \
		count=$$(unzip -l dist/*.whl | grep -c "$$dir" || true); \
		if [ "$$count" -eq 0 ]; then \
			echo "wheel-check: FAIL — no $$dir entries in dist/*.whl; an air-gapped install would ship without this frontend." >&2; \
			exit 1; \
		fi; \
		if ! unzip -p dist/*.whl "otto/_webassets/$$entry" > /dev/null; then \
			echo "wheel-check: FAIL — otto/_webassets/$$entry missing from dist/*.whl." >&2; \
			exit 1; \
		fi; \
		echo "wheel-check: OK — $$count $$dir entries embedded (incl. $${entry#*/})."; \
	done
	@scripts/check_airgap.sh
	@if unzip -l dist/*.whl | grep -q '\.map$$'; then \
		echo "wheel-check: FAIL — sourcemap (*.map) files embedded in the wheel; the wheel-exclude in pyproject.toml [tool.uv.build-backend] should strip them." >&2; \
		exit 1; \
	fi; \
	echo "wheel-check: OK — no *.map files in the wheel."

docs-media: ## (Docs) Force-regenerate the build-time GUI media (screenshots, clips, termynal blocks) in docs/_static/generated/
	@$(SAY) "capturing docs GUI media (screenshots + clips)"
	@uv run python scripts/capture_docs_media.py --mode force
	@$(SAY) "capturing docs termynal blocks"
	@uv run python scripts/capture_docs_termynal.py --mode force

profile: hyperfine ## (Dev) Enforce the import budget (module-count caps + snapshots + denylist) + hyperfine wall-clock
	@$(SAY) "import budget (module caps + snapshots + denylist) + hyperfine"
	@uv run python scripts/import_budget.py --check --hyperfine

build: ## (Build & Release) Build the project with uv
	@$(SAY) "uv build → dist/"
	@uv build

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
# Runs -n $(BROWSER_WORKERS): 2 (with OTTO_BROWSER_SHARD=1 for per-file
# groups) when the host passes the cores+RAM gate at the variable's
# definition, else the historical -n 1 serial pin — where the shard env
# stays unset so the e2e conftest's grouping policy keeps one group and
# extra workers would only sit idle emitting "No data was collected"
# coverage warnings. CI's dashboard jobs set the env themselves. Writes
# coverage DATA only: --cov-report= suppresses the report so a standalone run
# never stomps reports/coverage/html. Running first as `coverage-python`'s
# direct prerequisite (bare `coverage`'s only transitively, via
# coverage-python), its fresh data file is then extended by the main run's
# --cov-append, folding the browser-driven server/collector lines (e.g. the
# dashboard HTML route, UI event round-trips) into the gated report.
#
# Both suites — and the docs build, whose GUI media is photographed from the
# real shell (see docs/_build/html/index.html below) — drive real build
# artifacts: the React dashboard (src/otto/_webassets/monitor/dist/) and the
# coverage-report SPA (src/otto/_webassets/covapp/index.html).
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
# ── serial_timing legs ──────────────────────────────────────────────────────
# Wall-clock discriminators (`-m serial_timing`) reject the slow path by
# elapsed time, and sibling xdist workers can counterfeit that path as a
# false red — so every parallel lane below excludes the marker and re-appends
# it in a paired `-n0` leg (never exclusion alone: a filtered-out offender is
# CI-invisible). Where a lane enforces a coverage gate, --cov-fail-under
# rides the LAST leg — the serial leg folds in via --cov-append, so only the
# final database holds the lane's whole run. The root conftest fails any
# serial_timing test that reaches an xdist worker, so a future lane that
# forgets the exclusion goes deterministically red, not flaky-green.
# Exclusion↔leg pairing is pinned by tests/unit/test_lane_invariants.py.
coverage-python: dashboard ## Run the full Python suite (all tiers, pinned Python) and enforce the 95 gate; the browser (Playwright) suite runs first as its own process via the `dashboard` prerequisite — its coverage data is folded in via --cov-append. Requires lab VMs (+ `make browsers` once). JUnit XML lands in reports/junit/coverage-python/.
	@$(SAY) "pytest: all tiers, pinned Python (browser lane folded in)"
	@$(LEAK_DETECT) $(TIMEOUT_CMD) uv run pytest -m "not stability and not browser and not serial_timing" --cov-append --cov-fail-under=0 $(call junitxml,coverage-python)
	@$(SAY) "pytest: serial_timing discriminators, -n0 (gate: $(COVERAGE_THRESHOLD)% on the full fold)"
	@$(LEAK_DETECT) $(TIMEOUT_CMD) uv run pytest -m "serial_timing and not stability and not browser" -n0 --cov-append --cov-fail-under=$(COVERAGE_THRESHOLD) $(call junitxml,coverage-python-serial)

coverage: coverage-python coverage-ts ## Run BOTH language coverage gates: coverage-python (full pytest, 95 floor) + coverage-ts (merged vitest+e2e floor). The dashboard browser lane runs exactly once — coverage-python triggers it, and coverage-ts's artifact stamp sees it fresh.

coverage-unit: ## Run the unit level tier (tests/unit only; no testbed) with a coverage report (no gate — one tier can't meet the whole-repo floor). JUnit XML lands in reports/junit/coverage-unit/.
	@$(SAY) "pytest: tests/unit (no gate)"
	@$(LEAK_DETECT) $(TIMEOUT_CMD) uv run pytest tests/unit -m "not stability and not serial_timing" $(call junitxml,coverage-unit)
	@$(LEAK_DETECT) $(TIMEOUT_CMD) uv run pytest tests/unit -m "serial_timing and not stability" -n0 --cov-append $(call junitxml,coverage-unit-serial)

coverage-integration: ## Run the unit + integration level tiers (tests/unit + tests/integration) with a coverage report (no gate). Requires the full lab. JUnit XML in reports/junit/coverage-integration/.
	@$(SAY) "pytest: tests/unit + tests/integration (no gate)"
	@$(LEAK_DETECT) $(TIMEOUT_CMD) uv run pytest tests/unit tests/integration -m "not stability and not serial_timing" $(call junitxml,coverage-integration)
	@$(LEAK_DETECT) $(TIMEOUT_CMD) uv run pytest tests/unit tests/integration -m "serial_timing and not stability" -n0 --cov-append $(call junitxml,coverage-integration-serial)

coverage-hostless: ## Run the no-testbed CI gate suite (tests/unit + no-VM e2e) and enforce the CI coverage gate. No VMs. JUnit XML lands in reports/junit/coverage-hostless/.
	@$(SAY) "pytest: hostless CI slice, no VMs"
	@$(LEAK_DETECT) $(TIMEOUT_CMD) uv run pytest tests/unit tests/e2e -m "$(M_HOSTLESS) and not serial_timing" --cov-fail-under=0 $(call junitxml,coverage-hostless)
	@$(SAY) "pytest: serial_timing discriminators, -n0 (gate: $(CI_COVERAGE_THRESHOLD)% on the full fold)"
	@$(LEAK_DETECT) $(TIMEOUT_CMD) uv run pytest tests/unit tests/e2e -m "serial_timing and $(M_HOSTLESS)" -n0 --cov-append --cov-fail-under=$(CI_COVERAGE_THRESHOLD) $(call junitxml,coverage-hostless-serial)

coverage-unix: ## Run the Unix-VM resource slice (incl. multi-hop) with a coverage report (no gate). Requires lab VMs. JUnit XML in reports/junit/coverage-unix/.
	@$(SAY) "pytest: Unix-VM slice, incl. multi-hop (no gate)"
	@$(LEAK_DETECT) $(TIMEOUT_CMD) uv run pytest -m "$(M_UNIX) and not serial_timing" $(call junitxml,coverage-unix)
	@$(LEAK_DETECT) $(TIMEOUT_CMD) uv run pytest -m "serial_timing and $(M_UNIX)" -n0 --cov-append $(call junitxml,coverage-unix-serial)

coverage-embedded: ## Run the embedded (Zephyr) resource slice with a coverage report (no gate). Requires Vagrant lab up. JUnit XML in reports/junit/coverage-embedded/.
	@$(SAY) "pytest: embedded/Zephyr slice (no gate)"
	@$(LEAK_DETECT) $(TIMEOUT_CMD) uv run pytest -m "$(M_EMBEDDED)" $(call junitxml,coverage-embedded)

DASHBOARD_BROWSERS ?= chromium
# Browser-lane worker count. The suites are parallel-safe by construction
# (port=0 servers, pid+uuid coverage dumps — see the policy block in
# tests/e2e/conftest.py); the historical -n 1 pin was a RAM policy from the
# 3GB-dev-VM era, so the gate keys on the actual scarce resources: 2 workers
# when the host has >=2 CPUs AND >=6GiB physical RAM, else the serial
# fallback. Measured on the 8GB dev VM: 75 tests 51.5s -> 28.9s (1.8x);
# -n 3 adds nothing because test_review_shell alone is the wall-clock floor
# (per-FILE groups cannot split a module). Override with BROWSER_WORKERS=N.
BROWSER_WORKERS ?= $(shell if [ "$$(nproc)" -ge 2 ] && [ "$$(awk '/MemTotal/ {print $$2}' /proc/meminfo)" -ge 6291456 ]; then echo 2; else echo 1; fi)
# Sharding env must accompany >1 worker: without OTTO_BROWSER_SHARD=1 the
# suites share one serial xdist group and extra workers would sit idle.
# Recursive (=), not :=, so a command-line BROWSER_WORKERS override flows in.
BROWSER_SHARD_ENV = $(if $(filter-out 1,$(BROWSER_WORKERS)),OTTO_BROWSER_SHARD=1,)
DASHBOARD_DIST := src/otto/_webassets/monitor/dist/index.html
COVAPP_DIST := src/otto/_webassets/covapp/index.html

# Everything vite feeds into the two bundles: the app sources (including the
# committed api/*.gen.ts, which is the seam through which a pydantic-model
# change reaches the frontend — `make web` regenerates and diff-gates them),
# the html entries, the tsc/vite configs, and the dependency manifests. Biome's
# config and web/fixtures/ are deliberately absent: neither is a build input.
WEB_SRCS := $(shell find web/src -type f) \
            web/index.html               \
            web/covapp.html              \
            web/tsconfig.json            \
            web/vite.config.ts           \
            web/vite.covapp.config.ts    \
            web/package.json             \
            web/package-lock.json

$(DASHBOARD_DIST) $(COVAPP_DIST) &: $(WEB_SRCS) $(WEB_NODE_MODULES)
	@$(MAKE) web

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
# The two browser suites' own files, plus the shared tests/_fixtures/ modules
# they import (the harness, guard, fake collector, fixture-report builder, and
# the CDP-coverage collector) — editing any of these changes what the browser
# lane exercises, so the raw-coverage stamp must depend on them too.
BROWSER_TEST_SRCS := $(shell find tests/e2e/monitor/dashboard tests/e2e/cov/report_browser -name '*.py') \
                     tests/_fixtures/_ts_coverage.py       \
                     tests/_fixtures/_browser_guard.py      \
                     tests/_fixtures/_dashboard_harness.py  \
                     tests/_fixtures/_fake_collector.py     \
                     tests/_fixtures/_report_fixture.py

$(TS_E2E_RAW_STAMP): $(WEB_SRCS) $(BROWSER_TEST_SRCS)
	@$(MAKE) dashboard

$(TS_E2E_COV): $(TS_E2E_RAW_STAMP) $(WEB_NODE_MODULES) web/scripts/e2e_coverage_report.mjs
	@$(SAY) "merging raw V8 browser coverage → istanbul"
	@cd web && npm run e2e:coverage-report

# The `-m "browser and not soak"` below MUST match noxfile.py's
# DASHBOARD_MARKER_EXPR (the `dashboard` session's marker, which is what
# CI's `dashboard-e2e` job actually runs via `uv run nox -k <browser>` — NOT
# this target). See that constant's comment for why the two can't share one
# literal source and for the concrete incident (soak ran on every push, on
# every engine, until nox's expression was brought back in line with this
# one) that makes keeping them in step worth a standing comment. If this
# expression changes, change noxfile.py's too.
dashboard: $(DASHBOARD_DIST) $(COVAPP_DIST) ## Run the browser e2e suites (monitor dashboard + coverage report) on DASHBOARD_BROWSERS (default: chromium — feeds `coverage`). Full matrix: `make dashboard-all`. Needs `make browsers` once; (re)builds web/'s dist bundles when missing or older than web/src/ (see `make web`). Excludes `soak` (see `dashboard-soak`) — minutes of pushing, not a per-task gate.
	@$(SAY) "playwright e2e: $(DASHBOARD_BROWSERS) — $(BROWSER_WORKERS) worker(s)"
	@rm -rf reports/ts-e2e-cov/raw
# OTTO_TS_COVERAGE arms the browser suites' CDP V8-coverage collection
# (tests/_fixtures/_ts_coverage.py). Only make sets it, so ad-hoc or `nox`
# runs of these suites don't append raw dumps outside this recipe's rm+stamp
# protocol — which would let `make coverage-ts` merge in a browser run make
# never scheduled.
	@$(BROWSER_SHARD_ENV) OTTO_TS_COVERAGE=1 $(TIMEOUT_CMD) uv run pytest tests/e2e/monitor/dashboard tests/e2e/cov/report_browser -m "browser and not soak" $(foreach b,$(DASHBOARD_BROWSERS),--browser $(b)) -n $(BROWSER_WORKERS) --cov-report= --screenshot only-on-failure --output reports/playwright $(call junitxml,dashboard)
	@mkdir -p reports/ts-e2e-cov/raw && touch $(TS_E2E_RAW_STAMP)

dashboard-all: ## Run the dashboard e2e on ALL engines (Chromium + Firefox + WebKit); invoked by `make release`. Needs `make browsers` once.
	@$(MAKE) dashboard DASHBOARD_BROWSERS="chromium firefox webkit"

# --browser chromium is intentionally hardcoded, not DASHBOARD_BROWSERS:
# measured directly, the soak passes on Chromium in ~15s but WebKit's main
# thread can't answer a single DOM read within Playwright's 60s action
# timeout under the ~180k-point SSE firehose (see test_replay_soak.py's
# module docstring for the measurement). The test itself now skips loudly
# on any non-chromium `browser_name`, so this flag is belt-and-suspenders,
# not the only guard.
# -n0, not -n 1: the lane's only test is a serial_timing discriminator (it
# bounds a DOM read's elapsed time), and the root conftest fails any such test
# that lands in an xdist worker — `-n 1` is one worker, not no workers. The
# usual exclusion+serial-leg pairing does not fit a single-test lane: the
# non-serial leg would select nothing and pytest would exit 5. This lane was
# already serial by intent, as the SAY line says; now it is serial in fact.
dashboard-soak: $(DASHBOARD_DIST) ## Run the dashboard replay soak (Tier-3, `soak`-marked; NOT part of `make dashboard`/`make coverage`) — drives FakeCollector at max rate in-process, no VM. Chromium only (see comment above). JUnit XML lands in reports/junit/dashboard-soak/.
	@$(SAY) "playwright soak: SSE replay (chromium, serial, no coverage)"
	@$(TIMEOUT_CMD) uv run pytest tests/e2e/monitor/dashboard/test_replay_soak.py -m "browser and soak" --browser chromium -n0 --no-cov --screenshot only-on-failure --output reports/playwright $(call junitxml,dashboard-soak)

# Soak/stability + repeat targets disable coverage (--no-cov, overriding the
# --cov in pytest addopts). Per-test `--cov-context=test` tracing adds overhead
# to every one of the COUNT-multiplied iterations and, on slow CI runners under
# xdist, helps push tight per-test timeouts over their wall-clock budget. These
# runs exist to flush flakes, not to measure coverage — that's `make coverage`.
stability-unit: ## Run no-VM SessionManager concurrency/soak tests by marker. JUnit XML lands in reports/junit/stability-unit/. Override iterations with COUNT=N (default 50).
	@$(SAY) "pytest soak: concurrency marker, no VMs (x$(STABILITY_UNIT_COUNT), leak detector on)"
	@$(LEAK_DETECT) uv run pytest \
	    -m "concurrency and not serial_timing" \
	    --count=$(STABILITY_UNIT_COUNT) \
	    -p no:cacheprovider \
	    --no-cov \
	    $(call junitxml,stability-unit)
	@$(SAY) "pytest soak: serial_timing discriminators, -n0 (x$(STABILITY_UNIT_COUNT))"
	@$(LEAK_DETECT) uv run pytest \
	    -m "serial_timing and concurrency" \
	    --count=$(STABILITY_UNIT_COUNT) \
	    -n0 \
	    -p no:cacheprovider \
	    --no-cov \
	    $(call junitxml,stability-unit-serial)

stability-unix: ## Real telnet/SSH soak against the Unix Vagrant VMs (incl. multi-hop). Requires lab VMs. JUnit XML in reports/junit/stability-unix/. Override iterations with COUNT=N (default 10).
	@$(SAY) "pytest soak: real telnet/SSH on the Unix VMs (x$(STABILITY_UNIX_COUNT), leak detector on)"
	@$(LEAK_DETECT) uv run pytest \
	    -m "stability and integration and not embedded and not hops and not chaos" \
	    --count=$(STABILITY_UNIX_COUNT) \
	    -p no:cacheprovider \
	    --no-cov \
	    $(call junitxml,stability-unix)

# -n0 for the whole lane rather than an exclusion + serial leg. Every module
# under tests/e2e/tunnel_stability carries xdist_group("link_tunnels_e2e") and
# addopts sets --dist loadgroup, so all of these tests already land on ONE
# worker with its siblings idle: sibling counterfeiting was never possible
# here, and splitting the lane would only buy a second full live-bed
# setup/teardown. Same reasoning as dashboard-soak below.
stability-tunnel: ## Tunnel soak against the live bed (churn/concurrency/traffic/adversity/health/monitor-loop). Requires lab VMs. JUnit XML in reports/junit/stability-tunnel/. COUNT=N repeats the suite (default 1); CYCLES=N sets internal loop depth (default 5).
	@$(SAY) "pytest soak: tunnels on the live bed (x$(STABILITY_TUNNEL_COUNT), $(STABILITY_TUNNEL_CYCLES) cycles/test)"
	@$(LEAK_DETECT) OTTO_TUNNEL_SOAK_CYCLES=$(STABILITY_TUNNEL_CYCLES) uv run pytest \
	    tests/e2e/tunnel_stability \
	    -m "stability and hops" \
	    --count=$(STABILITY_TUNNEL_COUNT) \
	    -n0 \
	    -p no:cacheprovider \
	    --no-cov \
	    $(call junitxml,stability-tunnel)

stability-embedded: ## Cross-OS stability contract against real telnet/SSH targets (Zephyr). Requires Vagrant lab up. JUnit XML lands in reports/junit/stability-embedded/. Override iterations with COUNT=N (default 1).
	@$(SAY) "pytest soak: cross-OS contract incl. Zephyr (x$(STABILITY_EMBEDDED_COUNT), leak detector on)"
	@$(LEAK_DETECT) uv run pytest \
	    -m "stability and embedded and not chaos" \
	    -p no:cacheprovider \
	    --no-cov \
	    --count=$(STABILITY_EMBEDDED_COUNT) \
	    $(call junitxml,stability-embedded)

chaos: ## Tier-3 chaos lane, unix legs: interrupt/SIGKILL/reboot scenarios on a leased bed host. Requires lab VMs and EXCLUSIVE bed use (never co-run with other bed lanes). JUnit XML in reports/junit/nox-chaos/.
	@$(SAY) "pytest chaos: tier-3 scenarios on the live bed (unix legs, leak detector on)"
	@$(LEAK_DETECT) uv run nox -s chaos

chaos-embedded: ## Tier-3 chaos lane, zephyr console leg (console-client-death). Can wedge a board — run deliberately; a failure may need a zephyr bed restart. JUnit XML in reports/junit/nox-chaos-embedded/.
	@$(SAY) "pytest chaos: zephyr console scenarios (leak detector on)"
	@$(LEAK_DETECT) uv run nox -s chaos_embedded

stability: ## Run the full stability/soak suite: no-VM concurrency, then real telnet/SSH (Unix + embedded). Runs all tiers even if an earlier one is RED. Requires lab VMs for tiers 2-3. Override iterations with COUNT=N.
	@$(SAY) "Tier 1 — unit-level concurrency"
	-@$(MAKE) stability-unit COUNT=$(COUNT)
	@echo
	@$(SAY) "Tier 2 — real telnet/SSH"
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
	@$(SAY) "Tier 2b — tunnel soak"
	@$(MAKE) stability-tunnel $(if $(filter command line,$(origin COUNT)),COUNT=$(COUNT)) $(if $(filter command line,$(origin CYCLES)),CYCLES=$(CYCLES))
	@echo
	@$(SAY) "Tier 3 — cross-OS stability contract (includes embedded)"
	@$(MAKE) stability-embedded COUNT=$(COUNT)

repeat: ## Run the full local suite (unit + integration + e2e) under pytest-repeat (excludes `browser` — see note above M_HOSTLESS; run its soak separately). Local only; requires VMs. JUnit XML in reports/junit/repeat/. Override COUNT=N (default 10).
	@$(SAY) "pytest soak: full local suite, no browser (x$(COUNT), leak detector on)"
	@$(LEAK_DETECT) uv run pytest \
	    -m "not browser and not chaos and not serial_timing" \
	    --count=$(COUNT) \
	    -p no:cacheprovider \
	    --no-cov \
	    $(call junitxml,repeat)
	@$(SAY) "pytest soak: serial_timing discriminators, -n0 (x$(COUNT))"
	@$(LEAK_DETECT) uv run pytest \
	    -m "serial_timing and not browser and not chaos" \
	    -n0 \
	    --count=$(COUNT) \
	    -p no:cacheprovider \
	    --no-cov \
	    $(call junitxml,repeat-serial)

# ═══ Lab ════════════════════════════════════════════════════════════════════

vm-health: ## (Lab) Probe every lab VM + Zephyr QEMU instance; prints per-host timestamps + clock drift. Requires the Vagrant lab up.
	@$(SAY) "probing lab VMs + Zephyr QEMU (timestamps + clock drift)"
	@uv run python scripts/lab_health.py

qemu-restart: ## (Lab) Restart the Zephyr QEMU + SNMP-relay units on the hop VM(s), then health-check. Use to recover a wedged embedded bed.
	@$(SAY) "restarting Zephyr QEMU + SNMP relay, then health-checking"
	@uv run python scripts/lab_health.py --restart-qemu

# ═══ Quality: static analysis + autofix ═════════════════════════════════════

lint: lint-python lint-ts ## (Quality) Lint ALL code (Python + TS): sub-targets lint-python + lint-ts

# `lint-arch` is a PREREQUISITE here, not a separate thing to remember. CI's
# lint-python JOB is `nox -s lint`, which runs ruff AND tach AND ast-grep (see
# the session docstring in noxfile.py); this target is that job's local twin
# and has to match it — matched, not maximal, per the gate-twin rule. It ran
# ruff only until 2026-08-10, so the make target and the CI job of the same
# name disagreed, and a file could pass `make lint`, `make format` and every
# coverage lane while still carrying an architecture violation. Two did, and
# the pre-push `gate-fresh` hook is what stopped them.
#
# The arch rules mostly police TEST code (deadline polls, path arithmetic,
# module-scope env writes), so "it's only a test" is the case that needs this
# most. Make remakes a target once per invocation, so `check-python` and
# `gate-fresh` naming `lint-arch` alongside `lint-python` costs nothing.
lint-python: lint-arch ## (Quality) Ruff lint + format checks AND the architecture gates — local twin of CI's lint-python job (`nox -s lint`)
	@$(SAY) "ruff: lint + format check"
	@uv run ruff check .
	@uv run ruff format --check .

# lint-arch enforces the architecture rules ruff cannot express: tach.toml is
# the module-dependency ratchet baseline (its comments explain every DEBT
# edge and forbid `tach sync` as a "fix"); .ast-grep/rules/ hold the
# scope-sensitive pattern rules. Policy background:
# todo/churn-and-design-review-2026-08-03.md §5.
lint-arch: ## (Quality) Architecture gates: tach (module dependency contracts) + ast-grep (pattern rules)
	@$(SAY) "tach: module dependency contracts (tach.toml)"
	@uv run --group lint tach check
	@$(SAY) "ast-grep: architecture pattern rules (.ast-grep/rules/)"
	@uv run --group lint ast-grep scan src/otto web/src tests

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
# --error-on-warnings lives in web/package.json's `check` script, not here, so
# a bare `npm run check` in web/ enforces the same bar as CI. Biome exits 0 on
# warnings by default: 7 noNonNullAssertion warnings sat in tickets.test.ts
# under a green gate until 2026-07-28. There is no warn tier on the Python
# side (ruff errors only, pytest filterwarnings=error) and there is not one here.
lint-ts: $(WEB_NODE_MODULES) ## (Quality) Lint web/: the authoritative Biome gate (rules + format + assists) + knip (unused exports/files/deps)
	@$(SAY) "biome check (web/): rules + format + assists"
	@cd web && npm run check
	@$(SAY) "knip (web/): unused exports, files, deps"
	@cd web && npm run knip

format: format-python format-ts ## (Quality) Apply ALL safe autofixes (Python + TS): sub-targets format-python + format-ts

# "format" means: after this, everything auto-fixable that `make lint` gates
# is fixed — not merely reformatted. That is why the Python leg runs ruff's
# safe lint fixes before the formatter (fixes can need reformatting), and the
# TS leg runs `biome check --write` (biome format alone cannot apply assist
# actions like organize-imports, which lint-ts gates).
format-python: ## (Quality) Apply ruff safe lint autofixes + autoformat
	@$(SAY) "ruff: safe lint autofixes + format"
	@uv run ruff check --fix .
	@uv run ruff format .

format-ts: $(WEB_NODE_MODULES) ## (Quality) Apply Biome fixes to web/: rules + format + assists (`biome check --write`)
	@$(SAY) "biome check --write (web/): rules + format + assists"
	@cd web && npm run check:fix

typecheck: typecheck-python typecheck-ts ## (Quality) Type-check ALL code (Python + TS): sub-targets typecheck-python + typecheck-ts

typecheck-python: ## (Quality) Run ty type checker
	@$(SAY) "ty check"
	@uv run ty check

# Routed through scripts/typecheck_web.sh rather than `npm run typecheck`
# because tsconfig cannot scope rules by directory: `exclude` only drops a
# file from the program's ROOT set, and a file reached through an import is
# checked anyway, so vendored Untitled UI source cannot be exempted in config.
# The script derives its vendored path list from web/untitledui.lock.json --
# the same source scripts/check_untitledui_hash.sh reads -- so the gate that
# forbids editing those files and the gate that stops grading them cannot
# disagree about which files those are. web/package.json's `typecheck` script
# stays the raw, unfiltered tsc for ad-hoc use.
typecheck-ts: $(WEB_NODE_MODULES) ## (Quality) Type-check web/ with tsc --noEmit (no build), vendored Untitled UI diagnostics filtered out
	@$(SAY) "tsc --noEmit (web/) — vendored Untitled UI diagnostics filtered"
	@scripts/typecheck_web.sh

check: check-python check-ts ## (Quality) ALL static analysis (Python + TS): sub-targets check-python + check-ts

check-python: lint-python typecheck-python lint-arch ## (Quality) All Python static analysis: ruff (lint+format) + ty + architecture gates (lint-arch)

# REF reaches the shell through the environment, not Make's text
# substitution, and only when given explicitly on the command line — two
# separate Make footguns, both closed below:
#
# 1. `$(REF)` re-expands whatever `$`-sequences are IN the value (Make
#    evaluates the value, not the shell): `feature/$(build)` silently
#    truncates to `feature/` (calls undefined variable/function "build"),
#    `my $HOME ref` eats the "H" (`$H` is Make's one-letter-name reference
#    syntax), and `$(shell touch x)` actually runs `touch` the moment this
#    text is expanded — even under `make -n`, since a dry run still expands
#    recipe text to print it. `$(value REF)` sidesteps all of this: it
#    yields REF's stored text WITHOUT expanding it, so a `$` inside the
#    value is never re-interpreted as a Make reference. Assigning that
#    through `:=` (immediate, simple) rather than `=` means the result
#    isn't re-expanded again later either.
# 2. GNU Make auto-imports the invoking shell's environment as Make
#    variables, so a developer with an unrelated `REF` exported would
#    otherwise have it silently picked up. `$(origin REF)` reports where a
#    variable's current value came from; gating on `command line` (the same
#    convention `COUNT` already uses above) accepts an explicit
#    `make gate-fresh REF=...` while ignoring an ambient-only one.
#
# The value is exported under its own name (GATE_FRESH_REF) rather than
# reassigning REF itself. GNU Make's command-line precedence means this
# isn't a correctness fix — a target-specific `REF :=` here would itself be
# a no-op whenever REF came from the command line, and `$(origin REF)`
# would keep reporting `command line` regardless (verified empirically).
# The separate name earns its keep on clarity instead: it reads at the call
# site as "the ref this recipe resolved to use", keeps the recipe from
# depending on Make's override precedence to stay correct if this logic is
# ever refactored, and avoids a plain `REF` in the child process's
# environment shadowing anyone else's expectations of that name.
gate-fresh: export GATE_FRESH_REF := $(if $(filter command line,$(origin REF)),$(value REF),)
gate-fresh: ## (Quality) Run CI's assets-absent Python lanes (lint-python + lint-arch + typecheck-python + coverage-hostless) against the COMMITTED tree in a throwaway pristine worktree at REF (default HEAD). Catches gitignored-artifact, unsynced-uv.lock and forgotten-`git add` failures that the dev tree hides. Refuses if tracked files are modified or staged.
	@$(SAY) "gate-fresh: pristine worktree, assets-absent CI lanes"
	@uv run python scripts/gate_fresh.py $(if $(filter command line,$(origin REF)),--ref "$$GATE_FRESH_REF",)

# The vendored-source leg is deliberately part of check-ts rather than a
# post-build gate like check_airgap.sh / check_brand_tokens.sh: it reads the
# committed tree, not a built artifact, so it needs no `make web` (and no
# node_modules, and no network) and belongs with the other static gates that
# run on every push. It is the cheap half of a two-part contract with
# scripts/check_untitledui_drift.sh — this one answers "did WE edit the
# vendored source?" in under a second; the weekly drift workflow answers
# "did UPSTREAM change?" over the network. The drift check alone cannot tell
# those apart, so without this leg a hand-edit here is reported as upstream
# drift, forever, under the wrong title (issue #177).
check-ts: lint-ts typecheck-ts ## (Quality) All TS static analysis: Biome + knip (lint-ts) + tsc + the vendored Untitled UI never-hand-edited gate
	@$(SAY) "untitledui vendored source: contentHash vs web/untitledui.lock.json"
	@scripts/check_untitledui_hash.sh

coverage-ts-unit: $(WEB_NODE_MODULES) ## (Quality) Run the web/ vitest suite with v8 coverage and enforce the UNIT-tier floor (the TS analogue of coverage-hostless's reduced CI gate; the full merged gate is coverage-ts)
	@$(SAY) "vitest coverage (web/) — unit-tier floor"
	@cd web && npm run test:coverage

# The FULL TS coverage gate: vitest (unit) + the Playwright e2e leg, merged
# into ONE istanbul report and gated at the merged floor. The vitest-only
# floor (coverage-ts-unit, enforced inside vite.config.ts) is the reduced
# browserless tier CI runs — the exact analogue of coverage-hostless's 90 vs
# the full gate's 95 on the Python side.
coverage-ts: $(TS_E2E_COV) ## (Quality) Merged TS coverage gate: vitest + browser-e2e legs, one report, one floor (see also coverage-ts-unit)
	@$(SAY) "vitest coverage (web/) — unit leg"
	@cd web && npm run test:coverage
	@$(SAY) "merging vitest + browser-e2e coverage — merged floor"
	@rm -rf reports/ts-cov/final && mkdir -p reports/ts-cov/final
	@cp web/coverage/coverage-final.json reports/ts-cov/final/vitest.json
	@cp $(TS_E2E_COV) reports/ts-cov/final/e2e.json
	@cd web && npm run coverage:merged

schema: ## (Dev) Generate JSON Schema for lab.json / settings.toml / reservations into schemas/ (git-ignored; for editor autocomplete)
	@$(SAY) "exporting JSON Schema → schemas/"
	@uv run otto schema export --out schemas

monitor-fixtures: ## (Dev) Regenerate the committed monitor dummy-data fixtures in web/fixtures/ (spec 2026-07-10)
	@$(SAY) "regenerating monitor fixtures → web/fixtures/"
	@uv run python scripts/gen_monitor_fixtures.py web/fixtures

import-snapshot: ## (Dev) Regenerate import-budget golden snapshots + print per-surface counts (run after an intentional import change, then review the diff and update caps)
	@$(SAY) "updating import-budget golden snapshots"
	@uv run python scripts/import_budget.py --update

# ═══ Docs ═══════════════════════════════════════════════════════════════════

SPHINX_SRCS :=  docs/conf.py                        \
                $(shell find docs -name '*.rst')    \
                $(shell find docs -name '*.md')    \
                $(shell find src/otto -name '*.py') \

docs: docs-lint docs-html doctest doctest-src ## (Docs) Build HTML docs + Sphinx & src doctests (sub-targets: docs-lint, docs-html, doctest, doctest-src, docs-inventories)

docs-lint:
	@$(SAY) "doc8 + markdown-doctest lint (docs/)"
	@uv run doc8 docs/
	@uv run python scripts/lint_markdown_doctests.py docs/

docs-html: docs/_build/html/index.html

docs-inventories:
	@$(SAY) "fetching intersphinx inventories → docs/_inventories/"
	@mkdir -p docs/_inventories
	@curl -sSL --retry 3 -o docs/_inventories/python.inv     https://docs.python.org/3/objects.inv
	@curl -sSL --retry 3 -o docs/_inventories/typer.inv      https://typer.tiangolo.com/objects.inv
	@curl -sSL --retry 3 -o docs/_inventories/rich.inv       https://rich.readthedocs.io/en/stable/objects.inv
	@curl -sSL --retry 3 -o docs/_inventories/pydantic.inv   https://docs.pydantic.dev/latest/objects.inv
	@curl -sSL --retry 3 -o docs/_inventories/asyncssh.inv   https://asyncssh.readthedocs.io/en/stable/objects.inv
	@curl -sSL --retry 3 -o docs/_inventories/pytest.inv     https://docs.pytest.org/en/stable/objects.inv
	@curl -sSL --retry 3 -o docs/_inventories/telnetlib3.inv https://telnetlib3.readthedocs.io/en/latest/objects.inv

# -E (fresh env, no stale doctrees) + -a (write all) make a local build match
# CI's clean build, so incremental state can't mask or invent a warning.
#
# The dist prerequisites are load-bearing, not decorative: docs/conf.py runs
# scripts/capture_docs_media.py, which boots a real MonitorServer and
# photographs the REAL frontend through headless Chromium. That server serves
# the built dist, so without these the docs build happily photographs whatever
# stale bundle is lying around — or, on a fresh worktree, none at all.
docs/_build/html/index.html: $(SPHINX_SRCS) $(DASHBOARD_DIST) $(COVAPP_DIST)
	@$(SAY) "sphinx-build html (clean rebuild, warnings are errors)"
	@uv run sphinx-build -E -a -W -b html docs/ docs/_build/html

doctest:
	@$(SAY) "sphinx-build doctest"
	@uv run sphinx-build -E -b doctest docs/ docs/_build/doctest

doctest-src:
	@$(SAY) "pytest --doctest-modules src/otto"
# `-p no:tach` re-stated: the -o override drops pyproject's addopts whole, and
# this venv can carry tach after a `uv run --group lint` (issue #193). Pinned
# by tests/unit/test_lane_invariants.py.
	@uv run pytest -p no:cacheprovider -o addopts="--doctest-modules -p no:tach" src/otto

# web-clean is a prerequisite because the built frontend IS a generated
# artifact, and omitting it made this target quietly dishonest: a `make clean`
# followed by `make docs` used to keep serving a stale dashboard dist, because
# nothing in the clean removed it and the dist rule (see DASHBOARD_DIST) only
# rebuilt a MISSING bundle. Source-gated dist prerequisites now catch the stale
# case on their own, but "all generated artifacts" should still mean all of
# them. docs/_static/generated/ is the media capture's own output and is
# stamp-managed (it regenerates when its inputs move), so it stays.
clean: web-clean ## (Dev) Remove all generated artifacts
	@$(SAY) "removing dist/ reports/ docs/_build/"
	@rm -rf dist
	@rm -rf reports
	@rm -rf docs/_build
# Reset the embedded-gcov submodule(s) to pristine. This discards the gcc-12+
# patch that product/build.sh applies; that patch is tracked
# (tests/repo3/third_party/patches/) and re-applied idempotently on the next
# build, so resetting here keeps the submodule from drifting between builds (a
# stale patch/build is what desyncs .gcno and trips gcov's "stamp mismatch").
	@$(SAY) "resetting submodules to pristine"
	@git submodule foreach --recursive 'git reset --hard && git clean -fdx' >/dev/null

help: ## Show this help message
	@printf '\n\033[1mTesting\033[0m  (COUNT=N overrides iterations; omit the suffix to run all tiers)\n'
	@printf '  scope:  unit < integration < (all)   ·   unix · embedded   ·   hostless = no-VM CI gate\n'
	@printf '  \033[36m%-30s\033[0m %s\n' 'coverage-*'   'pinned Python + coverage    (bare coverage = BOTH languages: coverage-python, gated 95, + coverage-ts merged; hostless gated 90)'
	@printf '  \033[36m%-30s\033[0m %s\n' 'nox-*'        'every suffix, all Pythons   (bare nox = full on primary + hostless on rest; nox-full = full matrix)'
	@printf '  \033[36m%-30s\033[0m %s\n' 'stability-*'  'pytest-repeat soak          (unit · unix · tunnel · embedded; bare stability = all tiers)'
	@printf '  \033[36m%-30s\033[0m %s\n' 'chaos / chaos-embedded' 'tier-3 chaos lane (opt-in, bed-hostile; unix legs · zephyr console)'
	@printf '  \033[36m%-30s\033[0m %s\n' 'repeat'       'soak the full unit suite (pytest-repeat)'
	@awk 'BEGIN { FS=":.*?## "; n=split("Build & Release|Quality|Docs|Lab|Dev",order,"|") } /^[a-zA-Z_-]+:.*## \(/ { d=$$2; s=d; sub(/\).*/,"",s); sub(/^\(/,"",s); sub(/^\([^)]*\) */,"",d); items[s]=items[s] sprintf("  \033[36m%-16s\033[0m %s\n",$$1,d) } END { for(i=1;i<=n;i++) if(order[i] in items) printf "\n\033[1m%s\033[0m\n%s",order[i],items[order[i]] }' \
		$(MAKEFILE_LIST)
