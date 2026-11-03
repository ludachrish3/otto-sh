"""CI's admission job, the release guard and the nightly dashboard soak.

``gate-admission`` is CI's first job: on a pull request it waits for the
``local-gate`` commit status the local gate posted for the head, and every
other job ``needs:`` it, so no validation runs for a head nobody gated.
"""

import json

import pytest
import yaml

from scripts import local_gate_status
from tests._fixtures.gha_matrix import expand_names as _expand
from tests._fixtures.gha_matrix import matrix_rows, render
from tests._fixtures.paths import CHECKOUT_ROOT
from tests.unit.test_coverage_floors import makefile_variable
from tests.unit.test_lane_invariants import _PRIMARY_CI_SESSIONS

pytestmark = pytest.mark.interpreter_agnostic

_WORKFLOWS = CHECKOUT_ROOT / ".github" / "workflows"
CI = yaml.safe_load((_WORKFLOWS / "ci.yml").read_text())
RELEASE = yaml.safe_load((_WORKFLOWS / "release.yml").read_text())
NIGHTLY = yaml.safe_load((_WORKFLOWS / "nightly.yml").read_text())
_MAKEFILE = (CHECKOUT_ROOT / "Makefile").read_text()
ACTION = yaml.safe_load((CHECKOUT_ROOT / ".github/actions/hostless-setup/action.yml").read_text())


def _required() -> list[str]:
    return json.loads((CHECKOUT_ROOT / ".github/required_contexts.json").read_text())


def test_gate_admission_runs_on_every_event_and_polls_only_for_a_pull_request():
    job = CI["jobs"]["gate-admission"]
    assert "if" not in job
    assert job["timeout-minutes"] == 8
    assert job["permissions"] == {"statuses": "read", "contents": "read"}
    assert job["env"] == {"GH_TOKEN": "${{ github.token }}"}
    polling = [s for s in job["steps"] if "local_gate_status.py await" in s.get("run", "")]
    assert len(polling) == 1
    assert polling[0]["if"] == "github.event_name == 'pull_request'"
    assert '--sha "${{ github.event.pull_request.head.sha }}"' in polling[0]["run"]
    assert '--repo-slug "$GITHUB_REPOSITORY"' in polling[0]["run"]
    checkout = next(s for s in job["steps"] if s.get("uses", "").startswith("actions/checkout"))
    assert checkout["with"] == {
        "ref": "${{ github.event.pull_request.head.sha }}",
        "fetch-depth": 0,
    }
    noop = [s for s in job["steps"] if s.get("if") == "github.event_name != 'pull_request'"]
    assert len(noop) == 1


def test_every_validation_job_needs_the_admission_job():
    for job_id, job in CI["jobs"].items():
        if job_id == "gate-admission":
            continue
        assert "gate-admission" in job["needs"], job_id


MAIN_ONLY_REPORT = "failure() && github.ref == 'refs/heads/main'"


@pytest.mark.parametrize("workflow", [CI, NIGHTLY], ids=["ci", "nightly"])
def test_an_issue_is_filed_only_for_a_failed_run_on_main(workflow):
    """A pull request's checks run on ``refs/pull/<n>/merge`` and a hand-dispatched run on
    its own branch; only a red ``main`` is worth an issue, in every workflow that files one.
    """
    assert workflow["jobs"]["report-failure"]["if"] == MAIN_ONLY_REPORT


def test_report_failure_waits_for_the_admission_job():
    assert "gate-admission" in CI["jobs"]["report-failure"]["needs"]


def test_every_required_context_but_the_status_is_a_declared_job():
    declared = {name for job_id, job in CI["jobs"].items() for name in _expand(job_id, job)}
    required = _required()
    assert set(required) - {"local-gate"} <= declared, set(required) - {"local-gate"} - declared
    assert "busybox-artifacts (x86_64)" not in required
    assert "report-failure" not in required


def test_expand_follows_the_matrix_and_never_falls_back_to_the_id_of_a_named_job():
    matrix = {"python": ["3.10", "3.11"]}
    assert _expand(
        "tests", {"name": "tests_hostless-${{ matrix.python }}", "strategy": {"matrix": matrix}}
    ) == [
        "tests_hostless-3.10",
        "tests_hostless-3.11",
    ]
    assert _expand("x", {"name": "fixed"}) == ["fixed"]
    assert _expand("x", {}) == ["x"]
    assert _expand("tests", CI["jobs"]["tests"]) == [
        f"tests_hostless-{v}" for v in CI["jobs"]["tests"]["strategy"]["matrix"]["python"]
    ]
    required = _required()
    # a required context spelt as an id would never turn green
    assert "tests" not in required
    assert "dashboard" not in required


