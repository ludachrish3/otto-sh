# Multi-project environments: the orchestration venv and the dependency preflight

**Status:** approved design, not yet planned.
**Companion:** `2026-08-23-project-activation-design.md` (defines the
`active()` predicate this spec's preflight severity keys on; lands first).

## Motivation and provenance

Measured on 2026-08-23, answering "if repo B's libraries import paramiko and
the main repo A does not depend on it, how does otto resolve it?":

- It doesn't. Cross-project imports are `Repo.add_libs_to_pythonpath()` — a
  literal `sys.path.append` per `libs` entry into **whatever interpreter otto
  is running from** (`src/otto/bootstrap.py:271`). otto never reads a SUT
  repo's `pyproject.toml` dependencies; the only pyproject read anywhere is
  pytest markers (`src/otto/config/repo.py:700`).
- Failure shape depends on import time: an eager import (an `init` module)
  becomes a contained per-repo `BootstrapError` framed as a raw traceback; a
  lazy one (a `libs` module first imported by a suite or instruction) is an
  ImportError **mid-run**.
- It "works" today only when operators hand-install B's deps into A's venv —
  exactly the manual glue the multi-project goal forbids.
- Nothing tests any of this: no sample repo has a `pyproject.toml`, none
  imports a third-party package beyond otto's own dependency set, and no test
  declares `[dependencies]` between sample repos.

## Decision record (2026-08-23)

| decision | choice | rejected |
| --- | --- | --- |
| Source of truth for a repo's Python deps | **Its own `pyproject.toml`.** Rationale (user, verbatim intent): every repo is expected to manage its Python deps via pyproject and its own venv, whether created by uv or pip+venv. | A new settings.toml package list (hand-kept duplicate; drifts). Settings-with-pyproject-fallback (two sources, unattributable errors). |
| What otto does about missing deps at run time | **Verify and refuse, never install.** Preflight is metadata-only. | Installing at bootstrap (otto mutating the environment mid-command, network at bootstrap, resolver ownership). |
| Where multi-project runs live | **A per-user orchestration venv keyed by the normalized `OTTO_SUT_DIRS` set**, under the user data dir. | One venv per user (workspaces fight over pins; resolver errors name packages, not workspaces). User-named envs (name registry, "which env am I in"). Running from one repo's venv (privileges that repo for no principled reason). |
| Environment commands | **`otto env create`** (explicit on-ramp, refuses if exists, `--force` recreates) and **`otto env sync`** (incremental, creates-if-missing, always safe — the verb every error message names). Plus `otto env show`. | One do-everything verb (create/sync semantics collide); sync-only (no clean recreate story for a wedged env). |
| Backend | **uv when present, stdlib `venv` + pip fallback** — auto-detected; explicit override via `--backend uv|pip` and a settings key. Stdlib `venv`, not the `virtualenv` package: the fallback path adds no dependency. | uv-only (users without uv exist, per the user). |
| otto in the env | `env create`/`sync` install otto itself (the running version) into the env and print the activation line. | Leaving otto outside (a pipx-global otto would import against the wrong site-packages — the venv would be decoration). |
| User-level home | **`~/.otto/`** (2026-08-23 follow-up ruling): otto's user-level home — one `ls`-discoverable place, one contract ("everything under it is rebuildable"), deliberately rhyming with the per-repo `.otto/` dirs. `OTTO_HOME` relocates it wholesale (containers/CI). | The XDG split (`~/.local/share/otto/envs`): lifecycle-correct but three trees to document, and a re-derivable venv is cache-lifecycle anyway. NOT moved here: `~/.cache/otto/busybox` — that is otto's own test-infra artifact cache (`tests/_fixtures/busybox.py`; nothing in `src/otto` reads it), a develop-otto concern with no place in the user-facing home. |
| Home structure | **Workspace-keyed at the top** (second 2026-08-23 follow-up ruling): `~/.otto/<hash8>-<slug>/` is a *workspace home*, holding the venv at `env/` (singular — the per-env keying collapses into the directory above) AND the completion caches, which move out of `$OTTO_XDIR/.otto/`. One answer to "where do my otto caches live", and per-workspace reset is one `rm -rf`. | Envs keyed under a flat `envs/` with caches left xdir-scattered (N xdirs × one workspace = N byte-identical caches — the wart this fixes). |

## 1. The model (this section is docs-bearing)

Three environments exist; the docs must name all three:

1. **A repo's own venv** — for single-repo development. Each repo manages it
   with its own tools from its own pyproject. otto does not touch it.
2. **The orchestration venv** — where multi-project runs happen. One per
   (user × workspace), where a workspace is the normalized `OTTO_SUT_DIRS`
   set. It must be a **superset**: it satisfies the glue imports of every
   active repo, because otto is one process on one interpreter and per-repo
   venvs never participate at runtime. `otto env create` builds it;
   `otto env sync` keeps it current; the preflight (§3) verifies it.
3. **Anything else otto happens to run from** — legal for single-repo work,
   discouraged for multi-project work; the preflight names the escape.

The docs also name the uv-workspace alternative (one lockfile across member
projects) as the power-user equivalent that needs no otto involvement.

## 2. `otto env` command group

### Keying and location

Workspace key = the PEP-503-style slug of the sorted, absolute,
symlink-resolved SUT dirs, plus a short hash of the same. An empty set (no
`OTTO_SUT_DIRS` — the bare-lab-directory case) is legal and keys the same
way: the hash of the empty list with the slug `no-repos`, so lab-only users
get one stable workspace home rather than an error. Each workspace owns one
directory under otto's user-level home (`OTTO_HOME`, default `~/.otto`):

```
~/.otto/
  <hash8>-<slug>/               # the WORKSPACE HOME
    env/                        # the orchestration venv
    completion_cache.json       # moved from $OTTO_XDIR/.otto/
    remote_completion_cache.json
  tls/                          # user-level, workspace-independent
```

This gives the `.otto` name a three-line taxonomy the docs state verbatim:
a repo's `.otto/` holds *source config* (settings, coverage overrides —
unchanged); the xdir holds *run outputs* (unchanged); the workspace home
holds *everything otto derived and can rebuild*.

