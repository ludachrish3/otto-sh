"""``scripts/ci_failure_report.py``: the issue a red run on ``main`` files (#799).

The auto-filed issue used to say "check the job logs to see which of <twenty
jobs> failed". It now names the failed jobs and each one's first failing
line, and for a landing says whether the landing's diff holds the failing
test file, which is the question docs/contributing.md's "When main goes red"
asks first.

The log lines below are the real ones from the four red runs of 2026-11.
"""

import json
from pathlib import Path

import pytest

from scripts.ci_failure_report import (
    Run,
    collect,
    failing_file,
    failing_lines,
    main,
    render,
)
from tests._fixtures.gitrepo import TmpGitRepo, git_env

# The subject is a repository script, not otto under this interpreter.
pytestmark = pytest.mark.interpreter_agnostic

_TS = "2026-11-12T03:14:15.1234567Z "
_UNDO = (
    "FAILED tests/e2e/monitor/dashboard/test_undo.py::"
    "test_toast_is_reachable_with_the_events_panel_open[chromium]@dashboard_undo"
    " - AssertionError: Locator expected to be visible"
)
_SNMP = (
    "FAILED tests/unit/test_snmp_leaf.py::"
    "test_the_survey_reports_a_timeout_row_for_a_cancelled_real_get - assert 0 == 1"
)


def _log(*lines: str) -> str:
    return "".join(f"{_TS}{line}\n" for line in lines)


# ─── failing_lines ──────────────────────────────────────────────────────────


def test_the_summary_line_wins_over_the_progress_line_before_it():
    """xdist's progress line holds FAILED too, but not the assertion."""
    log = _log(
        "[gw0] [ 98%] FAILED tests/unit/test_snmp_leaf.py::test_x",
        "=========================== short test summary info ===========================",
        _SNMP,
        "##[error]Process completed with exit code 1.",
    )
    assert failing_lines(log) == [_SNMP]


def test_every_failing_test_of_the_job_is_reported_once_in_the_logs_order():
    """The summary is printed once per pytest leg; a session with two legs may repeat a line."""
    log = _log(_UNDO, _SNMP, "some output", _UNDO)
    assert failing_lines(log) == [_UNDO, _SNMP]


def test_a_coloured_summary_line_is_read_without_its_escapes():
    log = _log("\x1b[31mFAILED\x1b[0m tests/unit/test_a.py::\x1b[1mtest_b\x1b[0m - boom")
    assert failing_lines(log) == ["FAILED tests/unit/test_a.py::test_b - boom"]


def test_a_setup_or_collection_error_is_a_failure_line():
    log = _log("ERROR tests/unit/test_a.py - ImportError: no module named x", _SNMP)
    assert failing_lines(log) == [
        "ERROR tests/unit/test_a.py - ImportError: no module named x",
        _SNMP,
    ]


def test_a_captured_log_record_at_error_level_is_not_a_failure_line():
    """pytest prints captured records as ``ERROR    logger:file.py:12 text``."""
    log = _log("ERROR    otto.link:link.py:88 connection lost", _SNMP)
    assert failing_lines(log) == [_SNMP]


def test_a_job_with_no_pytest_failure_reports_its_first_error_annotation():
    log = _log(
        "building docs",
        "##[error]docs/contributing.md:12: WARNING: undefined label",
        "##[error]Process completed with exit code 2.",
    )
    assert failing_lines(log) == ["docs/contributing.md:12: WARNING: undefined label"]


def test_a_log_with_nothing_recognisable_reports_nothing():
    assert failing_lines(_log("all good", "bye")) == []
    assert failing_lines("") == []


# ─── failing_file ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("line", "path"),
    [
        (_UNDO, "tests/e2e/monitor/dashboard/test_undo.py"),
        (_SNMP, "tests/unit/test_snmp_leaf.py"),
        ("ERROR tests/unit/test_a.py - ImportError: x", "tests/unit/test_a.py"),
        ("ERROR tests/unit/test_a.py::test_b - fixture 'x' not found", "tests/unit/test_a.py"),
        ("FAILED src/otto/utils.py::otto.utils.wait_for", "src/otto/utils.py"),
        ("docs/contributing.md:12: WARNING: undefined label", None),
        ("Process completed with exit code 1.", None),
        ("", None),
    ],
)
def test_the_file_a_failure_line_names(line: str, path: "str | None"):
    assert failing_file(line) == path


