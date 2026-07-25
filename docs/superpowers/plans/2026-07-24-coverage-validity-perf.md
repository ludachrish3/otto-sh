# Coverage Validity, Batching & Cache (Plan A) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement spec §8/§9/§10 of `docs/superpowers/specs/2026-07-24-coverage-report-ui-rework-design.md`: rename-following validity, batched git operations, supersede-on-recapture, batched fallback resolution (the §9 validity cache was descoped at the Task 9 checkpoint), and the RepoTimeline test harness — all against the *current* HTML report (no UI changes in this plan).

**Architecture:** The per-file anchor chain in `validity.py` gains a per-capture `AnchorResolver` that runs ONE tree-wide `git diff -M -w -U0 <base_commit>` (rename-detecting, whitespace-immune, working-tree-target) and answers every file's diff from that single subprocess; the existing per-file blob chain becomes the fallback for GC'd/shallow bases. Manual captures are deduplicated by (tier, label, host) with newest-wins supersede before folding. The GC'd/shallow fallback is batched the same way (hash/exists/contents/diff each run once per capture — Task 10 as amended). A `RepoTimeline` fixture builder scripts real tmp-path git repos through mutations and asserts per-line dispositions.

**Tech Stack:** Python 3.12, pydantic v2, git CLI plumbing, pytest (unit + integration), existing `otto.coverage` modules.

## Global Constraints

- Never `from __future__ import annotations` (breaks Sphinx `-W`).
- Whitespace and EOL-only changes must NEVER revoke manual credits (spec §8.1). `-w` already does this — do not remove it.
- Renames followed exactly as far as `git diff -M` reports (spec §8.2). No custom rename inference.
- Reports stay single-process; the cache has no concurrent writers (spec §9).
- All test repos live in `tmp_path` — NEVER create/mutate git state inside the dev repo.
- Per-task gates: scoped `pytest` + `uv run nox -s lint` (ruff) after every task touching `src/`; `uv run nox -s typecheck` after src-signature changes. Full `make coverage` only at final task (dev VM: no heavy parallel loads mid-plan).
- Commits in this worktree: conventional prefix + `Assisted-by: Claude Fable 5` trailer.
- Fresh-worktree prep (once, before Task 1): `uv sync` (needed for nox/ty). No `npm ci` — this plan touches no web code.
- The tree diff must use `--relative` and a `-- .` pathspec so a sut_dir nested inside a larger repo stays scoped (the `blob_sha` nested-repo bug class, fix `73be215`).

---

### Task 1: Multi-file `-U0` diff parser (`treediff.py`)

**Files:**

- Create: `src/otto/coverage/capture/treediff.py`
- Test: `tests/unit/cov/test_treediff.py`

**Interfaces:**

- Produces: `FileDiff` (frozen dataclass: `old_path: str`, `new_path: str | None`, `hunks: list[Hunk]`) and `parse_multifile_u0(diff_text: str) -> dict[str, FileDiff]` keyed by **old_path**. `new_path is None` means deleted. Task 3 consumes both. Reuses `Hunk`/`parse_u0_hunks`-style parsing from `otto.coverage.capture.remap`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/cov/test_treediff.py
"""parse_multifile_u0: split one `git diff -M -w -U0` stream into per-file diffs."""

from otto.coverage.capture.remap import Hunk
from otto.coverage.capture.treediff import FileDiff, parse_multifile_u0

MODIFIED = """\
diff --git a/src/a.c b/src/a.c
index 1111111..2222222 100644
--- a/src/a.c
+++ b/src/a.c
@@ -3,0 +4,2 @@ int f(void)
+    x();
+    y();
@@ -10 +12 @@ int g(void)
-    old();
+    new();
"""

RENAMED_CLEAN = """\
diff --git a/src/old.c b/src/new.c
similarity index 100%
rename from src/old.c
rename to src/new.c
"""

RENAMED_EDITED = """\
diff --git a/src/tcp.c b/src/net/tcp.c
similarity index 91%
rename from src/tcp.c
rename to src/net/tcp.c
index 3333333..4444444 100644
--- a/src/tcp.c
+++ b/src/net/tcp.c
@@ -7 +7 @@
-    a();
+    b();
"""

DELETED = """\
diff --git a/src/gone.c b/src/gone.c
deleted file mode 100644
index 5555555..0000000
--- a/src/gone.c
+++ /dev/null
@@ -1,3 +0,0 @@
-int gone(void)
-{
-}
"""

ADDED = """\
diff --git a/src/fresh.c b/src/fresh.c
new file mode 100644
index 0000000..6666666
--- /dev/null
+++ b/src/fresh.c
@@ -0,0 +1,2 @@
+int fresh(void)
+{
"""

QUOTED = 'diff --git "a/sp ace.c" "b/sp ace.c"\n--- "a/sp ace.c"\n+++ "b/sp ace.c"\n@@ -1 +1 @@\n-a\n+b\n'


class TestParseMultifileU0:
    def test_modified_file_keeps_all_hunks(self):
        out = parse_multifile_u0(MODIFIED)
        fd = out["src/a.c"]
        assert fd == FileDiff(
            old_path="src/a.c",
            new_path="src/a.c",
            hunks=[Hunk(3, 0, 4, 2), Hunk(10, 1, 12, 1)],
        )

    def test_clean_rename_has_no_hunks(self):
        fd = parse_multifile_u0(RENAMED_CLEAN)["src/old.c"]
        assert fd.new_path == "src/new.c"
        assert fd.hunks == []

    def test_edited_rename_keeps_hunks_under_old_path_key(self):
        out = parse_multifile_u0(RENAMED_EDITED)
        fd = out["src/tcp.c"]
        assert fd.new_path == "src/net/tcp.c"
        assert fd.hunks == [Hunk(7, 1, 7, 1)]

    def test_deleted_file_maps_to_none(self):
        fd = parse_multifile_u0(DELETED)["src/gone.c"]
        assert fd.new_path is None

    def test_added_file_is_ignored(self):
        # /dev/null old side: nothing was anchored there; not our concern.
        assert parse_multifile_u0(ADDED) == {}

    def test_quoted_paths_are_unquoted(self):
        assert "sp ace.c" in parse_multifile_u0(QUOTED)

    def test_concatenated_stream_splits_per_file(self):
        out = parse_multifile_u0(MODIFIED + RENAMED_EDITED + DELETED)
        assert set(out) == {"src/a.c", "src/tcp.c", "src/gone.c"}

    def test_empty_input(self):
        assert parse_multifile_u0("") == {}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/cov/test_treediff.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'otto.coverage.capture.treediff'`

- [ ] **Step 3: Write the implementation**

```python
# src/otto/coverage/capture/treediff.py
"""Split one tree-wide ``git diff -M -w -U0`` stream into per-file diffs.

One subprocess answers the whole anchor chain for a capture (spec §9):
rename detection (``-M``), whitespace immunity (``-w``), and per-file
hunks arrive in a single pass instead of one ``git diff`` per file.
Keys are OLD paths — the coordinates a capture is anchored in.
"""

from dataclasses import dataclass, field

from .remap import Hunk, parse_u0_hunks

_DEV_NULL = "/dev/null"


@dataclass(frozen=True)
class FileDiff:
    """One file's slice of a tree diff, in capture (old-path) terms."""

    old_path: str
    new_path: str | None  # None = deleted since base
    hunks: list[Hunk] = field(default_factory=list)


def _unquote(path: str) -> str:
    """Undo git's C-style quoting for paths with specials (best effort)."""
    if path.startswith('"') and path.endswith('"'):
        return path[1:-1].encode().decode("unicode_escape")
    return path


def _strip_side(path: str, prefix: str) -> str | None:
    path = _unquote(path)
    if path == _DEV_NULL:
        return None
    if path.startswith(prefix):
        return path[len(prefix) :]
    return path


def parse_multifile_u0(diff_text: str) -> dict[str, FileDiff]:
    """Parse a multi-file ``-U0`` diff into ``{old_path: FileDiff}``.

    Sections are delimited by ``diff --git`` headers. Old/new paths come
    from ``---``/``+++`` lines when present (they carry rename targets and
    ``/dev/null`` markers); clean renames have no hunk block, so their
    paths come from ``rename from``/``rename to`` lines instead. Pure
    additions (old side ``/dev/null``) are dropped — no capture is
    anchored in a file that did not exist at base.
    """
    out: dict[str, FileDiff] = {}
    section: list[str] = []

    def flush() -> None:
        if not section:
            return
        old: str | None = None
        new: str | None = None
        have_newline = False
        for line in section:
            if line.startswith("rename from "):
                old = _unquote(line[len("rename from ") :])
            elif line.startswith("rename to "):
                new = _unquote(line[len("rename to ") :])
            elif line.startswith("--- "):
                old = _strip_side(line[4:], "a/")
            elif line.startswith("+++ "):
                new = _strip_side(line[4:], "b/")
                have_newline = True
        if old is None:
            return  # pure addition (old side /dev/null) or unparsable
        hunks = parse_u0_hunks("\n".join(section)) if have_newline or new is None else []
        out[old] = FileDiff(old_path=old, new_path=new, hunks=hunks)

    for line in diff_text.splitlines():
        if line.startswith("diff --git "):
            flush()
            section = [line]
        elif section:
            section.append(line)
    flush()
    return out
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/cov/test_treediff.py -v`
Expected: 8 passed. If `test_clean_rename_has_no_hunks` fails on `hunks`, check the `have_newline`/rename branch — a clean rename section has neither `---` nor `@@` lines.

- [ ] **Step 5: Lint and commit**

```bash
uv run nox -s lint
git add src/otto/coverage/capture/treediff.py tests/unit/cov/test_treediff.py
git commit -m "feat(cov): multi-file -U0 tree-diff parser

