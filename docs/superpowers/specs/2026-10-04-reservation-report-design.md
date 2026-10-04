# `otto.reservations`: one report, read by every reservation check

**Status:** approved in chat section by section (Chris, 2026-10-04).
**Issues:** fixes #588; refs #525 (thin-CLI series follow-up to item 7, #508).
**Principle:** `docs/architecture/principles.md`, "Input rules live in the
library entry point". Worked example: the item 7 spec
(`2026-10-03-session-library-design.md`), which left this item open.

## 1. Intent

`otto.reservations` owns the backends, identity resolution, the gate and the
verdict, but three call sites still decide reservation questions themselves:

- **`otto reservation check`** (`src/otto/cli/reservation.py`) builds its
  report in the CLI command: the hosts in play, the required-resource origins,
  the held set, the `none`-backend short-circuit, the empty-requirement
  short-circuit, the expiry warning, and then the verdict. A library caller can
  get the verdict (`check_reservations`) but not the facts the table shows, and
  the table and the verdict agree only because the command re-derives the same
  predicates by hand.
- **The out-of-fleet named-host check** (`check_named_hosts` in
  `src/otto/cli/host.py`) calls `check_reservations` with its own skip rules
  (built-in `local`, hosts the fleet covers, hosts declaring no resources) and
  its own expiry nudge.
- **`otto reservation whoami`** composes identity and backend in the CLI command.

And `otto.open_context` does not apply the gate at all. The cookbook says
"Reservation checks are a CLI concern", which contradicts item 7's rule that
the library path makes every decision the CLI makes.

The example third-party CLI (`src/otto/examples/reservations_cli.py`) teaches
the old pattern. It hand-builds the identity, backend and gate, installs an
`OttoContext` by hand, has no `-R`, and uses different exit codes from otto.

## 2. Rulings (Chris, 2026-10-04)

1. **`open_context` applies the gate by default**, with `holder=` and
   `skip_reservation_check=` mirroring `--holder` and `-R`. It is breaking only
   for repos that configure a real backend. With no `[reservations]` table
   anywhere, the backend resolves to `none` and the gate is a no-op.
2. **The named-host check moves into the library**, so all three call sites go
   through `otto.reservations` and none re-derives the guards.
3. **Gate-centric API.** `ReservationGate` is the one entry point; it already
   carries the identity, the backend, the skip flag and the lazy backend
   factory. Rejected: a free report function with every caller unpacking
   identity and backend itself (the re-derivation this item removes), and a
   report beside an untouched gate (two parallel ways to ask the same
   question).
4. **The example CLI follows otto's rules.** It parses, constructs through the
   library, calls one entry point, renders, and translates errors in one place
   with otto's exit codes. A test keeps it agreeing with otto.
5. **CLI output is byte-identical**, as in item 7: tables, refusal text, the
   `-R` warning, the `--holder` banner and exit codes.
6. **Backwards compatibility is not a constraint** (AGENTS.md).

## 3. Shape

### 3.1 The report

```python
@dataclass(frozen=True)
class ReservationRow:
    resource: str
    level: ResourceLevel           # "lab" | "element" | "host"
    owner: str
    held: bool | None              # None: the `none` backend cannot answer

@dataclass(frozen=True)
class MissingResource:
    resource: str
    holders: list[Reservation] | None   # None: the backend cannot report other users

@dataclass(frozen=True)
class ReservationReport:
    lab: str
    username: str
    in_play: list[str]
    rows: list[ReservationRow]          # required_resource_origins order
    null_backend: bool
    expiring: list[Reservation]         # held, required, ending within EXPIRY_WARNING_WINDOW
    missing: list[MissingResource]      # sorted by resource
    @property
    def covered(self) -> bool: ...      # not self.missing
```

`build_report(lab, username, backend, *, host_ids=None, now=None) ->
ReservationReport` is the one function that decides when the backend is
queried. It keeps today's order and rules:

1. **Walk the requirement first** (`required_resource_origins`). An unknown
   host id, or an element resource with no element identity, is still a bug
   that raises, even under `none`.
2. **No requirement:** no backend call. Return empty rows, nothing missing.
3. **`none` backend:** no backend call. Every `held` is `None` and nothing is
   missing.
4. **Otherwise:**
   - Check that the backend's `username` agrees with the one asked about (the
     existing `RuntimeError`).
   - Make **one** `active_reservations` call; that sets `held` and `expiring`.
   - For each missing resource, call `holders()` only if the backend supports
     `SupportsResourceHolders`; otherwise `holders=None`.

`check_reservations(lab, username, backend, *, host_ids=None)` stays public. It
builds the report and raises `MissingReservationError.from_report(report)` when
anything is missing.

`announce_expiring(report)` logs the expiry nudge for `report.expiring`. It
keeps the process-wide `(resource, end)` suppression and the reader's-zone
clock time. `warn_expiring_reservations` is replaced by it; its one remaining
internal use goes too.

### 3.2 The gate

`ReservationGate` keeps its fields and gains:

- **`gate.report(lab, host_ids=None) -> ReservationReport`**: builds the report
  for the gate's identity. The backend is the gate's own, or under `-R` is
  built lazily from `backend_factory`, as `otto reservation check` does today:
  a status report ignores `-R`. Raises `ReservationBackendError` if the backend
  cannot be built or queried, and `RuntimeError` if no identity was resolved.
- **`gate.evaluate(lab=None, host_ids=None) -> ReservationGateResult`**: same
  behaviour as today.
  - Under `-R` it returns the skip warning and logs it; no check runs.
  - With no backend it is a silent no-op.
  - Otherwise it reports over `host_ids`. If any are missing it raises
    `MissingReservationError.from_report`; on a pass it calls
    `announce_expiring(report)` and returns.

  `lab=None` reads the active lab (`get_lab()`). `host_ids=None` reads the
  hosts in play (`get_hosts_in_play()`) when `lab` was also omitted, and every
  host of the lab when a `lab` was passed. An explicit lab therefore needs no
  `OttoContext`. `ReservationGateResult` gains `report: ReservationReport |
  None` (`None` on the skip and no-backend paths).
- **`gate.check_hosts(lab, hosts) -> ReservationReport | None`**: the named-host
  check from `cli/host.py`, with its rules moved verbatim.
  - **A no-op returning `None`** under `-R`, with no backend, or with no
    identity.
  - **Hosts skipped:** a host in the fleet; the built-in `local` host; a host
    declaring neither `resources` nor element resources (a local read, never
    the backend).
  - **Otherwise:** one report over the remaining hosts. It raises when anything
    is missing; on a pass it announces the expiring bookings.

  `tach` forbids `otto.reservations` → `otto.host`, so the built-in-host
  predicate is reached through `otto.config`, or the caller passes the fleet
  with `local` excluded. The plan picks the seam. The rule's behaviour does not
  change.
- **`gate.identity_report() -> ReservationIdentity(username, source,
  backend_name)`** for `whoami`. Builds the backend lazily under `-R`.

### 3.3 Constructing a gate

`gate_from_settings(settings, repo_dir, *, holder, skip_reservation_check) ->
ReservationGate` holds the construction rules:
- the identity is resolved before the backend, which is built for that
  username;
- under `-R` no backend is built, only the lazy factory;
- a construction failure raises `ReservationBackendError`.

`build_reservation_gate(repos, *, holder, skip_reservation_check,
cwd_fallback)` becomes "pick the first repo with a `[reservations]` table (else
`{}` at `repos[0].sut_dir` or `cwd_fallback`), then delegate". So otto and a
third-party tool build gates the same way.

### 3.4 Errors

- **`MissingReservationError`** gains `report: ReservationReport | None` and a
  classmethod `from_report(report)`. The class formats the message from the
  report, with the same text as today: header line, then one line per origin
  with `(held by: …)`, where `nobody` means an empty list and `unknown — this
  backend cannot report other users` means `None`. Constructing it from a plain
  string still works.
