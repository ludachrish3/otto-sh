# Host construction and spec validation — design (spec 3b of the #590 contract-first series)

**Status:** v7, **approved by the owner 2026-10-06**, after the Opus review, five Astra reviews and
Fable's final review, with Astra's cross-review of Fable's proposals. **Date:** 2026-10-06.

**Terms used below.**
- **Spec:** a `HostSpec` subclass instance, the validated boundary model of one lab host entry.
  The built-ins are `UnixHostSpec` and `EmbeddedHostSpec`.
- **Host class:** the runtime `RemoteHost` subclass a spec builds (`UnixHost`, `EmbeddedHost`,
  `ZephyrHost`, or a registered custom class).
- **Entry:** one host table in a lab file, after inventory resolution.
- **Structural validation:** `Spec.model_validate(d)` with no context. It checks shape, types and
  registry names.
- **Contextual validation:** `spec.validate_in(ctx)`. It checks everything that needs the host's
  element, profile, class and effective selections, and it prepares what construction needs.
- **`Prepared`:** spec 3a's frozen result of a configured seam's `prepare` (parsed config,
  normalized form, provenance; R §4.1).
- **Readiness:** the prepared construction record that contextual validation issues and
  construction consumes (§3.4).
- **Field kind:** how a spec field reaches the host constructor (§4).
- **Selection:** the active `term`, `transfer` or `impairer`, chosen from its menu.
- **Paths.** `src/otto/` is left off source citations. All three specs below are in
  `docs/superpowers/specs/`:
  - R is spec 3a, `2026-10-06-registry-catalog-design.md`;
  - "appendix `:N`" is spec 1's appendix, `2026-10-04-public-api-manifest-appendix.md`;
  - P is spec 1, `2026-10-04-public-api-manifest-design.md`;
  - D is the dump spec, `2026-10-05-api-dump-design.md`.

**Where this comes from.**
- Spec 1 §7 lists host construction (`HostSpec`, `to_host`, custom host fields) as a pending seam
  that must be settled before P1.
- The owner split spec 3 into 3a (registries, approved) and 3b (this spec).
- 3a deferred four things here (R §11):
  - spec-time validation, including power's `prepare`;
  - custom host fields;
  - the class/spec field rule;
  - the `otto.models` ↔ `otto.host` coupling.
- Astra (Codex) and Fable consulted independently and cross-reviewed. The owner decided the two
  points they left open (§0). The draft design sections then had a second independent round and a
  cross-review, which settled every remaining point.
- An Opus review of v1 (six majors) is folded into v2. Its rulings are marked **(v2 ruling)**.
- Astra's review of v2 (four majors, two minors) is folded into v3. Its rulings are marked
  **(v3 ruling)**. Astra's review of v3 (one major, two minors, one nit) is folded into v4, marked
  **(v4 ruling)**. Astra's review of v4 (one major) is folded into v5, marked **(v5 ruling)**, and its
  review of v5 (one major) into v6, marked **(v6 ruling)**.
- Fable's final review of v6 (three majors, eight minors, eight nits) and Astra's cross-review of
  its two design proposals are folded into v7, marked **(v7 ruling)**. Both reviewers agreed to
  simplify the readiness record (§3.4), so nothing is left for the owner.

**Evidence.** Read-only surveys and reviews of src at `d0839893`; src is unchanged since.

**Series.** Spec 3b follows S-1, like specs 2, 3a and 4:
- public-path changes land in P1;
- contract changes land after P1 as marked commits, judged by the live API dump.

## 0. Decisions

| # | Decision | By |
|---|---|---|
| H-1 | **The host specs' public home is `otto.host`.** `HostSpec`, `UnixHostSpec` and `EmbeddedHostSpec` move there in P1, and their `otto.models` export retires. | owner, 2026-10-06 |
| H-2 | **Validation is structural first, then contextual.** `Spec.model_validate(d)` without context checks structure only. Contextual validation (element, profile, class, effective selections, power preparation under the real host id) is mandatory before construction. The factory, the doctor and identity all run it. Construction refuses a spec that skipped it. | owner, 2026-10-06 |
| H-3 | **Two declarations, one coverage rule.** A host's fields stay declared on the host class and on its spec. A structural rule at registration proves that every constructor argument will be supplied and every spec field is used. Neither declaration is generated from the other: the 19 conversion targets are substantive (`models/host.py:724-747, 889-898, 966-969`). | reviewers |
| H-4 | **Custom fields are taught through a paired spec subclass.** A plain custom field needs a class field and a spec field, nothing else. `metadata` stays the home for opaque extension data. | reviewers |
| H-5 | **`to_host` stays the public, provisional override hook**, now `to_host(self, *, ready)`. The stock builder makes overriding rare. | reviewers |
| H-6 | **Dependency target: `otto.host` stops importing `otto.models`.** Host construction is implemented in `otto.host`. Shared primitives sit below both packages. `otto.models` may keep importing `otto.host` and keeps the schema and snippet orchestration. A host-free `otto.models` is *not* a goal: it would cost about ten times as much for the same one ratchet edge (§6). Spec 5 does the moves. | reviewers |
| H-7 | **Impairer family checks are contextual.** Structural validation checks impairer membership and pin/menu consistency. Contextual validation resolves and checks the impairer menu members. 3a's impairer record is not amended (R:403). | reviewers |

## 1. What exists today

**Construction runs through `create_host_from_dict`** (`host/factory.py:167-232`):
1. `reject_unresolved_reference`;
2. selector → profile → class and spec class (`:201-205`);
3. with preferences, a second validation pass through the public `host_identity` to get the id for
   matching (`:209-216`);
4. `_merge_host_dict`: profile defaults < host fields < preference option tables, per key
   (`:58-80`);
5. `spec_cls.model_validate(merged)`;
6. `spec.to_host(cls, element=, preferences=)`;
7. provenance stamping;
8. `apply_providers`.

**The loader validates first and then calls the factory**, so each host is validated two or three
times (`labs/json_repository.py:702-711`).

**The spec checks some context-dependent things too early, and others too late:**
- The console rule and the cred term-candidate check use the pin or `menu[0]`
  (`models/host.py:588, 655-665, 685-710`). But a product preference beats the pin
  (`host/capability.py:35-50`). So an entry can pass validation and fail at construction
  (`tests/unit/models/test_host_specs.py:1092-1110`).
- `loader` has no check until `to_host` (`models/host.py:917, 936-969`).
- `power_control` is a raw `dict | str | None` until the host's `__post_init__`
  (`:429`; `host/power.py:157-171`).
