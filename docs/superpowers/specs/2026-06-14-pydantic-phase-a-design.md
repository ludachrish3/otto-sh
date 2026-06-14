# Pydantic Phase A — Boundary Models — Design

> Captured 2026-06-14. Workstream **#3** of the fable-review sequencing
> ([docs/superpowers/specs/2026-06-13-fable-review-sequencing-design.md](2026-06-13-fable-review-sequencing-design.md),
> item #3), following WS#1 (context/lifecycle, merged) and WS#2 (snake_case
> sweep, merged as `179ce81`). Pairs with the review findings in
> [todo/fable_review_outcome.md](../../../todo/fable_review_outcome.md)
> (decision #2, Phase A) and absorbs three backlog items:
> [todo/host-default-options.md](../../../todo/host-default-options.md),
> [todo/metric-point-dataclass.md](../../../todo/metric-point-dataclass.md),
> [todo/multi_interface_hosts.md](../../../todo/multi_interface_hosts.md).
>
> Branch off post-WS#2 `main` (`179ce81`). Stage only — Chris commits.

---

## Goal and organizing principle

Introduce **pydantic v2 at otto's data boundaries** — the points where external,
untrusted data (lab JSON, `.otto/settings.toml`, `OTTO_*` env, monitor
import/export) enters the program — to replace the hand-rolled validation and
three-layer merge in `src/otto/storage/factory.py` and the per-table validators
in `src/otto/configmodule/repo.py`.

Phase A locks the **shapes** the contract freeze will fix in place. Per the
freeze filter, a change belongs here only if deferring it would later force a
breaking change to a frozen name or shape. Everything that is purely an internal
implementation swap (e.g. validation *mechanism* with no shape change) is in
scope only because it is cheap to do now alongside the shape work; nothing here
is gated *out* by the freeze.

### What pydantic buys

- `extra='forbid'` — a typo'd config field (`labs`→`lab`, `connect_timeout`→
  `connet_timeout`) becomes an error with a field-name suggestion instead of
  being silently dropped (today's behavior via `_all_slots` filtering and the
  silently-ignored `*_options` keys).
- Typed coercion with real error locations.
- **JSON Schema export** for `hosts.json` / `settings.toml` editor
  autocomplete + generated schema docs.
- `pydantic-settings` for the `OTTO_*` environment surface.

---

## The central architecture: a leaf "spec" layer feeding unchanged runtime objects

Pydantic sits at the **parse/validate seam only**. The behavior-bearing runtime
objects — `UnixHost`, `EmbeddedHost`, `Lab`, `MetricCollector`, the `*Options`
classes that carry library adapters — keep their current types. A new leaf
package of pydantic **spec models** validates external data, then a thin builder
constructs the runtime objects from the validated specs.

```
hosts.json dict ─┐
profile.defaults ─┼─► precedence merge (per-key) ─► HostSpec.model_validate ─► spec.build(cls)
repo host_defaults┘        (dict-space, M1)            extra='forbid', typed        │
                                                                          UnixHost / EmbeddedHost
```

`validate_host_dict`, the `_all_slots`-walking, the manual `isinstance` checks,
and the dict→dataclass conversions in `factory.py` are **deleted** — validation
is intrinsic to `model_validate`.

### Module layout (new leaf package)

| Module | Contents |
| --- | --- |
| `src/otto/models/base.py` | `OttoModel(BaseModel)` — shared `model_config = ConfigDict(extra='forbid')` |
| `src/otto/models/options.py` | `*OptionsSpec` for every protocol + forward specs |
| `src/otto/models/host.py` | `HostSpec` + `UnixHostSpec` / `EmbeddedHostSpec` |
| `src/otto/models/settings.py` | `SettingsModel`, `DockerSettings*`, `OsProfileSpec`, `ReservationConfigSpec`, `OttoEnvSettings` (pydantic-settings) |
| `src/otto/models/monitor.py` | `MetricPoint`, DB-row import/export models |
| `src/otto/host/options.py` | **unchanged** runtime `*Options` dataclasses |
| `src/otto/storage/factory.py` | collapses to: merge → `Spec.model_validate` → `spec.build(cls)` |

Dependency direction is clean: `models/` is a leaf (imports nothing from otto's
runtime); `storage`, `configmodule`, `monitor` import from `models`.

---

## 1. Option models — the two-type split

The `*Options` classes carry a real third-party seam (`extra` dicts spread into
`asyncssh.connect()`, the `post_connect` callable, the `_kwargs()` adapters). To
keep pydantic fully isolated from that seam, each protocol gets **two types**:

- **`*OptionsSpec`** (pydantic, in `models/options.py`) — the JSON boundary.
  Holds only **curated, JSON-serializable** fields. `extra='forbid'`.
- **`*Options`** (dataclass, in `host/options.py`) — **unchanged** runtime
  object. Keeps `extra`, callables, the `_kwargs()`/`_client_kwargs()` adapters,
  and is what `connections.py` / `transfer.py` / `session.py` consume.

The factory builds the runtime object from the validated spec via a
`spec.to_runtime() -> *Options` method (or an equivalent builder).

**Why two types and not one:** the runtime objects touch the libraries; the spec
must not. Keeping the runtime dataclasses byte-for-byte unchanged means the
churny async consumer code (transports, sessions, transfer) is **not touched at
all** in Phase A — the lowest-risk option. The accepted cost is a duplicated
field list per protocol, guarded against drift by a test (below).

### Passthrough policy — strict, with an explicit `extra` block

All option specs are `extra='forbid'`. The split is:

- **Library-forwarding specs** — `SshOptionsSpec`, `TelnetOptionsSpec`,
  `SftpOptionsSpec`, `ScpOptionsSpec`, `FtpOptionsSpec` — expose an explicit
  `extra: dict[str, Any]` field. Curated fields are validated (typos →
  suggestions); any uncurated library kwarg is supported **only** when written
  inside the explicit `extra` block, which doubles as documentation that "this
  is a raw library kwarg." `extra='forbid'` rejects unknown *top-level* keys; it
  never inspects the contents of the `extra` field, so the escape hatch stays
  fully open and uncoupled.

  ```json
  "ssh_options": {
      "port": 22,
      "connect_timeout": 5.0,
      "extra": { "rekey_bytes": 1000000, "gss_host": "krb.example" }
  }
  ```

- **otto-owned specs** — `NcOptionsSpec`, `SnmpOptionsSpec`, `TftpOptionsSpec`,
  and the forward specs (`LocalPortForward` / `RemotePortForward` /
  `SocksForward`) — strict, **no** `extra` passthrough (there is no library
  option set to forward; an unknown key is always a typo).

### Conversions move into the spec

Two conversions currently hand-coded in `factory.py` become `field_validator`s
on the specs: `TelnetOptionsSpec.login_prompt` (str → bytes) and
`SnmpOptionsSpec.oids` (list → tuple).

### Drift guard

A unit test asserts, for every protocol, that the `*OptionsSpec` field set is a
subset of the runtime `*Options` field set, and that a default-built spec
round-trips to a valid runtime object. Spec and runtime cannot silently diverge.

---

## 2. Host models and the unified host-class registry

### Family resolution against an open registry

`os_type` is an **open** registry key (custom profiles), so a pydantic
discriminated union does not fit. The real validation axis is the **family**
(unix vs embedded), derived from the host *class*, exactly as today. The factory
flow:

```python
merged        = precedence_merge(profile.defaults, repo_defaults, host_dict)   # M1
profile       = build_os_profile(merged.get('os_type', 'unix'))
cls, spec_cls = registry_lookup(profile.base)
spec          = spec_cls.model_validate(merged)      # extra='forbid', typed, suggestions
host          = spec.build(cls)                       # generic builder
```

The `issubclass(EmbeddedHost)` family-branching and the
`_create_unix_host` / `_create_embedded_host` split are removed; one generic
path driven by the spec replaces them.

### The models

- **`HostSpec`** (abstract base, `extra='forbid'`) — common contract: `ip`,
  `element`, `creds`, `name`, `user`, `element_id`, `board`, `slot`, `hop`,
  `os_type`, `os_name`, `os_version`, `resources`, `log`, `labs`, `interfaces`,
  `snmp`, `toolchain`, `command_frame`, `default_dest_dir`. Resolves the
  `command_frame` string → `CommandFrame` via the registry; owns
  `interfaces`/`address_for` (below).
- **`UnixHostSpec(HostSpec)`** — `term`, `transfer`, `is_virtual`,
  `docker_capable`, and the six `*_options` specs.
- **`EmbeddedHostSpec(HostSpec)`** — `filesystem`, `transfer` constrained to
  `console`/`tftp`, `telnet_options` only; a validator **rejects**
  `docker_capable` (replacing the hand-rolled factory check).

Because the family specs are **exhaustive**, `extra='forbid'` now catches the
membership/family keys (`labs`, `term`, `transfer`, `is_virtual`, …) that
`_all_slots` used to silently drop on a typo. `HostSpec` becomes the single
authoritative source for the accepted host-dict schema and its JSON Schema.

