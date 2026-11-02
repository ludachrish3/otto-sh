"""Scratch repositories for the gate tests: a published main, a tip on top, its stamps.

Real throwaway git repos in ``tmp_path``, never the dev repo, which carries exactly the
dirt the gates judge. Every spawn is hermetic (``tests/_fixtures/gitrepo.py``'s env).
"""

import subprocess
from pathlib import Path

from scripts import gate_scope as gs
from scripts import gate_tip as gt
from tests._fixtures.gitrepo import git_env


def _git(repo: Path, *args: str) -> str:
    """Run one git command against *repo*, hermetically.

    The env is the suite's one builder (``tests/_fixtures/gitrepo.py``): a global
    ``commit.gpgsign`` or an ``/etc/gitconfig`` ``core.hooksPath`` must not fail the
    builders below, and identity comes from ``GIT_AUTHOR_*``/``GIT_COMMITTER_*``. The
    gates themselves spawn their own git; that is product behaviour, not touched here.
    """
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True, env=git_env(repo)
    ).stdout


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "pyproject.toml").write_text('[tool.pytest.ini_options]\ntestpaths = ["tests"]\n')
    (repo / "tracked.txt").write_text("v1\n")
    (repo / ".gitignore").write_text("ignored/\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    return repo


def _bare(tmp_path: Path, name: str) -> Path:
    bare = tmp_path / f"{name}.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True, env=git_env(tmp_path))
    return bare


def _commit_file(repo: Path, path: str, text: str, message: str) -> str:
    (repo / path).parent.mkdir(parents=True, exist_ok=True)
    (repo / path).write_text(text)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", message)
    return _git(repo, "rev-parse", "HEAD").strip()


def _published(tmp_path: Path, remote: str = "origin") -> "tuple[Path, Path, str, str]":
    """Two commits: _repo's (published as the remote's main = A), then a
    tests-leaf commit on top (the tip = B, checked out).

    Returns (repo, bare, remote_sha = A, tip_sha = B). The oracle's plan for A..B is the
    tests-leaf shape: its stamped static lanes, plus the live check-breaking line.
    """
    repo = _repo(tmp_path)
    remote_sha = _git(repo, "rev-parse", "HEAD").strip()
    bare = _bare(tmp_path, remote)
    _git(repo, "remote", "add", remote, str(bare))
    _git(repo, "push", "-q", remote, "HEAD:refs/heads/main")
    tip_sha = _commit_file(repo, "tests/unit/cli/test_b.py", "def test_b(): pass\n", "tip")
    return repo, bare, remote_sha, tip_sha


def _stamp_tip(repo: Path, remote_sha: str, tip_sha: str) -> None:
    """Stamp, at tip_sha's tree, every stamped lane the oracle names for remote_sha..tip_sha."""
    root = gt.stamps_root(repo, gt.STORE)
    tree = gt.tree_hash(repo, tip_sha)
    for key in gs.plan(repo, remote_sha, tip_sha).stamped:
        gt.write_stamp(root, tree=tree, key=key, base=remote_sha, measured={}, checkout=str(repo))
