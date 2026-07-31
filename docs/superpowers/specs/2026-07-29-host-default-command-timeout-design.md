# Default command timeout for `Host`

**Date:** 2026-07-29
**Status:** Awaiting review

## Problem

`Host.run()` and `Host.exec()` accept a `timeout`, but it defaults to `None` —
"block forever" — at every layer: the `Host` protocol, `BaseHost`, all four
concrete subclasses, and the `otto host <id> run --timeout` CLI option. A
command that hangs therefore hangs the test rather than failing it, which is
the opposite of what a test framework should do.

Two findings make this worse than a missing default.

**The SSH `exec()` fast path ignores `timeout` entirely.** At
`src/otto/host/session.py:1579-1587` the output loop is never wrapped in
`asyncio.wait_for`, so the `except asyncio.TimeoutError` below it is dead code
and `await process.wait()` on line 1587 is itself unbounded. Passing an
explicit timeout to `UnixHost.exec()` over SSH does nothing today.
`DockerContainerHost.exec()` delegates to its parent, so it inherits the gap.

**Callers already worked around it.** `tunnel/manage.py`,
`tunnel/discovery.py`, and `link/manage.py` wrap host calls in their own
external `asyncio.wait_for(..., 30.0)` instead of using the built-in kwarg —
eleven sites in total. That is direct evidence the built-in parameter was not
usable.

## Goals

1. No `Host` command execution can block forever by default, or by accident.
   Unbounded remains available, but only as an explicit, self-describing
   request (`float("inf")` — section 2b), never as a silent default.
2. The built-in `timeout` parameter is load-bearing on every path, so callers
   have no reason to re-implement it externally.
3. `otto host <id> run --help` advertises the default.
4. **Every command surface is as consistent as possible**: one constant, one
   validator, one place the default lives, and the same delegate-to-a-hook
   shape for `run`, `exec` and `expect` — so no host family can silently
   disagree with the documented interface (section 7a).

## Non-goals

- No `settings.toml` knob. Per-call overrides already exist via the `timeout=`
  kwarg and `ShellCommand.timeout`; a global config surface has no demonstrated
  use case yet. YAGNI — add it when a real one appears.
- No new exception type. Timeouts stay a returned `CommandResult`, preserving
  the existing contract and its tests.

## Design

### 1. One constant

```python
DEFAULT_COMMAND_TIMEOUT = 30.0
```

Defined and exported from `src/otto/host/host.py`. 30 seconds matches the de
facto convention already in the tree: `_TUNNEL_HOST_TIMEOUT` and
`_IMPAIR_HOST_TIMEOUT` are both `30.0`.

### 2. The timeout is a plain `float`, not `float | None`

The public command-execution signatures become:

```python
timeout: float = DEFAULT_COMMAND_TIMEOUT
```

The `| None` is dropped. `None` no longer has any meaning to express — it used
to mean "forever", which is exactly what this design removes — so admitting it
in the type would be dishonest.

Two independent mechanisms then keep unbounded execution *unreachable by
accident* — an explicit `float("inf")` still opts in, deliberately:

- **The signature default** is what `otto host <id> run --help` reads. The
  `otto host` verbs are synthesized from `BaseHost.run` by introspection
  (`src/otto/cli/expose.py`; `src/otto/cli/param_synth.py:231` forwards
  `inspect.signature` defaults to Typer verbatim), and `show_default` is left
  at Typer's `True`. Click suppresses a `None` default, which is why
  `--timeout` renders no default today while `--sudo` renders
  `[default: no-sudo]`.
- **Validation at the three public entry points** (section 3) closes the hole
  for callers a type checker never sees — downstream suites consuming otto as a
  library, where Python does not enforce annotations at runtime. See section 2a;
  an `is None` check is explicitly *not* what goes here.

#### Why plain `float` beats `float | None` — verified, not assumed

- **The CLI help is identical.** Inspecting the synthesized click parameter
  under both annotations with the default set to `30.0` yields
  `default=30.0 type=FLOAT show_default=True` in both cases. Narrowing the
  annotation costs nothing in help output.
