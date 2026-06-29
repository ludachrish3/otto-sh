# Import-budget guard: trim otto's startup footprint and lock it in

**Date:** 2026-06-29
**Status:** Design — awaiting review
**Related:** `todo/import-light-otto-init.md` — promoted into scope as **Part D** (sequenced follow-up)

## Problem

Running `otto` (e.g. `otto --help`) takes ~340–400 ms wall on the dev VM. A
bare `import otto` pulls **799 modules** into `sys.modules`, including three
heavy third-party stacks that have no business loading just to print a help
menu:

- **fastapi + uvicorn + starlette** (~84 ms) — a web server,
- **pytest** (~30 ms) — a test runner,
- **jinja2** (~8 ms) — an HTML templating engine.

Wall-clock time is a poor regression signal because it varies per host. We want
a **deterministic, host-independent** metric — the *number of modules imported*
(and *which* modules) — to (a) drive the reduction and (b) flag when bloat is
re-introduced.

## Goals

1. Remove the heavy third-party stacks (fastapi/uvicorn/starlette, pytest,
   jinja2) from otto's startup path so they load only when the feature that
   needs them is actually invoked.
2. Add a deterministic, host-independent **import-budget guard** that fails CI
   when bloat is re-introduced, expressed in module counts / module identity —
   never wall-clock time.
3. As a guard-protected follow-up (Part D), make `import otto` import-light via
   PEP 562 lazy re-exports, trimming the library surface.

## Non-goals

