# WS#1 — Context Object + Host Lifecycle (Design Spec)

> Captured 2026-06-13. Workstream #1 of the freeze critical path defined in
> [2026-06-13-fable-review-sequencing-design.md](2026-06-13-fable-review-sequencing-design.md).
> Implements review decision #3 (explicit context object replacing mutable global
> singletons) bundled with structural finding #4 (lifecycle-by-GC → scoped ownership,
> delete `__del__`). This is the freeze-gating foundation: it defines the runtime
> composition root and the host-lifecycle contract the migration freezes around.

---

## Goal

Replace otto's mutable module-level singletons and GC-based host teardown with an
explicit, contextvar-propagated **runtime composition root** (`OttoContext`) and a
**deterministic host lifecycle** (`async with` + an entry-point scope), so that:

- the config-access shape is stable enough to freeze;
- the live bug class rooted in `__del__` (issue #53 loop churn, the parked unclosed-loop
  `ResourceWarning` leak, hop-chain zombie transports) is retired by design;
- otto is usable as a plain async library in scripts, not only behind its Typer CLI.

### Non-goals (explicitly deferred)

- The mechanical camelCase→snake_case sweep of existing accessor names → **WS#2**.
- Making any config object a pydantic model → **WS#3 (Phase A)**.
- The Typer 0.26 upgrade / option-expansion rework → **Phase B (post-freeze)**.
- Splitting `transfer.py` → post-freeze.
- Rewriting the diagnostic loop tracker / reaper / leak detector beyond removing
  now-dead `__del__`-coping scaffolding.

---

## 1. `OttoContext` — the runtime composition root

New module `src/otto/context.py` (it spans config + runtime flags + lifecycle, so it does
not belong under `configmodule/`).

```python
@dataclass
class OttoContext:
    lab: Lab                                  # the active lab (chosen via --lab) — config DATA
    dry_run: bool = False                     # was host.py::_globalDryRun
    log_command_output: bool = True           # was host.py::_logCommandOutput
    scope: HostScope = field(default_factory=HostScope)   # lifecycle backstop (§3)

    # Canonical lookup surface (the explicit / DI path). Each method registers
    # the host(s) it returns into self.scope (§3) and resolves against self.lab.
    def get_host(self, host_id: str, **overrides) -> "UnixHost": ...
    def all_hosts(self, pattern=None, **overrides) -> "Iterator[RemoteHost]": ...
    async def do_for_all_hosts(self, method, *args, **kwargs): ...
    async def run_on_all_hosts(self, cmds, **kwargs): ...

_active: ContextVar[OttoContext | None] = ContextVar("otto_context", default=None)

def get_context() -> OttoContext:        # raises LookupError-style error if none active
def try_get_context() -> OttoContext | None:
def set_context(ctx: OttoContext) -> Token:    # thin wrapper over _active.set
def reset_context(token: Token) -> None:       # thin wrapper over _active.reset
```

**DI-friendly composition.** Explicit context-passing is first-class: `OttoContext` carries
the canonical lookup methods, `open_context()` (§5) yields a `ctx` callers pass around and
call methods on, and any component that wants its dependency visible takes a `ctx`. The
contextvar is the *plumbing* that lets the bare, zero-argument convenience accessors exist —
`get_host()` / `all_hosts()` / `run_on_all_hosts()` / `do_for_all_hosts()` delegate to
`get_context().<same method>`. (A zero-arg `all_hosts()` is irreducibly ambient; the
contextvar is just how it finds the active context.)

**Convention** (to prevent drift between the two paths): reusable/library code and tests
take an explicit `ctx` and call `ctx.all_hosts()`; CLI instructions/suites and one-off
scripts may use the bare accessors. The instruction/suite wrapper injects `ctx` into any
handler that *declares* a `ctx: OttoContext` parameter (§3, §8), so a handler author opts
into explicitness without threading anything manually.

**Propagation across the Typer boundary** is sound: the CLI callback builds the context and
`set_context()`s it synchronously; `asyncio.run` copies the current context into the new
loop, so it's visible to the command coroutine and every task it spawns.