- A pin outside its menu survives model validation (`test_host_specs.py:723-730`).
- **`os_type`** is checked by:
  - `create_host_from_dict`, via `build_os_profile` (`factory.py:202-203`; `os_profile.py:449-461`);
  - `host_identity` (`factory.py:150-156`);
  - `validate_host_dict`, with its own text (`:322-334`).

  A direct `model_validate` never checks it (`models/host.py:348`).

**Custom fields:**
- `register_host_class(name, cls, spec=MySpec)` selects a custom validator. But the builders
  forward fixed field lists, so a custom spec field validates and then never reaches the
  constructor (`models/host.py:713-748, 886-899, 966-970`). No test covers delivery
  (`tests/unit/host/test_os_profile.py:220-240`).
- The docs never show `spec=` (`docs/cookbook/extending/custom-host-classes.md`).

**Drift between class and spec is guarded only by tests.** Two tests compare field names after
exclusions:
- `test_host_spec_fields_match_runtime_init`, over the data list `HOST_SPEC_RUNTIME_PAIRS`
  (`models/host.py:973`; `test_host_specs.py:360-375`);
- `test_registered_pairs_drift_guard` (`:520-537`).

Nothing checks a third party's pair at registration.

**Coupling:**
- `models/host.py:18-44` imports host modules and registries at module scope.
- The host side reaches models at:
  - `host/app_shell.py:37` (`OttoModel`, module scope);
  - `host/element.py:79` (`resources_nonempty`);
  - `host/os_profile.py:240` (`HostSpec`), both function-local;
  - plus `TYPE_CHECKING`-only imports.
- The ratchet's `BASELINE` holds both directions (`tests/unit/test_import_cycle_ratchet.py:83, 90`).
- Tach counts function-local imports (`tach.toml:26-28`).

## 2. Public surface

### 2.1 P1 delta (path changes)

**`otto.host.__all__` gains** `HostSpec`, `UnixHostSpec`, `EmbeddedHostSpec` and `host_identity`.
- `host_identity` is already declared at `otto.host` (appendix `:111`), but its facade binding is
  missing. A custom lab backend must call it (`examples/lab_repository.py:43, 222`).

**`otto.models` drops the three specs**, from:
- its typing block (`models/__init__.py:26-27, 29`);
- its lazy bindings (`:69-70, 72`);
- its `__all__` (`:135, 139, 168`).

They are untaught exports (appendix C, row `otto.models`). An appendix G addendum records the move.

**Until spec 5 moves the implementation,** `otto.host`'s binding resolves into `otto.models.host`.
- The facade's `TYPE_CHECKING` import follows the `host/__init__.py:46` pattern.
- No new tach edge appears, because `otto.host → otto.models` is already in `BASELINE`, and spec 5
  removes it.
- Internal code imports the defining module (P:496-500).

**Unchanged:**
- `ToolchainSpec` and the eight exported `*OptionsSpec` classes stay public at `otto.models`.
- `CredSpec` is declared at `otto.models` by appendix B (`:141`) and taught there
  (`creds-backends.md:44`). It is not bound in the facade today, so the general P1 binding work
  adds it.
- `InterfaceSpec`, `ConsoleOptionsSpec` and `UserlandOptionsSpec` are internal today and stay
  internal.

**`to_host` is a member,** so its path is its class.

### 2.2 Additions after P1 (free)

**New names at `otto.host`**, each introduced by the commit named in §7:
- `HostContext`, `HostReadiness` and `FlatPreferences` (3b-3);
- `PowerControlSpec` (3b-3);
- `HostValidationError` (3b-3);
- the field-kind markers `Pass`, `Convert`, `Consume`, `Resolve`, `RuntimeOnly` and the `Omit`
  result (3b-2). See §4.1 for how their callables stay live before 3b-4's walker;
- `host_context(...)`, the supported route from an entry to a context (§3.6) (3b-3);
- `HostBuild`, the view that `Convert` and `Resolve` callbacks receive (§3.4) (3b-3).

**New members of the public `HostSpec`:**
- `validate_in` and `check_in` (3b-3);
- `build_kwargs` (3b-4).

### 2.3 Contract changes after P1 (marked)

**Signatures:**
- `to_host(cls, *, element, preferences)` becomes `to_host(self, *, ready)` (3b-3).
- `validate_host_dict(d)` becomes `validate_host_dict(d, *, element, profiles=)`. The doctor already
  has the element (`init/doctor.py:397-404`) (3b-3).
- `create_host_from_dict` keeps its signature, including the positional `preferences`
  (`host/factory.py:167-172`). Only its validation changes (3b-3). The `profiles=` keyword that
  §3.6 shows on all three callers is 3a 5.9's (R:590-592, R:1132), not a 3b change.

**Validation behaviour:**
- `host_identity` runs contextual validation in no-preference mode (§3.3), so an entry that cannot
  be built under any selection no longer yields an identity (3b-3).
- `power_control` accepts `PowerControlSpec | str | None` and is serialized as written (§5) (3b-3).
- Structural validation newly refuses:
  - an unknown `loader`;
  - an unknown power `type`;
  - a pin outside its menu (3b-1).
- Some checks move from structural to contextual validation (§3.3). So `Spec.model_validate`
  alone accepts entries it used to refuse; the factory and the doctor still refuse them (3b-3).

**Profiles and registration:**
- The profile-defaults vocabulary, as `register_os_profile`, `check_os_profile` and 3a's
  `check_data_profiles()` apply it, narrows from class slots to profile-eligible spec inputs
  (§4.5) (3b-2).
- `register_host_class` refuses a class/spec pair that fails the coverage rule (3b-2). It refuses
  at registration when both are real classes. With `spec=None` the nearest registered spec is
  usually a built-in `Ref` (`host/os_profile.py:505-511`), so the refusal comes at the first
  `get`, during lab load. The docs say so.

## 3. Validation: one pipeline, two phases (H-2)

### 3.1 The pipeline

The factory, the doctor and identity all run steps 1–6. Only construction runs step 7.

1. **Resolve** the selector, then the profile, then the registered `HostClassEntry`, through 3a's
   resolver with an explicit `ProfileContext` (R §3.4).
   - An unknown `os_type` raises one error text.
   - Resolution runs once, and nothing downstream re-resolves.
2. **Merge** the profile defaults beneath the entry's own fields (`_merge_host_dict`'s first
   layer).
3. **Identify (v2 ruling).** Validate the step-2 input structurally with the entry's spec class,
   then compose the canonical id from the *validated* `board` and `slot` with `make_host_id`.
   - This is exactly how `host_identity` derives it today (`host/factory.py:156-158`), so the id is
     byte-identical to the built host's.
   - A malformed `board` raises a `ValidationError`, never a raw `AttributeError` from
     `slug()` (`host/remote_host.py:85, 101-105`).
   - A lax-coerced `slot` (`3.0`, `"03"`) is normalised before it reaches the id.
   - This private helper is not the public `host_identity`, which would recurse into validation.
   - **Invariant (v7 ruling).** No structural check reads an option table. Option-dependent
     invariants are contextual by the §3.3 rule. So step 3 cannot refuse an entry that relies on
     a preference's option table, which only step 4 merges. A §8 test pins this over the built-in
     validators.
