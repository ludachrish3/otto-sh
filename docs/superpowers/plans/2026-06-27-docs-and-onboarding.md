# Backend Docs + Team-Setup Onboarding Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the workstream's documentation layer: ship a new **host-database** guide, upgrade the **reservations** guide (executed sample, multi-holder `who_reserved`, register-by-name selection replacing the removed dotted-path narrative, break-glass + `list_usernames` completion, a conformance "verify your backend" section), turn **repo-setup** into the one-time **team-setup** onboarding hub, and link inward from getting-started/overview — all with executable `{doctest}` blocks so the documented examples are continuously tested.

**Architecture:** Documentation-only. Each runnable example is a MyST ```` ```{doctest} ```` block that imports the **shipped, tested** reference backends (`otto.examples.lab_repository` / `otto.examples.reservations`) and the conformance helpers (`otto.testing`) — so the guide demonstrates the same artifact the unit suite verifies, and Sphinx's doctest builder re-runs it on every docs build. Non-runnable config/wiring is shown as plain ```` ```python ```` / ```` ```toml ```` fences (no `>>>`, so the markdown doctest-lint guard stays satisfied). No `...`-stub "skeleton" that can rot silently is introduced or kept.

**Tech Stack:** MyST Markdown + Sphinx (`sphinx.ext.doctest`, nitpicky `-W`, zero ignores), `scripts/lint_markdown_doctests.py` (forbids `>>>` outside `{doctest}` fences), `make docs` (= `docs-lint` + `docs-html` + `doctest` + `doctest-src`).

**Scope note:** This is **Plan D** (final phase) of the "pluggable host source + backend conformance" design (`docs/superpowers/specs/2026-06-25-pluggable-host-source-and-conformance-design.md` §6). It depends on **Plan A** (pluggable host source — committed), **Plan B** (reservation modernization — committed), and **Plan C** (conformance suite + `otto.examples` samples + `otto.testing` helpers — **staged, not yet committed**). Because the guide doctests import `otto.examples.*` and `otto.testing`, those modules must be present in the working tree when this plan's `make docs` runs — they are (Plan C is staged). Plan D adds **no source or test code**, only docs.

## Global Constraints

- **STAGE-ONLY — never `git commit`.** Each task's final step is `git add <listed files>`. The controller captures per-task tree snapshots.
- **BED-FREE — never touch lab/test-bed resources.** Everything here is docs + a docs build. Do **not** run `make coverage`, `make nox`, or any Vagrant/QEMU/SSH target. The gate is `make docs` (+ `make typecheck` and `make coverage-unit` once at the end as a no-regression check, both bed-free).
- **Never add `from __future__ import annotations`** (repo-wide rule — breaks Sphinx nitpicky). N/A here (no Python files change), but the constraint stands.
- **Doctest discipline:** every example containing a `>>>` prompt MUST be inside a ```` ```{doctest} ```` fence — `scripts/lint_markdown_doctests.py` makes a `>>>` in any other fence (or outside fences) a hard error. Config/wiring examples (no `>>>`) use plain ```` ```python ```` / ```` ```toml ```` / ```` ```text ```` fences.
- **All `{doctest}` blocks must pass** under `make doctest` (Sphinx). They are self-contained: each imports what it uses (the doctest namespace only pre-provides `run`, `Status`, `CommandStatus`, `split_on_commas`, `LocalHost`, `human_readable` — none of which these blocks rely on). Use only the deterministic, already-verified sample behavior:
  - `ExampleLabRepository().list_labs()` → `['east', 'west']`; `load_lab("east").name` → `'east'`; `sorted(load_lab("east").hosts)` → `['router1']`.
  - `ExampleReservationBackend().backend_name()` → `'example'`; `.who_reserved("shared")` → `['alice', 'bob']`; `sorted(.get_reserved_resources("alice"))` → `['lab-a', 'shared']`; `.list_usernames()` → `['alice', 'bob']`.
  - `assert_lab_repository_conforms(...)` / `assert_reservation_backend_conforms(...)` return `None` (no output line) on success.
- **No registry-mutating calls in `{doctest}` blocks** (`register_lab_repository` / `register_reservation_backend` pollute global state across the doctest session). Show registration as a plain ```` ```python ```` fence (no `>>>`), never as a runnable doctest.
- **Factual accuracy against the committed/staged code:**
  - Host source: `LabRepository.load_lab(name, preferences=None) -> Lab`, `list_labs() -> list[str]`; `[lab] backend = "<name>"` (default `"json"`); custom backend constructed as `cls(repo_dir=<root>, **[lab.<name>])`; unknown name → `LabRepositoryError`; built-in `json` reads `hosts.json` from the `labs` search paths.
  - Reservations: `who_reserved(resource) -> list[str]` (empty = no holders, never `None`); selection is **register-by-name only** (`register_reservation_backend("name", Cls)` from an `init` module + `[reservations] backend = "name"`); the `pkg.mod:ClassName` dotted-path is **gone**; `-R` now **skips backend construction entirely** (a backend that fails/hangs in `__init__` can't block); optional `SupportsUsernameCompletion.list_usernames()` powers cached `--as-user` completion.
- Match the existing guides' voice and MyST conventions (deflist `term\n: def`, `{doc}`/`{ref}` roles, relative `../api/*.rst` links).

---

## File Structure

**New**
- `docs/guide/host-database.md` — the host-source guide.

**Modified**
- `docs/guide/index.rst` — add `host-database` to the toctree.
- `docs/guide/reservations.md` — register-by-name, executed sample, multi-holder return, conformance + completion sections, reconciled break-glass/troubleshooting.
- `docs/guide/repo-setup.md` — `[lab]` + `[reservations]` field reference, startup-step note, **Team setup checklist** (labeled `team-setup-checklist`).
- `docs/getting-started.md` — inward links to the checklist.
- `docs/overview.md` — inward link to the checklist.

---

## Task 1: New host-database guide

**Files:**
- Create: `docs/guide/host-database.md`
- Modify: `docs/guide/index.rst`

- [ ] **Step 1: Write `docs/guide/host-database.md`**

Create the file with exactly this content:

````markdown
# Host Database

Otto builds its lab — the set of hosts a command can touch — from a **host
source**. By default that source is the `hosts.json` files under your `labs`
directories, but the source is a pluggable backend: point otto at a CMDB, an
inventory API, or any system of record by implementing one small interface.

```{note}
Choosing a host source is a one-time, team-level decision — part of setting otto
up for your team. See the {ref}`team-setup-checklist` in {doc}`repo-setup`.
```

Otto is strictly a consumer of host data. It reads hosts; it never writes back
to your source of record.

## The interface

A host source implements the [`LabRepository`](../api/storage.rst) protocol —
two read-only methods:

`load_lab(name, preferences=None) -> Lab`
: Build and return the named lab. Raises
  [`LabNotFoundError`](../api/storage.rst) if the name is unknown.

`list_labs() -> list[str]`
: The lab names this source can provide.

Configuration is supplied at construction time, so a backend is built once and
then queried.

## Quick start: the built-in JSON source

The default backend is `"json"`: it reads `hosts.json` from each directory in
your `labs` setting. No `[lab]` block is required — a repo with just
`labs = [...]` already uses it:

```toml
name = "my_project"
version = "1.0.0"

labs = ["${sut_dir}/lab_data"]
```

Writing it out explicitly is equivalent:

```toml
[lab]
backend = "json"
```

The per-host `hosts.json` schema — every field, and how labs merge — lives in
{doc}`lab-config`.

## Selecting a different source

`[lab] backend` selects any **registered** backend by name. Register your
backend from an `init` module (one of the modules listed in `init = [...]`),
then name it in settings:

```python
# my_lab_source.py  (listed in init = [...])
from otto.storage import register_lab_repository
from my_company.cmdb import CmdbLabRepository

register_lab_repository("cmdb", CmdbLabRepository)
```

```toml
[lab]
backend = "cmdb"

[lab.cmdb]
url = "https://cmdb.example.com"
```

Otto constructs the backend as
`CmdbLabRepository(repo_dir=<repo root>, url="https://cmdb.example.com")` — the
`[lab.<name>]` sub-table becomes keyword arguments, plus `repo_dir` for
resolving any relative paths. Selecting an unregistered name raises
[`LabRepositoryError`](../api/storage.rst), listing the registered names.

```{note}
This is the same named-registry mechanism otto uses everywhere else
(`register_term_backend`, `register_reservation_backend`, `register_host_class`).
An `init` module always imports before the lab is loaded, so the name is
registered by the time settings select it.
```

## Writing a custom backend

A backend is any class satisfying the two-method protocol. Otto ships a small,
dependency-free reference implementation —
[`otto.examples.lab_repository.ExampleLabRepository`](../api/examples.rst) — that
you can copy from `src/otto/examples/lab_repository.py` as a starting point. It
holds a mapping of lab name to host dicts and builds real hosts with
[`create_host_from_dict`](../api/storage.rst) so each becomes a `RemoteHost`
keyed by its `id` — which is what the rest of otto expects.

The shipped sample works out of the box and demonstrates the contract:

```{doctest}
>>> from otto.examples.lab_repository import ExampleLabRepository
>>> repo = ExampleLabRepository()
>>> repo.list_labs()
['east', 'west']
>>> lab = repo.load_lab("east")
>>> lab.name
'east'
>>> sorted(lab.hosts)
['router1']
```

Loading an unknown lab raises the contract's error — never a bare `KeyError` or
`None`:

```{doctest}
>>> from otto.storage import LabNotFoundError
>>> try:
...     repo.load_lab("does-not-exist")
... except LabNotFoundError:
...     print("not found")
not found
```

## Error contract

A backend signals trouble through two exceptions (from
[`otto.storage`](../api/storage.rst)):

[`LabNotFoundError`](../api/storage.rst)
: `load_lab` was asked for a name the backend does not know. Raise this — never
  return `None` or raise a bare `KeyError`.

[`LabRepositoryError`](../api/storage.rst)
: Any other failure (I/O, network, parse, credentials) that prevents a
  definitive answer. `LabNotFoundError` is a subclass, so callers can catch the
  base.

## Verify your backend

Otto ships a conformance helper that checks a backend against the full contract
and reports **every** violation at once (it raises a single `AssertionError`
listing each failed rule). The shipped sample conforms:

```{doctest}
>>> from otto.testing import assert_lab_repository_conforms
>>> from otto.examples.lab_repository import ExampleLabRepository
>>> assert_lab_repository_conforms(
...     ExampleLabRepository(), expected_labs=["east", "west"]
... )
```

Call it from your own test suite, passing `expected_labs=[...]` to also assert
specific labs are present and loadable against your known fixtures:

```python
from otto.testing import assert_lab_repository_conforms
from my_lab_source import CmdbLabRepository

def test_cmdb_conforms():
    assert_lab_repository_conforms(CmdbLabRepository(repo_dir="."))
```

## Troubleshooting

`"Unknown lab repository backend '...'"`
: `[lab] backend` names a backend that was never registered. Check the name, and
  confirm the `init` module that calls `register_lab_repository(...)` is listed
  in `init = [...]`.

`LabNotFoundError: Lab '...' not found`
: The backend has no lab by that name. Check `--lab` / `OTTO_LAB` against
  `list_labs()`.
````

- [ ] **Step 2: Add it to the guide toctree**

In `docs/guide/index.rst`, add `host-database` to the `toctree` immediately after `lab-config`:

```rst
   repo-setup
   lab-config
   host-database
   run
```

- [ ] **Step 3: Lint + run the doctests (per-task check)**

The guides cross-reference each other and the `team-setup-checklist` label (created in Task 3), so the full nitpicky `make docs` (with `-W`) can only pass once every cross-referenced doc exists — that runs at the end (Task 4). For THIS task, validate the two things you own:

Run: `python scripts/lint_markdown_doctests.py docs/`
Expected: passes (no `>>>` outside a `{doctest}` fence).

Run: `make doctest`
Expected: the doctest builder runs all `{doctest}` blocks (no `-W`, so a transient unresolved `{ref}`team-setup-checklist`` xref is only a warning, not a failure); the new host-database blocks pass — confirm the summary shows them passing and 0 failures. Report the pass count.

- [ ] **Step 4: Stage (do NOT commit)**

```bash
git add docs/guide/host-database.md docs/guide/index.rst
```

---

## Task 2: Upgrade the reservations guide

Reconcile `docs/guide/reservations.md` with the shipped code: register-by-name (drop the removed dotted-path), executed sample, multi-holder `who_reserved`, a conformance "verify" section, the `list_usernames` completion capability, and the `-R`-skips-construction behavior. Replace the non-executed `...`-stub skeleton.

**Files:**
- Modify: `docs/guide/reservations.md`

- [ ] **Step 1: Add the team-setup framing near the top**

After the intro paragraph that ends "...edits, or releases a reservation — the external scheduler remains authoritative." (the paragraph at lines ~10-12), insert:

```markdown

```{note}
Wiring up reservations is a one-time, team-level decision. See the
{ref}`team-setup-checklist` in {doc}`repo-setup` for the full onboarding map.
```
```

- [ ] **Step 2: Document cached `--as-user` completion (§3d)**

In the "Overriding the default user" section, after the paragraph ending "...you already know who you are." (around line 122), insert a new subsection:

```markdown
### Username tab-completion

If your backend can enumerate its users, otto offers them as `--as-user`
tab-completion values. A backend opts in by implementing the optional
[`SupportsUsernameCompletion`](../api/reservations.rst) capability — a single
`list_usernames() -> list[str]` method. Otto detects it structurally; backends
that can't list users simply omit it and `--as-user` still accepts free-form
input.

The values are cached with the same policy as host ids (otto's completion cache,
invalidated by the settings fingerprint and `--clear-completion-cache`), because
enumerating users can be slow and the list changes rarely. A cold cache yields
no suggestions and refreshes on the next normal run — completion never blocks on
the backend.
```

- [ ] **Step 3: Replace the "Writing a custom backend" body through "Third-party package layout"**

Replace the entire block from `## Writing a custom backend` (line ~213) down to the end of the `### Third-party package layout` section (the line `the factory picks it up by dotted path.  No otto-side code changes\nare needed to add a new backend.` and its closing, ~line 333) with:

````markdown
## Writing a custom backend

When your team already has a scheduler (Jira, a web API, a database), write a
backend that talks to it instead of using the JSON file. A backend implements
the [`ReservationBackend`](../api/reservations.rst) Protocol — three read-only
methods (`get_reserved_resources`, `who_reserved`, `backend_name`). Otto never
calls a write method; the scheduler stays authoritative.

Otto ships a small, dependency-free reference implementation —
[`otto.examples.reservations.ExampleReservationBackend`](../api/examples.rst) —
that you can copy from `src/otto/examples/reservations.py` as a starting point.
It demonstrates a multi-holder `who_reserved`, a stable `backend_name`, and the
optional `list_usernames` completion capability:

```{doctest}
>>> from otto.examples.reservations import ExampleReservationBackend
>>> backend = ExampleReservationBackend()
>>> backend.backend_name()
'example'
>>> sorted(backend.get_reserved_resources("alice"))
['lab-a', 'shared']
>>> backend.who_reserved("shared")
['alice', 'bob']
>>> backend.list_usernames()
['alice', 'bob']
```

### Selecting it in settings

Register the backend under a bare name from an `init` module (one of the modules
in `init = [...]`), then select it by that name:

```python
# my_team_backend.py  (listed in init = [...])
from otto.reservations import register_reservation_backend
from my_company.jira_backend import MyTeamBackend

register_reservation_backend("my-team-jira", MyTeamBackend)
```

```toml
[reservations]
backend = "my-team-jira"
url = "https://jira.example.com"

[reservations.my-team-jira]
api_key_env = "JIRA_API_KEY"
```

Otto constructs the backend as
`MyTeamBackend(url="https://jira.example.com", api_key_env="JIRA_API_KEY")` —
the `[reservations.<name>]` sub-table becomes keyword arguments, and `url` is
passed when present. Selecting an unregistered name raises an error listing the
registered backends. This is the same named-registry mechanism otto uses for
host sources, term/transfer backends, and host classes; an `init` module always
imports before the reservation check runs, so the name is registered in time.

### Verify your backend

Otto ships a conformance helper that checks a backend against the full contract
and reports every violation at once (a single `AssertionError` listing each
failed rule). The shipped sample conforms:

```{doctest}
>>> from otto.testing import assert_reservation_backend_conforms
>>> from otto.examples.reservations import ExampleReservationBackend
>>> assert_reservation_backend_conforms(
...     ExampleReservationBackend(),
...     known_user="alice",
...     known_resources=["lab-a", "shared"],
... )
```

Call it from your own suite. Passing `known_user` / `known_resources` (resources
that user is known to hold) enables the round-trip consistency rules against your
own fixtures:

```python
from otto.testing import assert_reservation_backend_conforms
from my_team_backend import MyTeamBackend

def test_my_backend_conforms():
    assert_reservation_backend_conforms(
        MyTeamBackend(url="https://jira.example.com"),
        known_user="alice",
        known_resources=["rack3-psu"],
    )
```

### Contract rules for implementers

- **Never mutate.** Otto only reads from the scheduler. Writes, releases,
  extensions — all stay in the scheduler's own UI/API.
- **Return the user's full reserved set** from `get_reserved_resources`. Don't
  pre-filter against what otto "might need" — otto does that filtering itself,
  and doing it twice loses information for the error message.
- **`who_reserved` returns a `list[str]`.** Return every username currently
  holding the resource, in a deterministic order with duplicates removed. An
  **empty list** means no one holds it — there is no `None` sentinel, and a
  resource can have any number of concurrent holders.
- **Raise [`ReservationBackendError`](../api/reservations.rst)** for *every*
  failure mode that prevents a definitive answer: network errors, timeouts,
  credential failures, malformed responses, missing data files. Do not swallow,
  do not return empty. The CLI surfaces this specific exception as a fail-closed
  startup error with an `-R` hint — swallowing it means otto proceeds as if the
  user holds nothing, the opposite of fail-closed.
- **String-match byte-for-byte.** The strings you return must match
  `UnixHost.resources` and `Lab.resources` exactly. Normalize inside your
  backend, not in otto.
- **`backend_name()` should be stable.** It shows up in diagnostics and skip
  warnings; changing it between versions breaks log-history searches.
- **`url` is optional on both sides.** Accept `url: str | None = None` and use
  it, or hardcode your endpoint and omit it — otto passes `url=` only when the
  setting is present.
- **Optionally implement `list_usernames()`** to power cached `--as-user`
  completion (see [Username tab-completion](#username-tab-completion)).
````

- [ ] **Step 4: Reconcile the fail-closed section with `-R`-skips-construction**

Replace the `## Fail-closed behavior` section (lines ~335-343) with:

```markdown
## Fail-closed behavior

If backend construction raises (scheduler unreachable, bad credentials), otto
exits before running the requested command — and the error message *does* mention
`-R`, because the user otherwise has no way to proceed.

Passing `-R` / `--skip-reservation-check` goes further: otto does **not construct
the backend at all**. A scheduler that fails or even hangs in its constructor can
never block lab access — that is the strongest form of break-glass. (The
introspection subcommands `otto reservation whoami` / `check` still build the
backend on demand when you ask them to.)

All other failures (the user genuinely doesn't hold the resource) exit via the
normal `MissingReservationError` path, which does not mention `-R`.
```

- [ ] **Step 5: Fix the troubleshooting entries**

In the `## Troubleshooting` section, **delete** the entry that begins
`"Could not import reservation backend module ..."` (dotted-path resolution was
removed) and its definition, and **replace** it with:

```markdown
`"Unknown reservation backend '...'"`
: `[reservations] backend` names a backend that was never registered. Check the
  name, and confirm the `init` module that calls
  `register_reservation_backend(...)` is listed in `init = [...]`.
```

- [ ] **Step 6: Lint + run the doctests (per-task check)**

The full nitpicky `make docs` runs at the end (Task 4). For this task:

Run: `python scripts/lint_markdown_doctests.py docs/`
Expected: passes (the old `...`-stub is gone; no `>>>` appears outside a `{doctest}` fence).

Run: `make doctest`
Expected: the two new reservations `{doctest}` blocks pass; 0 failures. Report the pass count.

Run: `grep -nE "pkg\.mod:|importlib|dotted|:ClassName|backend = \"my_team_backend:" docs/guide/reservations.md`
Expected: no matches (no dotted-path selection narrative survives).

- [ ] **Step 7: Stage (do NOT commit)**

```bash
git add docs/guide/reservations.md
```

---

## Task 3: Repo-setup becomes the team-setup hub

Add the `[lab]` and `[reservations]` field-reference entries, annotate the lab-loading startup step, and add the labeled **Team setup checklist** that the other guides link to.

**Files:**
- Modify: `docs/guide/repo-setup.md`

- [ ] **Step 1: Add `[lab]` and `[reservations]` to the field reference**

In `docs/guide/repo-setup.md`, in the `### Field reference` deflist, immediately after the `labs` entry (the one ending "...Defaults to `[]`."), insert:

```markdown
\[lab\]
: Optional table selecting the **host-source backend** — where otto's hosts come
  from. `backend` names a registered source (defaults to `"json"`, which reads
  `hosts.json` from the `labs` directories); a `[lab.<name>]` sub-table holds
  that backend's keyword arguments. See {doc}`host-database` for the full
  treatment.
```

And immediately after the `\[os_profiles\]` entry (end of the field reference), insert:

```markdown
\[reservations\]
: Optional table enabling the **reservation gate** — otto refuses to start
  live-lab commands against resources the current user doesn't hold. `backend`
  names a registered scheduler source (`"none"` — the default — disables the
  gate; `"json"` reads a reservation file). See {doc}`reservations` for backends,
  the file format, and the `--as-user` / `-R` break-glass overrides.
```

- [ ] **Step 2: Annotate the lab-loading startup step**

In the `## What happens at startup` numbered list, replace step 4 ("**Lab loading** -- Otto collects all `labs` search paths...") with:

```markdown
4. **Lab loading** -- Otto builds the host source via `build_lab_repository`
   (selected by `[lab] backend`, defaulting to the built-in `json` source over
   the merged `labs` search paths) and loads the lab(s) named by `--lab` or
   `OTTO_LAB`. Multiple labs are merged, combining their hosts. The host source
   is pluggable — see {doc}`host-database`.
```

- [ ] **Step 3: Add the Team setup checklist**

At the end of `docs/guide/repo-setup.md` (after the `## Lab files` section), append:

```markdown
(team-setup-checklist)=
## Team setup checklist

Most of otto's configuration is a **one-time, team-level** decision. New
contributors then just clone and run. Work through this map once when adopting
otto for a team:

1. **Create `.otto/settings.toml`** — `name`, `version`, and the `labs` / `libs`
   / `tests` / `init` paths (this page, above).
2. **Choose a host source** — the built-in `json` source (commit `hosts.json`
   under a `labs` directory) is the default; point `[lab] backend` at a CMDB or
   inventory API if you have one. See {doc}`host-database`.
3. **Decide on reservation gating** — leave it off (`backend = "none"`, the
   default) for sandbox labs, or wire `[reservations]` to your scheduler so otto
   refuses to clobber a held rack. Tell the team about the `--as-user` and
   `-R` / `--skip-reservation-check` break-glass overrides *before* they need
   them. See {doc}`reservations`.
4. **Register shared code** — put instruction/option modules under `libs` and
   list them in `init`; auto-import test suites from `tests`. See {doc}`run` and
   {doc}`test`.
5. **Set per-product preferences** — optional `[host_preferences]` /
   `[os_profiles]` (this page, above, and {doc}`lab-config` / {doc}`os-profiles`).
6. **Enable tab completion** — see {doc}`../getting-started`.

Each backend choice is verifiable: otto ships conformance helpers
(`otto.testing.assert_lab_repository_conforms` /
`assert_reservation_backend_conforms`) so a custom host source or reservation
backend can be checked against otto's contract in your own test suite.
```

- [ ] **Step 4: Lint + run the doctests (per-task check)**

The full nitpicky `make docs` runs at the end (Task 4). For this task:

Run: `python scripts/lint_markdown_doctests.py docs/`
Expected: passes (this task adds no `>>>` examples).

Run: `make doctest`
Expected: 0 doctest failures (this task adds no `{doctest}` blocks; it must not break existing ones). Report the pass count.

- [ ] **Step 5: Stage (do NOT commit)**

```bash
git add docs/guide/repo-setup.md
```

---

## Task 4: Inward links + API-reference completeness + full gate

Point the entry-point pages at the team-setup hub, confirm the new public surfaces are documented nitpicky-clean, and run the full bed-free gate.

**Files:**
- Modify: `docs/getting-started.md`, `docs/overview.md`

- [ ] **Step 1: Link getting-started at the team-setup hub**

In `docs/getting-started.md`, at the end of the `## Project setup` section (after the `### settings.toml` content, before the next top-level `##` heading), insert:

```markdown
```{tip}
Setting otto up for a *team* is a one-time exercise — host source, reservation
gating, shared libs, tab completion. The {ref}`team-setup-checklist` in
{doc}`guide/repo-setup` walks through it.
```
```

And in the `## Where to go next` list, add a first bullet:

```markdown
- {ref}`team-setup-checklist` -- One-time setup when adopting otto for a team
```

- [ ] **Step 2: Link overview at the team-setup hub**

In `docs/overview.md`, in the `## Where to go next` list, add a bullet after the `{doc}`getting-started`` one:

```markdown
- {ref}`team-setup-checklist` — One-time team setup (host source, reservations, libs)
```

- [ ] **Step 3: Confirm API-reference completeness**

The new public surfaces are documented by Plans A/C — verify nitpicky-clean coverage exists (no edits expected; if a target is genuinely missing, add the minimal `automodule`/`autofunction`, mirroring the existing entries):

Run: `grep -RnE "otto\.testing|otto\.examples|build_lab_repository|register_lab_repository|LabNotFoundError|LabRepository|SupportsUsernameCompletion|register_reservation_backend" docs/api/`
Expected: matches in `docs/api/testing.rst`, `docs/api/examples.rst`, `docs/api/storage.rst`, `docs/api/reservations.rst`.

- [ ] **Step 4: Full bed-free gate**

Run: `make docs`
Expected: **0 warnings**; all `{doctest}` blocks pass (Sphinx `doctest`); `docs-lint` clean; `doctest-src` unchanged.

Run: `make typecheck`
Expected: clean (no Python changed, so unchanged — a sanity check).

Run: `make coverage-unit`
Expected: unchanged from Plan C (this plan adds no source or test code) — green, coverage ≥85%. (Bed-free. Do NOT run `make coverage` / `make nox`.)

- [ ] **Step 5: Stage (do NOT commit)**

```bash
git add docs/getting-started.md docs/overview.md
```

- [ ] **Step 6: Hand off**

Report the staged file list and gate results to the controller for the final whole-branch review. Do **not** commit — Chris commits (and will want to commit Plan C first, or together with Plan D, since both are staged on the same index).

---

## Self-Review (controller, before dispatching Task 1)

1. **Spec coverage (§6):**
   - §6.1 new `host-database.md` (interface, json quick-start, register-by-name selection, custom backend via the executed `otto.examples` sample, error contract, conformance "verify", troubleshooting) + index.rst → Task 1. ✅
   - §6.1 reservations.md upgrade (executed sample replacing the `...`-stub; multi-holder `who_reserved`; register-by-name replacing dotted-path; "verify your backend" conformance; `list_usernames` completion; break-glass reconciled) → Task 2. ✅
   - §6.1 "each backend guide opens with a Team-setup-checklist framing" → Task 1 note + Task 2 Step 1. ✅
   - §6.1 API reference complete/nitpicky-clean → Task 4 Step 3 (verify; already shipped by A/C). ✅
   - §6.2 repo-setup hub (`[lab]` + `[reservations]` field reference; startup-step note; Team setup checklist) → Task 3. ✅
   - §6.3 inward links from getting-started + overview → Task 4 Steps 1-2. ✅
   - §7 guide `{doctest}` examples run under `make doctest`; lint guard satisfied → every task's `make docs` step. ✅
2. **Doctest correctness:** every `>>>` example is inside a `{doctest}` fence (lint guard); outputs match the deterministic, already-verified sample behavior fixed in Global Constraints; no registry-mutating call appears in a runnable block. ✅
3. **No rot-prone stubs:** the removed reservations `...`-stub is replaced by an executed example against the shipped, tested sample; the new host-database guide introduces no non-executed `...` skeleton. Custom-backend "how to" points at the real `src/otto/examples/*` source rather than inlining an untested copy. ✅
4. **Cross-refs:** `(team-setup-checklist)=` label defined in Task 3; referenced from Tasks 1/2/4 — the controller should expect a transient unresolved-ref warning if a referencing task's `make docs` runs before Task 3, and confirm the full gate is clean after Task 3 (Task 4 re-runs the whole `make docs`). ✅
5. **Factual accuracy:** register-by-name (no dotted-path), `who_reserved -> list[str]`, `-R`-skips-construction, `[lab]` default `json`, custom construction `cls(repo_dir=, **kwargs)` — all match committed/staged code per Global Constraints. ✅
6. **Bed-free:** no task runs any lab/bed/Vagrant target; gate is `make docs` (+ typecheck + coverage-unit no-regression). ✅