**Graceful degradation:** the dry-run / log-output reader shims (`isDryRun()`,
`getLoggingCommandOutputEnabled()` — kept names, swept in WS#2) must read via
`try_get_context()` and **return safe defaults (`False` / `True`) when no context is
active** — deep host paths (e.g. [host.py:491](../../../src/otto/host/host.py)) and
library/test code must keep working without a CLI invocation, exactly as the module globals
do today.

---

## 2. Decompose `ConfigModule` (no `LabConfig` wrapper)

The current `ConfigModule` ([configmodule.py:40-65](../../../src/otto/configmodule/configmodule.py))
bundles three independently-accessed concerns. Evidence from field-access audit:

| `ConfigModule` field | Accessed by | Disposition |
|---|---|---|
| `lab: Lab` | everywhere (`cfg.lab.hosts`, …); the dominant field | → `OttoContext.lab` (per-invocation) |
| `repos: list[Repo]` | `dockerHost.py`, `logRepoCommits()` — **already served by standalone `getRepos()`** (built at import) | → **stays in `getRepos()`**, not on the context (import-time environment) |
| `reservation_backend` + `identity` + `skip_reservation_check` | **only** the reservation subsystem; always travel together | → `ReservationState` in Typer `ctx.meta` (§4) |

`ConfigModule` / the proposed `LabConfig` wrapper is therefore **not created** — it would be
a near-empty shell around `Lab` plus two unrelated concerns. Decomposition into context
fields is cleaner and honors migration_plan's "no single config object."

**Accessor changes (WS#1):**

- `getConfigModule()` / `tryGetConfigModule()` — **removed.** There is exactly one
  `getConfigModule().lab` call site; everywhere else assigns to a local then reads one
  field. Replaced by:
  - `get_lab() -> Lab` → `get_context().lab` (active lab).
  - `getRepos()` → **unchanged** (single repos accessor).
  - reservation state → via `ctx.meta` (§4), never via the runtime context.
  - `try_get_context()` covers the gate's "is anything set up?" check.
- `setConfigModule()` / `setDryRun()` / `_setLoggingCommandOutputEnabled()` — **removed**;
  superseded by `OttoContext` construction + `set_context()` (CLI) and the test fixture (§6).
- The existing loader `getLab()` ([lab.py:89](../../../src/otto/configmodule/lab.py)) is a
  *file loader*, not an accessor — **rename to `load_lab()`** to free the `get_lab()` name
  and clarify intent.
- `get_host()`, `all_hosts()`, `do_for_all_hosts()`, `run_on_all_hosts()` — names unchanged
  in WS#1 (snake-swept in WS#2), but reimplemented as **module-level delegations to the
  active context's methods** (`get_context().get_host(...)`), so registration/lookup logic
  lives in one place (the `OttoContext` methods). `getEnv()` / `getCompletionNames()`
  unchanged.

> Names like `get_lab` / `load_lab` are authored in near-final form but final casing is
> WS#2's call; the **structural** decision (decompose, drop the bundle accessor) is WS#1's.

---

## 3. Deterministic host lifecycle — delete `__del__`

### `HostScope` — the entry-point backstop

```python
class HostScope:
    def register(self, host: RemoteHost) -> None: ...   # dedup; idempotent
    async def __aenter__(self) -> "HostScope": return self
    async def __aexit__(self, *exc) -> None:
        await asyncio.gather(*(h.close() for h in self._hosts if h._connected),
                             return_exceptions=True)     # error-isolating
```

### Three first-class, optional usage modes (the file-descriptor model)

1. `async with get_host('x') as h:` — tight/early scoping (per-host `__aenter__`/`__aexit__`
   → `close()`).
2. `h = get_host('x')`; pass `h` around; **the lab scope closes it at command end** — no
   ceremony, no leak. This is the backstop that makes "no mandatory context manager" safe.
3. `await h.close()` — explicit manual control.

Hosts support all three; none is mandatory.

### Registration

The `OttoContext.get_host()` / `all_hosts()` **methods** register the host(s) they return
into `self.scope` (the bare module accessors inherit this via delegation, §2) — **including
the `dataclasses.replace` override-copies**, since those carry their own `ConnectionManager`.
A directly-constructed `UnixHost(...)` is **user-owned, like an explicitly-opened FD**:
closeable via mode 1/3, or opted into the scope via `ctx.scope.register(h)` (a public
`adopt()`-style helper). We chose accessor-registration (not auto-register-on-connection-open)
to match the FD model; if direct-construction leaks become a real problem, connection-open
self-registration is the documented escalation.

### Host `_lab` back-reference (remove the host layer's last ambient reach)

Hosts currently reach ambient config for hop resolution (`_build_hop_transport` →
`get_host`, [remoteHost.py:254](../../../src/otto/host/remoteHost.py)). The storage factory
wires each host a `_lab` reference once the lab is built (a non-comparing/non-repr field or
a post-construct attribute — confirm interaction with any `slots`), so hop resolution
becomes `self._lab.hosts[hop]` with **no contextvar access from inside a host**. The
`dataclasses.replace` override-copies must carry `_lab` forward. The resulting lab↔host
reference cycle is harmless precisely because WS#1 **deletes `__del__`** — cycles plus
finalizers are the problematic combination; plain reference cycles are collected normally.

### Teardown site

The scope is entered/exited inside `async_typer_command`'s wrapper
([utils.py:70](../../../src/otto/utils.py)), around the command coroutine but within the
single `asyncio.run`, so connected hosts close **before** the loop closes (this is what
removes the ResourceWarning/zombie-transport class):

```python
async def _run():
    ctx = try_get_context()
    if ctx is None:
        return await f(*a, **kw)
    async with ctx.scope:
        return await f(*a, **kw)
return asyncio.run(_run())
```

### Then

- Add `__aenter__` / `__aexit__` to the `Host` protocol + `RemoteHost` base (delegating to
  `close()`).
- **Delete `RemoteHost.__del__`** ([remoteHost.py:162](../../../src/otto/host/remoteHost.py)).
- Ensure `close()` is idempotent (a second close, or closing an unconnected host, is a
  no-op) so an early CM-close and the end-of-command sweep never collide.
- Remove the `_closing`-flag scaffolding that exists *only* to no-op `__del__`
  ([connections.py:376-404](../../../src/otto/host/connections.py),
  [transport.py:107](../../../src/otto/host/transport.py)) — keep genuine double-close
  guards. **This removal is evidence-driven**: only after the suite is green without it.

---

## 4. Reservation state → Typer `ctx.meta`

Reservation state is **gate-scoped, not command-scoped**: built once in the top-level
callback, consumed by `gate()` in the four live-lab subcommand callbacks
([run.py:69](../../../src/otto/cli/run.py), [test.py:503](../../../src/otto/cli/test.py),
[host.py:102](../../../src/otto/cli/host.py),
[monitor.py:81](../../../src/otto/cli/monitor.py)) before the command body, and read by the
`otto reservation` command. Nothing reads it afterward. It must survive only the top-level →
subcommand callback hop — which is precisely what Click's `ctx.meta` (shared across the
command chain) is for.

- Introduce a `ReservationState` value object (`backend | None`, `identity | None`,
  `skip_check: bool`).
- Top-level `main` callback builds it and stores it: `ctx.meta["otto_reservation"] = ...`.
- `gate()` takes a `ReservationState` (or reads `ctx.meta`); no-ops when absent (same
  behavior as today's `tryGetConfigModule() is None`). Update the 4 callers (they already
  receive `ctx`) and the `otto reservation` command (add `ctx: typer.Context` if needed).
- The reservation **API** (`build_backend` / `resolve_username` / `check_reservations` /
  `ReservationState`) is the reusable component a script can call directly. `open_context()`
  does **not** gate by default (a library user reaching hosts directly typically doesn't
  want a reservation check).

This keeps `OttoContext` free of reservation fields → smallest possible frozen surface.

---

## 5. Library / bring-your-own-CLI API

The lab-scope backstop lives in the Typer wrapper, so scripts need a first-class public
entry point — not just prose.

**Recommended script path:**

```python
import asyncio, otto

async def main():
    async with otto.open_context(lab="mylab") as ctx:        # build + set contextvar + own scope
        results = await ctx.run_on_all_hosts("uname -a")     # explicit; bare run_on_all_hosts() also works
    # every host opened in the block is closed here, deterministically
asyncio.run(main())
```

`open_context()` is an `@asynccontextmanager`: resolve the lab (load by name via
`load_lab()` or accept a `Lab`), build `OttoContext`, `set_context()`, `async with
ctx.scope`, `reset_context()` on exit.

**Custom-CLI path** (a Typer-like split across a sync entry and async dispatch): expose and
document the lower-level primitives — `OttoContext(...)`, `set_context()` / `reset_context()`
(token-based), and `async with ctx.scope:`. otto's own CLI uses exactly these (sets the
contextvar in the callback, enters `ctx.scope` in `async_typer_command`), so the CLI is just
`open_context` taken apart across Typer's callback/subcommand boundary.

