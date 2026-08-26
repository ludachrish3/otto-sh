# Periodic review 2026-08-25 — public API, churn, flakiness

**Window:** `d731887a` (2026-08-06, the last squash of the 2026-08-03 churn
review) → `f3ee7552` (HEAD, 2026-08-25). 199 commits (150 human, 49
dependabot), **four releases** inside it: v0.8.4 (08-16), v0.8.5 (08-19),
v0.8.6 (08-22), v0.8.7 (08-25).

**Method:** three parallel read-only sweeps (public API, churn, flakiness),
then every headline claim below was re-verified by hand against the tree,
`gh`, and the CI job logs. **No test, gate, or build was run** — the
integration conftest reaps the lab's docker stacks for any run under it, and
the dev VM shares a bed with live sessions. Claims that rest only on a
sweep's reading are marked *(sweep)*; everything else was checked directly.

Prior reviews this continues: `todo/churn-and-design-review-2026-08-03.md`
(the menu), `todo/churn-review-remaining-work-2026-08-05.md`,
`todo/test-infra-review-2026-08-06.md`.

---

## 0. Verdicts

1. **CI is red at HEAD, and the two most recent commits on `main` are the
   cause.** Runs `32888520702` (`82c7cac7`) and `32897991866` (`f3ee7552`)
   fail 8 of 16 jobs each: `unit-repeat`, both `busybox-artifacts` legs, and
   all five `tests_hostless` legs. Issues #261/#262/#263 are the auto-filed
   records. Root cause in every job: `busybox.net` unreachable from the
   runners (`BusyBoxUnavailableError … <urlopen error _ssl.c:1000: The
   handshake operation timed out>`), surfacing as a collection-time
   `INTERNALERROR`. The two commits were the fix for #261 and they widened
   the blast radius instead of closing it (§1.1). Nightly is healthy (11
   consecutive greens since 08-14); Release workflows are clean.
2. **The public API has no compatibility policy at all, and the repo says so
   out loud.** `AGENTS.md:10`: *"Do not worry about backwards compatibility
   at this time."* 21 `!`-marked commits since v0.8.0, every v0.8.x release
   carries at least one, and `make release` defaults to `BUMP ?= patch`
   (`Makefile:7`). The surface with the highest blast radius — a user repo's
   `settings.toml` / `lab.json` / decorator import paths — is the one that
   churned hardest (4 breaks, all hard parse failures under `extra="forbid"`).
   The ast-grep-migration idea, measured against the actual breaks, covers
   14% of them; the plan in §2 redirects that effort.
3. **Churn is feature-shaped, not sweep-shaped, but the hubs keep growing and
   the structural menu is untouched for a third review.** Refactor share is
   back at 2.7% (July: 1.9%; the Aug 1-5 remediation wave: 14.5%). Measured
   in *code* lines (docstrings stripped): `host/host.py` +54%,
   `cli/invoke.py` +63%, `host/userland.py` new at 1,515 code lines and the
   window's top hotspot by 2×. Tier 1.2/1.8/1.9/1.10 of the 2026-08-03 menu
   have zero movement. The gate culture is the counterweight: ast-grep rules
   9 → 21, every ratchet held under +9,600 code lines, zero reverts.
4. **The test suite is fundamentally sound — no retry automation anywhere,
   red means red — but three items are release-blocking**, and one of them is
   a product bug a user hits with no test involved: the dashboard's SSE
   drop-oldest queue silently evicts the once-only `chart_map` frame (§1.2).

---

## 1. Flakiness

### 1.1 MUST FIX — the BusyBox network dependency in the default gate

`tests/_fixtures/busybox.py:462` is the **only external-network call in the
entire test tree** *(sweep, verified by grep: every other socket under
`tests/unit/` is loopback on port 0)*. The #261 fix (`82c7cac7` cache +
`f3ee7552` `preflight()`) has four defects, all verified:

- **g1 — the cache cannot bootstrap.** `gh api …/actions/caches` lists no
  `busybox-artifacts-*` entry; the job log says `Cache not found for input
  keys`. A cache populated only by a successful fetch is inert for the whole
  outage it was added to survive.
- **g2 — one of two consuming jobs got the cache step.** `.github/workflows/ci.yml`
  adds `actions/cache` to `tests` only; `unit-repeat` (`ci.yml:90-105`) runs
  the same `tests/unit` tree with no cache step and failed at HEAD.
- **g3 — `preflight()` raises from `pytest_collection_finish`.** That hook is
  reached by runs that execute nothing (`--collect-only` children driven by
  `tests/unit/test_lane_invariants.py:552` now hit the network and exit 3),
  and inside an xdist worker the raise becomes `worker_internal_error`, so the
  inner conformance run writes no `census.json` and the priming instructions
  the commit exists to deliver are buried under a traceback. This re-violates
  the #196 principle: a run precondition must not fire when nothing runs.
- **g4 — the busybox-tier guard does not do what its docstring says.**
  `tests/busybox/conftest.py:130-131` is `if session.items: preflight()` —
  the *whole session's* item list, not the busybox tree's. A run with every
  busybox test deselected and 7,000 unit tests remaining still probes the
  network. The conformance copy (`tests/conformance/conftest.py:340-343`)
  does it right (`cell.kind == BUSYBOX_ARTIFACT`). Both trees are in
  `testpaths` and `make coverage`'s legs are path-less, so on a cold cache
  `make coverage` would INTERNALERROR at collection — unobserved only because
  the dev VM's cache is warm.

