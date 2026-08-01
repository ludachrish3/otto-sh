# Settings Path Anchoring — Phase 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make every `Path`-typed field in `.otto/settings.toml` resolve against the repo root when relative, with `~` as the sole opt-out — fixing the silent CWD-relative bug in `labs`/`libs`/`tests`/`[docker]`.

**Architecture:** Declare the rule once as a pydantic annotated type (`RepoPath`) whose `AfterValidator` expands `~` and then joins any still-relative path onto a repo root supplied through pydantic's validation context. `Repo.parse_settings` is the only production caller and passes `context={"sut_dir": self.sut_dir}`. Eight field annotations change from `Path` to `RepoPath`; no resolution logic is added at any call site.

**Tech Stack:** Python 3.10+, pydantic v2 (2.13.4 installed, floor `>=2.6`), pytest.

**Spec:** `docs/superpowers/specs/2026-08-01-settings-path-anchoring-design.md` (in this worktree). This plan implements **Phase 1 only** (spec §11.1). Phases 2 (docs) and 3 (`${sut_dir}` narrowing) are out of scope.

## Global Constraints

- **No `from __future__ import annotations`** anywhere — it trips Sphinx `-W` in this project.
- **Do not add `.resolve()`** to the anchoring logic (spec §7). Joining only; symlinks must survive.
- **Do not `os.chdir`** anywhere in production code (spec §3). otto never chdirs.
- **Phase 1 is non-breaking.** `${sut_dir}` continues to expand exactly as today. Expansion runs *before* validation (`src/otto/config/repo.py:609-610`), so a `${sut_dir}`-prefixed value is already absolute when `RepoPath` sees it and the anchoring is a no-op. Do not remove, reject, or warn on `${sut_dir}` in this phase.
- **`SettingsModel` must stay independently validatable.** When the validation context is missing, leave the path relative rather than raising — several existing tests construct `SettingsModel` with no repo.
- **🚨 THE TEST BED IS OFF-LIMITS.** Another agent is running chaos testing against the lab VMs right now. Run **hostless targets only**. Permitted: `.venv/bin/pytest tests/unit/...`, `make check-python`, `make coverage-unit`. **Forbidden:** `make coverage`, `make coverage-python`, `make coverage-integration`, `make coverage-unix`, `make coverage-embedded`, `make nox*`, `make dashboard`, `make all`, `make ci`, `make validate*`, `make release`, and any bare `pytest` invocation that could collect outside `tests/unit/` (it would reach `tests/integration/` and `tests/e2e/`). Always pass an explicit `tests/unit/...` path. If a step seems to need the test bed, **stop and report** rather than improvising.
- **Per-step loop:** targeted `.venv/bin/pytest tests/unit/...`. **Per-task gate:** `make check-python && make coverage-unit` (both hostless). The full `make coverage` gate for the phase is Chris's to run later, once the bed is free.
- **Commits:** this is a worktree, so commit each task yourself. End every commit message body with the trailer `Assisted-by: Claude Opus 5`.

---

## File Structure

| File | Responsibility | Change |
| --- | --- | --- |
| `src/otto/models/settings.py` | Boundary specs for `settings.toml` | Add `_anchor_to_repo` + `RepoPath`; retype 8 fields; delete `MonitorSettingsSpec._expand_user` |
| `src/otto/config/repo.py` | Parses settings into a `Repo` | Pass `context={"sut_dir": ...}` at the one `model_validate` call |
| `tests/unit/config/test_settings_path_anchoring.py` | The anchoring contract | **Create** — all new tests live here |

All eight retyped fields are in `settings.py`; the rule is declared once at the top of that module. No new module is warranted — `settings.py` is the single home for settings boundary types, and `RepoPath` is one function plus one alias.

**The eight fields** (spec §5, Path A):

