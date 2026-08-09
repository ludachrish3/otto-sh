"""Behavior pins for tests/_fixtures/gitrepo.py — each against the defect
that motivated the fixture (review §7.2), not its implementation.
"""

import subprocess
from datetime import datetime, timedelta, timezone

import pytest

from tests._fixtures.gitrepo import TmpGitRepo, git_env


def test_commits_survive_a_poisoned_global_gitconfig(tmp_path):
    """THE motivating hazard: a global ``commit.gpgsign`` with no signing key
    fails every commit with an opaque CalledProcessError.  ``git_env`` points
    HOME into the sandbox, so the poison is planted exactly where git would
    look — the pin goes red if GIT_CONFIG_GLOBAL stops being neutered."""
    repo = TmpGitRepo(tmp_path / "repo")
    (repo.root / ".gitconfig").write_text("[commit]\n\tgpgsign = true\n")
    repo.write("f.txt", "x\n")
    sha = repo.commit("survives poison")
    assert len(sha) == 40


def test_env_is_closed_nothing_ambient_leaks(tmp_path, monkeypatch):
    """The dict is COMPLETE: a GIT_DIR/GIT_WORK_TREE leaking from the test
    runner's own environment would silently redirect every repo operation."""
    monkeypatch.setenv("GIT_DIR", "/nonexistent")
    assert set(git_env(tmp_path)) == {
        "GIT_AUTHOR_NAME",
        "GIT_AUTHOR_EMAIL",
        "GIT_COMMITTER_NAME",
        "GIT_COMMITTER_EMAIL",
        "GIT_CONFIG_GLOBAL",
        "GIT_CONFIG_SYSTEM",
        "GIT_TERMINAL_PROMPT",
        "HOME",
        "PATH",
    }
    assert set(git_env(tmp_path, dates="2026-01-01T00:00:00Z")) - set(git_env(tmp_path)) == {
        "GIT_AUTHOR_DATE",
        "GIT_COMMITTER_DATE",
    }
    # And the repo still works while the ambient poison is set.
    repo = TmpGitRepo(tmp_path / "repo")
    repo.write("f.txt", "x\n")
    repo.commit()


def test_pinned_dates_make_shas_reproducible(tmp_path):
    shas = []
    for name in ("a", "b"):
        repo = TmpGitRepo(tmp_path / name, dates="2026-01-01T00:00:00Z")
        repo.write("f.txt", "same\n")
        shas.append(repo.commit("same message"))
    assert shas[0] == shas[1]


def test_default_dates_are_real_time_not_pinned(tmp_path):
    """dates=None must keep producing "now" commits — 20 of the 22 migrated
    files create real-time commits, and the age-sensitive consumers among
    them (coverage validity aging) would change meaning under a silently
    pinned 2026-01-01 clock."""
    repo = TmpGitRepo(tmp_path / "repo")
    repo.write("f.txt", "x\n")
    repo.commit()
    # %at (epoch seconds), not %aI: git 2.45 made iso-strict render a
    # ZERO-OFFSET date as a trailing "Z" instead of "+00:00" ("date: make
    # 'iso-strict' conforming for the UTC timezone"), and
    # datetime.fromisoformat rejects "Z" until Python 3.11. Both conditions
    # are needed, so the ISO form was green on any developer machine outside
    # UTC and red only on a UTC CI runner in the 3.10 lane (issue #218: dev
    # VM America/Chicago + git 2.43 green; runner UTC + git 2.54 + 3.10 red,
    # while 3.11-3.14 passed). Epoch seconds carry no timezone and have no
    # format variants for a future git to restyle.
    stamp = repo.git("log", "-1", "--format=%at").strip()
    committed = datetime.fromtimestamp(int(stamp), timezone.utc)
    assert abs(datetime.now(timezone.utc) - committed) < timedelta(days=1)


def test_command_failure_is_loud(tmp_path):
    repo = TmpGitRepo(tmp_path / "repo")
    with pytest.raises(subprocess.CalledProcessError):
        repo.git("rev-parse", "--verify", "refs/heads/nonexistent-branch")
