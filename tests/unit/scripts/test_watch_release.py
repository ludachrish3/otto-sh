"""Pins for scripts/watch_release.sh -- the outside-the-terminal release watcher.

The watcher's job is to end with ONE line that says how the release ended, so a
reader who was not at the terminal (a phone, an agent) never has to infer it
from silence. Both endings are pinned here, against a stand-in process rather
than a real ``make release``: the watcher reads nothing from the release but
its pid and identity, the release record, ``dist/`` and the junit files, so a
child that makes those changes and exits is the whole contract.

MEASURED 2026-09-24, twice, on real releases: when a release failed after
``clean-dist`` had removed ``dist/``, the watcher exited 1 and printed nothing
-- ``dist="$(find dist/ ...)"`` fails under ``set -e`` -- so the one ending
that most needed a line got none.
"""

import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tests._fixtures.gitrepo import TmpGitRepo, git_env
from tests._fixtures.paths import CHECKOUT_ROOT

_SCRIPT = CHECKOUT_ROOT / "scripts" / "watch_release.sh"

_JUNIT_RED = (
    '<?xml version="1.0" encoding="utf-8"?><testsuites><testsuite name="pytest" '
    'errors="0" failures="5" skipped="0" tests="12" time="4.5">'
    '<testcase name="t" time="0.1"/></testsuite></testsuites>'
)


def _repo(tmp_path: Path) -> TmpGitRepo:
    """A repo shaped like otto-sh as far as the watcher looks: a script and a commit.

    The watcher finds its repo from its OWN path (``scripts/..``), so it is
    copied in rather than run from the dev tree -- which would read the dev
    tree's release record, ``dist/`` and junit reports instead of this one's.
    """
    repo = TmpGitRepo(tmp_path / "repo")
    (repo.root / "scripts").mkdir()
    shutil.copy2(_SCRIPT, repo.root / "scripts" / "watch_release.sh")
    # ``scripts`` is a namespace package (no __init__.py), importable from the repo root;
    # the record is read through release_record.py, which imports the others.
    for name in (
        "process_group.py",
        "release_record.py",
        "release_stages.py",
        "gate_stamp_common.py",
        "local_gate_status.py",
    ):
        shutil.copy2(CHECKOUT_ROOT / "scripts" / name, repo.root / "scripts" / name)
    # the session's whole venv, so the copy's interpreter has its packages (tomli)
    (repo.root / ".venv").symlink_to(sys.prefix, target_is_directory=True)
    repo.write("README", "r\n")
    repo.commit("base")
    return repo


