"""CI's admission job, the release guard and the nightly dashboard soak.

``gate-admission`` is CI's first job: on a pull request it waits for the
``local-gate`` commit status the local gate posted for the head, and every
other job ``needs:`` it, so no validation runs for a head nobody gated.
"""

import itertools
import json

import pytest
import yaml

from scripts import local_gate_status
from tests._fixtures.paths import CHECKOUT_ROOT

pytestmark = pytest.mark.interpreter_agnostic

_WORKFLOWS = CHECKOUT_ROOT / ".github" / "workflows"
CI = yaml.safe_load((_WORKFLOWS / "ci.yml").read_text())
RELEASE = yaml.safe_load((_WORKFLOWS / "release.yml").read_text())
NIGHTLY = yaml.safe_load((_WORKFLOWS / "nightly.yml").read_text())


def _product(axes: dict) -> list:
    """Every combination of the axes' values, none when there are no axes."""
    return list(itertools.product(*axes.values())) if axes else []


def _expand(job_id: str, job: dict) -> list[str]:
    """The check-run names ``job`` produces, as GitHub renders them.

    A ``${{ matrix.<key> }}`` in the name is replaced by each value of that key (``include``
    entries contribute their keys too). A matrix job whose name uses no such expression, or
    that has no ``name``, is rendered ``name (v1, v2)`` with the values in the matrix's key order.
    """
    base = job.get("name", job_id)
    matrix = job.get("strategy", {}).get("matrix", {})
    axes = {k: v for k, v in matrix.items() if isinstance(v, list) and k != "include"}
    include = matrix.get("include", [])
    if not axes and not include:
        return [base]
    tokens = {"${{ matrix." + k + " }}" for k in [*axes, *(k for i in include for k in i)]}
    if not any(t in base for t in tokens):
        values = [[str(x) for x in combo] for combo in _product(axes)]
        values += [[str(x) for x in entry.values()] for entry in include]
        return [f"{base} ({', '.join(v)})" for v in values]
    combos = [dict(zip(axes, combo, strict=True)) for combo in _product(axes)]
    combos += [dict(entry) for entry in include]
    names = []
    for combo in combos:
        name = base
        for key, value in combo.items():
            name = name.replace("${{ matrix." + key + " }}", str(value))
        names.append(name)
    return names


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


def test_report_failure_keeps_its_push_only_condition():
    job = CI["jobs"]["report-failure"]
    assert job["if"] == "failure() && github.event_name == 'push'"
    assert "gate-admission" in job["needs"]


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