Assisted-by: Claude Fable 5"
```

---

### Task 2: Tree-wide diff in gitio (+ shallow detection)

**Files:**

- Modify: `src/otto/coverage/capture/gitio.py` (append after `diff_no_index_u0`, line 116)
- Test: `tests/unit/cov/test_gitio_treediff.py`

**Interfaces:**

- Consumes: `_run` helper in `gitio.py:36`.
- Produces: `diff_tree_u0(repo_root: Path, base: str) -> str` and `is_shallow(repo_root: Path) -> bool`. Task 3 consumes both. `diff_tree_u0` raises `GitUnavailableError` when `base` is unknown (GC'd / shallow) — callers catch it to fall back.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/cov/test_gitio_treediff.py
"""diff_tree_u0: one rename-detecting, whitespace-immune diff for the whole tree."""

import subprocess
from pathlib import Path

import pytest

from otto.coverage.capture import gitio
from otto.coverage.capture.treediff import parse_multifile_u0


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
        env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
             "GIT_COMMITTER_EMAIL": "t@t", "HOME": str(cwd), "GIT_CONFIG_GLOBAL": "/dev/null",
             "GIT_CONFIG_SYSTEM": "/dev/null", "PATH": "/usr/bin:/bin"},
    ).stdout


@pytest.fixture
def repo(tmp_path: Path) -> tuple[Path, str]:
    root = tmp_path / "r"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    (root / "a.c").write_text("int a(void)\n{\n    return 1;\n}\n")
    (root / "b.c").write_text("int b(void)\n{\n    return 2;\n}\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "base")
    return root, _git(root, "rev-parse", "HEAD").strip()


class TestDiffTreeU0:
    def test_clean_tree_diffs_empty(self, repo):
        root, base = repo
        assert parse_multifile_u0(gitio.diff_tree_u0(root, base)) == {}

    def test_rename_is_detected(self, repo):
        root, base = repo
        _git(root, "mv", "a.c", "renamed.c")
        _git(root, "commit", "-qm", "mv")
        fd = parse_multifile_u0(gitio.diff_tree_u0(root, base))["a.c"]
        assert fd.new_path == "renamed.c"
        assert fd.hunks == []

    def test_whitespace_only_change_is_invisible(self, repo):
        root, base = repo
        (root / "a.c").write_text("int a(void)\n{\n        return 1;\n}\n")  # re-indent
        assert "a.c" not in parse_multifile_u0(gitio.diff_tree_u0(root, base))

    def test_worktree_edit_visible_without_commit(self, repo):
        root, base = repo
        (root / "b.c").write_text("int b(void)\n{\n    return 3;\n}\n")
        assert "b.c" in parse_multifile_u0(gitio.diff_tree_u0(root, base))

    def test_nested_repo_paths_are_relative_to_root(self, repo, tmp_path):
        # sut repo nested in an outer repo: paths must stay sut-relative
        root, base = repo
        outer = tmp_path / "outer"
        outer.mkdir()
        _git(outer, "init", "-q", "-b", "main")
        nested = outer / "sub"
        nested.mkdir()
        _git(nested, "init", "-q", "-b", "main")
        (nested / "x.c").write_text("int x;\n")
        _git(nested, "add", "-A")
        _git(nested, "commit", "-qm", "base")
        nbase = _git(nested, "rev-parse", "HEAD").strip()
        (nested / "x.c").write_text("int x = 2;\n")
        out = parse_multifile_u0(gitio.diff_tree_u0(nested, nbase))
        assert set(out) == {"x.c"}

    def test_unknown_base_raises(self, repo):
        root, _ = repo
        with pytest.raises(gitio.GitUnavailableError):
            gitio.diff_tree_u0(root, "0" * 40)


class TestIsShallow:
    def test_full_repo_is_not_shallow(self, repo):
        root, _ = repo
        assert gitio.is_shallow(root) is False

    def test_depth1_clone_is_shallow(self, repo, tmp_path):
        root, _ = repo
        _git(root, "commit", "-qm", "second", "--allow-empty")
        clone = tmp_path / "shallow"
        _git(tmp_path, "clone", "-q", "--depth", "1", f"file://{root}", str(clone))
        assert gitio.is_shallow(clone) is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/cov/test_gitio_treediff.py -v`
Expected: FAIL — `AttributeError: module ... has no attribute 'diff_tree_u0'`

- [ ] **Step 3: Append the implementation to gitio.py**

```python
def diff_tree_u0(repo_root: Path, base: str) -> str:
    """Tree-wide ``-U0`` diff of *base* vs the working tree, rename-detecting.

    One subprocess per capture replaces the per-file anchor diffs (spec
    §9). ``-M`` reports renames (spec §8.2: git's tracking is the whole
    policy); ``-w`` matches :func:`diff_worktree_file_u0`'s whitespace
    immunity (spec §8.1); ``--relative -- .`` scopes and re-roots paths
    when ``repo_root`` is nested inside a larger repository.

    Raises:
        GitUnavailableError: *base* is not a resolvable commit here
            (GC'd after a squash-merge, or absent from a shallow clone).
            Callers fall back to the per-file blob chain.
    """
    return _run(
        ["diff", "-M", "-w", "-U0", "--relative", base, "--", "."],
        repo_root,
    )


def is_shallow(repo_root: Path) -> bool:
    """True when the repo is a shallow clone (affects anchor degradation hints)."""
    return _run(["rev-parse", "--is-shallow-repository"], repo_root).strip() == "true"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/cov/test_gitio_treediff.py -v`
Expected: 8 passed.

- [ ] **Step 5: Lint and commit**

```bash
uv run nox -s lint
git add src/otto/coverage/capture/gitio.py tests/unit/cov/test_gitio_treediff.py
git commit -m "feat(cov): tree-wide rename-detecting -U0 diff + shallow detection

Assisted-by: Claude Fable 5"
```

---

### Task 3: AnchorResolver — batched anchor chain with rename following

**Files:**

- Create: `src/otto/coverage/anchor.py`
- Modify: `src/otto/coverage/validity.py` (`apply_manual_capture` at line 219, `_anchor_diff` at line 151, `_apply_unverifiable_capture` at line 190)
- Test: `tests/unit/cov/test_anchor.py`

**Interfaces:**

- Consumes: `gitio.diff_tree_u0`, `gitio.is_shallow` (Task 2), `parse_multifile_u0`/`FileDiff` (Task 1), existing `_anchor_diff` logic (moves into the resolver as the fallback).
- Produces: `class AnchorResolver` with constructor `AnchorResolver(repo_root: Path, base_commit: str)` and method `resolve(relpath: Path, fc: CaptureFileCov) -> AnchorResult`. `AnchorResult` is a frozen dataclass: `new_relpath: Path | None` (None = deleted/unverifiable), `hunks: list[Hunk]`, `verifiable: bool`. `apply_manual_capture` keeps its exact public signature — the resolver is constructed internally per capture.

Semantics table (write this as the module docstring):

| Situation | AnchorResult |
| --- | --- |
| base resolvable, file absent from tree diff | `(relpath, [], True)` — unchanged (or whitespace-only) |
| base resolvable, modified in place | `(relpath, hunks, True)` |
| base resolvable, renamed (clean or edited) | `(new_path, hunks, True)` — credits land at the NEW path |
| base resolvable, deleted | `(None, [], False)` → whole-file unverifiable/stale at old coordinates |
| base NOT resolvable (GC'd/shallow) | per-file fallback: blob fast-path → blob diff → unverifiable (today's `_anchor_diff` chain, unchanged) |

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/cov/test_anchor.py
"""AnchorResolver: one tree diff answers the whole capture; blob chain is the fallback."""

import subprocess
from pathlib import Path

import pytest

from otto.coverage.anchor import AnchorResolver
from otto.coverage.capture import gitio
from otto.coverage.capture.model import CaptureFileCov


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
        env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
             "GIT_COMMITTER_EMAIL": "t@t", "HOME": str(cwd), "GIT_CONFIG_GLOBAL": "/dev/null",
             "GIT_CONFIG_SYSTEM": "/dev/null", "PATH": "/usr/bin:/bin"},
    ).stdout