- No wall-clock *gate*. Wall-clock is host-dependent, so it never gates CI. It
  *is* used as a defined before/after validation step to confirm the
  deterministic reduction translated into real time saved (see "Time-based
  validation") — measured, not gated.
- No new runtime feature. This is startup hygiene + a test-time guard.

## Root-cause findings (measured, post-lint-refactor, 2026-06-29)

Measured by importing otto in a clean subprocess with `sys.argv` set per
surface and dumping `sorted(sys.modules)`:

| Surface | total modules | otto modules | heavies present |
| --- | --- | --- | --- |
| `import otto` (library) | 799 | 113 | fastapi, uvicorn, starlette, pytest, jinja2 |
| `otto --help` | 799 | 113 | fastapi, uvicorn, starlette, pytest, jinja2 |
| `otto run --help` | 584 | 78 | pytest |
| `otto host --help` | 586 | 80 | pytest |
| `otto reservation --help` | 584 | 78 | pytest |
| `otto docker --help` | 592 | 83 | pytest |
| `otto schema --help` | 584 | 78 | pytest |
| `otto monitor --help` | 748 | 86 | fastapi, uvicorn, starlette, pytest |
| `otto test --help` | 585 | 79 | pytest |
| `otto cov --help` | 617 | 90 | pytest, jinja2 |

Eager import sites (current line numbers):

- `src/otto/monitor/__init__.py:25` — `from .server import MonitorServer`;
  `server.py:22-24` imports `fastapi` (and pulls `uvicorn`/`starlette`). So any
  `import otto.monitor` drags in the whole web stack.
- `src/otto/suite/register.py:19` — `import pytest` at module top.
  `_SUITE_REGISTRY` (a plain dict) and the registration decorators live in this
  module, so anything that wants the registry imports pytest as a side effect.
- `src/otto/coverage/renderer/html_renderer.py:28` — `from jinja2 import ...`
  at module top.

Why **pytest is on every surface** (not just `test`): `configmodule/__init__`
does completion-cache refresh work at *import time* (slow path, lines ~100–106),
which reaches `suite.register` to read `_SUITE_REGISTRY`, and that import pulls
pytest. Because `configmodule` is on every CLI path, pytest rides along
everywhere.

Why **fastapi/jinja2 are confined** to `import otto` / `--help` / their owning
subcommand: the existing dispatch-aware subcommand registration
(`cli/main.py:426+`) imports only the invoked subcommand's module — but the
`otto --help` / bare-`otto` / `import otto` cases deliberately "load everything"
so help output is complete, which is when fastapi/jinja2 get pulled.

## Part A — Reduction

**Principle:** *No module on the `import otto` / `otto --help` path may import a
heavy third-party dependency (fastapi, uvicorn, starlette, pytest, jinja2) at
module top.* Defer it into the function/method that uses it, or cut the eager
package edge that pulls it onto the path.

Two tactics, chosen per case to minimize the registration-ordering risk noted in
`todo/import-light-otto-init.md`:

- **Defer into the function body** (preferred — lowest structural risk):
  - `suite/register.py` — move `import pytest` out of module top into the
    functions that actually call pytest. Importing `register.py` for
    `_SUITE_REGISTRY` / decorators must not load pytest.
  - `coverage/renderer/html_renderer.py` — move `from jinja2 import ...` into the
    render function.
- **Cut the eager package edge** (where deferral is awkward):
  - `monitor/__init__.py` — stop eager-importing `.server`. Expose
    `MonitorServer` via PEP 562 module `__getattr__` so
    `from otto.monitor import MonitorServer` still works but only loads fastapi
    on first attribute access. (Alternatively, defer the fastapi imports inside
    `server.py` into function bodies — but `server.py` likely uses FastAPI types
    at class/decorator scope, so cutting the `__init__` edge is cleaner.)

The exact minimal set of edges/deferrals is an implementation-plan concern; the
**success criterion is the denylist in Part B** — after reduction, the heavies
must be absent from the surfaces where they don't belong.

### Reduction success criteria (post-reduction expected state)

- `import otto` and `otto --help`: **none** of fastapi, uvicorn, starlette,
  pytest, jinja2 present.
- `otto run/host/reservation/docker/schema --help`: **none** of fastapi,
  pytest, jinja2 present.
- `otto monitor --help`: fastapi/uvicorn/starlette **allowed**; pytest/jinja2
  absent.
- `otto test --help`: pytest **allowed**; fastapi/jinja2 absent.
- `otto cov --help`: jinja2 **allowed**; fastapi/pytest absent.

Behaviour must be unchanged: `otto --help` still lists every subcommand with its
real help text; `otto monitor` still starts the dashboard; `otto test` still
runs suites; `otto cov` still renders HTML.

## Part B — The deterministic guard (three layers)

Each surface is measured in a **clean subprocess** (so pytest's own imports
never pollute the measurement) by setting `sys.argv`, importing otto (which runs
`_register_subcommands()` for that invocation), and capturing
`sorted(sys.modules)`. Three independent assertions per surface:

1. **Third-party denylist** *(semantic, near-zero churn)* — named heavy modules
   must be **absent** from the surfaces where they don't belong (per the table
   in Part A success criteria). This is the primary bloat tripwire and survives
   careless snapshot regeneration: even if someone blindly regenerates the
   golden snapshot, the denylist still fails.

2. **Per-surface module-count cap** *(catches slow accumulation of many small
   imports)* — `total_modules <= baseline + HEADROOM`, with `HEADROOM ≈ 15`.
   This is the headline "number of imports" number. Caps are derived from the
   post-reduction measured baselines and stored in the config table.

3. **Golden snapshot of `otto.*` modules only** *(precise + self-documenting)* —
   commit the sorted list of **otto-owned** modules (`otto` and `otto.*`) per
   surface. Any new otto submodule joining a startup path appears as a reviewable
   diff in the snapshot file.

### Why snapshot only `otto.*` (the key design decision)

A full-`sys.modules` snapshot would churn on every third-party/stdlib version
bump (pydantic internals, etc.) — noise unrelated to *our* bloat. Restricting
the golden snapshot to otto-owned modules makes it **stable across dependency
upgrades** while still catching otto-side bloat exactly. The third-party side is
governed by the denylist (layer 1) and the count cap (layer 2), so a newly
pulled-in heavy dep is still caught — by name (if denylisted) or by the cap
(total count rises). *(Confirmed with the user: otto-only snapshot, not full.)*