4. **Select preferences** by that id (`select_preferences`, `host/capability.py:88-103`), and merge
   their option tables per key over the entry's. Precedence is profile < host < preference
   (`host/factory.py:58-80`).
5. **Structural validation** of the effective input as a whole spec. Model-level invariants that
   relate an option to another host field still see the effective option tables
   (`models/host.py:685-710`).
   - Step 3's pass is reused when step 4 changed nothing.
   - Steps 3 and 5 cost CPU, not file operations.
6. **Contextual validation and preparation:** `spec.validate_in(ctx)` (§3.3), which issues the
   readiness record.
7. **Construction:** `spec.to_host(ready=...)`. Provenance stamping and `apply_providers` follow,
   as today.

### 3.2 `HostContext`

```python
FlatPreferences = dict[str, list[str]]   # step 4's selections, e.g. {"term": ["ssh"]};
                                         # option tables are already merged into the input

@dataclass(frozen=True)
class HostContext:
    element: "Element"                   # the exact Element; its identity is preserved
    entry: "HostClassEntry"              # step 1's RESOLVED entry (from 3a's get): cls and spec
                                         # are classes, never Refs; plus the class's ProfileFields
    profile: "OsProfile"                 # step 1's resolved profile (whole-profile first hit)
    host_id: str                         # step 3
    preferences: FlatPreferences | None  # None = absent (no-preference mode, §3.3)
```

- Contextual checks read the resolved profile from `ctx.profile`, never `ctx.entry.profile`. The
  latter is only the class layer (R:514-518).
- The context binds the *registration*, not just the spec class. `embedded` and `zephyr` share
  `EmbeddedHostSpec` but select different runtime classes (`host/os_profile.py:301-305, 505-508`).
- **`validate_in` refuses a mismatched context (v2 ruling)** with a `HostValidationError` when:
  - `ctx.entry.spec is not type(self)`. A spec subclass whose own fields the coverage rule never
    checked against `ctx.entry.cls` can therefore never reach that class's constructor;
  - `ctx.host_id` differs from `make_host_id(ctx.element.name, self.board, self.slot)`. This closes
    the hole for hand-built contexts.

  It never re-resolves the selector.

### 3.3 Which checks run where

| Check | Phase | Source today |
|---|---|---|
| types, `extra="forbid"`, comment-key stripping | structural | `models/base.py:36`; `models/host.py:431-441` |
| frame / landing-frame / session-setup / filesystem / login-proxy names | structural | `models/host.py:520-560, 583, 936` |
| landing-frame ancestry | structural; the one structural check that resolves a registry entry (the frame registry, see the rule below) | `:676-681` |
| `valid_*` menu members are registered; a pin is in its menu (**new**) | structural | `:232-288`; gap at `test_host_specs.py:723-730` |
| term/transfer menu families | structural, through 3a's static metadata via `peek` | `:232-250`; R:694-706 |
| auth vocabulary for cred `protocols` | structural, via `peek` of term/transfer `authenticates` | `host/capability.py:124-137` |
| `loader` name (**new**) | structural, against `LOADER_CLASSES` names | `models/host.py:917`; `host/binary_loader.py:157` |
| power `type` name (**new**) | structural, against `POWER_CONTROLLERS` names | `host/power.py:133-138` |
| impairer families (H-7) | contextual; resolves the impairer classes, without instances | `models/host.py:273-288` |
| effective term, the console rule, the cred term-candidate check, the effective half of protocol picking | contextual, selection-dependent | `:588, 605-710`; `host/capability.py:35-50` |
| docker priority against the effective `docker_capable`: the spec value if set, else `ctx.entry.cls`'s declared default (today's check reads only the spec field, default `False`) | contextual | `models/host.py:787, 819-827` |
| the embedded frame requirement | contextual, reading the selected class's declared default without calling it: a `default` other than `None`, or any `default_factory`, supplies a frame (v3 ruling). `ZephyrHost` declares `default_factory=ZephyrFrame`, so an omitted frame passes for `zephyr` and fails for `embedded`. A factory that returns the wrong thing fails at construction. | `host/embedded_host.py:214-222, 736` |
| power config parse | contextual: `POWER_CONTROLLERS.prepare(type, raw, PowerEnv(host_id))` (§5) | R:711-728, 778-788 |
| extension checks | contextual: `check_in` (§3.5) | new |

**The rule:**
- a check that needs only the entry and registry names is structural;
- a check that needs the selected class, the effective selections, or the host id is contextual.

Structural checks read only static facts: registry names, and 3a's static metadata through `peek`.
They never resolve a term, transfer, impairer, power or loader backend. **(v7 ruling)** The frame
registry is the one exception: landing-frame ancestry resolves the two named frame classes.
- The built-ins are already imported by the spec module.
- A custom frame is a leaf class whose import the user asked for by naming it.
- Making the check contextual would move a pure-shape error away from the field that caused it.

**Selection-dependent checks have two modes (v2 ruling):**
- **With preferences** (`ctx.preferences` is a dict, possibly empty), as in construction: the
  effective selection is the preference, else the pin, else `menu[0]`
  (`host/capability.py:35-50`). Each selection-dependent check runs once, against that selection.
- **No-preference mode** (`ctx.preferences is None`), as in `validate_host_dict` and
  `host_identity`: preferences are unknown, and a preference may choose any member of each menu,
  pin included. **(v3 ruling)** The entry passes only if at least one **combination** of
  selections, one member from each of the `term` and `transfer` menus, passes *every*
  selection-dependent check together. Each check passing for some member separately is not
  enough. The candidate set is the product of the two menus, a handful of pairs for real entries,
  and each candidate is checked against static facts only.
  - The first passing combination in menu order is the **witness**. It is recorded for error
    messages and is never used to build.
  - When no combination passes, the error reports the failures of the selections that the
    concrete resolver picks with empty preferences (**v4 ruling**). That is the same resolver
    construction uses, including its menu filtering: under `console`, for example, `nc` is dropped
    before the transfer is chosen (`models/host.py:875`). So its text matches what a load without
    preferences would say. A bare "pin or `menu[0]`" would not.
  - The impairer family check is not selection-dependent: it checks every menu member in both
    modes.

**So:**
- the doctor and identity refuse exactly the entries that cannot load under any preferences;
- a host that loads under its real preferences never fails the doctor, and never drops out of
  host summaries.

