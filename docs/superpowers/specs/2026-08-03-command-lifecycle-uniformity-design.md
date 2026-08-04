# Command lifecycle uniformity: one policy, free for every command, composable for `otto test`

**Date:** 2026-08-03
**Status:** agreed direction; implementation in waves (wave 1 = the `otto test` gap)
**Context:** churn/design review Tier 0.4 (`todo/churn-and-design-review-2026-08-03.md`);
Chris's question: should the CLI command-registration decorator own signal/lifecycle
setup so third-party commands get first-party safety guarantees for free, with an
escape hatch for `otto test`?

## Decision

Yes to the goal (lifecycle guarantees applied uniformly, including third-party
registered commands, with `otto test` composing rather than escaping) — but the
home is **not** the registration decorator. The policy stays defined once in
`otto.lifecycle`; it is **applied** at the existing leaf-invoke wrapper
(`cli/invoke.wrap_leaf_callbacks` → `_wrap_invoke`), and special orchestrators
use the lifecycle **primitives** directly instead of modifying a decorator.

### Why not the registration decorator

1. **Registration happens at import time; policy applies at invoke time.** A
   decorator wraps a function object once, when a module loads. The wrapper
   wraps `Command.invoke`, so `--help` never enters the policy, resolution
   stays lazy, and the spec on `ctx.meta` reflects the leaf actually invoked.
2. **Third parties don't register leaf functions.** `register_cli_command`
   takes a lazy loader that usually resolves to a whole Typer app/group. A
   registration-time decorator never sees the leaves; `wrap_leaf_callbacks`
   already recurses into groups AND wraps `get_command` so lazily-synthesized
   children (e.g. `otto host <verb>`) are wrapped on resolution. This is the
   exact "for free, including third-party" property wanted — the machinery
   exists and already carries `command_preamble`.
3. **Double-wrap hazard.** Commands self-wrap today via `@async_typer_command`
   (→ `run_command`). A second wrapping layer at registration would nest
   `run_command` inside `run_command` (a hard error: one `asyncio.run` per
   process invocation, AST-guarded). Applying policy at ONE seam with an
   idempotence marker (`_otto_preambled` pattern) avoids the class.
4. **Standalone testability.** Sub-apps are deliberately usable bare under
   `CliRunner` without the root dispatch (see `make_registry_group`'s
   docstring). Policy-at-invoke keeps unit tests loop- and signal-free.

### The escape hatch, reframed

`otto test` does not get a modified decorator; it gets **public primitives**:

- `run_command(coro)` — the async command policy (exists).
- `sync_phase(...)` — NEW: the same two-stage interrupt policy for a
  synchronous phase that owns its own event loops (pytest). Any future
  orchestrator with a foreign sync phase composes the same two pieces.

One policy definition, zero per-command variance, no decorator forks.

## Wave 1 (the Tier 0.4 fix): `lifecycle.sync_phase`

`suite/run.py` wraps its coverage phases in `run_command` but runs
`_run_pytest_session` bare between them — the longest, most host-holding phase
of the most-used command has no two-stage interrupt policy.

Semantics (mirroring `_CommandRun`):

1. **Enter:** install SIGINT/SIGTERM handlers (main thread; assert and
   document). Prior handlers captured for restoration.
2. **First signal:** deliver `KeyboardInterrupt` into the phase (pytest's own
   graceful teardown — fixtures unwind, suite fixtures release connections)
   and **arm the teardown deadline** (same budget the async policy uses).
3. **Second signal or deadline expiry:** force path — run
   `_run_force_exit_hooks()` (terminal restore), stop the logging queue
   listener explicitly (the async path gets this via atexit; a forced sync
   exit must not lose buffered log lines), then `os._exit(128 + signum)`.
   `os._exit` is deliberate: nothing else reliably preempts a wedged sync
   teardown; the async policy's cancel-based force has no sync equivalent.
4. **Exit (phase returned):** restore handlers. If a signal had fired, the
   guard reports it so `run_suite` exits `128 + signum` — identical to the
   async policy's contract — instead of pytest's rc-2 masquerading as a
   test-failure exit.
