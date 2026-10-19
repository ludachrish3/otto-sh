"""``scripts/release_preflight.py``: what ``make release`` refuses and clears before it builds.

Every test builds the hostile state itself in a throwaway repo. The dev VM
happening to carry a stale directory is not evidence of anything.
"""

import subprocess
from pathlib import Path

import pytest

from scripts import release_preflight as rp
from tests._fixtures.gitrepo import git_env

pytestmark = pytest.mark.interpreter_agnostic


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, env=git_env(repo))


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A committed checkout with one real package and the release's ignore lines."""
    root = tmp_path / "repo"
    (root / "src" / "otto" / "real").mkdir(parents=True)
    (root / "src" / "otto" / "real" / "__init__.py").write_text("")
    (root / "docs").mkdir()
    (root / "docs" / "index.md").write_text("# otto\n")
    (root / ".gitignore").write_text(
        "__pycache__/\nreports/\ntests/project5/build/\ndocs/generated.md\n"
    )
    _git(root, "init", "-q", "-b", "main")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "base")
    remote = tmp_path / "remote.git"
    _git(tmp_path, "init", "-q", "--bare", str(remote))
    _git(root, "remote", "add", "origin", str(remote))
    _git(root, "push", "-q", "-u", "origin", "main")
    # The script spawns git with the inherited environment, as `make release` does.
    for name, value in git_env(root).items():
        monkeypatch.setenv(name, value)
    return root


def _write(path: Path, text: str = "x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


@pytest.mark.parametrize(
    "stray",
    [
        "docs/cli/cov/instrumenting/renamed-matrix.md",
        "src/otto/new_module.py",
        "tests/unit/test_new.py",
        "schemas/new.schema.json",
    ],
)
def test_an_untracked_file_under_a_read_root_is_refused_and_named(repo, capsys, stray):
    _write(repo / stray)

    assert rp.preflight(repo) == 1

    err = capsys.readouterr().err
    assert f"  {stray}\n" in err
    assert "refusing to release" in err


def test_a_refusal_removes_nothing(repo):
    _write(repo / "docs" / "stray.md")
    observations = _write(repo / "reports" / "conformance-observations" / "a.json")

    assert rp.preflight(repo) == 1

    assert observations.exists(), "a refused release still cleared run output"


def test_untracked_files_outside_the_read_roots_are_allowed(repo):
    _write(repo / "todo" / "notes.md")
    _write(repo / "scratch.txt")

    assert rp.preflight(repo) == 0


def test_an_ignored_file_under_a_read_root_is_not_refused(repo):
    _write(repo / "docs" / "generated.md")

    assert rp.preflight(repo) == 0


def test_a_modified_tracked_file_is_not_this_step_s_concern(repo):
    """Dirty tracked files are the coverage stamp's refusal (exit 2), not a stray source."""
    (repo / "docs" / "index.md").write_text("# changed\n")

    assert rp.preflight(repo) == 0


def test_stale_observations_and_the_project5_build_are_cleared_and_reported(repo, capsys):
    stale = [
        _write(repo / "reports" / "conformance-observations" / "bed-1.json"),
        _write(repo / "reports" / "kmodcov-observations" / "obs.json"),
        _write(repo / "tests" / "project5" / "build" / "lib" / "old_name.c"),
    ]
    kept = _write(repo / "reports" / "coverage" / "index.html")

    assert rp.preflight(repo) == 0

    out = capsys.readouterr().out
    for path in stale:
        assert not path.exists(), f"{path} survived the preflight"
    assert "removed reports/conformance-observations" in out
    assert "removed reports/kmodcov-observations" in out
    assert "removed tests/project5/build" in out
    assert kept.exists(), "the preflight removed run output it does not own"


def test_a_package_left_holding_only_bytecode_is_removed(repo, capsys):
    orphan = repo / "src" / "otto" / "retired"
    _write(orphan / "__pycache__" / "__init__.cpython-310.pyc")
    nested = repo / "src" / "otto" / "real" / "gone"
    _write(nested / "sub" / "__pycache__" / "m.cpython-310.pyc")

    assert rp.preflight(repo) == 0

    assert not orphan.exists()
    assert not nested.exists()
    assert (repo / "src" / "otto" / "real" / "__init__.py").exists()
    out = capsys.readouterr().out
    assert "removed src/otto/retired" in out
    assert "removed src/otto/real/gone" in out


def test_a_package_with_a_source_file_is_kept_with_its_bytecode(repo):
    _write(repo / "src" / "otto" / "real" / "__pycache__" / "__init__.cpython-310.pyc")

    assert rp.preflight(repo) == 0

    assert (repo / "src" / "otto" / "real" / "__pycache__").exists()


def test_an_empty_directory_under_the_package_root_is_removed(repo):
    """An empty directory imports as a namespace package too; removing it is harmless."""
    (repo / "src" / "otto" / "empty").mkdir()

    assert rp.preflight(repo) == 0

    assert not (repo / "src" / "otto" / "empty").exists()


def test_an_unpushed_commit_is_refused_and_named_before_anything_is_cleared(repo, capsys):
    _write(repo / "docs" / "new.md")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "feat: not pushed yet")
    stale = _write(repo / "reports" / "conformance-observations" / "cell.json")

    assert rp.preflight(repo) == 1

    err = capsys.readouterr().err
    assert "feat: not pushed yet" in err
    assert "git push" in err
    assert stale.exists(), "a refusal clears nothing"


def test_once_pushed_the_same_commit_passes(repo):
    _write(repo / "docs" / "new.md")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "feat: pushed")
    _git(repo, "push", "-q")

    assert rp.preflight(repo) == 0


def test_a_branch_with_no_upstream_is_refused(repo, capsys):
    _git(repo, "branch", "--unset-upstream")

    assert rp.preflight(repo) == 1

    assert "no upstream" in capsys.readouterr().err


def test_a_clean_checkout_passes_and_changes_nothing(repo, capsys):
    before = sorted(p.relative_to(repo) for p in repo.rglob("*") if ".git" not in p.parts)

    assert rp.preflight(repo) == 0

    after = sorted(p.relative_to(repo) for p in repo.rglob("*") if ".git" not in p.parts)
    assert after == before
    assert "removed" not in capsys.readouterr().out


def test_a_symlinked_observations_dir_is_unlinked_and_its_target_is_kept(repo, tmp_path):
    outside = _write(tmp_path / "elsewhere" / "keep.json")
    (repo / "reports").mkdir()
    (repo / "reports" / "x-observations").symlink_to(outside.parent, target_is_directory=True)

    assert rp.preflight(repo) == 0

    assert not (repo / "reports" / "x-observations").exists()
    assert outside.exists(), "the preflight followed a symlink out of the checkout"