- **`ty` rejects `timeout=None` against a plain `float`**
  (`invalid-argument-type`) — but **only on the undecorated surfaces.**

  `@cli_exposed` is typed `Callable[..., Any] -> Callable[..., Any]`
  (`src/otto/utils.py`), which erases the decorated method's signature from
  `ty`'s view entirely. `BaseHost.run` carries that decorator, so **every call
  to `host.run(...)` is unchecked**. Measured against `[tool.ty.rules] all =
  "error"`, a decorated method silently accepted `timeout=None`,
  `timeout="banana"`, *and* a `bogus_kwarg=1` that does not exist in the
  signature — zero diagnostics — while the identical undecorated method was
  flagged.

  So the enforcement benefit applies to `exec` and `expect` (undecorated) and
  **not** to `run`. An earlier draft of this spec claimed the typecheck gate
  would find every internal `None`-forwarding caller; that is false for `run`,
  and the correction matters because section 4's forwarding wrappers
  (`context.py`, `config/fleet.py`) forward into `run` specifically. **They must
  be found by grep, not by `ty`.**

  This is a pre-existing, systemic hole rather than something this design
  introduces: *every* `@cli_exposed` host verb — `run`, `put`, `get`, `reboot`,
  and the rest of the `otto host` surface — is call-site-unchecked today. Worth
  its own issue; out of scope here.
- **The runtime validator draws no `ty` diagnostic**, despite
  `[tool.ty.rules] all = "error"` in `pyproject.toml:179-180`. `isinstance`
  narrowing, `math.isnan`, and a bound comparison on a `float`-annotated
  parameter all typecheck clean, so the runtime guarantee needs no suppression.
  This was the open question that made plain `float` look like it might cost us
  the runtime guarantee; it does not.

#### Where `| None` legitimately stays

`ShellCommand.timeout` keeps `float | None = None`, because there `None` means
"inherit the run-level value" — a real, still-needed meaning, and the reason
`_resolve_command`'s `default_timeout` parameter must also keep `float | None`
(the list path at `host.py:672` deliberately passes `None` so each command
inherits the budget rather than a per-command cap).

`BaseHost.app_shell` (`host.py:717`) is a different abstraction and is out of
scope.

### 2a. Validate the value, don't just check it isn't `None`

An `is None` check barely narrows anything — it admits negatives, `NaN`,
infinity, and non-numeric types, all of which `asyncio.wait_for` misinterprets
silently. The guard at the entry points is therefore a real validator:

```python
def _validate_timeout(timeout: float) -> float:
    """Reject values `asyncio.wait_for` would silently misinterpret."""
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
        raise TypeError(f"timeout must be a number, got {type(timeout).__name__}: {timeout!r}")
    if math.isnan(timeout):
        raise ValueError("timeout must not be NaN")
    if timeout < 0:
        raise ValueError(f"timeout must be >= 0, got {timeout!r}")
    return float(timeout)
```

Accepted: any number `>= 0`, **including `float("inf")`** (section 2b).
Rejected: `None` and other non-numerics, `bool`, `NaN`, and negatives —
note `-inf < 0`, so it is caught by the bound without a special case.

`None` is thus rejected loudly rather than silently coerced — strictly more
informative for a downstream suite written against the old API, since the error
names the offending value at the call site instead of quietly changing that
suite's timeout behavior.

Each of these rejections corresponds to a measured misbehavior of
`asyncio.wait_for` on Python 3.10.20, not a hypothetical:

| Value | `asyncio.wait_for` behavior | Why reject |
| --- | --- | --- |
| `-1` | `TimeoutError` immediately | every command fails; a plausible bug from a computed `deadline - now` |
| `NaN` | `TimeoutError` immediately — but *returns normally* if the coroutine is already done | outcome depends on scheduling; effectively nondeterministic |
| `inf` | never times out | **accepted** — this is the deliberate escape hatch (section 2b) |
| `True` | silently means 1.0s | `bool` is an `int` subclass; a typo'd flag becomes a 1-second timeout |

### 2b. `0` means "immediate"; `inf` means "indefinite"

