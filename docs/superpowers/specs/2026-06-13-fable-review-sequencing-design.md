# Fable Review — Workstream Sequencing Design

> Captured 2026-06-13. Pairs with [todo/fable_review_outcome.md](../../../todo/fable_review_outcome.md)
> (the findings + locked decisions) and [todo/migration_plan.md](../../../todo/migration_plan.md)
> (the migration this serves). This document is **not** an implementation plan for any
> single workstream — it fixes the *order* in which the review's six decisions are
> tackled, draws the contract-freeze line through them, and records why. Each
> critical-path workstream below gets its own brainstorm → spec → plan cycle when picked up.

---

## Organizing principle: the freeze line

The optimization target chosen for this sequencing is **shortest path to the contract
freeze** (the [migration_plan.md](../../../todo/migration_plan.md) Phase 1 milestone).

That converts ordering from a matter of taste into a hard filter:

> A change belongs **before** the freeze **only if** deferring it would otherwise force a
> *breaking* change to a frozen-contract name or shape afterward. Everything else —
> internal refactors, file splits, test-tree layout, implementation swaps — ships **after**
> the freeze under semver discipline.

The frozen surface (per migration_plan Phase 0/1) is: the `Host` protocol + result types
(`RunResult` / `CommandStatus` / `Status` / `ShellCommand` / `Expect` / `HostSession`),
the *decomposed* config access (`getConfigModule()` / `get_host()` / `all_hosts()` / `Lab`,
plus `RepoOptions` and the suite/instruction `Options` dataclass), stdlib `logging`, and the
extension-point registries. Applying the filter to the review's six decisions produces a
four-item critical path; the rest is explicitly deferred.

---

## Critical path (must land before the freeze)

### 1. Context object + lifecycle  *(review decision #3 + finding #4)*

The foundation, and the most invasive change — so it goes first, while otto's interface
churn is still free.

- Move the mutable global singletons into an explicit context object owned by the CLI entry
  point; keep the existing accessors as **thin shims** for instruction/suite ergonomics.
  Targets: `_logCommandOutput` / `_globalDryRun`
  ([src/otto/host/host.py:40-65](../../../src/otto/host/host.py)) and
  `getConfigModule()` / `all_hosts()`
  ([src/otto/configmodule/configmodule.py:86](../../../src/otto/configmodule/configmodule.py)),
  consumed across `cli/main.py`, `cli/docker.py`, `cli/reservation.py`, `cli/callbacks.py`,
  `host/dockerHost.py`, `docker/compose.py`. Write-once import-time registries stay as-is —
  only the *mutable* singletons are the problem.
- Bundle the **`async with host:` lifecycle and the deletion of `__del__`** here. The
  lab-level fan-out context *is* the context object, and `async with` adds
  `__aenter__` / `__aexit__` to the frozen `Host` protocol, so it must be pre-freeze.
- **Why it gates the freeze:** the contract freezes around the decomposed config-access
  *shape*; this workstream defines that shape.
- **Bonus payoff (not just freeze-gating):** retires a live bug class — issue #53 loop
  churn, the parked "unclosed event loop" ResourceWarning leak, and hop-chain zombie
  transports all trace to lifecycle-by-GC.
- **Explicitly NOT here: the Typer/Click triage.** See the decision record below.

### 2. Naming sweep  *(review decision #1)*

Mechanical and broad. Lands *after* #1 — so the big rename mops up the freshly-reworked
entry point rather than getting rebased over it — and *before* pydantic, so models are
authored in their final names.

- Normalize the Python API + module filenames (`embeddedHost.py` → `embedded_host.py`, etc.)
  and the JSON/lab-data schema field names (`osType` → `os_type`, `neId` → `ne_id`) to
  snake_case. **Keep** the telecom vocabulary (`ne` / `ne_id` conceptually).
- **Why it gates the freeze:** every renamed symbol and JSON field is frozen surface;
  renaming after the freeze is a breaking change. Zero users + no-backcompat policy makes
  now the only cheap moment.