### Surfaces gated

`import otto` (library), `otto --help`, and per-subcommand `--help` for: `run`,
`host`, `reservation`, `docker`, `schema`, `monitor`, `test`, `cov`. Each
surface row in the config table declares its denylist, count cap, and golden
snapshot file.

## Part C — Tooling, location, gate integration

- **Measurement harness:** `scripts/import_budget.py` — a small, testable helper
  that, given an argv list, spawns a clean subprocess (`sys.executable -c ...`),
  sets `sys.argv`, imports otto, and emits `{count, modules, otto_modules}` as
  JSON. Reused by both the gate test and the regeneration target. The default
  path is pure-Python and needs no profiler (this is what the gate runs).
- **`--hyperfine` (opt-in, never default):** when passed, `import_budget.py`
  *additionally* shells out to `hyperfine` to benchmark each surface's command
  and prints the per-surface wall-clock stats (mean ± σ) alongside the
  deterministic module counts — a manual-profiling aid, not part of the gate. If
  `hyperfine` isn't found it prints an install hint (`make hyperfine`) and the
  deterministic measurement still runs. The flag uses the same surface/argv
  config table, so the benchmarked commands always match what's gated.
  `-X importtime` / pyinstrument / py-spy remain ad-hoc diagnosis aids, not wired
  into the script.
- **Config table:** a single declarative structure (in the test module or a
  small data file) mapping each surface → `{argv, denylist, cap, snapshot_file}`.
- **Golden snapshots:** committed text files, one per surface, under
  `tests/unit/import_budget/snapshots/<surface>.txt` (sorted otto module names,
  one per line).
- **Gate test:** `tests/unit/test_import_budget.py` — pure unit, no VM/bed,
  deterministic. Parametrized over surfaces; asserts denylist + cap + snapshot.
  Runs inside `make coverage` (per-task gate) and the full gate.
- **Regeneration:** `make import-snapshot` (wrapping
  `python scripts/import_budget.py --update`) regenerates all snapshot files and
  prints current counts, so an intentional change is one command and produces a
  reviewable diff. (A `pytest --update-import-snapshots` flag is an acceptable
  alternative; the Makefile target is the canonical entry point.)
- **hyperfine bootstrap (`make hyperfine`):** a pinned Makefile target that
  installs hyperfine as a dev tool so profiling is one command. hyperfine is a
  Rust binary with no legitimate PyPI distribution (the PyPI `hyperfine` is an
  unrelated physics library), so it can't live in `[dependency-groups]`; this
  target fills that gap, complementing the existing `py-spy` / `pyinstrument` dev
  profilers. Behaviour:
  - Pins `HYPERFINE_VERSION` (1.20.0 at time of writing).
  - Detects OS (linux/darwin) and arch (x86_64/aarch64) and maps to the GitHub
    release asset (linux-x86_64 → `*-x86_64-unknown-linux-musl`, linux-aarch64 →
    `*-aarch64-unknown-linux-gnu`, plus the two `*-apple-darwin` variants).
  - Downloads the tarball, verifies it against a pinned per-asset **sha256**, and
    installs the binary into `.venv/bin/hyperfine` (on PATH whenever the venv is
    active).
  - Idempotent: skips the download if the pinned version is already present.
  - Hooked into `make dev` so a standard environment setup includes it; re-runs
    restore it if the venv is recreated.

## Part D — import-light `otto/__init__.py` (follow-up phase)

Promoted from `todo/import-light-otto-init.md`. Sequenced *after* the guard
(Parts A–C) lands — the guard is what makes this safe to attempt.

