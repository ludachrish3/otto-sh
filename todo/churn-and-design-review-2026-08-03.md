# Repo-wide churn review and critical design review

**Date:** 2026-08-03
**Method:** quantitative git analysis (full history + since 2026-05-01) plus seven
parallel subsystem reviews (host, CLI, config/models, coverage, monitor,
cross-cutting, web), each scored against the 2026-07-10 whole-repo review as a
baseline so this measures *maturation*, not just a snapshot. Companion to
[refactor-feedback-2026-08-02.md](refactor-feedback-2026-08-02.md).
Effort scale: **S** ≤ 1 day, **M** 2–3 days, **L** ≥ 1 week.

---

## 0. Executive summary

Otto is 14 weeks old and July ran at 2.5× June's commit volume (458 vs 179).
Under that load, roughly **half of the July-10 review findings were materially
fixed** — an unusually good hit rate — but the fixed and persisting halves are not
randomly distributed, and the split is the single most important fact in this
review:

> **Every line otto holds by automation has held. Every line held only by prose
> has decayed.**

Fixed since July: wire-type drift (codegen + CI gate), stale schemas (generated +
staleness-validated), tunnel↔link sentinel duplication (shared `daemon.py`),
`asyncio.run` sprawl (now exactly 1, AST-guarded), import budget (caps unchanged
5 weeks, snapshots *shrank*), vendored-UI drift (hash-gated, byte-clean),
`configmodule`/`storage`/`correlator`/`oneshot` renames. All of these have a gate,
a generator, or a guard.

Persisting or worse since July: host field multiplicity (38 verbatim-shared
fields, now with a real divergence bug), no `OttoError` (9 families → **26
classes, 8 roots**), `(Status, str)` tuples (docker regressed to bare `Status`),
monitor acquisition branch (1 site → **5**), core→cli imports (3 of 4 verbatim),
`Repo.settings` raw-dict readers, no extras in pyproject, `suite/plugin.py:7`
phantom docstring. None of these has a gate; each was a prose rule, a comment, or
a todo.

The second structural finding: **extractions have stopped at the wrong
altitude.** Phase 1 built `broadcast.py`/`db.py`/`store.py` but kept delegating
façades on the collector — which is now *larger* than before Phase 1. The
library-first workstream extracted `collect_coverage` but left the get/clean
workflows in `cli/cov.py` — which *grew* 17% while its sibling `cli/test.py`
halved. `daemon.py` extracted sentinel primitives but both launch→verify→rollback
state machines remain. The hubs keep every reason to be edited, so they keep
churning.

Churn quality also shifted in July: fix share doubled (12%→22%), refactor share
collapsed (8%→2%). Velocity went entirely to features and firefighting. The
"aging" feeling is real, it is measurable, and it dates to July.

---

## 1. Quantitative churn

### 1.1 Cadence

| Month | Commits | New src files | feat | fix | refactor | test |
|---|---|---|---|---|---|---|
| 2026-04 | 36 | 77 | — | — | — | — |
| 2026-05 | 91 | 7 | — | — | — | — |
| 2026-06 | 179 | 48 | 20% | 12% | 8% | 4% |
| 2026-07 | 458 | 83 | 31% | 22% | **2%** | 11% |

### 1.2 Hotspots (churn × size × rework × fix-density)

| File | Lines | Commits since May | Lifetime rework* | Fix % |
|---|---|---|---|---|
| [host/session.py](../src/otto/host/session.py) | 1919 | 51 | 1.8× | 24% |
| [cli/test.py](../src/otto/cli/test.py) | 638 | 44 | **5.2×** | 20% |
| [cli/main.py](../src/otto/cli/main.py) | 698 | 44 | 2.7× | 20% |
| [monitor/collector.py](../src/otto/monitor/collector.py) | 853 | 42 | 3.1× | — |
| [config/completion_cache.py](../src/otto/config/completion_cache.py) | 1405 | 29 | — | — |
| [config/repo.py](../src/otto/config/repo.py) | 845 | 28 | — | — |
| [monitor/server.py](../src/otto/monitor/server.py) | 917 | 28 | 1.6× | 25% |
| [cli/cov.py](../src/otto/cli/cov.py) | 961 | 35 | 1.7× | 14% |
| [host/login_proxy.py](../src/otto/host/login_proxy.py) | — | 12 | — | **33%** |

