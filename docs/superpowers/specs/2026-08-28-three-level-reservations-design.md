# Three-level reservations — labs, elements and hosts as reservable units

**Date:** 2026-08-28
**Status:** Designed (this session); awaiting implementation plan
**Depends on:** `2026-08-27-lab-definition-v2-design.md` (landed on main as `0fba081a`,
unpushed — this spec revises v2 §8 freely, since nothing depends on it yet)
**Companion:** `2026-08-28-host-inventory-layer-design.md` (independent: it touches
inventory-owned fields; this spec touches `resources`, which is otto-owned. Nothing
here moves the completion cache's `SCHEMA_VERSION` — §8.)

## 1. Problem

v2 made the lab the reservable unit — `resources` lives only in the `labs`
table and `required_resources(lab)` is `set(lab.resources)` — on the premise
that reservation never happened below the element. It does: several projects
deploy onto the same element (a multi-slot chassis), each project tests and
uses specific slots, and which slots is a per-project choice, sometimes settled
only when the run is configured. Today the only spelling is one whole-lab lock,
which serialises projects that could run side by side.

The obvious workaround — one dotted sub-lab per slot — fails on a v2 decision
that stands: membership is per **element**, so two boards of one chassis
element cannot sit in different labs (v2 §7; ruling R14 made a test writer
refuse exactly this shape). Slots below the element need resources of their own.

A lab is a collection of elements. What is reservable in a lab is a **mix of
elements and hosts**, plus infrastructure that belongs to the lab as a whole.
So `resources` becomes an optional declaration at all three levels, and the
gate reserves what the run will actually touch.

### In scope

- `resources` on elements and on host entries, beside the existing lab-level
  set (§2); how each reaches the runtime host (§3).
- `required_resources` over the hosts **in play**, and what "in play" means
  (§4–§5): the project's fleet of interest, which the scoping work already
  defines and every fleet walk already uses.
- The gate, `otto reservation check`, the `-R` warning and the missing-resource
  error, all naming *where* each requirement came from (§5).
- The doctor's overlap rule, extended for element- and host-level protection
  (§7); schema and completion cache (§8); backend-protocol wording (§9);
  fixtures, tests and docs (§10–§12).

### Out of scope

- Acquisition. The gate remains a **check** — "does this identity hold what the
  run needs" against `get_reserved_resources(username)`. Booking stays the
  reservation system's job.
- A global, per-invocation override of the fleet of interest (a bootstrap-level
  `--hosts`). Today the universe is declared per project in `[project]
  host_patterns`; the verb-level `--hosts` flags (`monitor`, `tunnel`) narrow a
  walk, not the gate, and this spec keeps that (§5). If per-invocation
  narrowing of the *gate* turns out to be needed, it is a small follow-up on
  top of this design, not a change to it.
- The reservation backend protocol's shape. Only its docstring wording changes (§9).
- Anything inventory-related: resource identifiers are otto-owned lab-file
  data, the reservation system's namespace, not a fact about a machine.

## 2. Data model

```json
{
  "labs": { "rig": { "resources": ["rig-pdu"] } },
  "elements": [
    { "name": "chassis", "id": 1, "labs": ["rig"], "resources": ["chassis-1"],
      "hosts": [
        { "ip": "10.0.0.11", "board": "slot", "slot": 1, "resources": ["chassis-1-slot-1"],
          "creds": [{"login": "admin", "password": "…"}] },
        { "ip": "10.0.0.12", "board": "slot", "slot": 2, "resources": ["chassis-1-slot-2"],
          "creds": [{"login": "admin", "password": "…"}] }
      ] },
    { "name": "gw", "labs": ["rig"],
      "hosts": [ { "ip": "10.0.0.1", "creds": [{"login": "admin", "password": "…"}] } ] }
  ]
}
```

| Level | Where | Meaning |
|---|---|---|
| lab | `labs.<name>.resources` (unchanged) | Infrastructure the lab shares as a whole — a switch, a PDU, a bed. |
| element | `elements[].resources` — **new**, `set[str]`, default empty | The element reserved as one unit. |
| host | host entry `resources` — **re-allowed**, `set[str]`, default empty | The slot. |

Every level is optional and they combine freely. A lab whose every element
carries a resource leaves nothing unguarded — every host in play then requires
something — but that is coverage, not a whole-lab lock: N elements declaring N
identifiers admit N concurrent holders, where one lab-level identifier admits
one. A chassis shared slot-by-slot needs only host-level entries; the example
above needs all three because it has all three kinds of thing.

Model changes:

- `ElementSpec.resources: set[str] = Field(default_factory=set)`.
- `HostSpec.resources: set[str] = Field(default_factory=set)` returns.
  `HOISTED_HOST_KEYS` shrinks to `{"element", "element_id", "labs"}`;
  `_drop_hoisted_keys` in the schema builder and the completeness guard's
  comment follow. The v2 §11 migration error text no longer lists `resources`
  among the keys that "moved to the labs table".
- Identifiers at every level are opaque strings matched byte-for-byte by the
  backend, as today. The same identifier may legitimately appear at more than
  one level or on more than one host (two slots sharing one physical lock);
  `required_resources` returns a set, so it costs nothing.

The test helper `lab_json_v2` keeps hoisting a flat v1 host's `resources` into
the labs table — its input is v1-shaped, and in v1 host resources aggregated
into the lab. Writers that want element- or host-level resources write the v2
shape directly (a `resources=` kwarg on `lab_json_v2` is **not** added; the
helper is for migrating v1 fixtures, not for authoring v2 ones).

## 3. Runtime carriage

Each level reaches the host by the road its neighbour already travels:

- `LabInfo.resources` stays the **lab-level** set (v2 §4), stamped by
  `config.lab.load_lab`'s sweep. Its docstring says so explicitly: element and
  host resources are on the host, not here.
- `RemoteHost.element_resources: frozenset[str]` — copied from the element by
  the factory through a new loader kwarg `create_host_from_dict(...,
  element_resources=None)`, exactly like `element_metadata` (a loader
  argument; the host spec forbids `element_resources` on the entry). The json
  backend passes `element.resources`.
- `RemoteHost.resources: frozenset[str]` — the host's own, from `HostSpec.resources`
  via `_common_host_kwargs`.
- Containers and the builtin `local` host: both empty. They are never
  reservable units — and `local` is additionally held out of the hosts in play
  altogether (§5), so the guarantee does not rest on the field staying empty.

An element is in play exactly when at least one of its hosts is, so carrying
the element's set on each member host makes "the union over elements in play"
fall out of "the union over hosts in play" with no element registry at runtime.

## 4. `required_resources`

```python
@dataclass(frozen=True)
class ResourceOrigin:
    resource: str
    level: Literal["lab", "element", "host"]
    owner: str            # lab name, element key (e.g. "('chassis', 1)"), or host id

def required_resource_origins(lab: Lab, *, host_ids: Iterable[str] | None = None) -> list[ResourceOrigin]: ...
def required_resources(lab: Lab, *, host_ids: Iterable[str] | None = None) -> set[str]: ...
```

- `required_resources(lab, host_ids=S)` =
  `lab.resources ∪ ⋃_{h ∈ S} (h.element_resources ∪ h.resources)`.
- `host_ids=None` means **every host in the lab** — the conservative reading,
  and today's semantics when everything is lab-level. It is the default so
  that a caller with no scope information (a library user, the completion
  path's temporary context) never under-reserves.
- `required_resource_origins` is the structured form; `required_resources`
  is derived from it, so the two cannot disagree. The origin list is sorted
  by resource, then level order lab → element → host, then owner, so output
  is stable across runs.
- An element `owner` renders the way the repo renders an element everywhere —
  `str(ElementKey(name, id))`, so `('chassis', 1)`, and `('gw', None)` for a
  single-instance element. It is a display value; nothing parses it back.
- An id in `host_ids` that the lab does not contain is a `ValueError` naming
  it — the caller passed a fleet from a different lab, which is a bug, not a
  condition to skip past.

## 5. In play — the fleet of interest

The scoping work (spec 2026-08-18) already defines the run's universe: each
repo's `[project] host_patterns` OR-ed into that repo's universe, the fleet
being the union across declaring repos, and the whole lab when no repo declares
one. `OttoContext.admissible_ids(None)` computes it live and every fleet walk
starts from it. The reservation side reads that same set — the gate in both
its checked and `-R` forms, `otto reservation check`, and completion's gate —
so "in play" has one definition and no second list to keep in step. They
differ from a walk in one keyword and one subtraction: a walk demands a
non-empty fleet, while the reservation readers pass `require_nonempty=False`
and take zero hosts in play as an answer; and every reservation reader goes
through `get_hosts_in_play()`, which holds the built-in `local` host out of
the set (below). Both differences live in that one accessor, so the four
readers cannot drift from each other:

- `OttoContext.admissible_ids(owner=None, *, require_nonempty=True)` becomes
  public (the private name stays as an alias for one release of internal
  callers; there are none outside the class today). Unchanged for walks,
  including raising `ProjectScopeError` when a declared scope admits nothing;
  `require_nonempty=False` is the reservation readers' spelling, which returns
  the empty set as the answer it is.
- `ReservationGate.evaluate()` computes `needed = required_resources(lab,
  host_ids=get_hosts_in_play())` — `otto.config.fleet.get_hosts_in_play()`, a
  lazy one-line reader of that set sitting beside `get_lab()`, because
  `tach.toml` does not grant `otto.reservations` the context module.
- A declared scope that admits nothing is **zero hosts in play**: the
  requirement narrows to the lab-level set and the gate reaches a verdict. The
  refusal belongs to the walk, which is the surface that would otherwise
  silently touch nothing — a run that walks its fleet still aborts with the
  same message, one step after the gate; a run that walks none (`otto run
  <instruction>`, `--show-lab`, `otto reservation check`) never had that abort
  and does not acquire one from the gate.
- `check_reservations(lab, username, backend, *, host_ids=None)` takes the
  same keyword; `MissingReservationError` lists each missing identifier with
  its origins — the slot, not just the string:

  ```
  User 'chris' does not hold all resources required by lab 'rig'. Missing:
    chassis-1-slot-2  host chassis1_slot2  (held by: dana)
    rig-pdu           lab rig  (held by: nobody)
  ```

- `otto reservation check` prints the requirement as a rounded Rich table —
  resource, level, owner, held — before the verdict (the dense-output rule).
  A requirement with no rows prints one sentence instead of an empty box, and
  its backend is never queried, so a scheduler that is up but unhappy cannot
  turn an empty requirement into a failure. Under `backend = "none"` the
  `held` column reads `n/a`: that backend holds nothing and is never asked, so
  there is no verdict to render per row. The `-R` skip warning stays one
  plain-text line listing the identifiers.
- Naming a host explicitly adds it to the gate's set. `otto host <id> --hop
  <id>` is unscoped by design — explicit targeting beats scoping — so the
  named target's and hop's own element- and host-level resources are required
  when they fall outside the fleet: holding the fleet's slots is not
  permission to touch a slot nobody reserved, and reaching a fleet host
  through an unreserved jump box is still using the jump box.
- The built-in `local` host is **never** in play. It is a member of every lab
  and scoping admits by pattern rather than by id, so it reaches the
  reservation readers' base set on the whole-lab fallback and wherever a
  declared scope's patterns match it — and `get_hosts_in_play()` subtracts it
  there, leaving a three-machine lab reporting `3 host(s) in play`. otto can
  always run on the machine it is running on, so a reservation standing
  between a user and `otto host local <verb>` costs them a run and buys nobody
  a slot. The subtraction is by host identity (`is_builtin_host`), not by the
  id string: a lab that declares its own `local` entry suppresses the built-in
  host altogether, and that entry's resources are enforced like any other's.
  Fleet WALKS are untouched — `include_local=True` remains their own opt-in.
- Completion's reservation gate (`remote_completion.py`) builds a temporary
  context; it passes that context's hosts in play plus any host the command
  line names, so completion and the run agree about what is needed.
- Verb-level `--hosts` (`otto monitor`, `otto tunnel`) narrows a **walk**
  inside a run the gate has already admitted; it does not narrow the gate,
  which runs in `command_preamble` before any verb. The reservations guide
  states this in one sentence, with the fix: narrow `[project] host_patterns`
  in the project that runs the slot. This is what "the project defines the
  runtime values" means in practice — the universe is project data.

## 6. Composite

No new merge unit. Element resources replace with the element (wholesale, v2
§6); host resources ride inside the element entry; the labs entry replaces as
today. A later source that restates an element without `resources` therefore
*removes* the element-level lock — consistent with "wholesale", and the
existing override warning already names both sources.

## 7. Doctor

The v2 §8.3 warning ("two labs share elements but declare disjoint resources")
exists because lab-level resources are declared, not derived, so a shared
element could go unprotected. With element and host levels there are two more
ways to protect it. A shared element is **protected** when the element
declares at least one resource, or every one of its host entries declares at
least one. The warning fires for a pair of labs only when: both declare at
least one lab-level resource (ruling R18 stands), their lab-level sets are
disjoint, **and** at least one shared element is unprotected — and it names
those unprotected elements, not the protected ones.

`lab_warnings` needs each element's `resources` and its raw host entries'
`resources`; both are already in the `ElementSpec` the doctor holds.

## 8. Schema and completion cache

- `lab.schema.json` picks up `ElementSpec.resources` and `HostSpec.resources`
  from the models; the only hand edit is `_drop_hoisted_keys` no longer
  dropping `resources`. `x-otto-version` re-stamps as always.
- `completion_cache.SCHEMA_VERSION` does not move and no cache key gains the
  admissible ids. Nothing `completion_cache.json` holds — labs, tests, host
  summaries — depends on the fleet, and the completion gate's own answer lives
  in a separate sidecar (`remote_completion_cache`, its own `SCHEMA_VERSION`)
  that keys on the username and stores the identity's *holdings*, asking
  `required <= holdings`. A narrower fleet requires less of the same holdings,
  so it is answered correctly by an entry written under a wider one — a
  new key would buy nothing and cost every user a cold rebuild.

## 9. Backend protocol wording

`ReservationBackend.get_reserved_resources` promises identifiers that match
"byte-for-byte the values in `Lab.resources`". That sentence becomes "the
identifiers `required_resources` computes — lab, element and host levels
alike"; the protocol's shape and `assert_reservation_backend_conforms` are
unchanged. `library/reservation-backends.md` says the same.

## 10. Errors and edge cases

| Situation | Behaviour |
|---|---|
| `resources` on a host entry | Allowed again; a set of non-empty strings, like the other two levels. |
| `resources` on an element | Allowed; same type. |
| Same identifier at two levels / two hosts | Fine; `required_resources` is a set. |
| `host_ids` names a host not in the lab | `ValueError` naming it (§4). |
| No repo declares `[project]` | In play = whole lab; identical to v2 behaviour for lab-level-only files. |
| Declared scope admits nothing | Zero hosts in play; the requirement is the lab-level set alone and the gate reaches a verdict. The `ProjectScopeError` stays with the first walk (§5). |
| Identity holds the lab's resources but not an in-play slot's | `MissingReservationError` naming the slot's host id. |
| Identity lacks a resource of a host **outside** the fleet that the run does not target | Not required; the run proceeds. That is the point. |
| Identity lacks a resource of a host outside the fleet that the run **names** (`otto host <id>`, `--hop`) | Required and checked: explicit targeting is unscoped, so the named host's own element- and host-level resources are demanded before the command runs (§5). |
| `-R` | Warning lists the in-play requirement; nothing checked. |
| Element restated by a later source without `resources` | Element-level lock removed for that element (wholesale); override warning already fires. |
| Container / builtin `local` | Never contribute resources. `local` is not counted among the hosts in play either — the reservation readers subtract it by identity (§5, §14). |
| Doctor: shared element protected at element or host level | No overlap warning for that element. |
| Doctor: shared element unprotected, lab sets disjoint and both non-empty | Warning naming the unprotected element(s) (R18 posture kept). |

## 11. Fixtures and tests

Red-first, mutation-proved, per the standing rule.

- `tests/_fixtures/lab_data/tech1/lab.json`: the `test1` element gains an
  element-level resource and the `test2` element's host entry gains a
  host-level one, so the bed-facing fixtures exercise every level through the
  loader. Every tech1 element holds exactly one host, so the two slots of one
  element — the shape §1 is about — stay pinned by hand-built labs in the
  unit tests rather than by a bed fixture. The pinned resource sets
  in the identity test (`test_pinned_identities.py` after the companion's
  rename, `test_v2_equivalence.py` otherwise) are updated **deliberately** in
  the same commit, with the diff called out in the commit message — they pin
  lab-level `Lab.resources`, which does not change; the new levels get their
  own pins.
- `tests/unit/reservations/`: `required_resource_origins` / `required_resources`
  at each level and in combination; `host_ids=None` = all; unknown id raises;
  origin ordering; `check_reservations` message names host ids;
  `ReservationGate.evaluate` uses the admissible set — the mutation that makes
  the gate pass `None` must turn the "slot 2 not required when the project
  targets slot 1" test red.
- `tests/unit/config/` / `tests/unit/cli/`: `admissible_ids` public contract;
  `otto reservation check` table; `-R` line; completion passes the fleet;
  `get_hosts_in_play` subtracts the built-in `local` host while
  `admissible_ids` still returns it (the walk's `include_local` knob is
  untouched), and a lab-declared `local` entry is NOT subtracted — the guard
  that fails if the exclusion is ever keyed on the id string.
- `tests/unit/host/`: factory stamps `element_resources` before providers
  run; `resources` from the spec; containers/local empty.
- `tests/unit/labs/`: element resources survive composite replacement and
  vanish when the later source omits them; `lab_json_v2` behaviour pinned
  (v1 host `resources` still hoist to the lab).
- `tests/unit/labs/test_doctor.py`: protected-by-element, protected-by-every-host,
  unprotected-one-host, and the R18 both-non-empty guard.
- `tests/unit/docs/test_lab_config_field_coverage.py`: `resources` rows at
  element and host level are required by construction once the field returns
  to `HostSpec`; the comment that said `resources` "left HostSpec outright"
  is updated.

## 12. Documentation

- `guide/cli/reservation/index.md` "What gets checked, and where": the three
  levels, the fleet-of-interest rule, the `--hosts`-does-not-narrow-the-gate
  sentence, the worked example from §2. `check.md`: the new table.
- `guide/configuration/lab-config.md`: `resources` rows for element and host;
  the labs-entry row reworded ("shared by the lab as a whole"); the migration
  note drops `resources` from the moved-keys list.
- `guide/configuration/host-sources.md`: one line under the composite rule —
  element resources replace with the element.
- `library/reservation-backends.md`: §9 wording.
- `architecture/subsystems/reservations.md`: the level model and the
  admissible-ids reader.

## 13. Decisions

| Question | Decision | Where |
|---|---|---|
| Reservable units | Lab, element, host — all optional, freely combined | §2 |
| Per-slot sub-labs instead? | No — membership is per element (v2 §7 / R14) | §1 |
| How element resources reach runtime | Copied onto each member host as `element_resources`, like `element_metadata` | §3 |
| "In play" | The fleet of interest — `OttoContext.admissible_ids()`, the same set every walk uses — plus any host the run names, less the built-in `local` host | §5 |
| Built-in `local` and the gate | Never in play: otto can always run on the runner, so reaching it never needs a slot. Subtracted by identity, not by id — a lab-declared `local` entry is enforced | §5, §14 |
| Default when no fleet is known | Every host in the lab (never under-reserve) | §4 |
| Verb-level `--hosts` and the gate | Narrows the walk, not the gate; narrow `[project] host_patterns` to narrow the gate | §5 |
| Gate under an empty declared fleet | Zero hosts in play — the lab-level requirement; the refusal stays with the walk | §5 |
| Overlap warning | R18 kept; a shared element protected at element or host level does not warn | §7 |
| Protocol | Unchanged; docstring wording only | §9 |
| `lab_json_v2` | Unchanged: v1 host resources still hoist to the lab | §2 |

## 14. Open follow-ups (not designed here)

A per-invocation override of the fleet of interest at bootstrap (a global
`--hosts` / `OTTO_HOSTS`) would narrow the gate *and* every walk consistently.
Nothing here precludes it — it would feed `admissible_ids()` — and nothing
here needs it: the project's `host_patterns` is the runtime knob today.

~~Whether the built-in `local` host should count as a host in play is open.~~
**Answered: it does not count.** Of the two answers this section framed —
excluding it from the count, or from the set the reservation readers ask for —
the second was chosen, and it subsumes the first. `get_hosts_in_play()`
subtracts the host, so the `N host(s) in play` count drops by one *and* no
resource it might ever carry can reach a requirement.

The question framed this as cosmetic, on the grounds that `local` declares no
resources and so can never change a verdict. That reasoning was the weak part:
it made a user-facing guarantee — that reaching the machine otto is already
running on never needs a slot — rest on a host class happening to leave a
field empty. Under the chosen answer the guarantee is structural, and the test
that pins it hands the built-in host a resource precisely so that a gate which
stopped excluding it would fail rather than pass on the empty default.