### 3. Pydantic Phase A — boundary only  *(review decision #2, Phase A)*

Locks the data-boundary *shapes* that the contract freezes: lab JSON, `.otto/settings.toml`,
`RepoOptions`, the suite/instruction `Options`, and the monitor/coverage records.

- Gains: `extra='forbid'` (typo'd config fields error with suggestions instead of being
  silently dropped — replaces the hand-rolled merge in `storage/factory.py`), typed coercion
  with real error locations, **JSON Schema export** for editor autocomplete, and
  `pydantic-settings` for `OTTO_*` env vars. `pydantic-core` is already on the air-gap wheel
  list.
- **Absorbs three backlog items** rather than building them twice:
  [host-default-options.md](../../../todo/host-default-options.md),
  [metric-point-dataclass.md](../../../todo/metric-point-dataclass.md), and — critically —
  [multi_interface_hosts.md](../../../todo/multi_interface_hosts.md), which **changes the
  meaning of the `ip` field** and is therefore itself a pre-freeze schema change.
- **Hosts the scope-resolving spike** (see below).

### 4. Registry public API  *(review decision #4 — public-API half only)*

The extension-point contract. A *hard requirement* (custom transfer/term backends) is
currently unmet because of closed `Literal` sets, so the public API must be frozen.

- Open `TermType` (`ssh`/`telnet`), `FileTransferType` (`scp`/`sftp`/`ftp`/`nc`), and the
  embedded `console`/`tftp` set to the same string-registry idiom used for host classes;
  otto's built-ins become first-party registry entries, users register more
  (`register_transfer_backend('xmodem', cls)`). Bare callables demoted to a code-only
  convenience; the config-facing selector is always a string.
- **Why it gates the freeze:** the `register_*` functions and the config-facing selector
  strings are frozen surface.
- **The 1,443-line `transfer.py` file split is the *internal* half — it ships after the
  freeze.** Only the public API is on the critical path.

### → FREEZE

---

## Deferred to post-freeze (semver-minor, internal — no contract impact)

