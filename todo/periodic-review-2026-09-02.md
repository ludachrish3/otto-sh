# Periodic review 2026-09-02 — public API, test health, churn

**Window:** `f3ee7552` (2026-08-25, the previous review's HEAD) → `b1893342`
(2026-09-02, v0.10.0). 79 commits (66 human, 13 dependabot), **three releases**
inside it: v0.8.8 (08-26, **patch**), v0.9.0 (08-31, minor), v0.10.0 (09-02,
minor). 14 new specs under `docs/superpowers/specs/`.

**Method:** three parallel read-only sweeps (public API, churn/debt, test
health); every headline claim below re-verified by hand against the tree, `gh`,
and job logs. **No test, gate, or build was run** (shared live bed). Claims
resting only on a sweep's reading are marked *(sweep)*. Code-line figures use a
tokenizer that runs ≈0.82× the 08-25 review's counter — **levels are not
comparable to the prior review; deltas and percentages are** (the counter
reproduces the prior "15 files ≥600" exactly at its ≥500 threshold).

Prior reviews this continues: `todo/periodic-review-2026-08-25.md`,
`todo/churn-and-design-review-2026-08-03.md`, `todo/test-infra-review-2026-08-06.md`.

---

## 0. Verdicts

1. **Operational health inverted since 08-25.** Last review opened with "CI is
   red at HEAD"; this window closes with **20 consecutive green push runs**,
   6 consecutive green nightlies, zero re-runs, zero retry automation, and
   every red in the window closed with a named root cause — the auto-filed
   issues (#264–#267) were all closed within ~2 days. The must-fix series
   landed whole. The bug-turnaround machinery growth will demand is
   demonstrably in place.
2. **Public-API break density roughly doubled while the absorb-cost tooling
   stayed at zero.** 11 breaking commits in 8 days (~1.4/day vs ~0.7 in the
   prior window) — 6 marked `!`, **5 unmarked, and two of the unmarked shipped
   inside patch release v0.8.8** one day after the review that called out
   exactly that pattern. Of the 7 gates the 08-25 review specified, **half of
   one** landed. Bump discipline improved (breaks now ride minor bumps) — but
   by hand, enforced by nothing, which is the precise shape the 08-03 review
   showed decays.
3. **The host API is genuinely converging — and all remaining instability is
   concentrated in one surface.** Against the migration-contract list:
   `Status`/`ShellCommand`/`Expect`/`HostSession`/the `Result` family were
   untouched all window (`utils.py`, `result.py` zero diff); config accessors
   and `OttoContext` moved additively. The **`Host` protocol alone took three
   breaks** (two landing the day before HEAD) and still has named follow-ups
   open. Cost quantification for the "what if we'd had users" question: **30
   commits have changed the signature or observable behaviour of
   `exec`/`run`/`put`/`get` since 2026-06-01**, 6 of them breaking a Python
   caller outright. Also: `todo/migration_plan.md:27` still names `RunResult`
   and `CommandStatus`, both deleted 07-01 — the freeze target is stale.
4. **The churn that would cost users most is happening *outside* the planned
   contract.** All 5 unmarked breaks touch surfaces the contract never
   named — `settings.toml` keys, lab.json shape, the `OttoSuite` class, and
   two third-party extension protocols — and those surfaces have the weakest
   gates: `assert_lab_repository_conforms` certifies a backend that production
   now TypeErrors on (§1.4), `docs/library/lab-source-backends.md:53` documents
   the pre-break signature, and one cross-family latent bug shipped
   (§1.5, coverage fetch from container hosts). **The support matrix — the
   page that exists to answer "can otto do X against a device like mine" —
   carries none of this per-family splintering**: its axes are measured
   surfaces × bed userlands, so the container family, `local`, and the
   per-family `user=`/`show_progress`/session semantics appear nowhere on it
   (§1.6; flagged by Chris mid-review).
5. **The test suite is clean: no papering found.** Skip density flat (all 4 new
   skips are capability probes), both xfails `strict=True`, zero retry
   automation, `filterwarnings` still bare `error`, real serialization flat at
   ~0.3% of tests. Three window commits explicitly *refuse* the timeout-bump
   lid in their own messages. Debits: one **new unmarked wall-clock
   discriminator** (`tests/unit/inventory/test_netbox.py:232`), #283 honest
   but unowned, six §1.5 items from last review unchanged, and a small set of
   stale-prose residuals (§2.5).
6. **Internal churn has become a repayment problem, not a growth problem.**
   The hubs stopped growing (`userland.py` **−8%**, the first hub shrink;
   `cli/cov.py` frozen a **fourth** consecutive review), zero new
   TODO/FIXME/HACK markers, ratchets byte-identical, and the window's new code
   is born better than the old (the `inventory/` package, four seams built on
   real registries, the transfer progress contract enforced at registration).
   But refactor share fell again to **1.5%** — the lowest measured —
   **0 of 14** standing debt items closed, ledgers run 19-opened/2-closed
   (191 open), `scripts/` grew +22% still ungoverned, and `type: ignore`
   (+18%) is the one suppression class outgrowing the tree.

**The 08-03 thesis holds for a third review: every line held by automation
held; every line held only by prose decayed.** Fresh instances this window:
hand-minor-bumps worked but nothing enforces them; `todo/nc-listener…08-11.md`
still claims a fix that `dcd9c56f` disproved; `Makefile:900-910`'s
conformance-bed sizing prose is stale; `migration_plan.md` names dead types;
`tests/_fixtures/support_matrix.py:624` cites a guard file that has never
existed; `no-tuple-return.yml`'s note still says 12 debt entries over a tree
holding 10.

---

## 1. Public API

### 1.1 Breaking-change census (11 breaks; ✱ = unmarked)

| commit | release | surface | what a user changes | diagnostic for the old shape |
| --- | --- | --- | --- | --- |
| ✱ `caf32183` nc universal spelling | **v0.8.8 (patch)** | `userland_options.nc_dash_n` key deleted (settings + lab.json reachable) | delete the key (capability retired) | generic `extra="forbid"` noise |
| ✱ `86114dfc` transfer progress | **v0.8.8 (patch)** | third-party transfer backends: `progress_granularity` ClassVar now **required at registration** | declare `ProgressGranularity(put=…, get=…)` | **best-in-class** — `transfer/registry.py:48-53` names the field and shows a worked value |
| ✱ `0fba081a` lab.json v2 | v0.9.0 | top-level `hosts` array gone; `labs` table + `elements` required; **also silently deleted the `log = true/false` compat coercion** (`HostSpec._coerce_log_bool`) | rewrite lab.json per the migration doc | **best in tree** — loader raises `_MIGRATION_HINT` citing `docs/guide/configuration/lab-config.md` § "Migrating from the hosts array" (a real worked example); the `log` bool gets only generic pydantic |
| ✱ `ec5ffac1` inventory layer | v0.9.0 | third-party `LabRepository.load_lab()` / `SupportsHostSummaries.list_host_summaries()` now called **by keyword** with `inventory=` (`labs/__init__.py:64`) | add the parameter | none — `TypeError` at call time; see §1.4 for the two aggravators |
| ✱ `143f6eb1` three-level reservations | v0.9.0 | `Host` protocol +3 required attrs (`resources: frozenset`, `element_resources`, …); host-entry `resources` re-legalized **with new meaning**; out-of-fleet `otto host <id>`/`--hop` newly refused | factory-built hosts unaffected (attrs stamped); hand-built hosts must add fields | silent for the semantic shift; `docs/library/lab-source-backends.md:40-48` warns about the stamping |
| `95aaae42` `!` logging | v0.9.0 | `[logging] capture` removed; `set_capture_prefixes`/`capture_external_loggers`/`product_log_prefixes`/`Repo.logging_capture` deleted; new `otto.logger.install/reset` | move to the zero-registration funnel | config key: the window's **best removed-key error** (`models/settings.py:369-386`, prefixes the offending file); the four deleted Python names: bare `AttributeError` |
| `7885b74c` `!` suite | v0.9.0 | `OttoSuite` drops `Generic[TOptions]`, `setup_method`/`teardown_*`, `self.testDir`/`suiteDir`/`logger`/`expect`; ensure-fixtures → `@pytest.mark.ensure(...)` — **every user suite rewrites** | rewrite per `writing-suites.md` | **none** — no `__class_getitem__` guard, no collection-time refusal naming a removed spelling; the docs sweep also removed every old name, so a user can't even search for them |
| `978369c2` `!` cache CLI | v0.10.0 | root `--clear-autocomplete-cache` deleted → `otto cache clear` | change the invocation | Typer "No such option" — and the **only removal in the window pinned by stay-removed tests** (unit + e2e) |
| `6b3e35dd` `!` container users | v0.10.0 | `login(as_user=)` → `login(user=)` (hard rename, body says "no deprecation shim, riding the 0.10.0 window"); `user=` added to run/exec/put/get ×5 families | rename the kwarg | `TypeError` |
| `d7426e75` `!` unix user= parity | v0.10.0 | on ssh unix, `exec/put/get(user=)` flips **refusal → authenticate-as** (second SSH connection) | semantic re-read of existing calls | n/a (a refusal becoming behaviour) |
| `c4b5df95` `!` docker use-cases | v0.10.0 | `[[docker.composes]].default_host` removed; `otto docker up/down/build` argument shapes change; `pyyaml` becomes a runtime dep | move to use-cases + `roles` | generic `extra="forbid"` (no targeted validator added) |

Not counted as breaks: `d2bdadf9` (pure relaxation), `5ca01dbf`
(`hw_version`/`sw_version` widening; only side effect is a looser generated
JSON-schema `anyOf`), `2e28f90b` (additive `[[products]]`/`[[dev_tools]]`),
`3ae343b1` (entry point `otto:app` → `otto._shim:main`; console-script name
unchanged). *(Prior sweep text said "13 breaks / 7 unmarked"; the two
relaxations are excluded here — the defensible count is 11 / 5.)*

**Reading the census fairly:** the breaks cluster into three *planned* arcs
(lab-v2 + inventory + reservations; the two `user=` commits; the 0.10 batch),
every one spec'd first — this is Phase 0 of `todo/migration_plan.md` working
as designed. The cost problem is not that otto breaks; it is that (a) marking
is inconsistent exactly on the highest-blast-radius surfaces, (b) v0.8.8
repeated the breaks-in-a-patch pattern the day after the review named it, and
(c) a user has **no migration surface**: the CHANGELOG renders a bare
**BREAKING** badge (deliberate, `cliff.toml:30-43`), carries zero old→new
prose, and does not even link the one excellent migration doc the window
produced.

### 1.2 Gate scorecard (the 08-25 review's §2.3/P4 plan)

| item | status |
| --- | --- |
| P0 replace `AGENTS.md:10` | **open by decision** — `migration_plan.md` Phase 1 defers it to the contract freeze; unchanged |
| 4.1 public-API snapshot | absent (zero tests reference `otto.__all__`/`_LAZY_EXPORTS`) |
| 4.2 removed-key diagnostics table | absent as a mechanism; the window hand-wrote 3 more one-off `mode="before"` refusals (5 total now) — the *pattern* spreads, the table doesn't exist, `valid_labs`/`${sut_dir}`/`markers`→`rules` still un-backfilled |
| 4.3 CLI inventory snapshot | **half** — `test_root_group.py:57-73` now derives from `CLI_COMMANDS.names()`, but it gates the *docs tables*, and both hardcoded literals survive and drifted further: `test_root_group.py:15-31` is a 9-of-14 subset check, `test_schema_run_help_e2e.py:18` an 8-of-14 copy; `inventory` was added to one by hand, **`cache` was added to neither** |
| 4.4 committed settings/lab schemas + diff gate | absent (`schemas/` in git = support-matrix only) |
| 4.5 `py.typed` | **absent — including from the shipped v0.10.0 wheel** (verified against `dist/`) |
| 4.10 bump-policy refusal | absent; `Makefile:16` `BUMP ?= patch`; zero grep hits for "breaking" in Makefile/noxfile/workflows. v0.9.0/v0.10.0 were minor **by hand** |
| P2d changelog Migration section | absent (deliberate; see above) |
| P1 `docs/stability.md` | absent; only compat policy in the docs tree is `docs/guide/cli/cov/tickets.md:285`, scoped to tickets.json |
| P2 deprecation machinery | absent — zero `DeprecationWarning`/`warnings.warn`/`@deprecated` in all of `src/otto`. The window's only compat alias is **private** (`context.py:404` `_admissible_ids`, "kept for one release") |

### 1.3 Host API deep dive (the "is it stable now?" question)

**Signature archaeology** *(sweep; method: per-commit extraction of the full
signature blocks — `git log -S/-G` on the `def` line under-reports badly)*:
since 2026-06-01, 21 commits changed a core-verb signature (17 substantive),
13 more changed semantics only; **6 broke Python callers**: `17632b4f`
(`log=False` dropped), `51a80159` (tuple→`Result`; `CommandStatus`→
`CommandResult`, `RunResult`→`Results`), `13e83418` (`oneshot`→`exec`,
`interact`→`login` — the README still taught `oneshot()` until `ff23b873`,
**50 days and 14 releases later**), `49dcb9c7` (`mode=` inserted before
`show_progress`), `4b9fa403` (default 30 s timeout; `timeout=None` →
`TypeError`), `6b3e35dd` (`user=` ×5; `login(as_user=)` rename).

**Where `user=` landed** (verified): uniform in *spelling* across
run/exec/put/get/login, but **three-valued in meaning** — authenticate-as
(unix ssh), `docker exec -u`/chown (containers), and refusal (embedded,
local, telnet, ftp-backend). Container `get(user=)` accepts-and-ignores by
documented argument (reads are ownership-indifferent — coherent, but a third
semantics). Deliberate and spec-recorded: unix `run(user=)` refuses because a
persistent session's identity is `as_user()`'s job
(`2026-09-01-unix-user-parity-design.md:26-37`).

**Not yet uniform, all with open ledgers/specs:** `open_session()` and
`HostSession.run/send` take no `user=` on any family (container session
identity rides a `_pending_run_user` side channel); `ShellCommand` has no
`user` field, so per-command users inside a `run([...])` list are
inexpressible; four parallel privilege mechanisms survive (`sudo:` flag,
`as_user()`/`switch_user()`, `elevation:`, connection-level `login_target`);
and CLI `--as-user` (`cli/main.py:618`) is *reservation identity* — an
unrelated name collision with the new `user=`.

**Convergence table** (contract items from `migration_plan.md` — corrected
names):

| item | window churn | foreseeable breaks on record |
| --- | --- | --- |
| `Host` protocol | **breaking ×3** (`143f6eb1`, `6b3e35dd`, `d7426e75`) | parity spec §8 (telnet per-user, ftp user, `login` fresh-connection refusal lift); `open_session` user binding; get-side ownership; `compress`, `stat()`, `.tainted` (`todo/TODO.md`); N1 refactor "fix-before-1.0" |
| `Results`/`CommandResult`/`Status` | untouched (`result.py`, `utils.py` zero diff) | none named — but `Status` has **no sanctioned re-export** (`otto.utils.Status` is the only path, taught by 5 docs pages) |
| `ShellCommand`, `Expect` | untouched | `user` field decision; `Expect` defined twice (`host.py:61`, `session.py:54`) |
| `HostSession` | additive only | standing-`LogMode` fold (ledgered); 2,633-line `session.py` split (3 ledgers) |
| logging | breaking **toward** the contract (`95aaae42` deleted the capture allowlist; stdlib funnel is the end state) | — |
| config accessors, `OttoContext` | additive (`Lab.metadata` appended last deliberately; `admissible_ids` promoted with a one-release private alias) | — |

**Verdict:** freeze distance is governed by **one surface**. If the parity
spec's §8 leftovers and the session-user question are either landed or
explicitly deferred past the freeze, the corrected contract list is
freezable; nothing else on it moved this window. The plan document itself
needs the rename pass first (`RunResult`/`CommandStatus`/`getRepos` are dead
names; `todo/TODO.md:61` proposes a method on a class deleted in July).

### 1.4 Third-party extension points — the weakest flank

Both extension protocols that broke this window are exactly the ones with no
conformance kit: `otto.testing` exposes asserters for lab repositories,
reservation backends, and inventories — **nothing for host classes or
transfer backends**. Worse, the lab asserter that does exist calls
`repo.list_host_summaries()` **with no arguments** (`testing/conformance.py:217`)
while production now calls it with `inventory=` (`labs/__init__.py:64`) — so
the shipped gate certifies a backend that production TypeErrors on. And
`docs/library/lab-source-backends.md:53` still documents the no-arg
signature: the two library pages now disagree with each other.

### 1.5 Latent cross-family bug (product, shipped in v0.10.0)

`src/otto/coverage/fetcher/remote.py:93` calls
`await host.get(gcda_files, dest, show_progress=False)` **through the `Host`
protocol — whose `get` has no `show_progress` parameter**. Unix/local/embedded
concrete signatures happen to accept it; `DockerContainerHost.get` does not.
A coverage fetch from any container host raises `TypeError`. This is the
signature-non-uniformity class made concrete: the protocol is the contract,
one caller trusts it, one family diverges. (Verified by reading all four
sites; not executed.)

### 1.6 The support matrix does not carry the splintering (Chris, mid-review)

The capability differences §1.3 tabulates — `user=` meaning authenticate-as
on unix ssh, `-u`/chown on containers, documented-ignore on container `get`,
refusal on embedded/local/telnet/ftp; `show_progress` absent from the
container family; sessions taking no user anywhere — are **communicated to
users nowhere**. Verified:

- `schemas/support_matrix.json` is 7 *measured* surfaces × 9 *userland
  profiles* (5 BusyBox, GNU, 3 Zephyr). **Zero occurrences of
  docker/container, `local`, `user`, or `show_progress`** anywhere in it.
  The matrix's own philosophy statement ("none of it is a judgement: every
  verdict was produced by a run") scopes it to measured bed behaviour, so
  whole host families without bed cells simply do not exist on the page.
- No page under `docs/guide/` or `docs/library/` tabulates per-family verb
  support. The `user=` semantics live in docstrings (the `Host.get`
  docstring is genuinely excellent), in non-user-facing spec documents, and
  in this review.
- The precedent for fixing this **already exists on the same page**:
  `docs/architecture/support-matrix.md:132` § "Transfer progress: what each
  backend promises" renders *declared* promises (the `ProgressGranularity`
  ClassVar each backend must register with) beside the *measured* cells,
  clearly typed as a different kind of fact. `86114dfc` built exactly the
  mechanism the host-verb × family grid needs — declare in code, render
  into the matrix page, gate the sync — and stopped at transfer backends.

Two distinct gaps, then: (a) **a missing axis** — declared per-family verb
capabilities (`user=`, `show_progress`, session identity, transfer-backend
restrictions) are a "promises" table the page has no analogue of for hosts;
(b) **missing families** — container and `local` have no presence at all,
measured or declared, even though the container family took two of the
window's four v0.10.0 breaks. Docker cells are also *measurable* with the
existing schema (the e2e stack on test3 is a real target; a container is a
userland reached over a different route, which the matrix's route dimension
already models) — but the declared table is the cheap, high-value half.

### 1.7 Surface drift

`otto.__all__` 26, byte-identical, parity with `_LAZY_EXPORTS` holds —
still ungated. `otto/host/__init__.py` 65 re-exports, still no `__all__`;
only 3 of 9 subpackages declare one (`otto.config`'s is new, added because
PEP 562 lazification forced it). Docs deep-import paths **37 → 55** *(sweep
method; +18, zero removed)* — `otto.inventory`, `otto.logger`,
`otto.declared`, `otto.registry`, deep `otto.docker.*`/`otto.config.*` all
join the taught surface. `register_*` family 25 → 29, two of the new ones
**deep-only** (`register_product_kind`, `register_dev_tool_kind` — reachable
only as `otto.host.product.…`/`otto.host.dev_tool.…`). The
`instructions.py:51` `register_hint="@otto.instruction()"` bug — an error
message advertising an import path that does not exist — survives its second
review unchanged.

---

## 2. Test health and flakiness

### 2.1 CI/nightly record

Every non-green run in the window, with disposition: run `32897991866`
(inherited busybox red at the boundary; closed by the mirror series),
`32977724331` (08-26, busybox.net still down while the drift job forced
`upstream`; root-cause fix `efe03bd1`, #266 closed), `33098700462` (08-27
nightly, same outage reaching the moved job; root-cause fix `d12371e4`, #267
closed), and `32984008411` (all 15 jobs cancelled with zero steps — runner
pickup, not a test failure). Since `86114dfc` (08-27): **20 consecutive green
push runs, 6 green nightlies, one re-run total in 98 runs (and that one
pre-window).** Zero `continue-on-error`, zero `|| true` in the pipeline.

One structural gap found *(verified)*: `report-failure` is `if: failure()`
over all real jobs — but `failure()` does not fire when every need is
*cancelled*, so a run red purely from cancellation files no issue (exactly
run `32984008411`'s shape).

### 2.2 Flake handling: principled throughout

All 13 test/fix commits in the window read; **zero paper-overs**
*(sweep, headline commits re-verified)*. The canonical case: `efe03bd1` moves
upstream-drift detection off the push gate *because* "monitoring enforced as
a merge gate fails when a third party is down rather than when the change is
bad, which is what a flaky gate is" — proven RED against the old ci.yml, and
the invariant is pinned from three sides. Three commits refuse the
timeout-bump lid explicitly (`1cfb15c3` "a seed-dependent timeout is a
flake"; `a4ec029d` clock-anchoring; `74b4c3d5` refusing off-loopback dials
and **failing** the test that tries).

The two serialization commits are genuine resource fixes, not masking:

- `aa957c5d` — five TCG BusyBox guests share test1's two cores; parallel
  cells starve each other's readiness polls. Serializes 10 of 49 conformance
  cells on their own fair lock (~+14 s of 1200 s budget); BusyBox/Zephyr
  stay mutually parallel; pins run in the default lane.
- `dcd9c56f` — the hop nc tests hard-coded test2 and sat outside both
  existing serialization layers; two processes could reserve the same remote
  port (measured: two transfers swapped byte counts 146↔33 on the nox 3.10
  leg). The commit takes the flock pool lease and — **honestly, in its own
  body** — records that concurrent same-host transfers remain the intended
  end-state and need collision-proof port negotiation *in otto*, not suite
  locks. Two residuals are unrecorded (§2.5): no ledger/issue for that
  product work, and the 08-11 nc-listener ledger still claims the old fix
  "closed the cross-worker window".

### 2.3 Issue #283 (the one open flake)

`test_first_party_is_never_refused` — honest (no skip/retry/xfail/group
anywhere near it), unowned (no ledger, no commit references it). Analysis
*(sweep, mechanism verified in the fixture)*: test hygiene, not product —
`_isolate_registries` snapshots at *this test's* setup and restores at
teardown, so it prevents leaking **out**, never **in**; higher-scoped
imports land inside every snapshot by design. The test's own comment claims
it relies on the same isolation as `test_default_instructions.py` — which in
fact **rolls its own** `_clear_first_party()`. Fix direction: own the
registry state like the sibling does. Its self-pin covers only the leak-out
direction.

### 2.4 Papering census (all flat or improved)

skips 34 → 38, all 4 new ones root-EUID capability probes; xfail 2, both
`strict=True`; retry: zero automation (the conftest hookwrapper exposes
otto's *product* retry feature to sample SUT repos; otto's own tests use it
nowhere); sleeps +4, none a grace sleep (one injected stub delay, two inside
subprocess argv strings, one zero-duration yield); `filterwarnings` still
bare `"error"`; real `-n0` serialization flat at 27 `serial_timing`
applications ≈ 0.3% of 8,697 tests; `xdist_group` 65 → 69 (two new groups,
both justified). The `not busybox and not conformance` count 11 → 20 is
`df9c936f` *correcting* legs that had silently dropped exclusions.

**One new defect:** `tests/unit/inventory/test_netbox.py:232`
(`ec5ffac1`) asserts `elapsed < 0.9` on the fast arm of a timeout test, in a
plain unit test riding every `-n auto` lane — a wall-clock discriminator in
the §1.5 class, unmarked. The guard cannot catch it:
`test_lane_invariants.py:355-379` walks a hand-maintained allowlist
(`_SERIAL_TIMING_TESTS`, 7 names, unchanged in window) — **it pins marker
retention, not discriminator detection.** All six 08-25 §1.5 items are
unchanged (verified one by one; the two nc rows' surrounding rationale was
rewritten but the rows still carry no `xdist_group`).

### 2.5 Residuals, redundancy, and stale prose

- **`unix user=` has zero live-level coverage** *(verified by grep)*: no
  integration/e2e/conformance test calls `exec/put/get(user="…")`; the whole
  `d7426e75` feature is pinned by mocks (the e2e suite covers the
  *login-proxy* mechanism instead). The dev session's live-bed leg was run
  interactively, not committed. For the flagship stability feature of the
  window, the suite cannot detect an authenticate-as regression on a real
  host.
- Redundancy (small, enumerated): `_validate_user` re-pinned at mock level
  at 6 non-dry-run call sites beyond its 13-case parametrized home; one
  intra-file duplicate user=-refusal pair (`test_unix_host.py:732,741`); one
  e2e cache-clear duplicate asserting a strictly weaker predicate
  (`test_root_flags_e2e.py:46`); seven tests pin the `_options_params`
  pass-through wrapper rather than its target; one integration-described
  file under `tests/unit/` (`test_import_and_register.py`). The pytest-native
  refactor left **no** old-generation tests behind (verified: the spec's own
  exit-criterion grep returns only negative assertions).
- Standing risks unchanged: `unit-repeat` still has no `timeout-minutes`
  (19 m 24 s at HEAD, 6-hour exposure) and the harness-bounds guard only
  inspects `make`-invoking jobs, so it cannot see it; nightly still draws
  one seed per process with no seed recorded outside the pytest header;
  PYTEST_ADDOPTS: 17 nested-pytest files, 11 neutralized, 6 not (one with no
  `env=` at all), and `tests/_fixtures/support_matrix.py:624` cites
  `tests/unit/test_pytest_addopts.py` — a guard that has never existed.
- Monitoring inertness: `check_busybox_upstream_drift.py` warns-and-exits-0
  on an unreachable upstream with no consecutive-outage tracking — a
  prolonged outage leaves drift detection silently inert (classification is
  well-guarded; persistence is not). The support-matrix downgrade guard has
  no staleness dimension (`as_of` is never read) and runs only from the dev
  VM's `make release-matrix`.
- Zephyr: #260 untouched for a third review (a second occurrence was
  reported in-window); `zephyr-2.7` is now `measured-ok` on **6** surfaces
  (was 5) while the 2.7 net-buffer leak ledger stays open-by-choice — the
  honesty question from 08-25 is still unanswered and the exposure grew.
- Stale prose (the automation-vs-prose thesis, test edition):
  `todo/nc-listener-readiness-under-concurrency-2026-08-11.md:122` (claim
  refuted by `dcd9c56f`), `Makefile:900-910` sizing figures (pre-`aa957c5d`),
  `no-tuple-return.yml` note (12 vs 10), `migration_plan.md` dead types.

---

## 3. Churn and design debt

### 3.1 Profile

| | 2026-06 | 2026-07 | 08-06→08-25 (n=150) | **08-25→09-02 (n=66)** |
| --- | --- | --- | --- | --- |
| rate/day (human) | 6.0 | 15.2 | 10.5 | 8.2 |
| feat | 19.9% | 29.7% | 26.7% | 19.7% |
| fix | 12.2% | 21.7% | 24.7% | 13.6% |
| **refactor** | 7.7% | 1.9% | 2.7% | **1.5%** |
| test | 4.4% | 10.8% | 14.0% | 12.1% |
| docs | 17.7% | 10.4% | 25.3% | **33.3%** |

0 merges, 0 reverts. `!` rate 0.63 → 0.75/day marked (≈1.4/day counting
unmarked). Docs is now the largest category — the getting-started overhaul,
version banners, and 14 specs. **Structural work (refactor+perf) is 3.0% —
the lowest of any measured window.**

### 3.2 Hubs (code lines, this review's counter — deltas comparable, levels not)

`host/userland.py` 1375 → **1260 (−8%, the only large-hub shrink ever
measured — `caf32183` deleting `nc_dash_n`)**; `session.py` +2%; `host.py`
+5% (entirely inside existing bodies; `BaseHost`'s method set is
byte-identical 58 names); `cli/invoke.py` +4%; `nc.py` +5%;
`link/manage.py`, `tunnel/manage.py`, `orchestrator.py` **0%**;
`cli/cov.py` **frozen, fourth review**. Growers are feature-shaped:
`cli/docker.py` 220→468, `models/settings.py` +37% (386→530),
`config/completion_cache.py` +18% (→1003, now #4 in the tree),
`cli/init.py` +134, `unix_host.py` +16%. Totals: `src/otto` +15.3% code
(36.7k→42.3k, my counter), files ≥500 (≈ prior ≥600) **15 → 20**;
`tests/` functions 7.5k → 8.7k (+16%); `scripts/` **+22%** (8,690 raw).
Test growth is ~92% unit-level; `tests/integration/` did not grow at all.

### 3.3 The standing menu: 0 of 14 closed

N1 unchanged (a Host-API field still costs **7 declaration sites** — verified
live on `products`/`dev_tools`; nuance: spec-only fields like
`hw_version` cost 4). N2 unchanged (no new clusters absorbed; zero
extracted). N3 mostly unchanged — `GAPS` still a bare list with three linear
walks; **the declared-products commit built a real `KindRegistry` and left
`_PRODUCT_PROVIDERS`/`_DEV_TOOL_PROVIDERS` as bare lists beside it, so each
seam now runs two registration mechanisms back-to-back**
(`host/factory.py:228-234`; the config-wins-over-code *precedence* is
specced — the mechanism split is the debt). N4 5→4 and N11 3→1 improved **by
deletion, not extraction** (`caf32183`); N11's root stands (one raw
`stat -c %s` at `nc.py:1008`; still nobody consults the `stat_size` probe).
N5 **worsened** (78→81 hand-mirrored CLI docs files, no gate). N6/N7/N8/N9/
N12/N13 unchanged (N8 and N13 byte-identical). N10 **worsened by volume**:
`scripts/` +22%, still explicitly excluded from tach (`tach.toml:74`), ty
(`include=["src"]`), and the ast-grep roots.

### 3.4 New debt (window)

- **Docker use-cases** (`c4b5df95`): the capability namespace is open
  strings from any repo's `[[docker.use_cases]]` — no vocabulary, origin
  attribution, or collision detection; a typo'd `provides` silently becomes
  a new capability with one winner. (The tie-refusal logic itself is
  well-argued with three actionable messages.) Eight near-duplicate pairs
  across `compose.py`/`deployment.py`/`cli/docker.py` *(sweep;
  displacement rendering verified as 3 copies)*; a transient-network-race
  retry gated by substring sniffing (`compose.py:48-53`) with an escalation
  runbook as a comment (`:508-515`); a documented never-cleaned
  `mkdtemp` per host per deploy (`deployment.py:373-376`). Docker never
  touches the `KindRegistry` it shipped beside.
- **Startup I/O budget** (`3ae343b1`): `--version` implemented twice
  (`_shim.py:23-39` + `main.py:80-101`, divergence argued in both, pinned);
  a two-module import cycle whose direction is load-bearing and declared
  nowhere (`cache_sections` ↔ `completion_cache`); `SECTIONS` is a bare
  list with a linear lookup; the cache `names` key set is spelled ~6 places;
  `ROOT_HELP_ARGV` hand-copies `help_option_names` with no pin;
  `Section.collect` is exercised only by tests — production writes bypass
  the abstraction that describes them.
- **Cache management** (`978369c2`): `CACHE_FILE_NAMES` hand-copies two
  constants (drift test-guarded, argued); `clear_cache()`/
  `clear_remote_cache()` survive as test-only orphans still claiming
  `otto cache clear` calls them.
- **Registry-shaped decisions without a registry: 3 new** (docker capability
  namespace; `SECTIONS`; and `INVENTORY_BACKENDS` — a real registry whose
  *contract* is bypassed: hard `== "json"` branches at
  `inventory/config.py:100,156,216` decide validation, caching, and
  construction, so a registered third-party backend takes the unvalidated
  `else` arm of all three). Against that: **4 seams built correctly on
  registries** this window (compose adapters, inventory lookup, product and
  dev-tool kinds) — the first window where the with-registry count beats the
  without.
- Suppressions: `type: ignore` 223 → 264 (**+18.4%**, the only class
  outgrowing the +15% tree); `ty: ignore` +3; `noqa` +7%;
  `ast-grep-ignore` flat at 21. **Zero** new TODO/FIXME/HACK/XXX/workaround
  comments (17, flat — debt goes to ledgers here, which is why ledger flow
  §3.5 matters).

### 3.5 Ledgers

19 opened / 2 closed this window (prior: 44/1). Open total: **78 todos +
113 specs = 191.** Closed: the docker-reaper ledger (fixed) and
`lab_flexibility.md` (superseded by lab v2). None of N1-N14's ledgers moved.
The close *rate* quintupled (0.05 → 0.25/day) — from a floor so low the
absolute picture is unchanged.

### 3.6 Healthy (the counterweight, and it is substantial)

The `inventory/` package born clean (protocol + registry + errors + doctor,
12 modules, its own error family in the AST gate); the transfer progress
contract enforced **at registration time** with a worked-value error message
(`86114dfc` — the window's best structural commit); `OttoError` 48 → 52, all
gated, the two new rootless errors argued and enumerated; ratchet baselines
byte-identical under +5.6k code lines; `caf32183` a net deletion that shrank
the biggest hub; `7885b74c` deleted a shim class and extracted
`suite/markers.py`; three releases shipped clean; the busybox mirror series
turned the default gate's only third-party network dependency into a
cached-mirror-first flow with the reasoning written into ci.yml.

---

## 4. The menu

Efforts: **S** ≤ 1 day, **M** 2–3 days, **L** ≥ 1 week. Ordering within
tiers is the recommended order.

**Tier 0 — defects and record fixes (this week, all S)**

1. Fix `coverage/fetcher/remote.py:93` — `show_progress` is not on the
   `Host` protocol; decide (add it to protocol + docker no-op, or drop the
   kwarg at the call site) and add the cross-family pin (§1.5).
2. `testing/conformance.py:217` — call `list_host_summaries(inventory=…)`
   the way production does, and fix the stale signature in
   `docs/library/lab-source-backends.md:53` (§1.4).
3. Mark or restructure `tests/unit/inventory/test_netbox.py:232` (either
   `serial_timing`, or assert the adapter's timeout *configuration* instead
   of elapsed wall time) (§2.4).
4. #283 — own the first-party registry state in
   `test_instruction_ownership.py` the way `test_default_instructions.py`
   does; open the ledger it currently lacks (§2.3).
5. Stale-prose sweep: `migration_plan.md` contract names
   (`Results`/`CommandResult`; `get_repos`), `todo/TODO.md:61`,
   `todo/nc-listener…08-11.md:122` + a ledger for the deferred
   collision-proof port negotiation, `Makefile:900-910`,
   `no-tuple-return.yml` note, `instructions.py:51` `register_hint` (§0, §1.7).

**Tier 1 — the API-cost sprint (unchanged from 08-25, urgency now proven;
all S, ~one sprint total).** The window demonstrated every failure mode these
gates exist to stop: v0.8.8 shipped unmarked breaks as a patch; `cache`
missed the hardcoded CLI list; the suite break shipped with no diagnostic.

6. `py.typed` (+ wheel include); public-API snapshot test
   (`__all__`/`_LAZY_EXPORTS` parity + sanctioned deep paths resolve);
   bump-policy refusal in `make release` (census `$(LAST_TAG)..HEAD` for
   `!`/`BREAKING CHANGE`, refuse `BUMP=patch` — precedent:
   `release-matrix`'s downgrade refusal); finish 4.3 by **deleting both
   literal lists**; removed-key table backfill (`valid_labs`, `${sut_dir}`,
   `markers`→`rules`, `nc_dash_n`, `default_host`); changelog `## Migration`
   section (or minimally: render the `BREAKING CHANGE:` footers and link
   migration docs — the lab-v2 doc exists and the CHANGELOG doesn't point
   at it).
7. **Extension-point conformance kit** (new since 08-25, motivated by §1.4):
   `assert_host_conforms` / `assert_transfer_backend_conforms` in
   `otto.testing`, exercising the exact call shapes production uses
   (keyword `inventory=`, `progress_granularity` present, protocol-only
   kwargs). This is what makes third-party breaks *visible to the third
   party* before an upgrade lands.
8. **Host-capability grid, documentation first (Chris, mid-review; §1.6):**
   a user-facing page tabulating family × verb support — `user=` (its three
   meanings and the refusals), `show_progress`, session identity,
   transfer-backend restrictions — for **every** family including container
   and `local`. Hand-authored now, but born with a gate: at minimum a test
   asserting the table's row set equals the shipped host families and its
   `user=` cells match the per-family refusal sites, so a new family or a
   flipped semantic cannot ship without the page moving. Link it from the
   support-matrix page with the declared-vs-measured distinction that page
   already draws for transfer progress. **S.**
9. **Marking discipline as a rule, then a gate**: contributing.md sentence —
   "a commit that deletes a settings key, a public symbol, or a protocol
   parameter is `!` regardless of tier" — then the cheap heuristic in CI
   (diff removes a model field / `__all__` name / protocol param without `!`
   in the subject → fail). Prose first is fine; the 08-03 thesis says don't
   stop there.

**Tier 2 — host-API freeze prep (M; do before inviting users onto `Host`)**

10. Disposition every named `Host` follow-up (§1.3): land or explicitly
    defer-past-freeze the parity-spec §8 items, the `open_session`/
    `HostSession` user binding, `ShellCommand.user`, get-side ownership.
    Exit criterion: an empty foreseeable-breaks list for the protocol, in
    the spec, before `docs/stability.md` names it Tier A.
11. N1 — collapse the `Host`/`BaseHost` double declaration (7 sites →
    ideally 2) *before* the freeze; every remaining pre-freeze protocol
    change pays the 7× cost until this is done.
12. **Declared per-family capability promises (§1.6, the mechanized half of
    item 8):** extend the `ProgressGranularity` pattern — each host family
    declares its verb × option support in code (a ClassVar table the
    registration/factory path refuses to accept incomplete), the matrix
    page renders it beside the measured cells, a docs-sync gate pins the
    two together. Item 8's hand-written page then becomes generated
    output instead of a fourth hand-mirror. Follow-up, separately
    schedulable: measured docker cells — the e2e stack on test3 is a real
    target and the matrix's route dimension already models
    transport-not-userland differences. **M.**
13. A committed live `user=` test (integration, one ssh host): the flagship
    feature currently has zero non-mock coverage (§2.5).
14. Commit `settings.schema.json`/`lab.schema.json` + regenerate-diff gate
    (4.4) — after lab v2 settles, so the schema history starts at the new
    shape.

**Tier 3 — repayment and guard-rail (the standing list, now 4th review)**

15. N5 with teeth: derive the CLI docs tree (or at least gate file-set
    equality against `CLI_COMMANDS.names()`); N10: put `scripts/` under ty +
    ast-grep + tach (it is release-critical and grew 22%); N9 treewide
    marker (specified since 08-16, speeds every other item's loop).
16. CI bounds: `timeout-minutes` on `unit-repeat` (and the nox-invoking
    jobs generally) + extend the harness-bounds guard to see non-`make`
    jobs; fix `report-failure`'s cancelled-run blindness (§2.1).
17. The 08-25 §1.5 six (unchanged for a review): mark/convert the three
    discriminators, group the two nc rows, nox web-dist prereq, `free_port`.
18. Zephyr: #260's message (third review) and an explicit ruling on
    `zephyr-2.7 measured-ok`-while-leak-open — either a matrix annotation
    or a written acceptance; the surface count growing (5→6) makes silence
    costlier each window.
19. PYTEST_ADDOPTS: neutralize the 6 raw nested-pytest sites; delete the
    phantom-guard citation (or write the guard it cites).
20. Drift-monitor persistence: consecutive-outage tracking so a long
    busybox.net outage cannot leave upstream-drift detection silently
    inert.

**Not recommended, still:** the general ast-grep migration pipeline /
`otto migrate` verb (08-25 §2.3 P3 measured it at 14% coverage);
pyproject extras (2026-08-05 reasoning stands).

---

## 5. Corrections to prior records

- 08-25 review §2.1 "two group-enumerating tests hardcode 8 of 12 — neither
  updated": one consumer was since wired to `CLI_COMMANDS.names()` but for
  the *docs* gate; both literals survive and drifted further (`cache` in
  neither).
- 08-25 §3.3 N14 "byte-identical `_print_dry_run_plan`": never byte-identical
  (parameter name differed at the time); the window let the docstrings
  diverge while the code stayed duplicated.
- 08-25 §3.3 N14 places the "staging failed" relay at `unix_host.py:950`;
  it is at `docker_host.py:843`.
- 08-25 §1.6 "12 files with nested pytest": re-measured at 17 (11
  neutralized, 6 not, 1 partial).
- The sweep-reported "13 breaks / 7 unmarked" headline is corrected to
  **11 / 5** here (two relaxations excluded, §1.1).
- Session memory says the `user=` live-bed leg PASSED — true for the dev
  session's interactive run; **no committed test** exercises it (§2.5).
- There is no `wall_clock` marker at HEAD (removed 2026-08-08); the live
  construct is `serial_timing`. Prior prose that says otherwise is stale.

## 6. Unverified / out of reach

- Whether any current lab config actually fetches coverage from a container
  host (the §1.5 TypeError is verified from code, not executed).
- External user count per release — the census assumes worst case, as
  before.
- RTD-side redirects for the docs reorganizations remain unverifiable from
  the repo.
- Nightly seed coverage adequacy (one seed/process/night) — flagged, not
  measured.
