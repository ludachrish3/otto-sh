# Settings Path Anchoring — Phase 3 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove `${sut_dir}` from `.otto/settings.toml` entirely, replacing it with a loud migration error, so the repo-root convention is the only path rule otto has.

**Architecture:** Phases 1-2 made a bare relative path resolve against the repo root everywhere — including the raw-dict readers. That makes `${sut_dir}` redundant for every real usage. This phase deletes all five hand-rolled substitution sites and gives custom reservation backends the `repo_dir=` their lab counterparts already receive.

**Tech Stack:** Python 3.10+, pydantic v2, pytest, MyST/Sphinx.

**Spec:** `docs/superpowers/specs/2026-08-01-settings-path-anchoring-design.md`, especially **§11a** (the decision to remove rather than narrow, and what capability that gives up). §6's conclusion is superseded by §11a; its analysis of why the passthrough tables are untypable is still accurate.

**This is a BREAKING change.** Phases 1-2 are merged to main (`61a58127`, `11f86aab`) and were both non-breaking. This one is not.

## Global Constraints

- 🚨 **THE TEST BED IS OFF-LIMITS.** Another agent is running chaos testing against the lab VMs, and some of its work is timing-sensitive. Permitted: `.venv/bin/pytest tests/unit/...` with an explicit path, `make check-python`, `make docs-lint`, `.venv/bin/sphinx-build -E -a -W -b html docs/ docs/_build/html`, `.venv/bin/python scripts/import_budget.py --check`. **Forbidden:** `make coverage*`, `make nox*`, `make dashboard`, `make all`, `make ci`, `make validate*`, `make release`, `make docs-media`, `make profile`, `--hyperfine`, and any bare `pytest` (it would collect `tests/integration/` and `tests/e2e/`, which need the lab).
- **Never use `git stash`.** The stash stack is shared across worktrees in this repo and popping it can destroy another session's work. Inspect earlier versions with `git show <sha>:<path>`.
- **No migration diagnostic.** `${sut_dir}` is simply no longer substituted; it survives parsing as a literal path segment. (This constraint originally demanded a loud parse-time rejection; Chris dropped it mid-execution — otto has no users, so the check could only ever see otto's own migrated test corpus.)
- **Do not weaken or delete the anchoring behavior.** `anchor_path` in `otto/utils.py` stays the single implementation; every current caller keeps calling it. Only the `${sut_dir}` *substitution* goes away.
- No `from __future__ import annotations`. Never add `.resolve()`.
- Use `.venv/bin/...`, not `uv run`.
- **Commits:** this is a worktree, so commit each task yourself. End every commit message body with the trailer `Assisted-by: Claude Opus 5`. The task that lands the breaking change must mark it per Conventional Commits (a `!` after the scope, plus a `BREAKING CHANGE:` footer) — this repo generates its CHANGELOG from commit history.

---

## File Structure

| File | Change |
| --- | --- |
| `tests/repo{1,2,3,_broken,_e2e}/.otto/settings.toml` | Migrate off `${sut_dir}` (Task 1) |
| ~17 test modules + `tests/custom_hosts/` | Same (Task 1) |
| `src/otto/config/repo.py` | Delete `_expand_string` / `_expand_recursive`; `lab_settings` / `reservation_settings` return raw (Task 2) |
| `src/otto/coverage/tiers.py` | Delete `_expand_harvest_dir`'s substitution (Task 2) |
| `src/otto/coverage/collect.py` | Delete substitution from `_anchor_build_dir` (Task 2) |
| `src/otto/coverage/overrides.py` | Delete substitution (Task 2) |
| `src/otto/cli/init.py` | Delete substitution from `_settings_paths` (Task 2) |
| `src/otto/reservations/__init__.py` | Pass `repo_dir=` to custom backends (Task 3) |
| `docs/guide/setup/repo-setup.md` + 1 other | Remove the `${sut_dir}` subsection and example (Task 4) |

---

### Task 1: Migrate every fixture and test off `${sut_dir}`

Pure preparation — **no behavior change**. Bare relative paths already resolve against the repo root (phases 1-2), so every one of these edits is a no-op at runtime today. Doing it first means Task 2 does not have to fix fixtures and code at once.

**Files:** every file matching the grep in Step 1. There are roughly 22.

**Interfaces:** none. This task changes only test data.

- [ ] **Step 1: Enumerate the work**

Run:

```bash
grep -rln '\${sut_dir}\|\${{sut_dir}}' tests/
```

Record the list in your report. Every file it names must be migrated in this task.

- [ ] **Step 2: Apply the transformation**

The rule, applied to each string value:

| Before | After | Why |
| --- | --- | --- |
| `"${sut_dir}/tests"` | `"tests"` | repo-root relative |
| `"${sut_dir}/../custom_hosts"` | `"../custom_hosts"` | `..` still works from the repo root |
| `"${sut_dir}"` (whole value) | `"."` | the repo root itself |

In Python test sources the literal appears inside f-strings as `${{sut_dir}}` (doubled braces). Apply the same transformation there, and **remove only the variable** — leave the surrounding f-string braces intact.

Do **not** hand-edit `docs/` in this task, and do **not** touch `src/`.

- [ ] **Step 3: Verify the migration is complete**

Run:

```bash
grep -rn '\${sut_dir}\|\${{sut_dir}}' tests/
```

Expected: **no output.** Any remaining hit is a file you missed.

- [ ] **Step 4: Verify nothing changed behaviorally**

Run: `.venv/bin/pytest tests/unit/ -q`

Expected: the same pass count as before your change (**4522 passed, 2 skipped, 1 xfailed**). This is the whole point of doing the migration first — if a test now fails, the bare relative path does *not* resolve where `${sut_dir}` did, and that is a real finding. **If anything fails, STOP and report** rather than adjusting the expected value.

- [ ] **Step 5: Run the static gate**

Run: `make check-python`

Expected: clean.

- [ ] **Step 6: Commit**

```bash
git add tests/
git commit -m "$(cat <<'EOF'
test: migrate fixtures off ${sut_dir}

Bare relative paths have resolved against the repo root since phase 1,
so every one of these edits is a runtime no-op today. Doing the fixture
migration on its own keeps the commit that removes ${sut_dir} support
focused on source, and proves the two spellings are equivalent before
one of them is deleted.

Assisted-by: Claude Opus 5
EOF
)"
```

---

### Task 2: Delete all substitution

The breaking change, landed atomically so there is never a commit where `${sut_dir}` silently misbehaves.

> **Amended mid-execution (Chris).** The task originally added a parse-time rejection naming the offending key. Dropped: otto has no users, so the only input the check could ever see is otto's own test corpus, which Task 1 already migrated — the helper is dead code by construction. A leftover `${sut_dir}` now survives parsing as a literal path segment. Steps 1-3 below are superseded by the amended versions inline; Steps 4-8 stand.

**Files:**

- Modify: `src/otto/config/repo.py`, `src/otto/coverage/tiers.py`, `src/otto/coverage/collect.py`, `src/otto/coverage/overrides.py`, `src/otto/cli/init.py`
- Test: `tests/unit/config/test_settings_path_anchoring.py` (append), plus whichever existing tests assert on expansion

**Interfaces:**

- Consumes: Task 1's migrated fixtures.
- Produces: `Repo.lab_settings` and `Repo.reservation_settings` now return the **raw** sub-dict (no expansion). Their return type is unchanged (`dict[str, Any]`).

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/config/test_settings_path_anchoring.py`:

```python
def test_sut_dir_variable_is_no_longer_substituted(tmp_path):
    """``${sut_dir}`` is gone: it survives parsing as a literal path segment."""
    sut = _write_repo(tmp_path / "repo", 'libs = ["${sut_dir}/pylib"]')

    repo = Repo(sut_dir=sut)

    assert repo.libs == [sut / "${sut_dir}" / "pylib"]
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/bin/pytest tests/unit/config/test_settings_path_anchoring.py -k sut_dir_variable -v`

Expected: FAIL — today `${sut_dir}` expands, so `repo.libs` is `sut / "pylib"`.

- [ ] **Step 3: Stop substituting in `repo.py`**

In `parse_settings`, validate the raw settings dict directly. Replace:

```python
        expanded = self._expand_recursive(self.settings)
        model = SettingsModel.model_validate(expanded, context={"sut_dir": self.sut_dir})
```

with:

```python
        model = SettingsModel.model_validate(
            self.settings, context={"sut_dir": self.sut_dir}
        )
```

- [ ] **Step 4: Delete the expansion machinery**

In `src/otto/config/repo.py`:

- Delete the `_expand_recursive` method entirely.
- Delete the `_expand_string` method entirely.
- In `reservation_settings`, return the raw sub-dict: replace `return self._expand_recursive(raw)` with `return raw`. Update its docstring — it no longer expands anything.
- In `lab_settings`, do the same.
- Update the `__post_init__` comment (around line 254) that explains why `sut_dir` is made absolute "before `${sut_dir}` expansion". The `.absolute()` call **stays** — `anchor_path` still needs an absolute root — but its justification is now "so anchoring produces absolute paths", not the expansion-ordering argument.

- [ ] **Step 5: Delete the four satellite substitution sites**

- `src/otto/coverage/tiers.py` — in `_expand_harvest_dir`, delete the `raw = raw.replace(...)` line and the now-unused `sut_dir` handling. If the function becomes a bare `Path(raw)`, inline it at its call site and delete the function. Update `load_tiers`'s docstring, which promises `${sut_dir}` expansion.
- `src/otto/coverage/collect.py` — in `_anchor_build_dir`, delete the `raw = raw.replace(...)` line, keeping the `anchor_path(...)` call. Update the docstring.
- `src/otto/coverage/overrides.py` — delete the `if raw_key: raw_key = raw_key.replace(...)` block, keeping the `anchor_path(...)` call.
- `src/otto/cli/init.py` — in `_settings_paths`, simplify `paths = [Path(str(v).replace(...)) for v in values]` to `paths = [Path(str(v)) for v in values]`. Update the docstring, which describes mirroring `Repo._expand_string` — a method that no longer exists.

**After this step, `grep -rn 'sut_dir}' src/` must return nothing** except unrelated `self.sut_dir` f-string interpolations.

- [ ] **Step 6: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/unit/config/test_settings_path_anchoring.py -v`

Expected: all pass, including the new no-substitution test.

- [ ] **Step 7: Fix the tests that asserted on expansion**

Several existing tests assert `${sut_dir}` expands. Find them:

```bash
.venv/bin/pytest tests/unit/ -q 2>&1 | tail -30
```

Known candidates: `tests/unit/config/test_repo.py` (the `${sut_dir}`-in-`host_preferences` and os-profile expansion tests), `tests/unit/cov/test_tiers.py`, `tests/unit/models/test_settings.py`.

For each, decide deliberately and say which you chose in your report:

- if the test exists to prove **expansion happens**, it is testing a removed feature — delete it;
- if it uses `${sut_dir}` only as a convenient way to write a path, migrate it to a bare relative path per Task 1's rule.

**Do not** weaken a test to make it pass. If one seems to require keeping expansion, STOP and report.

- [ ] **Step 8: Full hostless verification**

Run: `.venv/bin/pytest tests/unit/ -q`, then `make check-python`, then `.venv/bin/python scripts/import_budget.py --check`.

Expected: all green. Report the pass count.

- [ ] **Step 9: Commit**

```bash
git add src/ tests/
git commit -m "$(cat <<'EOF'
feat(settings)!: remove ${sut_dir}

A bare relative path in .otto/settings.toml has resolved against the
repo root since phase 1, and phase 2 extended that to the raw-dict
readers, so ${sut_dir} became redundant for every real usage -- a
repo-wide sweep found no value embedding it in a larger string, and no
custom backend outside src/otto/examples.

Keeping it was not free. The substitution is a plain str.replace run
over the settings dict BEFORE validation, and each subsystem reading
the raw dict had to re-implement it: five hand-rolled copies, two of
them added during phase 2. That ordering is also what produced phase
1's double-anchor regression, where ${sut_dir} expanded to a relative
string that then got anchored a second time.

Deletes all five substitution sites. A leftover ${sut_dir} is not
diagnosed specially -- otto has no users to migrate, so a dedicated
rejection path would be dead code. The variable simply survives
parsing as a literal path segment.

BREAKING CHANGE: ${sut_dir} is no longer expanded in .otto/settings.toml.
Drop the prefix -- relative paths resolve against the repo root.

Assisted-by: Claude Opus 5
EOF
)"
```

---

### Task 3: Give custom reservation backends `repo_dir=`

Removing `${sut_dir}` takes away the only way a custom reservation backend could reference the repo root. Custom **lab** backends already receive `repo_dir=`; this makes the two contracts symmetric (spec §11a).

**Files:**

- Modify: `src/otto/reservations/__init__.py`
- Test: wherever the reservation backend factory is tested (find it — likely `tests/unit/reservations/test_build_backend.py`)

**Interfaces:**

- Consumes: nothing from earlier tasks.
- Produces: custom reservation backend constructors now receive `repo_dir: Path` in addition to `url` and their `[reservations.<backend>]` kwargs.

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/reservations/test_build_backend.py`. The backend registry is **process-global**, so the registration must be undone in a `finally` — this mirrors `tests/unit/reservations/test_registry.py::test_register_and_lookup`, which is the established pattern in this suite:

```python
def test_custom_backend_receives_repo_dir(tmp_path):
    """Custom reservation backends get repo_dir, like custom lab backends."""
    from otto.reservations import build_backend, register_reservation_backend
    from otto.reservations.registry import RESERVATION_BACKENDS

    seen: dict[str, object] = {}

    class RecordingBackend:
        def __init__(self, **kwargs):
            seen.update(kwargs)

        def get_reserved_resources(self, username):
            return set()

        def who_reserved(self, resource):
            return []

        def backend_name(self):
            return "recording"

    register_reservation_backend("recording-test", RecordingBackend)
    try:
        build_backend({"backend": "recording-test"}, tmp_path)
    finally:
        RESERVATION_BACKENDS.unregister("recording-test")

    assert seen["repo_dir"] == tmp_path
```

Check the exact import locations before writing it — `register_reservation_backend` is re-exported from `otto.reservations`, and `RESERVATION_BACKENDS` lives in `otto.reservations.registry`.

- [ ] **Step 2: Run it to verify it fails**

Run the new test. Expected: FAIL with a `TypeError` about an unexpected keyword argument, or an assertion that `repo_dir` was absent.

- [ ] **Step 3: Pass `repo_dir=`**

In `src/otto/reservations/__init__.py`, in the custom-backend branch of `build_backend`, replace:

```python
    extra_kwargs: dict[str, Any] = settings.get(backend_name) or {}
    if url is not None:
        return cls(url=url, **extra_kwargs)  # type: ignore[no-any-return]
    return cls(**extra_kwargs)  # type: ignore[no-any-return]
```

with the same shape, passing `repo_dir=repo_dir` in both branches. Update `build_backend`'s docstring: `repo_dir` is now forwarded to custom backends, mirroring `otto.labs.build_lab_repository`.

Also update `src/otto/examples/reservations.py` so the example backend accepts `repo_dir` — it is the template users copy.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/unit/reservations/ -q`

Expected: all pass.

- [ ] **Step 5: Run the gates**

Run: `.venv/bin/pytest tests/unit/ -q` and `make check-python`.

Expected: green.

- [ ] **Step 6: Commit**

```bash
git add src/ tests/
git commit -m "$(cat <<'EOF'
feat(reservations)!: pass repo_dir to custom backends

Custom lab backends already receive repo_dir= and can anchor their own
path-like kwargs. Custom reservation backends did not, so ${sut_dir}
was their only way to reference the repo root -- and it has just been
removed. Passing repo_dir makes the two backend contracts symmetric.

BREAKING CHANGE: a custom reservation backend's __init__ now receives
repo_dir. Backends registered via register_reservation_backend must
accept it (the built-in json and none backends are unaffected).

Assisted-by: Claude Opus 5
EOF
)"
```

---

### Task 4: Documentation

**Files:**

- Modify: `docs/guide/setup/repo-setup.md`
- Modify: any other page still mentioning `${sut_dir}` (Step 1 finds them)

**Interfaces:** none.

- [ ] **Step 1: Find every remaining mention**

```bash
grep -rn '\${sut_dir}' docs/ --include="*.md" --include="*.rst" | grep -v "docs/superpowers/"
```

Record the list. `docs/superpowers/` holds specs and plans — the historical record — and must **not** be edited.

- [ ] **Step 2: Rewrite the `#### ${sut_dir}` subsection**

In `docs/guide/setup/repo-setup.md`, replace the entire `#### ${sut_dir}` subsection (heading, prose, and the `sqlite:///${sut_dir}/lab.db` example) with a short migration note:

```text
#### Removed: `${sut_dir}`

Earlier versions expanded `${sut_dir}` to the repo root.  It is gone —
a relative path already resolves there, so the prefix was redundant.  It
is no longer special in any way: a settings file still containing it
gets a directory literally named `${sut_dir}`.

Drop the prefix: `"${sut_dir}/tests"` becomes `"tests"`, and
`"${sut_dir}/../shared"` becomes `"../shared"`.

Values otto hands to a backend without interpreting them
(`[lab.<backend>]`, `[reservations.<backend>]`, `ssh_options`) must now
be absolute.  Custom lab and reservation backends both receive
`repo_dir` and can anchor their own paths.
```

- [ ] **Step 3: Clean up any other page**

Apply Task 1's transformation to any remaining `${sut_dir}` in a docs example, and delete any prose promising expansion.

- [ ] **Step 4: Verify**

```bash
grep -rn '\${sut_dir}' docs/ --include="*.md" --include="*.rst" | grep -v "docs/superpowers/"
```

Expected: only the migration note added in Step 2.

Then run `make docs-lint` and the forced `.venv/bin/sphinx-build -E -a -W -b html docs/ docs/_build/html`. `make docs-html` can be a silent no-op — always force the rebuild and confirm "build succeeded".

- [ ] **Step 5: Commit**

```bash
git add docs/
git commit -m "$(cat <<'EOF'
docs(settings): document the removal of ${sut_dir}

Replaces the variable's reference section with a migration note: drop
the prefix, and use absolute paths in the tables otto passes through
without interpreting.

Assisted-by: Claude Opus 5
EOF
)"
```

---

## Phase Completion

Verify the whole phase:

```bash
grep -rn 'sut_dir}' src/ tests/ docs/ --include="*.py" --include="*.toml" --include="*.md" | grep -v "docs/superpowers/"
```

Expected: only unrelated `self.sut_dir` f-string interpolations in `src/`, the literal-passthrough tests, and the migration note in `docs/guide/setup/repo-setup.md`.

Then report to Chris:

- The hostless results (`pytest tests/unit/`, `check-python`, `docs-lint`, forced sphinx, import budget).
- That **`make coverage` has not run** — the lab VMs are in use.
- That this phase is **breaking**, with both `BREAKING CHANGE:` footers, and that phases 1-2 on main were not.
- The spec §12 items still open: `~unknownuser` behavior, whether to anchor the otto-declared `ssh_options` path fields by name, and the structural follow-up (a single expanded-and-anchored settings view for the raw-dict readers) — removing the substitution deletes the substitution duplication but not the anchoring duplication.