**Fix direction (a decision for Chris):** the real problem is a third-party
mirror in the default gate. Options: (A) host the five pinned artifacts as a
GitHub Release asset on this repo (one-time trust, taken at a reviewed
moment, which is what the pin already argues for) and fetch from there with
busybox.net as the fallback; (B) commit them (~5 small static binaries) under
`tests/_fixtures/`. Either way, the hook fixes are unconditional: guard
`--collect-only`, scope g4's guard to busybox items, add the cache step to
`unit-repeat`, and make an unreachable source surface as **one named failing
test**, not an INTERNALERROR (never a skip — see the host-down rule).

*Status at time of writing:* busybox.net recovered ~20:20 local on 08-25 and a
parallel session already holds the #261-#263 investigation (it reproduced the
`--collect-only` download locally and proposes gating on
`not session.config.option.collectonly`); g2 and g4 above are the two facts
that investigation did not yet have. Re-running `32897991866` now would both
go green and seed the cache — which is the bootstrap problem, not a fix.

**Release-side exposure:** `make release` reaches busybox.net transitively via
`make nox`, and `release-matrix` runs `conformance-bed` across the live bed
including the Zephyr guests; `conformance-bed` runs `support-matrix` even when
the lane is red (`Makefile:869-870`), so a transient wedge folds a
`measured-broken` verdict into the working copy, `check_matrix_downgrades.py`
refuses, and make aborts leaving `schemas/support_matrix.json` dirty *(sweep)*.

### 1.2 ✅ FIXED — dashboard SSE `chart_map` eviction (product bug)