`ZephyrHost` and the built-in families add **no** new fields (subclasses only
re-declare defaults), so `UnixHostSpec` / `EmbeddedHostSpec` cover everything
otto ships.

### `command_frame` promoted to a common field

The `CommandFrame` is generic to all hosts — Unix uses a `BashFrame` (the
session manager defaults to it), embedded passes a `ZephyrFrame`, and it is
already a string-registry (`register_command_frame` / `build_command_frame`).
Today only `EmbeddedHost` exposes it as a field. Phase A promotes it to a common,
lab-declarable `HostSpec` field with per-family/class defaults (Unix →
`BashFrame`; bare embedded → required, fails loud; `zephyr` → `ZephyrFrame`).

**The one runtime host-class touch in Phase A:** `UnixHost` gains a
`command_frame` field (default `BashFrame`, passed to its session manager, which
already accepts one). This is the only behavior-bearing host-class change.

*Size assessment (why it stays here, not deferred):* `SessionManager` already
accepts and threads `command_frame` to the sessions it creates, so the change is
~under 10 lines — one dataclass field plus a `command_frame=self.command_frame`
kwarg at the two `SessionManager(...)` sites in `unix_host.py`, plus an optional
`__post_init__` str-guard mirroring `EmbeddedHost`; no `session.py` change. It is
kept in Phase A because it is small *and* deferring it converts a free pre-freeze
generalization into a breaking schema addition post-freeze.

### Multi-interface hosts (`interfaces` / `ip`)

Absorbs [todo/multi_interface_hosts.md](../../../todo/multi_interface_hosts.md).
A pre-freeze schema change, designed into the Host model now:

- `HostSpec.ip: str` — unchanged required **primary literal** (zero migration
  for existing single-`ip` hosts).
- `HostSpec.interfaces: dict[str, str] = {}` — additive, optional. A validator
  rejects a non-IP value.
- Runtime host gains `address_for(name_or_literal: str) -> str` — returns a
  literal IP unchanged, else resolves an interface name.
- `SnmpOptionsSpec.address` stays a plain string that defaults to the host `ip`
  and resolves via `address_for` — so the SNMP block needs no breaking change,
  exactly as its forward-compat note promised.

### First-party / third-party symmetry: one registration path

`register_host_class` carries the spec alongside the class, and otto registers
its **own built-ins through the same call** a third party uses:

```python
register_host_class('unix',     UnixHost,     UnixHostSpec)
register_host_class('embedded', EmbeddedHost, EmbeddedHostSpec)
register_host_class('zephyr',   ZephyrHost,   EmbeddedHostSpec)   # adds no fields → reuse
# third party, identical:
register_host_class('myos',     MyHost,       MyHostSpec)
```

The registry stores `(host_class, host_spec)` pairs. If `spec` is omitted, it
defaults to the spec registered for the nearest base class in the MRO (so a
custom subclass that adds no fields needs none; add fields → register a
`HostSpec` subclass). `spec.build(cls)` is the one generic builder; a custom
spec overrides `build`/adds validators for its own fields through the same
mechanism otto uses. The drift-guard test runs over **every** registered
`(cls, spec)` pair — built-in and third-party.

---

## 3. The merge: M1 — merge in dict-space, then validate once

Pydantic does not natively express "profile defaults < repo defaults < host
fields, per-key for `*_options`." A small, explicit reducer produces one merged
dict (per-key precedence for the `*_options` sub-tables), then a **single**
`HostSpec.model_validate(merged)`.

- `extra='forbid'` on the host spec and the option specs catches typos in **any**
  layer (profile, repo, or host) at one validation point.
- The reducer is ~10 lines of well-bounded precedence code; everything gnarly
  (slot-walking, type checks, sub-dict conversion) is deleted.

This is the honest reading of "pydantic replaces the hand-rolled merge": the
deleted code is the *messy* part; what remains is precedence ordering, which is
inherently a pre-validation concern.

---

## 4. Settings — `.otto/settings.toml` + `OTTO_*` env

**`SettingsModel(OttoModel)`** (`extra='forbid'`) validates the settings dict,
replacing `_parse_host_defaults` / `_parse_os_profiles` / `_parse_docker_settings`.
Fields: `name` (required), `version` (required, validator → `Version`),
`labs`/`libs`/`tests` (`list[Path]`), `valid_labs`/`init` (`list[str]`),
`host_defaults`, `os_profiles`, `docker`, `reservations`.

- **`${sut_dir}` expansion stays a pre-pass.** `_expand_recursive` runs first,
  then `SettingsModel.model_validate(expanded)`. Expansion is string
  substitution, orthogonal to validation — keeping it out of the model leaves
  `SettingsModel` context-free and its JSON Schema clean. Consistent with M1.