# ─── render ─────────────────────────────────────────────────────────────────

_GREEN = {"sha": "a" * 40, "url": "https://example.test/runs/1", "created": "2026-11-11T02:00:00Z"}


def _render(**overrides):
    run = Run(
        title=overrides.pop("title", "CI failed on main"),
        heading=overrides.pop("heading", "A CI run on `main` failed."),
        run_url="https://example.test/runs/2",
        sha="b" * 40,
        rule_url="https://example.test/rule",
    )
    args = {
        "failed": [
            {
                "name": "tests_hostless-3.11",
                "url": "https://example.test/job/7",
                "failures": [_SNMP],
            }
        ],
        "attribute": True,
        "landing_files": ["src/otto/snmp.py"],
        "commits": ["bbbbbbb fix(cli): something"],
        "last_green": _GREEN,
        "notes": "",
    }
    args.update(overrides)
    return render(run, **args)


def test_every_failed_job_is_named_in_the_title_and_the_body():
    failed = [
        {"name": "tests_hostless-3.11", "url": "u1", "failures": [_SNMP]},
        {"name": "dashboard-e2e (chromium)", "url": "u2", "failures": [_UNDO]},
    ]
    out = _render(failed=failed)
    assert out["title"] == "CI failed on main: tests_hostless-3.11, dashboard-e2e (chromium)"
    for job in failed:
        assert job["name"] in out["body"]
        assert job["failures"][0] in out["body"]


def test_a_long_list_of_jobs_keeps_the_title_short_and_the_body_whole():
    failed = [{"name": f"job-{n}", "url": "u", "failures": []} for n in range(12)]
    out = _render(failed=failed)
    assert out["title"] == "CI failed on main: job-0, job-1, job-2, job-3 and 8 more"
    assert all(job["name"] in out["body"] for job in failed)


def test_a_title_github_would_refuse_is_cut_to_the_names_that_fit():
    """Nightly's unit-matrix legs carry their session list in their names: 4 pass 256."""
    long = [
        f"unit-matrix (3.1{n}, {'tests_hostless_unit ' * 4}tests_hostless_e2e)" for n in range(5)
    ]
    title = _render(failed=[{"name": name, "url": "u", "failures": []} for name in long])["title"]
    assert len(title) <= 200
    assert title == f"CI failed on main: {long[0]} and 4 more"
    huge = [{"name": "x" * 300, "url": "u", "failures": []}] * 2
    assert _render(failed=huge)["title"] == "CI failed on main: 2 jobs"
    assert _render(failed=huge[:1])["title"] == "CI failed on main: 1 job"


def test_a_failing_file_the_landing_changed_points_at_the_revert_rule():
    out = _render(landing_files=["tests/unit/test_snmp_leaf.py", "src/otto/snmp.py"])
    assert "`tests/unit/test_snmp_leaf.py` is in this landing's diff" in out["body"]
    assert "(https://example.test/rule)" in out["body"]
    assert "revert" in out["body"]


def test_a_failing_file_the_landing_did_not_change_says_so_and_never_says_revert_it():
    out = _render(landing_files=["src/otto/cli/run.py"])
    assert "`tests/unit/test_snmp_leaf.py` is not in this landing's diff" in out["body"]
    assert "fails only sometimes" in out["body"]


def test_one_failing_file_in_the_diff_decides_whatever_else_failed_beside_it():
    """An older test that also failed in the job must not hide the landing's own failure."""
    old = "FAILED tests/unit/a/test_old.py::test_x - flaky"
    new = "FAILED tests/unit/b/test_new.py::test_y - broken"
    failed = [{"name": "tests_hostless-3.11", "url": "u", "failures": [old, new]}]
    out = _render(failed=failed, landing_files=["tests/unit/b/test_new.py"])
    assert "`tests/unit/b/test_new.py` is in this landing's diff" in out["body"]
    assert "not in this landing's diff" not in out["body"]
    assert old in out["body"]
    assert new in out["body"]


