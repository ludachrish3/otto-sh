# Fable Architecture Review — Findings & Decisions

> Captured 2026-06-12. Pairs with [todo/fable_review.md](fable_review.md) (the
> request). This file records the review findings, the decisions reached in
> discussion, and the open items, so the work can be picked up later. No code
> has been changed yet. Per `AGENTS.md`, migration *cost* was deliberately
> ignored when forming recommendations — every item has a clean incremental
> path.

---

## TL;DR

otto occupies a genuinely unoccupied niche (async multi-host orchestration +
first-class embedded console targets + pytest regression suites + air-gap
operation). No existing tool replaces it. The engineering hygiene is well past
typical alpha. The work ahead is about **interface consistency and boundaries
before the contract freeze** ([migration_plan.md](migration_plan.md) Phase 1),
not rot. Six direction-setting decisions were made; they are the spine of the
follow-up work.

---

## Build vs. buy (settled: build)

- **Robot Framework** — keyword DSL is an abstraction tax for an audience of
  embedded/Linux C engineers fluent in Python. Replaces only the suite
  runner/reporting, not otto's core. Not a replacement.
- **labgrid** (Pengutronix) — closest overlap on the embedded half
  (coordinator-based board reservation, driver/strategy taxonomy incl.
  **power/bootloader control**, pytest integration). Sync-Python; no async
  multi-host orchestration, no hop chains, no monitor. **Not a replacement, but
  its power-control/bootloader strategy layer is the idea most worth stealing.**
- **LAVA** — shared-farm scale, heavy server/worker infra, painful air-gapped.
  Different niche.
- Ansible / Fabric / nornir / testinfra / pytest-embedded / Boardfarm / Zephyr
  twister — each covers one slice; none combine the four otto pillars.

The real risk is the **maintenance surface** of owning transfer protocols +
monitor UI + coverage tooling + a test framework. That argues for keeping every
subsystem behind the pluggable seams already started.

---

## Decisions locked in

### 1. Full naming sweep — keep telecom vocabulary

- Normalize the **Python API to PEP 8 snake_case**, rename module files
  (`embeddedHost.py` → `embedded_host.py`, `unixHost.py` → `unix_host.py`, etc.),
  **and** normalize the **JSON/lab-data schema field names** (`osType` →
  `os_type`, etc.).
- **Keep** the telecom vocabulary: `ne` / `neId` stay (conceptually; `neId`
  spelling follows the snake_case rule → `ne_id`).
- Rationale: zero users + explicit no-backcompat policy = this is the cheap
  moment. After the contract freeze it becomes effectively impossible.

### 2. Pydantic — two-phase (boundary first, then all-in)

- **Phase A — boundary only:** lab JSON, `.otto/settings.toml`, monitor/coverage
  records. Gains, nearly free: `extra='forbid'` (typo'd config fields become
  errors with field-name suggestions instead of being silently dropped), typed
  coercion with real error locations, **JSON Schema export** (editor
  autocomplete/validation for `hosts.json` + generated schema docs),
  `pydantic-settings` for `OTTO_*` env vars. `pydantic-core` is already on the
  air-gap wheel list.
- **Phase B — all-in:** suite/instruction `Options` classes become pydantic
  models with a **pydantic → Typer bridge**, so user-facing CLI options also get
  validation/typing for free. Internal value objects (e.g. the proposed
  `MetricPoint`) become models too.
- The open `osType`/`os_type` registry composes fine: validate common fields
  with a base model, hand the validated dict to the registered class.

### 3. Explicit context object (replace mutable global singletons)

- Move `getConfigModule()` / `all_hosts()` and the mutable module state
  (`_logCommandOutput`, `_globalDryRun`) to an **explicit context object owned by
  the CLI entry point**, scoped downward (constructor-passed or contextvar).
  Keep the module-level accessors as **thin shims** for instruction/suite
  ergonomics.
- Write-once-at-import **registries are fine and stay** — only the *mutable*
  singletons are the problem.
- This is what the migration contract should freeze around, so it gates the
  freeze.
