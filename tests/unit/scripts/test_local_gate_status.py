"""Pins for scripts/local_gate_status.py: the local-gate status, its verifier and CI's verdict."""

import hashlib
import json
import os
import subprocess
import sys

import pytest

import scripts.local_gate_status as lgs
import tests._fixtures.fake_gh as fg
from scripts import gate_scope as gs
from tests._fixtures.gitrepo import TmpGitRepo, git_env
from tests._fixtures.paths import CHECKOUT_ROOT

pytestmark = pytest.mark.interpreter_agnostic

SHA = "a" * 40
TREE = "b" * 40
BASE = "c" * 40


def _desc() -> lgs.Description:
    return lgs.Description(TREE, BASE, lgs.lane_digest(["make check-python", "make collect-check"]))


class FakeClock:
    """A monotonic clock its own :meth:`sleep` advances; the slept intervals are kept."""

    def __init__(self) -> None:
        self.now = 0.0
        self.slept: "list[float]" = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def test_poll_charges_the_probe_s_own_time_against_the_deadline():
    clock = FakeClock()
    probes = []

    def slow_probe() -> bool:
        probes.append(clock.now)
        clock.now += 10  # the probe itself takes 10 s
        return False

    held = lgs.poll(slow_probe, timeout_s=30, interval=10, sleep=clock.sleep, clock=clock)
    assert not held
    assert probes == [0, 20]
    assert clock.slept == [10]


def test_poll_with_no_timeout_probes_once_and_never_sleeps():
    def no_sleep(_s: float) -> None:
        pytest.fail("slept")

    calls = []
    assert not lgs.poll(lambda: calls.append(1) and False, timeout_s=0, interval=10, sleep=no_sleep)
    assert calls == [1]


def test_poll_stops_as_soon_as_the_predicate_holds():
    clock = FakeClock()
    answers = iter([False, False, True])
    assert lgs.poll(lambda: next(answers), timeout_s=60, interval=5, sleep=clock.sleep, clock=clock)
    assert clock.slept == [5, 5]


def test_the_description_is_113_characters_and_round_trips():
    d = lgs.Description(TREE, BASE, lgs.lane_digest(["make check-python", "make coverage-bed"]))
    text = lgs.format_description(d)
    assert len(text) == 113 == lgs.DESCRIPTION_LEN
    assert text.startswith("v1 tree=")
    assert lgs.parse_description(text) == d
    assert lgs.parse_description("v0 junk") is None


def test_the_digest_is_the_first_12_hex_of_sha256_over_newline_joined_keys():
    assert lgs.lane_digest([]) == hashlib.sha256(b"").hexdigest()[:12]
    assert lgs.lane_digest(["a", "b"]) == hashlib.sha256(b"a\nb").hexdigest()[:12]


def test_post_sends_the_status_and_reads_a_missing_commit_as_push_first():
    gh = fg.scripted_gh([fg.GhReply(["statuses/" + SHA], stdout="{}")])
    out = lgs.post_status("o/r", SHA, _desc(), state="success", gh=gh)
    assert out.ok
    assert gh.calls[0][:4] == ["api", "-X", "POST", f"repos/o/r/statuses/{SHA}"]
    assert "-f" in gh.calls[0]
    assert "context=local-gate" in gh.calls[0]
    assert "state=success" in gh.calls[0]
    gh = fg.scripted_gh(
        [fg.GhReply(["statuses/"], status=1, stderr="HTTP 422: No commit found for SHA: " + SHA)]
    )
    out = lgs.post_status("o/r", SHA, _desc(), state="success", gh=gh)
    assert not out.ok
    assert out.no_commit
    assert out.message == "push the branch, then make gate-status"


def test_run_gh_bounds_a_hung_gh_and_reports_the_timeout(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "gh").write_text("#!/bin/sh\nsleep 5\n")
    (bin_dir / "gh").chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    out = lgs.run_gh(["api", "x"], timeout=0.2)
    assert out.status == 124
    assert out.stderr == "gh timed out after 0.2s"


