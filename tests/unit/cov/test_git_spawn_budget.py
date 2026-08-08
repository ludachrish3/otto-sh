"""Pin O(1) git subprocesses per capture — not O(files) (spec §9).

The counter wraps gitio._run_raw, the single chokepoint every helper
funnels through. Budget: resolver construction = 1 (tree diff) with +1
allowed for the shallow probe on the fallback path, and +1 more for the
work-tree probe that classifies the tree diff's failure; per-file resolves
on the tree path spawn ZERO except the existence stat (not git).

That third spawn is why the chokepoint matters more than the number: it is
a FAILURE-path spawn, and it goes through `_run_raw` so this counter can see
it at all. A budget that only counts the happy path bounds nothing.
"""

import subprocess
from pathlib import Path

import pytest

from otto.coverage.anchor import AnchorResolver
from otto.coverage.capture import gitio
from otto.coverage.capture.model import Capture, CaptureFileCov
from otto.coverage.store.model import CoverageStore
from otto.coverage.validity import apply_manual_capture
from tests._fixtures.gitrepo import git_env

N_FILES = 50


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        env=git_env(cwd),
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
        f"f{i:03}.c": CaptureFileCov(blob=gitio.blob_sha(root, Path(f"f{i:03}.c")), lines={3: 1})
        for i in range(N_FILES)
    }
    cap = Capture(
        tier="manual",
        base_commit=base,
        captured_at="2026-07-01T00:00:00Z",
        board="bench-1",
        files=files,
    )
    return root, cap


def test_failure_path_classification_probe_is_counted(counted_git, big_repo):
    """The work-tree probe must be VISIBLE to this counter, not invisible.

    It classifies a failed git call (not-a-repo vs. the command itself
    failing) and it runs through ``_run_raw`` for exactly that reason: a spawn
    that goes straight to ``subprocess`` is a spawn no budget guard can bound,
    and failure paths are precisely where an unnoticed per-file spawn would
    hide. So the number here going UP is the point — what would be wrong is a
    failure path the instrument cannot see.

    Fallback construction is three spawns: the tree diff that fails against an
    unresolvable base, the probe that classifies that failure, and the shallow
    probe behind the degradation warning.
    """
    root, _cap = big_repo
    counted_git.clear()
    AnchorResolver(root, "0" * 40)
    spawned = [" ".join(c) for c in counted_git]
    assert len(counted_git) == 3, spawned
    assert gitio._WORK_TREE_PROBE in counted_git, spawned


def test_fold_spawns_constant_git_calls(counted_git, big_repo):
    root, cap = big_repo
    # Touch a handful of files so the tree diff is non-trivial.
    for i in (3, 7):
        (root / f"f{i:03}.c").write_text(f"int f{i}(void)\n{{\n    return -1;\n}}\n")
    counted_git.clear()
    store = CoverageStore()
    apply_manual_capture(store, cap, root, max_age_days=None)
    assert len(counted_git) <= 2, [" ".join(c) for c in counted_git]