For example, `valid_terms=["console","ssh"]` with no console server and credentials usable over
`ssh` passes in no-preference mode, through the witness `ssh`. At load, a preference or pin for
`console` fails it. The same menu with console-only credentials fails in both modes, because no
term satisfies the console rule and the cred term-candidate check together.

**Probe records cannot build (v3 ruling).** In no-preference mode `validate_in` still runs
preparation, so the doctor and identity report power-config errors. But the record it issues is a
*probe* record: it validated no particular effective selection, so the stock builder refuses it
with a `HostValidationError`. Only a context with concrete preferences (a dict, possibly empty)
yields a buildable record.

### 3.4 `HostReadiness`: the prepared construction record

**What the record guards against (v7 ruling).** The record is an *accident guard*, not a security
boundary. It makes the stock path refuse a spec that skipped contextual validation, and it keeps
validated inputs isolated from later mutation. It does not try to resist deliberate forgery:
- direct construction (`cls(**kwargs)` with `power_control_from_spec`, §5) is a supported bypass;
- so is an overriding `to_host` (scope, below).

v3 to v6 hardened the record against forgery: an opaque handle, an issuer-held weak map, refusal
of copying and pickling, and a split owner. Fable and Astra agreed in cross-review that this bought
nothing H-2 needs, and v7 removes it.

`validate_in` is final. It runs the §3.2 context checks, the stock contextual checks, `check_in`
and preparation, then issues one `HostReadiness`.

**Where the types live (v2 ruling).**
- `otto.host.construction` holds:
  - the field-kind markers (§4.1);
  - `HostContext`, `HostReadiness`, `HostBuild` and `FlatPreferences`;
  - `HostValidationError`;
  - the private `_issue_readiness(...)`.
- `HostContext`'s field types (`Element`, `HostClassEntry`, `OsProfile`) are all `otto.host` types,
  so the module adds no cross-package edge.
- It imports them only under `TYPE_CHECKING`, with string annotations. So importing it loads no
  otto module beyond `otto.errors`, a zero-import leaf (`tach.toml:26-28`). 3b-2's measurement
  (§8) confirms this.
- `validate_in` and `build_kwargs` stay in `otto.models.host` until spec 5. They call
  `_issue_readiness` and read the record's private fields, in the allowed `models → host`
  direction. This is the one cross-package use of private names, and it disappears when spec 5
  moves `models/host.py` into `otto.host`.

**What the record holds.** `HostReadiness` is a frozen dataclass. Its public, read-only fields:
- `host_id`;
- `cls`, the runtime class: `ctx.entry.cls`, fixed at issue;
- `selections`, a read-only mapping of the effective selections, or `None` for a probe record.

Its private fields, which are not part of the contract:
- `_owner`: the originating spec instance, by reference and never copied;
- `_probe`: whether it was issued in no-preference mode (§3.3), with the no-preference witness for
  messages;