5. **Scope:** wrap ONLY `_run_pytest_session`. The existing
   `rebuild_connections()` patch-up stays (pytest's loops are still foreign);
   the guard bounds interruption, it does not adopt pytest's loop state.

### Testing (the chaos lessons apply verbatim)

Signal-path assertions are the vacuous-assertion minefield: every test must
carry a mid-flight positive control, and delivery must be proven via a
**subprocess** pytest run (ambient env, not monkeypatch — the #192 lesson)
with signals sent by PID to the child, never `pkill -f` on a redirect
operand. Minimum set: (a) first-SIGINT → graceful teardown ran, exit 130;
(b) wedged teardown + deadline → force hooks ran, exit 130, bounded wall
time (count work, not time, where possible); (c) double-SIGINT → immediate
force; (d) no-signal run → handlers restored (assert on the guard's exit,
positive control that they were installed mid-phase).

### Wave-1 implementation notes (post-review, 2026-08-04)

The formal review of the first implementation found the force path could
self-deadlock in signal-handler context (same-thread lock reentrancy:
`Timer.start`, `Lock.acquire`, and the listener flush all acquire locks the
interrupted frame can hold — a permanent, signal-unrescuable hang of the
exact mechanism whose job is "always gets out"). The landed design differs
from a naive reading of the semantics above in four ways:

1. **The force path runs on a watchdog thread**, spawned at `sync_phase`
   entry (normal context) and woken through a self-pipe — `os.write` is the
   only handler-side primitive. The handler itself only writes attributes,
   writes bytes, and raises; it takes no locks and starts no threads.
2. **The listener flush is bounded** (`shutdown_listener(join_timeout=...)`:
   probe the queue mutex, join with timeout) — the force path trades
   buffered log lines for a guaranteed exit, never the reverse.
3. **`SyncPhaseInterrupt(KeyboardInterrupt)` carries the signal number**, so
   the `128 + signum` contract survives the irreducible one-bytecode
   entry/exit windows where the raise escapes `sync_phase` itself.
4. **`install_handlers=False` is the seam** (mirroring `_CommandRun`'s): the
   root test conftest forces it process-wide (a real install/restore cycle
   disarms the harness's chained SIGINT faulthandler; opting back in via the
   `real_sync_phase` fixture re-arms it in teardown), and
   `_guarded_pytest_session` passes it off the main thread — library callers
   degrade to an unguarded session, exactly like the async policy.

## The user contract (wave 2's acceptance criterion)

A third-party command author writes a plain ``async def`` command on their own
Typer app and registers it — nothing else:

```python
app = typer.Typer()

@app.command()
async def deploy(target: str): ...   # plain async def

register_cli_command(name="deploy-tool", loader="my_pkg.cli:app", help="...")
```

Registration IS the opt-in. The leaf-invoke wrapper detects coroutine
callbacks (``iscoroutinefunction``) and bridges them through ``run_command``
(idempotence-marked; leaves already self-wrapped are skipped). The
``@instruction`` lane already meets this bar today — the decorator wraps
handlers with ``async_typer_command`` internally, so instruction authors get
the full policy with zero knowledge of it; wave 2 extends the same property
to registered Typer apps. End state: ``@async_typer_command`` leaves the
public surface — one way to get a lifecycle'd command. Failure mode inverts
from silent to loud: a bare ``async def`` outside otto's dispatch fails
visibly (un-awaited coroutine) instead of running with missing safety.
Docs cost: one paragraph on the extension-points page.

## Wave 2 (follow-up, pairs with Tier 2.3): policy-at-the-wrapper

Move the `run_command` entry from per-function `@async_typer_command` into
`_wrap_invoke`, so ANY registered command — including third-party loaders —
runs under the async policy with zero opt-in. Migration: teach the wrapper
to apply policy only when the leaf isn't already self-wrapped (marker), then
strip `@async_typer_command` sites module-by-module, then make the wrapper
unconditional. Natural companion: generalizing `_render_result` into the same
wrapper (Tier 2.3) — one seam then owns preamble, lifecycle, and rendering.
Out of scope here: `config/repo.py`'s property-triggered `run_command`
(Tier 2.5 owns it).