def test_an_unauthenticated_gh_is_reported_in_its_own_words():
    gh = fg.scripted_gh(
        [
            fg.GhReply(
                [], status=4, stderr="To get started with GitHub CLI, please run:  gh auth login"
            )
        ]
    )
    out = lgs.post_status("o/r", SHA, _desc(), state="success", gh=gh)
    assert not out.ok
    assert not out.no_commit
    assert "gh auth login" in out.message


def test_await_matches_a_success_with_the_same_description():
    d = _desc()
    body = json.dumps(
        {
            "statuses": [
                {
                    "context": "local-gate",
                    "state": "success",
                    "description": lgs.format_description(d),
                }
            ]
        }
    )
    gh = fg.scripted_gh([fg.GhReply(["commits/", "/status"], stdout=body)])
    clock = FakeClock()
    got = lgs.await_status("o/r", SHA, d, gh=gh, timeout_s=30, sleep=clock.sleep, clock=clock)
    assert got == []


def test_await_names_the_mismatching_field_and_does_not_keep_polling():
    posted = lgs.Description(TREE, BASE, "0" * 12)
    want = _desc()
    body = json.dumps(
        {
            "statuses": [
                {
                    "context": "local-gate",
                    "state": "success",
                    "description": lgs.format_description(posted),
                }
            ]
        }
    )
    gh = fg.scripted_gh([fg.GhReply(["commits/"], stdout=body)])
    clock = FakeClock()
    lines = lgs.await_status(
        "o/r", SHA, want, gh=gh, timeout_s=5, interval=1, sleep=clock.sleep, clock=clock
    )
    assert "lane digest" in lines[0]
    assert "tree" not in lines[0]
    assert lines[-1] == "run make gate-tip on this head, then make gate-status"
    assert clock.slept == []
    assert len(gh.calls) == 1


def test_both_fake_runners_consume_the_queue_the_same_way(tmp_path):
    replies = [
        fg.GhReply(["run", "list"], stdout="[]"),
        fg.GhReply(["run", "list"], stdout="[1]"),
        fg.GhReply(["api"], stdout="ok", repeat=True),
    ]
    calls = [["run", "list"], ["api", "x"], ["run", "list"], ["api", "y"], ["run", "list"]]
    gh = fg.scripted_gh(replies)
    in_process = [(r.status, r.stdout) for r in map(gh, calls)]
    bin_dir = fg.install_fake_gh(tmp_path / "bin", replies)
    env = {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}
    on_path = [
        (d.returncode, d.stdout)
        for d in (
            subprocess.run(["gh", *c], env=env, capture_output=True, text=True, check=False)
            for c in calls
        )
    ]
    assert in_process == on_path == [(0, "[]"), (0, "ok"), (0, "[1]"), (0, "ok"), (1, "")]


def test_await_polls_an_absent_status_then_gives_the_remedy():
    gh = fg.scripted_gh(
        [fg.GhReply(["commits/"], stdout=json.dumps({"statuses": []}), repeat=True)]
    )
    clock = FakeClock()
    lines = lgs.await_status(
        "o/r", SHA, _desc(), gh=gh, timeout_s=20, interval=10, sleep=clock.sleep, clock=clock
    )
    assert clock.slept == [10, 10]
    assert len(gh.calls) == 3
    assert lines[0] == f"gate-admission: no local-gate success on {SHA[:12]} after 20s"


def _runs(*items):
    return json.dumps(
        [
            {
                "databaseId": i,
                "status": s,
                "conclusion": c,
                "url": f"u{i}",
                "createdAt": f"2026-11-0{i}T00:00:00Z",
            }
            for i, s, c in items
        ]
    )


def _jobs(_status="completed", **by_name):
    return json.dumps(
        {
            "url": "run-url",
            "status": _status,
            "jobs": [
                {"name": n, "status": "completed" if c else "in_progress", "conclusion": c}
                for n, c in by_name.items()
            ],
        }
    )


REQ = ["tests_hostless-3.10", "lint-python", "local-gate", "gate-admission"]


