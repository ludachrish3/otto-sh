# Fable Review Verification — 2026-07-01

> Follow-up to [fable_review_outcome.md](fable_review_outcome.md) (the
> 2026-06-12 architecture review). Verifies the quality and thoroughness of the
> implementation work against every locked decision and finding, and records a
> fresh design-gap review of the post-refactor tree. Basis: main @ `9b7b0c4`,
> 144 commits since the review; seven parallel read-only verification passes
> (one per decision area + one fresh-eyes reviewer).

---

## TL;DR

**The review was implemented substantially, faithfully, and with high quality.**
All six locked decisions landed; where the implementation deviates from the
review's letter, the deviation is deliberate and usually an improvement (e.g.
time-boxed log rotation instead of a stat-count budget, `[host_preferences]`
superseding `[host_defaults]`). Standout pieces: the e2e collection-time marker
guard, the fail-safe `-R` reservation break-glass, the registry error messages,
and the hop-chain close cascade.

**Exactly one original finding is fully unaddressed:** the
`run()`/`oneshot()`/`get()`/`put()` return-type inconsistency (three result
shapes across four verbs). It was named in the review's interface-consistency
audit and should be settled **before the contract freeze**.

**The fresh review found one real functional bug** — `UnixHost.oneshot()`
silently ignores `timeout` over SSH (the most common transport) — plus a
robustness hole (import-time exec of user code with no error containment can
brick every `otto` invocation) and a handful of design clunk introduced by the
refactor wave itself. Ranked list at the bottom.

---

## Scorecard

| Review item | Verdict | Residual |
|---|---|---|
| #1 Naming sweep (API/files/JSON) | ✅ Implemented well | `suiteDir`/`testDir` camelCase island; `snmp` reclassified not renamed |
| #2 Pydantic Phase A (boundary) | ✅ Implemented well | No "did-you-mean" suggestions; coverage-store records still stdlib dataclasses |
| #2 Pydantic Phase B (options→Typer) | ✅ Implemented well | — |
| #2-adjacent: return-type consistency | ❌ **Not implemented** | `run`→`RunResult`, `oneshot`→`CommandStatus`, `get/put`→`tuple[Status, str]` still mixed |
| #3 Context object (OttoContext) | ✅ Implemented well | Non-`--cov` suite path closes hosts only at process exit |
| Lifecycle: `async with` + delete `__del__` | ✅ Implemented well | `HostScope.__aexit__` swallows close failures silently |
| #3-coupled: Typer/Click triage | ✅ Done | Typer 0.26.8; vendored-click coupling handled explicitly |
| #4 String registries everywhere | ✅ Implemented well | Lab-repo/reservations builders bypass their own registries for built-ins |
| #4 side benefit: transfer.py split | ✅ Done | — (but `session.py` is now the new giant, see F8) |
| #5 Test tree unit/integration/e2e | ✅ Implemented well | Minor fixture sprawl; `integration` name double duty |
| #6 Reservations server-less | ✅ Implemented well | Only JSON built-in ships (spirit intact; DB arrives via registry) |
| NFS hazards | ✅ Implemented well | Adaptations log at DEBUG, not WARNING |
| Quick hit: lint ratchet | ✅ Done | `select=ALL`, empty ratchet, ~193 rationale'd per-site noqas |
| Quick hit: contributor/architecture docs | ✅ Done | `docs/architecture/` tree (12 pages: layer map, lifecycle, registries, hosts, principles, …) added 2026-07-02 |
| Quick hit: startup profiling | ✅ Done | Import-budget guard + `make profile` (hyperfine, not pyinstrument) |
| Quick hit: automated VM tier | ❌ Absent | Nightly CI exists but is hostless-only, GitHub-hosted |
| Gap 1: `otto init` | ❌ Absent | Docs mitigate; no scaffold command |
| Gap 2: power/reset control | ✅ **Exists** | `PowerController` registry wired into embedded hosts |
| Gap 3: secrets indirection | ❌ Absent | Plaintext creds in lab JSON — biggest open gap |
| Gap 4: host groups/labels | ❌ Absent | Regex-over-IDs workaround only |
| Gap 5: structured result export | 🟡 Partial | Auto-JUnit XML per run; no otto-native JSON |
| Gap 6: cross-suite parallelism | ❌ Absent | Deliberately lowest priority; fine |