@pytest.fixture
def repo(tmp_path: Path):
    root = tmp_path / "r"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    (root / "a.c").write_text("l1\nl2\nl3\nl4\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "base")
    base = _git(root, "rev-parse", "HEAD").strip()
    blob = gitio.blob_sha(root, Path("a.c"))
    return root, base, CaptureFileCov(blob=blob, lines={1: 5, 3: 2})


class TestTreeDiffPath:
    def test_unchanged_file_is_verbatim(self, repo):
        root, base, fc = repo
        r = AnchorResolver(root, base).resolve(Path("a.c"), fc)
        assert (r.new_relpath, r.hunks, r.verifiable) == (Path("a.c"), [], True)

    def test_clean_rename_redirects_path(self, repo):
        root, base, fc = repo
        _git(root, "mv", "a.c", "moved.c")
        _git(root, "commit", "-qm", "mv")
        r = AnchorResolver(root, base).resolve(Path("a.c"), fc)
        assert r.new_relpath == Path("moved.c")
        assert r.verifiable and r.hunks == []

    def test_edit_produces_hunks(self, repo):
        root, base, fc = repo
        (root / "a.c").write_text("l1\nCHANGED\nl3\nl4\n")
        r = AnchorResolver(root, base).resolve(Path("a.c"), fc)
        assert r.verifiable and len(r.hunks) == 1

    def test_deleted_file_is_unverifiable(self, repo):
        root, base, fc = repo
        _git(root, "rm", "-q", "a.c")
        _git(root, "commit", "-qm", "rm")
        r = AnchorResolver(root, base).resolve(Path("a.c"), fc)
        assert not r.verifiable and r.new_relpath is None


class TestFallbackPath:
    def test_missing_base_with_matching_blob_is_verbatim(self, repo):
        # Squash-merge shape: base commit gone, file content unchanged.
        root, base, fc = repo
        r = AnchorResolver(root, "0" * 40).resolve(Path("a.c"), fc)
        assert (r.new_relpath, r.hunks, r.verifiable) == (Path("a.c"), [], True)

    def test_missing_base_and_changed_blob_diffs_via_blob(self, repo):
        root, base, fc = repo
        (root / "a.c").write_text("l1\nCHANGED\nl3\nl4\n")
        r = AnchorResolver(root, "0" * 40).resolve(Path("a.c"), fc)
        assert r.verifiable and len(r.hunks) == 1

    def test_missing_base_and_missing_blob_is_unverifiable(self, repo):
        root, base, _ = repo
        fc = CaptureFileCov(blob="1" * 40, lines={1: 1})
        (root / "a.c").write_text("different\n")
        r = AnchorResolver(root, "0" * 40).resolve(Path("a.c"), fc)
        assert not r.verifiable
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/cov/test_anchor.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'otto.coverage.anchor'`

- [ ] **Step 3: Write `anchor.py`**

```python
# src/otto/coverage/anchor.py
"""Per-capture anchor resolution: one tree diff, per-file blob fallback (spec §8/§9).

Semantics table lives in AnchorResolver's docstring; keep it in sync
with tests/unit/cov/test_anchor.py.
"""

import logging
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from .capture import gitio
from .capture.model import CaptureFileCov
from .capture.remap import Hunk, parse_u0_hunks
from .capture.treediff import FileDiff, parse_multifile_u0

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AnchorResult:
    """Where one file's capture coordinates land in the current tree."""

    new_relpath: Path | None  # None = deleted or unverifiable
    hunks: list[Hunk] = field(default_factory=list)
    verifiable: bool = True


class AnchorResolver:
    """Resolve every file of one capture against the working tree.

    | situation                         | result                                  |
    | base ok, absent from tree diff    | (relpath, [], True) — unchanged/ws-only |
    | base ok, modified                 | (relpath, hunks, True)                  |
    | base ok, renamed                  | (new_path, hunks, True)                 |
    | base ok, deleted                  | (None, [], False)                       |
    | base missing (GC'd / shallow)     | per-file blob chain fallback            |
    """

    def __init__(self, repo_root: Path, base_commit: str) -> None:
        self._root = repo_root
        self._base = base_commit
        self._tree: dict[str, FileDiff] | None = None
        try:
            self._tree = parse_multifile_u0(gitio.diff_tree_u0(repo_root, base_commit))
        except gitio.GitUnavailableError as e:
            if "not a git repository" in str(e):
                raise
            shallow_hint = (
                " (shallow clone — deepen with `git fetch --unshallow` to recover history)"
                if gitio.is_shallow(repo_root)
                else ""
            )
            logger.warning(
                "base_commit %s is not resolvable%s; falling back to per-file blob anchors.",
                base_commit[:12],
                shallow_hint,
            )

    def resolve(self, relpath: Path, fc: CaptureFileCov) -> AnchorResult:
        if self._tree is None:
            return self._resolve_by_blob(relpath, fc)
        fd = self._tree.get(relpath.as_posix())
        if fd is None:
            # Not in the diff: unchanged (or whitespace-only) — but only
            # verifiable if the file actually exists here (a capture from
            # a different repo names paths the diff never saw).
            if (self._root / relpath).is_file():
                return AnchorResult(new_relpath=relpath)
            return AnchorResult(new_relpath=None, verifiable=False)
        if fd.new_path is None:
            return AnchorResult(new_relpath=None, verifiable=False)
        return AnchorResult(new_relpath=Path(fd.new_path), hunks=fd.hunks)

    def _resolve_by_blob(self, relpath: Path, fc: CaptureFileCov) -> AnchorResult:
        """Today's per-file chain: blob fast-path → blob diff → unverifiable."""
        current = self._root / relpath
        if not current.is_file():
            return AnchorResult(new_relpath=None, verifiable=False)
        if fc.blob and gitio.hash_object(self._root, current) == fc.blob:
            return AnchorResult(new_relpath=relpath)
        base_blob = fc.blob if fc.blob and gitio.blob_exists(self._root, fc.blob) else None
        if base_blob is None:
            base_blob = gitio.blob_sha(self._root, relpath, rev=self._base)
        if base_blob is None:
            return AnchorResult(new_relpath=None, verifiable=False)
        with tempfile.NamedTemporaryFile(suffix=relpath.suffix) as tmp:
            Path(tmp.name).write_bytes(gitio.cat_blob(self._root, base_blob))
            diff = gitio.diff_no_index_u0(Path(tmp.name), current)
        return AnchorResult(new_relpath=relpath, hunks=parse_u0_hunks(diff))
```

- [ ] **Step 4: Rewire `apply_manual_capture` (validity.py)**

Replace the loop body of `apply_manual_capture` (validity.py:233-239) so the resolver is built once per capture and per-file results flow through it. The remainder of the function (remap, stale/aging marking) is unchanged except that `file_rec` uses the **resolved** path and `LineRemapper` takes `result.hunks` directly:

```python
    resolver = AnchorResolver(repo_root, capture.base_commit)
    for rel_str, fc in capture.files.items():
        relpath = Path(rel_str)
        result = resolver.resolve(relpath, fc)
        if not result.verifiable:
            file_rec = store.get_or_create_file(repo_root / relpath)
            _apply_unverifiable_capture(file_rec, capture, rel_str, fc, run_id)
            continue
        file_rec = store.get_or_create_file(repo_root / result.new_relpath)
        remapper = LineRemapper(result.hunks)
```

Add the import at the top of validity.py (`from .anchor import AnchorResolver`), delete the now-unused `_anchor_diff` function and its `tempfile` import, and update `load_dirty_capture_into_store` NOT AT ALL (e2e dirty path is per-file against HEAD and stays as-is in this plan). Extend `_apply_unverifiable_capture`'s warning to mention deletion vs missing anchors generically: change the message to `"Manual capture %s/%s is unverifiable (file deleted, or base_commit %s and blob missing) — treating as stale; re-capture to refresh."` and keep everything else identical.

- [ ] **Step 5: Run the full validity + anchor test set**

Run: `uv run pytest tests/unit/cov/test_anchor.py tests/unit/cov/test_validity.py tests/unit/cov/test_remap.py tests/integration/cov -v`
Expected: all pass. `test_validity.py` exercises the old semantics through the new resolver — any failure here is a behavior regression, not a test to update. Two allowed exceptions: (a) a test asserting the *warning text* updated in Step 4 — align that string only; (b) tests that import `_anchor_diff` directly — port each one to the equivalent `AnchorResolver.resolve` call in this same commit, preserving its scenario and expected outcome verbatim (behavior-preserving port, not a rewrite).

- [ ] **Step 6: Typecheck, lint, commit**

```bash
uv run nox -s lint && uv run nox -s typecheck
git add src/otto/coverage/anchor.py src/otto/coverage/validity.py tests/unit/cov/test_anchor.py
git commit -m "feat(cov): batched AnchorResolver with rename following + shallow hint

Assisted-by: Claude Fable 5"
```

---

### Task 4: Git-subprocess-count regression gate

**Files:**

- Create: `tests/unit/cov/test_git_spawn_budget.py`

**Interfaces:**

- Consumes: `gitio._run_raw` (the single subprocess chokepoint, gitio.py:16), `apply_manual_capture`, `AnchorResolver`.

- [ ] **Step 1: Write the gate**

```python
# tests/unit/cov/test_git_spawn_budget.py
"""Pin O(1) git subprocesses per capture — not O(files) (spec §9).

The counter wraps gitio._run_raw, the single chokepoint every helper
funnels through. Budget: resolver construction = 1 (tree diff) with +1
allowed for the shallow probe on the fallback path; per-file resolves
on the tree path spawn ZERO except the existence stat (not git).
"""

import subprocess
from pathlib import Path

import pytest

from otto.coverage.capture import gitio
from otto.coverage.capture.model import Capture, CaptureFileCov
from otto.coverage.store.model import CoverageStore
from otto.coverage.validity import apply_manual_capture

N_FILES = 50


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
        env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
             "GIT_COMMITTER_EMAIL": "t@t", "HOME": str(cwd), "GIT_CONFIG_GLOBAL": "/dev/null",
             "GIT_CONFIG_SYSTEM": "/dev/null", "PATH": "/usr/bin:/bin"},
    ).stdout