1. `SettingsModel.labs` — `settings.py:421`
2. `SettingsModel.libs` — `settings.py:423`
3. `SettingsModel.tests` — `settings.py:424`
4. `DockerImageSpec.dockerfile` — `settings.py:48`
5. `DockerImageSpec.context` — `settings.py:49`
6. `DockerComposeSpec.path` — `settings.py:80`
7. `MonitorSettingsSpec.tls_cert` — `settings.py:128`
8. `MonitorSettingsSpec.tls_key` — `settings.py:129`

---

### Task 1: `RepoPath` type, validation context, and `labs`/`libs`/`tests`

This task introduces the mechanism and applies it to the three fields where the bug bites hardest — `tests` currently discovers nothing when written as a bare relative path, and fails silently (`src/otto/config/repo.py:371` `if d.exists()`, `:729` `if test_dir.is_dir()`).

**Files:**

- Modify: `src/otto/models/settings.py` (imports; new type before `DockerImageSpec` at line 38; fields at 421-424)
- Modify: `src/otto/config/repo.py:610`
- Test: `tests/unit/config/test_settings_path_anchoring.py` (create)

**Interfaces:**

- Consumes: nothing from earlier tasks.
- Produces:
  - `otto.models.settings.RepoPath` — `Annotated[Path, AfterValidator(_anchor_to_repo)]`. Tasks 2 and 3 annotate their fields with this exact name.
  - `otto.models.settings._anchor_to_repo(v: Path, info: ValidationInfo) -> Path` — module-private; only `RepoPath` references it.
  - The validation context key is the literal string `"sut_dir"`, carrying a `pathlib.Path`.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/config/test_settings_path_anchoring.py`:

```python
"""Repo-root anchoring for the path fields of ``.otto/settings.toml``.

Every path is ``expanduser()``-expanded and, when still relative, resolved
against the repo root — never the process CWD. See
``docs/superpowers/specs/2026-08-01-settings-path-anchoring-design.md``.
"""

import textwrap
from pathlib import Path

from otto.config.repo import Repo


def _write_repo(repo_dir: Path, settings_body: str) -> Path:
    """Materialize a minimal SUT repo at *repo_dir* with *settings_body* appended."""
    otto_dir = repo_dir / ".otto"
    otto_dir.mkdir(parents=True)
    base = 'name = "tmp_repo"\nversion = "1.0.0"'
    body = textwrap.dedent(settings_body).strip()
    (otto_dir / "settings.toml").write_text(f"{base}\n{body}\n")
    return repo_dir


def test_relative_paths_anchor_to_repo_root_not_cwd(tmp_path, monkeypatch):
    """The core bug: a bare relative path must not depend on where otto was run."""
    sut = _write_repo(
        tmp_path / "repo",
        """
        labs  = ["lab_data"]
        libs  = ["pylib"]
        tests = ["tests"]
        """,
    )
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    repo = Repo(sut_dir=sut)

    assert repo.labs == [sut / "lab_data"]
    assert repo.libs == [sut / "pylib"]
    assert repo.tests == [sut / "tests"]


def test_sut_dir_variable_and_bare_relative_agree(tmp_path):
    """Phase 1 is non-breaking: both spellings resolve to the same place."""
    sut = _write_repo(tmp_path / "repo", 'libs = ["${sut_dir}/pylib", "pylib"]')

    repo = Repo(sut_dir=sut)

    assert repo.libs == [sut / "pylib", sut / "pylib"]


def test_absolute_paths_pass_through_unchanged(tmp_path):
    shared = tmp_path / "shared" / "pylib"
    sut = _write_repo(tmp_path / "repo", f'libs = ["{shared}"]')

    repo = Repo(sut_dir=sut)

    assert repo.libs == [shared]


def test_parent_relative_escapes_the_repo_root_unresolved(tmp_path):
    """``..`` works, and the join is NOT ``resolve()``d — symlinks must survive."""
    sut = _write_repo(tmp_path / "repo", 'libs = ["../shared/pylib"]')

    repo = Repo(sut_dir=sut)

    assert repo.libs == [sut / ".." / "shared" / "pylib"]