def test_several_failing_files_outside_the_diff_are_all_named():
    lines = [
        "FAILED tests/unit/a/test_old.py::test_x - flaky",
        "FAILED tests/unit/a/test_old.py::test_z - flaky",
        "FAILED tests/unit/b/test_other.py::test_y - flaky",
    ]
    out = _render(failed=[{"name": "j", "url": "u", "failures": lines}], landing_files=[])
    assert (
        "`tests/unit/a/test_old.py`, `tests/unit/b/test_other.py` are not in this landing's diff"
        in out["body"]
    )


def test_a_job_with_many_failures_quotes_the_first_few_and_counts_the_rest():
    lines = [f"FAILED tests/unit/test_a.py::test_{n} - x" for n in range(30)]
    body = _render(failed=[{"name": "j", "url": "u", "failures": lines}])["body"]
    assert lines[0] in body
    assert lines[29] not in body
    assert "and 25 more failing lines" in body


def test_a_failure_that_names_no_test_is_not_attributed():
    """A lint or docs failure must never read as "not in the diff"."""
    failed = [{"name": "docs", "url": "u", "failures": ["docs/x.md:1: WARNING: undefined label"]}]
    out = _render(failed=failed)
    assert "names no test file" in out["body"]
    assert "not in this landing's diff" not in out["body"]


def test_an_unreadable_log_is_said_to_be_unreadable():
    out = _render(failed=[{"name": "docs", "url": "u", "failures": []}])
    assert "No failing line could be read" in out["body"]
    assert "not in this landing's diff" not in out["body"]


def test_an_unreadable_landing_range_is_not_attributed():
    out = _render(landing_files=None)
    assert "landing's diff could not be read" in out["body"]
    assert "not in this landing's diff" not in out["body"]


def test_a_nightly_lists_the_commits_since_its_last_green_and_suggests_no_revert():
    """Several landings may sit between two nightlies."""
    out = _render(
        title="Nightly run failed",
        heading="The nightly run failed.",
        attribute=False,
        landing_files=None,
        commits=["bbbbbbb one", "ccccccc two"],
    )
    assert out["title"] == "Nightly run failed: tests_hostless-3.11"
    assert "bbbbbbb one" in out["body"]
    assert "ccccccc two" in out["body"]
    assert "landing's diff" not in out["body"]
    assert "revert" not in out["body"]


def test_the_last_green_run_is_named_or_said_to_be_missing():
    assert _GREEN["url"] in _render()["body"]
    assert ("a" * 12) in _render()["body"]
    assert "no earlier green run was found" in _render(last_green=None)["body"]


def test_the_workflows_own_notes_follow_the_report():
    out = _render(notes="**busybox-upstream-drift is not a test failure.**")
    assert out["body"].rstrip().endswith("**busybox-upstream-drift is not a test failure.**")


def test_a_failure_line_cannot_break_out_of_its_code_block_or_mention_anyone():
    line = "FAILED tests/unit/test_a.py::test_b - ``` @someone " + "x" * 2000
    body = _render(failed=[{"name": "j", "url": "u", "failures": [line]}])["body"]
    assert body.count("```") % 2 == 0
    block = body.split("```text\n", 1)[1].split("\n```", 1)[0]
    assert "@someone" in block
    assert len(block) < 600


# ─── collect ────────────────────────────────────────────────────────────────