\* total lines ever changed ÷ current size. `cli/test.py`'s 5.2× includes its
successful halving (moved code counts as churn); `collector.py`'s 3.1× does not —
it was extracted *from* and grew back.

**Correction factor:** per-commit categorization in the two files checked shows
**~22–26% of per-file commit counts are repo-wide lint/format/typing sweeps**
(11 of session.py's 50; 11 of main.py's 42). Raw counts overstate design signal
by about a fifth. And commit *sizes* are inflated by in-diff plan/design docs
(see the 08-02 doc: 59% of the timeout commit).

### 1.3 Structure

- God files (≥800 lines): session.py 1919, completion_cache.py 1405, host.py
  1242, coverage/reporter.py 1075, transfer/nc.py 1055, cli/cov.py 961,
  unix_host.py 941, monitor/server.py 917, collector.py 853, config/repo.py 845.
  Web: topo/layout.ts 704 is the largest authored TS file.
- Tightest cross-package co-change (small commits only): `models/monitor.py ↔
  monitor/collector.py` (8) — the codegen seam, expected; `cli/main.py ↔
  config/completion_cache.py` (5) — the completion-cache shadow-schema coupling,
  not expected; `monitor/server.py ↔ suite/suite.py` (4).
- Test:src churn ratios since May: monitor 1.97, suite 1.91, coverage 1.46,
  host 1.43, cli 1.16. Healthy for the discipline here; the timeout-commit
  analysis showed most test churn is new coverage, not repair.
- Commit-size distribution: median 2 source files, mean 8, p90 20 — the tail is
  deliberate cutovers (lab.json, registry unification, renames), which is what
  pre-user architecture change is supposed to look like.

### 1.4 Why session.py churns (the exemplar)

Only **3 of its 50 commits are about the session engine.** The rest are features
(login proxy, AppShell, elevation, shell history, LogMode), chaos hardening,
concurrency fixes, and sweeps that had to touch it because 13 separable
responsibilities live in one file — 4 transport subclasses, exec pooling,
recovery, user elevation, login-proxy replay, registry/locking, logging/redaction,
history suppression, transport dispatch (three copies of `match term`). Its top
co-changers are six different modules. **It is a hub, not a hotspot** — and the
same is true of collector.py (8 responsibilities) and main.py (5 jobs; 57% of its
commits touch the root callback).

---

## 2. Maturation scorecard vs 2026-07-10