def test_each_repo_anchors_to_its_own_root(tmp_path):
    """Multi-repo (OTTO_SUT_DIRS): identical text, per-repo resolution."""
    a = _write_repo(tmp_path / "a", 'libs = ["pylib"]')
    b = _write_repo(tmp_path / "b", 'libs = ["pylib"]')

    assert Repo(sut_dir=a).libs == [a / "pylib"]
    assert Repo(sut_dir=b).libs == [b / "pylib"]


def test_model_without_context_leaves_relative_paths_unchanged():
    """SettingsModel stays independently validatable with no repo attached."""
    from otto.models.settings import SettingsModel

    model = SettingsModel.model_validate(
        {"name": "x", "version": "1.0.0", "libs": ["pylib"]}
    )

    assert model.libs == [Path("pylib")]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/unit/config/test_settings_path_anchoring.py -v`

Expected: `test_relative_paths_anchor_to_repo_root_not_cwd`, `test_sut_dir_variable_and_bare_relative_agree`, `test_absolute_paths_pass_through_unchanged`, `test_parent_relative_escapes_the_repo_root_unresolved`, and `test_each_repo_anchors_to_its_own_root` FAIL — each with an assertion showing a bare relative `PosixPath('pylib')` where an absolute path under the repo root was expected. `test_model_without_context_leaves_relative_paths_unchanged` PASSES already (it asserts today's behaviour, and guards it from regressing).

- [ ] **Step 3: Add `AfterValidator` to the pydantic import**

In `src/otto/models/settings.py`, change the pydantic import line (currently line 20):

```python
from pydantic import (
    AfterValidator,
    ConfigDict,
    Field,
    ValidationInfo,
    field_validator,
    model_validator,
)
```

`Annotated`, `Path`, and `ValidationInfo` are already imported at lines 16-20 — do not re-add them.

- [ ] **Step 4: Declare the rule**

In `src/otto/models/settings.py`, insert immediately after the `if TYPE_CHECKING:` block (before `class DockerImageSpec` at line 38):

```python
def _anchor_to_repo(v: Path, info: ValidationInfo) -> Path:
    """Expand ``~``, then anchor a still-relative path to the repo root.

    ``settings.toml`` is committed and shared team-wide, so a CWD-relative
    value in it can never resolve stably. Absolute paths (including
    ``~``-rooted ones, already expanded here) pass through untouched.

    The repo root arrives via pydantic's validation context, which
    ``Repo.parse_settings`` supplies as ``{"sut_dir": ...}``. With no
    context the path is left relative so ``SettingsModel`` stays
    independently validatable.

    Deliberately does not ``resolve()``: that would collapse symlinks and
    change path identity for repos reached through symlinked checkouts.
    """
    v = v.expanduser()
    if v.is_absolute():
        return v
    sut_dir = (info.context or {}).get("sut_dir")
    return Path(sut_dir) / v if sut_dir is not None else v


RepoPath = Annotated[Path, AfterValidator(_anchor_to_repo)]
"""A ``settings.toml`` path: ``~``-expanded, then anchored to the repo root."""
```

- [ ] **Step 5: Thread the context through the one call site**

In `src/otto/config/repo.py`, change line 610 from:

```python
        model = SettingsModel.model_validate(expanded)
```

to:

```python
        model = SettingsModel.model_validate(expanded, context={"sut_dir": self.sut_dir})
```

- [ ] **Step 6: Retype the three fields**

In `src/otto/models/settings.py`, in `class SettingsModel` (lines 421-424), change:

```python
    labs: list[Path] = Field(default_factory=list)
    valid_labs: list[str] = Field(default_factory=list)
    libs: list[Path] = Field(default_factory=list)
    tests: list[Path] = Field(default_factory=list)
```

to:

```python
    labs: list[RepoPath] = Field(default_factory=list)
    valid_labs: list[str] = Field(default_factory=list)
    libs: list[RepoPath] = Field(default_factory=list)
    tests: list[RepoPath] = Field(default_factory=list)
```

Leave `valid_labs` alone — those are lab *names*, not paths (see the comment at `repo.py:615-617`).

- [ ] **Step 7: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/unit/config/test_settings_path_anchoring.py -v`

