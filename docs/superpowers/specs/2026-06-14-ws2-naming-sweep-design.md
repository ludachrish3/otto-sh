# WS#2 — Naming Sweep: Design

> Captured 2026-06-14. Workstream #2 of the fable-review sequencing
> ([docs/superpowers/specs/2026-06-13-fable-review-sequencing-design.md](2026-06-13-fable-review-sequencing-design.md),
> item #2). Pairs with the findings/decisions in
> [todo/fable_review_outcome.md](../../../todo/fable_review_outcome.md) and the
> kickoff handoff in [todo/ws2_naming_sweep_kickoff.md](../../../todo/ws2_naming_sweep_kickoff.md).
> This is the brainstorm output — the spec — not the implementation plan; the
> step-by-step plan follows in a writing-plans cycle.

---

## Goal

Normalize otto's Python API and on-disk data schema to PEP 8 `snake_case` **before
the contract freeze**, while the rename is still free. Every renamed symbol and
JSON field is frozen-contract surface; renaming after the freeze is a breaking
change, and otto has zero users + an explicit no-backcompat policy, so now is the
only cheap moment.

The sweep lands *after* WS#1 (context object + lifecycle, merged) so it mops up the
reworked entry point, and *before* Pydantic Phase A so those models are authored in
their final names.

Three deliverables, independent of each other:

1. The `typer<0.26` version ceiling (a carried-over WS#1 hygiene step, never done).
2. The snake_case + vocabulary rename itself.
3. Scoped ruff naming-lint rules that lock the rename in and prevent regression.

## Non-goals

- **No Pydantic.** Phase A (the next workstream) replaces the hand-rolled
  JSON→Lab/Host conversion in `storage/factory.py` with Pydantic models. The rename
  leaves that path's field names already final, so Phase A drops in with no second
  rename. Pulling even a hosts-only Pydantic slice forward would invert the reason
  the rename goes first and would author the `Host` model twice (Phase A's
  `multi_interface_hosts` item changes the meaning of the `ip` field).
- **No registry-API opening, no `transfer.py` split.** Those are WS#4 / post-freeze.
- **No re-renaming WS#1's surface.** The context/config-access layer
  (`load_lab`/`get_lab`, `OttoContext` accessors, fleet `all_hosts`/`get_host`/
  `do_for_all_hosts`/`run_on_all_hosts`) is already snake_case — leave it.
- **No alias shims, no `@deprecated` forwarders.** Clean rename; the reader does not
  accept old names or keys.

---

## Locked decisions

| Decision | Resolution |
|----------|-----------|
| **Sweep depth** | **API + fields.** Rename module-level functions, class/dataclass attributes, JSON/lab field keys, the 5 host filenames, and public method keyword-args — the contract surface plus the split-brain cross-module helpers. Tests are swept in lockstep. |
| **Pure-local variables** | **Deferred.** Loop vars / temporaries (`errorStr`, `varName`, `formattedLines`, …) are non-breaking and left for when the lint ratchet extends to `N806`. |
| **Telecom vocabulary** | **Reversed → `element` / `element_id`.** ⚠ This overrides the roadmap's locked decision #1 sub-point ("keep `ne`/`ne_id`, don't English-ify to node/element"). Per Chris's 2026-06-14 instruction this is a **deliberate reversal**, not an oversight — a future session must not "restore" `ne` from the roadmap. |
| **JSON migration** | **Fixtures only.** Repo fixtures (`tests/lab_data/tech1`+`tech2`) and doc examples are updated in place; the reader does not accept old keys. Chris hand-edits his own real lab files (two changed keys). No migration tooling. |
| **Naming lint** | **Enable scoped now.** Turn on ruff `N802` (functions), `N803` (arguments), `N815` (mixedCase class/instance attrs), `N816` (mixedCase globals). Leave `N806` (local vars) **off** so the deferred locals don't block. The lint flip is the static completeness check for the sweep. |
| **Pydantic** | **Deferred to Phase A** (next workstream). |

---

## Scope

### In

- **Python API surface:** module-level functions, class & dataclass attributes,
  public method keyword-arguments. Representative cross-module helpers found in the
  survey: `getOttoLogger`/`initOttoLogger`, `getRepos`, `applyRepoSettings`,
  `addHost`, `getEnvVar`, `getVersion`, `getCompletionNames`, `isDryRun`,
  `Repo.addLibsToPythonpath`, `removeOldLogs`, `splitOnCommas`, transfer kwargs
  `destDir`/`srcFiles`/`mustExist`. (The full set is enumerated from the code during
  planning — ~102 distinct camelCase identifiers in `src/`, ~73 in `tests/`.)
- **Host module filenames (import-affecting):** `dockerHost.py` → `docker_host.py`,
  `embeddedHost.py` → `embedded_host.py`, `localHost.py` → `local_host.py`,
  `remoteHost.py` → `remote_host.py`, `unixHost.py` → `unix_host.py`. Update every
  import site, `__all__`, the host-class string→class registry, any `importlib`
  references, and docs.
- **JSON / lab-data + coupled dataclass fields:** `osType`→`os_type`,
  `osName`→`os_name`, `osVersion`→`os_version`, plus the vocabulary change
  `ne`→`element` (the JSON key in every host entry **and** the `.ne` attribute) and
  `neId`→`element_id` / `neStr`→`element_str`. The complete field set is read from
  the host dataclass(es) during planning; most lab-JSON keys are already snake_case
  (`is_virtual`, `docker_capable`, `max_filename_len`, `telnet_options`, …).
- **Tests and docs:** swept in lockstep with the API/fields they reference.

### Out

- Pure-local variables and temporaries (deferred to the `N806` ratchet).
- WS#1's already-snake_cased context/config-access/fleet layer.
- Pydantic, registry-API opening, `transfer.py` split (later workstreams).
- `CHANGELOG.md`, the `pyproject.toml` version field, and `uv.lock` (release-managed;
  not WS#2 surface).

---

## Architecture / data flow

The sweep is mechanical everywhere except one seam: **`src/otto/storage/factory.py`**,
the JSON-key → host-construction path. Today a JSON key is read straight onto the
attribute of the same name (`host_data.get('osType', 'unix')` → `kwargs['osType'] =
selector`), so the JSON key and the Python attribute are **coupled** — renaming one
without the other breaks parsing silently. Both ends are therefore renamed in the
same change, and the lab-JSON fixtures are updated together so the parse path stays
green. The host-class string registry and any `importlib`/dynamic references are
verified when the filenames move.

## Working order (single branch, squash-merged)

All work lands on the `ws2-naming-sweep` branch; Chris squash-merges. The logical
order below is for reviewability/bisection within the branch, not separate PRs:

1. **`typer<0.26` ceiling** — `pyproject.toml` `"typer>=0.24.0"` →
   `"typer>=0.24.0,<0.26"`, then `uv lock`; confirm resolved `typer` stays `<0.26`
   and Dependabot no longer re-proposes the 0.26 bump (PR #47). Independent of the
   rename; recorded in code as the deferral of the Typer triage to Pydantic Phase B
   (remove the ceiling when Phase B lands). The live pin is `"typer>=0.24.0"` (no
   ceiling) as of `554ee69`.
2. **Host filenames** — `git mv` the 5 files + update imports / `__all__` / registry
   / `importlib` / docs. Bisectable in isolation.
3. **JSON/lab fields + coupled attributes** — `os_type`/`os_name`/`os_version`,
   `element`/`element_id`/`element_str`, fixtures, and doc schema tables, with the
   `factory.py` parse path changed atomically.
4. **Cross-module API helpers** — the remaining split-brain identifiers.
5. **Scoped ruff naming lint** — add `N802`/`N803`/`N815`/`N816` to `[lint] select`
   in `.ruff.toml` (currently `["E501", "I", "D"]`; `N806` stays off), fix any
   stragglers the lint flags. A clean lint run *proves* the sweep is complete.

## Verification

- **No-VM unit alone is insufficient.** A broad rename can break the
  integration/embedded paths and on-disk lab parsing. Run the VM tiers and at least
  one **combined** `make coverage` (single-process) — the WS#1 leak only surfaced in
  the combined run, never in the separated tiers.
- **Static completeness:** the ruff naming-lint flip (step 5) passing with the four
  scoped rules on.
- Existing coverage floor (90%) and the full test suite must stay green.

## Integration / deliverable

- **Branch off latest main; rebase onto current main right before merge.** Main gets
  force-pushed under Dependabot bumps + releases (the v0.4.2 release just landed as
  `554ee69`), so SHAs churn; a stale base throws spurious 3-way-merge conflicts.
- **No self-commit.** The `prepare-commit-msg` hook needs `/dev/tty`; agent commits
  mis-tag the AI-assist trailer. Work lands on the branch for Chris to squash-merge.
- **Deliverable:** the branch + **one paste-able squash-commit message** covering the
  whole sweep (Chris authors the squash commit so the trailer is correct).

## Sequencing context

WS#2 → **Pydantic Phase A** (boundary models + the scope-resolving spike, authored in
these final names) → Registry public API → **FREEZE**. Then: `transfer.py` split,
test-tree restructure, Pydantic Phase B (+ the Typer 0.26 triage, which retires the
`typer<0.26` ceiling).