@pytest.fixture
def counted_git(monkeypatch):
    calls: list[list[str]] = []
    real = gitio._run_raw

    def counting(args, cwd, ok_codes=(0,)):
        calls.append(list(args))
        return real(args, cwd, ok_codes)

    monkeypatch.setattr(gitio, "_run_raw", counting)
    return calls


@pytest.fixture
def big_repo(tmp_path: Path):
    root = tmp_path / "r"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    for i in range(N_FILES):
        (root / f"f{i:03}.c").write_text(f"int f{i}(void)\n{{\n    return {i};\n}}\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "base")
    base = _git(root, "rev-parse", "HEAD").strip()
    files = {
        f"f{i:03}.c": CaptureFileCov(
            blob=gitio.blob_sha(root, Path(f"f{i:03}.c")), lines={3: 1}
        )
        for i in range(N_FILES)
    }
    cap = Capture(
        tier="manual", base_commit=base, captured_at="2026-07-01T00:00:00Z",
        board="bench-1", files=files,
    )
    return root, cap


def test_fold_spawns_constant_git_calls(counted_git, big_repo):
    root, cap = big_repo
    # Touch a handful of files so the tree diff is non-trivial.
    for i in (3, 7):
        (root / f"f{i:03}.c").write_text(f"int f{i}(void)\n{{\n    return -1;\n}}\n")
    counted_git.clear()
    store = CoverageStore()
    apply_manual_capture(store, cap, root, max_age_days=None)
    assert len(counted_git) <= 2, [" ".join(c) for c in counted_git]
```

- [ ] **Step 2: Run and verify it passes**

Run: `uv run pytest tests/unit/cov/test_git_spawn_budget.py -v`
Expected: PASS with 1 call (`diff -M -w -U0 …`).

- [ ] **Step 3: Prove the gate is red against pre-batch behavior**

A regression guard must be proven red against the code it guards (project rule). Temporarily force the fallback path:

```bash
uv run python - <<'EOF'
# Simulate pre-batch behavior: resolver with an unresolvable base ⇒ per-file chain.
import subprocess, tempfile
from pathlib import Path
from otto.coverage.capture import gitio
calls = []
real = gitio._run_raw
gitio._run_raw = lambda a, c, ok=(0,): (calls.append(a), real(a, c, ok))[1]
# ... (reuse the big_repo shape at N=10 inline; expect >> 2 calls via _resolve_by_blob)
EOF
```

Simpler equivalent accepted: temporarily edit `AnchorResolver.__init__` to set `self._tree = None` unconditionally, run the test, observe FAIL with ~`3×N_FILES` calls, revert the edit, re-run, observe PASS. Record the red-run call count in the commit message.

- [ ] **Step 4: Lint and commit**

```bash
uv run nox -s lint
git add tests/unit/cov/test_git_spawn_budget.py
git commit -m "test(cov): pin O(1) git spawns per capture fold (red at ~150 calls unbatched)

Assisted-by: Claude Fable 5"
```

---

### Task 5: Supersede-on-recapture (spec §8.5)

**Files:**

- Create: `src/otto/coverage/capture/supersede.py`
- Modify: `src/otto/coverage/reporter.py` (`_load_manual_store`, line 598; `run` step 3b, line 410)
- Test: `tests/unit/cov/test_supersede.py`

**Interfaces:**

- Consumes: `Capture` model.
- Produces: `select_manual_captures(captures: list[Capture]) -> list[Capture]` — winners only, input order preserved for winners; logs each superseded capture at INFO. Identity key = `(capture.tier, capture.display_name or capture.board, capture.board)` (label falls back to board exactly as `register_capture_run` does; the explicit `host` field arrives in Plan B and will slot into this key). Newest `captured_at` (ISO-8601 strings — lexicographic order is chronological; empty string loses to any date) wins; on a tie the later list entry (later filename in the sorted store) wins.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/cov/test_supersede.py
"""Same-context re-capture supersedes: newest (tier, label, host) wins (spec §8.5)."""

from otto.coverage.capture.model import Capture
from otto.coverage.capture.supersede import select_manual_captures


def _cap(captured_at: str, board: str = "bench-1", tier: str = "manual",
         display_name: str | None = None, ticket: str | None = None) -> Capture:
    return Capture(tier=tier, base_commit="c" * 40, captured_at=captured_at,
                   board=board, display_name=display_name, ticket=ticket)


class TestSelectManualCaptures:
    def test_newest_same_context_wins(self):
        old, new = _cap("2026-06-01T00:00:00Z"), _cap("2026-07-01T00:00:00Z")
        assert select_manual_captures([old, new]) == [new]
        assert select_manual_captures([new, old]) == [new]

    def test_different_hosts_both_survive(self):
        a, b = _cap("2026-06-01T00:00:00Z", board="bench-1"), _cap("2026-06-01T00:00:00Z", board="bench-2")
        assert select_manual_captures([a, b]) == [a, b]

    def test_different_labels_both_survive(self):
        a = _cap("2026-06-01T00:00:00Z", display_name="bring-up")
        b = _cap("2026-06-02T00:00:00Z", display_name="cert-sweep")
        assert select_manual_captures([a, b]) == [a, b]

    def test_blank_captured_at_loses_to_dated(self):
        blank, dated = _cap(""), _cap("2026-01-01T00:00:00Z")
        assert select_manual_captures([dated, blank]) == [dated]

    def test_tie_keeps_later_entry(self):
        a, b = _cap("2026-06-01T00:00:00Z", ticket="A"), _cap("2026-06-01T00:00:00Z", ticket="B")
        assert select_manual_captures([a, b]) == [b]

    def test_superseded_is_logged(self, caplog):
        import logging
        old, new = _cap("2026-06-01T00:00:00Z"), _cap("2026-07-01T00:00:00Z")
        with caplog.at_level(logging.INFO):
            select_manual_captures([old, new])
        assert "supersed" in caplog.text.lower()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/cov/test_supersede.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Write the implementation**

```python
# src/otto/coverage/capture/supersede.py
"""Same-context re-capture supersedes (spec §8.5): newest wins, never accumulate.

Accumulating two captures of the same (tier, label, host) context would
double-count that context's coverage; the superseded capture drops out
of the runs table entirely. Plan B's explicit ``host`` field will
replace the board component of the key.
"""

import logging

from .model import Capture

logger = logging.getLogger(__name__)


def _key(cap: Capture) -> tuple[str, str, str]:
    return (cap.tier, cap.display_name or cap.board, cap.board)


def select_manual_captures(captures: list[Capture]) -> list[Capture]:
    """Return winners (input order), newest ``captured_at`` per context key."""
    winners: dict[tuple[str, str, str], Capture] = {}
    for cap in captures:
        key = _key(cap)
        prev = winners.get(key)
        if prev is not None and prev.captured_at > cap.captured_at:
            logger.info(
                "Superseded manual capture %s@%s (context %s): newer capture %s kept.",
                cap.ticket or "no-ticket", cap.captured_at or "undated", key[1],
                prev.captured_at,
            )
            continue
        if prev is not None:
            logger.info(
                "Superseded manual capture %s@%s (context %s): newer capture %s kept.",
                prev.ticket or "no-ticket", prev.captured_at or "undated", key[1],
                cap.captured_at,
            )
        winners[key] = cap
    keep = set(map(id, winners.values()))
    return [c for c in captures if id(c) in keep]
```

- [ ] **Step 4: Wire into the reporter**

In `reporter.py` `run()` step 3b (line 410), select winners once and use them for BOTH the dedupe seed and the fold — superseded captures must not seed `seen_runs` (a fresh cov-dir copy of a *superseded* run must also not fold as e2e; it is the same dead context):

```python
            from .capture.supersede import select_manual_captures

            manual_captures = select_manual_captures(self._manual_captures())
```

(the two following lines — `seen_runs` seeding and the later `self._load_manual_store(store, manual_captures)` — are unchanged; they now see winners only).

- [ ] **Step 5: Run the affected suites**

Run: `uv run pytest tests/unit/cov/test_supersede.py tests/unit/cov -k "reporter or pipeline" tests/integration/cov -v`
Expected: all pass.

- [ ] **Step 6: Lint, typecheck, commit**

```bash
uv run nox -s lint && uv run nox -s typecheck
git add src/otto/coverage/capture/supersede.py src/otto/coverage/reporter.py tests/unit/cov/test_supersede.py
git commit -m "feat(cov): supersede same-context manual re-captures, newest wins

Assisted-by: Claude Fable 5"
```

---

### Task 6: RepoTimeline harness + movement/whitespace cases

**Files:**

- Create: `tests/_fixtures/_repo_timeline.py`
- Create: `tests/integration/cov/test_repo_timeline.py`

**Interfaces:**

- Consumes: `Capture`/`CaptureFileCov`, `gitio`, `CoverageStore`, `apply_manual_capture`, `register_capture_run`.
- Produces (for Tasks 7–8): `class RepoTimeline` — `write(rel, text)`, `commit(msg) -> sha`, `capture(label, lines, *, tier="manual", host="bench-1", captured_at="2026-07-01T00:00:00Z", ticket=None) -> Capture` (anchored at current HEAD, blob-stamped), `fold(*, max_age_days=None, today=None) -> CoverageStore` (registers + folds every captured Capture in order), `dispositions(store, rel) -> dict[int, str]` with values `"hit"` (any-tier hit), `"stale"`, `"aging"`, `"none"` (record exists, no hit/state). Plus `self.root: Path` and `self.git(*args) -> str` for arbitrary mutations.

- [ ] **Step 1: Write the harness**

```python
# tests/_fixtures/_repo_timeline.py
"""Script a git repo through time and assert per-line coverage dispositions.

The heart of spec §10: each aging-repo scenario is `commit → capture →
mutate → fold → assert`. Folding goes straight through
``apply_manual_capture`` (the unit under test); the full reporter
pipeline is exercised elsewhere (test_capture_report_cycle).
"""

import subprocess
from datetime import datetime
from pathlib import Path

from otto.coverage.capture import gitio
from otto.coverage.capture.model import Capture, CaptureFileCov
from otto.coverage.store.model import CoverageStore
from otto.coverage.validity import apply_manual_capture, register_capture_run

_GIT_ENV = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
    "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null",
    "GIT_AUTHOR_DATE": "2026-01-01T00:00:00Z", "GIT_COMMITTER_DATE": "2026-01-01T00:00:00Z",
    "PATH": "/usr/bin:/bin",
}