def test_ci_verdict_is_green_when_required_contexts_succeed_and_optional_failures_do_not_count():
    gh = fg.scripted_gh(
        [
            fg.GhReply(["run", "list"], stdout=_runs((1, "completed", "success"))),
            fg.GhReply(
                ["run", "view"],
                stdout=_jobs(
                    **{
                        "tests_hostless-3.10": "success",
                        "lint-python": "success",
                        "busybox-artifacts (x86_64)": "failure",
                    }
                ),
            ),
        ]
    )
    v = lgs.ci_verdict("o/r", SHA, REQ, gh=gh, sleep=lambda s: None)
    assert v.green
    assert not v.pending
    assert v.run_url == "run-url"
    assert "--event" in gh.calls[0]
    assert "push" in gh.calls[0]
    assert "--workflow" in gh.calls[0]
    assert "ci.yml" in gh.calls[0]


def test_ci_verdict_reports_a_missing_context_a_pending_job_and_a_failure():
    gh = fg.scripted_gh(
        [
            fg.GhReply(["run", "list"], stdout=_runs((1, "completed", "failure"))),
            fg.GhReply(
                ["run", "view"],
                stdout=_jobs(**{"tests_hostless-3.10": None, "lint-python": "failure"}),
            ),
        ]
    )
    v = lgs.ci_verdict("o/r", SHA, REQ, gh=gh, sleep=lambda s: None)
    assert not v.green
    assert v.pending
    assert v.detail == ["lint-python: failure"]
    gh = fg.scripted_gh(
        [
            fg.GhReply(["run", "list"], stdout=_runs((1, "completed", "failure"))),
            fg.GhReply(["run", "view"], stdout=_jobs(**{"lint-python": "failure"})),
        ]
    )
    v = lgs.ci_verdict("o/r", SHA, REQ, gh=gh, sleep=lambda s: None)
    assert not v.green
    assert not v.pending
    assert v.detail == ["tests_hostless-3.10: no job", "lint-python: failure"]


def test_a_required_job_absent_from_an_unfinished_run_is_pending_and_optional_jobs_never_gate():
    gh = fg.scripted_gh(
        [
            fg.GhReply(["run", "list"], stdout=_runs((1, "in_progress", None))),
            fg.GhReply(["run", "view"], stdout=_jobs("in_progress", **{"lint-python": "success"})),
        ]
    )
    v = lgs.ci_verdict("o/r", SHA, REQ, gh=gh, sleep=lambda s: None)
    assert v.pending
    assert not v.green
    assert v.detail == []
    gh = fg.scripted_gh(
        [
            fg.GhReply(["run", "list"], stdout=_runs((1, "in_progress", None))),
            fg.GhReply(
                ["run", "view"],
                stdout=_jobs(
                    "in_progress",
                    **{
                        "tests_hostless-3.10": "success",
                        "lint-python": "success",
                        "busybox-artifacts (x86_64)": None,
                    },
                ),
            ),
        ]
    )
    v = lgs.ci_verdict("o/r", SHA, REQ, gh=gh, sleep=lambda s: None)
    assert v.green
    assert not v.pending


def test_an_unfinished_run_with_no_jobs_yet_is_pending_not_a_missing_context():
    gh = fg.scripted_gh(
        [
            fg.GhReply(["run", "list"], stdout=_runs((1, "queued", None))),
            fg.GhReply(["run", "view"], stdout=_jobs("queued")),
        ]
    )
    v = lgs.ci_verdict("o/r", SHA, REQ, gh=gh, sleep=lambda s: None)
    assert v.pending
    assert not v.green
    assert v.detail == []


def test_ci_verdict_takes_the_newest_run_and_waits_for_one_to_appear():
    gh = fg.scripted_gh(
        [
            fg.GhReply(["run", "list"], stdout="[]"),
            fg.GhReply(
                ["run", "list"],
                stdout=_runs((1, "completed", "failure"), (2, "completed", "success")),
            ),
            fg.GhReply(
                ["run", "view", "2"],
                stdout=_jobs(**{"tests_hostless-3.10": "success", "lint-python": "success"}),
            ),
        ]
    )
    clock = FakeClock()
    v = lgs.ci_verdict(
        "o/r", SHA, REQ, gh=gh, appear_s=20, interval=10, sleep=clock.sleep, clock=clock
    )
    assert v.green
    assert clock.slept == [10]
    assert gh.calls[-1][:5] == ["run", "view", "--repo", "o/r", "2"]