Fixed on the busybox-gate-fixes branch exactly along the fork below: on
overflow the subscriber's queue is emptied and handed a `LAPSED` sentinel,
`/api/stream` ends that response, and the client's `onerror → resync →
reopen` path recovers (`web/src/data/` unchanged — verified it replaces
`sessions`, and so `chartMap`, wholesale on resync). `test_broadcast.py`'s
drop-oldest pin is replaced by lapse pins and a raw-ASGI stream test;
`docs/architecture/subsystems/monitoring.md` and `todo/TODO.md` no longer
describe drop-oldest. The original analysis follows for the record.

`src/otto/monitor/broadcast.py:41-44` still drops the oldest frame on overflow
(`SUBSCRIBER_QUEUE_MAX = 1024`, `:19`); the last commit touching the file is
the one that introduced drop-oldest. `chart_map` is emitted once, on the first
appearance of each bare label (`collector.py:557,583-589` → `:907`), and
rides the same lossy queue. `web/src/data/stream.ts:96-105` resyncs only from
`source.onerror`, which an eviction never triggers; `web/src/data/` has zero
commits in the window. Symptom: 13 ungrouped `chart-m*` charts instead of one
`chart-CPU`, silent, unrecoverable until reload. Real-user shape: a subscriber
that drains nothing for ~57 s at live-bed rate (backgrounded tab, half-dead
connection) while a new label appears (new interface, returning host).

Two facts worth recording: (i) **no `todo/` entry exists for this** — the
2026-08-08 root cause never made it into a ledger; (ii) drop-oldest is pinned
*as intended* by `tests/unit/monitor/test_broadcast.py:44-49`, so the fix must
change that test. Fix fork (unchanged from 08-08): on overflow mark the
subscriber lapsed and close/sentinel it so the existing `onerror → resync →
reopen` path recovers. Raising the bound fixes nothing.

### 1.3 ✅ FIXED — `tests/integration/conftest.py` reaped the lab's docker for every test under it

Was: a session-scoped autouse fixture SSHed to `10.10.200.13` and `docker rm
-f`'d every `-e2e-` container for **any** test under `tests/integration/`,
though 27 of 31 files there never mention docker — a flakiness *generator*
for everyone else on the bed. Fixed: the reaper is requested by name
(`usefixtures`) from the four docker modules beside their
`xdist_group("docker_e2e")`, asserts its own premise at runtime over
`session.items`, and `tests/unit/test_docker_reaper_scope.py` pins both
directions in the default lane. The 2026-08-16 ledger is deleted (complete).

### 1.4 Known-item status (was open as of mid-August)

| item | status | user-visible | severity |
| --- | --- | --- | --- |
| dashboard SSE `chart_map` eviction | **OPEN** | yes | MEDIUM |
| `SshHopTransport` forward-cache race (`close()` without a lock) | FIXED `edda27af` (generation counter) | — | low |
| nc per-connection channel budget | FIXED `d33b3daf`; 4 of 5 residuals open by design | non-default sshd only | low-med |
| `monitor_db_count10_flakes.md` | init race FIXED `15299154`; the two `--count=10` flakes OPEN (test-only) | no | low |
| `unraisable-subprocess-transport-flake.md` | FIXED `dab13a7b` | — | low |
| `flaky-run-timeout-donation-2026-08-08.md` | FIXED `f2c05328` | no | none |
| Zephyr console wedge + journald window | mitigation LANDED `4b48e891`; **the 2.7 net-buffer leak itself OPEN by choice**, while `schemas/support_matrix.json` says `zephyr-2.7` `measured-ok` on five surfaces; **#260 open** — a held console slot is reported as "unresponsive or bad credentials" on a device that has no login | **yes** | **MEDIUM-HIGH** |
| `_dashboard_harness.run()` `.result(timeout=10)` | still present (`tests/_fixtures/_dashboard_harness.py:211`); runaway guard for 38/39 call sites, a *discriminator* for `test_replay_soak.py:143`; soak-only, outside every default gate | no | low |
| BusyBox cache + preflight | **OPEN — actively red** (§1.1) | CI/release | **CRITICAL** |

In-window flakes that were caught and closed: `dashboard-e2e (firefox)`
`test_a_silent_hosts_drillin_shows_a_growing_unreachable_banner` ("Unreachable
for 36s") — FIXED 35 minutes later at `4a5edf34`, the assert now derives its
text from the paused clock; `busybox-artifacts (arm64)` `.part` temp-file race
under xdist (#246). Earlier reds #247, 08-12 ×3, 08-09 ×2, 08-07, 08-06 — all
closed with root causes.

### 1.5 SHOULD FIX — wall-clock discriminators that ride the parallel leg

The wall-clock policy has a ratified hole: `docs/architecture/quality-gates.md:183`
records "**Not attempted:** finding a wall-clock bound written *into* an
assertion", and `tests/unit/test_declared_harness_bounds.py:27-38` records the
scanner that would have found them was cut. These are the hole's current
occupants (all verified):

1. `tests/e2e/test_link_impair_e2e.py:135` `_RTT_RESTORE_TOLERANCE_MS = 20.0`
   — a two-sided 20 ms tolerance on a **nested-VM ICMP RTT**; unmarked, so it
   rides `make coverage`'s `-n auto` leg. Companion `:134/:140` 150 ms floors.
2. `tests/unit/host/test_unix_host.py:790` `asyncio.wait_for(…, timeout=1.0)`
   wrapped in `except TimeoutError: pytest.fail(...)` — a discriminator
   wearing a guard's clothes; `concurrency`-marked only → parallel leg under
   coverage tracing.
3. `tests/unit/test_host_pool_lease.py:51` `assert waited >= 0.3` in
   `test_lease_is_cross_process` (`:41`) with 0.2 s of slack across an
   `mp.Queue` round trip — **unmarked**, while its sibling `:24-25` carries
   `serial_timing`.
4. `tests/integration/host/test_unix_host_integration.py:43-44` — the only
   `nc` transfer rows without `xdist_group("nc-serial")` *(sweep)*; the
   `tomato`/`nc-serial` guard from the prior incident is otherwise intact.
5. `noxfile.py:141-168` `tests_integration` has no frontend prerequisite —
   `todo/nox-integration-web-dist-prereq-2026-08-22.md`, 5 tests fail by
   worker placement, open.
6. `tests/integration/chaos/_sshd.py:61-65` `free_port()` bind-then-close,
   justified by "sequential suite"; its conformance callers
   (`tests/conformance/_cells.py:202,247`) run under a lane that is
   explicitly not `-n0` *(sweep)*.
7. `tests/unit/testing/test_conformance.py:168` `_NOW = datetime.now(tz=…)`
   at **module import**, with `_window()` spanning `_NOW ± 1h` while the
   validator compares against a fresh `now()`. It reddened in run 262
   (`TestReservationWindowsConformance::test_conforming_backend_passes`)
   because `unit-repeat` ran 64 minutes (inflated by the BusyBox stalls), so
   the body executed 1h02m after import. Latent: reddens whenever
   `unit-repeat` exceeds ~60 min, BusyBox or not. Found by the parallel #261
   session; the sweep here missed it — it is a wall-clock bound written into
   *data*, not into an assertion, which is a third shape the policy hole
   in `quality-gates.md:183` does not name.

### 1.6 Accepted risk — write it down

- `filterwarnings = ["error"]` bare (`pyproject.toml:277-279`) is a standing
  amplifier: any new unclosed fd/transport, including third-party, reds the
  gate attributed to whichever test GC ran inside (`24ca84ed`, `d0ce8fe6` were
  it firing).
- `@pytest.mark.retry` — the ast-grep fence is clean (zero suppressed
  violations), but `tests/conftest.py:201-209` registers the retry hookwrapper
  into otto's own suite, so the ban is lint-only around a live mechanism.
- pytest-randomly is on everywhere; **nightly runs one seed per night**
  (`--count` duplicates items inside one shuffled session); 12 files with
  nested pytest never clear `PYTEST_ADDOPTS` *(sweep, from the adoption doc)*.
- No timezone pinning in `tests/`; safe by the `tz=timezone.utc` convention,
  enforced by nothing.
- `unit-repeat` has no `timeout-minutes` (runs ~22 min; a hang burns the
  6-hour default).
- Closed as a non-issue: `tests/e2e/suite/test_library_run_e2e.py:35` reads
  `os.environ` at what looks like module scope — it is inside the `_SCRIPT`
  string (`:14`), a child script; not a #192 shape.

---

## 2. Public API — inventory, churn, and the stability plan

### 2.1 Surfaces and verdicts

| surface | defining files | gate today | verdict |
| --- | --- | --- | --- |
| (a) library API | `otto/__init__.py` (26 names, `__all__` + `_LAZY_EXPORTS`), `bootstrap.py`, `context.py`, `result.py`, `errors.py`, `host/__init__.py` (65 re-exports, **no `__all__`**) | import-budget snapshot pins *laziness*, not membership; set-equality gates for `otto.docker`/`otto.suite` only; `OttoError` invariant gated; **no `py.typed`** (absent from `src/` and the v0.8.7 wheel) | CHURNING |
| (b) CLI | `cli/builtin_commands.py` (12 groups), root callback (21 global options), `cli/invoke.py:1312` render seam | no `--help` snapshot; the two group-enumerating tests (`tests/unit/cli/test_root_group.py:19`, `tests/e2e/cli/test_schema_run_help_e2e.py:18`) hardcode **8 of 12** — neither updated when `otto env` landed; CLI docs hand-written, no generator | CHURNING |
| (c) user-repo contract | `settings.toml` (`models/settings.py:621`, 16 top-level keys), `lab.json`, `[init]` modules, `@instruction` (`cli/run.py:127`), `OttoSuite`, `@cli_exposed`, `os_profiles`, 11 `register_*` | **strongest area**: scaffold-mentions-every-field test, fixture-repo validation (3 of 6 repos), `init → list-suites → run` e2e in the hostless lane; **no gate on the import paths themselves** | CHURNING — highest blast radius |
| (d) spec models / schemas | `models/base.py:19` `extra="forbid"`, `models/jsonschema.py` → `otto schema export` | freshness checked only downstream by the `otto init` doctor in the *user's* repo; no CI signal when a model change alters the emitted schema. (The July "stale `schemas/`" finding was a false positive: 9 of 11 files there are gitignored build output; `hosts.schema.json` is a dead artifact from a stem retired in `a1f54bb3`.) | STABLE model, UNGATED emission |
| (e) monitor HTTP + SSE | `monitor/server.py` (9 routes), `models/monitor.py:507` `format: Literal[1]` required | best-gated surface: codegen'd both sides, `git diff --exit-code` in CI. The July `/api/data` hand-mirror no longer exists. One residual: `/api/mode` (`server.py:316` raw dict ↔ `web/src/data/bootstrap.ts:35`) | STABLE |
| (f) coverage formats + `otto cov` | four independently versioned formats (store v7, cov_data v2, ticket export v2, capture v2), user-authored `coverage-overrides.toml` | best version discipline, fastest churn: store v4→v7 in a month; `tickets.json` ("otto's first public coverage export") has no JSON Schema | EXPERIMENTAL |
| (g) entry points | `pyproject.toml:73-74` | one console script; no `[project.entry-points]`, no extras | FROZEN in practice |

**The under-appreciated fact about (a):** `otto.__all__` has 26 names, but the
docs instruct users to import from **33 distinct module paths** *(sweep)*.
`@instruction` — in every fixture repo and the `otto init` scaffold — is
reachable only as `from otto.cli.run import instruction`. Proof the surface
already drifts unnoticed: `src/otto/instructions.py:51` sets
`register_hint="@otto.instruction()"`, and `otto.instruction` does not exist
(zero hits in `otto/__init__.py`). A user following that error's advice gets
an `AttributeError`.

### 2.2 Breaking-change census, v0.8.0 → HEAD

| range | date | breaking / commits |
| --- | --- | --- |
| v0.7.4..v0.8.0 | 07-26 | 3 / 29 |
| v0.8.0..v0.8.1 | 07-30 | 2 / 11 |
| v0.8.1..v0.8.2 | 08-02 | 2 / 38 |
| v0.8.2..v0.8.3 | 08-04 | 5 / 31 |
| **v0.8.3..v0.8.4** | 08-16 | **10 / 129** |
| v0.8.4..v0.8.5 | 08-19 | 1 / 30 |
| v0.8.5..v0.8.6 | 08-22 | 2 / 29 |
| v0.8.6..v0.8.7 | 08-25 | 1 / 27 |

Every v0.8.x release carries ≥1 break and every one shipped as a **patch**
bump. The CHANGELOG badge (`56e89e01`, `cliff.toml:55`) is deliberately bare —
it carries the subject, not the `BREAKING CHANGE:` footer (`cliff.toml:35-42`,
after the F9 truncation finding) — so v0.8.5's notes say `**BREAKING**
**project**: per-project lab/host scoping` and nothing about `re.fullmatch`
selectors or `valid_labs` being retired. The commit bodies are excellent;
none of it reaches the user.

Two **undeclared** user-visible changes in the window *(sweep)*: `6c9e1e9e`
relocated the completion caches to `~/.otto` and inverted the "no cache when
`OTTO_XDIR` is unset" behaviour; `e3452c80` reorganized the docs site with no
redirect config in `.readthedocs.yaml`/`docs/conf.py` (RTD dashboard
redirects unverifiable from the repo).

**Migration-mechanizability of the 21 user-facing breaks** *(sweep, per-commit
table in the sweep report)*:

| class | count |
| --- | --- |
| (i) cleanly auto-fixable Python rewrite (`d333087f` `async_typer_command` deleted, `c579d502` `@instruction` requires `async def`, `ff99e3fe` `discover()` → `DiscoveryResult`) | **3 (14%)** |
| (i) partial — ast-grep finds it, the fix needs review (`4b9fa403` timeout, `9f1731c3`, `ee688b05` Product→Result, `c0bcf90d`) | 4 (19%) |
| (i) detect-only (`03407261` `Status.NotRun`) | 1 (5%) |
| **(ii) config transform — ast-grep cannot** (`ad0edab3` `${sut_dir}`, `afc417c4` fullmatch + `valid_labs`, `8d6fe4ee` `[[lab.sources]]`, `7d3bd9f5` `markers`→`rules`) | **4 (19%)** |
| (iii) behavioural, prose only (the six refusals, dry-run, exit codes, store bumps) | 9 (43%) |

The four config breaks are the ones that hard-fail a user's repo at parse
time under `extra="forbid"` — the highest blast radius, and exactly what
ast-grep cannot touch. otto's only TOML dependency is `tomli` (read-only); a
comment-preserving rewrite of a user's `settings.toml` needs `tomlkit` as a
new runtime dependency, and the scaffold's comments are the self-documenting
contract (`init_templates.py:1-8`), so a naive round-trip would delete the
user's documentation.

**In-flight rename** (`docs/superpowers/specs/2026-08-22-test-strategy-and-unix-lab-rename-design.md`):
`veggies`/`fruits` → `unix`/`unix_alt`, `carrot`… → `test1`…. These are bed
fixture values, not API names, and `slug()` is explicitly untouched — but the
fixture repos are the worked example users copy. Land it before or with the
docs freeze, never after.

### 2.3 The plan (efforts: S < 1 day, M a few days, L a week+)

**P0 — replace `AGENTS.md:10`** with the tier policy below and a pointer to
the deprecation mechanism. `ad0edab3`'s body reasons from that line to leave
a `${sut_dir}` leftover silently ignored. Nothing else holds while it stands,
because every contributor and agent will correctly obey it. **S, first.**

**P1 — three tiers** (`docs/stability.md`, linked from `docs/index.rst` and
README — neither contains "stability", "semver" or "breaking change" today):

- **Tier A — FROZEN** (major version + written migration + a diagnostic):
  `settings.toml` top-level keys and `[host_preferences]`/`[project]`/
  `[[lab.sources]]` shapes; `lab.json` structure and required `HostSpec`
  fields; the registration decorators **and their import paths**
  (`@instruction`, `@cli_exposed`, `@cli_command`, `register_cli_command`,
  `OttoSuite`, `register_project_actions`, the 11 `register_*`);
  `[project.scripts] otto`.
- **Tier B — STABLE with one release of deprecation**: `otto.__all__` plus a
  declared list of sanctioned deep import paths; the `Result` family and
  `exit_code` semantics; the `OttoError` invariant; the CLI tree, global
  option names and exit codes; the documented `docs/library/` ABCs
  (`Product`, `ProjectActions`, backends).
- **Tier C — EXPERIMENTAL, documented as such**: monitor routes/SSE; all
  coverage formats — with **`tickets.json` promoted to B** (it already moved
  only 1→2 while the store moved 4→7; write that asymmetry down and give it
  a schema).

**P2 — deprecation mechanism in otto's idioms** (there is none today — no
`DeprecationWarning` is raised anywhere in `src/otto`; every break has been a
hard cutover):
- (b) **`RemovedKeyError(OttoError)` at settings parse** — generalize the
  pattern that already exists at `models/settings.py:256-271` (the two
  removed-shape errors `8d6fe4ee` added) into a `_REMOVED_KEYS` table and
  backfill `valid_labs`, `labs`, `lab_data_type`, `[coverage.exclusions]
  markers`, plus a value-level `${sut_dir}` scan. Converts `extra="forbid"`'s
  opaque "extra inputs are not permitted" into a sentence naming the
  successor. **Highest value per line in this plan. S.**
- (d) **changelog `## Migration` section** rendering the `BREAKING CHANGE:`
  footer verbatim (7 of the 23 breaking commits already carry excellent
  footers). **S.**