**What it does:** convert `otto/__init__.py` from eager re-exports to PEP 562
lazy exports via module `__getattr__` + `__all__` (`app`, `options`,
`get_otto_logger`, `all_hosts`, `get_host`, `get_lab`, `run_on_all_hosts`,
`OttoContext`, `get_context`, `open_context`, `try_get_context`). Each resolves
its submodule on first attribute access. Entry point `otto:app` and
`from otto import X` keep working (attribute access triggers `__getattr__`).

**Which surface it helps — and which it does NOT:** this benefits the *library*
surface. A consumer doing `import otto` and touching only e.g. `OttoContext` no
longer drags in the whole Typer app + every subcommand. It does **not** speed up
`otto --help`: the CLI entry must import `cli.main` to run, so the help path
loads it regardless of how lazy `__init__` is. (The optimization that *does* move
`otto --help` further is **Part E** — static top-level help via a single-source
manifest.)

**The invariant this changes** (important): today a bare `import otto` eagerly
populates the host/backend/os-profile registries as a side effect of the eager
imports. After Part D, a bare `import otto` will deliberately *not* populate
them — they populate on first access of the public API that needs them. So the
Part A "registration-ordering smoke" (registries populated after bare
`import otto`) is **reframed** for Part D: the assertion becomes *accessing the
public API (`all_hosts()`, `get_host()`, building a host, the CLI) triggers
correct registration*, not *bare import does*. The plan must enumerate every
import-time self-registration on the deferred edges and add a smoke per public
entry path (CLI invocation + each library accessor).

**Acceptance is visible in the guard:** Part D sharply lowers the `import otto`
surface's otto-module count, so its golden snapshot + count cap are regenerated
(`make import-snapshot`) and the drop shows up as a reviewable diff — a built-in
demonstration that the guard works. The per-subcommand and `otto --help` surfaces
are essentially unchanged by Part D.

### Implemented — measured result

Wall-clock timing measured via `hyperfine` on the dev VM; module counts via the
import-budget harness (`scripts/import_budget.py`).

| Library usage | modules now → after | wall-clock |
| --- | --- | --- |
| bare `import otto` | 508 → 65 | ~152 → ~11 ms |
| `from otto import OttoContext` | 508 → 139 | ~152 → ~29 ms |
| `from otto import options` | 508 → 206 | (pydantic-bound) |
| `from otto import all_hosts` | 508 → 458 | modest (lab graph) |
| `import otto` + `.app` (CLI) | 508 → 508 | unchanged |

The import-budget guard's `import_otto` surface was re-baselined (snapshot
110 → 1 otto modules, cap 523 → 85); all CLI surfaces unchanged (the guard
measures import footprint via `otto.app` access).

**Part E (static-help `--help`) was dropped by decision** — it only saved
~45 modules / ~10 ms post-Phase-A; the SSOT-manifest complexity was not
worth the gain.

## Part E — static-help top-level `--help` (follow-up phase)

This is the optimization that actually cuts the **`otto --help`** surface (the
headline complaint): render the top-level help — the list of subcommands and
their one-line summaries — *without importing the 8 subcommand modules*. Today
`_requested_subcommands()` returns the full set for `otto --help`, so all 8 load
just to print their `help=` lines (799 modules; placeholder-only floor is ~583).

The only blocker was **help-text drift** (a static summary diverging from the
real subcommand). That is made **impossible by construction** with a
single-source-of-truth manifest, so this phase is in scope:

**Single-source-of-truth (SSOT) manifest — drift impossible by construction.**
A lightweight, heavy-dep-free module (e.g. `cli/_manifest.py`) holds one
constant per subcommand: `{name: short_help}`. Both consumers read the *same*
constant, so they cannot diverge:

- The top-level placeholder Typers (used now even for `otto --help`) take their
  `help=` from the manifest — no module import needed to render the listing.
- The real sub-app sets the first line of its own `help=` from the same manifest
  constant (its fuller `otto <sub> --help` body can extend below it).