| July finding | Verdict | Evidence anchor |
|---|---|---|
| 1. Test/cov orchestration only in cli/ | **suite FIXED / cov HALF** | `run_suite` clean library API; `cli/cov.py` grew 820→961, ~400 lines library logic + private exception hierarchy |
| 2. tunnel↔link duplication | **primitives FIXED / lifecycle PERSISTS** | `host/daemon.py` owns codec+ps-scan+kill; launch→verify→rollback still written twice; `Provenance.DYNAMIC` still dead (now with two apology comments) |
| 3. Host field multiplicity | **PERSISTS, slightly WORSE** | Unix∩Embedded = 38 verbatim fields; 204 byte-identical lines; **`close()` diverged: chaos fix in unix_host.py:502 never mirrored to embedded_host.py:407** |
| 4. Library hygiene | **HALF** | logger lazy ✓, reservations typer-free ✓; `import otto.host` still pulls rich (transfer/__init__.py eager re-export); Repo's 5 Rich-panel methods persist; typer now module-level in `suite/register.py`, `params.py` |
| 5. Extensibility bypasses | **MIXED** | Tunnel CARRIERS seam ✓, 3 parser registries ✓ (17 registries total); coverage still **zero** registries and renderer seam *deleted*; monitor acquisition branch **1→5 sites**; product providers still a bare list; 3 hardcoded option-spec lists held by drift tests |
| 6. No pyproject extras | **PERSISTS verbatim** | one flat 20-dep list; fastapi/uvicorn on every install |
| 7. Scattered outcomes | **WORSE** | 9 families → 26 exception classes / 8 roots, no OttoError; docker regressed to bare `Status` (staging.py:130, compose.py:313); `typer.Exit(1)` 36→43 in 4 dialects; unified `_render_result` has 1 caller |
| 8. /api/data TS hand-mirror | **FIXED** | codegen + `git diff --exit-code` in CI; residue: `/api/mode` (3 fields, hand-written both sides) |
| 9. Doc drift | **MIXED** | schemas now generated + validated ✓; monitor `__init__` verbs ✓; `suite/plugin.py:7` `_test_lifecycle` phantom survives verbatim |
| 10. Terminology | **MIXED** | configmodule→config ✓, storage→labs ✓, correlator→merge ✓, oneshot→exec ✓; element/board/slot kept **by recorded decision**; `term` entrenched into 6-symbol registry vocab; coverage "context" now **5 senses**, "stamp" 3 |
| Server-safety traps | **PERSISTS +2** | logger `_state` global unchanged; 2 of 4 monitor registries still raise on re-register; **new**: two import-time uvicorn logger mutations (server.py:87,124); bootstrap `_discovery_errors` accumulator now a *filed bug* with the fix declined |

Score: ~12 fixed, ~13 persist/worse. The pattern in §0 (gated vs prose) predicts
which side almost perfectly.

---

## 3. The five design patterns behind the churn

### P1. Façade-preserving extractions don't shrink hubs

Phase 1 extracted store/db/broadcast/export from the collector *but kept
pass-through methods on `MetricCollector`* so consumers wouldn't churn — and the
collector is now bigger than before Phase 1, still owning acquisition dispatch,
scheduling, parsing dispatch, health, DB orchestration, broadcast, event CRUD,
and a 65-line presentation model. The identical shape: `collect_coverage` was
extracted but `cli/cov.py` kept the workflows and grew; `daemon.py` extracted
primitives but both lifecycle state machines remain; session.py's clean
extractions (`shell_liveness`, mostly-clean `command_frame`) coexist with 13
retained responsibilities. **An extraction only reduces churn if ownership moves —
the façade preserves every reason to edit the hub.**

### P2. Validation gate ≠ representation ("Path B")