class RepoTimeline:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.captures: list[Capture] = []
        self.git("init", "-q", "-b", "main")

    def git(self, *args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=self.root, check=True, capture_output=True,
            text=True, env={**_GIT_ENV, "HOME": str(self.root)},
        ).stdout

    def write(self, rel: str, text: str) -> None:
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)

    def commit(self, msg: str = "c") -> str:
        self.git("add", "-A")
        self.git("commit", "-qm", msg, "--allow-empty")
        return self.git("rev-parse", "HEAD").strip()

    def capture(
        self,
        label: str,
        lines: dict[str, dict[int, int]],
        *,
        tier: str = "manual",
        host: str = "bench-1",
        captured_at: str = "2026-07-01T00:00:00Z",
        ticket: str | None = None,
    ) -> Capture:
        files = {
            rel: CaptureFileCov(
                blob=gitio.blob_sha(self.root, Path(rel)), lines=dict(hits)
            )
            for rel, hits in lines.items()
        }
        cap = Capture(
            tier=tier, base_commit=gitio.head_commit(self.root),
            captured_at=captured_at, board=host, display_name=label,
            ticket=ticket, files=files,
        )
        self.captures.append(cap)
        return cap

    def fold(
        self, *, max_age_days: int | None = None, today: datetime | None = None
    ) -> CoverageStore:
        store = CoverageStore()
        for cap in self.captures:
            run_id = register_capture_run(store, cap)
            apply_manual_capture(
                store, cap, self.root, max_age_days=max_age_days,
                today=today, run_id=run_id,
            )
        return store

    def dispositions(self, store: CoverageStore, rel: str) -> dict[int, str]:
        rec = store.get_or_create_file(self.root / rel)
        out: dict[int, str] = {}
        for lineno, lr in sorted(rec.lines.items()):
            if lr.hits.is_hit():
                out[lineno] = "hit"
            elif lr.state in ("stale", "aging"):
                out[lineno] = lr.state
            else:
                out[lineno] = "none"
        return out
```

- [ ] **Step 2: Write the movement/whitespace cases (failing first run is fine — they must PASS against Tasks 1–3's code; any failure is a bug found)**

```python
# tests/integration/cov/test_repo_timeline.py
"""Aging-repo scenarios (spec §10) — movement, whitespace, EOL."""

from pathlib import Path

import pytest

from tests._fixtures._repo_timeline import RepoTimeline

SRC = "int a(void)\n{\n    run();\n    more();\n}\n"


@pytest.fixture
def tl(tmp_path: Path) -> RepoTimeline:
    t = RepoTimeline(tmp_path / "repo")
    t.write("a.c", SRC)
    t.commit("base")
    return t


class TestLineMovement:
    def test_insert_above_shifts_credits(self, tl):
        tl.capture("run1", {"a.c": {3: 5, 4: 2}})
        tl.write("a.c", "/* new */\n/* new */\n" + SRC)
        tl.commit("insert above")
        d = tl.dispositions(tl.fold(), "a.c")
        assert d == {5: "hit", 6: "hit"}

    def test_delete_above_shifts_credits_up(self, tl):
        tl.write("a.c", "/* hdr */\n" + SRC)
        tl.commit("with header")
        tl.capture("run1", {"a.c": {4: 5}})
        tl.write("a.c", SRC)
        tl.commit("drop header")
        assert tl.dispositions(tl.fold(), "a.c") == {3: "hit"}

    def test_edit_inside_revokes_only_edited_lines(self, tl):
        tl.capture("run1", {"a.c": {3: 5, 4: 2}})
        tl.write("a.c", "int a(void)\n{\n    changed();\n    more();\n}\n")
        tl.commit("edit line 3")
        d = tl.dispositions(tl.fold(), "a.c")
        assert d[3] == "stale"
        assert d[4] == "hit"


class TestWhitespaceImmunity:
    """Spec §8.1: whitespace/EOL changes NEVER revoke."""

    def test_reindent_keeps_all_credits(self, tl):
        tl.capture("run1", {"a.c": {3: 5, 4: 2}})
        tl.write("a.c", "int a(void)\n{\n        run();\n        more();\n}\n")
        tl.commit("reformat")
        assert tl.dispositions(tl.fold(), "a.c") == {3: "hit", 4: "hit"}

    def test_crlf_conversion_keeps_all_credits(self, tl):
        tl.capture("run1", {"a.c": {3: 5}})
        (tl.root / "a.c").write_bytes(SRC.replace("\n", "\r\n").encode())
        tl.commit("crlf")
        assert tl.dispositions(tl.fold(), "a.c")[3] == "hit"

    def test_mixed_ws_and_real_edit_revokes_only_real(self, tl):
        tl.capture("run1", {"a.c": {3: 5, 4: 2}})
        tl.write("a.c", "int a(void)\n{\n        run();\n    other();\n}\n")
        tl.commit("reindent 3, edit 4")
        d = tl.dispositions(tl.fold(), "a.c")
        assert d[3] == "hit"
        assert d[4] == "stale"
```

- [ ] **Step 3: Run**

Run: `uv run pytest tests/integration/cov/test_repo_timeline.py -v`
Expected: 6 passed. A failure in `TestWhitespaceImmunity` means the `-w` flag regressed somewhere in Tasks 2–3 — fix the source, not the test.

- [ ] **Step 4: Lint and commit**

```bash
uv run nox -s lint
git add tests/_fixtures/_repo_timeline.py tests/integration/cov/test_repo_timeline.py
git commit -m "test(cov): RepoTimeline harness + movement/whitespace timeline cases

Assisted-by: Claude Fable 5"
```

---

### Task 7: Timeline cases — identity & history shape

**Files:**

- Modify: `tests/integration/cov/test_repo_timeline.py` (append classes)

**Interfaces:**

- Consumes: `RepoTimeline` (Task 6) exactly as defined; no new production code expected — these pin Tasks 2–3 against real git history shapes.

- [ ] **Step 1: Append the cases**

```python
class TestFileIdentity:
    def test_clean_rename_follows(self, tl):
        tl.capture("run1", {"a.c": {3: 5}})
        tl.git("mv", "a.c", "b.c")
        tl.commit("mv")
        assert tl.dispositions(tl.fold(), "b.c")[3] == "hit"

    def test_rename_plus_edit_follows_unchanged_lines(self, tl):
        tl.capture("run1", {"a.c": {3: 5, 4: 2}})
        tl.git("mv", "a.c", "b.c")
        tl.write("b.c", "int a(void)\n{\n    run();\n    edited();\n}\n")
        tl.commit("mv+edit")
        d = tl.dispositions(tl.fold(), "b.c")
        assert d[3] == "hit"
        assert tl.dispositions(tl.fold(), "a.c")[4] == "stale"  # stale stays at OLD coords

    def test_deleted_file_goes_stale_not_crash(self, tl):
        tl.capture("run1", {"a.c": {3: 5}})
        tl.git("rm", "-q", "a.c")
        tl.commit("rm")
        assert tl.dispositions(tl.fold(), "a.c")[3] == "stale"