---

## Decision-by-decision detail

### #1 Naming sweep — implemented well, one pocket

- All targeted helpers gone: zero hits for `getConfigModule`, `sutDir`,
  `osType`, `neId`, `logCommandOutput`, `embeddedHost` etc. across src/tests.
  Zero camelCase module filenames. Lab JSON fixtures and the pydantic schema
  are fully snake_case; the seven transport tables uniformly `*_options`.
- `element`/`element_id` (the later deviation from "keep `ne`") carried through
  consistently — schema, docs, CLI, completion.
- **Remnant:** `self.suiteDir` / `self.testDir` (`src/otto/suite/suite.py:182,318`)
  — the lone camelCase island in the public suite API, actively taught in
  `docs/guide/test.md:201` and `docs/cookbook/suite-recipes.md:160`. It slipped
  the lint net because ruff N815 flags class-scope declarations, not
  instance-attribute assignments. Also camelCase artifact dir names
  `setupClass`/`teardownClass` (suite.py:212,221). No documented exemption —
  needs a decision: rename (breaking for user suites) or record the exemption.
- **`snmp` was reclassified, not renamed:** docs now frame it as a monitoring
  block, distinct from `*_options` transport tables — coherent, but the classes
  are still named `SnmpOptions`/`SnmpOptionsSpec` (`host/options.py:500`),
  so the code says "options table" while the docs say "not an options table."
  Pick one story.
- Cosmetic: stale `:meth:`ConfigModule.logRepoCommits`` xref in
  `tests/unit/configmodule/test_repo_git_subprocess_leak.py:80`; prose
  "configModule" comments in three files.

### #2 Pydantic two-phase — Phase A + B done; three concrete misses

- `storage/factory.py` shrank ~380 → 127 lines: documented precedence merge →
  `model_validate` → `spec.to_host(cls)`. `extra='forbid'` via shared
  `OttoModel` base; unknown keys are loud errors naming the key. Registry-valued
  fields get "Known: …" enumerations from the **live** registries.
- Coverage: host specs, options specs, settings.toml (`SettingsModel`, with
  deliberate `extra='allow'` only where third-party backend sub-tables live),
  monitor records, reservations JSON, `OttoEnvSettings` for `OTTO_*`.
- `otto schema export` + `make schema` real and registry-aware (custom backends
  appear in the exported enums). Verified by running it.
- `@options` bridge: `params.py` expands pydantic fields to CLI params;
  `ValidationError` → `typer.BadParameter` with field-located messages. Wired in
  both suites and instructions.
- `[host_defaults]` deviated by design → superseded by `[host_preferences]`
  with an explicit migration error. Multi-interface landed additively
  (`interfaces: dict[str,str]` + `address_for()`); the primary `ip` field was
  not reworked.
- **Misses vs the review text:**
  1. No typo *suggestions* (no `difflib.get_close_matches` anywhere) — errors
     name the offending key but don't propose the fix.
  2. The coverage subsystem (`src/otto/coverage/store/model.py`) never got
     pydantic models — still stdlib dataclasses + hand `to_dict()`; the
     `[coverage]` settings table is an unvalidated `dict[str, Any]`
     (settings.py:255).
  3. **Return-type consistency unaddressed** (see scorecard; detail in ranked
     list item 3).
- Clunk noted: double validation on lab load (`json_repository.py:70` runs
  `validate_host_dict` then `create_host_from_dict`, each doing its own
  merge+validate, and the two merges differ); dict↔model round-trips for
  `host_preferences`; `to_runtime()` boilerplate (drift-test-protected);
  `from __future__ import annotations` in four model files
  (models/host.py:11, settings.py:14, monitor.py:24, jsonschema.py:18) despite
  the repo-wide ban — presumably surviving because those modules' docs build
  clean, but it contradicts the stated convention.

### #3 Context object + lifecycle — faithfully implemented