def test_ci_verdict_refuses_when_no_run_appears():
    gh = fg.scripted_gh([fg.GhReply(["run", "list"], stdout="[]", repeat=True)])
    clock = FakeClock()
    v = lgs.ci_verdict(
        "o/r", SHA, REQ, gh=gh, appear_s=5, interval=5, sleep=clock.sleep, clock=clock
    )
    assert not v.green
    assert v.detail == [f"no ci.yml push run on {SHA[:12]}"]
    assert clock.slept == [5]


def test_ci_verdict_names_a_failed_run_listing_not_a_missing_run():
    gh = fg.scripted_gh(
        [fg.GhReply(["run", "list"], status=1, stderr="HTTP 403: Resource not accessible")]
    )
    v = lgs.ci_verdict("o/r", SHA, REQ, gh=gh, appear_s=0)
    assert not v.green
    assert not v.pending
    assert v.detail == [f"no ci.yml push run on {SHA[:12]}", "HTTP 403: Resource not accessible"]
    gh = fg.scripted_gh([fg.GhReply(["run", "list"], stdout="{}")])
    v = lgs.ci_verdict("o/r", SHA, REQ, gh=gh, appear_s=0)
    assert v.detail == [f"no ci.yml push run on {SHA[:12]}", "gh returned unparsable JSON"]


def test_the_cli_ci_verdict_prints_a_failed_listing_s_cause_and_exits_1(
    tmp_path, monkeypatch, capsys
):
    bin_dir = fg.install_fake_gh(
        tmp_path / "bin",
        [fg.GhReply(["run", "list"], status=1, stderr="HTTP 403: Resource not accessible")],
    )
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    required = tmp_path / "required.json"
    required.write_text(json.dumps(REQ))
    args = ["ci-verdict", "--repo-slug", "o/r", "--sha", SHA, "--required", str(required)]
    assert lgs.main(args) == 1
    assert "HTTP 403: Resource not accessible" in capsys.readouterr().out


def test_admit_reports_a_failed_run_listing_and_exits_1(capsys):
    gh = fg.scripted_gh(
        [fg.GhReply(["run", "list"], status=1, stderr="HTTP 403: Resource not accessible")]
    )
    assert lgs.admit_pr_run("o/r", SHA, gh=gh) == 1
    err = capsys.readouterr().err
    assert err.strip() == (
        "gate-status: cannot list the pull request's runs: HTTP 403: Resource not accessible"
    )
    assert len(gh.calls) == 1


def test_admit_reads_a_listing_that_is_not_a_list_as_malformed_not_as_no_run(capsys):
    gh = fg.scripted_gh([fg.GhReply(["run", "list"], stdout="{}")])
    assert lgs.admit_pr_run("o/r", SHA, gh=gh) == 1
    captured = capsys.readouterr()
    assert "no pull request run yet" not in captured.out
    assert captured.err.strip() == (
        "gate-status: cannot list the pull request's runs: gh returned unparsable JSON"
    )


def test_admit_waits_out_an_in_progress_admission_and_reruns_a_failed_one():
    # the admission job's final poll missed a status posted a moment later: the job failed,
    # and the late `make gate-status` re-runs it
    gh = fg.scripted_gh(
        [
            fg.GhReply(["run", "list"], stdout=_runs((7, "in_progress", None))),
            fg.GhReply(["run", "view"], stdout=_jobs(**{"gate-admission": None})),
            fg.GhReply(["run", "view"], stdout=_jobs(**{"gate-admission": "failure"})),
            fg.GhReply(["run", "rerun"], stdout=""),
        ]
    )
    clock = FakeClock()
    assert lgs.admit_pr_run("o/r", SHA, gh=gh, wait_s=50, sleep=clock.sleep, clock=clock) == 0
    assert clock.slept == [10]
    assert gh.calls[-1] == ["run", "rerun", "--repo", "o/r", "7", "--failed"]


