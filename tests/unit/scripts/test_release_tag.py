"""scripts/release_tag.py: ``make release-tag`` tags main's tip after the release pull request.

The record is judged before anything touches the remote; then main's tree and
version must be the record's, main's CI must be green through the shared
verdict reader, and main must not have moved during the wait.
"""

import json
import os
import subprocess
from pathlib import Path

import pytest

import scripts.local_gate_status as lgs
import scripts.release_record as rr
import scripts.release_tag as rt
import tests._fixtures.fake_gh as fg
from scripts import process_group as pg
from scripts.local_gate_status import GhRunner
from tests._fixtures.gitrepo import TmpGitRepo, git_env
from tests._fixtures.paths import CHECKOUT_ROOT
from tests.unit.scripts.test_local_gate_status import FakeClock, _jobs, _runs

pytestmark = pytest.mark.interpreter_agnostic

_started = pg.process_start(os.getpid())
assert _started is not None
RUN = rr.RunIdentity(os.getpid(), _started)
REQ = json.loads((CHECKOUT_ROOT / ".github/required_contexts.json").read_text())
REQ_JOBS = [c for c in REQ if c not in lgs.EXCLUDED_FROM_VERDICT]


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TmpGitRepo:
    """A scratch repo at 0.18.1, ``main`` pushed to a bare ``origin``.

    The origin is a filesystem URL, which ``remote_slug`` refuses by design, so
    the slug is stubbed; the script's git runs with the hermetic environment.
    """
    scratch = TmpGitRepo(tmp_path / "work")
    scratch.write(
        "pyproject.toml",
        "[tool.bumpversion]\n"
        'current_version = "0.18.1"\n'
        'tag_name = "v{new_version}"\n'
        'tag_message = "Release v{new_version}"\n',
    )
    scratch.commit("base")
    bare = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True, env=git_env(tmp_path))
    scratch.git("remote", "add", "origin", str(bare))
    scratch.git("push", "-q", "origin", "main")
    monkeypatch.setattr(rt, "remote_slug", lambda repo, remote: "o/r")
    for name, value in git_env(scratch.root).items():
        monkeypatch.setenv(name, value)
    return scratch


def _green() -> GhRunner:
    """A fresh queue per call: the replies are consumed."""
    return fg.scripted_gh(
        [
            fg.GhReply(["run", "list"], stdout=_runs((1, "completed", "success"))),
            fg.GhReply(["run", "view"], stdout=_jobs(**dict.fromkeys(REQ_JOBS, "success"))),
        ]
    )


def _certified(repo: TmpGitRepo, *, tree: "str | None" = None, version: str = "0.18.1") -> None:
    rr.write_pending(repo.root, RUN)
    rr.certify(
        repo.root,
        RUN,
        head=repo.git("rev-parse", "HEAD").strip(),
        tree=tree or repo.git("rev-parse", "HEAD^{tree}").strip(),
        version=version,
    )


def _tag(repo: TmpGitRepo, gh: "GhRunner | None" = None) -> int:
    return rt.release_tag(
        repo.root, remote="origin", required=REQ, wait_s=0, interval=1, gh=gh or _green()
    )


def test_no_record_and_a_pending_record_refuse(repo, capsys):
    assert _tag(repo) == 1
    assert "no release record" in capsys.readouterr().err
    rr.write_pending(repo.root, RUN)
    assert _tag(repo) == 1
    assert "the record is pending" in capsys.readouterr().err


def test_a_tree_or_version_that_is_not_the_record_s_refuses(repo, capsys):
    _certified(repo, tree="0" * 40)
    assert _tag(repo) == 1
    assert "not the certified tree" in capsys.readouterr().err
    _certified(repo, version="0.19.0")
    assert _tag(repo) == 1
    assert "0.19.0" in capsys.readouterr().err


def test_a_pending_run_is_waited_for_up_to_the_ceiling_then_refused(repo, capsys):
    _certified(repo)
    clock = FakeClock()
    gh = fg.scripted_gh(
        [
            fg.GhReply(["run", "list"], stdout=_runs((1, "in_progress", None)), repeat=True),
            fg.GhReply(
                ["run", "view"],
                stdout=_jobs("in_progress", **dict.fromkeys(REQ_JOBS)),
                repeat=True,
            ),
        ]
    )
    rc = rt.release_tag(
        repo.root,
        remote="origin",
        required=REQ,
        wait_s=30,
        interval=10,
        gh=gh,
        sleep=clock.sleep,
        clock=clock,
    )
    assert rc == 1
    assert clock.slept == [10, 10, 10]
    assert "still running after 0 minutes" in capsys.readouterr().err
    assert repo.git("tag", "--list") == ""