- `_payload`, the **construction payload**. It holds:
  - the `HostContext`, carrying the entry, the exact `Element`, the resolved profile and the host
    id;
  - a **snapshot** of the validated effective input: its values, which fields were set
    (`model_fields_set`), and the merged option tables;
  - the effective selections;
  - the power `Prepared` from 3a (`prepare`'s frozen result, R §4.1), or `None`;
- a private consumption flag, which only the builder sets.

**Implementation notes.**
- The dataclass is frozen, so the builder sets the flag with `object.__setattr__`.
- `copy.deepcopy` of a `types.MappingProxyType` raises `TypeError`. So the read-only views
  (`selections`, and `HostBuild.inputs`) are built *over* the issue-time copy, never copied
  themselves.

**Supported use:**
- A record is issued only by `validate_in`.
- It is consumed by exactly one build, by its owner.
- Constructing a record any other way, mutating its fields, or cloning it (`copy`,
  `dataclasses.replace`, pickling) is **unsupported**. The docs say so, and nothing promises to
  detect it.

**Issuing.**
- `_issue_readiness` deep-copies the construction payload once, at issue. Its memo maps exactly two
  objects to themselves:
  - the `Element`, which is the lab's object and is passed by identity by design;
  - the registry that the `Prepared` names. 3a's provenance check compares registry identity and
    generation (R:720, 728). A pinned registry and a copied integer generation keep both.
- Everything else is the record's own copy: preferences, option tables, and the `Prepared`'s
  config, normalized form, prepared facts and `Env`. A caller that later mutates its preferences
  dict, the `HostContext` it passed in, or the spec cannot change what is built. Classes copy to
  themselves under `deepcopy`, so `cls` is the registered class.
- **A copy failure is a validation failure (v7 ruling).** If the payload cannot be deep-copied,
  `validate_in` raises a `HostValidationError` naming the host id, with the cause chained. In
  practice that means a non-copyable power config. So the doctor, identity and the loader all
  report it, before any record exists. 3a's `build` makes its own per-call copy of the config for
  the factory and keeps its own construction error for a failure there (§10). This spec does not
  claim that copy can never fail.

**Registrations that share a spec.** `embedded` and `zephyr` share `EmbeddedHostSpec`
(`host/os_profile.py:505-508`). The record's `cls` is fixed at issue from the resolved entry. The
spec-type check (§3.2) does not choose the class.

**Construction.**
- **`to_host(self, *, ready)`.** There is no separate `cls`: the class is `ready.cls`.
- **Refusals, then consumption (v7 ruling).** `build_kwargs(ready)` (§4.4) first refuses, with a
  `HostValidationError`:
  - a record whose `_owner is not self`. This covers a genuine record from another spec or class:
    the entry match is already guaranteed by §3.2's `entry.spec is type(self)`;
  - a probe record (§3.3);
  - a record already consumed ("record already consumed; validate again to build again").

  It then marks the record consumed, *before* any callback runs. The record stays consumed even if
  the build later fails.
  - Consumption lives in the public `build_kwargs`, so an override that builds through it cannot
    reuse a payload by accident.
  - The stock `to_host` is `ready.cls(**self.build_kwargs(ready))` and does not check or consume a
    second time.
- **`HostBuild`: what callbacks see.** After consumption, the builder makes one frozen `HostBuild`
  (public, 3b-3) over the payload and hands it to every `Convert` and `Resolve` callback. It holds:
  - `inputs`: a read-only mapping over the snapshot's values;
  - `context`: the payload's `HostContext`;
  - `selections`: the effective selections;
  - `power`: the payload's `Prepared`.

  The issue-time copy *is* the build's private copy. Because the record is single-use, no second
  build can observe what a callback or the constructor did to it, so no per-build copy is needed.
- **Snapshot semantics.** The builder constructs **only from the payload**. Mutating the spec after
  validation cannot change what is built.
- **Power keeps 3a's own provenance checks:** a foreign or stale `Prepared` is refused (R:717-729).
- **In 3b-3**, before the generic builder exists (3b-4), the per-family `to_host` methods do the
  same thing:
  - perform the refusals and the consumption;
  - make the `HostBuild`;
  - read only it, never `self.*`.

  So the guarantee holds from 3b-3 on (§7).
- **Scope of the guarantee.** It covers the stock factory and the stock builder. An overriding
  `to_host` can ignore any convention, so `custom-host-classes.md` says that an override must
  build through `build_kwargs` (§4.4) or honour the record itself.
- **Building twice.** To build two hosts from one entry, validate twice. That costs CPU, plus one
  config parse per `prepare` (R:711-714), not file operations.

### 3.5 The extension hook and the error type

**`HostSpec.check_in(self, ctx: HostContext) -> None`** is public and overridable, and does nothing
by default. `validate_in` calls it once:
- after the stock checks, so it cannot skip them;
- before preparation.

A custom spec puts its contextual checks there. A conversion callback (§4) must not be the only
place where extension configuration is checked.

**`HostValidationError(OttoError, ValueError)` (v2 ruling)** is what `validate_in` and the stock
builder raise for a contextual failure.
- The message names the host id.
- A `check_in` failure of any `Exception` type is wrapped in it, with the cause chained.
  Process-control exceptions pass through (`errors.py:114-133`).
- Power preparation raises 3a's `PowerConstructionError(PowerControlError, ValueError)` (R:778),
  which `validate_in` also wraps, prefixing the host id. A parse error is rendered through 3a's
  `describe_parse_error`, so no secret appears in the message.
- The doctor's catch set (`init/doctor.py:340-351`) and the summary drop set
  (`labs/json_repository.py:526`: `ValueError`, `TypeError`, `InventoryError`) need no widening.
  The loader already wraps every exception with file, element and index (`:712-734`).

### 3.6 Callers and what each promises

**The callers:**
- **`create_host_from_dict(d, preferences=None, ..., *, element, profiles=, ...)`** runs steps 1–7,
  with preferences (an empty dict when none are given).
- **`validate_host_dict(d, *, element, profiles=)`** runs steps 1–6 in no-preference mode. It is
  the doctor's probe.
  - Until 3b-5, the loader also calls it (`labs/json_repository.py:704`), and 3b-3 updates that
    call.
- **`host_identity(d, element, *, profiles=)`** runs steps 1–6 in no-preference mode, and returns
  the identity (H-2). Its readers:
  - the completion cache writer and host summaries (`labs/json_repository.py:524-530`);
  - cross-lab link addressing, `addressing_from_dict`, called per record of every lab file with
    skip-on-error (`link/derive.py:55`; `labs/json_repository.py:395`);
  - the example backend (`examples/lab_repository.py:222`).

  Preparation resolves each power config model once per process. From 3b-5 it parses once per
  entry per route. Between 3b-3 and 3b-5 the loader's interim `validate_host_dict` call and the
  factory each prepare, so power is parsed twice per loaded host. These paths are measured (§8). Warm completion reads never prepare (R §4.3).
- **`host_context(d, *, element, profiles=, preferences=None) -> tuple[HostSpec, HostContext]`**
  (public, 3b-3) runs steps 1–5 and returns the validated spec and its context. It is the
  supported route for library code and tests that want a host without the factory:
  `spec.to_host(ready=spec.validate_in(ctx))`. The migrated tests use it, or
  `create_host_from_dict`, rather than building contexts by hand.
  - Its `preferences` is the same unified preferences table that `create_host_from_dict` takes,
    keyed by selector. `host_context` runs step 4's `select_preferences` on it, and the context
    carries the resulting `FlatPreferences` (v7 ruling).
  - **(v3 ruling)** Its `preferences=None` means *no preferences*: the context carries `{}`, and
    its record is buildable, exactly as `create_host_from_dict`'s `None` does today. Probe mode is
    reached only through `validate_host_dict` and `host_identity`. `host_context` never returns a
    probe context.

**What the doctor promises:**
- For identical resolved inputs, the doctor and the loader run identical configuration checks. The
  doctor runs them in no-preference mode, so it refuses only what no preference can rescue.
- It does **not** promise that the repo loads. Constructors, overrides and providers can still
  fail after validation (`host/factory.py:221-231`; `labs/json_repository.py:725-734`).

**Without an element** (bare library use), only structural validation is available.

## 4. Fields: two declarations, one coverage rule (H-3, H-4)

### 4.1 Field kinds

Every spec field has exactly one kind, declared beside it as `Annotated` metadata. `Pass` is the
default and needs no marker. The markers live in `otto.host.construction` (§3.4), so the host
classes' own fields can carry `RuntimeOnly` without importing `otto.models`.

**Pydantic and docs:**
- Pydantic keeps arbitrary `Annotated` metadata in `FieldInfo.metadata` and out of the JSON schema
  (`.venv/…/pydantic/fields.py:144-148`).
- Autodoc expands `Annotated` (`models/host.py:76-86`), and metadata becomes dotted `py:class`
  targets that fail nitpicky `-W` (`models/options.py:285-288`). So each marker's `repr` names
  only the marker (`Convert`, never its callable).

| Kind | Semantics | Today's examples |
|---|---|---|
| `Pass` (default) | Forwarded under the same name **if set** (in the snapshot's `model_fields_set`), so a class default survives an omitted field. | `hw_version`, a custom plain field |
| `Convert(fn)` | `fn(value, build) -> object \| Omit` runs if the field is set. `Omit` leaves the argument out. Presence-sensitive: `snmp=None` forwards `None`, while `command_frame=None` returns `Omit`. | `creds`, `default_dest_dir`, `metadata`, `resources`, `debug_log_globs`, `interfaces`, `telnet_options`, `console_options`, `snmp`, `toolchain`, `command_frame`, the six Unix `*_options`, `filesystem`, `loader` (19 targets); `power_control` from 3b-3 (§5) |
| `Consume(reason)` | Used before construction; never forwarded. | `inventory` |
| `Resolve(fn)` | `fn(build) -> object` always runs, whether or not the field was set. `build.inputs` is a read-only mapping of the record's snapshot values. It must not depend on field order. | the `valid_*` menus and the `term`/`transfer`/`impairer` selections (`models/host.py:862-885, 956-964`) |

**The built-in callables are live from 3b-2 (v7 ruling).**
- 3b-2 lifts each of the 19 built-in value conversions out of `_common_host_kwargs` and the
  per-family loops into a module-level helper of the value alone, such as `_to_creds(value)`.
  Each reads only the value (`models/host.py:713-748, 886-898, 966-969`).
- The existing builders call those helpers from 3b-2 on, and each marker wraps the same helper:
  `Convert(_value_only(_to_creds))` adapts it to the `fn(value, build)` signature.
- So no callable sits unused, and 3b-4 changes only who walks the fields.
- `power_control` is the one conversion that reads `build` (§5). It stays `Pass` until 3b-3
  introduces both `HostBuild` and its `Convert`.

**Marker rules:**
- A subclass that overrides a field without declaring a marker **inherits** its parent's marker.
  Pinned by an inheritance test, not a refusal.
- Two markers on one field are refused.
- A class-side `RuntimeOnly` on a name the spec also declares is refused as contradictory.

### 4.2 The class side

A host-class init field with no spec counterpart carries
`field(metadata={"otto": RuntimeOnly(reason=..., supplied_by=None | "element")})`.
- `supplied_by="element"` means the stock builder passes `build.context.element`. It is the only
  supplier the builder implements; any other value is refused.
- `supplied_by=None` means the field keeps its runtime default and is never passed.
- Today's runtime-only fields:
  - `element` (supplied);
  - `inventory_ref`, `lab_info`, `products`, `dev_tools`, `shadowed_products` and
    `shadowed_dev_tools` (defaulted).

  Products and provenance are assigned *after* construction (`host/factory.py:221-231`).
- Private constructor fields (`_lab`, `_connection_factory`; `host/remote_host.py:321-325`) are
  outside the rule, as they are in today's guard (`test_host_specs.py:368-370`). Each must still
  have a default, so that no required argument goes unsatisfied. 3a's #601 commit deletes
  `_connection_factory`.

### 4.3 The coverage rule: argument availability

**Where it runs.** 3a's `validate` on `HostClassEntry` checks it when both members are real
classes. Otherwise `check_resolved` checks it on the whole record, at the first `get` (R:304-317;
§2.3).

**What it requires:**
- every `Pass`/`Convert`/`Resolve` spec field has a same-named class init field;
- every public class init field is a spec field, or is marked `RuntimeOnly`;
- **every required init parameter (no default) is available.** That means it is one of:
  - a *required* spec field of kind `Pass` or `Convert`;
  - a `Resolve` field;
  - a `RuntimeOnly(supplied_by="element")`.

  An optional `Pass` field does **not** satisfy a required parameter, even when the names match.
  For example, a class requiring `serial` paired with `serial: str = "default"` would be refused.
- every marker is well formed.

**What it checks.** The rule checks declared facts and never calls a default factory. An
implementation that violates its own declaration fails at construction, with a construction error.

**`Omit` on a required parameter (v3 ruling).** A `Convert` may return `Omit`, so a required
`Convert` field is statically *available* but not guaranteed *supplied*. The static rule is about
availability. The runtime half belongs to the stock builder: when a `Convert` for a **required**
constructor parameter returns `Omit`, it raises a `HostValidationError` naming the field and the
host id, before calling the constructor. So the constructor never sees a missing required
argument from the stock path.

**From 3b-2 to 3b-4 (v2 ruling).** In 3b-2 the rule checks name coverage and marker validity only.
The argument-availability clause, which only the generic builder can honour, turns on in 3b-4.
Until then the per-family builders forward fixed lists, so availability cannot be promised.

**Tests.** Today's two drift tests become checks that the built-ins' declarations pass the rule.
The behavioural conversion tests stay, because coverage cannot prove that a callback returns the
right value (`test_host_specs.py:56-62`).

### 4.4 One stock builder

**`HostSpec.build_kwargs(ready) -> dict`** (public, final) walks the spec's fields by kind over the
record's `HostBuild`, and adds the `RuntimeOnly` suppliers. The stock `to_host` is
`ready.cls(**self.build_kwargs(ready))`. `build_kwargs` performs the §3.4 refusals and the
consumption.

It replaces three things:
- `_common_host_kwargs`;
- `_COMMON_PLAIN_FIELDS`;
- the per-family name loops (`models/host.py:208-229, 713-748, 886-898, 966-970`).

As a result:
- a custom plain field needs a class field and a spec field, nothing else;
- a converted custom field adds `Convert(fn)`;
- a custom contextual check overrides `check_in`;
- overriding `to_host` is needed only for construction that is not keyword arguments.

The dump spec forbids teaching overrides of private members (D:530-541). `build_kwargs` is public
for exactly that reason.

### 4.5 Profile-eligible inputs

**A profile default may set** any spec field of kind `Pass`, `Convert` or `Resolve`, except
`os_type` (the selector overwrites it, `host/factory.py:218-220`). `Consume` fields such as
`inventory` are excluded, because inventory is resolved before the profile merge
(`labs/json_repository.py:702-705`).

**Where it applies.** 3a's `check_data_profiles()`, `check_os_profile` and `register_os_profile`
validate defaults keys against this set instead of class slots. Class slots wrongly admit
`products`, `element`, `lab_info` and `_connections` (`host/os_profile.py:167-178, 352-357`).

**Cost (v7 ruling).** Reading the vocabulary needs the spec class's `model_fields` and markers,
so `otto.models.host` must be importable where 3a's `check_data_profiles()` runs (R:554-556). That
is acceptable only because the check runs where a host class is already being resolved:
- the bootstrap's post-loop step, for repos that declare `[os_profiles.*]` tables;
- the resolver's first selection.

- **Expected, not yet shown.** Neither site is expected on `help_repo_warm`.
  `completion_repo_handover` is a real-entry completion that reaches the fleet and lab config
  (`scripts/import_budget.py:455-470`), so the bootstrap's post-loop check may run there for a repo
  with `[os_profiles.*]` tables.
- **Measured.** 3b-2 measures both surfaces with a fixture repo that declares a data profile.
- **Fallback.** If the handover grows, 3b-2 runs the data-profile check only at the resolver's
  first selection, which no completion reaches. The bootstrap's post-loop step then validates
  names alone and defers the vocabulary check. Otherwise the commit must name the growth and its
  reason (P:482-488).

**Amendment to 3a.** This amends R §3.4 ("is every defaults key a field of that class"). It is a
marked commit after 3a 5.9 (§7).

## 5. Power control

**The field (v2 ruling).** `power_control: PowerControlSpec | str | None`, serialized as written.
- A string stays a string, and `None` stays `None`. A JSON dump of a spec therefore writes back
  what the user wrote.
- `PowerControlSpec(type: str, extra="allow")` is the table form.
- Structural validation checks only the `type` name, which a string carries itself, against
  `POWER_CONTROLLERS`.
- The JSON schema shows all three forms.

**Contextual:**
- `validate_in` normalises the field into `(type, raw)`: a string means no config. It then calls
  `POWER_CONTROLLERS.prepare(type, raw, PowerEnv(host_id=ctx.host_id))`.
- The `Prepared` goes into the readiness record.
- No controller is built, and no peer host is looked up. Peer existence stays a runtime lookup
  (`host/power.py:81-97`).

**The field's kind:**
- `Pass` in 3b-2, today's plain forward (`models/host.py:226`).
- From 3b-3, `Convert(_build_power)`, where `_build_power(value, build)` returns
  `POWER_CONTROLLERS.build(build.power)`. `build` makes no parse (R §4.1).
- Presence rule: an unset field keeps the class default (`None`), and an explicit `None` forwards
  `None`.

**Direct construction** (a host built without a spec) keeps 3a's
`power_control_from_spec(value, *, host_id)` (R:786-787).

Readiness and markers never appear in emitted JSON or schemas.

## 6. The models ↔ host coupling (H-6; spec 5 executes)

The ratchet's `BASELINE` holds both `otto.models → otto.host` and `otto.host → otto.models`. 3b
sets the target, and spec 5 moves the modules and measures. **Target: remove `otto.host →
otto.models`.**

**Already in `otto.host` from 3b on** (§3.4, §4.1): `otto.host.construction`, with:
- the field-kind markers;
- `HostContext`, `HostReadiness` and `HostBuild`;
- `FlatPreferences` and `HostValidationError`;
- `_issue_readiness`.

**Moves into `otto.host` in spec 5:**
- the rest of `models/host.py`: `HostSpec`, `UnixHostSpec`, `EmbeddedHostSpec`, `PowerControlSpec`,
  `validate_in`, `build_kwargs`;
- the helper specs that only host construction uses, with their conversion methods:
  - `ToolchainSpec` and `ToolchainToolSpec` (`models/host.py:93-150`);
  - `InterfaceSpec` (`:153-202`);
  - the `*OptionsSpec` classes (`models/options.py`, ten `to_runtime` methods at
    `:49, 91, 123, 151, 192, 248, 291, 319, 342, 381`).

  No public member is removed. The `otto.models` exports that exist today (`ToolchainSpec`, the
  eight `*OptionsSpec`) stay as facade bindings, because `models → host` is allowed.

**Below both packages, in a leaf both may import:**
- `OttoModel` and `compact_validation_error` (for `host/app_shell.py:37` and 3a's
  `describe_parse_error` callers);
- `resources_nonempty` (for `host/element.py:79`);
- **`CredSpec` and what it needs.** `CredSpec` is shared with `otto.creds` and `otto.inventory`.
  Placing it in `otto.host` would add a `creds → host` edge, which the ratchet forbids. Its
  `to_cred` returns a host `Cred`. So spec 5 decides by measurement whether `Cred` and
  `cred_identity` move down with it, or `to_cred` becomes a host-side converter. The latter changes
  a public member, so it is a marked contract change scheduled explicitly under S-1, never hidden
  inside a move.

**Stays in `otto.models`:**
- the schema and snippet orchestration (`models/jsonschema.py:453-476`; `models/snippets.py`),
  which imports `otto.host` legitimately;
- `ElementSpec.to_element` (`models/lab.py:128, 162-178`);
- the settings models.

**Nominal checks stay nominal:** `issubclass(spec, HostSpec)` (`host/os_profile.py:239-244`)
becomes an intra-package check once the spec lives in `otto.host`.

**What this buys.**
- One ratchet edge (`otto.host → otto.models`) goes.
- `otto.models` stays in the cycle through `config` (`models/settings.py:1163`; `tach.toml:85-91`).
  The gain is one edge, not a leaf.
- A host-free `otto.models` (the reverse target) would also need these moves, about ten times this
  work for the same single edge, so it is not planned:
  - every converter;
  - `cred_identity` and `slug`;
  - the option annotations' runtime types (`models/options.py:44-46, 280-282`);
  - the exporter.
- No file-operation saving is claimed. Spec 5 measures each surface before and after, and no
  critical surface may grow (P:478-489).

## 7. Commits (after P1 unless stated)

| # | Commit | Depends on |
|---|---|---|
| P1 | `otto.host.__all__` gains the three specs and `host_identity`; `otto.models` drops the three specs (§2.1); docs re-point | — |
| 3b-1 | `feat(models)!`: structural gaps, which are loader membership, power `type` membership (today's `dict \| str` input kept), pin membership, term/transfer menu families via `peek`, auth vocabulary via `peek`; docs: `lab-config.md` (`loader`) | 3a 5.1, 5.8 |
| 3b-2 | `feat(host)!`: `otto.host.construction` with the field-kind markers and `RuntimeOnly`; the coverage rule on `HostClassEntry` (name coverage and markers); profile-eligible inputs (amends 3a 5.9's data-profile check); docs: `os-profiles.md`, and a `make docs` build proving the markers render | 3a 5.9 |
| 3b-3 | `feat(host)!`: the pipeline, `HostContext`, `HostReadiness`, `HostBuild`, `FlatPreferences`, `HostValidationError`, final `validate_in`, `check_in`, `host_context`, `to_host(self, *, ready)`, the §3.3 migration with both modes, `PowerControlSpec` with `prepare` in `validate_in` and `build` in construction, `validate_host_dict(element=, profiles=)` (and the loader's interim call), `host_identity` on the pipeline, the doctor on it. The per-family builders consume the record and perform the refusals. Docs: `lab-config.md` (`power_control`), `data-boundary.md`, `custom-host-classes.md` (`check_in`, the override rule); a nitpicky `make docs` build proving `HostContext`'s quoted annotations render | 3b-2, 3a 5.12 |
| 3b-4 | `feat(host)!`: the stock `build_kwargs` replaces `_common_host_kwargs`, `_COMMON_PLAIN_FIELDS` and the per-family loops; custom plain fields forward; the coverage rule's argument-availability clause turns on; docs: `custom-host-classes.md` (a paired spec with one plain and one converted field) | 3b-3 |
| 3b-5 | `refactor(labs)`: the loader drops its separate `validate_host_dict` call, once a test shows the factory reports the same errors with the same file/element/index attribution, for entries outside no-preference mode's difference (§3.3) | 3b-3 |

**3b-3 test migration:**
- 52 `to_host` call lines in five test files, plus `host/factory.py:221`;
- the `validate_host_dict` callers: 13 lines in `tests/unit/host/test_factory.py` and one each in
  `test_factory_element_boundary.py`, `test_init_scaffold.py` and `test_resolve.py`, plus the
  loader and the doctor;
- the `host_identity` tests in six files, whose semantics change.

The readiness guarantee is published in 3b-3, which already includes power preparation. No
intermediate commit advertises a guarantee it cannot meet.

## 8. Tests and measurement

Tests that must be able to fail:
- **Custom field delivery.** A custom spec's plain field reaches the constructor (3b-4). This fails
  today.
- **Coverage refusals** at registration (real classes) and at the first `get` (`Ref`):
  - a spec field with no class counterpart;
  - a class field that is neither a spec field nor `RuntimeOnly`;
  - from 3b-4, an optional `Pass` paired with a required parameter;
  - conflicting markers, and a contradictory `RuntimeOnly`.

  Marker inheritance on override is pinned by its own test.
- **Context refusals:**
  - a `HostContext` whose `entry.spec` is a base of `type(self)`;
  - a `HostContext` whose `host_id` disagrees with the spec's `board`/`slot`.
- **Readiness:**
  - a genuine record from another spec instance, or from another class, is refused;
  - a probe record (from `validate_host_dict`'s route) is refused at build;
  - a second `to_host(ready=)` with the same record is refused as consumed. So is a retry after a
    build whose constructor raised: the record was consumed before callbacks ran;
  - the stock `to_host` consumes exactly once: a stock build succeeds, it does not refuse its own
    record;
  - the spec, the caller's preferences dict and the `HostContext` passed in, each mutated after
    `validate_in`, do not change what is built;
  - in a `HostBuild`, `context.element` is the issued `Element` (identity), and `build(build.power)`
    passes 3a's provenance check;
  - `ready.selections` is read-only;
  - a power config model holding a non-copyable private attribute fails in `validate_in` with a
    `HostValidationError` naming the host id, reported by the doctor, `host_identity` and the
    loader.
- **Identity:**
  - `board: 5` raises a `ValidationError`, not an `AttributeError`;
  - `slot: "03"` and `slot: 3.0` give the same id as the built host.
- **Validation modes:**
  - a console host whose *preference* selects console fails contextual validation, not
    construction (`test_host_specs.py:1092-1110`);
  - `valid_terms=["console","ssh"]` with no console server and ssh-usable credentials passes the
    doctor and stays in host summaries, and fails at load only under a console selection;
  - the same menu with console-only credentials and no console server fails the doctor and drops
    out of summaries and link addressing. Each check alone has a passing member, but no single
    term passes both (the combination rule);
  - an omitted `command_frame` passes for `zephyr` (declared `default_factory`) and fails for
    `embedded`, through the same spec class;
  - a `Convert` for a required parameter returning `Omit` raises a `HostValidationError` naming the
    field (3b-4);
  - a pin outside its menu fails structural validation;
  - an impairer menu with a member of the wrong family fails contextual validation in both modes.
- **Errors:**
  - a malformed power table is reported by the doctor, the loader and host summaries;
  - its message names the host id;
  - no secret appears in it;
  - no controller is built during validation;
  - a `check_in` raising `KeyError` surfaces as a `HostValidationError` in all three routes.
- **The doctor and the loader** report identical errors for the same bad entry under the same
  inputs.
- **Step-3 invariant:** no built-in structural validator reads an `*_options` table (v7 ruling).
  - An entry whose `telnet_options` table comes only from a `[host_preferences]` entry passes
    step 3, and the merged table reaches step 5. `telnet_options` is one of the tables preferences
    may set (`models/settings.py:596-604`; `host/factory.py:65-80`).
  - The console rule's contextual placement is pinned separately, by the console-preference test
    under "Validation modes".
- **Profile defaults:** a profile default naming `products` is refused. It is admitted today, so
  this must fail first.
- **Typing:** `to_host` without `ready` is a typing error. 3a's typing-fixture infrastructure
  (R §7) runs a `ty` negative fixture for it.

**Measurement:**
- **Phase tables:** `import_budget.py --report-json` before and after for `import_otto`,
  `bootstrap_repo`, `completion_repo_handover` and `help_repo_warm`.
  - 3b-2 makes code-profile registration read the spec class's markers, which `bootstrap_repo`
    covers.
  - Spec 1 measures phase tables on Python 3.10 (P:482-483).
- **The paths with no surface** (the completion cache writer over host summaries, link addressing
  in `addressing_from_dict`, and the doctor) are measured with `strace -f` file-operation counts on
  a fixture lab, per series practice.
- **The warm completion read** must stay at its ceiling (410 on 3.10; 387 on 3.12), because it never
  validates.

## 9. Out of scope

- **The module moves:** spec 5, against §6's target.
- **#600:** the term-backend ABC.
- **Session-setup config models:** 3a gives session setup none (R:399-405).
- **Promotion of any host-construction name to stable.**
- **Declared products and dev tools (v2 ruling).**
  - Appendix D's entry for `otto.declared` assigned "declared-entry validation" to 3b. That
    assignment was an error in spec 3a's amendments: nothing there is a pending seam.
  - `DeclaredEntry`'s path is already declared at `otto.host.product` (appendix D), `otto.declared`
    stays internal, and 3a owns `KindBuilder`.
  - The doctor does not check that a `[[products]] kind` resolves. That is a doctor gap with no
    public-path consequence, so it does not block P1, and this spec does not own it.
  - §10 corrects appendix D.

## 10. Amendments (made in this spec's commits)

- **Spec 1, design §7 pending seams:** the host-construction line names 3b, with H-1 and the P1
  delta. It is marked **settled**.
- **Spec 1, appendix D** (`otto.declared`, `:307`): "3b for declared-entry validation" is removed.
  The entry reads "spec 3a for `KindBuilder`; `DeclaredEntry` is declared at `otto.host.product`".
- **Spec 1, appendix G:** an addendum moving `HostSpec`, `UnixHostSpec` and `EmbeddedHostSpec` from
  `otto.models` to `otto.host`.
- **The P1 checklist** gains "Spec 3b's P1 work", with the §2.1 lines and the after-P1 commits
  3b-1 to 3b-5. All are marked except 3b-5, which is a refactor.
- **Spec 3a:**
  - §3.4's data-profile validation names the profile-eligible inputs of 3b §4.5 as the vocabulary
    from 3b-2 on;
  - §4.1's `build` hands the factory a deep copy of the parsed config (`copy.deepcopy`, per call),
    so a factory cannot change a later build from the same `Prepared` (v3 ruling). 3b's records
    are single-use, but 3a's reservation gate builds one `Prepared` twice
    (`reservations/factory.py:261-269`). It lands in
    3a 5.12, where power gains `prepare`/`build`. Provenance checks are unchanged;
  - **(v4 ruling)** a configured seam's config model must therefore be deep-copyable (3b's issue
    copy needs it too). This is
    stated beside 3a's config-model protocol and in the extension docs for every configured seam.
    A copy failure raises the seam's construction error with the cause chained, before the
    factory is called;
  - §11 points to this spec.