def test_admit_with_no_run_is_success():
    gh = fg.scripted_gh([fg.GhReply(["run", "list"], stdout="[]")])
    assert lgs.admit_pr_run("o/r", SHA, gh=gh, sleep=lambda s: None) == 0
    assert len(gh.calls) == 1


def test_the_fake_gh_on_path_answers_and_logs(tmp_path):
    bin_dir = fg.install_fake_gh(tmp_path / "bin", [fg.GhReply(["api"], stdout='{"ok": 1}')])
    env = {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}
    done = subprocess.run(["gh", "api", "x"], env=env, capture_output=True, text=True, check=False)
    assert done.returncode == 0
    assert done.stdout == '{"ok": 1}'
    assert fg.gh_calls(bin_dir) == [["api", "x"]]


def test_the_cli_post_uses_gh_on_path(tmp_path):
    bin_dir = fg.install_fake_gh(tmp_path / "bin", [fg.GhReply(["statuses/"], stdout="{}")])
    env = {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}
    done = subprocess.run(
        [
            sys.executable,
            "scripts/local_gate_status.py",
            "post",
            "--repo-slug",
            "o/r",
            "--sha",
            SHA,
            "--tree",
            TREE,
            "--base",
            BASE,
            "--digest",
            "0" * 12,
        ],
        cwd=CHECKOUT_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    assert fg.gh_calls(bin_dir)[0][:3] == ["api", "-X", "POST"]


def test_required_contexts_is_the_committed_list():
    got = lgs.required_contexts(CHECKOUT_ROOT / ".github/required_contexts.json")
    assert len(got) == 21
    assert "local-gate" in got
    assert "gate-admission" in got
    assert "uv-floor" in got
    assert "busybox-artifacts (x86_64)" not in got
    assert "report-failure" not in got


def _landing(tmp_path) -> "tuple[TmpGitRepo, str, str]":
    """A scratch repo with a base commit and a tip that changes one src module."""
    repo = TmpGitRepo(tmp_path / "repo")
    repo.write("src/otto/cli/x.py", "x = 1\n")
    base = repo.commit("base")
    repo.write("src/otto/cli/x.py", "x = 2\n")
    return repo, base, repo.commit("tip")


def _await_by_path(repo: TmpGitRepo, bin_dir, *, sha: str, base: str):
    # Merged, not verbatim: the Python child needs the session's environment; the
    # git it spawns gets the fixture's hermetic settings (the fixture's PATH and HOME
    # are for git alone, so they stay the session's, with the fake gh in front).
    env = {
        **os.environ,
        **{k: v for k, v in git_env(repo.root).items() if k not in {"PATH", "HOME"}},
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
    }
    return subprocess.run(
        [
            *[sys.executable, "scripts/local_gate_status.py", "await", "--repo-slug", "o/r"],
            *["--repo", str(repo.root), "--sha", sha, "--base", base],
            *["--attempts", "1", "--interval", "0"],
        ],
        cwd=CHECKOUT_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _posted(description: str) -> str:
    status = {"context": lgs.CONTEXT, "state": "success", "description": description}
    return json.dumps({"statuses": [status]})


def test_the_cli_await_by_path_computes_the_plan_s_description_and_matches_it(tmp_path):
    repo, base, tip = _landing(tmp_path)
    want = lgs.expected(gs.plan(repo.root, base, tip, git_env(repo.root)))
    assert want.tree == repo.git("rev-parse", f"{tip}^{{tree}}").strip()
    assert want.base == base
    posted = fg.GhReply(["commits/" + tip], stdout=_posted(lgs.format_description(want)))
    bin_dir = fg.install_fake_gh(tmp_path / "bin", [posted])
    done = _await_by_path(repo, bin_dir, sha=tip, base=base)
    assert done.returncode == 0, done.stderr
    assert fg.gh_calls(bin_dir) == [["api", f"repos/o/r/commits/{tip}/status"]]
    # the control: a success for another lane set is refused, the field named
    other = lgs.Description(want.tree, want.base, lgs.lane_digest(["make check-python"]))
    stale = fg.GhReply(["commits/" + tip], stdout=_posted(lgs.format_description(other)))
    bin_dir = fg.install_fake_gh(tmp_path / "bin2", [stale])
    done = _await_by_path(repo, bin_dir, sha=tip, base=base)
    assert done.returncode == 1
    assert "lane digest" in done.stderr


def test_the_cli_await_by_path_reports_an_unanswerable_plan_in_one_line(tmp_path):
    repo, base, _tip = _landing(tmp_path)
    bin_dir = fg.install_fake_gh(tmp_path / "bin", [])
    done = _await_by_path(repo, bin_dir, sha="f" * 40, base=base)
    assert done.returncode == 1
    assert "Traceback" not in done.stderr
    assert len(done.stderr.strip().splitlines()) == 1, done.stderr
    assert done.stderr.startswith("gate-admission: cannot compute the expected description")
    assert fg.gh_calls(bin_dir) == []


def test_await_reports_a_lane_the_oracle_does_not_classify_in_one_line(
    tmp_path, monkeypatch, capsys
):
    """The same refusal gate_tip's pre-push check gives, never a traceback."""
    repo, base, tip = _landing(tmp_path)
    monkeypatch.setattr(gs, "TEST_SUITE_LANES", [*gs.TEST_SUITE_LANES, "make no-such-goal"])
    bin_dir = fg.install_fake_gh(tmp_path / "bin", [])
    args = lgs._parser().parse_args(
        [
            *["await", "--repo-slug", "o/r", "--repo", str(repo.root)],
            *["--sha", tip, "--base", base, "--attempts", "1", "--interval", "0"],
        ]
    )
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    assert lgs._await_command(args) == 1
    err = capsys.readouterr().err
    assert len(err.strip().splitlines()) == 1, err
    assert "does not classify: make no-such-goal" in err
    assert fg.gh_calls(bin_dir) == []


def test_head_status_raises_on_a_gh_failure_with_its_stderr():
    from scripts.gate_stamp_common import GateError

    gh = fg.scripted_gh([fg.GhReply([], status=1, stderr="HTTP 403: Resource not accessible")])
    with pytest.raises(GateError, match="HTTP 403: Resource not accessible"):
        lgs.head_status("o/r", SHA, gh=gh)


@pytest.mark.parametrize("code", ["HTTP 404", "HTTP 422"])
def test_head_status_is_none_when_the_sha_is_not_on_github_yet(code):
    gh = fg.scripted_gh([fg.GhReply([], status=1, stderr=f"gh: Not Found ({code})")])
    assert lgs.head_status("o/r", SHA, gh=gh) is None


def test_await_command_reports_an_unreadable_status_and_exits_1(tmp_path, monkeypatch, capsys):
    repo, base, tip = _landing(tmp_path)
    bin_dir = fg.install_fake_gh(
        tmp_path / "bin", [fg.GhReply([], status=1, stderr="HTTP 403: Resource not accessible")]
    )
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    args = lgs._parser().parse_args(
        [
            *["await", "--repo-slug", "o/r", "--repo", str(repo.root)],
            *["--sha", tip, "--base", base, "--attempts", "3", "--interval", "0"],
        ]
    )
    assert lgs._await_command(args) == 1
    err = capsys.readouterr().err
    assert "gate-admission: cannot read the local-gate status:" in err
    assert "HTTP 403" in err
    assert len(fg.gh_calls(bin_dir)) == 1


def test_run_gh_reports_a_missing_gh(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path))
    out = lgs.run_gh(["api", "x"])
    assert out.status == 127
    assert out.stderr == "gh: not found on PATH"


def test_ci_verdict_names_unparsable_json_from_gh():
    gh = fg.scripted_gh(
        [
            fg.GhReply(["run", "list"], stdout=_runs((1, "completed", "success"))),
            fg.GhReply(["run", "view"], stdout="<html>"),
        ]
    )
    v = lgs.ci_verdict("o/r", SHA, REQ, gh=gh, sleep=lambda s: None)
    assert not v.green
    assert not v.pending
    assert v.detail == ["gh returned unparsable JSON"]
    gh = fg.scripted_gh([fg.GhReply(["run", "list"], stdout="not json", repeat=True)])
    clock = FakeClock()
    v = lgs.ci_verdict(
        "o/r", SHA, REQ, gh=gh, appear_s=10, interval=10, sleep=clock.sleep, clock=clock
    )
    assert v.detail == [f"no ci.yml push run on {SHA[:12]}", "gh returned unparsable JSON"]


def test_admit_fails_when_the_admission_job_never_finishes():
    gh = fg.scripted_gh(
        [
            fg.GhReply(["run", "list"], stdout=_runs((7, "in_progress", None))),
            fg.GhReply(["run", "view"], stdout=_jobs(**{"gate-admission": None}), repeat=True),
        ]
    )
    clock = FakeClock()
    assert lgs.admit_pr_run("o/r", SHA, gh=gh, wait_s=10, sleep=clock.sleep, clock=clock) == 1
    assert clock.slept == [10]


def test_admit_fails_when_the_rerun_is_refused():
    gh = fg.scripted_gh(
        [
            fg.GhReply(["run", "list"], stdout=_runs((7, "completed", "failure"))),
            fg.GhReply(["run", "view"], stdout=_jobs(**{"gate-admission": "failure"})),
            fg.GhReply(["run", "rerun"], status=1, stderr="HTTP 403"),
        ]
    )
    clock = FakeClock()
    assert lgs.admit_pr_run("o/r", SHA, gh=gh, wait_s=10, sleep=clock.sleep, clock=clock) == 1


def test_the_cli_ci_verdict_wait_minutes_polls_while_pending(tmp_path, monkeypatch, capsys):
    pending = _jobs("in_progress", **{"tests_hostless-3.10": None})
    done = _jobs(**{"tests_hostless-3.10": "success", "lint-python": "success"})
    run = _runs((1, "in_progress", None))
    replies = [
        fg.GhReply(["run", "list"], stdout=run),
        fg.GhReply(["run", "view"], stdout=pending),
        fg.GhReply(["run", "list"], stdout=run),
        fg.GhReply(["run", "view"], stdout=done),
    ]
    bin_dir = fg.install_fake_gh(tmp_path / "bin", replies)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    slept = []
    monkeypatch.setattr(lgs, "CLI_SLEEP", slept.append)
    required = tmp_path / "required.json"
    required.write_text(json.dumps(REQ))
    args = ["ci-verdict", "--repo-slug", "o/r", "--sha", SHA, "--required", str(required)]
    assert lgs.main([*args, "--wait-minutes", "1"]) == 0
    assert slept == [10.0]
    assert len(fg.gh_calls(bin_dir)) == 4


_REQ_JOBS = [c for c in REQ if c not in lgs.EXCLUDED_FROM_VERDICT]


def _pending_then(final: str) -> "list[fg.GhReply]":
    """One pending read of run 1, then *final* for ``lint-python`` from then on."""
    return [
        fg.GhReply(["run", "list"], stdout=_runs((1, "in_progress", None))),
        fg.GhReply(["run", "view"], stdout=_jobs("in_progress", **dict.fromkeys(_REQ_JOBS))),
        fg.GhReply(["run", "list"], stdout=_runs((1, "completed", final)), repeat=True),
        fg.GhReply(
            ["run", "view"],
            stdout=_jobs(**{**dict.fromkeys(_REQ_JOBS, "success"), "lint-python": final}),
            repeat=True,
        ),
    ]


@pytest.mark.parametrize(("final", "green"), [("success", True), ("failure", False)])
def test_await_verdict_keeps_the_verdict_that_settled(final, green):
    clock = FakeClock()
    gh = fg.scripted_gh(_pending_then(final))
    v = lgs.await_verdict(
        "o/r", SHA, REQ, wait_s=60, interval=10, gh=gh, sleep=clock.sleep, clock=clock
    )
    assert v.green is green
    assert not v.pending
    assert v.detail == ([] if green else ["lint-python: failure"])
    assert clock.slept == [10]


def test_await_verdict_returns_the_pending_verdict_at_the_deadline():
    clock = FakeClock()
    gh = fg.scripted_gh(
        [
            fg.GhReply(["run", "list"], stdout=_runs((1, "in_progress", None)), repeat=True),
            fg.GhReply(
                ["run", "view"],
                stdout=_jobs("in_progress", **dict.fromkeys(_REQ_JOBS)),
                repeat=True,
            ),
        ]
    )
    v = lgs.await_verdict(
        "o/r", SHA, REQ, wait_s=30, interval=10, gh=gh, sleep=clock.sleep, clock=clock
    )
    assert v.pending
    assert not v.green
    assert clock.slept == [10, 10, 10]


def test_await_verdict_with_no_wait_reads_once_and_never_sleeps():
    def no_sleep(_s: float) -> None:
        pytest.fail("slept")

    gh = fg.scripted_gh(_pending_then("success"))
    v = lgs.await_verdict("o/r", SHA, REQ, wait_s=0, interval=10, gh=gh, sleep=no_sleep)
    assert v.pending
    assert [c[:2] for c in gh.calls] == [["run", "list"], ["run", "view"]]


_FORBIDDEN = "HTTP 403: Resource not accessible"


def test_a_gh_failure_with_no_stderr_is_never_an_empty_error(capsys):
    gh = fg.scripted_gh([fg.GhReply(["run", "list"], status=1, stderr="")])
    assert lgs.admit_pr_run("o/r", SHA, gh=gh) == 1
    err = capsys.readouterr().err
    assert "gh exited 1" in err
    assert "no pull request run yet" not in err


def test_admit_stops_at_a_failed_run_view_and_names_it(capsys):
    def no_sleep(_s: float) -> None:
        pytest.fail("slept: a failed view must not be waited out")

    gh = fg.scripted_gh(
        [
            fg.GhReply(["run", "list"], stdout=_runs((7, "in_progress", None))),
            fg.GhReply(["run", "view"], status=1, stderr=_FORBIDDEN),
        ]
    )
    assert lgs.admit_pr_run("o/r", SHA, gh=gh, sleep=no_sleep) == 1
    err = capsys.readouterr().err.strip()
    assert err == f"gate-status: cannot read the pull request's run: {_FORBIDDEN}"
    assert len(gh.calls) == 2


def test_await_verdict_probes_never_overrun_the_deadline_by_more_than_one_interval():
    clock = FakeClock()
    gh = fg.scripted_gh([fg.GhReply(["run", "list"], stdout="[]", repeat=True)])
    v = lgs.await_verdict(
        "o/r", SHA, REQ, wait_s=30, interval=10, gh=gh, sleep=clock.sleep, clock=clock
    )
    assert clock.now <= 30 + 10
    assert clock.slept == [10, 10, 10]
    assert not v.green
    assert v.detail == [f"no ci.yml push run on {SHA[:12]}"]


def test_await_verdict_waits_out_a_failing_listing_and_settles_green():
    clock = FakeClock()
    gh = fg.scripted_gh(
        [
            fg.GhReply(["run", "list"], status=1, stderr=_FORBIDDEN),
            fg.GhReply(["run", "list"], status=1, stderr=_FORBIDDEN),
            fg.GhReply(["run", "list"], stdout=_runs((1, "completed", "success"))),
            fg.GhReply(
                ["run", "view"],
                stdout=_jobs(**dict.fromkeys(_REQ_JOBS, "success")),
            ),
        ]
    )
    v = lgs.await_verdict(
        "o/r", SHA, REQ, wait_s=60, interval=10, gh=gh, sleep=clock.sleep, clock=clock
    )
    assert v.green
    assert clock.slept == [10, 10]


def test_await_verdict_returns_a_listing_that_kept_failing_with_its_cause():
    clock = FakeClock()
    gh = fg.scripted_gh([fg.GhReply(["run", "list"], status=1, stderr=_FORBIDDEN, repeat=True)])
    v = lgs.await_verdict(
        "o/r", SHA, REQ, wait_s=30, interval=10, gh=gh, sleep=clock.sleep, clock=clock
    )
    assert not v.green
    assert not v.pending
    assert v.unread
    assert _FORBIDDEN in v.detail
    assert clock.slept == [10, 10, 10]