@pytest.mark.parametrize(
    ("final", "rc", "said"),
    [("success", 0, "pushed v0.18.1"), ("failure", 1, "lint-python: failure")],
)
def test_the_wait_keeps_the_verdict_that_settles(repo, capsys, final, rc, said):
    _certified(repo)
    clock = FakeClock()
    gh = fg.scripted_gh(
        [
            fg.GhReply(["run", "list"], stdout=_runs((1, "in_progress", None))),
            fg.GhReply(["run", "view"], stdout=_jobs("in_progress", **dict.fromkeys(REQ_JOBS))),
            fg.GhReply(["run", "list"], stdout=_runs((1, "completed", final)), repeat=True),
            fg.GhReply(
                ["run", "view"],
                stdout=_jobs(**{**dict.fromkeys(REQ_JOBS, "success"), "lint-python": final}),
                repeat=True,
            ),
        ]
    )
    got = rt.release_tag(
        repo.root,
        remote="origin",
        required=REQ,
        wait_s=60,
        interval=10,
        gh=gh,
        sleep=clock.sleep,
        clock=clock,
    )
    assert got == rc
    captured = capsys.readouterr()
    assert clock.slept == [10]
    assert said in captured.out + captured.err


def test_the_wait_is_the_shared_verdict_waiter():
    """``ci-verdict --wait-minutes`` and the tag share one waiter, pinned in local_gate_status's."""
    assert rt.await_verdict is lgs.await_verdict


def test_a_zero_wait_reads_the_verdict_once_and_never_sleeps(repo):
    _certified(repo)
    gh = _green()
    rc = rt.release_tag(
        repo.root,
        remote="origin",
        required=REQ,
        wait_s=0,
        interval=10,
        gh=gh,
        sleep=lambda s: pytest.fail("slept"),
    )
    assert rc == 0
    assert [c[:2] for c in gh.calls] == [["run", "list"], ["run", "view"]]


def test_a_ci_run_that_cannot_be_read_by_the_deadline_refuses_naming_the_cause(repo, capsys):
    _certified(repo)
    clock = FakeClock()
    gh = fg.scripted_gh(
        [fg.GhReply(["run", "list"], status=1, stderr="HTTP 403: Forbidden", repeat=True)]
    )
    rc = rt.release_tag(
        repo.root,
        remote="origin",
        required=REQ,
        wait_s=30,
        interval=10,
        gh=gh,
        sleep=clock.sleep,
        clock=clock,
    )
    assert rc == 1
    err = capsys.readouterr().err
    assert "cannot be read" in err
    assert "HTTP 403: Forbidden" in err
    assert "still running" not in err
    assert repo.git("tag", "--list") == ""


def test_a_red_main_refuses_naming_the_run(repo, capsys):
    _certified(repo)
    gh = fg.scripted_gh(
        [
            fg.GhReply(["run", "list"], stdout=_runs((1, "completed", "failure"))),
            fg.GhReply(
                ["run", "view"],
                stdout=_jobs(**{**dict.fromkeys(REQ_JOBS, "success"), "lint-python": "failure"}),
            ),
        ]
    )
    assert _tag(repo, gh) == 1
    assert "lint-python: failure" in capsys.readouterr().err
    assert repo.git("tag", "--list") == ""


@pytest.mark.parametrize("same_tree", [False, True])
def test_main_moving_during_the_wait_refuses_even_with_the_same_tree(
    repo, capsys, monkeypatch, same_tree
):
    _certified(repo)
    first = repo.git("rev-parse", "HEAD").strip()

    def advance() -> str:
        if same_tree:
            repo.git("commit", "-q", "--allow-empty", "-m", "chore: revert of a revert")
        else:
            repo.write("y", "2")
            repo.commit("more")
        repo.git("push", "-q", "origin", "main")
        return repo.git("rev-parse", "HEAD").strip()

    fetches = iter([lambda: first, advance])
    monkeypatch.setattr(rt, "fetch_main", lambda r, remote: next(fetches)())
    assert _tag(repo) == 1
    assert "main moved during the wait" in capsys.readouterr().err
    assert repo.git("tag", "--list") == ""


