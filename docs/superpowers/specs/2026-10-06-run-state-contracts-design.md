# Run-state contracts — design (spec 2 of the #590 contract-first series)

**Status:** v6, **approved by the owner 2026-10-06**, on Astra's agreement with all six rulings below (two with the caveats folded into §3.1 and §3.3). **Date:** 2026-10-06. **Amended 2026-10-09 (owner):** `OttoContext`'s constructor drops `dry_run`, `log_command_output` and `output_dir`; `policy=` is the only way to set them on a hand-built context (§2, §3.1, §6, §7).

**Terms used below.** A **boundary** is a counted hold on a loop's host cleanup: `open_context`,
`run_command` and each pytest runner take one (R-2, §3.3). A **sweep** (or **drain**) closes every
host registered on a loop; a loop is **draining** while one runs. A host's **generation** is its
connection epoch, bumped on every reconnect (§3.3). **Preparation** is where a run installs its
policy before the lab is built (§3.1).

- v1 had an Opus review, verdict "not ready": one critical finding (what policy a
  hand-built context gets, now §3.1) and 25 others, all folded into v2. None reopens
  R-1..R-7.
- v2 had an Astra (Codex) review, verdict "not ready": five important findings and one minor, all
  folded into v3.
  - a close left running past its deadline must never touch a later connection (§3.3, "Generations");
  - the registry needs a draining protocol (§3.3, "One sweep per loop");
  - commit 4 cannot use commit 5's boundaries (§3.1, §6);
  - moving `scopes` into `otto.config` would add a `config → session` edge (§2);
  - the docker conveniences cannot work without a context (§4);
  - the registry guard must allow session-owned hosts (§7).
- Astra's re-check of v3 resolved four of those and left two partial, with five
  important findings. v4 answers them by simplifying, not by adding rules:
  - a new boundary never runs alongside a drain: it waits for the drain to end, and one
    requested from inside the sweep is refused. No release can then meet a running sweep, and
    no new holder's host can be closed under it (§3.3);
  - the generation is the host's connection epoch, bumped by every `rebuild_connections` as
    well as every claim, and it guards every host-state change a close makes after an await,
    including the docker host's run-channel binding (§3.3).
- Astra's second re-check (of v4) confirmed all five resolved and raised three
  important findings in how a sweep meets hosts reconnected during it. v5 drops the sweep's
  rounds: it works from the live registry at every step, abandons the whole registry at once on
  expiry, and admits waiting acquisitions together when it ends (§3.3).
- Astra judged v5 ready. A Fable final review found nothing unsound and fourteen
  clarity and completeness gaps, folded into v6: the sync and waiting boundary entry points,
  where the generation lives (a `BaseHost` field; `rebuild_connections` becomes a final template
  over four family overrides), the CLI's policy flow, the full commit 5 test-migration list, and
  what the registry guard checks in the unit tree.