### Completion caches move to the workspace home

Their content was already a pure function of the workspace:
`compute_fingerprint(repos)` hashes each repo's `settings.toml` and
init-module files and nothing else — the xdir was a storage location, never
a semantic key. Relocation is therefore the path function only
(`completion_cache.py`'s cache-path helper and the remote cache beside it);
fingerprint semantics, TTLs and invalidation are untouched. It also
*deduplicates*: today, invoking otto from N directories against the same
repos maintains N identical caches. No migration code: stale
`$OTTO_XDIR/.otto/` cache files have no remaining reader and may be deleted
by hand; the docs say so where the old location was documented
(`docs/guide/cli/index.md`).

`otto env show` prints: the workspace home path, backend recorded at
creation, otto version installed, per-repo install state, and whether any
repo's pyproject/dist-metadata is newer than the last sync (staleness).

### `otto env create [--force] [--backend uv|pip]`

Fresh build: create the venv, install each discovered repo **editable**
(`pip install -e` / `uv pip install -e`) so live checkouts stay live, install
otto at the running version, print the activation line and the path to the
env's `otto`. Exists already → error naming `--force` (which removes and
rebuilds — the recovery story for a wedged env). Repos without a
`pyproject.toml` are skipped with a notice: they are not installable, their
`libs` ride `sys.path` at bootstrap exactly as today, and that remains
correct.

### `otto env sync [--backend uv|pip]`

Incremental: bring the workspace env up to date (re-run the editable
installs; the backend resolves what changed). Env missing → behaves as
`create`. This is the verb the preflight refusal names, because it can never
destroy anything.

### Shared mechanics

- Backend auto-detect: `uv` on PATH → uv; else stdlib `venv` + the env's own
  pip. Override order: flag > settings key (`[env] backend = "pip"`) >
  auto-detect. The chosen backend is recorded in the env and re-used by
  `sync`; switching backends on an existing env is a `create --force` matter.
- Resolver conflicts (A pins `paramiko<3`, B needs `>=3`) fail inside uv/pip
  with the resolver's own message; otto relays it verbatim and adds one line
  naming the two repos whose installs collided. otto never resolves.
- Arguments after a literal `--` pass through to the underlying installer
  verbatim (`otto env sync -- --no-index --find-links ../wheels`): hermetic
  test installs and corporate index pins need to reach the resolver, and
  re-encoding installer surface as otto options would chase two tools'
  flag sets forever.
- Both commands are lab-free (no `-l` required) and act on the **discovered**
  repo set, not the active set: an environment is workspace-scoped, and
  building it must not depend on which labs today's command happens to load.

## 3. The bootstrap preflight

Runs inside `bootstrap()` after discovery, metadata-only, importing nothing:

1. For each discovered repo with a `pyproject.toml`, obtain its requirements:
   **installed-dist metadata first** — look up the pyproject `[project].name`
   via `importlib.metadata`; if found (the repo is installed in this env,
   editable or not), read `Requires-Dist`. This is exact and works for
   `dynamic = ["dependencies"]`. Not installed → parse the pyproject file's
   `[project.dependencies]`. Dynamic and not installed → one WARNING
   (`cannot preflight repo2: dependencies are dynamic and repo2 is not
   installed in this environment`) and no check.