class _Gh:
    """A stand-in for the ``gh api`` runner: answers by endpoint, or fails."""

    def __init__(self, answers: "dict[str, str]") -> None:
        self.answers = answers
        self.asked: "list[str]" = []
        self.paginated: "dict[str, bool]" = {}

    def __call__(self, endpoint: str, jq: "str | None" = None, *, paginate: bool = False) -> str:
        self.asked.append(endpoint)
        self.paginated[endpoint] = paginate
        # The longest matching fragment answers: `/runs/42` must not answer `/runs/42/jobs`.
        matching = [fragment for fragment in self.answers if fragment in endpoint]
        if not matching:
            raise OSError(f"no answer for {endpoint}")
        return self.answers[max(matching, key=len)]


def green_of(gh: _Gh) -> str:
    [endpoint] = [e for e in gh.asked if "/workflows/" in e]
    return endpoint


def _jobs(*jobs: dict) -> str:
    """What ``gh api --paginate --jq '.jobs[]'`` prints: one job object per line."""
    return "".join(json.dumps(job) + "\n" for job in jobs)


def _job(job_id: int, name: str, conclusion: "str | None") -> dict:
    return {
        "id": job_id,
        "name": name,
        "conclusion": conclusion,
        "html_url": f"https://example.test/job/{job_id}",
    }


def test_collect_reports_the_jobs_that_did_not_pass_each_with_its_first_failing_line():
    gh = _Gh(
        {
            "/actions/runs/42/jobs": _jobs(
                _job(1, "lint-python", "success"),
                _job(2, "tests_hostless-3.11", "failure"),
                _job(3, "docs", "cancelled"),
                _job(4, "Open issue on failure", None),
                _job(5, "wheel-smoke", "skipped"),
            ),
            "/actions/jobs/2/logs": _log(_SNMP),
            "/actions/jobs/3/logs": _log("##[error]The operation was canceled."),
            "/actions/workflows/ci.yml/runs": json.dumps(
                {"head_sha": "a" * 40, "html_url": "green", "created_at": "t"}
            ),
        }
    )
    got = collect(gh, repo="owner/repo", run_id="42", workflow="ci.yml")
    assert got["failed"] == [
        {
            "name": "tests_hostless-3.11",
            "url": "https://example.test/job/2",
            "failures": [_SNMP],
        },
        {
            "name": "docs (cancelled)",
            "url": "https://example.test/job/3",
            "failures": ["The operation was canceled."],
        },
    ]
    assert got["last_green"] == {"sha": "a" * 40, "url": "green", "created": "t"}
    # No creation time could be read for run 42, so the newest green run stands in.
    assert "created=" not in green_of(gh)
    # A landing's last green is a push's: a fork's pull request from its own `main` is not.
    assert "event=push" not in green_of(gh)
    pushed = _Gh(dict(gh.answers))
    collect(pushed, repo="owner/repo", run_id="42", workflow="ci.yml", event="push")
    assert "&event=push" in green_of(pushed)
    # The job list must be whole; the newest green run must not walk the run history.
    [jobs] = [e for e in gh.asked if "/runs/42/jobs" in e]
    [green] = [e for e in gh.asked if "/workflows/ci.yml/runs" in e]
    assert gh.paginated[jobs] is True
    assert gh.paginated[green] is False
    assert "per_page=1" in green


def test_the_last_green_run_is_one_from_before_the_failed_run():
    """A failed run re-run after a later green one must not name that later run."""
    gh = _Gh(
        {
            "/actions/runs/42/jobs": "",
            "/actions/runs/42": "2026-11-10T02:00:00Z\n",
            "/actions/workflows/nightly.yml/runs": json.dumps(
                {"head_sha": "a" * 40, "html_url": "green", "created_at": "t"}
            ),
        }
    )
    collect(gh, repo="owner/repo", run_id="42", workflow="nightly.yml")
    assert "&created=%3C2026-11-10T02:00:00Z" in green_of(gh)


def test_collect_still_reports_when_github_answers_nothing():
    """The issue is filed whatever the API does: an unfiled red is the worst outcome."""
    got = collect(_Gh({}), repo="owner/repo", run_id="42", workflow="ci.yml")
    assert got == {"failed": [], "last_green": None}