- `OttoContext` (`src/otto/context.py:93`) via ContextVar; CLI root callback
  installs it, scripts use `async with otto.open_context(...)`; instructions
  get typed DI injection (an `OttoContext`-annotated param is stripped from the
  CLI signature and injected at call time). Shims in `configmodule` are genuine
  one-line delegates; zero hits for `_logCommandOutput`, `_globalDryRun`,
  `getConfigModule`, `ConfigModule`.
- Lifecycle: `__aenter__/__aexit__` on protocol, hosts, sessions; **zero
  `__del__` in src/otto**; `HostScope` sweeps every handed-out host on scope
  exit; `SshHopTransport` parent-links so `close()` cascades down hop chains
  (the documented zombie-transport fix).
- Typer 0.26.8; no `import click` anywhere; the vendored-click trap is handled
  with an explanatory comment at `cli/main.py:361-366`.
- Residuals:
  1. **`otto test` path:** pytest classes run on their own event loops where
     `HostScope` can't reach; hosts are swept by `_otto_release_connections`
     **only under `--cov`** (`suite/suite.py:358-395`). Outside coverage runs,
     suite-opened hosts stay connected until process exit. Intentional
     (persistent sessions) but it's the one place the lifecycle fix doesn't
     fully apply.
  2. `SuppressCommandOutput` in no-host form still mutates
     `ctx.log_command_output` — the last ambient-mutable-flag pattern
     (context-scoped now, and its docstring warns about concurrent use).
  3. Scope entry lives in `async_typer_command` (utils.py:72-93) while context
     install lives in main.py — split ownership that's non-obvious.
  4. Leaf helpers use `try_get_context()` with hardcoded defaults — two
     behavior regimes (with/without context), confined but classic optional-ctx.

### #4 String registries — uniform core, two builders bypass their own registry

- Ten registries share one idiom (private dict, `register_*(name, cls)`,
  `build_*` raising ValueError that lists registered names and points at the
  register function; built-ins registered through the same public path).
  Literal sets fully gone from src/; selectors are plain `str` validated
  against **live registries** by pydantic validators (models/host.py:80-98).
- `transfer/` split is clean: one module per backend, self-registering;
  `nc.py` (964 lines) is the largest but is one genuinely complex backend.
- `post_connect`: no longer silently dropped — `SshOptionsSpec` omits it and
  `extra='forbid'` makes it a loud error; docs explain "can't be expressed in
  JSON." Code-only escape hatch retained.
- `docs/guide/extending-backends.md` verified accurate against the code on
  every cross-checked point.
- Deviations/wrinkles:
  1. **Built-ins bypassed in two builders:** `build_lab_repository`
     (`storage/__init__.py:80-81`) and reservations' `build_backend`
     (`reservations/__init__.py:101-117`) hard-code their built-in branches
     before consulting the registry — re-registering `json` has no effect.
     Largest deviation from "built-ins become first-party registry entries."
  2. Build-signature divergence: term/transfer/power return the class;
     frame/filesystem/loader return instances; lab-repo/reservations take
     settings kwargs. Documented per-registry, but not one idiom.
  3. `models/host.py:23,28` and `cli/host.py:55` import private
     `_TERM_BACKENDS`/`_TRANSFER_BACKENDS` dicts — no public
     `registered_*_backends()` accessor for those two.
  4. Residual `term == "ssh"` capability checks (unix_host.py:418,467,
     transfer/nc.py:353,848, docker_host.py:144) — behavioral, not dispatch,
     but tunneling/interact features silently key off the built-in name and
     won't extend to a custom term backend.
  5. Conformance suite covers only lab-repository + reservation backends —
     2 of ~10 seams; no public conformance helper or sample for
     transfer/term/frame/filesystem backends.

### #5 Test tree — names no longer lie; enforcement is real

- `tests/{unit,integration,e2e}`; the three called-out liars moved truthfully;
  zero `*integration*`/`*e2e*` files under tests/unit.
- dir=level / markers=resource is **enforced**: tests/e2e/conftest.py:29-55
  auto-stamps `e2e` and raises `pytest.UsageError` at collection unless exactly
  one primary resource marker is present. Gates are marker-driven
  (Makefile:69-71,188).