- **Coupled investigation — Typer/Click upgrade:** the newer Typer (which
  vendors Click) breaks otto. Pin/triage this as part of the context-object
  work, since the CLI entry point is being reworked anyway.
  - **Dependabot PR:** [ludachrish3/otto-sh#47](https://github.com/ludachrish3/otto-sh/pull/47)
    — `chore(deps): bump typer from 0.25.1 to 0.26.7` (opened 2026-06-06,
    **still OPEN**; touches only `uv.lock`).
  - **Failure signal:** the bump fails CI on **all five test-matrix Pythons
    (3.10–3.14) and `typecheck`**; `docs` passes. So the breakage is
    behavioral/typing, not a doc/import-surface issue.
  - **Current pin:** `typer>=0.24.0` in
    [pyproject.toml](pyproject.toml) — the floor is fine; the problem is the
    newer release's vendored-Click behavior, so a triage (not a floor bump) is
    what's needed.
  - **Why it belongs here:** otto's CLI leans on Typer/Click internals (async
    command wrapping in [src/otto/cli/run.py](src/otto/cli/run.py), the
    suite/instruction option-expansion in
    [src/otto/suite/register.py](src/otto/suite/register.py) and
    [src/otto/params.py](src/otto/params.py), plus completion plumbing). Typer
    0.26 vendoring Click is exactly the kind of internal-coupling break the
    context-object/CLI rework should absorb in one pass — diagnose the failing
    tests, decide pin-vs-adapt, and land it with the entry-point changes rather
    than as a standalone scramble.

### 4. Unify on string registries everywhere

- Every extension point uses the **same string-registry idiom** as host classes
  / command frames / os_profiles. This includes opening up the currently-closed
  `Literal` sets:
  - `TermType = Literal['ssh','telnet']` ([src/otto/host/connections.py](src/otto/host/connections.py))
  - `FileTransferType = Literal['scp','sftp','ftp','nc']` ([src/otto/host/transfer.py](src/otto/host/transfer.py))
  - embedded `Literal['console','tftp']`
- **otto's built-ins become first-party registry entries; users register
  additional ones** (`register_transfer_backend('xmodem', cls)`).
- **Bare callables demoted to a code-only convenience.** The config-facing
  selector is always a string; the registry maps name → callable-or-class. The
  *only* thing given up is anonymous inline callables in declarative config
  (e.g. `SshOptions.post_connect`, which is **silently dropped from JSON today**
  at [src/otto/storage/factory.py](src/otto/storage/factory.py)) — and those
  were never serializable anyway. A callable can still be registered under a
  name to keep closure/custom-code power.
- Trade-off accepted: lose `Literal` exhaustiveness checks; runtime validation
  must list registered names in errors (the os_profile registry already models
  this well).
- Side benefit: this is the refactor that **splits the 1,443-line
  `transfer.py`** into per-backend modules.

### 5. Test tree split: unit / integration / e2e — names must not lie

- Restructure directories to **unit / integration / e2e** and stop maintaining
  two competing taxonomies. Today `tests/unit/` contains
  `test_hop_integration.py`, `test_interact_e2e.py`,
  `test_session_stability_integration.py` — the real axes are "needs VMs?"
  (markers `integration`/`embedded`/`hops`) × tier (fast/`concurrency`/
  `stability`), which the current `unit`/`integration` split encodes in neither
  directories nor filenames.
- Rename misleading files to match their tier.
- Secondary: consolidate the scattered conftests + fixture repos
  (`repo1/2/3`, `custom_hosts`, `lab_data`, `mockrepo.py`) under a discoverable
  `tests/_fixtures/`.

### 6. Reservations — stay server-less (JSON/DB protocol check only)

- Keep the **read-only JSON/database reservation backend behind the protocol**.
  otto does **not** grow a coordinator service.
- The team will rely on an **existing external resource scheduler server**.
- otto stays server-less except for the **optional** live-monitoring dashboard.
- (Implication: the labgrid-style claim/release coordinator idea is explicitly
  out of scope.)

---