def test_lint_python_checks_the_lock_before_any_sync_and_collects_before_the_api_steps():
    runs = [s.get("run", "").strip() for s in CI["jobs"]["lint-python"]["steps"]]
    lock = runs.index("make lock-check")
    collect = runs.index("make collect-check")
    api = runs.index("make check-api-teaching")
    assert runs[lock - 1] == "uv python install 3.10"
    assert not any(r.startswith("uv run") or "uv sync" in r for r in runs[:lock])
    assert lock < runs.index("uv run nox -s lint") < collect < api
    assert not any("make web" in r for r in runs)


def test_the_status_and_the_job_have_different_names():
    job_name = CI["jobs"][local_gate_status.ADMISSION_JOB]["name"]
    assert job_name == local_gate_status.ADMISSION_JOB
    assert job_name != local_gate_status.CONTEXT


def test_the_release_guard_checks_out_verifies_ancestry_and_asks_the_verdict_reader():
    guard = RELEASE["jobs"]["guard"]
    assert guard["permissions"] == {"actions": "read", "contents": "read"}
    assert guard["env"] == {"GH_TOKEN": "${{ github.token }}"}
    checkout = next(s for s in guard["steps"] if s.get("uses", "").startswith("actions/checkout"))
    assert checkout["with"] == {"fetch-depth": 0}
    verify = [s for s in guard["steps"] if "ci-verdict" in s.get("run", "")]
    assert len(verify) == 1
    assert "merge-base --is-ancestor" in verify[0]["run"]
    assert "python3 scripts/local_gate_status.py ci-verdict" in verify[0]["run"]
    assert not any("|| true" in s.get("run", "") for s in guard["steps"])


def test_nightly_has_a_dashboard_soak_job_the_failure_report_watches():
    job = NIGHTLY["jobs"]["dashboard-soak"]
    assert any(s.get("run") == "make dashboard-soak" for s in job["steps"])
    assert "dashboard-soak" in NIGHTLY["jobs"]["report-failure"]["needs"]


def test_include_entries_merge_into_matching_rows_and_append_only_when_none_match():
    job = {
        "strategy": {
            "matrix": {
                "python": ["3.10", "3.11"],
                "include": [{"python": "3.10", "sessions": "a b"}, {"os": "mac", "python": "3.12"}],
            }
        }
    }
    assert matrix_rows(job) == [
        {"python": "3.10", "sessions": "a b"},
        {"python": "3.11"},
        {"os": "mac", "python": "3.12"},
    ]
    everywhere = {"strategy": {"matrix": {"py": ["3.10", "3.11"], "include": [{"os": "x"}]}}}
    assert matrix_rows(everywhere) == [{"py": "3.10", "os": "x"}, {"py": "3.11", "os": "x"}]


def test_nightly_unit_matrix_renders_five_rows_each_naming_its_sessions():
    job = NIGHTLY["jobs"]["unit-matrix"]
    rows = matrix_rows(job)
    assert len(rows) == 5
    runs = [str(s["run"]) for s in job["steps"] if "run" in s]
    for row in rows:
        assert "sessions" in row, row
        for run in runs:
            assert "${{ matrix.sessions }}" not in render(run, row)


def test_expand_renders_include_entries_and_unnamed_matrix_values_as_github_does():
    included = {
        "name": "run ${{ matrix.browser }}",
        "strategy": {"matrix": {"include": [{"browser": "chromium"}, {"browser": "firefox"}]}},
    }
    assert _expand("j", included) == ["run chromium", "run firefox"]
    plain = {"name": "soak", "strategy": {"matrix": {"py": ["3.10", "3.12"], "os": ["a"]}}}
    assert _expand("j", plain) == ["soak (3.10, a)", "soak (3.12, a)"]
    assert _expand("job_id", {"strategy": {"matrix": {"py": ["3.10"]}}}) == ["job_id (3.10)"]
    assert _expand("j", {"name": "solo"}) == ["solo"]


LEG_JOBS = ["tests-primary", "tests-canary", "tests"]
CERTIFICATES = {
    "hostless-primary": (
        "tests_hostless-3.10",
        ["gate-admission", "tests-primary", "coverage-hostless"],
    ),
    "hostless-canary": ("tests_hostless-3.14", ["gate-admission", "tests-canary"]),
}