- Fixtures: `tests/_fixtures/` is the discoverable home; SUT fixture repos
  stayed at tests/ top level **by recorded design decision** (restructure spec
  line 69). 8 conftests, sensible hierarchy.
- Nits: `integration` is both a tier directory and a resource marker name;
  stray second fixture home `tests/unit/fixtures/embedded_coverage/`; stale
  pre-restructure `.pyc` ghosts at old paths (cosmetic); no inverse guard
  stopping a resource marker inside tests/unit (currently zero offenders).

### #6 Reservations — server-less, fail-safe, conformance-tested

- `ReservationBackend` protocol is strictly read-only ("otto never creates,
  modifies, or releases reservations"); named registry, built-ins `none`+`json`;
  no coordinator anywhere. Multi-holder `who_reserved` (deduped, list-based).
- `-R` break-glass: the backend is **never constructed** (a hanging scheduler
  constructor can't block lab access), with a factory thunk retained for
  on-demand `otto reservation` subcommands. Conformance suite for third-party
  backends ships in `otto.testing`.
- Nit: review said "JSON/DB backend"; only JSON ships — deviation in letter,
  not spirit.

### NFS hazards — handled; visibility is the residual

- `src/otto/filesystem.py`: stdlib-only mountinfo parser, fails safe to local.
- Monitor DB: `journal_mode = DELETE if network else WAL`
  (monitor/collector.py:219-237) — but the downgrade and the flock
  "same-host-only" caveat log at **DEBUG**, not warning as the review
  specified; an operator won't see either at default verbosity. flock itself is
  retained on network FS (documented trade-off, not an oversight).
- Log rotation: wall-clock time-boxed (`LOG_ROTATE_BUDGET_SECONDS = 5.0`,
  logger/management.py:41-45,309-360) — a *time* budget instead of the
  suggested stat-count budget; arguably better on slow NFS. Name-pattern
  guard fail-safes the rmtree.

### Quick hits + feature gaps

- **Lint:** `select = ["ALL"]` with a principled, rationale-commented deny list;
  no ratchet file remains; ~193 narrow per-site noqas with inline rationale;
  enforced in CI + nightly. Done as specified.
- **Docs:** contributor docs substantial (contributing.md 436 lines, guides,
  cookbook); ✅ architecture ask closed 2026-07-02 — `docs/architecture/`
  now holds a 12-page tree (overview/layer map, lifecycle, registries,
  hosts, data boundary, results+logging, test pipeline, monitor+coverage,
  extension points, design principles, docker-hosts design) wired into the
  root toctree; the old orphaned `docs/design/docker_hosts.md` moved in.
- **Profiling:** import-budget guard + golden snapshots + `make profile`
  (hyperfine) + PEP 562 lazy `import otto`. Intent fully satisfied.
- **VM tier in CI:** absent. Nightly workflow exists (cron + auto-issue on
  failure) but every job is GitHub-hosted and hostless-only.
- **Feature gaps:** power/reset control **closed** (`host/power.py`:
  `PowerController` ABC, `CommandPowerController`, registry, wired into
  embedded hosts with fail-loud `None`). `otto init`, secrets indirection,
  host groups: absent. Result export: partial (auto-JUnit per `otto test` run;
  no otto-native JSON schema). Cross-suite parallelism: absent, and that's fine.
- `todo/TODO.md:20` still has the aioftp→pyftpdlib role-conflation note
  (pyftpdlib is a server; otto's aioftp use is the client backend).

---

## Fresh findings — new review pass

Fresh-eyes review of the post-refactor tree. Overall assessment: genuinely good
shape; session/connection code is unusually well-commented and
cancellation-aware; the repeat-scheduler and OttoLogger removals left no dead
code; malformed-lab-JSON errors are exemplary. What's left, by impact:

### Confirmed by reading the code

**F1. `UnixHost.oneshot(timeout=...)` is silently ignored over SSH — and the
`except TimeoutError` there is unreachable.**
`session.py:1344-1370` (`SessionManager.oneshot`, ssh branch): no
`asyncio.wait_for`/`asyncio.timeout` around `create_process()` + the stdout
read loop; the `except asyncio.TimeoutError: process.terminate()` can never
fire, so it's dead code that *looks* like timeout handling. Docstring promises
"seconds before the command is considered hung"; on the most common transport a
hung remote command hangs the caller forever. Every other transport honors the
timeout (telnet via `session.run`, embedded via `run_cmd`, local via per-line
`wait_for`) — inconsistent in exactly the way a user can't observe until a
command wedges. Blast radius includes `DockerContainerHost._oneshot_via_parent`
and `file_ops.py`. **Fix:** wrap read loop + `process.wait()` in
`asyncio.timeout`, terminate on expiry, return the session path's
`Status.Error` shape. While touching it, align local's per-`readline` *stall*
timeout with the documented total-budget semantics.

**F2. The 8-field protocol-options block is hand-duplicated across ~9
signatures.**
`term, transfer, ssh_options, telnet_options, sftp_options, scp_options,
ftp_options, nc_options` repeats verbatim in `context.py:136-230` and five
functions in `configmodule/configmodule.py` (signatures + forwards +
docstrings; both fan-out functions carry `# noqa: PLR0913`), while
`OttoContext.get_host/all_hosts` take untyped `**overrides: Any` — the same
operation has two signature styles. Adding one protocol option class means ~9
coordinated edits. This is the main clunk the context refactor introduced.
**Fix:** one frozen `ProtocolOverrides` bundle dataclass; single
`overrides: ProtocolOverrides | None = None` param; `_apply_option_overrides`
becomes the only place that knows the field list.

**F3. `import otto.configmodule` exec's every user init module and `test_*.py`
with zero error containment — one broken file bricks the whole CLI.**
`configmodule/__init__.py:52-53,130-154` runs repo discovery +
`apply_repo_settings` at package-import time; `import_init_modules` /
`import_test_files` (repo.py:652-683,705-713) call `spec.loader.exec_module`
raw. A syntax error in one `test_*.py` under `OTTO_SUT_DIRS` makes **every**
otto invocation (`--help`, `--version`, completion) die with a raw chained
traceback before Typer parses argv. Stark asymmetry: bad lab *data* fails with
beautiful layered errors; bad user *code* fails as an interpreter crash with no
framing of which repo/file to fix. **Fix (minimum):** wrap each exec and
re-raise with `"{repo.sut_dir}: failed to load {test_file}: …"`.
**Fix (better):** move the composition root out of `__init__` into an explicit
`bootstrap()` called by the CLI and lazily by `open_context` — this also kills
the import-order comment gymnastics at `__init__.py:81-90`.

**F4. `Lab.__add__` has in-place mutation semantics.**
`configmodule/lab.py:62-76`: renames `self`, merges into `self.hosts`,
repoints `other`'s hosts' `_lab` back-links, returns `self` — `__iadd__`
wearing `__add__`'s name. `combined = lab_a + lab_b` silently corrupts `lab_a`;
reuse compounds names (`a_b`, `a_b_x`). The docstring advertises merge-via-`+`,
so library users will hit it. **Fix:** return a new `Lab` (re-registering
hosts), or replace with explicit `Lab.merged(*labs)`.

**F5. `Arg(name=...)` / `Opt(name=...)` are dead knobs on the public
`@cli_exposed` overlay.**
`utils.py:96-117` define `name` on both; `build_cli_binding`
(cli/param_synth.py:151-232) never reads it. A host-class author writing
`Opt(name="--dest")` gets a silent no-op. Honor it or delete it before
third-party host classes depend on it.

**F6. `HostScope.__aexit__` swallows host-close failures without a trace.**
`context.py:52-60`: `gather(..., return_exceptions=True)` result dropped — a
raising `close()` (wedged telnet writer, dead tunnel) leaves nothing in the
log; leaked-FD debugging has no starting point. Also decides "needs closing"
via a `getattr(h, "_connected", True)` private-attr probe. **Fix:** log each
exception with host id; consider a public `needs_close` property.

**F7. Public-surface incoherence left by the logger/ConfigModule removals.**
`otto/__init__.py:23-52`: `get_otto_logger` **and** alias `get_logger` both
exported (two names for one function — the exact duplicated-concept scar the
OttoLogger deletion should have resolved); `load_lab` missing from top level
even though the library guide's bring-your-own-CLI recipe requires it (while
`open_context`, which wraps it, *is* exported); the `configmodule` package name
itself is scar tissue ("fleet accessors over the active context"), and one
cookbook teaches the inner double-name path
`from otto.configmodule.configmodule import ...`. **Cheap now:** export
`load_lab`, pick one logger name, fix the docs to the package path. **Later:**
an `otto.fleet`-style alias to start the rename migration.

**F8. `host/session.py` (1,548 lines) is now the module `transfer.py` used to
be.** ShellSession engine + four transports + HostSession + SessionManager in
one file; top churn hot spot historically. Quality inside is high — this is
maintenance cost, not correctness. A `host/session/` package (engine / one
module per transport / manager) would let the next transport bring-up touch one
file. Two cross-module private reaches to clean while splitting:
session.py:998 imports `_normalize_expects`/`_resolve_command`/
`_run_cmds_with_budget` from host.py; `open_session` pokes
`existing._session.close()` (session.py:1436).

### Suspicions worth a look (not fully verified)

- **Lab-free flag detection can false-positive on option values** —
  `_is_lab_free_flag_invocation` (cli/main.py:133-149) set-intersects raw
  tokens with `{"--help", "--list-tests", ...}`; an option *value* equal to one
  of those tokens skips lab bootstrap while the subcommand still executes,
  landing on a confusing `get_context()` RuntimeError.
- **`Repo.commit`/`Repo.description` call `asyncio.run()` inside properties**
  (repo.py:743,756) — raises `RuntimeError` if touched from suite/instruction
  code already inside a loop.
- **Telnet oneshot pool re-appends dead sessions** (session.py:1382-1384) —
  filtered on next acquire but transports reclaimed only at `close_all`; a long
  flaky-telnet fleet run could accumulate `__oneshot_pool_N__` entries.

### Checked and found clean

Repeat-scheduler removal (no leftovers); OttoLogger deletion (`'otto'` is a
plain `logging.Logger`; management.py composition well-guarded with symmetric
reset); malformed-lab-JSON error layering; term/transfer/host-class registries
(genuine extension stories, not over-registration); cancellation safety in
session/connection code (close-before-propagate on half-built sessions,
`_needs_recovery` marking, handshake retry — deliberate and correct as read).

---

## Ranked follow-ups

Combining residual review items and fresh findings, in recommended order:

1. **Fix SSH `oneshot` timeout (F1)** — real correctness bug on the most
   common transport; small, well-scoped fix.
2. **Contain import-time exec of user code (F3)** — robustness/UX; minimum fix
   is cheap, `bootstrap()` refactor is the right end state.
3. **Settle return-type consistency (`run`/`oneshot`/`get`/`put`)** — the one
   review finding never addressed; belongs **before the contract freeze**, same
   reasoning as the naming sweep ("cheap now, impossible later").
4. **Secrets indirection in lab JSON** — top open feature gap; plaintext creds
   block sharing the host database; env-var interpolation at ingest is small.
5. **`ProtocolOverrides` bundle (F2)** — removes the 9-signature duplication
   and unifies the two fan-out signature styles.
6. **`Lab.__add__` fix (F4)** + **`Arg`/`Opt` dead `name` knob (F5)** +
   **`HostScope` close logging (F6)** — three small footgun removals, could be
   one polish PR.
7. **Public API tidy (F7)** — export `load_lab`, deprecate one logger alias,
   fix cookbook inner-path imports.
8. **`otto init` scaffolding** — biggest remaining learning-curve lever.
9. **`suiteDir`/`testDir` decision + `SnmpOptions` naming story** — last
   naming-sweep stragglers; decide before the freeze locks them in.
10. **NFS adaptations → WARNING level** — one-line visibility fix.
11. **`session.py` package split (F8)** — maintenance investment, no urgency.
12. **VM-tier nightly on a self-hosted runner** — infra exists (nightly +
    auto-issue); marginal work is the runner.
13. Small stuff batched: coverage-store pydantic models + typed `[coverage]`
    table; `from __future__ import annotations` in four model files vs the
    ban; conformance samples for transfer/term seams; TODO.md aioftp line;
    stale `.pyc` ghosts; docs/design architecture page.
