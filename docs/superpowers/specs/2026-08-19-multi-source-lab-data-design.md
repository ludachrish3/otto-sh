# Multi-Source Lab Data (`[[lab.sources]]`) — Design

**Date:** 2026-08-19
**Status:** Approved design, pre-implementation
**Origin:** `todo/lab_flexibility.md`; deferred by
`docs/superpowers/specs/2026-08-18-project-lab-host-scoping-design.md` §13.

## 1. Problem

Some lab data is global truth: physical devices whose records live in a
database (or a globally referenced JSON file), where every team at any moment
must be served the same host data. Other lab data is repo-owned: VMs and QEMU
guests that a project's team deploys, re-images, and reconfigures at will —
their definitions belong in the project repo and change with its emulation
needs.

otto today supports exactly one host-source backend per process
(`[lab] backend = "<name>"`), and when several repos declare one, the first
repo in `OTTO_SUT_DIRS` order wins while the others' declarations are
silently ignored. There is no way to combine a global database with
repo-owned host definitions.

## 2. Goal

A general mechanism: each repo declares an **ordered list of host-data
sources** — any registered `LabRepository` backend (databases by whatever
addressing the backend takes; JSON files by absolute path or repo-relative
path). otto reads them all and combines them, with later sources overriding
earlier ones per host record, loudly.

## 3. Decisions locked during brainstorm

- **Override, not fail-loud, on cross-source collision.** When two sources
  define the same host id for the same lab, the later source's record wins
  wholesale and otto logs a warning naming both sources. Rationale: this is
  the mechanism for testing a data change locally before saving it to the
  database, and the warning provides the transparency.
- **Whole-record override.** The later record replaces the earlier one
  entirely; there is no field-level merge. To test a DB change you paste the
  complete modified record into the repo source.
- **Hard cutover.** `[[lab.sources]]` is the only lab-source config shape.
  The legacy top-level `labs = [...]` key, the `[lab] backend =` key,
  `[lab.<backend>]` kwarg tables, and the long-dead passthrough
  `lab_data_type` key are removed, and their presence is a fail-loud parse
  error with a migration-pointing message. otto has zero external users; one
  shape, no aliases.
- **Mechanism only.** No built-in database backend ships. Databases plug in
  through the existing `register_lab_repository` registry and are checked by
  the existing conformance suite; a generic backend cannot guess an org's
  schema, and otto takes no DB-driver dependencies.
- **Lab metadata is out of scope.** Arbitrary lab-scoped/environment-scoped
  metadata (the "usernames valid to query", lab-configuration-blob thread)
  is explicitly deferred to a future spec.

## 4. Config shape

`.otto/settings.toml` may declare an ordered array of sources:

```toml
[[lab.sources]]
name = "global"              # optional label, used in warnings and errors
backend = "cmdb"             # any name registered via register_lab_repository
server = "cmdb.example.com"  # remaining keys = constructor kwargs for that backend

[[lab.sources]]
name = "virtual"
backend = "json"
paths = ["lab"]              # json-specific; entries are directories or .json files
```

Rules:

- `backend` is required and must name a registered backend (the existing
  "unknown backend" error, listing registered names, applies).