class TestHistoryShape:
    def test_squash_merge_survival_via_blob(self, tl, tmp_path):
        """Spec §10: capture on branch → squash → branch deleted → gc → credits live."""
        tl.git("checkout", "-qb", "feature")
        tl.write("a.c", SRC + "int extra(void)\n{\n}\n")
        tl.commit("feature work")
        tl.capture("run1", {"a.c": {3: 5, 6: 1}})
        tl.git("checkout", "-q", "main")
        tl.git("merge", "--squash", "-q", "feature")
        tl.commit("squashed")
        tl.git("branch", "-qD", "feature")
        tl.git("reflog", "expire", "--expire=now", "--all")
        tl.git("gc", "--prune=now", "-q")
        d = tl.dispositions(tl.fold(), "a.c")
        assert d[3] == "hit"
        assert d[6] == "hit"

    def test_base_gone_and_blob_changed_is_stale_not_crash(self, tl):
        tl.git("checkout", "-qb", "feature")
        tl.write("a.c", "int a(void)\n{\n    feature_only();\n}\n")
        tl.commit("feature")
        tl.capture("run1", {"a.c": {3: 7}})
        tl.git("checkout", "-q", "main")   # main never sees the feature blob
        tl.git("branch", "-qD", "feature")
        tl.git("reflog", "expire", "--expire=now", "--all")
        tl.git("gc", "--prune=now", "-q")
        assert tl.dispositions(tl.fold(), "a.c")[3] == "stale"

    def test_shallow_clone_degrades_with_hint(self, tl, tmp_path, caplog):
        import logging

        tl.capture("run1", {"a.c": {3: 5}})
        tl.write("a.c", SRC + "// trailing\n")
        tl.commit("second")
        clone_root = tmp_path / "shallow"
        tl.git("clone", "-q", "--depth", "1", f"file://{tl.root}", str(clone_root))
        shallow = RepoTimeline.__new__(RepoTimeline)  # adopt existing clone
        shallow.root = clone_root
        shallow.captures = tl.captures
        with caplog.at_level(logging.WARNING):
            store = shallow.fold()
        assert "shallow clone" in caplog.text
        # Content still matches at line 3? blob fast-path may still save it:
        # the capture blob is unreachable in a depth-1 clone, so this stays
        # stale — pin the degradation, not a miracle.
        assert shallow.dispositions(store, "a.c")[3] == "stale"

    def test_revert_resurrects_credits(self, tl):
        """Spec §8.3: pinned so nobody 'fixes' resurrection away."""
        tl.capture("run1", {"a.c": {3: 5}})
        tl.write("a.c", "int a(void)\n{\n    other();\n    more();\n}\n")
        tl.commit("break it")
        assert tl.dispositions(tl.fold(), "a.c")[3] == "stale"
        tl.write("a.c", SRC)
        tl.commit("revert")
        assert tl.dispositions(tl.fold(), "a.c")[3] == "hit"
```

Note on the shallow-clone case: if `git clone --depth 1 file://` keeps the capture blob reachable on your git version and the fold comes back `"hit"`, that is git being generous, not a bug — flip the final assertion to accept `"hit"` and instead pin ONLY the warning text (`"shallow clone"` present). Decide by running it; document the observed behavior in the test docstring.

- [ ] **Step 2: Run**

Run: `uv run pytest tests/integration/cov/test_repo_timeline.py -v`
Expected: all pass (see the shallow-clone note above for the one environment-dependent assertion).

- [ ] **Step 3: Lint and commit**

```bash
uv run nox -s lint
git add tests/integration/cov/test_repo_timeline.py
git commit -m "test(cov): identity + history-shape timeline cases (squash, gc, shallow, revert)

Assisted-by: Claude Fable 5"
```

---

### Task 8: Timeline cases — time, multi-capture, oddities, golden run

**Files:**

- Modify: `tests/integration/cov/test_repo_timeline.py` (append classes)

**Interfaces:**

- Consumes: `RepoTimeline.fold(max_age_days=, today=)` passthrough to `apply_manual_capture`; `select_manual_captures` (Task 5).

- [ ] **Step 1: Append the cases**

```python
class TestTime:
    def test_aging_boundary_is_strictly_greater(self, tl):
        from datetime import datetime, timezone

        tl.capture("run1", {"a.c": {3: 5}}, captured_at="2026-06-01T00:00:00Z")
        at_limit = datetime(2026, 7, 1, 0, 0, 0, tzinfo=timezone.utc)     # exactly 30d
        past_limit = datetime(2026, 7, 2, 0, 0, 0, tzinfo=timezone.utc)   # 31d
        assert tl.dispositions(tl.fold(max_age_days=30, today=at_limit), "a.c")[3] == "hit"
        d = tl.dispositions(tl.fold(max_age_days=30, today=past_limit), "a.c")
        assert d[3] == "hit"  # aging lines still carry hits...
        store = tl.fold(max_age_days=30, today=past_limit)
        rec = store.get_or_create_file(tl.root / "a.c")
        assert rec.lines[3].state == "aging"  # ...but the state marks them

    def test_future_dated_capture_is_not_aging(self, tl):
        from datetime import datetime, timezone

        tl.capture("run1", {"a.c": {3: 5}}, captured_at="2027-01-01T00:00:00Z")
        today = datetime(2026, 7, 1, tzinfo=timezone.utc)
        store = tl.fold(max_age_days=30, today=today)
        assert store.get_or_create_file(tl.root / "a.c").lines[3].state is None


class TestMultiCapture:
    def test_overlap_one_valid_one_stale_no_double_count(self, tl):
        tl.capture("old", {"a.c": {3: 5}}, host="bench-1")
        tl.write("a.c", "int a(void)\n{\n    run();\n    other();\n}\n")  # line 4 changes
        tl.commit("edit 4")
        tl.capture("new", {"a.c": {3: 2, 4: 9}}, host="bench-2",
                   captured_at="2026-07-02T00:00:00Z")
        store = tl.fold()
        rec = store.get_or_create_file(tl.root / "a.c")
        assert rec.lines[3].hits.total() == 7          # 5 + 2, both credited once
        assert len(rec.lines[3].run_hits) == 2

    def test_supersede_visible_end_to_end(self, tl):
        from otto.coverage.capture.supersede import select_manual_captures

        tl.capture("run1", {"a.c": {3: 5}}, captured_at="2026-06-01T00:00:00Z")
        tl.capture("run1", {"a.c": {3: 9}}, captured_at="2026-07-01T00:00:00Z")
        winners = select_manual_captures(tl.captures)
        assert len(winners) == 1 and winners[0].captured_at == "2026-07-01T00:00:00Z"

    def test_wrong_repo_capture_warns_and_stales(self, tl, caplog):
        import logging

        tl.capture("run1", {"never/existed.c": {1: 3}})
        with caplog.at_level(logging.WARNING):
            store = tl.fold()
        assert "unverifiable" in caplog.text
        assert tl.dispositions(store, "never/existed.c")[1] == "stale"


class TestContentOddities:
    def test_non_utf8_source_folds_without_crash(self, tl):
        (tl.root / "l1.c").write_bytes(b"int a;\n/* caf\xe9 */\nint b;\n")
        tl.commit("latin1")
        tl.capture("run1", {"l1.c": {1: 2, 3: 1}})
        assert tl.dispositions(tl.fold(), "l1.c") == {1: "hit", 3: "hit"}

    def test_hits_past_eof_after_shrink_stay_stale_records(self, tl):
        tl.capture("run1", {"a.c": {3: 5, 5: 2}})
        tl.write("a.c", "int a(void)\n{\n    run();\n}\n")  # 4 lines now
        tl.commit("shrink")
        d = tl.dispositions(tl.fold(), "a.c")
        assert d[3] == "hit"
        assert d[5] == "stale"


def test_golden_mixed_timeline(tmp_path):
    """Months of history in one run: every mechanism at once, aggregate sanity."""
    tl = RepoTimeline(tmp_path / "repo")
    tl.write("core.c", SRC)
    tl.write("util.c", "int u(void)\n{\n    helper();\n}\n")
    tl.commit("v1")
    tl.capture("bring-up", {"core.c": {3: 4, 4: 4}, "util.c": {3: 8}},
               captured_at="2026-01-10T00:00:00Z")
    # month 2: reformat core (must not revoke), rename util (must follow)
    tl.write("core.c", SRC.replace("    ", "\t"))
    tl.git("mv", "util.c", "helpers.c")
    tl.commit("v2 reformat+rename")
    # month 3: real edit to core line 4; fresh re-capture of core only
    tl.write("core.c", "int a(void)\n{\n\trun();\n\tredone();\n}\n")
    tl.commit("v3 edit")
    tl.capture("bring-up", {"core.c": {3: 6, 4: 6}}, captured_at="2026-03-01T00:00:00Z")
    from otto.coverage.capture.supersede import select_manual_captures

    tl.captures = select_manual_captures(tl.captures)
    assert len(tl.captures) == 1  # same label+host: month-3 capture superseded month-1
    store = tl.fold()
    assert tl.dispositions(store, "core.c") == {3: "hit", 4: "hit"}
    # the superseded January capture no longer credits helpers.c at all:
    assert tl.dispositions(store, "helpers.c") == {}
```

Note the golden run's teaching point: supersede is by (tier, label, host) — the month-3 partial re-capture *supersedes the whole January capture*, dropping `helpers.c` credits. That is the spec §8.5 semantic, made visible. If Chris wants per-file supersede instead, that is a spec change to raise at review — do not soften the test.

- [ ] **Step 2: Run the full timeline file**

Run: `uv run pytest tests/integration/cov/test_repo_timeline.py -v`
Expected: all pass.

- [ ] **Step 3: Lint and commit**

```bash
uv run nox -s lint
git add tests/integration/cov/test_repo_timeline.py
git commit -m "test(cov): time/multi-capture/oddity timeline cases + golden mixed run

Assisted-by: Claude Fable 5"
```