Expected: all 6 PASS.

- [ ] **Step 8: Verify no existing test regressed**

Run: `.venv/bin/pytest tests/unit/config/test_repo.py tests/unit/models/ tests/unit/cli/test_listing.py tests/unit/cli/test_init_scaffold.py tests/unit/bootstrap/ -q`

Expected: all pass. The `${sut_dir}` fixtures still work (expansion happens before validation, so anchoring is a no-op on them), and `SettingsModel`-without-context tests still pass via the absent-context fallback. **If anything fails here, stop and report** — a regression means the absent-context fallback or the expansion ordering assumption is wrong, and the rest of the plan rests on both.

- [ ] **Step 9: Run the task gate**

Run: `make check-python && make coverage-unit`

Expected: ruff lint + format clean, `ty` clean, unit suite green.

- [ ] **Step 10: Commit**

```bash
git add src/otto/models/settings.py src/otto/config/repo.py \
        tests/unit/config/test_settings_path_anchoring.py
git commit -m "$(cat <<'EOF'
fix(settings): anchor relative labs/libs/tests to the repo root

A bare relative path in settings.toml resolved against the process CWD,
so it never resolved stably for a committed, team-shared file -- and
every consumer skips silently on a miss (repo.py:371 `if d.exists()`,
repo.py:729 `if test_dir.is_dir()`), so `tests = ["tests"]` discovered
nothing and reported nothing.

Declares the rule once as a RepoPath annotated type: expanduser(), then
anchor to the repo root if still relative. The root reaches the
validator through pydantic's validation context, supplied by the single
model_validate call in Repo.parse_settings. New path fields inherit the
convention by construction rather than by memory.

Non-breaking: ${sut_dir} expansion runs before validation, so those
values are already absolute and the anchoring is a no-op on them.

Assisted-by: Claude Opus 5
EOF
)"
```

---

### Task 2: `[docker]` image and compose paths

`DockerImage.dockerfile`, `DockerImage.context`, and `DockerCompose.path` are documented in `src/otto/config/repo.py:60-64,78-79` as *"Absolute path to..."* — an invariant the code does not currently enforce. Their consumers use the value raw (`docker/staging.py:75` `tar.add(image.context)`, `docker/_context_hash.py:77` `image.dockerfile.read_bytes()`), so a bare relative entry silently reads from the wrong directory.

**Files:**

- Modify: `src/otto/models/settings.py:48-49` (`DockerImageSpec`), `:80` (`DockerComposeSpec`)
- Test: `tests/unit/config/test_settings_path_anchoring.py` (append)

**Interfaces:**

- Consumes: `RepoPath` from Task 1.
- Produces: nothing new. Runtime field names are unchanged — `repo.docker_settings.images[i].dockerfile` / `.context`, `repo.docker_settings.composes[i].path`.

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/config/test_settings_path_anchoring.py`:

```python
def test_docker_paths_anchor_to_repo_root(tmp_path, monkeypatch):
    """Dockerfile/context/compose paths are documented absolute; enforce it."""
    sut = _write_repo(
        tmp_path / "repo",
        """
        [[docker.images]]
        name = "api"
        dockerfile = "docker/api.Dockerfile"
        context = "docker"

        [[docker.composes]]
        path = "docker/compose.yml"
        """,
    )
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    repo = Repo(sut_dir=sut)

    image = repo.docker_settings.images[0]
    assert image.dockerfile == sut / "docker" / "api.Dockerfile"
    assert image.context == sut / "docker"
    assert repo.docker_settings.composes[0].path == sut / "docker" / "compose.yml"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/bin/pytest tests/unit/config/test_settings_path_anchoring.py::test_docker_paths_anchor_to_repo_root -v`

Expected: FAIL — `assert PosixPath('docker/api.Dockerfile') == PosixPath('/tmp/.../repo/docker/api.Dockerfile')`.

- [ ] **Step 3: Retype the docker path fields**

In `src/otto/models/settings.py`, in `class DockerImageSpec` (lines 48-49), change:

```python
    dockerfile: Path
    context: Path