@pytest.mark.parametrize("job_id", sorted(CERTIFICATES))
def test_the_bookend_certificates_keep_their_context_names_and_accept_only_success(job_id):
    """A skipped required check counts as passing, so the certificate must run
    (``always()``) and turn red on any need that is not ``success``."""
    name, needs = CERTIFICATES[job_id]
    job = CI["jobs"][job_id]
    assert job["name"] == name
    assert job["needs"] == needs
    assert job["if"] == "always()"
    (step,) = job["steps"]
    clauses = [c.strip() for c in step["run"].split("&&")]
    assert clauses == [f'test "${{{{ needs.{n}.result }}}}" = success' for n in needs]
    assert name in _required()


def _fold_sessions() -> "list[str]":
    """The primary's hostless sessions, from their one home: the Makefile's fold list."""
    return makefile_variable(_MAKEFILE, "HOSTLESS_FOLD_SESSIONS").split()


def test_every_list_of_the_primarys_sessions_is_the_fold_list():
    """CI's primary matrix, nightly's 3.10 row and the partition pin's list are the sessions
    the fold combines: a session uploaded but not folded would leave the floor judged on part
    of the primary's selection."""
    fold = set(_fold_sessions())
    assert fold, "the fold names no session"
    assert set(CI["jobs"]["tests-primary"]["strategy"]["matrix"]["session"]) == fold
    (row,) = [
        r for r in matrix_rows(NIGHTLY["jobs"]["unit-matrix"]) if r["python-version"] == "3.10"
    ]
    assert {s.removesuffix("-3.10") for s in row["sessions"].split()} == fold
    assert set(_PRIMARY_CI_SESSIONS) == fold


def test_the_primary_legs_check_the_data_file_then_upload_it_and_the_fold_downloads_it():
    primary = CI["jobs"]["tests-primary"]
    sessions = primary["strategy"]["matrix"]["session"]
    assert sessions == _fold_sessions()
    assert _expand("tests-primary", primary) == [f"{s}-3.10" for s in sessions]
    steps = primary["steps"]
    runs = [s.get("run", "") for s in steps]
    assert "uv run nox -s ${{ matrix.session }}-3.10" in runs
    check = runs.index("test -f reports/coverage/.coverage")
    assert steps[check]["name"] == "the session's combined data exists"
    upload = next(i for i, s in enumerate(steps) if s.get("uses") == "actions/upload-artifact@v7")
    assert check < upload
    assert steps[upload]["with"] == {
        "name": "coverage-${{ matrix.session }}-3.10",
        "path": "reports/coverage/.coverage*",
        "include-hidden-files": True,
        "if-no-files-found": "error",
    }
    canary = CI["jobs"]["tests-canary"]
    assert _expand("tests-canary", canary) == [
        "tests_hostless_unit-3.14",
        "tests_hostless_e2e-3.14",
    ]
    assert not any("upload-artifact" in s.get("uses", "") for s in canary["steps"])
    fold = CI["jobs"]["coverage-hostless"]
    assert fold["needs"] == ["gate-admission", "tests-primary"]
    download = next(s for s in fold["steps"] if s.get("uses") == "actions/download-artifact@v8")
    assert download["with"] == {"pattern": "coverage-*", "path": "reports/coverage/hostless"}
    fold_runs = [s.get("run", "") for s in fold["steps"]]
    assert "uv python install 3.10" in fold_runs
    assert fold_runs[-1] == "make coverage-hostless-fold"


def test_the_leg_jobs_share_one_setup_action():
    for job_id in LEG_JOBS:
        steps = CI["jobs"][job_id]["steps"]
        assert steps[0]["uses"].startswith("actions/checkout"), job_id
        assert steps[1]["uses"] == "./.github/actions/hostless-setup", job_id
        assert "python" in steps[1]["with"], job_id
    assert ACTION["runs"]["using"] == "composite"
    assert ACTION["inputs"]["python"]["required"] is True
    run_steps = [s for s in ACTION["runs"]["steps"] if "run" in s]
    assert run_steps
    assert all(s.get("shell") == "bash" for s in run_steps)
    assert "uv python install ${{ inputs.python }}" in [s["run"] for s in run_steps]


def test_report_failure_needs_every_job():
    others = set(CI["jobs"]) - {"report-failure"}
    assert set(CI["jobs"]["report-failure"]["needs"]) == others


def test_expand_renders_a_session_matrix():
    job = {"name": "${{ matrix.session }}-3.14", "strategy": {"matrix": {"session": ["a", "b"]}}}
    assert _expand("j", job) == ["a-3.14", "b-3.14"]