- (a) `@deprecated` in `otto/utils.py` beside `cli_exposed`, same
  `TypeVar`-bound overload shape; (c) a deprecation banner through
  `render_leaf_value`, rendered like `_render_dry_run_decline`
  (`invoke.py:1284`), never changing the exit code. **With the first Tier-B
  deprecation. M.**

**P3 — ast-grep migration scripts: do NOT build the general pipeline.**
Measured: `ast-grep scan -r <rule.yml> .` runs against a user's tree with no
project config and produced a clean import-rewrite diff across three fixture
repos; a structural `decorated_definition` + non-`async` detector for the
`c579d502` shape fires (proven red). **New gotcha measured this session: a
`fix:` that captures `$$$` across lines flattens a multi-line signature to
column 0** — valid but mangled; any shipped rule must say "run your formatter
afterwards", and otto cannot assume a user has ruff. With 14% coverage, four
already-known ast-grep blind spots, and no TOML writer, a `migrations/` tree
is L effort for the wrong 14%. **Instead:** ship rules *opportunistically*
under `docs/migrations/<version>/*.yml` for the clean import/unpack/decorator
class, with the exact `-r … ` preview/apply invocation in the release notes
and a proven-red fixture per rule (**S per rule**). **Do not add an `otto
migrate` verb** — it would make `ast-grep-cli` a runtime dependency, and
`pyproject.toml:143-176` records why the lint group stays out of dev (#193).
Put the real investment in P2(b).

**P4 — gates that slow future churn**

| # | gate | exists? | effort |
| --- | --- | --- | --- |
| 4.1 | **public-API snapshot** — walk `otto.__all__` + the sanctioned deep paths, assert each resolves, diff a committed golden; clone `scripts/import_budget.py`'s `--update/--check` + `make api-snapshot` shape; assert `set(__all__) == set(_LAZY_EXPORTS)` | no | S |
| 4.2 | removed-key diagnostics (P2b) | pattern exists at `models/settings.py:256-271` | S |
| 4.3 | **CLI inventory snapshot** derived from `CLI_COMMANDS.names()` (not a literal); delete the two stale 8-of-12 lists | no | S |
| 4.4 | commit `lab.schema.json` + `settings.schema.json`, `git diff --exit-code` on regeneration — the monitor codegen gate's exact shape (`Makefile:447-448`); reverses `.gitignore:64-66` for two files | no | M |
| 4.5 | **`py.typed`** — one empty file + build-backend include; PEP 561 currently makes every downstream type-checker treat otto as untyped | no | S |
| 4.6 | fixture-repo settings gate over all 6 repos — reuse `tests/unit/test_sample_repo_hygiene.py:44`'s glob instead of `test_settings.py:444`'s 3-tuple | partial | S |
| 4.7 | `/api/mode` pydantic model so it rides the existing `export.gen.ts` gate; delete the hand-written `ModePayload` | no | S |
| 4.8 | `tickets.json` JSON Schema | no | S |
| 4.9 | docs-coverage gate (69 of 230 modules have no `automodule` page, incl. `otto`, `otto.host`, `otto.suite`) | no | M, optional |
| 4.10 | **bump-policy refusal** — a `make release` stage that runs this report's `git log --grep` census over `$(LAST_TAG)..HEAD` and refuses `BUMP=patch` when it finds a `!`; precedent: `release-matrix`'s downgrade refusal | no | S |

Suggested order: P0 → 4.5 → 4.1 → 4.2 → 4.10 → 4.3 → 4.6 → 4.7 → P1 → 4.4 →
P2(a,c) → 4.8 → 4.9. Everything through 4.7 is S — one sprint.

---

## 3. Churn and design debt

### 3.1 Profile

| | 2026-06 | 2026-07 | window (08-06→08-25, human-only n=150) |
| --- | --- | --- | --- |
| commits | 181 | 471 | 199 (150 + 49 dependabot) |
| rate/day | 6.0 | 15.2 | 10.5 |
| feat | 19.9% | 29.7% | 26.7% |
| fix | 12.2% | 21.7% | 24.7% |
| **refactor** | 7.7% | **1.9%** | **2.7%** |
| test | 4.4% | 10.8% | 14.0% |
| docs | 17.7% | 10.4% | 25.3% |

0 merges, 0 reverts, 12 `!` commits in 19 days. The repo-wide lint/format
sweeps that inflated the prior review's per-file counts have **vanished** —
all 19 commits touching >25 files are deliberate feature cutovers, so per-file
counts are now real design signal. **44 `todo/`+spec ledgers opened in the
window, 1 closed.**

**Growth measured in code lines** (tokenizer split; otto's hubs are 60-74%
prose, and raw `wc -l` overstated growth ~2× — `config/scope.py` is 885 raw /
262 code and is *not* a god file):

| file | code then → now |
| --- | --- |
| `host/userland.py` | new → **1,515** (largest code body in `src/`; 22 commits, 17 focused, born 08-12) |
| `host/host.py` | 768 → 1,186 (**+54%**) |
| `cli/invoke.py` | 532 → 866 (**+63%**) |
| `host/session.py` | 1,198 → 1,410 (+18%; raw said +33%) |
| `link/manage.py` | 611 → 935 |
| `tunnel/manage.py` | 492 → 727 |
| `host/transfer/nc.py` | 792 → 985 |
| `project/orchestrator.py` | new → 645 |
| **`cli/cov.py`** | **576 → 583 (+7 — frozen; the extraction was never finished, third review running)** |

`src/otto` overall: 35,182 → 44,795 code lines (+27%); files ≥600 code lines
8 → 15. `tests/`: 5,059 → 7,507 test functions (+48%). `scripts/`: ~3,000 →
7,122 lines.

**Fix-chasing-fix:** `nc.py` has 6 fix commits, every one root-caused with
measured evidence; five of six are the same class — *otto created a resource
on or through the remote and did not reliably bound or reap it* (listener,
port forward, exec channel, staged temp file). The seam is emerging
(`e557482a` routes the fifth through `otto.lifecycle.compensate`); the class
recurs at every new remote-side-effect site until `compensate` is the
documented default. Healthy arc, named owner forming.

### 3.2 Prior-menu scorecard (2026-08-03 review §4, open at 08-06)

| item | verdict | evidence |
| --- | --- | --- |
| 1.2 split `session.py` | UNTOUCHED, grew | `DeclinedSession` (`session.py:1617`, overrides 7 methods) + BusyBox line budgets added; zero extraction |
| 1.3 delete `Repo.settings` raw dict | **PARTIAL — best movement on the menu** | readers 5 → 3; all new config (`[lab]`,`[project]`,`[env]`) reads the validated model; survivors `coverage/config.py:31,39`, `repo.py:852` |
| 1.7 option-kwarg collapse | UNTOUCHED, worse | `userland_options` is the **8th** option table at the same 7 signatures (`config/fleet.py:46,122,226,303,365`; `context.py:516,584`) |
| 1.8 finish the cov extraction | UNTOUCHED, mirror diverged | `suite/run.py:320` still "mirroring cov._resolve_cov_settings"; the two sides changed *differently* this window with a comment explaining the difference (`run.py:346-348`) |
| 1.9 `ACQUISITION_BACKENDS` | UNTOUCHED | — |
| 1.10 de-façade collector | UNTOUCHED | 908 lines both ends |
| 2.2 pyproject extras | UNTOUCHED | none; 4 more releases past the "gets harder after release" threshold |
| 2.3 retire `typer.Exit` dialects | REGRESSED 57 → 60 | 13 in `cli/cov.py`; partial credit: `main.py:874-920` `except OttoError` frame + `invoke.py` `fail()` |
| 2.4 split `utils.py` | REGRESSED 213 → 446 | +148 is `wait_for` + its gate (`22c16b3a`) — real repayment filed in the wrong drawer |
| 2.9 `rebuild_connections` protocol | UNTOUCHED, 4th `getattr` site | `cli/remote_completion.py:274` |
| §1b baselined tuple returns | PARTIAL 1 of 11 | `repair_all` → `RepairAllReport`; **zero new entries** under +9,613 code lines; `no-tuple-return.yml:35-37` prose now disagrees with its own baseline (says 12 debt; tree holds 10) |
| §1c plan-coordinate blind spots | UNTOUCHED | all three rules carry the identical unwidened regex |
| §2.2 tach panic, §2.4 docker accumulator, §2.5 repair one-sided | DONE in window | `config/repo.py:589`, `cli/docker.py:196-213`, `a14258e1` |
| §5.1 layering gate | **DONE, strong** | ast-grep rules 9 → 21, every one with a demonstrated-red baseline |
| §5.2 "a registry or a recorded exemption" | UNTOUCHED — **violated 4× in the window** | `GAPS`, `_DEV_TOOL_PROVIDERS`, exclusion-rule kinds, `env` backends |

### 3.3 New debt candidates (ranked; all verified by the sweep's code reading, headline ones re-checked)

- **N1 — `host.py` declares every field and method twice** (`Host(Protocol)`
  `:421-792` and `BaseHost(ABC)` `:795+`), and a new host field costs **7
  declaration sites** (verified for `dev_tools`: `host.py` ×2,
  `remote_host.py`, `unix_host.py`, `embedded_host.py`, `local_host.py`,
  `docker_host.py`). The 2026-07 "4× multiplicity" is measurably 7×. *Fix:*
  generate the Protocol from `BaseHost` or drop it as the typing surface.
  **S-M, fix-before-1.0; it halves the per-field cost and multiplies N2.**
- **N2 — `BaseHost` absorbed six responsibility clusters, zero extracted**
  (+418 code lines; dry-run `03407261` and project actions `c27c44fb` each
  added ~440 raw lines to the same class). `log_dest`/`get_logs`/
  `get_product_logs`/`get_debug_logs` and the `*_toolchain_tools` trio are
  artifact-collection and toolchain concerns; the package already has the
  mixin shape (`PosixFileOps`, `PosixPrivilege`, `UserlandHost` on
  `unix_host.py:253`). **M, fix-before-1.0.**
- **N3 — `userland.py` is two modules in one file** (its own docstring at
  `:151` concedes the split) and **bypassed the Registry engine three times**:
  `GAPS: list[Gap]` (`:2350`) with a linear `gap_for()` (`:3182`) — no
  `origin()`, no duplicate-surface detection, so a third-party repo cannot
  contribute a gap; capability probes are a hard-coded 7-tuple (`:895-903`)
  where one new capability = 7 edits; `dev_tool.py:92 _DEV_TOOL_PROVIDERS` is
  a bare list. Plus 155 lines of console report rendering in a library module.
  **M, fix-now (the split).**
- **N4 — the refuse-guard shape is written five times** (`file_ops.py:133-138`,
  `scp.py:175-182`, `nc.py:431-440`, `unix_host.py:228-233`, `sftp.py:141`),
  each with 60-130 lines of docstring, several arguing against each other.
  One `refuse_if_capability_settled(...)` carries the argument once. **S.**
- **N5 — `docs/guide/cli/` is a 78-file hand-mirror with no gate** (verified:
  no generator wired in `docs/conf.py`; the four test hits are incidental
  prose). Verb order is a third copy — `5c5156cd` reordered
  `builtin_commands.py` to match the docs and says "No test pins the order".
  `CLI_COMMANDS.names()` already returns registration order. **~30-line test,
  S, fix-now.**
- **N6 — `cli/invoke.py` is the new hub** (+63%, ~12 responsibilities, six
  arcs in one window). `build_lab_from_repos` (`:397`) has no Typer in its
  signature; **`cli/probe.py` (267 lines) is the window's clearest
  CLI-only-orchestration-that-belongs-in-the-library** — a fleet
  reachability fan-out a library user cannot reach without `otto.cli`.
  Counter-example done right: `cli/env.py` is 120 lines of pure surface over
  `otto.env`. **M.**
- **N7 — three registration authorities that drift**: the repo's own ledgers
  (`cli-dispatch-metadata-one-declaration-2026-08-15.md`,
  `test-harness-declares-registration-2026-08-16.md`,
  `registry_builtin_registration_symmetry.md`) describe one problem in three
  places; `tests/_fixtures/dispatch.py:869` builds its **own** `CommandSpec`
  instead of reading the shipped one — three green-on-drift sightings. Respect
  the ledger's warning that the two-level stamp system is load-bearing. **M,
  as one item.**
- **N8 — coverage exclusions**: the package split is *the* clean-extraction
  existence proof (zero-re-export `__init__`, old module deleted, deep imports,
  an anti-façade test at `tests/unit/cov/test_pipeline.py:159`) — cite it as
  the model. But rule-kind dispatch is a 4-branch string chain
  (`rules.py:134-176`) + two `isinstance` chains, coverage still has **zero**
  registries, and a sixth validation-only spec family
  (`models/settings.py:393-465`) duplicates every check in `rules.py`. **S
  (registry) / M (the duplication).**
- **N9 — 12 tree-wide guards are invisible to every scoped run**
  (`todo/treewide-guards-invisible-to-scoped-runs-2026-08-16.md`): a
  non-hermetic `subprocess.run(["git", …])` rode seven green scoped cycles
  and surfaced at `make coverage`. All twelve run in 24 s single-process;
  proposal fully specified (`treewide` marker + `make gate-treewide`). **S,
  do first — it speeds every other item's loop.**
- **N10 — `scripts/` (7,122 lines, doubled in the window) is the largest
  ungoverned Python surface**: not typechecked (`pyproject.toml:190`
  `include = ["src"]`), not tach-governed, not in the ast-grep scan roots
  (`sgconfig.yml:6`) — and it is release-critical (it writes and gates
  `schemas/support_matrix.json`). Also: the verdict vocabulary is declared in
  four places with no gate (`schemas/support-matrix.schema.json:124-129`,
  `collate_support_matrix.py:166-168`, `check_matrix_downgrades.py:46-47`,
  inline literals in `render_support_matrix.py:457,654,668`). **S.**
- **N11 — three probe engines, three unconsulting `stat -c %s` copies**:
  `nc.py:1050,1242,1331` hard-code `stat -c %s` while `userland.py:1040`
  probes `stat_size` and `shell.py:2007` consumes it; `nc.py:270-277`
  `_STRATEGY_PROBE` is a second `command -v` engine with its own cache;
  `daemon.py:107` and `tunnel/socat.py:99` are third and fourth. The gap
  registry reaches 5 of 8 measured-broken surfaces. **S-M.**
- **N12** second completion cache (`config/remote_completion_cache.py`) —
  split is justified; `_atomic_write_json` re-spelled inline (`:113-131`),
  reaches a private `completion_cache._cache_path` (`:74`). **Leave; S if touched.**
- **N13** `env/backends.py:53,92,107` `if backend == "uv"` chain — cheap now,
  the fourth registry-shaped decision made without a registry this window.
  **Leave / S.**
- **N14 small, verified:** byte-identical `_print_dry_run_plan` in
  `cli/link.py:108-127` and `cli/tunnel.py:171-190`; `DeclinedSession`
  doubles every new session verb; `HostSession` never folds the host's
  standing log mode (`hostsession-standing-log-mode-fold-2026-08-15.md`);
  the failure-message relay family — `unix_host.py:950` composes "staging
  failed" about a thing never attempted; `cli/cov.py:519,531,915` are the
  **only** exception roots outside `OttoError` (49 classes, AST-gated).

### 3.4 What is healthy (say it, the list above is long)

ast-grep 9 → 21 rules, all proven red; `no-tuple-return` / `no-bare-status-return`
ratchets held under +9,613 code lines (zero new entries, one retired);
`OttoError` 27 → 49 classes, every new one rooted correctly; BusyBox landed
through `OS_PROFILES` + `FRAME_CLASSES` + `TRANSFER_BACKENDS`, not a fifth
host class; the dry-run contract is ambient (`OttoContext.dry_run` read at the
device boundary, zero `dry_run` parameters repo-wide); the exclusions package
split; `nc.py:1164 _gather_per_file` replacing three byte-identical gathers;
`22c16b3a` deleting hand-rolled poll loops across 13 files *with* a gate; the
support-matrix pipeline (single writer, evidence schema, generated docs,
disjoint downgrade gate); six root-caused `nc.py` fixes, zero reverts.

---

## 4. The combined menu

**Tier 0 — this week, before any release**

1. Un-red CI: the four hook fixes in §1.1 unconditionally (a parallel
   session already owns the `--collect-only` half; g2 `unit-repeat` cache
   step and g4 the vacuous `session.items` guard are additional), plus
   Chris's call on hosting the artifacts (Release asset vs commit) so the
   default gate has no third-party mirror in it at all. The two commits that
   widened the blast radius came out of my own earlier session.
2. §1.2 dashboard SSE lapse signal (+ the test that pins drop-oldest, + a
   ledger entry so it stops being folklore).
3. §1.3 scope the integration docker reaper to the tests that drive docker.
4. P0 — replace `AGENTS.md:10`.

**Tier 1 — before first users (all S)**

5. `py.typed` (4.5), public-API snapshot (4.1), removed-key diagnostics
   (4.2), bump-policy refusal (4.10), CLI inventory snapshot + delete the two
   stale lists (4.3), changelog `## Migration` (P2d), `docs/stability.md` (P1).
6. N9 treewide marker; N5 CLI-docs gate; N10 `_VERDICTS` pin + put `scripts/`
   under ty/ast-grep.
7. §1.5 items 1-3 (mark or convert the three discriminators), 4 (`nc-serial`
   on the two rows), 5 (nox integration web-dist prereq), 6 (`free_port`).
8. #260's message (a held console slot is not "bad credentials"), and decide
   whether `zephyr-2.7` `measured-ok` is honest while the leak is open by choice.

**Tier 2 — structural (M), the third-review items**

9. N1 (generate or drop the `Host` Protocol) → N2 (`HostArtifacts`/`HostTooling` mixins).
10. Tier 1.8 finally — the cov extraction; the exclusions package is the model.
11. §5.2 as a gate (four registry-shaped decisions without a registry in one window).
12. N3 + N4 together; N7 as one item; N6 (`cli/probe.py` → library).

**Not recommended:** a general ast-grep migration pipeline / `otto migrate`
verb (§2.3 P3). Pyproject extras (2.2) stay not-recommended for the reason
recorded 2026-08-05 (air-gapped `pip download` resolves base deps only).

---

## 5. Corrections to prior records

- `project_issue_261` memory and the two commit messages claim #261 "fixed
  both ends" — **falsified**: the cache is unpopulated, `unit-repeat` has no
  cache step, and `preflight()` INTERNALERRORs at collection. §1.1.
- The 2026-07 review's "on-disk `schemas/` stale" — false positive (gitignored
  build output + one dead file).
- The 2026-07 review's "`/api/data` hand-mirrored ungated" — fixed 2026-07-11
  (`aa99ee13`); residual is `/api/mode` only.
- Session notes saying the Zephyr journald window is STAGED — it is on main
  (`4b48e891`). The hop forward race — fixed by a generation counter
  (`edda27af`), not a `_closed` flag. The nc bound — `d33b3daf`.
- "Two releases in the window" — four.
- Raw `wc -l` growth figures from any prior review overstate hub growth ~2×.

## 6. Unverified / out of reach from here

- Whether `busybox.net` is reachable right now, and whether a re-run of
  `32897991866` goes green.
- Whether `make coverage` on a cold cache actually INTERNALERRORs (g4 —
  mechanism verified from code, not observed).
- RTD dashboard-side redirects for the `e3452c80` docs reorganization.
- How many external users exist and on which v0.8.x — the tier assignments
  assume the worst case.