- `name` is optional. The source's **label** — used in every warning and
  error — is always `<repo-name>/<name-or-default>`, where the default is
  `<backend>#<ordinal>` (1-based position within that repo's list). Two
  sources in one repo may not share an explicit `name` (parse error); the
  repo prefix disambiguates across repos.
- **json source:** `paths` is required and non-empty. Each entry is either a
  directory (searched for `lab.json`, today's rule) or a path ending in
  `.json` (used directly as a lab file). Entries are repo-relative (anchored
  to the repo root, same anchoring as every other settings path) or
  absolute. No keys beyond `backend`/`name`/`paths` are accepted — the json
  backend takes nothing else, and an unknown key is a typo, not cargo.
- **Custom backend source:** every key other than `backend`/`name` is passed
  to the backend's constructor as a keyword argument, plus `repo_dir=<repo
  root>` — the same constructor contract `[lab.<name>]` tables had.
- The `[lab]` table may contain **only** `sources`. Any other key — notably
  the removed `backend` — is a parse error telling the user to move it into
  a `[[lab.sources]]` entry. A top-level `labs = [...]` key is likewise a
  parse error: "replace with `[[lab.sources]]` `backend = "json"`,
  `paths = [...]`". Silent ignoring would strand a repo's lab data
  invisibly.
- A repo with no `[lab]` table contributes no sources. That is normal for
  dependency repos.

## 5. Aggregation across repos

The process-wide source list is the concatenation of each repo's
`[[lab.sources]]` list (in file order) in `OTTO_SUT_DIRS` repo order. Later
entries override earlier ones — the same later-overlays-earlier direction
`[host_preferences]` already uses.

This replaces the "first repo's `[lab]` block wins, the rest are silently
dead" rule. Every declared source is live.

If no repo declares any source, construction still succeeds (an empty
composite): `list_labs()` returns `[]`, summaries return nothing, and
`load_lab` raises `LabNotFoundError` with the guidance "no repo declares a
`[[lab.sources]]` entry" — loud exactly where a lab is actually demanded, so
lab-free commands are unaffected.

## 6. Composite semantics

New `CompositeLabRepository` in `otto.labs`, holding ordered
`(label, repository)` pairs. It satisfies `LabRepository` and
`SupportsHostSummaries`, so everything downstream — `config.lab.load_lab`,
completion, the completion cache, per-project scoping's `source_lab` stamp,
the conformance suite — consumes it unchanged.

When the compiled process-wide list has exactly one source, the bare backend
is returned instead of a composite; single-source setups run the same code
they would without this feature.

### 6.1 `load_lab(name, preferences)`

- Ask each source **in order**, forwarding `preferences` verbatim.
- A source's `LabNotFoundError` is absorbed (that source doesn't know this
  lab). Any other `LabRepositoryError` **propagates immediately** — a dead
  global database must never be silently dropped from the merge.
- If no source knows the lab: raise `LabNotFoundError` naming the requested
  lab and every source label consulted.
- Otherwise merge the per-source labs in order into a single `Lab`:
  - `name` = the requested lab name; `component_names = [name]`.
  - **Hosts:** keyed by host id. A colliding id → the later source's host
    replaces the earlier one wholesale, and otto logs
    `logger.warning("host %r in lab %r: %s overrides %s", host_id, name,
    later_label, earlier_label)`. A different `ip` on the override record is
    allowed — repointing at a re-imaged VM is a legitimate use.
  - **Links:** merged keyed by link id, later source wins on collision.
  - **Resources:** recomputed from the *winning* host set (union of the
    merged hosts' `resources`), plus any lab-level resources a backend added
    beyond its own hosts' (each source lab's `resources` minus its hosts'
    union). A plain union would keep a resource name an override deliberately
    dropped, and the reservation gate would then demand reserving a resource
    that no longer exists.
  - Logical indices are re-stamped after the merge
    (`_assign_logical_indices`), as every lab-producing path does.
- This merge is **not** `Lab.__add__`. `__add__`'s semantics (same-id +
  same-ip dedup, same-id + different-ip error) remain reserved for
  cross-*lab* merges (`a+b`), which still happen afterwards in
  `config.lab.load_lab` over the composite's per-lab results. The composite
  merges the *same* lab across *sources*; the two operations never share
  code or rules.
- The composite does not inject the built-in `local` host —
  `config.lab.load_lab` keeps that job.
- `source_lab` stamping is untouched: it names the component **lab**, not
  the source, so `[project]` scoping stays source-agnostic by construction.

### 6.2 `list_labs()`

Sorted union across sources. A backend error propagates — hiding a dead
source behind a shorter lab list would be a lie. (Existing consumers already
guard: `get_lab_panel` renders the error in-panel; completion paths catch
broadly.)

### 6.3 `list_host_summaries()`

Union by host id, built per source via the existing
`otto.labs.host_summaries()` helper (so a backend without
`SupportsHostSummaries` still contributes through the load-and-summarize
fallback). Later source wins the colliding summary; `labs` membership lists
are unioned. **Best-effort by contract:** a source that errors during
summarizing is skipped with a debug log — summaries feed shell completion,
which must never crash or spam the shell. No override warnings here.

### 6.4 Within-source behavior is unchanged

A duplicate host id inside one source (e.g. two records in the same json
source's files) keeps that source's fail-loud behavior — inside a source it
is a typo. Splitting data across two `[[lab.sources]]` entries is the opt-in
to override semantics.

## 7. Construction seam

- **New:** `build_lab_sources(repos) -> LabRepository` — compiles each
  repo's parsed source list, constructs each backend through the registry
  (json → `search_paths` from anchored `paths`; custom → `repo_dir=` +
  inline kwargs), and wraps the result (composite, bare single source, or
  empty composite).
- **Removed:** `build_lab_repository(settings, repo_dir, search_paths)` —
  its signature exists to parse the removed shape. Per-entry construction
  becomes an internal helper of `build_lab_sources`.
- **Call sites that switch:** `build_lab_from_repos`
  (`src/otto/cli/invoke.py`), both completion-cache repository builders
  (`src/otto/config/completion_cache.py`), and `Repo.get_lab_panel`
  (`src/otto/config/repo.py`) — the panel compiles **only its own repo's**
  list, keeping it a per-repo view; a repo with zero sources renders that
  fact instead of an error.
- **`Repo` model:** the `labs: list[Path]` field is replaced by the parsed,
  validated source list (settings-model instances, paths already anchored).
  Settings validation replaces `LabConfigSpec` with a `[lab]` model whose
  only key is `sources` (`extra="forbid"`), plus a per-entry source model
  (`backend` required; json entries validated as §4; custom entries
  `extra="allow"`).
- **Library API unchanged:** `config.lab.load_lab(labnames, search_paths=…,
  repository=…)` keeps its signature — `search_paths` is a code-level
  convenience over the json backend for scripts, not a config shape.
- The json backend's `_find_lab_files` gains the file-vs-directory rule from
  §4 (a `.json` path is used directly; a directory is searched for
  `lab.json`).

## 8. Error surfaces

Fail-loud at settings parse (bootstrap):

- `[lab]` carrying any key other than `sources` (names the migration).
- Top-level `labs = [...]` present (names the migration).
- Empty `[[lab.sources]]` array (declare a source or delete the table).
- json source with missing/empty `paths`, or with unknown keys.
- Duplicate explicit `name` within one repo's list.

Fail-loud at construction / load:

- Unknown `backend` name (existing registry error listing registered names).
- Backend construction or query failures propagate as today
  (`LabRepositoryError`); `load_lab` additionally distinguishes
  `LabNotFoundError` per §6.1.
- Lab requested but no source knows it / no sources declared:
  `LabNotFoundError` naming the sources consulted (or the absence of any).

Warnings:

- Cross-source host override (§6.1) — the only new warning; names lab, host
  id, winning label, losing label.

## 9. Behavior changes (breaking)

1. **Config shape:** the only lab-source spelling is `[[lab.sources]]`; the
   legacy `labs = [...]` / `[lab] backend =` / `[lab.<backend>]` spellings
   become parse errors. Every in-tree settings file, template, and doc
   migrates in this workstream.
2. **All declared sources are live:** previously the first repo's `[lab]`
   block won and other repos' declarations (and, with a custom backend,
   every repo's `labs` paths) were silently ignored. A repo whose lab data
   was silently dead becomes live.
3. **Cross-source host-id collision** is now a warning + override; it was
   previously impossible by construction (one backend per process).

## 10. Testing

- **Unit — compilation:** each parse-error case in §8; ordering (file order
  within a repo, `OTTO_SUT_DIRS` order across repos); label derivation and
  uniqueness; json path anchoring (relative vs absolute; file vs directory);
  custom-backend kwarg forwarding incl. `repo_dir`.
- **Unit — composite:** override replaces the record wholesale (assert the
  winning host object's fields, not just presence); the warning fires with
  both labels (assert text); within-source duplicate still fails loud;
  links/resources merge rules; preferences forwarded verbatim to every
  source; all-miss `LabNotFoundError` naming labels; non-NotFound error
  propagation from a mid-list source; single-source bare return; empty
  composite behavior (§5).
- **Conformance:** `assert_lab_repository_conforms` against a composite of
  two example backends — the composite is itself a backend and must honor
  the full contract, summaries agreement included.
- **e2e:** a repo with a "global" json source plus a repo-local json source
  overriding one host: the CLI resolves the override (the host built from
  the later record) and logs the warning; completion offers the union.
- Every guard follows the house rule: mutation-proven (show red under the
  inverse mutation) — plan-level detail per task.

## 11. Documentation

- `docs/guide/setup/host-database.md`: reworked around `[[lab.sources]]` —
  declaring sources, ordering/override semantics with the
  test-a-DB-change-locally story, json file-vs-directory paths, custom
  backends as sources (no legacy section retained).
- `docs/guide/setup/repo-setup.md`: `labs` key replaced by the new shape.
- `otto init` template writes a single json source (`paths = ["lab_data"]`,
  matching the existing scaffold directory); template drift guard updated.
- Example backend docs show a DB-style source composing with a repo json
  source.

## 12. Out of scope / deferred

- **Lab/environment metadata** (opaque, both-scoped) — own future spec.
- **Built-in database backend** (sqlite or SQL-generic) — custom backends
  via the registry remain the path.
- **Field-level record patching / augment overlays** — whole-record override
  only.
- **Per-source runtime selection** (e.g. `--skip-source`) — nothing selects
  sources at runtime.
- **Attribute-based scoping patterns and other §13 items** of the scoping
  spec — unchanged by this work.