---

### Task 9: Benchmark spike (scale + cache decision evidence)

**Files:**

- Create: `scripts/cov_validity_bench.py`
- Test: `tests/integration/cov/test_validity_scale.py`

**Interfaces:**

- Consumes: `RepoTimeline`, `apply_manual_capture`, `gitio._run_raw` counter pattern (Task 4).
- Produces: benchmark numbers recorded in the task's commit message and `docs/superpowers/plans/2026-07-24-coverage-validity-perf.md` completion notes — they are the evidence for the Task 10 checkpoint.

- [ ] **Step 1: Write the benchmark script**

```python
# scripts/cov_validity_bench.py
"""Manual benchmark: fold cost at scale, with git-spawn + I/O op counts.

Run on demand (never in CI): `uv run python scripts/cov_validity_bench.py [N_FILES]`.
Reports wall-clock, git subprocess count, and open()-per-fold so the
cache decision (spec §9) is made on round-trips, not just SSD wall-clock.
"""

import sys
import time
from pathlib import Path
from tempfile import TemporaryDirectory

from otto.coverage.capture import gitio

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tests"))
from _fixtures._repo_timeline import RepoTimeline  # noqa: E402


def bench(n_files: int) -> None:
    with TemporaryDirectory() as td:
        tl = RepoTimeline(Path(td) / "repo")
        for i in range(n_files):
            tl.write(f"src/f{i:04}.c", f"int f{i}(void)\n{{\n    return {i};\n}}\n")
        tl.commit("base")
        tl.capture("bench", {f"src/f{i:04}.c": {3: 1} for i in range(n_files)})
        for i in range(0, n_files, 10):  # touch 10% of files
            tl.write(f"src/f{i:04}.c", f"int f{i}(void)\n{{\n    return -{i};\n}}\n")
        tl.commit("churn")

        calls = 0
        real = gitio._run_raw

        def counting(args, cwd, ok_codes=(0,)):
            nonlocal calls
            calls += 1
            return real(args, cwd, ok_codes)

        gitio._run_raw = counting
        try:
            t0 = time.perf_counter()
            tl.fold()
            dt = time.perf_counter() - t0
        finally:
            gitio._run_raw = real
        print(f"files={n_files} fold={dt * 1000:.0f}ms git_spawns={calls}")


if __name__ == "__main__":
    bench(int(sys.argv[1]) if len(sys.argv) > 1 else 2000)
```

- [ ] **Step 2: Run it once at 2000 files (single process, light — allowed on the dev VM)**

Run: `uv run python scripts/cov_validity_bench.py 2000`
Expected shape: `files=2000 fold=<a few hundred>ms git_spawns=1`. Record the actual line.

- [ ] **Step 3: Write the CI-safe scale pin**

```python
# tests/integration/cov/test_validity_scale.py
"""Small-scale pin of the batched fold: spawn count is the load-bearing assert.

Wall-clock budgets flake on shared runners; the spawn count is
deterministic. 200 files keeps this test <1s everywhere.
"""

from pathlib import Path

from otto.coverage.capture import gitio
from tests._fixtures._repo_timeline import RepoTimeline


def test_fold_200_files_is_batched(tmp_path, monkeypatch):
    tl = RepoTimeline(tmp_path / "repo")
    for i in range(200):
        tl.write(f"f{i:03}.c", f"int f{i}(void)\n{{\n    return {i};\n}}\n")
    tl.commit("base")
    tl.capture("bench", {f"f{i:03}.c": {3: 1} for i in range(200)})
    tl.write("f000.c", "int f0(void)\n{\n    return -1;\n}\n")
    tl.commit("churn")

    calls: list[list[str]] = []
    real = gitio._run_raw
    monkeypatch.setattr(
        gitio, "_run_raw",
        lambda a, c, ok=(0,): (calls.append(list(a)), real(a, c, ok))[1],
    )
    tl.fold()
    assert len(calls) <= 2, [" ".join(c) for c in calls]
```

- [ ] **Step 4: Run, lint, commit (record the bench line in the message)**

```bash
uv run pytest tests/integration/cov/test_validity_scale.py -v
uv run nox -s lint
git add scripts/cov_validity_bench.py tests/integration/cov/test_validity_scale.py
git commit -m "test(cov): scale pin + on-demand validity benchmark (2000 files: <RECORD ms>, 1 spawn)

Assisted-by: Claude Fable 5"
```

- [ ] **Step 5: CHECKPOINT — report benchmark numbers before Task 10**

STOP. Report the 2000-file numbers to Chris. Spec §9 commits to shipping the JSON cache; if the batched fold at 2000 files is already comfortably sub-second with 1 spawn, the cache's remaining value is limited to the *fallback* path (GC'd/shallow bases, where per-file blob diffs still occur). Chris decides at this checkpoint: build Task 10 as specified, narrow it to the fallback path only, or descope it (spec §9 amendment). Do not proceed on assumption.