- **`host_defaults` reuses the option specs**, kept **partial** for the per-key
  merge: `OptionsSpec.model_validate(table).model_dump(exclude_unset=True)`
  validates types + forbids typos while recovering only the user-set keys. Same
  for a profile's `*_options` defaults.
- **`docker` becomes pydantic models** (`DockerSettings` / `DockerImage` /
  `DockerCompose`), replacing `_parse_docker_settings`. (Its TOML *shape* is
  unchanged; this is a validation-mechanism swap bundled in now while we are
  here.)
- **`os_profiles`** → `OsProfileSpec` (`base` required; remaining keys collected
  as a defaults dict, validated against the family spec when later merged into a
  host).
- **`reservations`** is **not** opaque. Split:
  - `ReservationConfigSpec` validates the otto-owned envelope (`backend: str`,
    `url: str | None`); the backend-specific `[reservations.<backend>]`
    sub-table stays **open** (backend-defined kwargs forwarded to the backend's
    `__init__` — otto-core cannot type it).
  - The built-in `JsonReservationBackend` parses a reservation **file** — that
    structured external data gets a pydantic model so malformed entries raise a
    clear error rather than a vague `ReservationBackendError`. (Confirm the
    exact file shape during planning.)
  - The `ReservationBackend` Protocol returns already-normalized primitives
    (`set[str]`, `str | None`); nothing to model at that seam.
- **`OTTO_*` env** → a `pydantic-settings` `BaseSettings` model (`OttoEnvSettings`)
  formalizing what `configmodule/env.py` reads ad hoc. Candidate vars:
  `OTTO_SUT_DIRS`, `OTTO_LAB`, `OTTO_XDIR`, `OTTO_LOG_DAYS`, `OTTO_LOG_LEVEL`,
  `OTTO_LOG_RICH`, `OTTO_COMPOSE_SUFFIX`, `OTTO_FIELD_DEFAULT`,
  `OTTO_FIELD_PRODUCTS`, `OTTO_BASE`. The exact in-scope set is finalized in the
  plan. (`OTTO_COMPLETE` is Typer's; `OTTO_PEN` is a code constant — both
  excluded.)

---

## 5. Monitor records

- **`MetricPoint(OttoModel)`** — `ts` / `value` / `meta`; replaces the
  `(ts, value, meta)` 3-tuple in `MetricCollector._series`. Absorbs
  [todo/metric-point-dataclass.md](../../../todo/metric-point-dataclass.md). The
  hot append path uses `MetricPoint.model_construct(...)` (trusted, validation
  skipped); the DB/JSON **import** path uses full `model_validate`.
  `get_series()` returns `dict[str, list[MetricPoint]]`; consumers move from
  positional unpacking to `.ts`/`.value`/`.meta`, and the `getMonitorResults`
  metadata-strip wrapper goes away.
- **DB import/export row models** for `metrics(ts, host, label, value)` and
  `events(ts, end_ts, label, source, color, dash)` — used at the JSON
  import/export boundary and the dashboard `/api/data` serialization (the actual
  untrusted read-back seam).
- **`SnmpMetric`** (the OID → chart **descriptor**, pure data + a `to_point`
  helper) → pydantic `frozen=True` model. Low-volume (built-ins + registered),
  so no `model_construct` needed. Combined with the symmetric registration
  below, one validation path covers built-in and third-party descriptors.
- **Not converted:** `MetricParser` is an **ABC with `parse()`** (behavior, like
  `CommandFrame`) — stays a class. `MetricDataPoint` is a lightweight
  `NamedTuple` value — left as-is.

### SNMP-metric registration symmetry

Today the built-in descriptors are loaded by **direct dict construction**
(`_SNMP_METRICS = _default_metrics()`), bypassing the `register_snmp_metric()`
entry point that custom descriptors use. Phase A makes otto register its
built-ins through the **same** public path:

```python
_SNMP_METRICS: dict[str, SnmpMetric] = {}

def _register_builtin_metrics() -> None:
    for m in (SnmpMetric(OID_SYS_UPTIME, 'Uptime', ...), ...):
        register_snmp_metric(m)

_register_builtin_metrics()
```

One path → one validation, applied uniformly to first- and third-party
descriptors. This mirrors the host-class registry decision.

**Bounding principle:** Phase A applies first/third-party registration symmetry
**only where it is already converting the registry's value type to pydantic** —
host specs and SNMP metric descriptors. The same asymmetry in the
**behavior-class** registries (`_FRAME_CLASSES`, `_FILESYSTEM_CLASSES`,
`DEFAULT_PARSERS`) is deferred to a separate hygiene pass — see
[todo/registry_builtin_registration_symmetry.md](../../../todo/registry_builtin_registration_symmetry.md).