`0` is accepted and carries no special meaning: `asyncio.wait_for(coro, 0)`
raises `TimeoutError` at once, so a zero timeout yields an ordinary
`CommandResult` with `timed_out=True` and a `"timed out after 0s"` message.
That is self-explanatory and needs no special-casing anywhere.

`0` was considered as a sentinel for "wait indefinitely" and **rejected**,
because it would invert the meaning the value already has in the primitive it
is passed to. Within Python the convention is consistent: `wait_for(coro, 0)`
times out immediately, `socket.settimeout(0)` selects non-blocking mode, and
`subprocess.run(timeout=0)` times out at once. `host.exec(cmd, timeout=0)`
would therefore read as "fail instantly" to anyone who knows asyncio. The
convention is not universal — Java's `setSoTimeout(0)` and `curl --max-time 0`
do mean infinite — but otto is Python, and inverting the host language's
meaning to save a sentinel is a poor trade.

The honest spelling for unbounded is `float("inf")`, and it **is accepted**. A
deliberate, clearly-marked unbounded call is legitimate; what this design
removes is unbounded-*by-default* and unbounded-by-accident. `inf` satisfies
that distinction in a way a sentinel cannot: it is self-describing at the call
site, it needs no special-casing anywhere in otto (measured — `wait_for(coro,
inf)` simply never fires), and it is greppable, so every intentional unbounded
call in the tree can be enumerated with a single search.

It is also reachable from the CLI with no work: click's `FLOAT` parses `inf`,
`Inf`, `INF`, `infinity`, and `Infinity`, plus any overflowing literal such as
`1e400`, all into `float("inf")` — and `-inf` is rejected by the section 2c
bound with a clean exit-2 usage error. So `otto host <id> run --timeout inf`
expresses a deliberate unbounded run, and no CLI-layer code is needed to
support it.

Accepting `>= 0` rather than `> 0` also has a practical benefit: it is exactly
expressible as a click bound (section 2c), whereas an exclusive lower bound is
not.

### 2c. CLI-level bound

`Opt` gains a `min` field which `param_synth` forwards to
`typer.Option(min=...)`, and `--timeout` sets `min=0.0`. This was measured to
give click's own clean usage error rather than a traceback —
`Invalid value for '--timeout': -5.0 is not in the range x>=0.0.` with exit
code 2 — and renders `<float range> [x>=0.0]` alongside `[default: 30.0]` in
help. That matches the house pattern of surfacing bad CLI input as a
`typer.BadParameter`-style usage error, and keeps `Opt` free of any typer
import.

Two limits are worth recording so nobody assumes otherwise:

- **click's `FloatRange` does not reject `NaN`.** `--timeout nan` passes an
  inclusive `min=0.0` bound (measured — `nan >= 0` evaluating false does *not*
  make click reject it). It is then caught by `_validate_timeout`, which for CLI
  use surfaces as a traceback rather than a usage error. `NaN` is now the only
  such input, since `inf` is accepted and `-inf` gets a clean usage error. One
  degenerate spelling producing an ugly-but-correct failure is an acceptable
  rough edge; closing it would need a Typer `callback` on the synthesized
  option, which is not worth the `param_synth` complexity.
- **An exclusive lower bound is not reachable through Typer.**
  `typer.Option` exposes only `min`/`max`/`clamp`, not click's `min_open`.
  Routing a `click.FloatRange(min=0, min_open=True)` in via `click_type` was
  tried and is worse: it degrades the help metavar to `<function>` and turns
  bad input into an exit-1 exception instead of an exit-2 usage error.

### 3. Three validation points

**`BaseHost.run`** — one normalization at the top of the method covers both the
single-command path (`host.py:659`) and the cumulative-budget list path
(`host.py:685`), for all four subclasses.

**`exec`** — `BaseHost.exec` currently only raises `NotImplementedError`.
Convert it into a concrete template method that normalizes the timeout and
delegates to a new abstract `_exec_one`; the four subclasses rename their
`exec` to `_exec_one`. This mirrors the `run`/`_run_one` template already in
the same file, and means a future host subclass cannot forget the
normalization.

The alternative — repeating the normalization line in each of the four `exec`
overrides — is less churn but leaves that omission bug permanently available.
The template is preferred because the failure mode it prevents is silent.