`settings.toml` is validated through `SettingsModel`, the validated object is
discarded, and 8 production sites read the raw dict (`Repo.settings` is a public
field; `repo.py:624` bypasses its own model *on the line after validating*
because the model's type annotation is wrong for it). All five `[coverage]` specs
are documented "validation-only". `[lab]` is validated twice and used raw.
Consequences measured: every coverage feature edits two places (4 of
models/settings.py's commits), lab.json has **three independent raw parsers**
(json_repository, completion_cache ×2, cli/init), and a custom lab backend
registered through the shiny `LabRepository` seam gets no tab completion and no
tunnel discovery because those readers only understand the built-in JSON layout.

### P3. The registry pattern is real but the biggest decisions bypass it

17 live registries on one clean generic engine — and then: coverage (the
second-largest subsystem) uses **zero**, and deleted its renderer alternative
rather than registering it; monitor acquisition is an if/else that metastasized
to 5 sites (log-sourced parsers had to *tunnel through the shell branch* because
there was no slot to register into); product providers are a bare list wearing
the "same extension hook" docstring; per-protocol option specs are three parallel
hardcoded lists guarded by drift tests — the exact boilerplate registries exist
to remove.

### P4. Shadow copies and mirror sites are the amplifier

The completion cache is a hand-synced denormalization of *eleven* sources —
every namespace change bumps its schema (v3→v11, all eight bumps driven by
unrelated features), `main.py:648-676` hardwires the 11-collector write, and the
cache reaches into CLI privates, which is 2 of the 3 surviving backward imports.
Host options are re-declared in **7 signatures** (fleet ×5, context ×2) plus 8
spec/runtime dataclass pairs. UnixHost/EmbeddedHost share 38 fields and 204
lines, which has already produced the `close()` divergence bug. Web has two app
shells, two `/`-handlers, four theme owners, and a 32-line duplicated CSS
preamble. Every one of these turns one semantic change into N edits, and N is
what the timeout commit *felt* like.

### P5. Gated lines hold; prose lines don't

The evidence is almost embarrassingly clean. Held for 5+ weeks with zero
raises: import-budget caps (snapshots *shrank*), vendored-UI content hash,
wire-type diff gate, `asyncio.run` AST guard, warnings-as-errors web builds,
airgap/brand gates, console-output-fails vitest policy. Decayed in the same
window: "models never imports config" (written down only in a plan doc, violated
as literally worded, enforced by nothing), "open the DB before spawning the
task" (a convention replicated as comments at four call sites, after a
five-issue flake wave), completion-cache docstring (three versions stale),
`suite/plugin.py:7` (phantom name, survived a full workstream), 59
plan-coordinate comments ("Task 10") in shipped coverage source, `classicScript`
build plugin that silently no-ops if Vite changes a bundle key. **The repo's own
history is a controlled experiment: automation is the only enforcement that
survives this commit rate.**

---

## 4. Ranked findings

### Tier 0 — divergence/correctness issues found by this review (do soon regardless of refactor plans)

| # | Finding | Effort |
|---|---|---|
| 0.1 | `EmbeddedHost.close()` missing the chaos-hardening transport-close fix that `unix_host.py:502` got — embedded sessions can strand transports on refusal | S |
| 0.2 | Monitor memory/latency compound on long live runs: unbounded `Broadcaster` queues (broadcast.py:20) + unbounded `MetricStore.series` + O(run-length) `/api/monitor_sessions` re-hit on **every SSE reconnect** | S–M (bound queues + maxlen S; cache/limit the live export M) |
| 0.3 | Acknowledged orphaned-bucket bug (collector.py:428-440): gather re-raise orphans sibling loops — live *today* whenever `tunnel_source` is wired | M |
| 0.4 | `otto test`'s pytest session runs **outside** `run_command` — the longest, most host-holding phase has no two-stage interrupt policy (suite/run.py:589) | M |
| 0.5 | `cli/cov.py:530` raw `git config` shell-out: bypasses gitio hardening AND runs in process CWD → wrong `user.email` when run outside the SUT | S |
| 0.6 | `bootstrap._discovery_errors` accumulator (filed in todo/, fix declined): one failed discovery poisons every later result in-process | S |
| 0.7 | DB-init "open before spawn" is comment-enforced at 4 call sites after a 5-issue flake wave — needs one owning seam | S–M |
| 0.8 | `classicScript()` silently no-ops if the bundle key changes → covapp stops loading over `file://` with no build error; add `else throw` | S (minutes) |

### Tier 1 — highest-leverage structural fixes

| # | Fix | Effort | Payoff |
|---|---|---|---|
| 1.1 | **Move the 4 duplicated session-delegation methods + `verify_connection` onto `RemoteHost`** (it already declares `_session_mgr`); kills the close()-divergence class | S–M | Ends the 204-line duplication *mechanism*, not just today's instance |
| 1.2 | **Split session.py along its visible seams**: transports (~290 ln), exec pool (~150 ln), recovery, elevation | M–L | ~23% of the top hotspot leaves with near-zero design decisions; the rest becomes reviewable |
| 1.3 | **Delete `Repo.settings` raw-dict API**; route `[coverage]`/`[lab]`/`[reservations]` through the specs that already validate them | M | Ends P2; halves the models churn tax; 8 call sites |
| 1.4 | **Give `LabRepository` a cheap `list_host_ids()`** and route completion/tunnel/init through it; kills 3 raw lab.json parsers and makes custom backends real | M | Fixes the bypass class + tab-completion latency (no more full host construction to read `.id`) |
| 1.5 | **Move `INSTRUCTIONS` to core beside `SUITES`** | S | Closes 2 of 3 backward imports incl. the `sys.modules` hack |
| 1.6 | **`OttoError` base + re-parent the 22 public exceptions** (multiple inheritance keeps current roots — zero behavior change) | S–M | The cheapest before-users interface fix in the repo; gets harder every week (9→26 in 3 weeks) |
| 1.7 | **Collapse the 7-way option-kwarg duplication** into one `HostOverrides` carrier (`all_hosts` already proves `**overrides` works); drop `timeout` from `run_on_all_hosts` (deletes the documented E402 cycle hack in context.py) | M | −6 mirror signatures, −1 circular-import workaround |
| 1.8 | **Finish the cov extraction**: move `_do_get`/`_do_clean`/`_resolve_cov_settings`/`_connect_cov_hosts` (~400 ln) into `otto.coverage`, deduplicate the `suite/run.py` mirror that has *already diverged* | M | cli/cov.py joins test.py's success story instead of moving backwards |
| 1.9 | **`ACQUISITION_BACKENDS` registry** for monitor (shell/SNMP/log-sourced as first-class backends); collapse the 5-site branch | M | Ends P3's worst instance; unblocks the "external libraries provide plots" TODO cleanly |
| 1.10 | **De-façade the collector**: move event CRUD to store/event_ops, meta derivation to models, make consumers call the extracted modules directly | M–L | The Phase-1 investment finally pays out; collector becomes tick-loop-only as the roadmap intended |

### Tier 2 — worth doing, less urgent

| # | Fix | Effort |
|---|---|---|
| 2.1 | Lazy rich in `host/transfer/__init__.py` + scp/sftp; add `import otto.host` budget surface with rich/pydantic denylisted | S |
| 2.2 | pyproject extras (`[monitor]`, `[cli]`…) — a 15-line edit that gets much harder after first release | S |
| 2.3 | Generalize `_render_result` into the leaf-invoke wrapper; retire the 43 `typer.Exit` sites / 4 error dialects gradually | M |
| 2.4 | Split `utils.py` per its own TODO: `Status` → result.py; `cli_exposed`/`Arg`/`Opt` → cli/ (39 importers, ~24 only want `Status`) | S–M |
| 2.5 | `config/repo.py` split: data / Rich panels / pytest collection / git client (7 concerns) | M |
| 2.6 | Coverage: move `read_cov_*` meta readers down to capture/, breaking the backward `produce→reporter` edge; `reporter.py` → meta/fold/tickets (~350 ln core) | M |
| 2.7 | Monitor `/api/mode` → pydantic model through the existing codegen gate | S |
| 2.8 | Web: shared `AppBarShell`; one theme owner (4→1); move TreeView/CodeView to covapp/ or split `ui/` honestly; monitor testUtils (26 stub copies → 3); delete dead `types.gen.ts`; knip entry for `src/main.tsx`; shared CSS preamble | S each, M together |
| 2.9 | Host: declared `rebuild_connections` protocol (3 `getattr` sites silently no-op on embedded); EmbeddedHost through `build_term_backend` (registry bypass + missing rebuild) | S–M |
| 2.10 | Completion cache: registries expose `snapshot()`; cache becomes serialization, not re-derivation; per-key versioning | L |
| 2.11 | Dead/stale sweep: `_element_id_str`/`_slot_str`, `BootstrapError` never raised, `report_config` phantom re-export, `suite/plugin.py:7`, completion_cache docstring versions, stale TODOs (server.py:775, unix_host.py:38-42), the 59 plan-coordinate comments | S–M total |

### Explicitly deferred (agree with the standing decisions, revisit at freeze)

- Spec/runtime field-collapse (`todo/collapse-pure-data-spec-runtime-types.md`) — but 1.1 shrinks the blast radius meanwhile.
- `element/board/slot` vocabulary — locked by recorded decision.
- Choke-point default resolution — wait for a second fan-out (per the 08-02 doc).

---

## 5. Policy candidates (AGENTS.md / CONTRIBUTING.md)

Directly implied by §3/P5 — each rule below exists today as prose and has already
been violated; the ask is to convert each into a gate:

1. **Layering is tested or it isn't real.** Add an import-direction test
   (import-linter or a 30-line AST walk like `test_no_bare_asyncio_run.py`):
   `cli → *` only, `models ↛ config` at module scope, `coverage.capture ↛
   coverage.reporter`. Effort S.
2. **A registry or a recorded exemption.** New extension decision = registry
   entry, or a comment linking a recorded decision not to. Review-checklist
   line, plus periodically grep for `_PROVIDERS`-style bare lists.
3. **One value, one home — now with teeth.** The drift-test pattern
   (`OPTIONS_KEYS`, format constants) is the *fallback*; the rule is derive,
   don't mirror. Add the missing cross-language pin: a test asserting
   `OTTO_COV_DATA_FORMAT == EXPECTED_DATA_FORMAT`. Effort S.
4. **No plan coordinates in shipped source.** "Task 10"/"spec §4" comments die
   at merge (a grep gate on `src/` is trivial); design rationale goes in
   docstrings or docs/architecture.
5. **Conventions that must hold at N call sites become functions.** The DB-init
   rule, the "usage-error exits outside the try" comment duplicated in
   link/tunnel — each is one extraction away from unforgeable.
6. **Every subsystem names its outcome convention.** New public API returns
   `Result`-family, raises an `OttoError` subclass, or documents why not —
   checked in review. (Blocked on 1.6 existing.)
7. **Refactor share is a health metric.** July = 2% is the number to watch;
   sustained < 5% at this feature rate is how hubs form. Cheap to compute
   (the one-liner is in this review's history); consider it a monthly glance,
   not a gate.

---

## 6. What is genuinely maturing well

Worth stating plainly, because the debt list above is long and the base rate is
not bad — it is exceptional for 14 weeks:

- **Lifecycle**: one `asyncio.run`, AST-guarded; two-stage interrupt policy that
  actively defends itself against uvicorn's signal capture; `compensate` shared
  by 6 rollback sites. This was the hardest cross-cutting problem in the repo
  and it is *solved* (minus 0.4).
- **The gate culture**: import budget, vendored-hash, codegen diff, airgap,
  brand tokens, console-fail tests, shuffled test order, differential
  strictness ratchet with a self-deleting escape hatch. This is the machinery
  that makes P5 an opportunity rather than just a diagnosis.
- **The registry engine** and its 17 adopters; tunnel carriers and monitor
  parser API v2 landed *through* it since July.
- **covapp**: designed once, extended monotonically, zero refactor commits,
  colocated tests, clean data/highlight/render layers. It is the existence
  proof that the codebase knows how to do this.
- **The e2e testid discipline** (95% of 377 selectors) and the schema
  generation story (`otto schema export` + staleness validation).

The maturation problem is not capability. It is that the practices that produced
covapp and the lifecycle module are applied at module scale but not yet at
subsystem scale — and that at 458 commits/month, anything not automated decays
in weeks. The highest-leverage change is not any single refactor above; it is
converting the surviving prose rules into the gate form this repo has already
proven it can hold.