def test_a_green_match_tags_main_s_tip_with_the_configured_name_and_pushes_it(repo):
    _certified(repo)
    sha = repo.git("rev-parse", "HEAD").strip()
    assert _tag(repo) == 0
    assert repo.git("rev-parse", "v0.18.1^{commit}").strip() == sha
    assert repo.git("tag", "-l", "-n1", "v0.18.1").split(None, 1)[1].strip() == "Release v0.18.1"
    assert repo.git("ls-remote", "--tags", "origin", "refs/tags/v0.18.1").strip() != ""


@pytest.mark.parametrize("final", ["landed", "tip-certified"])
def test_a_landed_and_a_tip_certified_record_are_accepted(repo, final):
    _certified(repo)  # writes pending then certified for RUN with HEAD's sha, tree and 0.18.1
    if final == "landed":
        rr.mark_landed(repo.root, RUN, url="https://x/pull/1")
    else:
        rr.tip_certified(
            repo.root,
            RUN,
            head=repo.git("rev-parse", "HEAD").strip(),
            tree=repo.git("rev-parse", "HEAD^{tree}").strip(),
            version="0.18.1",
        )
    assert _tag(repo) == 0


def test_a_local_tag_left_by_a_failed_push_is_reused_and_pushed(repo, capsys):
    _certified(repo)
    sha = repo.git("rev-parse", "HEAD").strip()
    repo.git("tag", "-a", "v0.18.1", "-m", "Release v0.18.1", sha)
    assert _tag(repo) == 0
    assert f"reusing the local tag v0.18.1 on {sha[:12]}" in capsys.readouterr().out
    assert repo.git("ls-remote", "--tags", "origin", "refs/tags/v0.18.1").strip() != ""


@pytest.mark.parametrize("other", ["commit", "message"])
def test_a_local_tag_that_is_not_the_release_s_refuses_and_pushes_nothing(repo, capsys, other):
    sha = repo.git("rev-parse", "HEAD").strip()
    if other == "commit":
        repo.write("y", "2")
        stray = repo.commit("elsewhere")
        repo.git("reset", "-q", "--hard", sha)
        repo.git("tag", "-a", "v0.18.1", "-m", "Release v0.18.1", stray)
        said = f"points at {stray[:12]}, not main's tip {sha[:12]}"
    else:
        repo.git("tag", "-a", "v0.18.1", "-m", "something else", sha)
        said = "Release v0.18.1"
    _certified(repo)
    assert _tag(repo) == 1
    err = capsys.readouterr().err
    assert "the local tag v0.18.1" in err
    assert said in err
    assert "git tag -d v0.18.1" in err
    assert repo.git("ls-remote", "--tags", "origin").strip() == ""


def test_the_tag_name_and_message_come_from_the_bumpversion_config():
    config = {"tag_name": "v{new_version}", "tag_message": "Release v{new_version}"}
    assert rt.tag_for(config, "1.2.3") == ["v1.2.3", "Release v1.2.3"]


def test_an_unknown_placeholder_in_the_tag_format_is_named_as_such():
    config = {"tag_name": "v{new_verison}", "tag_message": "Release v{new_version}"}
    with pytest.raises(rr.ReleaseError, match=r"unknown placeholder \{new_verison\}"):
        rt.tag_for(config, "1.2.3")


def test_the_tag_format_is_main_s_not_the_working_tree_s(repo):
    _certified(repo)
    text = (repo.root / "pyproject.toml").read_text()
    (repo.root / "pyproject.toml").write_text(text.replace('"v{new_version}"', '"x{new_version}"'))
    assert _tag(repo) == 0
    assert repo.git("tag", "--list").split() == ["v0.18.1"]


def test_a_main_without_a_bumpversion_table_refuses(repo, capsys):
    repo.write("pyproject.toml", "[tool.other]\nx = 1\n")
    repo.commit("drop the table")
    repo.git("push", "-q", "origin", "main")
    _certified(repo)
    assert _tag(repo) == 1
    assert "tool.bumpversion" in capsys.readouterr().err
    assert repo.git("tag", "--list") == ""


def test_a_missing_required_contexts_file_refuses(repo, capsys):
    _certified(repo)
    assert rt.main(["--repo", str(repo.root), "--required", "absent.json"]) == 1
    assert "absent.json" in capsys.readouterr().err