**`HostSession.run`** (`session.py:1078-1117`) is a **third** public entry point,
not an internal one. `HostSession` is exported from `otto.host`
(`src/otto/host/__init__.py:55`), documented in `docs/guide/docker.md`, and
obtained by callers via `await host.open_session(name)`; it has its own
`timeout` parameter and its own `_run_cmds_with_budget` call, so it needs the
same signature change and the same validation as `BaseHost.run`.

Note its current default is `10.0`, not `None` — so unlike `run`/`exec` this
one is already bounded, and moving it to 30s is a loosening. That is the right
direction for consistency (one advertised default across every entry point),
but it is a behavior change in its own right: a caller relying on a 10s bound
via `open_session` gets 30s.

`SessionManager.exec` and `SessionManager.run_cmd` genuinely need no
validation — they are only reached through the three entry points above, which
have already validated. Placing the guard at the public boundary and nowhere
else is deliberate: it is what makes narrowing the internals safe.

#### Internal seams narrow to plain `float` too

Because the entry points guarantee a real number, the `None` branches
downstream become dead and should be removed rather than left as false
suggestions that unbounded execution is still representable:

- `_run_cmds_with_budget` (`host.py:134-177`) — `timeout` and the derived
  `deadline` are always set, so the `if timeout is not None` guard and the
  `remaining is None` arm of the effective-timeout calculation collapse.
- `_run_one` on `BaseHost` and all four subclasses — the computed effective
  timeout is always a `float`.
- `ShellSession.run_cmd` (`session.py:406-420`) — the
  `if timeout is not None: return await asyncio.wait_for(...)` conditional
  becomes unconditional. Every command now goes through `wait_for`, including
  an intentionally unbounded one: `wait_for(coro, inf)` was measured to
  complete normally and never fire, so `inf` needs no bypass branch.

This is the substantive payoff of dropping `| None`: not just an honest
signature, but a single code path for every command. Unbounded execution stops
being a *different branch* and becomes merely a *different value* — which is
why it can be permitted safely.

### 4. Timeout semantics for command lists

For a list, the run-level `timeout` is a **cumulative budget** shared across
all commands, and the default participates in exactly that role — a default
`run([...])` gives the whole list 30 seconds, not 30 seconds each. This is a
deliberate behavioral change for multi-command callers, accepted because
otto's own internals are the only callers today.

The mechanism is unchanged: `_run_cmds_with_budget` (`host.py:134-177`) gives
each command the lesser of its own `ShellCommand.timeout` and the remaining
budget, and marks the remainder `Status.Error` with
`"Skipped: cumulative timeout budget exhausted"` once the budget is gone.

In-repo impact is limited. `monitor/collector.py:324` — the only multi-command
caller — already passes an explicit `float`, so it is unaffected.

**The forwarding wrappers do need edits.** `context.py:236-260`
(`OttoContext.run_on_all_hosts`) and `config/fleet.py:265-315`
(`run_on_all_hosts` / `do_for_all_hosts`) both declare
`timeout: float | None = None` and forward it straight into `host.run(...)`.
Each should adopt `timeout: float = DEFAULT_COMMAND_TIMEOUT` to match.

**`ty` will NOT flag these — find them by grep.** Because `BaseHost.run` is
`@cli_exposed`, its signature is erased at every call site (section 2), so
forwarding a `float | None` into it is invisible to the typecheck gate. An
earlier draft of this spec asserted these two wrappers needed no edit at all,
and then a later draft asserted `ty` would catch them; **both claims were
wrong**. The first was wrong because they really do forward the value; the
second was wrong because the decorator erases the check. The reliable search is:

```bash
grep -rn "timeout" src/otto/context.py src/otto/config/fleet.py
grep -rn "float | None" src/otto/ | grep -i timeout
```

The lesson worth keeping: a typecheck gate only enforces what it can see, and a
permissively-typed decorator silently removes a method from its view.

### 5. Fix the SSH `exec` gap

