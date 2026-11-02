"""scripts/release_record.py: the release record's lifecycle, mode decision and CLI."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import gate_stamp_common
from scripts import process_group as pg
from scripts import release_record as rr
from tests._fixtures.gitrepo import TmpGitRepo, git_env
from tests._fixtures.paths import CHECKOUT_ROOT

_started = pg.process_start(os.getpid())
assert _started is not None
RUN = rr.RunIdentity(os.getpid(), _started)


@pytest.fixture
def repo(tmp_path: Path) -> TmpGitRepo:
    """A scratch repo at version 0.18.1 with a bare ``origin``."""
    scratch = TmpGitRepo(tmp_path / "work")
    scratch.write("pyproject.toml", '[tool.bumpversion]\ncurrent_version = "0.18.1"\n')
    scratch.commit("base")
    bare = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True, env=git_env(tmp_path))
    scratch.git("remote", "add", "origin", str(bare))
    scratch.git("push", "-q", "origin", "main")
    return scratch


def test_a_run_starts_pending_and_a_second_run_overwrites_a_certificate(repo: TmpGitRepo) -> None:
    rr.write_pending(repo.root, RUN)
    rr.certify(repo.root, RUN, head="h", tree="t", version="0.18.1")
    record = rr.read(repo.root)
    assert record is not None
    assert record.state == "certified"
    rr.write_pending(repo.root, rr.RunIdentity(RUN.pid, RUN.pid_started + 1))
    record = rr.read(repo.root)
    assert record is not None
    assert record.state == "pending"
    assert record.tree is None


def test_certify_landed_and_tip_certified_follow_the_lifecycle_of_one_run(repo: TmpGitRepo) -> None:
    other = rr.RunIdentity(RUN.pid, RUN.pid_started + 1)
    with pytest.raises(rr.ReleaseError):
        rr.certify(repo.root, RUN, head="h", tree="t", version="v")  # no pending yet
    rr.write_pending(repo.root, RUN)
    with pytest.raises(rr.ReleaseError):
        rr.certify(repo.root, other, head="h", tree="t", version="v")  # another run
    rr.certify(repo.root, RUN, head="h", tree="t", version="0.18.1")
    rr.mark_landed(repo.root, RUN, url="https://x/pull/1")
    record = rr.read(repo.root)
    assert record is not None
    assert (record.state, record.url, record.head, record.tree, record.version) == (
        "landed",
        "https://x/pull/1",
        "h",
        "t",
        "0.18.1",
    )
    rr.write_pending(repo.root, RUN)
    rr.certify(repo.root, RUN, head="m", tree="t2", version="0.18.1")
    rr.tip_certified(repo.root, RUN, head="m", tree="t2", version="0.18.1")
    record = rr.read(repo.root)
    assert record is not None
    assert record.state == "tip-certified"


def test_landing_needs_a_certificate_by_the_same_run(repo: TmpGitRepo) -> None:
    rr.write_pending(repo.root, RUN)
    with pytest.raises(rr.ReleaseError):
        rr.mark_landed(repo.root, RUN, url="u")  # still pending
    with pytest.raises(rr.ReleaseError):
        rr.tip_certified(repo.root, RUN, head="h", tree="t", version="v")


def test_read_is_none_without_a_record(repo: TmpGitRepo) -> None:
    assert rr.read(repo.root) is None


def test_identity_refuses_an_unreadable_start(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rr.process_group, "process_start", lambda pid: None)
    with pytest.raises(rr.ReleaseError, match="readable identity"):
        rr.identity(1)


def test_mode_is_release_when_the_current_version_is_tagged_on_the_remote(
    repo: TmpGitRepo,
) -> None:
    repo.git("tag", "v0.18.1")
    repo.git("push", "-q", "origin", "v0.18.1")
    assert rr.mode(repo.root, "origin", "0.18.2") == "release"


def test_mode_is_recertify_when_the_bump_is_untagged_and_the_census_equals_it(
    repo: TmpGitRepo,
) -> None:
    assert rr.mode(repo.root, "origin", "0.18.1") == "recertify"
    assert rr.mode(repo.root, "origin", "0.19.0") == "release"
    with pytest.raises(rr.ReleaseError, match="already carries the untagged bump"):
        rr.mode(repo.root, "origin", "0.18.0")
    with pytest.raises(rr.ReleaseError):
        rr.mode(repo.root, "origin", "")


def test_the_landing_is_decided_by_the_shared_main_tip_predicate() -> None:
    assert rr.at_main_tip is gate_stamp_common.at_main_tip
    assert not hasattr(rr, "nothing_to_merge")


def test_the_cli_writes_the_states_with_make_s_pid(repo: TmpGitRepo) -> None:
    pid = str(os.getpid())
    assert rr.main(["--repo", str(repo.root), "pending", "--pid", pid]) == 0
    assert rr.main(["--repo", str(repo.root), "certify", "--pid", pid, "--version", "0.18.1"]) == 0
    record = rr.read(repo.root)
    assert record is not None
    assert record.head == repo.git("rev-parse", "HEAD").strip()
    assert record.tree == repo.git("rev-parse", "HEAD^{tree}").strip()
    assert rr.main(["--repo", str(repo.root), "landed", "--pid", pid, "--url", "u"]) == 0
    record = rr.read(repo.root)
    assert record is not None
    assert record.state == "landed"


def test_the_cli_reports_a_refusal_on_stderr(
    repo: TmpGitRepo, capsys: pytest.CaptureFixture[str]
) -> None:
    pid = str(os.getpid())
    assert rr.main(["--repo", str(repo.root), "landed", "--pid", pid, "--url", "u"]) == 1
    assert "release-record:" in capsys.readouterr().err


def test_the_cli_mode_and_landing(repo: TmpGitRepo, capsys: pytest.CaptureFixture[str]) -> None:
    argv = ["--repo", str(repo.root), "mode", "--remote", "origin", "--census", "0.18.1"]
    assert rr.main(argv) == 0
    assert capsys.readouterr().out.strip() == "recertify"
    landing = ["--repo", str(repo.root), "landing", "--remote", "origin"]
    assert rr.main(landing) == 0
    assert capsys.readouterr().out.strip() == "tip-certified"
    repo.write("x", "1")
    repo.commit("more")
    assert rr.main(landing) == 0
    assert capsys.readouterr().out.strip() == "pull-request"


def test_a_landing_that_cannot_fetch_main_is_a_refusal_not_a_pull_request(
    repo: TmpGitRepo, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo.git("remote", "set-url", "origin", str(tmp_path / "gone.git"))
    assert rr.main(["--repo", str(repo.root), "landing", "--remote", "origin"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("release-record: origin has no main")


def test_read_fields_prints_one_value_per_line_and_empty_for_none(
    repo: TmpGitRepo, capsys: pytest.CaptureFixture[str]
) -> None:
    fields = ["--repo", str(repo.root), "read", "--fields", "state,run.pid,run.pid_started,url"]
    assert rr.main(fields) == 0
    assert capsys.readouterr().out == "\n\n\n\n", "no record: every field empty"
    rr.write_pending(repo.root, RUN)
    rr.certify(repo.root, RUN, head="h", tree="t", version="0.18.1")
    rr.mark_landed(repo.root, RUN, url="https://x/pull/1")
    assert rr.main(fields) == 0
    out = capsys.readouterr().out
    assert out.splitlines() == ["landed", str(RUN.pid), str(RUN.pid_started), "https://x/pull/1"]
    rr.write_pending(repo.root, RUN)
    assert rr.main(fields) == 0
    assert capsys.readouterr().out.splitlines() == [
        "pending",
        str(RUN.pid),
        str(RUN.pid_started),
        "",
    ]


def test_read_fields_refuses_a_field_the_record_does_not_have(
    repo: TmpGitRepo, capsys: pytest.CaptureFixture[str]
) -> None:
    rr.write_pending(repo.root, RUN)
    assert rr.main(["--repo", str(repo.root), "read", "--fields", "state,run.colour"]) == 1
    assert "release-record: the record has no field run.colour" in capsys.readouterr().err


def test_importing_the_module_does_not_load_packaging() -> None:
    """The module imports with the stdlib and tomli alone; only ``mode`` needs packaging."""
    code = (
        "import sys; "
        f"sys.path.insert(0, {str(CHECKOUT_ROOT)!r}); "
        "import scripts.release_record; "
        "sys.exit(1 if 'packaging' in sys.modules else 0)"
    )
    assert subprocess.run([sys.executable, "-I", "-c", code], check=False).returncode == 0


def test_a_corrupt_record_is_refused_not_a_traceback(
    repo: TmpGitRepo, capsys: pytest.CaptureFixture[str]
) -> None:
    repo.write(str(rr.RECORD), "{garbage")
    with pytest.raises(rr.ReleaseError, match="not a release record"):
        rr.read(repo.root)
    assert rr.main(["--repo", str(repo.root), "read"]) == 1
    assert "release-record:" in capsys.readouterr().err
