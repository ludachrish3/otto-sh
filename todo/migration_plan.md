# Migration TODO — Old Monolith → otto as the Platform

> Working notes / task list for migrating the in-house monolith to otto.
> Decisions below were settled in planning; see "Key decisions" at the bottom
> for the reasoning behind each. Documentation generation and execution will be
> picked up later off this file.

## The shape of the migration (one paragraph)

otto becomes the *platform* — the CLI orchestrator and the base library
dependency. The old monolith (one CLI + a large web of interdependent libraries)
is retired underneath it. otto already replaces the old CLI and most old core
libraries; the real concern is a set of **valuable domain libraries that can't
be folded into otto** and must become **dual-platform** (run on the old tools
*and* on otto) so the old platform can be removed without rewriting them again.
The mechanism is **dependency inversion**: the valuable libraries port once onto
otto's public interfaces (the "contract"); the *old platform* gets additive
adapters that masquerade as otto so those libraries keep running there during
the bridge period; the adapters are deleted when the old platform retires.

## Phase 0 — now → contract freeze (~months out)

otto's interface is still freely changing, and that is fine. Do **not** start
bulk library ports yet. Only do work that tolerates or *informs* otto's churn:

- [ ] Draft the **migration-stable contract** (target list, not yet frozen):
  - [ ] Host: `Host` protocol + `RunResult` / `CommandStatus` / `Status` /
        `ShellCommand` / `Expect` / `HostSession`.
  - [ ] Logging: plain stdlib `logging.Logger` (libraries call
        `logging.getLogger(__name__)`; otto's logger is a `Logger` subclass).
  - [ ] Config: the **decomposed** pieces — `get_lab()` / `get_host()` /
        `all_hosts()` / `Lab` for lab+host data (the old `getConfigModule()`
        bundle was removed in WS#1); `getRepos()` for repos (import-time
        environment, not per-invocation); a project-owned `RepoOptions`
        dataclass for repo-wide options; the suite/instruction `Options`
        dataclass for per-invocation CLI values. There is **no single config
        object** in the contract. The per-invocation runtime is `OttoContext`
        (lab + runtime flags + host-lifecycle scope), propagated via a
        contextvar and obtainable explicitly via `get_context()` /
        `open_context()`; **reservations are a CLI-layer concern carried in
        Typer `ctx.meta`, not part of the runtime-context contract.**
- [ ] Prototype the old-platform adapters (additive code in the old tools' repo):
  - [ ] `OldHostAsOttoHost` — wraps the old sync host, implements otto's async
        `Host` protocol via `asyncio.to_thread`.
  - [ ] Logger routing — old platform installs a stdlib `logging` handler so
        library `getLogger` output reaches its sinks; add an
        `install_otto_bridge()` helper for mixed processes.
  - [ ] Config decomposition adapter — **not** a 1:1 wrapper; fans the old
        god-object configmodule out into otto shapes (`Lab` / host registry,
        the project `RepoOptions`, CLI values). Per-project mapping.
- [ ] Pilot **one** representative leaf library end-to-end. Treat pilot friction
      as a signal to fix **otto's interface**, not the library — this is the
      point of piloting before the freeze.
- [ ] Build the codemod (`tools/codemod/`, `libcst`-based) for the mechanical
      parts of host porting: import swaps, type annotations, `await` insertion.
      (Config porting is **not** a codemod target — see Phase 1.)

## Phase 1 — contract freeze milestone

Reached when otto is genuinely ready to be evaluated as a replacement.

- [ ] Publish the migration-stable surface as an explicit module + name list in
      `docs/` (the existing `__all__` lists are the starting point).
- [ ] Adopt semver discipline for that surface (breaking changes gate a minor
      bump; everything else stays free to churn).
- [ ] Update `AGENTS.md` to exclude that surface from "no backwards
      compatibility." (Not before this milestone.)
- [ ] Add a CI snapshot test of the public API of `otto.host` / `otto.logger` /
      `otto.configmodule`.

## Phase 2 — bulk library porting (post-freeze)

Order = the dependency graph: leaf libraries first, then their dependents, then
the suites/instructions that consume them.

Per valuable library, make it dual-platform:

- [ ] **Host usage** — annotate against otto's `Host` protocol; ensure only
      contract methods are called; host-touching functions become `async`.
  - Injected libraries: type-change only.
  - Directly-importing libraries: convert to injection where feasible
    (preferred), else route through an old-tools `bridge` module.
- [ ] **Logger usage** — switch to `logging.getLogger(__name__)`.
- [ ] **Config usage (the thorniest)** — classify *each* old-configmodule access
      and re-home it:
  - lab/host data → `get_host()` / `all_hosts()` or an injected `Host`;
  - repo-wide option → injected `RepoOptions` (or the field);
  - CLI argument → threaded down from the suite/instruction `Options` dataclass;
  - maps to nothing → flag as dead config or a real otto modelling gap.
  - This needs human judgement per access site; not codemoddable.
- [ ] **Dual-platform conformance test** — run the library's own test suite
      twice: once with `OldHostAsOttoHost` adapters injected, once with native
      otto hosts. Both must pass before declaring the library dual-platform.

## Phase 3 — suites, instructions, teardown

- [ ] Rewrite each suite to `OttoSuite` + `@register_suite`; each instruction to
      an `@instruction` async function. (No legacy suite runner in otto.)
      Rewrite a suite only once all libraries it depends on are dual-platform.
- [ ] New projects: otto from day one, no exceptions.
- [ ] New features land only in otto; old platform gets fixes only (pull-based
      carrot — avoids a mandate).
- [ ] Track migration state per library / per suite / per project; quarterly
      review; flag anything stuck mid-state > 6 months.
- [ ] Once no consumer imports the old CLI: delete the old CLI, the old core
      libraries otto replaced, and all old-platform adapters. The valuable
      libraries remain — now otto-native.
- [ ] Defer setting a hard sunset date for the old platform until adoption
      metrics make it credible.

## Open items to resolve before/while executing

- [ ] **Config fan-out mapping** — work out the old god-object → otto-decomposed
      mapping on one representative project; surface any genuine gaps in otto's
      config model.
- [ ] **A2a vs A2b split** — audit 2–3 directly-importing libraries to size how
      many convert cleanly to injection vs. need the bridge module.

## Key decisions (the "why")

- **otto is the platform**, not a set of parts to cherry-pick. Old suites,
  instructions, and libraries adopt otto as the framework they run in.
- **Compat/adapter code lives in the old tools** (additive only — their
  internals can't be refactored), **never in otto**. otto stays clean.
- **Dependency inversion onto otto's interfaces** is the model: libraries port
  once toward the end state; the old platform is adapted *up* to otto.
- **Async**: otto's `Host` is async; porting makes host-touching functions
  async. async-wrapping-sync (in the adapter) is trivial; sync-calling-async
  (old suites calling migrated libraries) is handled by an `asyncio.run`
  boundary — confirmed acceptable.
- **Config is the thorniest concern**: the old configmodule is a god-object
  (lab data + CLI args + repo options + more) with **no 1:1 mapping** to otto's
  decomposed model. Library bodies read it directly. Porting config = classify
  and re-home each access; cannot be codemodded.
- **Suites/instructions are rewritten** to otto's decorators — otto will not
  carry a legacy suite runner.
- **The contract freeze is a future gate**, months out. otto's interface should
  keep evolving until then; AGENTS.md's "no backcompat" stance stays for now.
  Bulk library porting is gated on the freeze.
- **A few diamond-dependency libraries will stay dual-platform for a very long
  time** — acceptable; that is exactly what the contract is designed for.
- **Contract maintainer** is named and will train others — no rot risk.