```

to:

```python
    dockerfile: RepoPath
    context: RepoPath
```

And in `class DockerComposeSpec` (line 80), change:

```python
    path: Path
```

to:

```python
    path: RepoPath
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `.venv/bin/pytest tests/unit/config/test_settings_path_anchoring.py -v`

Expected: all 7 PASS.

- [ ] **Step 5: Verify the docker suites still pass**

Run: `.venv/bin/pytest tests/unit/docker/ -q`

Expected: all pass. `tests/unit/docker/test_compose.py:78-82` and `test_cli.py:29,49-52` write `${sut_dir}`-prefixed docker paths, which stay absolute pre-validation.

- [ ] **Step 6: Run the task gate**

Run: `make check-python && make coverage-unit`

Expected: clean.

- [ ] **Step 7: Commit**

```bash
git add src/otto/models/settings.py tests/unit/config/test_settings_path_anchoring.py
git commit -m "$(cat <<'EOF'
fix(settings): anchor relative [docker] paths to the repo root

DockerImage.dockerfile/.context and DockerCompose.path are documented as
absolute (config/repo.py:60-64,78-79) but nothing enforced it, and their
consumers use the value raw -- tar.add(image.context) in staging.py and
image.dockerfile.read_bytes() in _context_hash.py -- so a bare relative
entry silently read from the process CWD.

Assisted-by: Claude Opus 5
EOF
)"
```

---

### Task 3: `[monitor]` TLS paths

These already `expanduser()` via a dedicated field validator (`settings.py:131-134`), which `RepoPath` subsumes. The `~` convention documented at `settings.py:121-127` and `config/repo.py:108-115` — the committed value points under `~/.config/otto/tls/` and resolves per-user — **must survive untouched**. Only the fallback changes, from CWD to the repo root.

**Files:**

- Modify: `src/otto/models/settings.py:128-134` (`MonitorSettingsSpec`)
- Test: `tests/unit/config/test_settings_path_anchoring.py` (append)

**Interfaces:**

- Consumes: `RepoPath` from Task 1.
- Produces: nothing new. `repo.monitor_settings.tls_cert` / `.tls_key` are unchanged in name and type.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/config/test_settings_path_anchoring.py`:

```python
def test_monitor_tls_home_convention_survives(tmp_path, monkeypatch):
    """``~`` is the opt-out: it must NOT be swallowed by repo anchoring."""
    home = tmp_path / "home"
    (home / ".config" / "otto" / "tls").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))

    sut = _write_repo(
        tmp_path / "repo",
        """
        [monitor]
        tls_cert = "~/.config/otto/tls/cert.pem"
        tls_key = "~/.config/otto/tls/key.pem"
        """,
    )

    repo = Repo(sut_dir=sut)

    assert repo.monitor_settings.tls_cert == home / ".config" / "otto" / "tls" / "cert.pem"
    assert repo.monitor_settings.tls_key == home / ".config" / "otto" / "tls" / "key.pem"


def test_monitor_tls_relative_anchors_to_repo_root(tmp_path, monkeypatch):
    """A bare relative TLS path resolves under the repo, not the CWD."""
    sut = _write_repo(
        tmp_path / "repo",
        """
        [monitor]
        tls_cert = "certs/bundle.pem"
        """,
    )
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    repo = Repo(sut_dir=sut)

    assert repo.monitor_settings.tls_cert == sut / "certs" / "bundle.pem"
    assert repo.monitor_settings.tls_key is None