**Public surface:** `open_context`, `OttoContext`, `get_context`, `try_get_context`,
`get_lab`, `get_host`, `all_hosts`, `run_on_all_hosts` importable from the top-level `otto`
package (exact re-export wiring is an implementation detail).

**Deliverable:** a "using otto as a library" docs page covering both paths and the teardown
guarantees (when hosts close, what the scope does, FD-style direct construction).

---

## 6. Testing strategy

TDD throughout — write the failing test, then implement.

- **Context fixture:** one fixture builds an `OttoContext` from test lab data and
  `set_context()` / `reset_context()` around the test (token-based), replacing scattered
  `setConfigModule` calls and the autouse `_reset_dry_run`
  ([conftest.py:331](../../../tests/conftest.py)). The CLI-unit conftest fixture migrates
  to it.
- **Cases:** shims read the contextvar; `isDryRun()` / log accessor degrade safely with no
  context; `get_host`/`all_hosts` register returned hosts (and override-copies) into the
  scope; scope closes only `_connected` hosts and is error-isolating; `async with host:`
  closes; idempotent double-close; `gate()` no-ops with empty `ctx.meta` and checks with a
  populated `ReservationState`; `open_context()` tears down hosts on block exit.
- **Lifecycle teardown in tests:** the scope teardown is tied to `async_typer_command`'s
  `asyncio.run`; tests that exercise host lifecycle outside the CLI must use `open_context()`
  or `async with ctx.scope` (don't rely on an implicit backstop).

---

## 7. Migration-plan contract draft update (deliverable)

Update [todo/migration_plan.md](../../../todo/migration_plan.md) Phase 0/1 "decomposed
pieces": replace `getConfigModule()` with `get_lab()`, and note repos via `getRepos()` and
reservations as a CLI-layer concern (not part of the runtime-context contract). Pre-freeze,
refining the draft is the intended use of this window.

---

## 8. Suggested implementation order (TDD-friendly; detailed plan is writing-plans' job)

1. `OttoContext` (data + canonical lookup **methods**) + contextvar + `get_context`/
   `try_get_context`/`set_context`/`reset_context` + `HostScope` (with tests), no call-site
   changes yet.
2. Reimplement the bare accessors (`get_host`, `all_hosts`, `run_on_all_hosts`,
   `do_for_all_hosts`) as **delegations** to the active context's methods; add the `get_lab`
   shim and the `isDryRun`/log shims; `load_lab()` rename; delete `getConfigModule`/
   `setConfigModule`/`setDryRun`. Migrate the test fixture.
3. Host `_lab` back-reference: wire it in the storage factory, repoint `_build_hop_transport`
   off ambient `get_host`, carry it through `dataclasses.replace`.
4. Wire the CLI entry point: build `OttoContext` + `set_context()` in `main`; enter
   `ctx.scope` in `async_typer_command`.
5. Instruction/suite wrapper: inject `ctx` into handlers that declare a `ctx: OttoContext`
   parameter (exclude it from Typer's option expansion).
6. Reservation state → `ctx.meta`; update `gate()` + 4 callers + `otto reservation`.
7. Add `Host.__aenter__/__aexit__`, delete `__del__`, confirm idempotent `close()`, remove
   dead `_closing` scaffolding (only once green).
8. `open_context()` + public re-exports + library docs page.
9. Update the migration-plan contract draft.

---

## 9. Risks / edge cases

- **Nested `asyncio.run`** (inner pytest sessions / embedded coverage e2e): a fresh
  `asyncio.run` copies the current context, so the contextvar is still visible, but nested
  scopes must not double-close shared hosts — idempotent `close()` covers this; verify on
  the embedded e2e tier.
- **xdist / parallel workers:** each worker process has its own contextvar — no cross-worker
  bleed; confirm the fixture sets per-test.
- **Scaffolding removal regressions:** the loop tracker/reaper/leak detector stay; only the
  `__del__`-specific `_closing` guards go, and only after a green run. The review warned the
  composition root *reduces but won't eliminate* test-infra leaks.
- **Directly-constructed hosts** outside any context have no backstop (by design, FD model);
  the library docs must state this clearly.
- **Instruction `ctx`-injection touches deferred code:** excluding `ctx` from Typer's option
  expansion lives in `_wrap_with_options` / `register_suite`
  ([params.py](../../../src/otto/params.py)) — the same machinery Phase B rewrites. Keep the
  injection minimal (prepend `get_context()` at call time) to limit rework.
- **`_lab` wiring** must survive `dataclasses.replace` and any `slots`; verify against the
  hop-chain tests. The lab↔host cycle is intentionally fine post-`__del__`.
