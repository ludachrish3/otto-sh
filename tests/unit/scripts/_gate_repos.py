"""Scratch repositories for the gate tests: a published main, a tip on top, its stamps.

Real throwaway git repos in ``tmp_path``, never the dev repo, which carries exactly the
dirt the gates judge. Every spawn is hermetic (``tests/_fixtures/gitrepo.py``'s env).
"""

import subprocess
from collections.abc import Callable
from dataclasses import dataclass
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


def _published_src(tmp_path: Path, remote: str = "origin") -> "tuple[Path, Path, str, str]":
    """Like :func:`_published`, but the tip changes a module under ``src/otto/``.

    The remote's main (A) seeds the module, its unit tree, and a minimal support matrix
    with one of its readers, so the oracle has a row for each. The tip (B) edits the
    module: a src-leaf landing whose plan reaches the bed (``make coverage-bed``), and
    from which a later content edit of the matrix is a tests-leaf change over its reader
    (an edit of only its ``as_of`` date classes as ``none``, the oracle's re-date, so a
    content edit must change more than ``as_of``).

    Returns (repo, bare, remote_sha = A, tip_sha = B).
    """
    repo = _repo(tmp_path)
    seed = {
        "src/otto/host/unix.py": "VALUE = 1\n",
        "tests/unit/host/test_unix.py": "def test_unix(): pass\n",
        "schemas/support_matrix.json": '{"as_of": "2026-10-20"}\n',
        "tests/unit/test_support_matrix.py": "def test_support_matrix(): pass\n",
    }
    for path, text in seed.items():
        (repo / path).parent.mkdir(parents=True, exist_ok=True)
        (repo / path).write_text(text)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "seed src")
    remote_sha = _git(repo, "rev-parse", "HEAD").strip()
    bare = _bare(tmp_path, remote)
    _git(repo, "remote", "add", remote, str(bare))
    _git(repo, "push", "-q", remote, "HEAD:refs/heads/main")
    tip_sha = _commit_file(repo, "src/otto/host/unix.py", "VALUE = 2\n", "tip")
    return repo, bare, remote_sha, tip_sha


def _stamp_tip(repo: Path, remote_sha: str, tip_sha: str) -> None:
    """Stamp, at tip_sha's tree, every stamped lane the oracle names for remote_sha..tip_sha."""
    root = gt.stamps_root(repo, gt.STORE)
    tree = gt.tree_hash(repo, tip_sha)
    for key in gs.plan(repo, remote_sha, tip_sha).stamped:
        gt.write_stamp(root, tree=tree, key=key, base=remote_sha, checkout=str(repo))


@dataclass(frozen=True)
class _Rebased:
    """A src landing published at B0 and rebased onto B1, its plans over both ranges.

    ``t0`` and ``t1`` are the landing's TREE ids before and after the rebase; the
    ``landing`` branch is checked out at the rebased tip, and main (local and the bare's)
    is B1.
    """

    repo: Path
    bare: Path
    b0: str
    t0: str
    b1: str
    t1: str
    plan0: gs.Plan
    plan1: gs.Plan


def _rebased_landing(
    tmp_path: Path,
    *,
    main_change: "Callable[[Path], None]",
    amend: "Callable[[Path], None] | None" = None,
) -> _Rebased:
    """Build :func:`_published_src`'s landing, land *main_change* on main, rebase onto it.

    *main_change* writes files in the checkout (at B0); they are committed on main as B1 and
    pushed to the bare. The landing (B) is rebased onto B1, then *amend*, when given, writes
    more files that are amended into the rebased tip. ``plan0`` is the oracle's plan for
    ``b0..B``, ``plan1`` for ``b1..HEAD``.
    """
    repo, bare, b0, tip = _published_src(tmp_path)
    _git(repo, "checkout", "-q", "-b", "landing")
    _git(repo, "checkout", "-q", "-B", "main", b0)
    main_change(repo)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "main change")
    b1 = _git(repo, "rev-parse", "HEAD").strip()
    _git(repo, "push", "-q", "origin", "main")
    _git(repo, "checkout", "-q", "landing")
    _git(repo, "rebase", "-q", "main")
    if amend is not None:
        amend(repo)
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "--amend", "--no-edit")
    head = _git(repo, "rev-parse", "HEAD").strip()
    return _Rebased(
        repo=repo,
        bare=bare,
        b0=b0,
        t0=gt.tree_hash(repo, tip),
        b1=b1,
        t1=gt.tree_hash(repo, head),
        plan0=gs.plan(repo, b0, tip),
        plan1=gs.plan(repo, b1, head),
    )


def _write(repo: Path, path: str, text: str) -> None:
    """Write *text* at *path* under *repo*, creating parent directories; nothing is staged."""
    (repo / path).parent.mkdir(parents=True, exist_ok=True)
    (repo / path).write_text(text)


def _docs_change(repo: Path) -> None:
    """Main adds a docs page: class docs, no bed lane."""
    _write(repo, "docs/note.md", "a docs change\n")


def _src_change(repo: Path) -> None:
    """Main adds a module beside the landing's: a src change, which reaches the bed."""
    _write(repo, "src/otto/host/other.py", "OTHER = 1\n")


def _hostless_test_change(repo: Path) -> None:
    """Main edits the module's hostless unit test: a tests-leaf change, no bed lane."""
    _write(repo, "tests/unit/host/test_unix.py", "def test_unix(): pass\ndef test_more(): pass\n")


def _matrix_change(repo: Path) -> None:
    """Main edits the support matrix beyond its ``as_of`` date: a tests-leaf change."""
    _write(repo, "schemas/support_matrix.json", '{"as_of": "2026-10-21", "rows": []}\n')