**Task 9 completion note (2026-07-24):** measured `files=2000 fold=56ms git_spawns=1` (reviewer independently reproduced 57ms/1 spawn; N=50 smoke: `files=50 fold=2ms git_spawns=1`). The batched tree-diff path needs no cache; remaining cache value is confined to the blob fallback path (GC'd/shallow bases) — evidence for the Task 10 checkpoint decision.

**Checkpoint decision (2026-07-24, Chris): batch the fallback path; descope the cache.** Comparative profile (scratchpad `fallback_options_profile.py`, every variant correctness-gated identical to the committed resolver at N=200/2000): GC'd-base fallback baseline at 2000 files/10% churn = 2602 spawns, 1.53s SSD, ~27.6s at a 10ms-per-spawn NFS projection; cache-as-specified warm = 2202 spawns (~15% better; cold is a net add); batched = 5 flat spawns, 49ms SSD, ≤0.1s projected; cache-on-top saves ≤3 spawns (~60ms) — below a persistent store's carrying cost. Spec §9 amended in the same commit; Task 10 rewritten below.

---

### Task 10 (amended at the Task 9 checkpoint): Batched blob-fallback resolution

> **Amendment provenance (2026-07-24):** this task originally shipped the spec §9 JSON
> validity cache (`CacheStore`). At the Task 9 Step 5 checkpoint Chris chose batching the
> fallback instead; the comparative profile and ruling are recorded in the Task 9
> completion note above and in amended spec §9. Requirement in one line: the GC'd/shallow
> fallback path must resolve a whole capture in a **constant number of git spawns**
> (≤6), with results byte-identical to the per-file chain it replaces.

**Files:**

- Modify: `src/otto/coverage/capture/gitio.py` (stdin-batch helpers)
- Modify: `src/otto/coverage/anchor.py` (batched fallback index)
- Modify: `src/otto/coverage/validity.py` (pass the capture's files through)
- Create: `tests/unit/cov/test_gitio_batch.py`
- Modify: `tests/unit/cov/test_anchor.py`, `tests/integration/cov/test_validity_scale.py`

**Interfaces:**

- Produces in gitio: `_run_raw_input(args, cwd, stdin: bytes, ok_codes=(0,)) -> bytes` —
  a SEPARATE private helper mirroring `_run_raw`'s error translation. Do NOT change
  `_run_raw`'s signature: the committed spawn-count monkeypatches (Task 4 gate, Task 9
  pin) forward it positionally and must keep passing untouched.
- `hash_objects(repo_root: Path, paths: list[Path]) -> list[str]` — one
  `git hash-object --stdin-paths` spawn; output order matches input order; must equal
  per-file `hash_object` for every input (it applies the same attribute filters).
- `blobs_exist(repo_root: Path, shas: list[str]) -> set[str]` — one
  `cat-file --batch-check` spawn; returns the subset of `shas` present in the odb
  (lines not ending in `missing`).
- `cat_blobs(repo_root: Path, shas: list[str]) -> dict[str, bytes]` — one
  `cat-file --batch` spawn; stream format is `<sha> <type> <size>\n` followed by
  exactly `size` bytes and a trailing `\n`; duplicate shas are fine (last write wins).
- `diff_no_index_dir_u0(dir_a: Path, dir_b: Path) -> str` — one
  `git diff --no-index -w -U0 <dir_a.name> <dir_b.name>` spawn with
  `cwd=dir_a.parent`, `ok_codes=(0, 1)` (exit 1 = differences = success, exactly like
  `diff_no_index_u0`). Contract: the two dirs are siblings; output paths are therefore
  `<dir_a.name>/<rel>`, which the caller strips.
- Produces in anchor: `AnchorResolver.__init__(repo_root, base_commit, *,
  files: dict[str, CaptureFileCov] | None = None)`. When the tree diff raises AND
  `files` is given, build a batched fallback index ONCE: skip files whose current path
  is missing (unverifiable) or whose `fc.blob` is None (left to the lazy path); hash
  all remaining current files via `hash_objects`; fast-path those whose hash equals
  `fc.blob` (verbatim); for the changed rest — one `blobs_exist` (absent base blob →
  unverifiable), one `cat_blobs`, write two sibling temp trees (base content vs current
  content at capture-relative paths), one `diff_no_index_dir_u0`, parse with the
  existing `parse_multifile_u0`, strip the dir prefix from its keys; a pair absent from
  the `-w` diff output is whitespace-only → `hunks=[]` (verbatim). `resolve()` answers
  from the index when present; `_resolve_by_blob` REMAINS the lazy per-file path for
  `files=None` callers (existing unit tests) and for any relpath not in the index.
- Consumes: `apply_manual_capture` constructs the resolver as
  `AnchorResolver(repo_root, capture.base_commit, files=capture.files)`.

**Semantics parity is the requirement.** Every row of AnchorResolver's docstring table
must produce byte-identical `AnchorResult`s on the batched and per-file paths: unchanged
→ verbatim; whitespace-only → `hunks=[]` verbatim; modified → the same hunks the
per-file `--no-index` diff yields; current file missing → unverifiable; base blob absent
from the odb → unverifiable (with an unresolvable base, `blob_sha(rev=base)` cannot
rescue it — same terminal state as the per-file chain); `fc.blob is None` → lazy path.
No behavior change on the healthy-base tree path.

- [ ] **Step 1: Write the failing gitio batch tests**

`tests/unit/cov/test_gitio_batch.py`, against a `tmp_path` repo (reuse the
`RepoTimeline` fixture or bare `git init` per the existing `test_gitio.py` conventions):
`hash_objects` equals per-file `hash_object` across several files including an empty
one; `blobs_exist` partitions present/absent shas; `cat_blobs` round-trips contents
with embedded newlines and non-UTF-8 bytes; `diff_no_index_dir_u0` yields hunks for a
changed pair, omits a whitespace-only pair under `-w`, tolerates exit code 1, and its
keys carry the `<dir_a.name>/` prefix contract.

Run: `uv run pytest tests/unit/cov/test_gitio_batch.py -v`
Expected: FAIL — missing functions.

- [ ] **Step 2: Implement the gitio helpers**

All raise `GitUnavailableError` consistently via the shared error translation.
Run Step 1's file again. Expected: all pass.

- [ ] **Step 3: Write the failing anchor parity + spawn tests**

Extend `tests/unit/cov/test_anchor.py` with a batched-vs-lazy parity class: build the
repo scenarios from the existing fallback tests (unchanged / ws-only / modified /
deleted current / absent base blob), resolve each once with a bare resolver and once
with `files=` under an unresolvable base, assert identical results. Add to
`tests/integration/cov/test_validity_scale.py`:
`test_fold_gcd_base_is_batched` — 100 files, capture, churn every 10th, force
`base_commit="0" * 40` via `model_copy(update=...)`, count spawns across `tl.fold()` by
monkeypatching BOTH `gitio._run_raw` and `gitio._run_raw_input`, assert total `<= 6`.

Run both files. Expected: parity tests fail (no `files=` parameter yet); the scale
test fails on spawn count (~130 today).

- [ ] **Step 4: Implement the batched index + wiring**

`anchor.py` per the Interfaces block; one-line change in `validity.py`.
Run Step 3's files again. Expected: all pass.

- [ ] **Step 5: Full scoped sweep**

Run: `uv run pytest tests/unit/cov/test_gitio_batch.py tests/unit/cov/test_gitio.py
tests/unit/cov/test_anchor.py tests/unit/cov/test_validity.py
tests/unit/cov/test_git_spawn_budget.py tests/integration/cov -v`
Expected: all pass — including the committed tree-path pins (≤2 spawns) and the
timeline squash/shallow cases, which now traverse the batched fallback.

- [ ] **Step 6: Lint, typecheck, commit**

```bash
uv run nox -s lint && uv run nox -s typecheck
git add src/otto/coverage/capture/gitio.py src/otto/coverage/anchor.py src/otto/coverage/validity.py tests/unit/cov/test_gitio_batch.py tests/unit/cov/test_anchor.py tests/integration/cov/test_validity_scale.py
git commit -m "feat(cov): batch the blob-fallback anchor path to O(1) git spawns

Assisted-by: Claude Fable 5"
```

---

### Task 11: Docs + full gates

**Files:**

- Modify: `docs/guide/coverage.md` (validity section — whitespace/EOL immunity, rename following, supersede, shallow-clone hint, squash-merge fallback)
- Modify: `docs/architecture/subsystems/coverage.md` (anchor-chain design details — see Step 2, Chris-requested depth)
- Modify: `todo/TODO.md` (strike items delivered by this plan, if listed)

- [ ] **Step 1: Update the guide**

In `docs/guide/coverage.md`, find the manual-capture validity section and add (matching surrounding prose style — read the section first):

- Whitespace-only and line-ending-only changes never revoke manual coverage; any semantic change revokes exactly the changed lines.
- Encoding-only sweeps (BOM addition, transcoding) are NOT exempt — git diff cannot see past them, so affected lines revoke and must be re-proven (documented limitation).
- Renames are followed as far as `git diff -M` tracks them; file splits/copies are not followed — re-prove coverage after restructuring.
- A newer manual capture with the same run label and host **replaces** the older one entirely.
- On shallow clones, captures older than the clone depth degrade to stale with a deepen hint.
- Captures whose base commit has been squash-merged away are still verified per file against the recorded blobs; that fallback is batched into a constant number of git calls per capture, so validity stays fast for large repos on NFS mounts too.

- [ ] **Step 2: Update the architecture page (design details — explicitly requested by Chris)**

In `docs/architecture/subsystems/coverage.md`, extend the anchor-chain/validity section to cover the shipped design at architecture depth (match the page's existing prose/diagram style — read it first):

- **Two-path anchor resolution.** Per capture: one tree-wide `git diff -M -w -U0 --relative <base> -- .` answers every file while the base commit resolves (rename-following, whitespace-immune, nested-repo-safe). When it does not resolve (squash-merged away, shallow clone), the batched blob fallback takes over.
- **Batched fallback pipeline** (order matters): `hash-object --stdin-paths` over all current files (fast-path files whose hash equals the capture blob) → `cat-file --batch-check` (base blobs absent from the odb degrade to unverifiable/stale) → `cat-file --batch` (base contents) → one dir-level `git diff --no-index -w -U0` over two sibling temp trees, parsed by the same multi-file `-U0` parser as the tree path.
- **Parity contract.** Both paths produce identical `AnchorResult`s for every row of the resolver's semantics table; whitespace immunity comes from `-w` on every diff flavor. The per-file chain remains as the lazy path (callers without a capture-wide file map, capture entries without a recorded blob).
- **Spawn budgets are pinned contracts, not aspirations:** ≤2 spawns per fold on the tree path, ≤6 on the fallback path — enforced by `tests/unit/cov/test_git_spawn_budget.py` and `tests/integration/cov/test_validity_scale.py`. Spawn counts are the NFS round-trip proxy; this is why the fallback was batched rather than cached (spec §9 checkpoint ruling, with the 2026-07-24 profile numbers).
- **Supersede-on-recapture** sits before folding: manual captures deduplicate by (tier, label, host), newest wins, replaced captures drop from the runs table entirely.
- **RepoTimeline** (`tests/_fixtures/_repo_timeline.py`) is the executable spec for aging-repo behavior — commit → capture → mutate → fold → per-line dispositions.

- [ ] **Step 3: Build docs clean**

Run: `make docs`
Expected: zero warnings (`-W`). A `:doc:`-ref break in touched docstrings only surfaces on a CLEAN rebuild — this is that rebuild.

- [ ] **Step 4: Full gate sweep**

```bash
uv run nox -s lint && uv run nox -s typecheck
make coverage
```

Expected: coverage ≥ the configured floor, all tests green. This is the one full-suite run of the plan (dev-VM load rule).

- [ ] **Step 5: Commit**

```bash
git add docs/guide/coverage.md docs/architecture/subsystems/coverage.md todo/TODO.md
git commit -m "docs(cov): validity rulings + two-path anchor architecture (batched fallback)

Assisted-by: Claude Fable 5"
```

---

## Self-Review Notes (kept for the executor)

- **Spec coverage:** §8.1 → Tasks 2/6; §8.2 → Tasks 1/2/3/7; §8.3 → Task 7; §8.4 → Tasks 3/7; §8.5 → Tasks 5/8; §9 batching → Tasks 2/3/4 (tree path) + Task 10 as amended (fallback path); §9 cache → DESCOPED at the Task 9 checkpoint (ruling in amended §9 + the Task 9 completion note); §9 benchmark/op-counts → Task 9; §10 harness/cases → Tasks 6–8; §10 spawn-count gate → Tasks 4/9. NOT in this plan (later plans): store v4 host field (Plan B), decision slots (B), all UI (C), Jinja deletion + CLI rename (D), NFS auto-detection hook (D — `is_network_fs` wiring becomes relevant only if a second backend ever ships).
- **Type consistency:** `AnchorResult.new_relpath: Path | None`; `FileDiff.new_path: str | None` (str at parse layer, Path at resolve layer — deliberate); `select_manual_captures` list-in/list-out; batched fallback keeps `AnchorResult` as the single resolution currency (hunkless result = verbatim, `verifiable=False` = stale — no new types).
- **Known judgment calls encoded:** supersede replaces whole captures (not per-file) — surfaced loudly in Task 8's golden test note; shallow-clone assertion is environment-checked in Task 7; e2e dirty path (`load_dirty_capture_into_store`) intentionally NOT batched in this plan (per-file against HEAD, different shape — candidate for Plan D if the bench says it matters).