Wrap the output loop at `session.py:1579-1587` in `asyncio.wait_for(...,
timeout)` and bound `await process.wait()` as well, returning the same
`CommandResult` shape the other paths already produce: `Status.Error`,
`retcode=-1`, and a `"Command timed out after Ns"` message. Without this, the
new default is a lie on the most-used remote path.

### 6. Make the timeout structurally detectable

Add a field to `CommandResult` (`src/otto/result.py:62`):

```python
timed_out: bool = False
"""True when this command was killed by its timeout rather than exiting."""
```

`CommandResult` is a frozen dataclass whose fields all have defaults, so this
is backward-compatible.

This field is required by section 7. `retcode=-1` cannot serve the purpose: it
already means both "never ran" and "skipped: cumulative budget exhausted", so
distinguishing a timeout from an ordinary failure would otherwise require
string-matching `"timed out"` in `.value` — fragile in precisely the way that
would silently degrade the error messages below.

Every internal timeout path sets it:

- `ShellSession.run_cmd` (`session.py:406-420`)
- `LocalHost._exec_subprocess` (`local_host.py:262-269`)
- the new SSH `exec` guard (section 5)
- the budget-exhausted skip in `_run_cmds_with_budget` (`host.py:157-165`)

### 7. Retire the external `asyncio.wait_for` workarounds

Nine of the eleven sites drop their external wrapper and pass the module's
existing named constant as the built-in `timeout=` instead:

| File | Lines | Call |
| --- | --- | --- |
| `tunnel/manage.py` | 80, 299, 323, 344, 460, 513 | `exec` |
| `tunnel/discovery.py` | 72 | `exec` |
| `link/manage.py` | 131 | `exec` |
| `link/manage.py` | 149 | `run` |

`_TUNNEL_HOST_TIMEOUT` and `_IMPAIR_HOST_TIMEOUT` are **kept** and passed
explicitly rather than deleted in favor of the default. Both modules document
bounding every host call as a requirement — `link/manage.py:5-6` cites spec §9
— so a named local value preserves that stated intent and keeps these paths
pinned if the global default is ever retuned.

**Error messages must be preserved exactly.** These sites do more than bound a
call; they convert the raised `asyncio.TimeoutError` into contractual
diagnostics. Because a timeout now *returns* rather than raises, each site
switches to checking `result.timed_out` and then raises or logs its existing
message verbatim:

- `link/manage.py:128-136` and `139-154` — a host-named `RuntimeError`
  (`host {id!r} unreachable running {cmd!r}`). Per the module docstring a down
  host must be "a loud, host-named `RuntimeError` — never a skip", which also
  matches the standing project rule against skipping on host-down.
- `tunnel/manage.py` — per-operation messages: "timed out inspecting
  container", "timed out checking for {tools}", "timed out probing for free
  ports", "timed out spawning the tunnel".
- `tunnel/manage.py:513-519` and `discovery.py:72-78` — log a warning and add
  the host to the `unreachable` set for partial-reap reporting, rather than
  raising.
- `tunnel/manage.py:344-348` — best-effort rollback reap; keeps swallowing.

**Two sites are deliberately kept as-is:** `discovery.py:70` and
`manage.py:107` wrap `host.is_running()`, which is a liveness probe rather
than a command-execution method and has no `timeout` parameter. It becomes
transitively bounded once `exec` is, but it can issue more than one command,
so inventing a timeout parameter for it is out of scope. Each keeps its
wrapper plus a comment stating why.

### 7a. `expect` — one advertised default across every command surface

Consistency across command surfaces is a goal in its own right, and auditing
every `timeout` in the host package surfaced a live defect in `expect`:

- `Host.expect` protocol (`host.py:294`) and `BaseHost.expect` (`host.py:754`)
  promise `timeout: float = 30.0`.
- **Every concrete implementation uses `10.0`** — `local_host.py:307`,
  `unix_host.py:743`, `embedded_host.py:494`, `docker_host.py:377`.
- `HostSession.expect` (`session.py:1169`) is `10.0` while `ShellSession.expect`
  beneath it (`session.py:342`) is `30.0`.