- **`ReservationBackendError`** is unchanged. The CLI's "pass `-R`" translation
  stays CLI-side. The library and the example raise it plain, and the docs name
  `skip_reservation_check=True`.

### 3.5 The CLI renders

All output is byte-identical (ruling 5).

- **The preamble** is unchanged. `ensure_lab_context` builds the gate after the
  lab and before the context is installed. `present_reservation_gate` calls
  `evaluate()` after the inactive-instruction and dependency refusals, on
  commands whose spec gates.
- **`otto reservation check`** renders `gate.report(get_lab(),
  get_hosts_in_play())`:
  1. With no rows, it prints the existing sentence.
  2. Otherwise it renders the table from `report.rows`: same title, rich
     escaping, `n/a` for `None`, green `yes` and red `no`.
  3. Then `announce_expiring(report)`. It comes after the table and before the
     verdict, and is not suppressed by `-R`, as today.
  4. Then the verdict: `fail(MissingReservationError.from_report(report))` when
     anything is missing, otherwise the green OK line.

  It no longer imports `active_reservations`, `is_null_backend` or
  `required_resource_origins`.
- **`otto reservation whoami`** renders `gate.identity_report()`. The `lab:`
  line stays CLI-side, because it echoes the CLI's own `--lab`.
- **`otto host`**: `check_named_hosts` becomes `gate.check_hosts(lab, named)`,
  with `fail(e)` on `MissingReservationError`.
- **Kept CLI-side:** the `ReservationBackendError` translation (exit 1, naming
  `-R`), the magenta `--holder` banner, and `ctx.meta["otto_reservation"]`.

### 3.6 `open_context`

`open_context(..., holder: str | None = None, skip_reservation_check: bool =
False)`, in the CLI's order:

1. **After the lab is built** (or a passed `Lab` is taken as given), and
   **before the context is installed**, build the gate with
   `build_reservation_gate(repos, holder=holder,
   skip_reservation_check=skip_reservation_check, cwd_fallback=Path.cwd())`.
   An unbuildable backend raises `ReservationBackendError` before any context
   exists.
2. **After `check_dependencies`, before `yield`:** call `gate.evaluate()`.
   - A missing resource raises `MissingReservationError` with `.report`.
   - The variant and context are restored on that exit, as on every other
     setup refusal.
   - `dry_run=True` is gated too, because the CLI gates before its dry-run
     seam.
3. **`holder=`** logs "acting as …" at INFO as plain text; the library carries
   no rich markup. **`skip_reservation_check=True`** builds no backend, and
   `evaluate()` logs the SKIPPED warning it already logs.

The open_context docstring and the cookbook's parameter table gain the two
parameters next to their CLI flags.

### 3.7 The example CLI

`src/otto/examples/reservations_cli.py` mirrors otto's shape.

- **Flags:** `--holder` and `-R` / `--skip-reservation-check` (otto's
  spellings), plus `--backend` and repeatable `--resource`.
- **`gate` command:**
  - builds the gate with `gate_from_settings({"backend": name}, Path.cwd(),
    holder=…, skip_reservation_check=…)`;
  - builds a `Lab` from `--resource`;
  - calls `gate.evaluate(lab)`;
  - prints the skip warning if one comes back, otherwise `OK`.
- **`check` command:** renders `gate.report(lab)` as one line per row plus the
  verdict.
- **One translation point:**
  - `MissingReservationError` prints its message and exits 1;
  - `ReservationBackendError` prints "reservation backend unavailable: …" with
    the `-R` hint and exits 1.

  These are otto's codes. Today's 2 for an unavailable backend goes.
- **No logic of its own:** no `set_context`, no hand-built `ReservationGate`,
  no `resolve_username` or `build_backend` sequence.
- **Its doctest** keeps exercising the library through the module's
  render-and-translate helpers against `ExampleReservationBackend`.

### 3.8 Layering