Only the **one-line summary** shown at the top level needs to be static; the full
per-subcommand help still comes from the real module when you actually invoke
`otto <sub> --help`. So the static surface is tiny.

**Backstop drift guard (same philosophy as the rest of this spec).** A unit test
imports every subcommand module (import cost is irrelevant in a test) and asserts:
(1) `set(manifest) == set(_SUBCOMMAND_MODULES)` — every subcommand has exactly one
manifest entry, so a newly added subcommand *cannot* be merged without one; and
(2) each real sub-app's resolved short help equals its manifest entry. Divergence
is a hard CI failure, not a silent inconsistency. Between SSOT (structural) and
this guard (enforced), drift is impossible.

**Trade-off acknowledged:** the top-level summary is now sourced from the manifest
rather than read live from each module — a deliberate indirection. The SSOT +
guard is what makes that safe. Acceptance: `otto --help` output is byte-identical
to today's (verified in the behaviour smoke), but its guard snapshot/count drop
toward the ~583 floor, and the heavies (fastapi/jinja2) leave the `--help` path
entirely (they already leave via Part A; Part E also drops the non-heavy
subcommand-specific modules).

**Scope boundary — top-level only.** Part E makes the *top-level* listing static.
It does **not** make per-subcommand help (`otto <sub> --help`) static: rendering
a subcommand's real options/arguments/sub-commands requires importing that
subcommand's CLI-definition module (`cli/<sub>.py`), which is necessary for
accurate help. The *cost* of those paths is the heavy runtime deps they pull
(fastapi/pytest/jinja2), and that is removed by **Part A** (deferring e.g.
`cli/monitor.py`'s `from ..monitor.server import MonitorServer` into the
`start`/`serve` function bodies) — so `otto monitor --help` drops toward the
common floor without any static-subcommand machinery. Fully static subcommand
help is **explicitly not pursued**: (1) it is partly impossible — `otto host`
synthesizes per-class menus at runtime and `otto test`/`otto run` attach
per-suite/instruction stubs from the completion cache, neither statically
representable; (2) the drift surface would balloon to every option/type/default;
(3) otto's completion-cache `build_stub_command` already shows that subcommand
*structure* is best *cached* (auto-regenerated), not hand-maintained. For
`--help`, importing the Part-A-lightened real definitions is correct.

## Sequencing

1. **Reduce first** (Part A): defer/cut the heavy imports until the denylist
   success criteria hold when measured ad-hoc.
2. **Capture the trimmed baseline** (Part B): generate golden snapshots and set
   count caps from the *post-reduction* measured state, so we never freeze fastapi
   or pytest into the baseline.
3. **Wire the gate** (Part C): land the test + Makefile target.
4. **Phase D — import-light `otto/__init__.py`** (follow-up, see Part D): only
   *after* the guard from steps 1–3 is merged and proven, because the guard is
   the safety net that catches the registration-ordering regressions this phase
   risks. Implemented and merged as its own step.
5. **Phase E — static-help top-level `--help`** (follow-up, see Part E): the
   manifest + drift-guard work that cuts the `otto --help` surface toward the
   ~583-module floor. Independent of Part D; sequence after the guard. Its own
   step. D and E can land in either order.

## Verification

- Ad-hoc measurement script confirms the post-reduction expected state (denylist
  table above) before snapshots are captured.
- `make coverage` green (the new test runs here; coverage floor maintained).
- `make typecheck` clean (`scripts/import_budget.py` typed).
- `make docs` 0 warnings (no new public API needing docs, but verify the
  drift/doctest gates stay green).
- Behaviour smokes: `otto --help` lists all subcommands with real help;
  `otto monitor`, `otto test`, `otto cov` still function.
- Registration-ordering smoke: after a bare `import otto`, host/backend/os-profile
  registries are populated (cutting the monitor/suite edges must not leave a
  registry empty). Add an explicit assertion if any registry self-registers via
  the cut edge. *(This invariant is deliberately changed by Part D — see its
  "invariant this changes" note; the smoke is reframed there.)*

