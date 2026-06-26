# Pluggable host source + backend conformance testing — design

**Date:** 2026-06-25
**Status:** Approved (design); pending implementation plan
**Scope:** Make otto's host-data-source seam a registered, pluggable backend;
unify host-source and reservation backend selection onto otto's existing
named-registry model (dropping reservations' dotted-path resolution) and upgrade
`who_reserved` to a multi-holder list; ship a reusable conformance suite (built
on the `expect` construct) so otto, its samples, and SUT repos can prove a
backend complies with the interface; ship sample reference backends; and weave
both backend choices into a coherent one-time "set up otto for your team"
documentation path.

---

## 1. Motivation

otto exposes two "backend" interfaces that a SUT (system-under-test) repo can
customize: the **host source** (where host definitions come from) and the
**reservation service** (who currently holds which lab resources). Today these
two interfaces are in very different shape, and both are under-tested at the
seam.

### 1.1 Current state (findings)

**Reservation services — genuinely pluggable.**

- `ReservationBackend` is a `@runtime_checkable` Protocol
  (`src/otto/reservations/protocol.py`) with `get_reserved_resources`,
  `who_reserved`, `backend_name`.
- `build_backend(settings, repo_dir)` (`src/otto/reservations/__init__.py`)
  dispatches by name (`"json"`, `"none"`) or dotted path
  (`"pkg.mod:ClassName"`), reading a `[reservations]` settings block with a
  per-backend kwargs sub-table.
- Built-ins: `NullReservationBackend`, `JsonReservationBackend`.
- An error contract exists: `ReservationBackendError`, `MissingReservationError`.
- Tests exercise the extension seam (`tests/unit/reservations/test_build_backend.py`
  injects a `FakeBackend` via `sys.modules`).
- Docs: `docs/guide/reservations.md` has a "Writing a custom backend" section —
  but the skeleton is a plain ` ```python ` fence with `...` stubs that is **not
  executed**, so it can rot silently.

**Host source — a dead seam.**

- `LabRepository` is a `@runtime_checkable` Protocol
  (`src/otto/storage/protocol.py`) with `load_lab(name, search_paths,
  preferences)`, `list_labs(search_paths)`, `supports_location(path)`.
- But it is **not pluggable**: only `JsonFileLabRepository` exists, it is
  hard-coded at `src/otto/configmodule/lab.py:97`, there is no registration or
  config hook, no protocol-level/extension tests, no error contract, and no
  docs.
- (Separately, host *class* registration — `register_host_class` /
  `register_os_profile` in `src/otto/host/os_profile.py` — is live and
  documented. That seam is **out of scope**; this work is about the host
  *data source*.)

**Documentation testing — a paved road.**

- Sphinx `{doctest}` blocks run via `make doctest`; `make doctest-src` runs
  `pytest --doctest-modules` over `src/otto`.
- `scripts/lint_markdown_doctests.py` fails the build if a `>>>` prompt appears
  in any non-`{doctest}` fence, preventing silent example rot.
- `nitpicky = True` with zero ignores; docs warnings are errors (`-W`).

### 1.2 The `expect` construct (conformance engine)

`ExpectCollector` (`src/otto/suite/expect.py`) is **already standalone** —
stdlib-only, no coupling to `OttoContext`, suites, hosts, or contextvars. API:

- `expect(condition, msg=None)` — record a non-fatal failure (captures source
  line + locals).
- `.failures: list[str]` — public, readable.
- `reset()` — clear.
- `raise_if_failures()` — raise a single `AssertionError` aggregating all
  recorded failures.

The *coupled* variant is `OttoSuite.expect()` (tied to the pytest fixture
lifecycle). `ExpectCollector` has **no dedicated unit tests** today.

This is the engine for the conformance suite: run every contract rule as an
`expect()`, then `raise_if_failures()` so a backend author sees **all**
violations at once instead of dying on the first.

---

## 2. Goals / Non-goals

### Goals

1. Make the host source a registered, pluggable backend selected by **bare
   name**, with `JsonFileLabRepository` reframed as the built-in `"json"`
   pass-through backend. Use the **same named-registry mechanism** as otto's
   other extension points (`register_term_backend`, `register_transfer_backend`,
   `register_host_class`), and **migrate reservations onto it too** so every
   otto backend is registered and selected the same way (see §3b — drops
   reservations' dotted-path resolution).
2. Define a host-source **error contract** mirroring reservations.
3. Ship a reusable **conformance suite** for both interfaces, built on the
   `expect` construct, exposed as a single ergonomic helper per interface that
   collects and reports all rule violations together.
4. Ship **sample reference backends** for both interfaces as importable
   `otto.examples.*` modules **and** demonstrate them via executable `{doctest}`
   blocks in the guides.
5. Wire conformance + doc examples into the gate so the documentation is
   continuously verified against the real interfaces.
6. Make host-source and reservation setup part of a coherent **one-time team
   setup** documentation path, reducing the onboarding learning curve.
7. Upgrade the reservation interface so a resource can have **multiple
   holders**: `who_reserved(resource)` returns `list[str]` (empty = unreserved),
   replacing the `str | None` single-holder model (see §3a).

### Non-goals

- Host *class* / OS-profile registration (`register_host_class`,
  `register_os_profile`) — already live and documented; untouched.
- Building an entry-point pytest plugin with auto-collection magic. The
  conformance suite ships as importable helpers; an optional pytest entry-point
  registration is noted as polish, not a requirement (see §5.4).
- Changing `Lab`, host construction (`create_host_from_dict`), or the host
  spec/registry machinery.
- Adding new reservation backends beyond the sample. (`null`/`json` stay.)

---

## 3. Host source becomes a pluggable backend

### 3.1 Reshaped `LabRepository` Protocol (construct-time config)

Configuration moves into the backend at construction time, so the generic
Protocol sheds the JSON-specific `search_paths` concept and becomes symmetric
with reservations (where the backend is constructed with its config, then
queried):

```python
@runtime_checkable
class LabRepository(Protocol):
    def load_lab(self, name: str, preferences: dict[str, dict[str, Any]] | None = None) -> Lab: ...
    def list_labs(self) -> list[str]: ...
```

- `search_paths` is supplied to `JsonFileLabRepository` at construction
  (`JsonFileLabRepository(search_paths=...)`), fed from the existing top-level
  `labs = [...]` setting. It no longer appears on the generic interface.
- `supports_location(path)` is **removed**: under name-dispatch selection it is
  vestigial. *Confirm-at-plan-time:* grep the tree for any remaining caller
  before deleting; if one exists, keep it but mark it non-essential to the
  Protocol.

### 3.2 Error contract

Introduce a host-source error contract mirroring `ReservationBackendError`:

- `LabRepositoryError` — raised by a backend when a query fails (I/O, network,
  parse, credentials).
- `LabNotFoundError` — raised by `load_lab` when the named lab does not exist.
  (A missing lab must raise this, not return `None` or raise a bare `KeyError`.)

Location: a small `src/otto/storage/errors.py` (or alongside the Protocol),
re-exported from `otto.storage`. The built-in `json` backend raises these; the
conformance suite asserts them.

### 3.3 Factory + config surface

Add `build_lab_repository(settings, repo_dir) -> LabRepository`, paralleling
`build_backend`:

- Reads a `[lab]` settings block:
  ```toml
  [lab]
  backend = "json"            # default; or a registered name like "myteam-cmdb"

  [lab.json]
  # backend-specific kwargs (json: search paths default to top-level `labs`)
  ```
- **Name dispatch via the registry** (§3b): `"json"` → pre-registered
  `JsonFileLabRepository` (no rename; just exposed by name); a custom backend
  is selected by the bare name its init module registered. No dotted-path /
  `importlib` resolution.
- Per-backend kwargs come from `[lab.<name>]`, passed as `**kwargs` to the
  constructor (same convention as reservations).
- **Backward compatible:** when no `[lab]` block is present, the backend
  defaults to `"json"` over the existing `labs = [...]` search paths. Existing
  repos need zero config change.
- Add `LabConfigSpec` to `src/otto/models/settings.py` (mirror
  `ReservationConfigSpec`: `backend: str = "json"`, `extra="allow"` for the
  per-backend sub-table) and wire it into `SettingsModel`.

### 3.4 Wiring + multi-repo resolution

- Replace the hard-coded `JsonFileLabRepository()` at
  `src/otto/configmodule/lab.py:97` with `build_lab_repository(...)`, then call
  `repo.load_lab(labname, preferences)`.
- **Multi-repo resolution** (otto merges multiple repos): mirror reservations'
  "first repo that declares the block wins" rule for backend *selection*, while
  preserving today's path-merge behavior for the default json backend:
  - Default `json`: receives the **aggregated** `labs` search paths from all
    repos (preserves current merge semantics).
  - A non-default `[lab] backend`: taken from the **first** repo declaring one,
    constructed once with its `[lab.<name>]` kwargs.
  - *Confirm-at-plan-time:* validate this against the current
    `configmodule/lab.py` aggregation and `load_lab`/`load_lab` callers in
    `configmodule/lab.py`.

---

## 3a. Reservation interface change: multi-holder `who_reserved`

Shared lab resources can legitimately have more than one concurrent holder
(e.g. a shared service, or a soft/advisory reservation). The current
`who_reserved(resource) -> str | None` cannot express that. Change it to:

```python
def who_reserved(self, resource: str) -> list[str]: ...
```

- Returns the usernames currently holding `resource`; an **empty list** means no
  one holds it (replacing the `None` sentinel).
- Order is deterministic (file/registration order for the json backend);
  duplicates removed.

Ripple (all in the reservation half of the work):

- `ReservationBackend.who_reserved` Protocol signature + docstring
  (`src/otto/reservations/protocol.py`).
- `NullReservationBackend.who_reserved` → return `[]`
  (`src/otto/reservations/null_backend.py`).
- `JsonReservationBackend.who_reserved` → aggregate **all** active entries whose
  `resources` include the lookup (drop today's "first writer wins"), returning
  the deduped holder list (`src/otto/reservations/json_backend.py`).
- `check.py` failure formatting: `holders: dict[str, list[str]]`; render
  `(unreserved)` for an empty list and `held by alice, bob` for multiple
  (`src/otto/reservations/check.py:113`). `get_reserved_resources` /
  `MissingReservationError` *gating* semantics are unchanged — the gate still
  asks "does this user hold the resource"; only the informational holder
  lookup becomes multi-valued.
- Tests: `test_null_backend` (`== []`), `test_json_backend` (list returns + a
  **new multi-holder** case), `test_check`, `test_reservation` (CLI),
  `test_build_backend` fakes.
- Docs: `docs/guide/reservations.md` and `docs/api/reservations.rst` return-type
  and contract updates; the upgraded executed sample (§6.1) reflects the list
  return.

This change rides with the reservation conformance work (§4.3) so the new
contract is asserted from day one.

---

## 3b. Unified named-registry registration (both backends)

Every *other* otto extension point — host classes, OS profiles, term backends,
transfer backends, product providers — is registered by **name** in an `init`
module and selected by that bare name from lab data / settings. Reservations'
`pkg.mod:ClassName` dotted-path resolution is the lone exception. This work
removes that exception so there is **one registration model across all of
otto**.

### Mechanism

- Add `register_reservation_backend(name, cls)` (re-exported from
  `otto.reservations`) and `register_lab_repository(name, cls)` (re-exported
  from `otto.storage`), mirroring `register_term_backend` /
  `register_transfer_backend` / `register_host_class`.
- Built-ins are **pre-registered at module import**: `"none"` / `"json"`
  (reservations), `"json"` (lab source) — exactly how the built-in term/transfer
  backends seed their registries.
- A custom backend registers itself in an `init` module the SUT already lists,
  and settings select it by **bare name** (`backend = "myteam"`).
- `build_backend` / `build_lab_repository` resolve the name against the registry
  and pass `[<block>.<name>]` kwargs to the constructor. **No `importlib` /
  dotted-path resolution path remains.**
- An unknown name raises the interface's error (`ReservationBackendError` /
  `LabRepositoryError`) listing the registered names — same helpful
  registry-miss behavior as elsewhere.

### Why ordering is safe (the feasibility check)

A name registry only works if init modules import **before** backend selection.
They do: the CLI top-level callback calls `get_repos()` first, which runs
`apply_repo_settings(_repos)` → each repo's `apply_settings()` →
`import_init_modules()` (`src/otto/configmodule/repo.py`, the `for mod in
self.init: importlib.import_module(mod)` loop). Only **after** `get_repos()`
returns does the callback call `load_lab(...)` and then `build_backend(...)`
(`src/otto/cli/main.py`). So any `register_*()` a custom init module runs has
already populated the registry by the time either backend is selected — the
same guarantee term/transfer selection already relies on.

(Completion mode skips init import and reads names from the completion cache;
it never *builds* a backend, so there is no ordering concern there either.)

### Hard cutover

Dotted-path support is **removed**, not deprecated. Any existing
`[reservations] backend = "pkg.mod:ClassName"` config migrates to: (1) an
`init`-module `register_reservation_backend("name", ClassName)` call, and
(2) `backend = "name"`. Reservations is new enough that this is acceptable; the
migration is one line of init + one settings edit, and is called out in the
reservations guide.

### Optional symmetry follow-on (out of core scope)

Term/transfer backend names are injected into the generated `hosts.json` schema
enums and cached for tab-completion via `collect_backend_names()`. Reservation
and lab backends are selected in `settings.toml` (not via CLI args), so they do
not need completion. Injecting their registered names into a `settings.toml`
JSON-schema enum is a nice parallel but is deferred — noted, not built here.

---

## 3c. Break-glass overrides: `--as-user` and `-R/--skip-reservation-check`

Two operator escape hatches wrap the reservation gate so a wrong or unreachable
scheduler can never permanently block lab access. They are **gate-layer**
mechanisms, *not* part of the backend Protocol — a custom backend implements
neither — so they are verified against otto's gate, not by the conformance suite
(§4).

- `--as-user USERNAME` — run the reservation check as USERNAME instead of
  `$USER`. Use when a teammate holds the shared lab. Precedence: `--as-user` >
  `getpass.getuser()` (`src/otto/reservations/identity.py`).
- `-R` / `--skip-reservation-check` — bypass the check entirely, for emergencies
  when the scheduler is wrong or unreachable.

### Current coverage + the gaps

- Unit-tested today: `resolve_username` precedence
  (`tests/unit/reservations/test_identity.py`); `gate()` honoring `skip_check`
  (`tests/unit/reservations/test_check.py::test_skip_flag_short_circuits`) and
  the normal/failing check paths.
- **Gap 1 — end-to-end wiring is unverified.** Nothing exercises the *top-level
  callback* threading the flags through: `--as-user` → `resolve_username` → the
  username actually passed to `backend.get_reserved_resources(...)`, and `-R` →
  `ReservationState.skip_check=True` → gate skip. The pieces are tested in
  isolation; the flag → state → query path is not.
- **Gap 2 — `-R` does not cover a broken backend (a real break-glass hole).**
  The callback builds the backend *before* consulting `skip_reservation_check`:
  a `ReservationBackendError` at build time raises `typer.Exit(1)` whose message
  says "Pass --skip-reservation-check / -R to proceed without the check" — but
  the code never honors `-R` on that path (`src/otto/cli/main.py`, the
  `build_backend` try/except). So the exact emergency the flag exists for —
  scheduler misconfigured/unreachable — is *not* escapable. (Query-time failures
  already short-circuit correctly, because `gate()` checks `skip_check` before
  calling `check_reservations`.)

### Changes

1. **`-R` skips backend construction entirely** (decided — behavior change to
   the callback): when `skip_reservation_check` is set, the top-level callback
   does **not** call `build_backend` at all for normal commands. It stores
   `ReservationState(backend=None, skip_check=True)` and proceeds. Because the
   backend is never constructed, a reservation system that **fails or hangs** in
   its constructor can never block lab access — the strongest break-glass. The
   `Exit(1)`-on-build-failure path only applies when `-R` is *not* set.
   - *Implementation subtlety:* `gate()` currently returns **silently** when
     `res.backend is None` (the early `if res is None or res.backend is None:
     return`), and only emits the loud "check SKIPPED" warning inside the
     `skip_check` branch that follows. With `-R` now yielding `backend=None`,
     `gate()` must emit that loud warning whenever `skip_check` is true,
     *before* the backend-None early return — a skipped check must never be
     silent.
   - The `otto reservation whoami` / `check` introspection subcommands build
     their own backend on demand when actually needed (they are the only place
     that still wants the backend under `-R`).
2. **Integration tests** (Typer `CliRunner` against the real top-level callback,
   with a fake backend):
   - `--as-user bob` makes the gate query reservations as `bob` (assert the
     username reaching the fake backend) and prints the acting-as banner.
   - `-R` runs a normal command **without constructing the backend at all**
     (assert `build_backend` is never invoked — e.g. a fake that would raise/
     hang if called), and still emits the loud "check skipped" warning (Gap 2).
   - default (no flags) runs the check as `$USER`.
3. **Docs** — a "Break-glass overrides" subsection in `reservations.md` and a
   line in the Team setup checklist (§6.2), so operators know the emergency
   overrides exist and when to reach for them.

These ride with the reservation work alongside §3a/§3b.

---

## 3d. Optional capability: `--as-user` username completion (cached)

A backend that can enumerate its users may offer them as tab-completion values
for `--as-user`. This is an **optional capability**, modelled as a separate
`@runtime_checkable` Protocol so backends that cannot list users (e.g. `none`,
or one fronting a system with no user directory) simply don't implement it:

```python
@runtime_checkable
class SupportsUsernameCompletion(Protocol):
    def list_usernames(self) -> list[str]: ...
```

- The built backend is checked structurally (`isinstance(backend,
  SupportsUsernameCompletion)`); when present, `list_usernames()` feeds
  `--as-user` completion. When absent, `--as-user` still accepts free-form input
  with no suggestions. Naming mirrors `list_labs()` on the host side.

### Caching — same policy as host IDs

Enumerating users can be slow (network/DB) and the list can change, so the
values are cached in otto's completion cache exactly like host IDs:

- Add a `usernames` list to the cache payload; bump `SCHEMA_VERSION` 5 → 6
  (`src/otto/configmodule/completion_cache.py`). Extend `read_cache` /
  `write_cache` with the new key. (An old cache fails the version check and is
  rebuilt — `read_cache` already returns `None` on a `schema_version` mismatch.)
- A `collect_reservation_usernames(repos)` collector — sibling to
  `collect_host_ids` — builds the selected backend (first repo with
  `[reservations]`, after init modules are imported) and calls
  `list_usernames()` when the capability is present, running in the **slow
  path** and written to the cache there.
- Completion mode (fast path) reads `usernames` from the cache and never builds
  a backend — identical to host-id completion. A cold cache yields no
  suggestions (graceful degradation), refreshed on the next slow-path run.
- Invalidation follows the existing `compute_fingerprint` policy plus the
  `--clear-completion-cache` flag — the same controls users already have for
  host IDs.

### Hard requirement: collection must never block

Because collection can touch the network, `collect_reservation_usernames` is
**best-effort**: any backend-build / enumeration error (or absence of the
capability) yields an empty list and is swallowed — exactly as the existing
cache writes are "best-effort, never block real work." *Confirm-at-plan-time:*
(a) whether to bound it with a short timeout so a slow backend can't stall the
slow path, and (b) whether to collect in the configmodule slow path (simplest,
but builds the backend a second time on a cache miss — the callback also builds
it for the check) or to refresh `usernames` from the CLI callback after it has
already built the backend (avoids the double build, but does not refresh under
`-R`, which skips construction entirely per §3c).

### Wiring + conformance + sample

- `--as-user` (`src/otto/cli/main.py`) gains an `autocompletion=` callback that
  reads `usernames` from the cache, mirroring the host-id completion callback.
- Conformance (§4.3): an **optional** rule — when a backend implements
  `SupportsUsernameCompletion`, assert `list_usernames()` returns `list[str]`;
  skipped otherwise.
- The `otto.examples.reservations` sample implements `list_usernames()` to
  demonstrate the capability and exercise it in conformance + docs.

---

## 4. Conformance suite (the centerpiece)

### 4.1 Public surface

A new public module `otto.testing` exposing one ergonomic helper per interface:

```python
# otto.testing
def assert_lab_repository_conforms(repo, *, expected_labs=None) -> None: ...
def assert_reservation_backend_conforms(
    backend, *, known_user=None, known_resources=None
) -> None: ...
```

Each helper:

1. Constructs a single `ExpectCollector`.
2. Runs **structural/type rules unconditionally**.
3. Runs **behavioral round-trip rules when the caller supplies ground truth**
   (so a SUT author can leverage their own fixtures/known state).
4. Calls `collector.raise_if_failures()` — so in pytest the result is one
   failing test listing **every** violated rule, not just the first.

Each rule's `msg` carries a descriptive rule label so the aggregated report is
self-explanatory. (`ExpectCollector` is used as-is; no structured-result
enrichment in v1 — YAGNI. Revisit only if the string report proves
insufficient.)

### 4.2 LabRepository conformance rules

Structural (always):

- `isinstance(repo, LabRepository)` (runtime_checkable).
- `load_lab` and `list_labs` are callable.
- `list_labs()` returns a `list[str]` (every element a `str`).

Behavioral:

- For each name in `list_labs()`, `load_lab(name)` returns a `Lab`.
- `Lab.hosts` invariants: each key equals its host's id; each value is a
  `RemoteHost`.
- An unknown lab name raises `LabNotFoundError` (not bare `KeyError`/`None`).
- `load_lab` is idempotent: two calls for the same name yield equivalent labs.
- If `expected_labs` is given: each appears in `list_labs()` and loads.

### 4.3 ReservationBackend conformance rules

Structural (always):

- `isinstance(backend, ReservationBackend)`.
- `get_reserved_resources`, `who_reserved`, `backend_name` are callable.
- `backend_name()` returns a non-empty `str`, stable across calls.

Behavioral:

- `get_reserved_resources(user)` returns a `set[str]` (set-like; elements
  `str`).
- `who_reserved(resource)` returns a `list[str]` (every element a `str`; an
  empty list means no holders — never `None`).
- Round-trip consistency when `known_user`/`known_resources` given:
  `U ∈ who_reserved(r)` ⇒ `r ∈ get_reserved_resources(U)`, and each resource in
  `known_resources` held by `known_user` lists `known_user` in
  `who_reserved(r)`.
- *Error-contract rule* (`ReservationBackendError` on failure) is exercised via
  a purpose-built failing sample in otto's own tests, not by the generic
  helper (the helper cannot force a healthy backend to fail).
- *Optional capability rule* (§3d): when the backend implements
  `SupportsUsernameCompletion`, `list_usernames()` returns a `list[str]` (every
  element a `str`). Skipped entirely when the capability is absent.

### 4.4 `ExpectCollector` tests + optional polish

- Add the missing `tests/unit/suite/test_expect.py`: pass/fail recording,
  `.failures` content, `reset()`, `raise_if_failures()` raise/no-raise, message
  formatting, optional logger.
- **Optional (not required):** register `otto.testing` via a pytest entry-point
  so the helpers are importable without a hard dependency edge. Decide during
  the plan; the helpers themselves are the deliverable.

---

## 5. Sample reference backends

Ship infra-free, in-memory reference implementations — small enough to teach,
real enough to exercise the full contract, no network/files so they run inside
`{doctest}` blocks and the conformance suite.

- `src/otto/examples/lab_repository.py` — e.g. an in-memory/dict-backed
  `LabRepository` constructed from a mapping of lab name → host dicts, building
  hosts via `create_host_from_dict`. Demonstrates the construct-time config
  model, raising `LabNotFoundError`, and `register_lab_repository(...)` (the
  guide shows registering it in an init module and selecting it by name).
- `src/otto/examples/reservations.py` — e.g. an in-memory dict-backed
  `ReservationBackend` (user → resources), demonstrating `backend_name`
  stability, multi-holder `who_reserved`, round-trip consistency,
  `register_reservation_backend(...)`, and the optional
  `SupportsUsernameCompletion.list_usernames()` capability (§3d).

These are:

- **Conformance-verified** in otto's own suite (helpers run against them).
- **Demonstrated** via executable `{doctest}` blocks in the guides (the
  documented code is the tested artifact).
- **Copyable/importable** by SUT authors as a starting point.

`otto.examples` becomes a small, intentionally-public surface (documented and
versioned as reference material).

---

## 6. Documentation + one-time team setup

### 6.1 New + upgraded backend guides

- **New** `docs/guide/host-database.md` — mirrors the structure of
  `reservations.md`: the interface, the config block, the built-in `json`
  backend, writing a custom backend (executable sample from
  `otto.examples.lab_repository`), selecting it in settings, the error contract,
  and **running the conformance helper** on it. Added to `docs/guide/index.rst`.
- **Upgrade** `docs/guide/reservations.md` — replace the non-executed `...`-stub
  skeleton with an executed example (sample from `otto.examples.reservations`),
  update the `who_reserved` return type/contract to the multi-holder `list[str]`
  model (§3a), add a "Verify your backend" subsection showing
  `assert_reservation_backend_conforms`, add a "Break-glass overrides"
  subsection documenting `--as-user` and `-R/--skip-reservation-check` (§3c),
  and document the optional `list_usernames()` capability that powers cached
  `--as-user` completion (§3d).
- Each backend guide opens with a "one-time, team-level setup — see the Team
  setup checklist" framing that links back to the hub (§6.2).
- API reference: ensure `otto.testing`, `otto.examples.*`, the reshaped
  `LabRepository`, and the new error types are documented (nitpicky-clean).

### 6.2 Onboarding hub: `repo-setup.md`

`docs/guide/repo-setup.md` is the spine of one-time team setup but currently
documents only `labs`, `libs`, `tests`, `init`, `[host_preferences]`,
`[os_profiles]`. Additions:

- **Field reference:** add `[lab]` (host-source backend selection) right
  alongside `labs`, and `[reservations]` (reservation gating).
- **Startup sequence:** annotate step 4 ("Lab loading") to note the source is
  pluggable and built via `build_lab_repository`.
- **New "Team setup checklist" subsection:** a single onboarding map enumerating
  the one-time decisions — `settings.toml`, host source backend, reservation
  gating, `libs`/`init`, tab completion — each linking to its guide
  (host-database, reservations, os-profiles, getting-started). Include a pointer
  to the reservation **break-glass overrides** (§3c) so operators know the
  emergency escape hatches exist before they need them.

### 6.3 Entry points link inward

- `docs/getting-started.md` § "Project setup" gains a pointer to the Team setup
  checklist.
- `docs/overview.md` "Where to go next" gains the same pointer.

Result: backend setup is part of a coherent "set otto up for your team" path
rather than isolated reference pages.

---

## 7. Testing & gate integration

- otto's own suite runs `assert_lab_repository_conforms` and
  `assert_reservation_backend_conforms` against **both** the built-ins
  (`json` host source; `null`/`json` reservations) **and** the `otto.examples`
  samples — so shipped reference code is conformance-verified in CI.
- New extension-seam tests for `build_lab_repository` (mirror
  `test_build_backend.py`): registered-name dispatch, kwargs forwarding,
  default-to-json, unknown-name error. Update `test_build_backend.py` itself —
  it currently injects a `FakeBackend` via `sys.modules` for dotted-path
  resolution; rewrite it to `register_reservation_backend(...)` a fake and
  select by name (§3b).
- Update `tests/unit/storage/test_json_repository.py` for the construct-time
  signature.
- New `ExpectCollector` unit tests (§4.4).
- New break-glass integration tests (§3c) — `CliRunner` against the top-level
  callback: `--as-user` threads the impersonated username to the backend query;
  `-R` skips the gate including when the backend is unbuildable; default runs as
  `$USER`.
- Username-completion tests (§3d) — completion-cache round-trip for `usernames`
  (schema v6); `--as-user` completion reads cached usernames and filters by
  prefix; `collect_reservation_usernames` returns `[]` (best-effort) when the
  backend lacks the capability or raises; cold-cache yields no suggestions.
- Guide `{doctest}` examples run under `make doctest`; the
  `lint_markdown_doctests.py` guard prevents `>>>`-in-non-doctest rot;
  `make doctest-src` covers any `src/otto` docstring examples (samples).
- **Verification gate:** `make coverage`, `make doctest`/`make doctest-src`,
  `ty`, and `make docs` (nitpicky, `-W`) all green. Live `make nox` /
  full-matrix run is Chris's call per project convention.

---

## 8. Phasing

One design, four phases (the implementation plan sequences them):

1. **Feature — pluggable host source.** Reshape `LabRepository`; error contract;
   `build_lab_repository` + `LabConfigSpec` + `register_lab_repository`; reframe
   `json` built-in; construct-time `JsonFileLabRepository`; wire
   `configmodule/lab.py`; unit + extension-seam tests. Backward-compatible
   default.

   **Reservation interface workstream (parallel, independent of host-source):**
   §3a multi-holder `who_reserved`, §3b registry migration
   (`register_reservation_backend`, drop dotted-path), §3c break-glass
   verification + the `-R`-skips-construction fix, §3d optional
   `SupportsUsernameCompletion` + cached `--as-user` completion. Lands in Phase 1
   alongside the host-source feature; its conformance + docs join Phases 2–4.
2. **Conformance framework.** `otto.testing` helpers for both interfaces;
   `ExpectCollector` unit tests; (optional) pytest entry-point registration.
3. **Samples + docs.** `otto.examples.lab_repository` /
   `otto.examples.reservations`; new host-database guide; reservations guide
   upgrade; conformance docs — all with executable doctests + conformance calls.
4. **Onboarding + gate.** `repo-setup.md` hub edits (field reference, startup
   note, Team setup checklist); inward links from getting-started + overview;
   conformance tests wired against built-ins + samples; full gate green.

Dependencies: Phase 2's host-source conformance needs Phase 1; Phase 3/4 need
1+2. Reservation conformance in Phase 2 can proceed independently (reservations
are already pluggable).

---

## 9. Files touched (indicative, not exhaustive)

**Source**

- `src/otto/storage/protocol.py` — reshape Protocol.
- `src/otto/storage/json_repository.py` — construct-time `search_paths`; raise
  new errors.
- `src/otto/storage/errors.py` *(new)* — `LabRepositoryError`,
  `LabNotFoundError`.
- `src/otto/storage/__init__.py` — `build_lab_repository`,
  `register_lab_repository`; re-export errors.
- `src/otto/storage/registry.py` *(new)* — lab-repository name registry +
  `register_lab_repository`, seeded with the built-in `json` backend.
- `src/otto/models/settings.py` — `LabConfigSpec`; wire into `SettingsModel`.
- `src/otto/configmodule/lab.py` — use the factory; drop `search_paths` from the
  call.
- `src/otto/testing/__init__.py` *(new)* — conformance helpers.
- `src/otto/examples/lab_repository.py`, `src/otto/examples/reservations.py`
  *(new)*.
- `src/otto/reservations/registry.py` *(new)* — reservation-backend name
  registry + `register_reservation_backend`, seeded with `none`/`json`.
- `src/otto/reservations/__init__.py` — `build_backend` resolves via the
  registry (drop `importlib`/dotted-path); export `register_reservation_backend`.
- `src/otto/reservations/protocol.py` — multi-holder `who_reserved` (§3a) +
  optional `SupportsUsernameCompletion` Protocol (§3d).
- `src/otto/reservations/null_backend.py`, `json_backend.py` — multi-holder
  `who_reserved` (§3a).
- `src/otto/cli/main.py` — when `-R` is set, skip `build_backend` entirely
  (`backend=None`, `skip_check=True`); the `Exit(1)`-on-build-failure path
  applies only without `-R` (§3c). Add `--as-user` `autocompletion=` from the
  cache (§3d).
- `src/otto/reservations/check.py` — `gate()` emits the loud "check SKIPPED"
  warning whenever `skip_check` is true, before the `backend is None` early
  return (§3c).
- `src/otto/cli/reservation.py` — `whoami`/`check` build their own backend on
  demand (the only place that still needs it under `-R`) (§3c).
- `src/otto/configmodule/completion_cache.py` — `SCHEMA_VERSION` 5 → 6,
  `usernames` payload key, `collect_reservation_usernames`, `read_cache` /
  `write_cache` extension (§3d).
- `src/otto/configmodule/__init__.py` — collect usernames in the slow path and
  pass to `write_cache` (§3d).

**Tests**

- `tests/unit/storage/test_build_lab_repository.py` *(new)*.
- `tests/unit/storage/test_json_repository.py` — signature update.
- `tests/unit/suite/test_expect.py` *(new)*.
- `tests/unit/testing/test_conformance.py` *(new)* — helpers vs built-ins +
  samples.
- `tests/unit/examples/` *(new)* — sample-specific behavior if needed.
- `tests/unit/reservations/test_null_backend.py`, `test_json_backend.py`
  (+ multi-holder case), `test_check.py`, `test_build_backend.py`,
  `tests/unit/cli/test_reservation.py` — `who_reserved` → `list[str]` (§3a).
- `tests/unit/cli/test_reservation_flags.py` *(new)* — break-glass integration
  tests for `--as-user` / `-R` end-to-end through the callback (§3c).

**Docs**

- `docs/guide/host-database.md` *(new)*, `docs/guide/index.rst`.
- `docs/guide/reservations.md` — skeleton → executed; multi-holder return;
  `register_reservation_backend` in an init module + bare-name selection
  (replaces the dotted-path instructions); migration note; add verify
  subsection. `docs/api/reservations.rst` — return-type +
  `register_reservation_backend`.
- `docs/guide/repo-setup.md` — `[lab]` + `[reservations]` field entries; startup
  note; Team setup checklist.
- `docs/getting-started.md`, `docs/overview.md` — inward links.
- `docs/api/*` — `otto.testing`, `otto.examples`, reshaped Protocol, error types.

---

## 10. Risks / open items (confirm at plan time)

- **`supports_location` removal** — confirm no live caller before deleting.
- **Multi-repo backend selection** — validate the "first repo declares, json
  aggregates" rule against current `configmodule/lab.py` aggregation.
- **`load_lab` signature change** — one internal caller plus tests; verify no
  other callers (e.g. `library-usage` doctests, `configmodule`) rely on
  `search_paths`.
- **`otto.examples` as public surface** — keep deliberately minimal; it is
  reference material and will be documented/versioned as such.
- **Backward compatibility** — repos with no `[lab]` block must behave exactly
  as today (json over merged `labs`); covered by an explicit regression test.
- **Reservation dotted-path hard cutover** (§3b) — removing `pkg.mod:ClassName`
  resolution breaks any existing config using it. Audit for in-tree/lab-data
  usage, provide the one-line init-module migration in the reservations guide,
  and confirm no shipped fixture/test still relies on dotted-path selection.
- **`-R`-skips-construction behavior change** (§3c) — the callback currently
  `Exit(1)`s on a build-time `ReservationBackendError` regardless of `-R`.
  Decided fix: with `-R`, do not construct the backend at all (so a constructor
  that fails *or hangs* can't block). It is a behavior change to a safety path —
  verify the loud "check skipped" warning still fires (gate-ordering subtlety
  above), and that `otto reservation whoami`/`check` still work by building on
  demand.
- **Username-collection latency / blocking** (§3d) — enumerating users can hit
  the network. `collect_reservation_usernames` must be best-effort and
  non-blocking (swallow errors → empty list; consider a short timeout) so it
  never stalls a normal command. Settle the collection hook (slow-path collector
  vs callback refresh) and the double-build trade-off at plan time. The schema
  bump (5 → 6) is safe — `read_cache` rebuilds on version mismatch.
```