`open_context` adds the edge **`otto.context` → `otto.reservations`**. Both
modules are already in the import cycle (#590), so the edge adds no member. The
plan re-measures with `scripts/render_module_graph.py` and records the edge in
`tach.toml` with its reason. No other new edges: `otto.cli`, `otto.examples`
and `otto.reservations` → `otto.config` already exist.

## 4. Testing

Every new test is proven red against a planted fault before it is trusted.

1. **The query rules**, with a backend that counts its calls:
   - zero calls when nothing is required;
   - zero calls under `none`;
   - exactly one `active_reservations` call otherwise;
   - `holders()` only for missing resources, and never when the backend lacks
     the capability;
   - the requirement-walk bugs still raise under `none`;
   - `expiring` respects the window and the clock passed as `now`.
2. **The CLI is unchanged.** The output assertions of the existing tests for
   `otto reservation check`, `whoami`, the `otto host` named-host check and the
   preamble gate stay as they are: the same tables, lines and exit codes. A
   test that patches an internal this item moves is retargeted to the
   library, and its expected output is not edited. A #525-style differential
   asserts that `otto reservation check`'s rows and verdict equal
   `gate.report()` over the same lab: the resource, level, owner and held
   columns, and the OK or refusal line.
3. **`check_hosts`.** The named-host check's existing tests move with it:
   - fleet hosts skipped;
   - built-in `local` skipped, but a lab's own `local` entry not;
   - hosts without resources skipped, with zero backend calls;
   - one query for two named hosts, naming both;
   - `-R` and no-backend are no-ops.
4. **`open_context`**, against the JSON backend in `tmp_path`:
   - it refuses with `MissingReservationError` and `.report`;
   - it passes when the resources are held;
   - `skip_reservation_check=True` builds no backend (the factory is never
     called) and logs the warning;
   - `holder=` changes the identity checked;
   - `dry_run=True` is gated;
   - a passed `Lab` is gated;
   - no `[reservations]` table means a no-op;
   - an unbuildable backend raises before any context is installed, and the
     variant is restored.
5. **The example.** The example's `check` and `otto reservation check` agree on
   the verdict and on which resources are held and missing, for the same lab
   and the sample backend. Its exit codes match otto's.
6. **Golden and marks.** The public-API golden is regenerated; the
   breaking-change marks pass `make check-breaking`.

## 5. Documentation

- **`docs/cookbook/python-library.md`:**
  - "Reservation checks are a CLI concern" becomes "`open_context` applies the
    reservation gate, like the CLI", covering the two parameters and
    `MissingReservationError.report`.
  - The parameter-to-flag table gains `holder` → `--holder` and
    `skip_reservation_check` → `-R`.
- **`docs/cookbook/extending/reservation-backends.md`**, "Using the reservation
  library in your own CLI", is rewritten around the new example: construct
  with `gate_from_settings`, call `evaluate` or `report`, render, translate.
- **`docs/architecture/subsystems/reservations.md`** gains a short section: the
  report is the single source of reservation facts, and the gate's methods and
  the CLI are renderers of it. It links to the API docs, not a restatement.
- **The CLI reference pages** do not change, because the output does not.

## 6. BREAKING (migration recipe for the squash message)

- **`open_context` applies the reservation gate.** Under a repo that configures
  a reservation backend, a script whose user does not hold the lab's resources
  now raises `MissingReservationError`. To check as another user, pass
  `holder="name"`. To opt out (the `-R` break-glass), pass
  `skip_reservation_check=True`.
- **`warn_expiring_reservations` is replaced by `announce_expiring(report)`.**
- **`examples/reservations_cli.py`**: `run_check` is gone. The example now
  exits 1, not 2, when the backend is unavailable.

## 7. Out of scope

- Banning `import typer` outside `otto.cli`: #513 (options metadata) plus the
  completion-cache move named in #590.
- Gating library verbs other than `open_context` (e.g. named-host checks from
  library host verbs). The gate is applied once per run, as on the CLI.
- Any change to the reservation backends, the window predicate or identity
  precedence.