- **Rulings since v1 that the owner should see:**
  - `set_variant` refuses while a context is installed (§3.1);
  - `ctx.sweep_loop` refuses while a boundary holds the loop (§3.3);
  - under `otto test`, an `open_context` inside a test defers its sweep to its runner loop's end
    (the session's, by default), a
    direct consequence of R-2 (§3.3);
  - container auto-start asks `otto.docker` instead of taking an injected callback (§4);
  - an `open_context` entered while its loop is closing hosts waits for that to finish, and one
    entered from a host's close is refused (§3.3).
  - with no run policy installed, the teardown deadline is 10.0 rather than
    `OTTO_TEARDOWN_DEADLINE`; every CLI, `open_context` and library `run_tests` path installs
    one, so bare library host use (for example `as_user`'s cleanup) and tests meet this (§3.1).

**Amends:** spec 1 (`2026-10-04-public-api-manifest-design.md`) and its appendix. It settles the
pending seam spec 1 §7 assigns to spec 2: "run state (`otto.context`)". It also answers the
questions spec 4 (`2026-10-06-repo-and-scope-inputs-design.md`, S-5 and §11) left to spec 2.
§10 lists every amendment.

**Series.** Spec 2 follows spec 4's rules (S-1): the public-path delta lands in P1, and contract
changes land after P1 as their own marked commits, judged by the live API dump.

**Where the design came from.** The #590 target-architecture draft proposed a `RunPolicy`, loop
scopes and a host resolver. Codex (Astra) said in its review of that draft (finding C3) that the
host layer reads the context for three separate things and needs three separate contracts. Asked
again for this spec, Astra recommended the shape the owner adopted (R-1), with loop ownership
taken out of contexts entirely.

## 0. Owner decisions (2026-10-06)

| # | Decision |
|---|---|
| R-1 | **Three narrow contracts, with loop ownership independent of contexts** (Astra's option "A′": loop ownership outside contexts). `RunPolicy`, `HostResolver` and a loop-owned registration service live in a leaf module the host layer may import. `OttoContext` composes them; `get_context()` and `OttoContext` stay fully typed and public. Loops, not contexts, own host registrations. |
| R-2 | **Counted cleanup boundaries.** `open_context`, `run_command` and each pytest runner loop acquire a boundary on their loop. An inner exit does not sweep while an enclosing boundary on the same loop is open; the last release sweeps; a runner's shutdown always sweeps before its loop closes. |
| R-3 | **The public surface of `otto.context`** as in §2 (approved as design section 1). |
| R-4 | **The contracts** as in §3 (approved as design section 2). |
| R-5 | **The repos are full-context run state**, read where orchestration happens and passed down. The host layer never reaches them (§4, approved as design section 3). |
| R-6 | **The CLI's reset tokens are owned by the invocation**, through Click's `call_on_close`, not by module globals (§5). |
| R-7 | **Sequencing** as in §6 (approved as design section 4). |

## 1. What exists today

Evidence: a read-only survey at `d0839893` (src is unchanged at this spec's base), Astra's
review of the options, and an Opus review of v1.

- **`otto.context` is one 1,350-line module with no `__all__`.** At module level it imports one
  otto name, `DEFAULT_COMMAND_TIMEOUT` from `otto.host.host`, so importing it imports the host
  layer. The root package re-exports `OttoContext`, `get_context`, `try_get_context` and
  `open_context`.
- **Run state lives in four places:**
  - the `_active` ContextVar, holding the `OttoContext`;
  - the `_variant` ContextVar, kept separate because a lab is ingested before any context exists
    and ingest reads the variant;
  - two module globals, `_cli_token` and `_variant_token`, where the CLI parks reset tokens;
  - the bootstrap singleton for the repos (spec 4).

  There is no "policy" object. The per-run flags (`dry_run`, `log_command_output`,
  `output_dir`) are fields of `OttoContext`. The teardown deadline is read from the environment
  in three places: `lifecycle._resolve_teardown_deadline`, `open_context`, and
  `suite/loops.py`.
- **Every read of the context below it.** All are function-scope imports of `otto.context`:
  - **run policy:**
    - `get_logging_command_output_enabled` and `is_dry_run` (`host/host.py`);
    - `SuppressCommandOutput` in its no-host form, which flips `ctx.log_command_output` on the
      captured context and restores it (`host/host.py`);
    - the session-log destination, `ctx.output_dir` (`host/host.py`, `host/interact.py`);
    - the variant (`host/listing.py`, and `declared.py` outside the host package).
  - **loop ownership:** `BaseHost._claim_loop` registers the host with
    `try_get_context().scope_for(loop)`.
  - **host resolution:** `RemoteHost` resolves a hop, and `PowerController` its controller host,
    through the host's `_lab` back-reference, else the active context's lab.

  These reads are why `otto.host ↔ otto.context` is a mutual pair in the cycle.
- **Three defects follow from that shape:**
  - **A host registers once, with whatever context is active.** `_claim_loop` returns early when
    the host already owns this loop, so it registers only on the first claim. A host first used
    inside a nested `open_context` registers with the inner context, and the inner exit closes it
    although the outer context still uses it.
  - **A host connected with no context active is never registered,** so nothing sweeps it.
  - **The CLI's tokens leak across invocations in one process.** Only the root test conftest
    (`_reset_otto_context`) restores them.

## 2. The public surface of `otto.context`

**Module-level names after P1** (the module's first `__all__`):

| Name | Disposition |
|---|---|
| `OttoContext`, `get_context`, `try_get_context`, `open_context`, `set_context`, `reset_context`, `variant`, `set_variant`, `reset_variant` | declared in P1, as appendix F already lists |
| `Variant` | declared in P1: it is the type of `set_variant`'s argument and `open_context(variant=)` |
| `ProjectContextView` | declared in P1: it is what the taught `ctx.for_repo()` returns |
| `RunPolicy`, `HostResolver`, `ContextBinding` | declared at `otto.context` by the commit that creates them (commit 4, after P1). Additions are free; P1 cannot declare names that do not exist yet. Their implementation lives in the leaf `otto.invocation`, which has no public path |
| `set_cli_context`, `set_cli_variant`, `reset_cli_context` | deleted before P1 (§5, commit 2). They are CLI plumbing with no taught use and no golden line |
| `HostScope`, `LIBRARY_LAB_NAME`, `VARIANTS` | not declared. `HostScope` is deleted in commit 5 (§3.3) |

`Variant` and `VARIANTS` move to `otto.invocation` in commit 4, and `otto.context` re-exports
`Variant`, so the rendered annotations of `RunPolicy` resolve. The public path does not change.

**`OttoContext` members.** P1 renames none of them (spec 1 Q4: no narrowing in the cutover).
`src/` lint selects every rule, `SLF001` included, so a member that other otto modules read cannot
simply become private. After P1, spec 2's contract commits change these, each one marked:
- **`dry_run`, `log_command_output`, `output_dir`** become read-only properties backed by
  `ctx.policy` (commit 4). The policy is their one writable home: `ctx.policy.dry_run = …`.
  - The class keeps `@dataclass`, but takes a hand-written `__init__`:
    `OttoContext(lab, *, cov_decision=None, include_projects=(), exclude_projects=(),
    policy: RunPolicy | None = None, bootstrap: BootstrapResult | None = None)` (§4 for
    `bootstrap`).
  - **Owner decision, 2026-10-09.** The three flag parameters are gone from the constructor.
    `policy=` is the only way to set them on a hand-built context, so
    `OttoContext(lab, dry_run=True)` raises `TypeError` (an unexpected keyword), with or
    without `policy=`. There is no refusal logic and no sentinel default. Commit 4 migrates
    every caller that passes a flag keyword to `policy=RunPolicy(...)`.
  - The parameters after `lab` are keyword-only. Dropping the flags would otherwise move
    `cov_decision` into second place, and a positional call written for the old order
    (`OttoContext(lab, True)`, meaning dry-run) would turn coverage on and run live.
  - The docstring that says the dataclass "stays plain" is rewritten: normalization of
    `include_projects` and `exclude_projects` still happens on read.
  - `repr` and equality change with the field set. `dataclasses.replace` on a context is not
    supported (no caller uses it), and the docstring says so.
- **`scope_for` and `abandon_closed_loops`** leave `OttoContext` (commit 5). Loop ownership moves
  to `otto.invocation`. `sweep_loop` stays public; §3.3 gives its new rule.
- **`scopes`** leaves the public class (commit 6). It returns `ProjectScope`, which spec 4 keeps
  internal, and a public member should not hand out an internal type. Its ten code reads (in
  `context.py`, `config/scope.py`, `session/activation.py`, `project/orchestrator.py` and
  `monitor/live.py`) call `otto.config.scope.scopes_of(ctx)` instead.
  - **The computation stays at the context layer.** `OttoContext` keeps it in a private method,
    `_resolve_scopes()`, which caches the verdicts and spec 4's refusal in a private field. It
    calls spec 4's classifier in `otto.session` (`context → session` is a baseline edge) over the
    context's one `BootstrapResult` (§4).
  - `scopes_of(ctx)` is the single entry point for readers. It calls `ctx._resolve_scopes()`
    with one justified `# noqa: SLF001`; the codebase already carries justified `SLF001` lines in
    `context.py`. So `otto.config` gains no import of `otto.session`, which would be a new
    in-cycle edge, and no reader gains an import of `otto.context`.
  - Tests that assign `ctx.scopes = …` or build fakes with a `.scopes` attribute migrate to a
    test helper, `seed_scope_verdicts(ctx, verdicts)` in `tests/unit/conftest.py`, that seeds the
    private cache (§7, commit 6 list).
- **Additions:** `policy` (commit 4), `repos` and `ordered_repos` (commit 6).

Everything else stays public-provisional and unchanged:
- **taught:** `lab`, `get_host`, `options`, `cov`, `all_hosts`, `do_for_all_hosts`,
  `run_on_all_hosts`, `for_repo`, `sweep_loop`;
- **read across otto modules:** `admissible_ids`, `verb`, `bind_verb_options`,
  `verb_binding_preserved`, `verb_option_source`, `cov_decision`, `include_projects`,
  `exclude_projects`. This answers spec 4 §11: `admissible_ids` keeps its contract.

The private alias `_admissible_ids`, "kept for one release", is deleted in commit 6.
`context.py`'s import of the private `config.scope._not_switched_off` stays, because the
computation stays in `context.py`. It is an existing import on a baseline edge, and spec 5 owns
where the scope computation finally lives (spec 4 §11).

**What the dump sees.** Commit 4's dump diff shows three member kind changes (`field` →
read-only property), a changed `__init__` call record (the three flag parameters gone, the rest
after `lab` keyword-only, `policy` and `bootstrap` added), plus the added `policy` member.
Commits 5 and 6 show the removed `scope_for`, `abandon_closed_loops` and `scopes`, and the added
`repos` and `ordered_repos`. Each of those commits is marked.

## 3. The contracts (leaf module `otto.invocation`)

`otto.invocation` has no runtime dependency on any otto module: annotations only, under
`TYPE_CHECKING`, except `Variant` and `VARIANTS`, which it defines. Its readers import it inside
the functions that use it, as they import `otto.context` today, so no import surface grows (§8).
Its process-wide getters (`current_policy`, `installed_policy`, `installed_resolver`) join spec
4's lazy-getter guard set, so a module-level binding that would dodge test patches fails the
guard.

### 3.1 `RunPolicy`: what this run does, read live

- **Fields:** `dry_run: bool = False`, `log_command_output: bool = True`,
  `output_dir: Path | None = None`, `variant: Variant = "debug"`,
  `teardown_deadline: float = 10.0`. These are today's defaults (`OttoContext`'s fields,
  `_variant`'s default, `lifecycle.DEFAULT_TEARDOWN_DEADLINE`).
- **A mutable dataclass**, because `SuppressCommandOutput()` flips `log_command_output` on the
  installed object.
- **One ContextVar holds the installed policy.**
  - Readers (`current_policy()`) always get an answer: the installed policy, or a **fresh**
    default policy when none is installed. No two callers ever share a default object.
  - Writers ask for the installed policy (`installed_policy() -> RunPolicy | None`) and treat
    `None` as nothing to change.
- **What a context's policy is:**
  - `OttoContext(..., policy=p)` uses `p` as given.
  - `OttoContext(...)` without `policy=` gets a new `RunPolicy`. Its `variant` and
    `teardown_deadline` are copied from `current_policy()`: those two were ambient before this
    spec (the variant was its own ContextVar, the deadline came from the environment). Its
    `dry_run`, `log_command_output` and `output_dir` are `RunPolicy`'s defaults (`False`,
    `True`, `None`); a hand-built context that needs others passes `policy=RunPolicy(...)`.
    A fresh context never inherits another context's flags, as today.
  - That keeps the documented manual pattern (`python-library.md`) working:
    `set_variant("field")`, then `OttoContext(lab=lab)`, then `set_context(ctx)` runs with
    `"field"`. So do the library `run_tests` sentinel context and remote completion's contexts.
- **Preparation.** The CLI root callback and `open_context` install a policy before the lab is
  built, because ingest reads the variant (`KindRegistry.build` picks same-name entries by it).
  - They install the variant and flags **first**, as `open_context` installs the variant first
    today, so a bad variant refuses before anything needs undoing.
  - They fill `teardown_deadline` **after** discovery, from `get_env().teardown_deadline`. The
    policy is mutable, so this is safe. If discovery fails, the deadline stays 10.0, as
    `_resolve_teardown_deadline` falls back today.
  - The library `run_tests` is a third preparation point: before it builds its sentinel context,
    it installs a policy whose deadline it fills the same way. Today its runner sweep reads
    `OTTO_TEARDOWN_DEADLINE` itself (`suite/loops.py`), so that setting keeps working.
    When a context is already active, `run_tests` builds none: it writes its log directory to
    `active.output_dir` when that is unset, and restores the prior value on the way out, as it
    restores `cov_decision` (`suite/run.py`). The property is read-only, so both writes go
    through `active.policy.output_dir`.
  - The context built afterwards carries the same object as `ctx.policy`. On the CLI that means:
    the root callback puts the variant and `dry_run` (today's only root flag) on the policy it
    installs; `ensure_lab_context` builds `OttoContext(..., policy=installed_policy())`; and the
    later write in `create_output_dir` becomes `get_context().policy.output_dir = …`, on the
    same object (the context's properties are read-only). `set_context` installs that object
    again, which changes nothing.
  - Preparation installs its policy directly through `otto.invocation`. It never calls the public
    `set_variant`, so a nested `open_context` is not refused (today it calls `set_variant`).
- **Who reads the teardown deadline (commit 4).** Every caller of
  `lifecycle._resolve_teardown_deadline` reads `current_policy().teardown_deadline` instead:
  `run_command` (when no explicit `teardown_deadline=` is given), `sync_phase`, `compensate`
  (when its `deadline` is `None`), and the runner sweep in `suite/loops.py`. The helper is then
  deleted, and the test that patches it (`tests/unit/suite/test_loop_sweep.py`) installs a policy
  instead.
  - With no policy installed, these read 10.0 rather than `OTTO_TEARDOWN_DEADLINE`. This is a
    marked change. Every CLI, `open_context` and library `run_tests` path installs a policy, so
    only bare library use and tests meet it: for example, `as_user`'s undo calls `compensate`
    with no deadline (`host/privilege.py`), so outside any context it is bounded by 10.0.
  - Commit 5's boundaries then record the deadline when acquired (§3.3), from the same policy.
- **One source of truth for the variant.**
  - `variant()` reads `current_policy().variant`.
  - `set_variant(v)` validates `v`. **It raises `RuntimeError` while a context is installed.**
    Otherwise it installs a copy of the current policy with that variant and returns a
    `ContextBinding` (§3.4) holding one token; `reset_variant(binding)` restores the previous
    policy.
  - Why the refusal: inside a context, a copied policy would split `ctx.policy` (which the
    context's properties read) from the installed policy (which hosts read), and a
    `SuppressCommandOutput` interleaved with it would leak or end early. Its remaining public
    use is the manual pattern's step 4, which is pre-context. From commit 4 the CLI root callback
    and `open_context` install their policy through the leaf instead (see "Preparation"; §5). To run under another variant mid-run, open a nested
    `open_context(variant=…)`.
  - `open_context(variant=None)` inherits the current value.
  - A supplied `Lab` keeps the products it was built with; changing the variant later does not
    rebuild them. `open_context`'s docstring already says so.
- **Never captured.** A host reads the policy when it acts, never at construction. Hosts outlive
  contexts (`get_host` returns one shared instance), so a captured value would go stale.
- **`SuppressCommandOutput()` keeps today's semantics.** On enter it captures the installed
  policy and flips `log_command_output`; on exit it restores that same object. It does nothing
  when no policy is installed. As today, the flip is shared across tasks, not isolated per task.
  `get_logging_command_output_enabled` and `is_dry_run` survive as leaf readers.
- **The session-log destination is unchanged:** an explicit destination, else
  `policy.output_dir`, else the working directory. An interactive session writes no log without
  an output directory.

### 3.2 `HostResolver`: looking up a peer host

- **What it is:** a `typing.Protocol` with a read-only `name: str` and
  `hosts: Mapping[str, Host]`. `Lab` satisfies it structurally. It offers no fleet selection, no
  overrides, no repos and no context.
- **Precedence, unchanged:**
  1. The host's own `_lab` back-reference, set by `Lab.add_host`.
  2. The installed resolver.
  3. Today's errors, unchanged: `RemoteHost`'s names `Lab.add_host` and `open_context`;
     `PowerController`'s says the controller was not found in the lab.

  An attached host is never silently re-pointed at a nested context's lab.
- **The installed resolver reads `ctx.lab` live.** `set_context` installs a small adapter over
  the context, not a snapshot, so a later `ctx.lab = …` is seen. Lookups stay live in the lab's
  mapping, as today.

### 3.3 Loop-owned registrations and cleanup boundaries

- **One registry per event loop,** in a plain dict keyed by loop. Weak keying is not relied on: a
  record holds its host, and the host holds its loop. **Every registration prunes the
  registries of closed loops,** only forgetting them, as `scope_for` prunes today.
- **A host registers when it claims a loop,** context or not: `_claim_loop` registers with the
  loop's registry instead of `try_get_context().scope_for(loop)`. The cross-live-loop refusal
  (`HostLoopError`) and closed-loop reconnection are unchanged.
- **The registration record** is built by the host, so the leaf never reaches into `BaseHost`
  internals. It carries:
  - an identity key and a display id;
  - the keys it depends on (today's `parent` ranking: a docker exec channel closes before its
    parent transport);
  - `close: Callable[[], Awaitable[None]]`;
  - an ownership check: is this still the loop that owns the host;
  - an abandon callback, which drops dead connections with no I/O;
  - the host's **generation** when it registered (below).
- **Generations: a close never touches a later connection.** Today a sweep that runs past its
  deadline drops the stuck hosts' connections, but a close that refuses cancellation keeps
  running. If the host reconnects meanwhile, that old close can close the new connection's
  manager (`RemoteHost._close` reads `self._connections` after an await) or clear the new owner
  (`BaseHost.close` clears `_owner_loop` in its `finally`). Commit 5 closes this hole:
  - **The generation is the host's connection epoch.** It is bumped whenever the host's
    connection managers are replaced (`rebuild_connections`, which is public and also runs on the
    owning loop, for example after a reboot or from `otto host`) and whenever `_claim_loop` takes
    ownership. Abandonment replaces the managers through the same path, so it bumps it too.
  - **Where it lives.** The generation is a `BaseHost` field. `rebuild_connections` becomes a
    `@final` `BaseHost` method that bumps the generation, re-registers when the host has an owner
    loop, and calls the family's `_rebuild_connections`. Today `_drop_dead_connections` reaches the
    method by `getattr`; the four overrides (`unix_host.py`, `local_host.py`, `docker_host.py`,
    `embedded_host.py`) are renamed to `_rebuild_connections`, so no family can forget the bump.
    A host family with nothing to rebuild inherits a no-op.
  - **One record per host, at its current generation.** A rebuild on a host that still has an
    owner loop re-registers it at the new generation, replacing the old record, without changing
    the owner. Abandonment clears the owner **before** it rebuilds, so it removes the record and
    leaves none behind. A close that completes removes the record only if the record still
    carries the generation that close started with.
  - **Every host-state change a close makes after an await is guarded.** `BaseHost.close()`
    captures the generation on entry, and when `_close` returns or raises it clears `_owner_loop`
    only if the generation is unchanged. Each family's `_close` (`remote_host.py`,
    `local_host.py`, `docker_host.py`) takes the managers it will close into locals before its
    first await, and makes every later change to host state only under the same check. One such
    change exists today: `DockerContainerHost._close`'s (`docker_host.py`) `finally` calls `_forget_run_channel_binding()`,
    which clears the user the run channel is bound to. Unguarded, an old close could erase a
    later channel's binding, and a command asking for another user would pass the mismatch
    refusal and run on the wrong user's shell.
  - Abandonment acts only when the record's generation is the host's current one and the host's
    owner is that loop or none.
  - Tests, for each concrete host family: an old close that resumes **after** a reconnect,
    whether the reconnect followed abandonment or a same-loop `rebuild_connections`, leaves the
    new managers and owner untouched; on `DockerContainerHost` it also leaves the run-channel binding, so
    the user-mismatch refusal still fires. A stale abandonment is a no-op.
- **Sweeping** keeps today's guarantees:
  - dependency order;
  - a failed close is isolated and logged;
  - bounded cancellation within the deadline, with the same grace;
  - progress reporting;
  - abandonment on expiry (below).
- **One sweep per loop, and no new boundary during it.** A loop's registry is `open`,
  `draining` or `shut`.
  - **Entering `draining` is atomic:** before its first await, the sweep marks the registry. At
    most one sweep runs per loop.
  - **The sweep works from the live registry, not a snapshot.** It has no rounds. At each step it
    starts closing every outstanding record that no other outstanding record depends on, and it
    recomputes that set from the registry whenever a close finishes or a record changes. So a
    close that reconnects a host registers it into the same sweep, and a reconnected child (a
    docker channel on its parent's transport) is closed before its parent, whichever step
    reconnected it. A parent whose close has not started waits for every current dependent; one
    already closing when a dependent registers is a close the dependent must survive, as today.
  - **It ends when the registry is empty or its deadline expires.** On expiry, in one step with
    no await, it abandons every record outstanding in the registry, whenever it registered: each
    abandon callback acts only if the host's generation still matches the record's and its owner
    is that loop or none, and the record is removed either way. No record outlives an expired
    sweep, and abandonment registers none.
    - **It also abandons the closes it cut short.** A close whose task the expiry cancelled, or
      that is not done, is abandoned at its record's own generation, even though the close's
      `finally` may already have unregistered that record: the close's connections are still
      half-closed. A cut record that a newer registration of the same host replaced is left to
      the newer record's abandonment.
    - Abandonment is generation-checked, so a stale one (the host has reconnected since) is a
      no-op.
    - **The expiry warning names only the abandons that acted,** never a stale no-op.

    The registry then returns to `open`.
  - **An acquisition on a `draining` loop waits for the drain to end.** Only `open_context` can
    meet one: it acquires from a task on the running loop, so it awaits. The pytest
    runners acquire before their loop runs, and `run_command` as its fresh loop starts (see
    "Boundaries"); no sweep can be in progress at either point.
  - **Waiters are admitted together, so none starves.** When a drain ends, before any other task
    runs, it grants every waiting acquisition in arrival order: each one is counted as held at
    that moment. Since the count is then non-zero, no new drain can start until those holders
    release. A waiter cancelled after its grant gives the grant back as an ordinary release. One
    cancelled before its grant leaves the queue.
  - **The wait is bounded** by the running sweep's own deadline and grace. A
    `ctx.sweep_loop(..., deadline=None)` that its caller left unbounded is the one exception, by
    that caller's choice.
  - **An acquisition from inside the sweep raises `RuntimeError`.** The sweep sets a ContextVar
    that the tasks it spawns inherit, so a close callback that enters `open_context` is refused
    instead of waiting on the sweep that is waiting on it. `ctx.sweep_loop` on a `draining` loop
    raises `RuntimeError` for the same reason.
  - **Consequently no boundary is ever held while a loop drains** (except at a runner's shutdown,
    below), so no release can meet a running sweep, and no new holder's host is closed under it.
    By the time a waiting `open_context` proceeds, the drained hosts are closed or abandoned, and
    its next use of one reconnects it.
  - **What it does not cover:** a task that keeps using a host on that loop during a drain,
    without a boundary of its own, races the close, as it does today. Its next use reconnects.
  - **A runner's shutdown** moves the registry to `shut`. It sweeps every record whatever the
    count. A later acquisition raises, and a later release sweeps nothing.
- **Boundaries (R-2):**
  - **Two entry points, one rule.** `acquire_boundary(loop, *, deadline)` is synchronous. It
    takes the loop explicitly, because a pytest runner acquires before its loop runs, and it raises
    `RuntimeError` if the loop is `draining`, `shut` or closed. `run_command` keeps handing loop
    creation to `asyncio.run` (`lifecycle.py`); it acquires first thing in the coroutine it hands
    over, before the command body starts. A fresh loop has no sweep in progress, so this
    synchronous entry point never meets a drain there.
    `await acquire_boundary_waiting(loop, *, deadline)` is the only waiting path, and only
    `open_context` uses it: it waits out a drain (the admission and cancellation rules above apply
    to it) and raises on the same `shut` or closed loop. Both return the boundary that `release()`
    gives back.
  - **A boundary records its deadline when acquired:** `run_command`'s explicit
    `teardown_deadline=` if given (remote completion pins 2.0 s), else
    `current_policy().teardown_deadline`.
  - `open_context`, `run_command` and each pytest runner loop acquire one.
  - Releasing a boundary that is not the last one sweeps nothing. The last release sweeps,
    bounded by that boundary's recorded deadline. A runner's shutdown sweeps before its loop
    closes, whatever the count.
  - **Release is exactly once.** The count drops synchronously, before the first await, so a
    release cancelled during its sweep still counts. Releasing a boundary twice raises.
  - **On exit, `open_context` releases its boundary, and so sweeps, before it restores its
    policy and resolver,** as it sweeps before `reset_context` today. A lone `open_context`
    therefore sweeps under its own environment-derived deadline.
  - **This is a behaviour change:** hosts a nested context opened live until the outer boundary
    exits. Before, the inner exit closed them, including hosts the outer context still used.
  - **Under `otto test`** each runner loop holds its boundary for its whole loop scope. The
    default scope is the session (`ASYNCIO_LOOP_ARGS`); a test or fixture that asks for a
    narrower `loop_scope` gets its own runner (`suite/loops.py`). So an `open_context` inside a
    test leaves its hosts open at its exit, and that runner's shutdown closes them: at the
    session's end by default, sooner for a narrower scope. Commit 5 states
    this in `docs/cookbook/host-scopes.md`.
- **Surviving public entry points:**
  - `ctx.sweep_loop(loop, ...)` sweeps every registration on that loop now. It is meant for
    callers who drive their own loop with a hand-built context and no boundary. **It raises
    `RuntimeError` while any boundary holds that loop**, because the boundary owns that loop's
    cleanup, and a sweep there would close an enclosing context's hosts, which is the defect R-2
    fixes. It also raises while the loop is `draining`.
  - `abandon_closed_loops()` becomes an `otto.invocation` function; the suite calls it at session
    end as its backstop.
- **`HostScope` is deleted** in commit 5. Its sweep logic is the registry's. The architecture docs
  that link it re-point to `otto.invocation`'s internal API entry.
- **Unmanaged loops:** a host connected on a loop the caller drives, with no boundary, is
  registered but not swept for them. Such a caller opens `open_context`, calls `ctx.sweep_loop`,
  or closes its hosts (the `async with` host pattern the docs teach). Commit 5 states this in
  `docs/cookbook/host-scopes.md` and `docs/cookbook/python-library.md`.

### 3.4 `ContextBinding`: installing a context

- `set_context(ctx)` installs three values: the context, `ctx.policy`, and a resolver adapter
  over `ctx`. It returns a `ContextBinding` holding the three tokens.
- `reset_context(binding)` resets them in reverse order, exactly once. A second reset raises.
- As with a raw token, a binding must be reset in the execution context that set it; otherwise
  it raises.
- `set_variant` returns a `ContextBinding` holding one token (§3.1). It is the same type, and
  `reset_variant` takes it.
- The documented pattern `token = set_context(ctx) … reset_context(token)` is unchanged.
- `set_context` acquires no boundary: a hand-built context sweeps with `sweep_loop`, as today.
- `get_context() -> OttoContext` and `try_get_context() -> OttoContext | None` keep their types.

### 3.5 What the layer below the context imports afterwards

- **After commit 4:** the host layer reads policy, variant, log destination and resolver through
  `otto.invocation`. `_claim_loop`'s registration still reads the context. It moves with the
  registry in commit 5.
- **After commit 5:** no module under `otto/host/` imports `otto.context` at runtime, and tach
  drops `host → context`.
- **`declared.py`** reads the variant through `otto.invocation` from commit 4. `otto.declared`
  is not a tach module, so neither tach nor the ratchet sees its imports. A static test pins
  them: `declared.py` imports `otto.invocation`, function-scope, and never `otto.context`.

## 4. The repos as run state, and spec 4's S-5 edges

- **`ctx.repos`** (every parsed repo) and **`ctx.ordered_repos`** (dependency order, skipped
  repos absent) are typed, read-only views. A list of `Repo` objects is not deeply immutable; the
  views promise the membership and order only.
- **One snapshot.** The context holds one `BootstrapResult` privately. `repos`, `ordered_repos`
  and spec 4's fatal-error classifier all read that one result, so they never disagree after an
  `invalidate()`.
- **Filled by preparation.** The CLI and `open_context` pass the bootstrap result they already
  hold, through the keyword-only constructor parameter `bootstrap=` (§2). `BootstrapResult` is
  public at `otto.bootstrap` (spec 4 §2).
- **Hand-built contexts acquire it lazily,** at the context layer, on first read, by calling
  `otto.bootstrap.bootstrap()`. The outcome is cached, refusal included. "Known empty" (no SUT
  directories) stays distinct from "unavailable" (discovery raised). Spec 4 §4's rules carry over
  unchanged, now resolved over `ctx.repos`.
- **The library sentinel lab.** `ctx.repos` on the sentinel still bootstraps lazily, so library
  `run_tests` coverage keeps its repos. Only `scopes_of` and `_detect_cov` special-case the
  sentinel, before reading `ctx.repos`.
- **The patch seam.** Spec 4's tests patch `otto.bootstrap.get_repos`. After commit 6 the lazy
  read calls `bootstrap()`, so those tests migrate to building their context with `bootstrap=` a
  test result. That is no patch at all, and commit 6 names each test.
- **Nothing a host can read carries repos:** not `RunPolicy`, not `HostResolver`, not the loop
  registry.

**S-5's seven edges** were added in P1 by spec 4, when the accessors moved to `otto.bootstrap`.

| Module | Replacement | Commit | Result |
|---|---|---|---|
| `otto.lifecycle` | every deadline reader (`run_command`, `sync_phase`, `compensate`, the runner sweep) reads the installed policy, and `_resolve_teardown_deadline` is deleted (§3.1, "Who reads the teardown deadline"). Commit 5's boundaries take their deadline from the same policy | 4 | `→ bootstrap` retired |
| `otto.coverage` | reads `ctx.repos` where it orchestrates (`collect`, `get`, `instrumentation`, `_detect_cov`) and passes lists down | 6 | `→ bootstrap` retired; `→ context` exists already |
| `otto.monitor.live` | reads `ctx.repos` for D3 and TLS | 6 | same |
| `otto.project` | `plan` and the orchestrator read `ctx.ordered_repos` / `ctx.repos` | 6 | same |
| `otto.suite` | `run_tests` sets up or inherits its context **before** it selects repos (today it reads them first), then reads `ctx.repos` | 6 | same |
| `otto.docker` | the public conveniences (`build_on`, `deploy`, `teardown`, `deployed`) keep their signatures and **still require a context**, as today: they resolve the lab through `get_lab()`, which is `get_context().lab`, before they need repos. They read the repos through an internal `otto.config.fleet` accessor: `ctx.repos` when a context is active, else the bootstrap result. `docker → config` is a baseline edge, so commit 6 adds none (P1's move of `get_lab` to `otto.lab` is spec 4's approved S-5 re-baseline, not this commit's). The accessor and the declared-use-case query (next row) are what work without a context. Internal helpers take the repos explicitly | 6 | `→ bootstrap` retired |
| `otto.host` | `docker_host.py`'s auto-start asks `otto.docker` whether `self.project` is a declared use-case; docker answers through the same accessor. `host → docker` already exists, so no edge is added and no callback joins the host's construction | 6 | `→ bootstrap` retired |

**Proof, not assumption.** Commits 4 and 6 start from an import-site inventory of every
`otto.bootstrap` read in their modules, taken at their base. A site that cannot move without
recreating an upward edge gets a composition wrapper above its module, and the commit names it.
The ratchet's `BASELINE` shrinks by exactly the edges the inventory proves gone.

## 5. The CLI's reset tokens

- **Today:** `set_cli_context` and `set_cli_variant` park their tokens in module globals, because
  the install points (the root callback in `cli/main.py`, `ensure_lab_context` in
  `cli/invoke.py`) and the reset (`entry()`'s `finally`) share no stack frame. Invoking `app()`
  directly, or through `CliRunner`, never reaches `entry()`, so only the test conftest restores
  them.
- **After commit 2:**
  - The root callback sets the variant and registers its reset with
    `click_ctx.find_root().call_on_close(...)`. `ensure_lab_context` installs the context and
    registers its reset the same way.
  - Click closes its root context once, when the invocation ends, for the console script and for
    `app()` / `CliRunner` alike, on the same thread and in the same execution context. It runs
    close callbacks last-in first-out and runs every one even if one raises. So each reset runs
    exactly once, where its token was made, and the context resets before the variant.
  - `entry()`'s error handling reads no run state, so resetting at Click's close, before
    `entry()`'s handler runs, is safe.
  - `_cli_token`, `_variant_token`, `set_cli_context`, `set_cli_variant` and `reset_cli_context`
    are deleted. `entry()` no longer resets anything.
  - After commit 4, the root callback's registration covers the policy binding instead of the
    variant's.
- **Commit 2 lands before P1.** It changes no public contract: the three functions have no taught
  use, no golden line, and no caller outside `otto.cli`.
- **Out of scope:** `cli/main.py`'s `_root_log_level` is a third per-invocation global with the
  same leak. It cannot move to `call_on_close`, because `entry()` reads it after `app()` returns.
  It keeps its own conftest fixture.

## 6. Commits

| # | Commit | Lands |
|---|---|---|
| 1 | `docs(spec)`: this spec and the spec 1 amendments (§10) | now |
| 2 | `fix(cli)`: the CLI's resets run on Click's close (§5) | before P1 |
| 3 | `otto.context`'s first `__all__` (§2) | inside P1's single marked commit |
| 4 | `feat(context)!`: `otto.invocation` with `RunPolicy` (single variant source, `set_variant`'s refusal), `HostResolver`, `ContextBinding`; `ctx.policy`, the hand-written `__init__` without the three flag parameters, and the read-only properties; the host layer reads policy and resolver through the leaf; every teardown-deadline reader on the policy and `_resolve_teardown_deadline` deleted; the three names declared at `otto.context` | after P1 |
| 5 | `fix(context)!`: loop-owned registrations, counted boundaries with the draining protocol, generation-scoped closes, context-less connects registered, `sweep_loop`'s refusal; `scope_for`, `abandon_closed_loops` and `HostScope` go; `host → context` and `lifecycle → context` cut | after commit 4 |
| 6 | `refactor!`: `ctx.repos` and `ctx.ordered_repos` over one `BootstrapResult`; `scopes` → `config.scope.scopes_of(ctx)`; `_admissible_ids` deleted; S-5's remaining edges retired per the inventory | after commit 5 and spec 4's commit 4 |

The marks on commits 4–6 cover what the dump sees (§2, "What the dump sees") and the behaviour
it cannot: the policy defaults, `set_variant`'s refusal, the boundary timing, `sweep_loop`'s
refusal and the deadline change.

### 6.1 Docs, per commit

`make docs` runs with `-W`, so each commit carries its pages.

- **Commit 2:** the `set_cli_variant` role in `cli/invoke.py`'s docstring, and the comments and
  docstrings that name the deleted functions.
- **P1 (commit 3):**
  - `docs/api/context.rst` renders the public `__all__`, plus an Internals entry
    (`:ignore-module-all:`) for the names still linked from the architecture docs;
  - the `pending = "spec 2 …"` note in `scripts/api_public_preview.toml` is removed.
- **Commit 4:**
  - `docs/cookbook/python-library.md` steps 4, 7 and 10 (`set_variant` before any context; a
    hand-built context copies the variant), and its smallest-version example
    (`python-library.md:223`), whose `OttoContext(lab=lab, dry_run=False)` passes the flag
    through `policy=RunPolicy(dry_run=False)`;
  - `docs/configuration/declared-products-tools.md` (`set_variant`'s refusal inside a context);
  - the teardown-deadline row of `docs/cli/index.md`;
  - `tach.toml` gains `otto.invocation`, and `docs/architecture/modules.md` is regenerated.
- **Commit 5:**
  - `docs/architecture/lifecycle.md` (loop-owned registrations, boundaries, `HostScope`'s links);
  - `docs/cookbook/host-scopes.md` (the `otto test` consequence, the unmanaged-loop rule);
  - `docs/cookbook/python-library.md` ("Host lifetimes", `sweep_loop`'s refusal);
  - the per-context-scope sentences in `docs/architecture/subsystems/execution.md`,
    `docs/architecture/overview.md` and `docs/architecture/principles.md`;
  - the `scope_for` links in `context.py`'s docstrings; `modules.md` regenerated.
- **Commit 6:** `docs/cli/run/defaults.md` and spec 4 §4's docstring list (`scopes` →
  `scopes_of`, `ctx.repos`); `modules.md` regenerated.

## 7. Tests

- **Commit 2:**
  - two CLI invocations in one process, with the autouse reset fixture disabled, through
    `CliRunner` and through `entry()`; the second sees the defaults, not the first's variant or
    context;
  - an invocation that raises still resets.
  - **Migrations:** `tests/conftest.py`'s `_reset_otto_context` stops snapshotting the deleted
    globals (it keeps the ContextVars); `tests/unit/test_root_conftest_guards.py`;
    `tests/unit/test_context.py` and `tests/unit/test_context_variant.py`; and
    `tests/unit/cli/test_main.py`, `cli/test_session_differential.py` and
    `cli/test_preflight_gate.py`, which spy on `set_cli_context`. They capture the context through
    a replacement seam instead: the installed context, read at Click's close.
- **Commit 4:**
  - With no policy installed, readers see the defaults and `SuppressCommandOutput()` changes
    nothing.
  - The manual pattern: `set_variant("field")`, a hand-built context, `set_context`; the body
    reads `"field"`. The library `run_tests` sentinel under `set_variant("field")` also reads
    `"field"`.
  - A context built with any of the three flag keywords raises `TypeError`, with or without
    `policy=`; so does a positional flag; the three properties are read-only, and
    `ctx.policy.<flag> = …` is read live. Every caller passing a flag keyword at the commit's
    base migrates to `policy=RunPolicy(...)`.
  - `set_variant` inside an installed context raises.
  - A `--field` run's ingest sees `variant == "field"`: declared entries pick the field variant.
  - A nested `open_context` restores the outer policy on exit, including after its setup fails.
  - A shared host reads `log_command_output` live: inside `SuppressCommandOutput()` it is
    `False`; after, `True`.
  - Resolver precedence: back-reference over the installed resolver over the error; a nested
    context's lab never re-points an attached host; a later `ctx.lab = …` is seen.
  - Binding: reverse-order reset, a second reset raises, a reset in another execution context
    raises.
  - `run_command`, `sync_phase`, `compensate` and the runner sweep read the installed policy's
    deadline; with none installed they read 10.0. Library `run_tests` honours
    `OTTO_TEARDOWN_DEADLINE`. Remote completion's explicit 2.0 s still wins.
  - A nested `open_context(variant=…)` inside an installed context is not refused.
  - **Migrations (deadline):** `tests/unit/test_lifecycle.py`'s two
    `_resolve_teardown_deadline` tests move to the preparation points, and
    `tests/unit/suite/test_loop_sweep.py` installs a policy instead of patching the helper.
  - The `declared.py` static import check.
  - **Migrations:** direct `_variant` users (`tests/unit/test_declared.py`,
    `test_context_variant.py`, `tests/unit/docs/test_getting_started_example.py`); the root fixture and its
    guard test snapshot `otto.invocation`'s ContextVars.
- **Commit 5:**
  - a context-less connect under `run_command` is swept when the command ends;
  - an inner `open_context` exit does not close a host the outer context uses, and the outer exit
    does;
  - under a session-scoped runner, a test's `open_context` exit leaves its hosts open, and the
    runner's shutdown closes them;
  - a pytest runner's shutdown sweeps with no context active;
  - a boundary keeps the deadline it recorded: remote completion's 2.0 s, and a lone
    `open_context`'s environment-derived deadline;
  - `ctx.sweep_loop` raises while a boundary holds the loop or while it is `draining`, and sweeps
    without one;
  - a registration made during a sweep (a close that reconnects a peer) is swept;
  - a child reconnected by another host's close, while its parent's close has not started, is
    closed before the parent;
  - on expiry, a host re-registered at a new generation while its old close is still in flight
    is abandoned too: the registry is empty after an expired sweep, and abandonment adds no record;
  - on expiry, a close the sweep cut short is abandoned at its own generation even after its
    `finally` unregistered it; a cut record a newer registration replaced is left to that record;
    a stale abandonment is a no-op; and the warning names only the abandons that acted;
  - an `open_context` waiting on a drain is admitted when it ends, even while other tasks keep
    opening and closing short boundaries on the same loop; a waiter cancelled after its grant
    releases it;
  - an `open_context` entered on a `draining` loop waits for the drain, then reuses a parent
    host the drain closed by reconnecting it, never by finding it closed under its new child;
  - a close callback that enters `open_context` during a sweep is refused with `RuntimeError`,
    and the sweep still completes within its deadline;
  - after a runner's shutdown an acquisition raises and a release sweeps nothing;
  - a release cancelled during its sweep still counts, and a second release raises;
  - the generation rows of §3.3: an old close resuming after a reconnect (following abandonment,
    and following a same-loop `rebuild_connections`) leaves the new managers and owner
    untouched, for each concrete host family, and on `DockerContainerHost` the run-channel binding too;
  - registering prunes closed loops' registries;
  - a stale abandonment does not touch a reconnected host;
  - dependency close order; a failed close is isolated; cancellation is bounded by the deadline;
  - two `pytest.main` sessions in one process leave no registration behind;
  - the static check: no module under `otto/host/` imports `otto.context` at runtime.
  - **What the unit tree relies on.** Unit tests run under bare pytest without `OttoPlugin`, so
    no runner boundary exists there, and `pyproject.toml` sets
    `asyncio_default_fixture_loop_scope = "function"`: each test's loop is closed by its own
    fixture teardown. `ctx.sweep_loop` on a test's running loop therefore stays the unit tests'
    sweep (`tests/unit/host/test_loop_ownership.py` calls it eight times), and the refusal never
    meets it there.
  - **Migrations:** the tests that use `scope_for`, `HostScope`, `_loop_scopes` or
    `abandon_closed_loops` move to `otto.invocation`'s registry: `tests/unit/test_context.py`,
    `tests/unit/test_lifecycle.py`, `tests/unit/host/test_loop_ownership.py`,
    `tests/unit/suite/test_run_api.py`, `tests/unit/cli/test_remote_completion.py` and
    `tests/unit/suite/test_loop_sweep.py`.
  - **The registry guard.** The root fixture's teardown runs after the test's own loop fixtures
    have closed its loop. It forgets closed loops' records, then fails on any record left on a
    loop that is still open and that no boundary holds: an orphan. In the unit tree that catches a
    test that leaves its own loop open with a host on it. Under `otto test`, a session fixture's
    host stays registered on the runner's loop, which a boundary holds, so it passes
    (`tests/unit/suite/test_loop_sweep.py`). A session-finish hook asserts the registry is empty
    after the runners shut down. The guard test proves both wirings, including that the
    between-tests check runs after the test's loop fixture has closed its loop.
- **Commit 6:**
  - `ctx.repos` acquired lazily and cached, refusal included; "known empty" and "unavailable" are
    distinct;
  - `ctx.repos` on the sentinel lab still bootstraps;
  - `repos`, `ordered_repos` and the classifier read one result across an `invalidate()`;
  - spec 4's §8 walk-contract rows still hold, now resolved over `ctx.repos`;
  - `run_tests` builds or inherits its context before selecting repos;
  - the docker repos accessor and the declared-use-case query work inside a context and without
    one; the conveniences still refuse without a context, as today;
  - the ratchet test's `BASELINE` shrinks by exactly the inventoried edges.
  - **Migrations:** `ctx.scopes = …` assignments (`tests/unit/test_run_instruction_activation.py`,
    `session/test_dependencies.py`, `session/test_activation.py`) and `.scopes` fakes
    (`project/test_orchestrator.py`, `project/test_plan.py`,
    `project/test_ensure_differential.py`, `monitor/test_monitor_live.py`) move to
    `seed_scope_verdicts`; spec 4's `get_repos` patches move to `bootstrap=`.

## 8. File operations and the cycle ratchet

- **File operations.** Each of commits 4–6 carries a 3.10 before/after
  `import_budget.py --report-json` table (spec 1 §7).
  - Readers import `otto.invocation` inside functions, as they import `otto.context` today, so
    no surface gains a module at import time.
  - On paths that reached `otto.context` only for a flag, `otto.context` and what it alone
    pulls in at call time may stop loading, so decreases are expected. `otto.host.host` is
    already loaded on those paths, so it is not among them.
  - Each change is named, and shrunk ceilings are re-baselined in the same commit.
  - Commits 2 and 3 are expected to be zero.
- **The cycle ratchet**, from Astra's simulation of the declared graph and checked against the
  baseline by the v1 review. The commits record the measured table.
  - `otto.invocation` is a leaf (`depends_on = []`), so its new edges never join the cycle.
  - **After P1** (spec 4), `otto.lifecycle`'s baseline edges are `→ bootstrap` and
    `→ context`. Spec 4's P1 already cut `→ config`.
  - **Commit 4** retires `lifecycle → bootstrap`.
  - **Commit 5** retires `host → context` and `lifecycle → context`. **`otto.lifecycle` then
    leaves the cycle:** it depends only on `otto.logger`, which is acyclic. `BASELINE` drops the
    entries that point at it from `cli`, `docker`, `host`, `link`, `suite` and `tunnel`. Those
    imports stay real, and tach still declares them.
  - **Commit 6** retires the S-5 edges the inventory proves.
  - `context → host` stays: the context still uses host constants, override logic and host types.
    Spec 5 owns it.

## 9. Out of scope, recorded

- **Spec 3:** the term-backend seam (`ConnectionManager`, `TermContext`, `register_term_backend`)
  and host construction. `RunPolicy` is not a construction input, and neither is anything else in
  this spec.
- **Spec 5:** `context → host` (constants, override logic, host types), and where `OttoContext`'s
  fleet methods live.
- **A task-local `SuppressCommandOutput`.** Today's shared mutation is kept (§3.1). Isolating it
  per task would be its own marked behaviour change.
- **`_root_log_level`** (§5).

## 10. Spec 1 amendments (made in this spec's commits)

- **Design §7, pending seams:** the run-state line is marked settled and links here.
- **Appendix A:** `otto.context`'s note names this spec.
- **Appendix F:** `otto.context`'s initial `__all__` gains `Variant` and `ProjectContextView`. A
  note says `RunPolicy`, `HostResolver` and `ContextBinding` are declared by commit 4 after P1.
- **The P1 checklist** (`todo/590-p1-public-surface-switch-on.md`) gains commit 3, a pre-P1 box
  for commit 2, the P1 docs items in §6.1, and the after-P1 commits 4–6.