## Findings detail (condensed)

### Hard-requirements audit

- **Not met — custom protocols blocked:** closed `Literal` sets for
  term/transfer mean a user repo can ship a custom *host class* but not a custom
  *transfer/term backend* without forking `transfer.py`. → fixed by decision #4.
- **Not met — interface consistency:** string-vs-callable split
  (`post_connect`); return-type inconsistency (`run()`→`RunResult`,
  `oneshot()`→`CommandStatus`, `get`/`put`→`tuple[Status, str]`); JSON schema
  mixes top-level scalars with nested `*_options` tables, and `snmp` breaks the
  `*_options` naming convention. → addressed by #1, #2, #4.
- **Met:** Python 3.10+, air-gap, dynamic instruction/suite help, per-host +
  per-class behavior hooks (with `[host_defaults]` from
  [host-default-options.md](host-default-options.md) filling the global gap).

### Five structural findings

1. **Config/data boundary is hand-rolled** — ~380-line `storage/factory.py`
   does manual three-layer merge + `__slots__`-walking field filters +
   redundant `validate_host_dict`. Unknown keys silently dropped. → pydantic
   (#2).
2. **Naming is split-brained** — `getConfigModule`/`sutDir`/`osType` next to
   `register_host_class`/`open_session`. → sweep (#1).
3. **Global singletons fight the platform-migration goal** — libraries calling
   `all_hosts()` depend on hidden global state; the test suite already pays the
   isolation tax. → context object (#3).
4. **Lifecycle by GC** — no `__aenter__`/`__aexit__` on hosts; `__del__`
   fallback is the root of issue #53 (loop churn), the parked ResourceWarning
   leak, and hop-chain zombie transports. Add `async with host:` and a
   lab-level fan-out context, then **delete `__del__`**. *Clearest case of a bug
   class that was avoidable by design.*  (Not among the six questions but
   strongly recommended; sequence with #3.)
5. **Test tree names lie** — see #5.

### Hot spots (churn + co-change since 2025-09)

- Top churners: `session.py` (11 commits / 1,351 lines), `remoteHost.py` (9),
  `transfer.py` (8 / 1,443 lines), `connections.py` (7), `configmodule/repo.py`
  (7 / 803 lines), `cli/main.py` (7).
- Strongest co-change cluster: **`repo.py` ↔ `storage/factory.py` ↔
  `host/__init__.py` ↔ `remoteHost.py`** — the config-load→host-construction
  seam. Adding one host field today touches the dataclass, the factory merge,
  `validate_host_dict`, the docs schema table, and often `repo.py`. Pydantic
  (#2) collapses this.
- `session.py ↔ telnet.py ↔ connections.py` churn is mostly genuine async
  bug-fixing post command-frame split — leave it, keep watching.
- `transfer.py` churns alone and is the largest file — the registry refactor
  (#4) splits it per backend.

### Gotcha retrospective — what was avoidable by design

- **Avoidable (lifecycle-by-GC):** issue #53, async ResourceWarning leak,
  hop-chain zombie transports → scoped ownership (finding #4).
- **Avoidable (stringly-typed routing):** route-on-invariant lesson (fixed by
  class registry); remaining instance is the closed `Literal` sets → #4.
- **Not avoidable (domain physics):** Zephyr single-client console wedges, gcov
  stamp/ninja order-only deps, UART RX FIFO pacing, telnet ECHO negotiation —
  handled well and documented.
- **Partially avoidable (test-infra leaks):** loop reaper, inner pytest
  sessions, xdist contention — a single composition root (#3) reduces but won't
  eliminate.

### NFS scaling hazards (concrete)

1. **Monitor DB on NFS will break:** WAL + `flock`
   ([src/otto/monitor/collector.py](src/otto/monitor/collector.py) ~208–217).
   SQLite WAL requires shared-memory mmap on one machine and does not work on
   NFS; `flock` semantics vary. Mitigation: detect network FS → fall back to
   `journal_mode=DELETE` with a warning, or document "DB on local disk."
2. **Log-dir rotation scans / per-run artifact trees** slow on NFS at scale;
   bounded by the name-pattern guard, but add a stat-count budget.
3. Lab JSON / settings.toml are read-once — fine.

---

## todo/ backlog triage

**Do (roughly in order), folding into the decisions above:**

- [host-default-options.md](host-default-options.md) — decided & shaped; **fold
  into the pydantic boundary work (#2)** rather than building twice.
- [metric-point-dataclass.md](metric-point-dataclass.md) — yes; becomes a
  pydantic model under #2.
- [test-mock-boundary-rewrite.md](test-mock-boundary-rewrite.md) — principle is
  right (mock at I/O, not business logic); Phase 1 already paid off.
- [parser-core-count-via-parse-kwarg.md](parser-core-count-via-parse-kwarg.md) —
  small, clean; removes collector↔parser mutation coupling.
- [multi_interface_hosts.md](multi_interface_hosts.md) — do it **with** the
  schema rework (#1/#2), not after — it changes the meaning of the `ip` field.
- [ty_vs_pylance_eval.md](ty_vs_pylance_eval.md) — keep the rubric; low effort.

**Defer / gate on a concrete need:**

- [phase3_mixed_chains.md](phase3_mixed_chains.md) — telnet-hop tunneling is
  inherently fragile (the doc admits it); require a real lab that needs it.
- [udp_hop_forwarding.md](udp_hop_forwarding.md) — same gate (real target first).
- [topology_plan.md](topology_plan.md) — rides on schema decisions; sequence
  after #1/#2.
- [branch_to_target.md](branch_to_target.md) — correctly self-assessed as deep
  research for modest payoff.
- TODO.md aioftp→pyftpdlib note conflates roles — **pyftpdlib is a server**;
  confirm which role otto needs before swapping.

---

## CI / docs / speed quick hits

- **CI:** solid. Gaps: lint is opt-in with acknowledged debt (sweep rule-by-rule
  with `--fix` + ratchet, not a big bang); no VM tier runs automatically —
  consider a scheduled self-hosted runner on the dev VM later (`make nox`
  exists for it).
- **Docs:** good for *users*; thin for *contributors/architecture*. The best
  architecture writeup lives in the private wiki, not `docs/design/`. Port
  architecture/concepts/principles pages into the repo.
- **Startup speed:** not profiled (avoided mutating state). `completion_cache.py`
  (590 lines) hints import-time cost is already known. Add a
  `make profile-startup` target using the already-present `pyinstrument`.
  Pydantic-at-boundary slightly helps merge cost but adds ~50–80 ms import.

---

## Feature gaps vs. landscape (for later discussion, priority order)

1. **`otto init` scaffolding** — biggest single lever on the learning-curve
   goal (generate `.otto/settings.toml`, a lab JSON, one instruction, one suite,
   runnable).
2. **Power/reset control abstraction** for embedded hosts (PDU/relay/`usbreset`)
   — labgrid's core strength; table stakes for real board farms.
3. **Secrets indirection** in lab JSON (env-var/file interpolation) — minimum
   viable answer to the plaintext-creds gap.
4. **Host groups/labels** in lab data — run an instruction against a named group.
5. **Structured (JSON) result export** for CI dashboards.
6. Cross-suite parallelism — last; per-host asyncio fan-out covers the common
   case.

---

## Suggested sequencing (not yet a plan)

1. **#3 context object + Typer/Click triage** (gates the freeze; recover the
   cut-off reference first) — bundle with the **`async with` lifecycle / delete
   `__del__`** finding.
2. **#1 naming sweep** (cheap now; do before pydantic so models are authored
   with final names).
3. **#2 Phase A pydantic at the boundary** (absorbs host-default-options +
   multi-interface-hosts + metric-point).
4. **#4 string-registry unification** (splits `transfer.py`).
5. **#5 test-tree restructure** (can run in parallel with the above).
6. **#2 Phase B pydantic→Typer bridge** once the boundary is proven.

Each becomes its own brainstorm → spec → plan cycle when we pick it up.