2. Evaluate each requirement against the running environment with
   `packaging`: markers against the live interpreter, specifier against
   `importlib.metadata.version`. **Base dependencies only** (no extras),
   **direct dependencies only** (transitive consistency is the installer's
   promise).
3. Verdicts: every requirement satisfied → silence. Unsatisfied on an
   **active** repo (companion spec's predicate) → refusal, exit 1, before any
   host is contacted. Unsatisfied on an **inactive** repo → one WARNING line.
   Repos with no pyproject → vacuously satisfied (today's behaviour,
   unchanged — the samples keep working unmodified).

The refusal names everything the operator needs:

```
error: repo 'repo2' requires 'paramiko >= 3' — not satisfied in this
environment (found: none)
  fix: otto env sync
  or:  uv pip install 'paramiko >= 3'
```

`found: none` distinguishes absent from too-old (`found: 2.12`); the second
line always names `env sync`; the third names the direct install for
operators managing their env by hand.

Performance: the check is `importlib.metadata` lookups per direct requirement
of each repo — no network, no imports. If measured cost on `otto --help`
paths matters, the completion cache's settings fingerprint already provides
the invalidation seam to memoize behind; that optimization is not part of
this spec's contract.

## 4. Sample-repo and test additions

A new sample repo, **repo4**, kept OUT of every default fixture (existing
repo-count assertions stay untouched; tests opt in explicitly):

- `pyproject.toml` with `[project] name = "otto-sample-repo4"` and
  `dependencies = ["beetroot >= 0.1"]` — `beetroot` being a **local fixture
  wheel** built once under `tests/_fixtures/wheels/`, never on PyPI-resolved
  paths (`--no-index --find-links` at install time). Not a real package otto
  could accidentally satisfy.
- `[dependencies] required = ["repo1"]`, `optional = ["repo3"]` — the first
  sample use of inter-repo dependencies (currently unit-tested against fakes
  only).
- A `libs` module that imports `beetroot` at module level, and an instruction
  that calls it — covering both the eager and lazy failure shapes.

Tests:

- **Preflight unit (hostless):** fabricate `*.dist-info` dirs in a tmp site
  dir to stage installed/absent/too-old/marker'd/dynamic cases — no venvs, no
  network, no subprocesses. Both verdict arms per case (satisfied → silent,
  unsatisfied → exact message), so no guard can pass by refusing everything.
- **`env` e2e (hostless, subprocess):** create → env exists, otto and repo4
  installed editable, activation line printed; create again → refusal naming
  `--force`; sync after editing repo4's pyproject → new requirement
  installed (the fixture wheel reaching the resolver via the `--`
  passthrough); both backends parametrized (`--backend pip` forces the fallback
  on a uv-equipped host); resolver-conflict arm pins the two-repo
  attribution line.
- **Workspace-home keying unit tests:** same dirs in different order or via
  symlink → same key; different sets → different keys; the empty set → the
  `no-repos` key. Relocation e2e: a completion-cache write lands in the
  workspace home and the xdir stays clean — the discriminator being the same
  invocation before the change, which wrote under `$OTTO_XDIR/.otto/`.
- **The original scenario, end to end:** repo4 discovered, `beetroot` absent
  → `otto run <repo4-instruction>` refuses with the §3 message; after
  `otto env sync` (wheel via `--find-links`) → the instruction runs. The pair
  is the spec's acceptance test.
- Every new guard mutation-proven before landing, per house rule.

## 5. Documentation ripple

- `docs/installation.md`: a "Multi-project workspaces" section teaching the
  §1 model, `env create`/`sync`, and the uv-workspace alternative.
- `docs/guide/cli/env/` — new verb tree (index + one page per subcommand),
  matching the guide's one-page-per-verb convention.
- `docs/guide/cli/index.md` command table gains `env`.
- The `[dependencies]` docs gain the pyproject relationship: inter-repo deps
  name *repos*; Python deps live in each repo's *pyproject*.
- `~/.otto` documented as the user-level home (`OTTO_HOME` override) with the
  workspace-home taxonomy above, and the
  monitor-TLS convention re-pointed from `~/.config/otto/tls/` to
  `~/.otto/tls/` — docs and the `repo.py` comment only; existing settings name
  explicit paths and keep working unchanged.

## 6. Out of scope

- Extras (`repo4[labjack]`) semantics — no story until something needs one.
- Named or shared orchestration envs.
- otto resolving version conflicts, or any install at bootstrap time.
- Auto-running `env sync` when the preflight fails (the refusal names it;
  running it is the operator's call).
- Windows activation-line variants beyond printing the correct path shape.