def test_collect_keeps_a_job_whose_log_cannot_be_read():
    gh = _Gh({"/actions/runs/42/jobs": _jobs(_job(2, "tests_hostless-3.11", "failure"))})
    got = collect(gh, repo="owner/repo", run_id="42", workflow="nightly.yml")
    assert got["failed"] == [
        {"name": "tests_hostless-3.11", "url": "https://example.test/job/2", "failures": []}
    ]
    assert got["last_green"] is None


# ─── the command ────────────────────────────────────────────────────────────


def _landing(tmp_path: Path) -> "tuple[TmpGitRepo, str, str]":
    repo = TmpGitRepo(tmp_path / "repo")
    repo.write("tests/unit/test_snmp_leaf.py", "def test_a(): pass\n")
    repo.write("src/otto/cli.py", "x = 1\n")
    before = repo.commit("base")
    repo.write("src/otto/cli.py", "x = 2\n")
    sha = repo.commit("fix(cli): the landing")
    return repo, before, sha


def _main(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, gh: _Gh, *args: str) -> "dict[str, str]":
    monkeypatch.setattr("scripts.ci_failure_report.gh_api", gh)
    title, body = tmp_path / "title.txt", tmp_path / "body.md"
    code = main(
        [
            "--title",
            "CI failed on main",
            "--heading",
            "A CI run on `main` failed.",
            "--repo",
            "owner/repo",
            "--run-id",
            "42",
            "--run-url",
            "https://example.test/runs/42",
            "--title-out",
            str(title),
            "--body-out",
            str(body),
            *args,
        ]
    )
    assert code == 0
    return {"title": title.read_text(), "body": body.read_text()}


def test_the_command_attributes_a_landing_over_the_pushed_range(tmp_path, monkeypatch):
    repo, before, sha = _landing(tmp_path)
    for name, value in git_env(repo.root).items():
        monkeypatch.setenv(name, value)
    gh = _Gh(
        {
            "/actions/runs/42/jobs": _jobs(_job(2, "tests_hostless-3.11", "failure")),
            "/actions/jobs/2/logs": _log(_SNMP),
        }
    )
    out = _main(
        tmp_path,
        monkeypatch,
        gh,
        "--workflow",
        "ci.yml",
        "--sha",
        sha,
        "--before",
        before,
        "--checkout",
        str(repo.root),
    )
    assert out["title"] == "CI failed on main: tests_hostless-3.11\n"
    assert "`tests/unit/test_snmp_leaf.py` is not in this landing's diff" in out["body"]
    assert "fix(cli): the landing" in out["body"]
    assert (
        "https://github.com/owner/repo/blob/main/docs/contributing.md#when-main-goes-red"
        in out["body"]
    )


def test_the_command_files_a_report_even_when_nothing_can_be_read(tmp_path, monkeypatch):
    """No API, no such commit: the issue still says the run failed and links it."""
    repo, _, _ = _landing(tmp_path)
    for name, value in git_env(repo.root).items():
        monkeypatch.setenv(name, value)
    out = _main(
        tmp_path,
        monkeypatch,
        _Gh({}),
        "--workflow",
        "ci.yml",
        "--sha",
        "f" * 40,
        "--before",
        "e" * 40,
        "--checkout",
        str(repo.root),
    )
    assert out["title"] == "CI failed on main\n"
    assert "https://example.test/runs/42" in out["body"]
    assert "could not be read" in out["body"]


def test_the_command_appends_the_workflows_notes(tmp_path, monkeypatch):
    repo, _, sha = _landing(tmp_path)
    for name, value in git_env(repo.root).items():
        monkeypatch.setenv(name, value)
    notes = tmp_path / "notes.md"
    notes.write_text("**python-canary may not be a failure on a supported Python.**\n")
    out = _main(
        tmp_path,
        monkeypatch,
        _Gh({}),
        "--workflow",
        "nightly.yml",
        "--sha",
        sha,
        "--checkout",
        str(repo.root),
        "--notes",
        str(notes),
    )
    assert "python-canary may not be a failure" in out["body"]
    assert "landing's diff" not in out["body"]
