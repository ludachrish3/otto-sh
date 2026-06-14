# WS#2 Naming Sweep Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Normalize otto's Python API, host-module filenames, and on-disk lab-data schema to PEP 8 `snake_case` (with `ne`→`element`) before the contract freeze, and lock it in with scoped ruff naming-lint.

**Architecture:** Five sequential layers on the `ws2-naming-sweep` branch. The work is mechanical but **not** blindly automatable: the camelCase inventory is laced with names otto does not own (stdlib `logging`, `unittest`, config keys, SNMP MIB names, regex fragments). The safety mechanism is ruff's `N802/N803/N815/N816` rules, which flag otto's own *definitions* and never third-party *calls* — so the lint output is the exact rename worklist and auto-excludes the foreign names. The `ne`→`element` change is the one piece lint can't drive (`ne` is already valid snake_case), so it is handled by explicit, thorough grep first. The existing test suite + the lint flip are the regression harness; there are no new behavioral tests.

**Tech Stack:** Python 3.10+, ruff (`.ruff.toml`), pytest, `uv`, `make` targets (`make test`, `make coverage`, `make nox`).

---

## Conventions for this plan

- **No self-commit.** The `prepare-commit-msg` hook needs `/dev/tty`; agent commits mis-tag the AI-assist trailer. Each task ends with a **checkpoint** (run verification, `git add` the staged work) — *not* a `git commit`. Chris squash-merges the branch at the end using the message from Task 5.
- **Do NOT edit point-in-time docs.** Everything under `docs/superpowers/plans/` and `docs/superpowers/specs/` (except this plan and the WS#2 spec) records history as it was — leave old `embeddedHost.py`/`osType` references intact. Only update *live* docs: `docs/api/host/*.rst`, `docs/guide/*.md`, `docs/cookbook/*.md`, `docs/overview.md`, `docs/conf.py`.
- **Class names never change.** `EmbeddedHost`, `RemoteHost`, `UnixHost`, `LocalHost`, `DockerContainerHost`, `ZephyrHost` are PascalCase and correct. Only the lowercase-first *module path token* (`embeddedHost` → `embedded_host`) changes.
- **Lint is non-blocking and the baseline is NOT clean.** On `554ee69`, `ruff check src tests scripts` already reports ~203 known-debt violations under the currently-selected `E501`/`I`/`D` rules: `D209`×121, `I001`×54, `D401`×14, `E501`×9, `D403`×5. That debt is **out of scope** (it's the separate lint-ratchet effort) — do **not** fix it, and don't let the rename *materially* balloon those counts. The WS#2 lint gate is the **naming subset only**: `ruff check src tests --select N802,N803,N815,N816` must reach **0 violations** (it shows **149** at baseline — the Task 3+4 worklist). ruff's default `pep8-naming` `ignore-names` already exempts `setUp`/`tearDown`/`setUpClass`/… so unittest fixtures are not flagged.
- **Verification baseline:** before starting, confirm `make test` passes on `554ee69`. (Do not gate on bare `ruff check` — see above.)

---

## File Structure (what changes, by layer)

- **Task 1** — `pyproject.toml`, `uv.lock` (typer ceiling). *Excluded from this list elsewhere: never touch `CHANGELOG.md` or the version field.*
- **Task 2** — `git mv` 5 files `src/otto/host/{docker,embedded,local,remote,unix}Host.py` → `*_host.py`; 4 test files `tests/unit/host/test_{docker,embedded,local,unix}Host.py` → `test_*_host.py`; all import sites; `docs/api/host/*.rst`, live guide/cookbook/overview docs, `docs/conf.py`.
- **Task 3** — `src/otto/host/{remote,unix,embedded}Host.py` (now `*_host.py`) dataclass fields; `src/otto/storage/factory.py` parse path + docstrings; `tests/lab_data/tech1/hosts.json`, `tests/lab_data/tech2/hosts.json`; all `ne`/`neId`/`osType`/`osName`/`osVersion` reference sites in `src/` + `tests/`; live docs that show schema fields.
- **Task 4** — `.ruff.toml` (`[lint] select`); every otto definition flagged by `N802/N803/N815/N816` across `src/` + `tests/` + its references.
- **Task 5** — verification only; produces the squash-commit message.

---

### Task 1: `typer<0.26` version ceiling

Independent of the rename; the carried-over WS#1 hygiene step. Records the deferral of the Typer 0.26 triage to Pydantic Phase B.

**Files:**
- Modify: `pyproject.toml:57`
- Modify: `uv.lock` (regenerated)

- [ ] **Step 1: Confirm the current pin**

Run: `grep -n 'typer' pyproject.toml`
Expected: line 57 reads `    "typer>=0.24.0",`

- [ ] **Step 2: Add the ceiling**

In `pyproject.toml:57`, change:
```toml
    "typer>=0.24.0",
```
to:
```toml
    "typer>=0.24.0,<0.26",
```

- [ ] **Step 3: Re-lock**

Run: `uv lock`
Expected: completes; `uv.lock` updated.

- [ ] **Step 4: Verify the resolved version stayed < 0.26**

Run: `grep -A2 'name = "typer"' uv.lock | grep version`
Expected: a `version = "0.25.x"` line (must be `< 0.26`).

- [ ] **Step 5: Checkpoint**

Run: `make test` (sanity — should be unaffected) → Expected: PASS.
Then: `git add pyproject.toml uv.lock`

---

### Task 2: Host-module + test filename renames

Filenames are not lint-driven, so they get their own pass. PascalCase class names are untouched.

**Renames (modules):**
| old | new |
|-----|-----|
| `src/otto/host/dockerHost.py` | `src/otto/host/docker_host.py` |
| `src/otto/host/embeddedHost.py` | `src/otto/host/embedded_host.py` |
| `src/otto/host/localHost.py` | `src/otto/host/local_host.py` |
| `src/otto/host/remoteHost.py` | `src/otto/host/remote_host.py` |
| `src/otto/host/unixHost.py` | `src/otto/host/unix_host.py` |

**Renames (tests):**
| old | new |
|-----|-----|
| `tests/unit/host/test_dockerHost.py` | `tests/unit/host/test_docker_host.py` |
| `tests/unit/host/test_embeddedHost.py` | `tests/unit/host/test_embedded_host.py` |
| `tests/unit/host/test_localHost.py` | `tests/unit/host/test_local_host.py` |
| `tests/unit/host/test_unixHost.py` | `tests/unit/host/test_unix_host.py` |

(There is no `test_remoteHost.py` — it was previously renamed to `test_unixHost.py`.)

- [ ] **Step 1: `git mv` the 9 files**

```bash
cd /home/vagrant/otto-sh
git mv src/otto/host/dockerHost.py   src/otto/host/docker_host.py
git mv src/otto/host/embeddedHost.py src/otto/host/embedded_host.py
git mv src/otto/host/localHost.py    src/otto/host/local_host.py
git mv src/otto/host/remoteHost.py   src/otto/host/remote_host.py
git mv src/otto/host/unixHost.py     src/otto/host/unix_host.py
git mv tests/unit/host/test_dockerHost.py   tests/unit/host/test_docker_host.py
git mv tests/unit/host/test_embeddedHost.py tests/unit/host/test_embedded_host.py
git mv tests/unit/host/test_localHost.py    tests/unit/host/test_local_host.py
git mv tests/unit/host/test_unixHost.py     tests/unit/host/test_unix_host.py
```

- [ ] **Step 2: Rewrite the module-path token in all live (non-historical) files**

Locate every reference (excludes the historical `docs/superpowers/` trees):
```bash
grep -rEln '(dockerHost|embeddedHost|localHost|remoteHost|unixHost)' src tests docs \
  --include='*.py' --include='*.rst' --include='*.md' \
  | grep -vE '^docs/superpowers/(plans|specs)/'
```
For each file in that list, replace the five tokens with their snake_case forms (`dockerHost`→`docker_host`, etc.). These tokens only ever appear as the module path (`from otto.host.embeddedHost import …`, `.embeddedHost`, `automodule:: otto.host.embeddedHost`, `{class}\`~otto.host.unixHost.UnixHost\``) — the PascalCase class names do not contain the lowercase token, so a token-level replace is safe. Apply with sed per file, e.g.:
```bash
sed -i 's/dockerHost/docker_host/g; s/embeddedHost/embedded_host/g; s/localHost/local_host/g; s/remoteHost/remote_host/g; s/unixHost/unix_host/g' <file>
```

- [ ] **Step 3: Rename the `docs/api/host/*.rst` files to match (optional but consistent)**

The rst stubs are already lowercased (`dockerhost.rst`); only their `automodule::`/title content changed in Step 2. Leave the filenames as-is (Sphinx toctree references them by stem) unless `docs/api/host/index.rst` lists them — check:
```bash
grep -rn 'host/' docs/api/host/index.rst 2>/dev/null || grep -rn 'dockerhost\|unixhost' docs/api
```
If a toctree lists the stems, they already match; no rename needed.

- [ ] **Step 4: Verify no stale module token remains in live code**

Run:
```bash
grep -rEn '(dockerHost|embeddedHost|localHost|unixHost)|remoteHost' src tests \
  --include='*.py' | grep -vE 'Host[A-Za-z]'
```
Expected: empty (no output). Any hit is a missed import.

- [ ] **Step 5: Run the host + storage + cli test slices**

Run: `python -m pytest tests/unit/host tests/unit/storage tests/unit/cli -q`
Expected: PASS (collection succeeds with the renamed files; imports resolve).

- [ ] **Step 6: Full unit run + lint sanity**

Run: `make test` → Expected: PASS.
Run: `ruff check src tests --select N802,N803,N815,N816 | tail -1` → Expected: still `Found 149 errors` (renaming files adds no *naming* violations). Renaming import lines may shift `I001` (baseline 54) slightly — acceptable, do not chase it.

- [ ] **Step 7: Checkpoint**

Run: `git add -A` → then confirm with `git status` that the 9 renames show as `renamed:` and edited import sites are staged.

---

### Task 3: Data-schema rename — `ne`→`element` vocabulary + `os*` fields + JSON

The only coupled layer: a JSON key is read straight onto the attribute of the same name in `factory.py`, so attribute + JSON key + every reference move together. The `ne`→`element` change is semantic (not lint-enforced — `ne` is valid snake_case), so it must be exhaustive.

**Field renames (dataclass attribute AND JSON key, same name on both sides):**
| old | new | kind |
|-----|-----|------|
| `ne` | `element` | dataclass field + JSON key + `ne=` kwarg |
| `neId` | `element_id` | dataclass field + JSON key |
| `osType` | `os_type` | dataclass field + JSON key |
| `osName` | `os_name` | dataclass field + JSON key |
| `osVersion` | `os_version` | dataclass field + JSON key |

**Also rename (class-scope `ne`-vocab attributes/properties, N815-relevant):**
| old | new |
|-----|-----|
| `_neIdStr` (property, `remote_host.py`) | `_element_id_str` |

**Leave deferred (function-body local, N806-off, per scope decision):**
- `neStr` at `src/otto/host/remote_host.py:201` — a local variable. It will reference `self.element` after this task; that is fine. It is renamed later when the lint ratchet extends to `N806`.

#### `ne` → `element` (do this first — it is the substring-dangerous one)

`ne` is a 2-character token; **never** do a bare text replace. Rename only these exact forms:

- [ ] **Step 1: Rename the dataclass field definitions and the `_neIdStr` property**

In `src/otto/host/remote_host.py`:
- line ~77 `ne: str` → `element: str`
- line ~98 `neId: Optional[int]` → `element_id: Optional[int]`
- line ~195 `f"{self.ne}{self._neIdStr}"` → `f"{self.element}{self._element_id_str}"`
- line ~197 `f"{self.ne}{self._neIdStr} {self.board}{self._slotStr}"` → `f"{self.element}{self._element_id_str} {self.board}{self._slotStr}"`
- line ~201 `neStr = f"{self.ne.lower()}{self._neIdStr}"` → `neStr = f"{self.element.lower()}{self._element_id_str}"` (keep the **local** name `neStr`; only the attribute references change)
- line ~206 `f"{neStr}_{self.board.lower()}{self._slotStr}"` → unchanged except it already uses local `neStr`
- line ~211 `if self.neId is None:` → `if self.element_id is None:`
- the `_neIdStr` property definition (search `def _neIdStr` / `_neIdStr`) → rename to `_element_id_str`
- line ~214 `return f"{self.ne}"` → `return f"{self.element}"`

In `src/otto/host/unix_host.py` line ~129: `neId: Optional[int] = field(...)` → `element_id: Optional[int] = field(...)`.
In `src/otto/host/embedded_host.py` line ~122: `neId: Optional[int] = field(...)` → `element_id: Optional[int] = field(...)`.

- [ ] **Step 2: Rename every `ne` / `neId` reference site (attribute, kwarg, JSON key access)**

Enumerate exact forms (word-boundary; the regex below excludes `one`, `done`, `line`, `connection`, etc.):
```bash
grep -rEn "(\.ne\b|\.neId\b|\bne\s*=|'ne'|\"ne\"|'neId'|\"neId\"|\[.ne.\]|\bneId\b)" src tests --include='*.py' \
  | grep -vE '^docs/'
```
For each hit, apply the mapping: `.ne`→`.element`, `.neId`→`.element_id`, `ne=`→`element=`, `'ne'`/`"ne"`→`'element'`/`"element"`, `'neId'`→`'element_id'`, `data["ne"]`→`data["element"]`. Notable sites confirmed during planning: `src/otto/host/options.py:18` (`ne='lab'`), `tests/conftest.py:404,579`, `tests/unit/host/test_hop_integration.py` (many `ne=data["ne"]`), `tests/unit/host/test_unixHost.py` (now `test_unix_host.py`) `assert host.ne ==` and many `ne=` kwargs, `tests/unit/host/test_dockerHost.py` (now `test_docker_host.py`) `ne="fake_ne"` (the **string value** `"fake_ne"` is data, not an identifier — leave the value, change only the kwarg key: `element="fake_ne"`), `tests/unit/storage/test_factory.py:119,126` (`'neId': 1`, `host.neId`).

- [ ] **Step 3: Update the JSON fixtures' `ne` key**

In `tests/lab_data/tech1/hosts.json` and `tests/lab_data/tech2/hosts.json`, rename the `"ne"` key (present in every host object) to `"element"`. Use a key-only edit (do not touch the value):
```bash
sed -i 's/"ne":/"element":/g' tests/lab_data/tech1/hosts.json tests/lab_data/tech2/hosts.json
```
Verify no `osType`/`osVersion` yet (handled next) and that `"element":` now appears:
```bash
grep -c '"element":' tests/lab_data/tech1/hosts.json   # expect: 4
grep -c '"ne":' tests/lab_data/tech1/hosts.json         # expect: 0
```

#### `osType` / `osName` / `osVersion` → `os_type` / `os_name` / `os_version`

These three are mixedCase (lint will also flag the attrs in Task 4 if missed) and collision-safe for word-boundary replace.

- [ ] **Step 4: Rename the field definitions**

In `src/otto/host/remote_host.py`: line ~110 `osType: OsType` → `os_type: OsType`; line ~114 `osName: Optional[str]` → `os_name: Optional[str]`; line ~117 `osVersion: Optional[str]` → `os_version: Optional[str]`.
Check `unix_host.py` / `embedded_host.py` / `os_profile.py` for any `osType`/`osName`/`osVersion` field defs or defaults and rename likewise.

- [ ] **Step 5: Rename all references + the `factory.py` parse path**

```bash
grep -rEln '\b(osType|osName|osVersion)\b' src tests --include='*.py' | grep -vE '^docs/'
```
For each file, replace `osType`→`os_type`, `osName`→`os_name`, `osVersion`→`os_version` (word-boundary). Critical site: `src/otto/storage/factory.py` — `host_data.get('osType', 'unix')` → `host_data.get('os_type', 'unix')` and `kwargs['osType'] = selector` → `kwargs['os_type'] = selector` (lines ~168, ~228, ~300, ~331, ~336), plus the docstrings at ~123–163 that name `osType`/`osName`/`osVersion`/`neId`.

- [ ] **Step 6: Rename the JSON fixtures' `os*` keys**

```bash
sed -i 's/"osType":/"os_type":/g; s/"osName":/"os_name":/g; s/"osVersion":/"os_version":/g' \
  tests/lab_data/tech1/hosts.json tests/lab_data/tech2/hosts.json
grep -cE '"(osType|osName|osVersion)":' tests/lab_data/tech1/hosts.json   # expect: 0
```

- [ ] **Step 7: Update live docs that show schema fields**

```bash
grep -rEln "\b(osType|osName|osVersion|neId)\b|'ne'|\"ne\"" docs --include='*.md' --include='*.rst' \
  | grep -vE '^docs/superpowers/(plans|specs)/'
```
Update each live doc (guide/host.md, guide/os-profiles.md, overview.md, cookbook, hosts.json schema tables) to the new field names. Leave `docs/superpowers/plans|specs/*` untouched.

- [ ] **Step 8: Verify no stale schema token in live code/data/docs**

```bash
grep -rEn "\b(osType|osName|osVersion|neId)\b" src tests --include='*.py'   # expect: empty
grep -rEn '"(ne|osType|osName|osVersion)":' tests/lab_data                   # expect: empty
```
Expected: both empty.

- [ ] **Step 9: Run the data-path tests**

Run: `python -m pytest tests/unit/storage tests/unit/host tests/unit/configmodule tests/conftest.py -q`
Expected: PASS (lab JSON parses; `factory.py` builds hosts with new field names).
Run: `make test`
Expected: PASS.

- [ ] **Step 10: Checkpoint**

`git add -A`

---

### Task 4: Enable scoped naming-lint and fix every flagged definition

Now the lint becomes the worklist. After Tasks 2–3, `osType`/`neId` are already done, so they won't reappear. Turning on `N802/N803/N815/N816` lists every *remaining* otto-owned mixedCase definition (functions, args, class/instance attrs, module globals) and excludes all stdlib/unittest/config/SNMP names automatically.

**Files:**
- Modify: `.ruff.toml` (`[lint] select`)
- Modify: every file ruff flags + reference sites.

- [ ] **Step 1: Turn the rules on**

In `.ruff.toml`, change the `[lint] select` list from:
```toml
select = [
    "E501",
    "I",
    "D",
]
```
to:
```toml
select = [
    "E501",
    "I",
    "D",
    "N802",
    "N803",
    "N815",
    "N816",
]
```
(Do **not** add `N806` — locals are deferred. Do **not** add `N801`/`N804`/`N805`.)

- [ ] **Step 2: Generate the worklist**

Run: `ruff check . --select N802,N803,N815,N816 --output-format concise`
Expected: a list of violations — these are the definitions to rename. Representative set confirmed in planning: functions `getOttoLogger`/`initOttoLogger`/`getRepos`/`_getRepos`/`getCompletionNames`/`getVersion`/`getEnvVar`/`getEnv`/`getEnvInt`/`getEnvPath`/`getEnvPaths`/`applyRepoSettings`/`applySettings`/`splitOnCommas`/`addHost`/`collectTests`/`startMonitor`/`stopMonitor`/`validatePath`/`runGitCommand`/`removeOldLogs`/`importTestFiles`/`importInitModules`/`readSettings`/`parseSettings`/`setGitDescription`/`setCommitHash`/`getOttoSettingsPath`/`getLoggingCommandOutputEnabled`/`addLibsToPythonpath`/`prettyPrint`/ rich-panel builders (`getTestSuitesPanel`,`getLabPanel`,`getInstructionsPanel`,`getTestsPanel`,`getTestFilesPanel`,`_makeTestPanel`)/ monitor getters (`getMonitorResults`,`getMonitorEvents`,`addMonitorEvent`)/ private helpers (`_expandString`,`_expandRecursive`,`_parseOsProfiles`,`_generateName`,`_generateId`,`_commandToDirName`,`_parseHostDefaults`,`_parseDockerSettings`,`_getIndividualLab`,`_fieldDefault`,`_addLogHandlers`,`_runSshCmds`); args `destDir`/`srcFiles`/`mustExist`; attrs/properties `isDryRun`/`_slotStr`/`_neIdStr`(already done)/`reservationSettings`/`versionDict`/`commandStatus`(if attr)/`settingsText`(if attr); globals `_gitDescription`/`_gitHash`/`_activeMonitorCollector`/`configModule`.

  **Sanity — these must NOT appear** (proof the rule scoping is right): `getLogger`, `setLoggerClass`, `addHandler`, `setLevel`, `setUp`, `tearDown`, `reportUnusedFunction`, `sysUpTime`, `sysDescr`, `macOS`. If any do, investigate before renaming (they may be otto definitions shadowing a stdlib name).

- [ ] **Step 3: Rename each flagged definition + its references, file by file**

For each violation, snake_case the **definition** and update every **reference**. Procedure per symbol `oldName` → `new_name`:
```bash
# find references (word-boundary), excluding historical docs
grep -rEln "\b oldName \b" src tests --include='*.py' | grep -vE '^docs/superpowers/(plans|specs)/'
# rename in each file
sed -i 's/\boldName\b/new_name/g' <files>
```
Work in small batches (e.g. all logging helpers, then all `getRepos`/repo helpers, then the rich panels, then the monitor getters, then the kwargs `destDir`/`srcFiles`/`mustExist`, then the globals). After each batch run the relevant test slice (below) to catch a missed reference early. The mapping is the literal snake_case of each name: `getOttoLogger`→`get_otto_logger`, `destDir`→`dest_dir`, `srcFiles`→`src_files`, `mustExist`→`must_exist`, `isDryRun`→`is_dry_run`, `_gitHash`→`_git_hash`, `_activeMonitorCollector`→`_active_monitor_collector`, etc.

- [ ] **Step 4: Iterate until lint is clean**

Run: `ruff check . --select N802,N803,N815,N816`
Expected: `All checks passed!` — this **proves** the API/field rename is complete (no otto mixedCase definitions remain except the deferred locals, which `N806`-off ignores).

- [ ] **Step 5: Run the full unit suite + confirm no naming-debt regression**

Run: `make test` → Expected: PASS.
Run: `ruff check src tests --select N802,N803,N815,N816` → Expected: `All checks passed!` (0 — already covered by Step 4, re-confirm here).
Run: `ruff check src tests scripts --statistics` → Expected: only the pre-existing `D209`/`I001`/`D401`/`E501`/`D403` debt remains, with no new `N8xx` lines and no material jump in `E501`/`I001`. The four `N` rules are now in `.ruff.toml` with 0 violations, so they won't add to the debt.

- [ ] **Step 6: Checkpoint**

`git add -A`

---

### Task 5: Cross-tier verification and squash message

The rename can break integration/embedded paths and on-disk parsing that no-VM unit never exercises. The spec's gotcha: the WS#1 leak only surfaced in the *combined* single-process run.

- [ ] **Step 1: Combined coverage run (single process, all tiers)**

Run: `make coverage`
Expected: PASS at or above the 90% floor. Watch specifically for lab-JSON parse errors (`element`/`os_type` key misses) and import errors (missed module-path token).

- [ ] **Step 2: VM tiers**

Run: `make nox` (or the project's VM-tier target) to exercise integration/embedded/hops against the lab VMs.
Expected: PASS. A failure naming a missing `ne`/`osType` key or an unresolved `*Host` import is a missed reference — fix it, re-run Tasks 3/4 verification, and return here.

- [ ] **Step 3: Final stale-token sweep (whole tree, live files only)**

```bash
grep -rEn "\b(osType|osName|osVersion|neId)\b|(dockerHost|embeddedHost|localHost|unixHost|remoteHost)" \
  src tests --include='*.py'
grep -rEn '"(ne|osType|osName|osVersion)":' tests/lab_data
ruff check . --select N802,N803,N815,N816
```
Expected: first two empty; lint clean.

- [ ] **Step 4: Produce the squash-commit message**

Hand Chris this paste-able conventional-commit message (he authors the squash so the AI-assist trailer is correct):

```
refactor(naming)!: snake_case sweep — element vocabulary, os_* fields, host filenames

Normalize the Python API, host-module filenames, and lab-data schema to PEP 8
snake_case before the contract freeze (fable-review WS#2). Clean rename, no
backcompat shims.

- Rename network-element vocabulary ne→element, neId→element_id (JSON key,
  dataclass field, and all references). Deliberate reversal of the roadmap's
  "keep ne" decision, per 2026-06-14.
- snake_case the lab-data schema fields osType/osName/osVersion→os_type/os_name/
  os_version in both the dataclasses and the on-disk JSON; update the factory
  parse path and fixtures.
- Rename the 5 host modules {docker,embedded,local,remote,unix}Host.py →
  *_host.py and their test files; update all imports, registry refs, and live
  docs.
- snake_case the remaining split-brain API (get_otto_logger, get_repos,
  apply_repo_settings, is_dry_run, transfer kwargs dest_dir/src_files/must_exist,
  …); enable ruff N802/N803/N815/N816 to lock it in (N806/locals deferred).
- Add the typer<0.26 ceiling (carried-over WS#1 hygiene; records the Typer 0.26
  triage deferral to Pydantic Phase B).

BREAKING CHANGE: lab JSON keys ne/osType/osName/osVersion renamed to
element/os_type/os_name/os_version; host module paths renamed to snake_case.
No alias shims — update lab files and imports.
```

- [ ] **Step 5: Hand off**

Report the branch state, the verification results (`make test` / `make coverage` / `make nox` / `ruff check`), and the squash message. Do not merge — Chris rebases onto current main and squash-merges.

---

## Self-Review (completed during planning)

- **Spec coverage:** typer ceiling → Task 1. Host filenames → Task 2. JSON/`os_*`/`element` schema + factory seam → Task 3. Cross-module API helpers + scoped lint → Task 4. Combined-coverage + VM-tier verification → Task 5. Non-goals (Pydantic, registry split, WS#1 surface, locals/`N806`) explicitly excluded. ✅
- **Placeholder scan:** no TBD/TODO; every rename has an explicit old→new mapping or a deterministic `ruff`/`grep` worklist with the literal snake_case rule stated. ✅
- **Name consistency:** `element`/`element_id`/`_element_id_str`, `os_type`/`os_name`/`os_version`, `dest_dir`/`src_files`/`must_exist` used identically across Tasks 3–5 and the squash message. The deferred local `neStr` is called out once and consistently. ✅