So a caller reading the documented interface gets a third of the advertised
wait. `expect` adopts `DEFAULT_COMMAND_TIMEOUT` everywhere and follows the same
template shape as `exec`: `BaseHost.expect` validates once and delegates to a
new `_expect_one`. The four implementations are near-identical — the only
differences are the dry-run message wording and `DockerContainerHost`'s extra
`_ensure_running()` — so the template removes the duplication and the
disagreement together. `expect` is not `cli_exposed`, so there is no CLI change.

**The per-family hooks take no default.** `_run_one`, `_exec_one` and
`_expect_one` are always called explicitly by their template, so a default on
them is dead code that can only drift from the advertised one — which is
precisely how `expect` came to disagree with its own protocol, and what the
cosmetic `10.0` on `_run_one` was. The default lives on the public surface; the
hooks require the value.

#### `AppShell` joins the constant, and keeps its `None`

`AppShell.cmd_timeout: ClassVar[float] = 30.0` (`app_shell.py:275`) already
equals the constant chosen here — independent corroboration of `30.0` — but two
equal literals are a drift risk, so it becomes
`cmd_timeout: ClassVar[float] = DEFAULT_COMMAND_TIMEOUT`. This is free: importing
`otto.host.app_shell` already loads `otto.host.host` transitively via the
package `__init__` (measured), so a module-level
`from .host import DEFAULT_COMMAND_TIMEOUT` adds no modules to the import budget
and creates no cycle — `host.py` imports `app_shell` only under `TYPE_CHECKING`.

**`AppShell` keeps `timeout: float | None = None`, and this is consistent rather
than an exception.** It has a three-level cascade — the `cmd_timeout` class
default, a per-session override via `attach()` / `BaseHost.app_shell()`, and a
per-call override via `cmd()` — where each level's `None` means "not specified,
inherit the level above" (`app_shell.py:283-287`, `:418`). The sentinel is
load-bearing: `DEFAULT_COMMAND_TIMEOUT` is itself a *valid explicit value*, so it
cannot double as the "not specified" signal. Were `cmd(timeout=...)` to default
to the constant, `app_shell(SomeShell, timeout=120)` followed by a plain
`sh.cmd("…")` would silently use 30s instead of the session's 120s, breaking
per-session overrides.

That yields one statable invariant for the whole codebase, which is what
"consistent" should mean here:

> `float | None` appears **iff** there is an enclosing default to inherit from;
> plain `float` appears wherever the value is final. `None` never means
> unbounded, anywhere — `float("inf")` is the only way to opt out of a bound.

The three surviving `float | None` sites all satisfy it: `AppShell`'s cascade,
`ShellCommand.timeout`, and `_resolve_command`'s `default_timeout`.

#### `HostSession.expect` must validate, which is what covers `AppShell`

`AppShell` performs no validation of its own, and both of its waits
(`app_shell.py:372`, `:420`) go through `HostSession.expect`. Since
`HostSession` is public — exported from `otto.host`, documented — and
`HostSession.run` validates (section 3), `HostSession.expect` must validate too.
Otherwise `sh.cmd("…", timeout=-5)` reaches `asyncio.wait_for` unchecked and
fails every command instantly.

Validating there rather than inside `AppShell` is deliberate: it is the boundary
at which a cascaded value becomes final, so one check covers the class default,
the session override and the per-call override, and `AppShell` needs no
validation code at all.

#### Deliberately not unified

Forcing one number onto these would be cargo-culting a rule past its meaning:

- **Transport/connect timeouts** — `options.py` (`connect_timeout`,
  `socket_timeout`, `path_timeout`, `listener_timeout`,
  `echo_negotiation_timeout`), `transfer/nc.py:134,481`, `session.py`'s
  `_INIT_TIMEOUT` / `_RECOVERY_TIMEOUT`. These bound establishing or tearing
  down a channel, not running a command.
- **Reachability and power** — `is_reachable(10.0)`, `wait_until_up/down`,
  `reboot(600.0)`. A reboot legitimately takes ten minutes.
- **`_ProxyIO.expect` (`interact.py:363`)** — raw transport IO during
  login/proxy negotiation; its 10s is a login-prompt wait, not a host command.

### 8. Cleanups