- **`transfer.py` per-backend file split** — the internal half of #4.
- **Test-tree restructure** (review decision #5): unit/integration/e2e, consolidated
  `tests/_fixtures/`. Touches zero public surface. May run in parallel earlier if capacity
  exists, but it never blocks the freeze.
- **Pydantic Phase B** (review decision #2, Phase B): pydantic `Options` models + the
  pydantic → Typer bridge + the **Typer 0.26 triage**, folded together. Conditional — see
  the hinge below.

---

## The Phase A spike (dual purpose)

Run a short, scoped spike *during* Phase A (it is pydantic-adjacent) to resolve the one
question that decides Phase B's placement and to read its scope:

1. **Compatibility hinge:** can `RepoOptions` / `Options` move from stdlib dataclass to a
   pydantic type **without breaking subclassers** (users write `class Options(RepoOptions): ...`)?
   - If **yes** (e.g. `pydantic.dataclasses.dataclass` as a near drop-in) → Phase B stays
     fully post-freeze. Shortest path holds.
   - If **no** → only the *base-class shape decision* is pulled before the freeze (freeze
     `Options` as pydantic from the start); the bridge mechanics + Typer triage can still
     trail post-freeze.
2. **Typer scope read:** reproduce the failing Typer 0.26 bump
   ([ludachrish3/otto-sh#47](https://github.com/ludachrish3/otto-sh/pull/47)) far enough to
   confirm the break lands in the **option-expansion / signature-introspection layer**
   (`options_params` in [src/otto/params.py](../../../src/otto/params.py),
   `_wrap_with_options` in [src/otto/cli/run.py:116-166](../../../src/otto/cli/run.py),
   `register_suite` in [src/otto/suite/register.py:111-131](../../../src/otto/suite/register.py)),
   not the state layer. This confirms Phase B can absorb it.

The spike informs, it does not implement. Default assumption pending its result: Phase B is
fully post-freeze.

**Result (2026-06-16) — RESOLVED, default confirmed.** Both probes ran (report:
[pydantic-phase-a-7-spike-report.md](pydantic-phase-a-7-spike-report.md)). (1) Hinge =
**yes, drop-in**: `pydantic.dataclasses.dataclass` keeps otto's stdlib-dataclass contract
(`is_dataclass` / `dataclasses.fields` / `options_params()` unchanged), so the `Options`
move is backward-compatible and opt-in — no base-class decision is pulled pre-freeze.
(2) Typer 0.26 break is in the **option / CLI-wiring layer**, not the state layer:
under `typer 0.26.7` / `click 8.3.1` the load-bearing cluster (18 failures) is parent-runner
option forwarding via the Typer `Context.obj` handoff in `cli/test.py` — while the
synthesized-signature machinery (`options_params` / `_wrap_with_options` / `register_suite`)
**still works** under 0.26 (so the precise fragility is `ctx.obj` propagation, not the
parameter expansion). **Phase B stays fully post-freeze** and absorbs the Typer triage.

---

## Decision record: Typer/Click triage moves out of item #1

The review's draft sequence bundled the Typer/Click triage with the context object, on the
grounds that "the CLI entry point is being reworked anyway." That is **proximity, not
coupling**, and tracing the code shows the two live in different layers:

- **The context object reworks *state ownership*** — the mutable globals
  ([host.py:40-65](../../../src/otto/host/host.py),
  [configmodule.py:86](../../../src/otto/configmodule/configmodule.py)). These are plain
  module globals with **zero Typer involvement**.
- **The Typer 0.26 break lives in the *option-expansion / signature-introspection*
  machinery** — `options_params` building `inspect.Parameter` lists from
  `Annotated[T, typer.Option(...)]`, and the synthetic `__signature__` that Typer
  introspects ([params.py](../../../src/otto/params.py),
  [run.py:116-166](../../../src/otto/cli/run.py),
  [register.py:111-131](../../../src/otto/suite/register.py)). The async wrapper
  ([utils.py:70](../../../src/otto/utils.py) `async_typer_command`) is a trivial
  `asyncio.run(...)` and low-risk.
- **Pydantic Phase B is defined as exactly the rewrite of that option-expansion layer**
  ("Options classes become pydantic models with a pydantic → Typer bridge"). Doing the Typer
  triage at item #1 would harden code that Phase B then tears out — double work.

Decisive point: the **context object gates the freeze; the Typer upgrade gates nothing.**
The `typer>=0.24.0` floor works today; only the *newer* release breaks. Bundling a
non-blocking, potentially-deep Typer rabbit hole into the one workstream that gates the
freeze is pure schedule risk.

**Resolution:** the Typer triage moves to Phase B (post-freeze by default). Add an explicit
`typer<0.26` ceiling to `pyproject.toml` as a one-line hygiene step during item #1 — it
records the deferral in code and stops Dependabot re-proposing the broken bump.

*Honesty note:* the layer attribution above is inferred from the coupling surface, not from
a reproduced failure. The sequencing conclusion holds regardless (the state globals have no
Typer surface at all); the Phase A spike confirms the Phase B scope.

---

## Out of scope for this sequencing

- **Reservations** (review decision #6): stays server-less; no coordinator service. No
  sequencing impact.
- **Feature gaps** (`otto init`, power/reset control, secrets indirection, host groups,
  JSON export, cross-suite parallelism): post-freeze product work, sequenced separately.
- **Deferred backlog** (`phase3_mixed_chains`, `udp_hop_forwarding`, `topology_plan`,
  `branch_to_target`): gated on a concrete lab need, per the outcome doc's triage.

---

## Next steps

Each critical-path item becomes its own brainstorm → spec → plan cycle, taken in order:

1. Context object + lifecycle  *(+ `typer<0.26` ceiling)*
2. Naming sweep
3. Pydantic Phase A boundary  *(+ the scope-resolving spike)*
4. Registry public API
5. **Freeze.** Then: transfer.py split, test-tree restructure, Pydantic Phase B (+ Typer triage).