```

- [ ] **Step 2: Run the tests to verify the state**

Run: `.venv/bin/pytest tests/unit/config/test_settings_path_anchoring.py -k monitor -v`

Expected: `test_monitor_tls_home_convention_survives` PASSES already (the existing `_expand_user` validator handles it — this test is the regression guard proving Step 3 does not break it). `test_monitor_tls_relative_anchors_to_repo_root` FAILS: `assert PosixPath('certs/bundle.pem') == PosixPath('/tmp/.../repo/certs/bundle.pem')`.

- [ ] **Step 3: Replace `_expand_user` with `RepoPath`**

In `src/otto/models/settings.py`, in `class MonitorSettingsSpec`, change lines 128-134 from:

```python
    tls_cert: Path | None = None
    tls_key: Path | None = None

    @field_validator("tls_cert", "tls_key")
    @classmethod
    def _expand_user(cls, v: Path | None) -> Path | None:
        return v.expanduser() if v is not None else v
```

to:

```python
    tls_cert: RepoPath | None = None
    tls_key: RepoPath | None = None
```

`RepoPath` expands `~` itself, so the validator is redundant — deleting it keeps one implementation of the rule.

Then update the class docstring at lines 121-127, replacing:

```text
    TLS for the dashboard server. Paths are ``expanduser()``-expanded here
    (settings expansion only handles ``${sut_dir}``): the committed value is
    shared by the whole team, so it conventionally points under
    ``~/.config/otto/tls/`` — identical text, per-user resolution. ``tls_key``
    without ``tls_cert`` is rejected; ``tls_cert`` alone is fine (bundled PEM).
```

with:

```text
    TLS for the dashboard server. Paths follow the settings-wide convention
    (``RepoPath``): ``~``-expanded, then anchored to the repo root if still
    relative. The committed value is shared by the whole team, so it
    conventionally points under ``~/.config/otto/tls/`` — identical text,
    per-user resolution. ``tls_key`` without ``tls_cert`` is rejected;
    ``tls_cert`` alone is fine (bundled PEM).
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/unit/config/test_settings_path_anchoring.py -v`

Expected: all 9 PASS — including `test_monitor_tls_home_convention_survives`, which proves the `~` convention outlived the validator swap.

- [ ] **Step 5: Verify `field_validator` is still used**

Run: `make lint-python`

Expected: clean. `field_validator` remains imported and used by other specs in the module (e.g. `CoverageTierSpec._validate_color`, `SettingsModel._validate_version_format`), so the import must NOT be removed. If ruff reports an unused import, something else was deleted by mistake — stop and report.

- [ ] **Step 6: Verify the monitor suites still pass**

Run: `.venv/bin/pytest tests/unit/models/test_monitor.py tests/unit/config/test_repo.py -q && .venv/bin/pytest tests/unit -k monitor -q`

Expected: all pass.

- [ ] **Step 7: Run the task gate**

Run: `make check-python && make coverage-unit`

Expected: clean.

- [ ] **Step 8: Commit**

```bash
git add src/otto/models/settings.py tests/unit/config/test_settings_path_anchoring.py
git commit -m "$(cat <<'EOF'
refactor(settings): fold [monitor] TLS paths into RepoPath

MonitorSettingsSpec had its own expanduser() field validator; RepoPath
subsumes it, leaving one implementation of the rule. The `~` convention
is unchanged and now guarded by a test -- only the fallback moves, from
the process CWD to the repo root.

Assisted-by: Claude Opus 5
EOF
)"
```

---

## Phase Completion

After Task 3, all eight Path-A fields carry `RepoPath`. Verify the full set in one pass:

```bash
grep -n "RepoPath" src/otto/models/settings.py
```

Expected: the `_anchor_to_repo` definition, the `RepoPath` alias and its docstring, and exactly 8 field annotations (`labs`, `libs`, `tests`, `dockerfile`, `context`, `path`, `tls_cert`, `tls_key`).

Then hand back to Chris for the full gate (`make coverage`), which needs lab VMs and the browser lane. Report:

- The 9 new tests and what each pins down.
- That `${sut_dir}` behaviour is untouched in this phase.
- Spec §12 open items still unanswered — hard error vs. deprecation warning for phase 3, and `expanduser()` on an unresolvable `~user`. Phase 1 inherits whatever the stdlib does for the latter; it is not exercised by these tests.

Phases 2 (docs + `otto init` template) and 3 (`${sut_dir}` narrowing) get their own plans.