### Time-based validation (measured, not a CI gate)

A `hyperfine` before/after wall-clock comparison confirms the deterministic
module reduction translated into real time saved. This is run by hand and
recorded here; it is **not** wired into CI (wall-clock isn't host-portable).
hyperfine is installed via the `make hyperfine` bootstrap target (Part C) into
`.venv/bin/`; it is a bootstrapped dev tool, not a `pyproject.toml` dependency.
(The baseline below was captured with a hand-installed v1.20.0 binary before the
bootstrap target existed; re-runs use the bootstrapped one.)

Exact command (re-run identically post-reduction):

```
hyperfine --warmup 5 --min-runs 30 --shell=none \
  -n python-baseline ".venv/bin/python -c pass" \
  -n import-otto      ".venv/bin/python -c 'import otto'" \
  -n otto--help       ".venv/bin/otto --help"
```

**BEFORE baseline** (dev VM, 2026-06-29, pre-reduction):

| Command | mean ± σ |
| --- | --- |
| `python -c pass` (bare interpreter) | 8.3 ± 0.4 ms |
| `python -c "import otto"` | 291.9 ± 3.6 ms |
| `otto --help` | 334.2 ± 3.8 ms |

Derived: otto's marginal import cost ≈ **284 ms** above bare Python; help/Typer
rendering adds ≈ **42 ms**. The targeted heavies (fastapi ~84 ms + pytest ~30 ms
+ jinja2 ~8 ms ≈ **122 ms**) are the attributable, removable chunk. Acceptance:
post-reduction `otto --help` and `import otto` drop by a wall-clock margin
consistent with the module-count drop (the deterministic guard is the gate; this
number is the sanity check). Record the AFTER table beside this one when the
reduction lands.

**AFTER baseline** (dev VM, 2026-06-29, post-reduction — Parts A+B+C landed):

| Command | mean ± σ |
| --- | --- |
| `python -c pass` (bare interpreter) | 8.5 ± 0.6 ms |
| `python -c "import otto"` | 152.3 ± 2.7 ms |
| `otto --help` | 199.0 ± 3.6 ms |

Module count dropped from **799 → 508** (`import otto` surface; fastapi/uvicorn/
starlette/pytest/jinja2 deferred). Wall-clock: `import otto` −**140 ms** (52%
faster); `otto --help` −**135 ms** (40% faster) — consistent with the ~122 ms
heavy-stack estimate.

## Risks & mitigations

- **Registration ordering** — deferring/cutting imports could leave a registry
  empty when queried. *Mitigation:* enumerate what self-registers at import of
  the cut modules; add the registration smoke above; verify both the CLI and
  library entry paths populate registries.
- **Snapshot churn from otto refactors** — renaming/adding otto modules dirties
  snapshots. *Mitigation:* that is the intended signal; `make import-snapshot`
  makes regeneration one command, and the diff is reviewed.
- **Subprocess measurement flakiness** — `__pycache__` warmup or env differences.
  *Mitigation:* measure module *identity/count*, which is insensitive to caching;
  run each surface in its own fresh subprocess; pin `sys.argv` explicitly.
- **fastapi at class/decorator scope in `server.py`** — may resist
  function-body deferral. *Mitigation:* prefer cutting the `monitor/__init__`
  edge (PEP 562) over deferring inside `server.py`.

## Out of scope

- Reducing pydantic/pydantic_core cost (~30 ms) — largely unavoidable given
  `@options` dataclasses; not a heavy-feature dependency, left alone.

(Two items that previously sat here are now sequenced follow-up phases: the PEP
562 lazification of `otto/__init__.py` is **Part D**, and the static-help
top-level `--help` is **Part E** — promoted once we established its drift risk is
eliminable via a single-source-of-truth manifest + drift-guard test.)