def _watch(
    repo: TmpGitRepo, release_body: str, *, delay: float = 0, cwd: Path | None = None
) -> subprocess.CompletedProcess[str]:
    """Run *release_body* (bash) as the "release", and the watcher against its pid.

    THE RELEASE IS ORPHANED, not a child of this process: a real ``make
    release`` is never the watcher's caller's child, so the test orphans the
    body the same way.

    *delay* sleeps before the watcher starts (late attachment); *cwd* is the
    watcher's working directory (default the repo root).
    """
    env = {**git_env(repo.root), "POLL_SECONDS": "0.2"}
    pid = subprocess.run(
        ["bash", "-c", f"( {release_body} ) >/dev/null 2>&1 & echo $!"],
        cwd=repo.root,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    time.sleep(delay)
    return subprocess.run(
        ["bash", str(repo.root / "scripts" / "watch_release.sh"), pid],
        cwd=cwd or repo.root,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def _last_line(result: subprocess.CompletedProcess[str]) -> str:
    lines = result.stdout.strip().splitlines()
    assert lines, f"the watcher printed nothing; stderr: {result.stderr!r}"
    return lines[-1]


def test_a_release_that_dies_untagged_with_no_dist_ends_on_a_failed_line(tmp_path):
    """The measured case: no tag, and ``dist/`` gone because ``clean-dist`` ran."""
    repo = _repo(tmp_path)
    assert not (repo.root / "dist").exists()

    result = _watch(repo, "sleep 1")

    assert result.returncode == 0, result.stderr
    last = _last_line(result)
    assert last.startswith("FAILED:"), result.stdout
    assert "dist: <empty>" in last, last


def test_a_failed_line_names_the_red_lane_it_saw(tmp_path):
    """A RED lane is the likeliest reason; the ending should not make you scroll for it."""
    repo = _repo(tmp_path)
    junit = repo.root / "reports" / "junit" / "nox"
    junit.mkdir(parents=True)

    result = _watch(
        repo,
        f"sleep 0.5; printf '%s' '{_JUNIT_RED}' > {junit / 'tests_all-3.10.xml'}; sleep 1",
    )

    assert "LANE RED: nox/tests_all-3.10.xml" in result.stdout, result.stdout
    last = _last_line(result)
    assert last.startswith("FAILED:"), result.stdout
    assert "nox/tests_all-3.10.xml" in last, last


def _release_body(
    repo: TmpGitRepo,
    *,
    state: str,
    url: str = "",
    own_identity: bool,
    same_pid_other_start: bool = False,
    sleep: float = 3.0,
) -> str:
    """The bash body ``_watch`` orphans: sleep, then write a release record.

    The body's identity is ``$BASHPID``, never ``$$``: ``_watch`` runs it as
    ``( body ) &``, a subshell whose ``$$`` is the parent bash's pid while the
    watcher is handed ``$!``, the subshell's own pid. *same_pid_other_start*
    writes the watched pid with another start time: a reused pid's record.
    """
    if same_pid_other_start:
        ident = "pid=$BASHPID; started=1"
    elif own_identity:
        ident = (
            "pid=$BASHPID; started=$(./.venv/bin/python -c 'import sys; "
            "from scripts.process_group import process_start; "
            'print(process_start(int(sys.argv[1])))\' "$pid")'
        )
    else:
        ident = "pid=$((BASHPID + 1)); started=1"
    record = (
        f'{{"state": "{state}", "run": {{"pid": %s, "pid_started": %s}}, "head": null, '
        f'"tree": null, "version": "0.18.1", "url": "{url}", "written_at": "x"}}'
    )
    return (
        f"cd {repo.root}; {ident}; sleep {sleep}; mkdir -p reports/release; "
        f'printf \'{record}\' "$pid" "$started" > reports/release/record.json; exit 0'
    )


def test_a_release_whose_record_lands_ends_on_a_success_line(tmp_path):
    repo = _repo(tmp_path)
    result = _watch(
        repo,
        _release_body(repo, state="landed", url="https://x/pull/9", own_identity=True, sleep=1),
    )
    assert _last_line(result).startswith("SUCCESS:"), result.stdout + result.stderr
    assert "https://x/pull/9" in _last_line(result)


def test_a_tip_certified_record_is_a_success_too(tmp_path):
    repo = _repo(tmp_path)
    result = _watch(repo, _release_body(repo, state="tip-certified", own_identity=True))
    assert _last_line(result).startswith("SUCCESS:"), result.stdout + result.stderr


def test_a_record_from_another_run_is_not_a_success(tmp_path):
    repo = _repo(tmp_path)
    result = _watch(repo, _release_body(repo, state="landed", own_identity=False))
    last = _last_line(result)
    assert last.startswith("FAILED:"), result.stdout + result.stderr
    assert "another run's record: yes" in last


def test_the_watcher_reads_the_record_through_its_one_owner():
    """The record's schema lives in release_record.py; the watcher asks it for the fields."""
    script = _SCRIPT.read_text()
    assert "scripts/release_record.py read --fields state,run.pid,run.pid_started,url" in script
    assert "import json" not in script


def test_a_record_with_the_watched_pid_but_another_start_time_is_not_a_success(tmp_path):
    repo = _repo(tmp_path)
    body = _release_body(repo, state="landed", own_identity=False, same_pid_other_start=True)
    result = _watch(repo, body)
    last = _last_line(result)
    assert last.startswith("FAILED:"), result.stdout + result.stderr
    assert "another run's record: yes" in last


@pytest.mark.parametrize("state", ["pending", "certified"])
def test_a_run_that_dies_before_landing_is_a_failure_naming_the_state(tmp_path, state):
    repo = _repo(tmp_path)
    result = _watch(repo, _release_body(repo, state=state, own_identity=True))
    last = _last_line(result)
    assert last.startswith("FAILED:"), result.stdout + result.stderr
    assert f"record {state}" in last


def test_the_watcher_attaches_late_to_a_running_release(tmp_path):
    repo = _repo(tmp_path)
    # the body is 1 s in when the watcher reads its identity
    result = _watch(repo, _release_body(repo, state="landed", own_identity=True, sleep=3), delay=1)
    assert _last_line(result).startswith("SUCCESS:"), result.stdout + result.stderr


def test_the_watcher_runs_from_outside_the_checkout(tmp_path):
    repo = _repo(tmp_path)
    # REPO comes from the script's own path, the interpreter from $REPO/.venv
    result = _watch(repo, _release_body(repo, state="landed", own_identity=True), cwd=tmp_path)
    assert _last_line(result).startswith("SUCCESS:"), result.stdout + result.stderr


def test_a_pid_that_is_gone_at_start_is_refused(tmp_path):
    repo = _repo(tmp_path)
    # exited: its pid is free
    dead = subprocess.run(
        ["bash", "-c", "echo $BASHPID"], capture_output=True, text=True, check=False
    ).stdout.strip()
    result = subprocess.run(
        ["bash", str(repo.root / "scripts/watch_release.sh"), dead],
        cwd=repo.root,
        capture_output=True,
        text=True,
        env=git_env(repo.root),
        timeout=30,
        check=False,
    )
    # the identity read fails first; the old "no running make release" line
    # is reached only with no pid argument
    assert result.returncode == 1
    assert f"cannot read pid {dead}" in result.stderr


def test_the_watcher_judges_liveness_by_identity_not_by_kill_zero():
    # pid reuse is pinned at the predicate (test_process_group: a different start
    # time is not alive);
    # here: alive() is the ONLY liveness call
    text = _SCRIPT.read_text()
    assert "kill -0" not in text
    assert "process_group import alive" in text
    assert 'cd "$REPO"' in text
    assert ".venv/bin/python" in text
    # the interpreter is the venv's; the stage matcher may still mention
    # `uv run python` as a process-name pattern
    assert not re.search(r"uv run .*process_group", text)


def test_a_gh_pr_create_is_a_stage_of_its_own(tmp_path):
    repo = _repo(tmp_path)
    fake = repo.root / "bin"
    fake.mkdir()
    # ps shows `gh pr create 2`: the anchored matcher reads raw argv, where a
    # script would show its interpreter first
    (fake / "gh").write_text('#!/bin/bash\nexec -a "gh pr create" sleep 2\n')
    (fake / "gh").chmod(0o755)
    body = f"cd {repo.root}; PATH={fake}:$PATH gh pr create --title x; " + _release_body(
        repo, state="landed", own_identity=True, sleep=0
    )
    result = _watch(repo, body)
    assert "STAGE: gh pr create" in result.stdout, result.stdout + result.stderr
    assert _last_line(result).startswith("SUCCESS:")


def _watcher_stages(script: str) -> "list[str]":
    """The words of the script's ``STAGES=( … )`` array, backslash continuations joined."""
    match = re.search(r"^STAGES=\(([^)]*)\)", script.replace("\\\n", " "), re.MULTILINE)
    assert match is not None, "the watcher has no STAGES=( … ) array"
    return match.group(1).split()


def _release_make_goals(makefile: str) -> "list[str]":
    """The ``$(MAKE) <goal>`` words of the ``release:`` recipe, in recipe order."""
    match = re.search(r"^release:[^\n]*\n((?:\t[^\n]*\n)+)", makefile, re.MULTILINE)
    assert match is not None, "the Makefile has no `release:` rule with a recipe"
    return re.findall(r"\$\(MAKE\)\s+([a-zA-Z0-9_.-]+)", match.group(1))


def test_the_watcher_s_stages_are_the_release_recipe_s_make_goals_in_order():
    """``STAGE:`` lines name the release's gates; a stale list names stages that no longer run."""
    red = _release_make_goals("release: ## x\n\t@$(MAKE) a \\\n\t\t&& $(MAKE) b\n")
    assert red == ["a", "b"]
    assert _watcher_stages("STAGES=(b \\\n        a)\n") != red, "order must be compared"
    goals = _release_make_goals((CHECKOUT_ROOT / "Makefile").read_text())
    assert goals.count("coverage") == 1, goals
    assert _watcher_stages(_SCRIPT.read_text()) == goals