---

## 6. JSON Schema export

The `HostSpec` / family specs and `SettingsModel` export JSON Schema for editor
autocomplete on `hosts.json` and `settings.toml`. Add a generation entry point
(a `make schema` target or a script under `scripts/`) and a snapshot test that
fails if the committed schema drifts from the models.

---

## 7. The Phase A spike (dual-purpose — informs, does not implement)

Run during Phase A; the deliverable is a **written report** that gates Phase B's
placement.

1. **Compatibility hinge.** Can `RepoOptions` / suite `Options` move from stdlib
   dataclass to a pydantic type **without breaking user subclassers**
   (`class Options(RepoOptions): ...`)? Probe `pydantic.dataclasses.dataclass`
   as a near drop-in.
   - Yes → Phase B stays fully post-freeze (shortest path holds).
   - No → only the base-class shape decision is pulled pre-freeze (freeze
     `Options` as pydantic from the start); the bridge + Typer triage still
     trail.
2. **Typer scope read.** Reproduce the failing `typer` 0.26 bump
   ([ludachrish3/otto-sh#47](https://github.com/ludachrish3/otto-sh/pull/47)) far
   enough to confirm the break is in the **option-expansion / signature-
   introspection** layer (`options_params` in `params.py`, `_wrap_with_options`
   in `cli/run.py`, `register_suite` in `suite/register.py`), not the state
   layer — confirming Phase B can absorb it.

Default assumption pending the result: **Phase B is fully post-freeze.**

---

## Scope

### In scope

Option specs (two-type split, `extra='forbid'`, explicit `extra` passthrough on
the five library-forwarding protocols); `HostSpec` + family specs via the
unified `register_host_class(name, cls, spec)`; `command_frame` promoted to a
common field (+ the `UnixHost` field touch); `interfaces` / `address_for`;
generic factory collapse + the M1 merge; `SettingsModel` (incl. `[docker]`
models, `os_profiles`, reservation envelope + JSON-backend-file model);
`pydantic-settings` for `OTTO_*`; monitor records (`MetricPoint`, DB
import/export rows, `SnmpMetric` + symmetric registration); JSON Schema export;
the spike report.

### Out of scope (deferred)

- **Pydantic Phase B** — converting `RepoOptions` / suite `Options` to pydantic,
  the pydantic→Typer bridge, and the **Typer 0.26 triage** (post-freeze by
  default, conditional on the spike).
- `transfer.py` per-backend split, test-tree restructure, registry public API
  (separate workstream #4).
- Behavior-class registry symmetry (frames/filesystems/parsers) — see the
  hygiene note.
- The `typer<0.26` ceiling **stays** through Phase A.

---

## Dependencies

Add `pydantic` (v2) and `pydantic-settings` to `pyproject.toml` `dependencies`
(currently neither is present). **Confirm the air-gap / offline wheel
provisioning includes `pydantic`, `pydantic-core`, and `pydantic-settings`** for
all supported Pythons (3.10–3.14) before merge — the outcome doc notes
`pydantic-core` is already provisioned; verify the others.

---

## Testing & verification

- **Factory / hosts:** precedence-merge scenarios (host > profile > repo,
  per-key for `*_options`); `extra='forbid'` typo errors *with* field
  suggestions; family resolution; `interfaces`/`address_for`; custom-class spec
  registration; the `(cls, spec)` drift guard; the option-spec ↔ runtime
  drift-guard.
- **Settings:** `host_defaults` / `docker` / unknown-key errors; partial-table
  `exclude_unset` merge; reservation envelope + JSON-backend-file validation;
  `OTTO_*` env parsing.
- **Monitor:** `MetricPoint` round-trip + `model_construct` hot path; DB
  import/export row validation; `SnmpMetric` built-in registration through the
  public path.
- **Schema:** JSON Schema export snapshot test.
- **Gates:** `make test` (full suite incl. live VM tiers — **do not kill
  mid-run**); `make coverage` (single-process, ≥ 90% gate); `make nox` (all
  Pythons); `ty` 0 diagnostics; ruff clean.

---

## Execution notes

- Branch off post-WS#2 `main` (`179ce81`); rebase onto current `main` before
  merge (main is force-pushed under dependabot/releases).
- **Stage only — Chris commits.** Agent self-commit mis-tags the AI-assist
  trailer; the `prepare-commit-msg` hook needs `/dev/tty`.
- Next after Phase A: Registry public API (workstream #4) → **FREEZE**.