- Delete the misleading `timeout: float | None = 10.0` defaults on the four
  `_run_one` overrides (`local_host.py:193`, `unix_host.py:598`,
  `embedded_host.py:429`, `docker_host.py:333`). `run()` always passes the
  value explicitly so they never fire, but they would now contradict the
  single source of truth.
- Replace every explicit `timeout=None` with `float("inf")`. There are **five**
  such callers, not two, and they are exactly what the section 2b escape hatch
  is for:

  | Site | Command | Why unbounded is right |
  | --- | --- | --- |
  | `docker/build.py:98` | `docker build` | no defensible bound; varies by builder |
  | `docker/compose.py:125` | `docker compose` | same |
  | `transfer/nc.py:646` | `nc -N <ip> <port> < src` | duration is the transfer |
  | `transfer/nc.py:713` | `nc -Nl -w <listener_timeout> …` | **self-bounding** via netcat's own `-w` |
  | `transfer/nc.py:846` | `nc -l -w <listener_timeout> …` | **self-bounding** via netcat's own `-w` |

  A made-up generous constant would be a guess that is eventually wrong on a
  slower builder or a larger file. `float("inf")` states the intent instead of
  encoding a guess, and keeps all five sites findable with one grep.

  **`ty` will catch only the two docker sites.** The nc calls go through an
  injected `exec_cmd` typed `Callable[..., Coroutine[Any, Any, CommandResult]]`
  (`transfer/nc.py:174`); the `...` parameter spec means kwargs are unchecked,
  so those three must be fixed by hand. Do not rely on the typecheck gate to
  find them.

  The `nc -w` cases deserve a note: the command already bounds itself, so an
  otto-level timeout would be redundant and could fire *before* netcat's own
  deadline, turning a working transfer into a spurious failure. Layering an
  otto bound slightly above `_nc_listener_timeout` was considered as
  defense-in-depth and deliberately not done — it adds a margin constant to
  keep in sync with a value the user can already configure via
  `NcOptions.listener_timeout`.

  The tradeoff, stated plainly: a wedged `docker build` now hangs rather than
  eventually failing CI. A generous constant would cap it. This is the right
  call because these are the deliberate, clearly-marked calls — but if the
  preference is that *nothing* in otto can hang, these are the sites to
  overrule it.

`embedded_host.py:658` (`120.0`) and `:706` (`20.0`) already carry real numeric
defaults and are `Exclude`d from the CLI. They are left alone.

### 9. Documentation that must be corrected

Two places actively state the old behavior and would otherwise be left lying:

- **`src/otto/host/host.py:230-232`** — the `Host` protocol's `run` docstring
  says the timeout is "a cumulative budget shared across all commands. ``None``
  means no limit." Both the `None` clause and the notion of no limit are now
  false; it must name `DEFAULT_COMMAND_TIMEOUT` as the default and state that
  execution is always bounded. `BaseHost.run`'s own docstring
  (`host.py:641-645`) describes the budget mechanics correctly and only needs
  the default named.
- **`docs/guide/cli-reference.md:260`** — the `--timeout SECS` row for
  `host ... run` has no default listed. The neighbouring `reboot` table
  (`:296`) already carries a `Default` column showing `600.0`, so this table
  should gain one showing `30.0`, and the description should mention
  `--timeout inf` for a deliberate unbounded run. The rendered `--help` output
  updates itself via introspection; this hand-written table does not.

`config/fleet.py:291` ("Per-host timeout forwarded to ``run``") stays accurate
as written; naming the resolved default there is optional polish.

Note that the `Host` protocol signatures (`host.py:215-222` for `run`,
`:244-249` for `exec`) change their annotation and default alongside
`BaseHost`, so the protocol and its implementations stay in agreement. A
protocol left at `float | None` while implementations narrow to `float` would
be an unsound widening that `ty` may or may not catch depending on how the
protocol is consumed — they must move together.

## Testing

The existing suite already covers this area well and establishes the patterns
to follow.

- **Default resolution.** Extend `tests/unit/host/test_run_timeout.py`: assert
  that `run(cmd)` with no timeout reaches `_run_one` with
  `DEFAULT_COMMAND_TIMEOUT`. The file's existing
  `patch.object(host, "_run_one", ...)` style applies directly.
