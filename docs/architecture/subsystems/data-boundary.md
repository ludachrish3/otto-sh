# Data at the boundary

otto consumes data from three outside sources: lab files (`lab.json`),
repo settings (`.otto/settings.toml`), and `OTTO_*` environment variables.
The rule for all of them is the same: **pydantic at the boundary, plain
objects inside**. External data is validated exactly once, by a spec model in
`otto.models`, and what crosses into the rest of the codebase is an ordinary
runtime object that is never re-validated.

## The spec → runtime pattern

Each kind of input has a `*Spec` model whose job ends at construction:

```text
lab.json entry      → HostSpec.model_validate(...)  → spec.to_host(cls, ...)   → UnixHost
settings.toml tables  → settings spec models          → spec.to_runtime()        → backend objects
OTTO_* environment    → OttoEnvSettings               → typed fields (paths, …)
```

The split keeps validation errors where the *data* is (a bad `lab.json`
field fails with a pydantic error naming the file and field, not a traceback
deep in connection code) and keeps runtime classes free of parsing concerns.
Field names are `snake_case` end to end — JSON, TOML, models, and runtime
attributes all agree, so there is no translation layer.

One deliberate escape hatch: keys beginning with `_` are stripped from each
`lab.json` entry before validation — the sanctioned way to keep comments
in a format that has none (`"_comment": "…"`). Everything else unknown is
still rejected loudly.

## Host construction

{class}`~otto.models.host.HostSpec` is the abstract boundary model for one
lab-data host entry; `UnixHostSpec` and `EmbeddedHostSpec` extend it with
family-specific fields (menus like `valid_transfers`, embedded strategy
selectors like `filesystem` and `binary_loader`, per-protocol option tables
like `ssh_options`):

```{inheritance-diagram} otto.models.host.UnixHostSpec otto.models.host.EmbeddedHostSpec
:parts: 1
:top-classes: otto.models.host.HostSpec
```

Construction, driven by
{func}`otto.host.factory.create_host_from_dict`, runs in a fixed order:

1. The entry's `os_type` selects an {class}`~otto.host.os_profile.OsProfile`,
   which names the base family — and thereby the host class and which spec
   validates the entry.
2. Profile defaults are merged under the host's own fields (explicit lab data
   always wins over profile defaults), and preference-resolved option
   defaults are folded in ({doc}`hosts`).
3. The spec validates the merged dict; `to_host()` builds the runtime host.
4. Product providers run, attaching products ({doc}`hosts`).

A drift guard in otto's test suite enforces that runtime host fields and spec
fields stay mirrored — adding an init field to a host class without its spec
counterpart fails CI.

## Settings and environment

`OttoEnvSettings` (pydantic-settings) is the single reader of `OTTO_*`
variables. Repo `settings.toml` files are parsed during bootstrap phase 1
into `Repo` objects ({doc}`../lifecycles/index`); their tables (`[docker]`,
`[reservations]`, `[coverage]`, `[[os_profiles]]`, `[host_preferences]`) each
have spec models. `otto.models.settings` is deliberately a leaf module — it
must not import the packages it configures, or validation would drag the app
graph into every boundary crossing.

## Where labs come from: the labs package

Lab loading is behind a protocol so hosts don't have to come from JSON files:
`LabRepository` (in {mod}`otto.labs.protocol`) is the host-source
contract, the built-in `json` backend reads `lab.json` files from the
configured lab paths, and alternatives (a database, an inventory service)
register a name via {func}`otto.labs.register_lab_repository`.
{func}`otto.testing.assert_lab_repository_conforms` verifies a custom backend
against the contract, and `otto.examples.lab_repository` is a copyable
reference implementation. See {doc}`../../guide/host-database`.

Merging is part of loading: `--lab` may be passed multiple times and the
resulting `Lab` objects merge, so a shared lab file and a personal overlay
compose without editing either.

## Schemas as exports, not just validation

Because every boundary is a pydantic model, otto can *emit* its data contracts:
`otto schema export` writes JSON Schemas for `lab.json`,
`settings.toml`, and reservation files, which editors use for completion and
inline validation ({doc}`../../guide/editor-schemas`). The schema version is
bumped when host-spec fields change shape, keeping downstream lab data
diagnosable.

## Filesystem awareness

One boundary is physical: where otto *writes*. `otto.filesystem` detects
network filesystems (NFS/SMB), and write-heavy components adapt — the monitor
database uses SQLite WAL journaling on local disks but DELETE journaling on
network mounts (where WAL's shared-memory semantics are unreliable), and log
rotation time-boxes its directory scans so an NFS stat storm cannot stall
startup ({doc}`../utilities/logging`).