- **The validator rejects each bad value.** Parametrize over `None`, `"30"`,
  `True`, `float("nan")`, `-1`, and `float("-inf")`, asserting `TypeError` for
  the non-numerics and `ValueError` for the rest, on both `run` and `exec`.
  Because the annotation now forbids these, the test passes them deliberately as
  an unchecked caller would; `tests/` is excluded from `ty`
  (`pyproject.toml:173`) so no suppression is needed, but the test should carry
  a comment saying the bad values are intentional in case that exclusion
  changes.
- **`0` is accepted and behaves as immediate.** Assert `timeout=0` yields
  `Status.Error` with `timed_out=True` rather than raising — this is the guard
  against someone later "helpfully" reinterpreting `0` as indefinite.
- **`inf` is accepted end to end.** Assert `run(cmd, timeout=float("inf"))`
  does not raise and does not time out, and that `--timeout inf` parses to
  `float("inf")` rather than erroring — this is the guard against a future
  tightening of `_validate_timeout` silently removing the escape hatch.
- **The CLI bound.** Assert `--timeout -5` exits 2 with click's range message,
  and that `--help` shows both `[x>=0.0]` and `[default: 30.0]`.
- **No unbounded path remains.** A test asserting `exec` and `run` bound their
  work on every concrete host class, via the
  `tests/integration/host/test_host_contract.py` cross-host-class conformance
  pattern.
- **The SSH gap, proven red first.** A test that must fail against the current
  `session.py` — a stalling SSH `exec` with an explicit short timeout returning
  `Status.Error` and `timed_out=True` rather than hanging. Per project
  practice a regression guard has to be demonstrated red against pre-fix code,
  otherwise it certifies nothing.
- **`timed_out` is set on all four paths** listed in section 6, and the
  converted call sites still produce their original messages — assert on the
  `RuntimeError` text in `link/manage.py` and on the warning + `unreachable`
  bookkeeping in the tunnel paths.
- **CLI help.** No test currently asserts `--timeout` help text. Add one
  asserting `[default: 30.0]` appears in `otto host <id> run --help`, so the
  introspection path that delivers it for free cannot silently regress.
- **Session survival.** `test_run_timeout.py` and `test_session.py` already
  assert the shell recovers after a timeout (Ctrl+C plus `confirm_live`).
  Those must keep passing unchanged — the recovery mechanism is untouched.

## Risks

**Multi-command callers change behavior.** A default `run([...])` is now
bounded cumulatively at 30s. Accepted per above: otto's internals are the only
callers, and the sole multi-command site already passes an explicit timeout.
Worth restating in release notes, because a downstream suite doing
`host.run([...ten commands...])` would newly see budget-exhausted skips.

**Legitimately slow single commands now fail.** Any internal command that
routinely exceeds 30s and does not pass an explicit timeout starts erroring.
The implementation plan should grep for long-running commands — package
installs, image pulls, coverage dumps — and give them explicit values. Several
already have them (`coverage/fetcher/embedded.py:112` at 120s,
`coverage/merge/merger.py:260,308` at 300s, `coverage/fetcher/remote.py` at
60s), which suggests coverage is good but is not proof.

**Narrowing the annotation is a source-compatibility break for downstream
suites.** Any consumer that passes `timeout=None` — including one written
against today's API where `None` was the documented way to say "no limit" —
becomes a type error in their tree and now raises `TypeError` at runtime rather
than being silently coerced. This is a louder break than the earlier
coerce-quietly draft, and deliberately so: a suite that asked for unbounded
execution should be told, not have its timeout silently changed underneath it.
The migration is a one-liner and the capability is not actually lost —
`timeout=None` becomes `timeout=float("inf")` — which makes this a rename of an
existing behavior rather than a removal. Still needs a release note.

**The `exec` → `_exec_one` rename touches four subclasses.** Mechanical, but
`exec` is part of the `Host` protocol, so the protocol keeps `exec` while
implementations move to `_exec_one`. Any external caller or test that
subclassed a host and overrode `exec` would silently stop being called —
`tests/` must be grepped for host subclasses defining `exec`.
