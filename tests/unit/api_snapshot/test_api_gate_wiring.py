"""The public-surface gates run where the specs put them: CI and the invariance lane.

Spec ``docs/superpowers/specs/2026-10-04-public-api-manifest-design.md`` §6 (the
docs validator gates) and dump spec §6 (the version-invariance lane). The
pre-push lane list is pinned in ``tests/unit/scripts/test_gate_fresh.py``.
"""

import ast
import re

import pytest
import yaml

from scripts import api_regen
from tests._fixtures.paths import PROJECT_ROOT

pytestmark = pytest.mark.interpreter_agnostic

CI = PROJECT_ROOT / ".github" / "workflows" / "ci.yml"
NOXFILE = PROJECT_ROOT / "noxfile.py"


def _jobs() -> "dict":
    return yaml.safe_load(CI.read_text(encoding="utf-8"))["jobs"]


def _runs(job: "dict") -> "list[str]":
    return [step["run"] for step in job.get("steps", []) if "run" in step]


def _step(job: "dict", run: "re.Pattern[str]") -> "dict":
    """The one step of ``job`` whose ``run`` matches ``run``."""
    (step,) = [s for s in job.get("steps", []) if "run" in s and run.search(s["run"])]
    return step


def _blocking(step: "dict") -> bool:
    """No ``continue-on-error`` and no ``if:``: a step that always runs and fails its job."""
    return "continue-on-error" not in step and "if" not in step


def _reported(jobs: "dict") -> "set[str]":
    needs = jobs["report-failure"]["needs"]
    return {needs} if isinstance(needs, str) else set(needs)


def test_ci_runs_the_teaching_gate_on_the_tree_the_pr_would_land():
    """``make check-api-teaching`` runs in a blocking, reported job, before the marking step.

    The marking step may detach to a pull request's head; the teaching gate
    judges the merged tree, as the lint before it does.
    """
    jobs = _jobs()
    gate = re.compile(r"\bmake\b[^\n]*\bcheck-api-teaching\b(?![-\w])")
    running = [name for name, job in jobs.items() if any(gate.search(r) for r in _runs(job))]
    assert running == ["lint-python"], running
    assert "continue-on-error" not in jobs["lint-python"]
    assert "if" not in jobs["lint-python"]
    assert _blocking(_step(jobs["lint-python"], gate))
    assert "lint-python" in _reported(jobs)
    runs = _runs(jobs["lint-python"])
    teaching = next(i for i, run in enumerate(runs) if gate.search(run))
    marking = next(i for i, run in enumerate(runs) if "check_breaking_marks.py" in run)
    assert teaching < marking


def _noxfile() -> "tuple[str, ast.Module]":
    source = NOXFILE.read_text(encoding="utf-8")
    return source, ast.parse(source)


def _python_versions() -> "list[str]":
    _, tree = _noxfile()
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "PYTHON_VERSIONS" for t in node.targets
        ):
            return ast.literal_eval(node.value)
    raise AssertionError("noxfile.py binds no PYTHON_VERSIONS")


def test_the_invariance_lane_regenerates_the_dump_on_every_matrix_python():
    jobs = _jobs()
    job = jobs["api-dump-invariance"]
    assert job["strategy"]["matrix"]["python"] == _python_versions()
    assert "uv run nox -s api_dump_invariance-${{ matrix.python }}" in _runs(job)
    assert "continue-on-error" not in job
    assert "if" not in job
    nox = re.compile(r"\bnox -s api_dump_invariance-")
    assert _blocking(_step(job, nox))
    assert "api-dump-invariance" in _reported(jobs)


def _invariance_session() -> "tuple[str, ast.FunctionDef]":
    source, tree = _noxfile()
    (session,) = [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "api_dump_invariance"
    ]
    return source, session


def _session_keywords(session: "ast.FunctionDef") -> "dict[str | None, ast.expr]":
    (decorator,) = session.decorator_list
    assert isinstance(decorator, ast.Call)
    return {k.arg: k.value for k in decorator.keywords}


def test_the_invariance_lane_installs_otto_s_runtime_dependencies_alone():
    """The lane installs no otto and no dependency group or extra (dump spec §5.2).

    ``uv_no_install_project`` keeps otto out (the dump child puts ``src``
    first on its own path); any other environment keyword (``uv_groups``,
    ``uv_extras``, ``uv_all_extras`` ...) would add packages the regeneration
    in ``check-breaking`` never sees.
    """
    _, session = _invariance_session()
    keywords = _session_keywords(session)
    assert ast.literal_eval(keywords["uv_no_install_project"]) is True
    assert set(keywords) == {"python", "uv_no_install_project"}, sorted(map(str, keywords))


def _session_runs(session: "ast.FunctionDef") -> "list[ast.Call]":
    return [
        node
        for node in ast.walk(session)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "run"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "session"
    ]


def test_each_invariance_leg_checks_under_its_own_hash_seed():
    """Dump spec §6: a different ``PYTHONHASHSEED`` per interpreter, never the gate's own."""
    source, session = _invariance_session()
    python = _session_keywords(session)["python"]
    assert isinstance(python, ast.Name)
    assert python.id == "PYTHON_VERSIONS"
    text = ast.get_source_segment(source, session) or ""
    assert 'seed = str(session.python).replace(".", "")' in text
    (run,) = _session_runs(session)
    assert run.keywords == []
    *literal, last = run.args
    assert [a.value if isinstance(a, ast.Constant) else ast.dump(a) for a in literal] == [
        "python",
        "scripts/api_snapshot.py",
        "--manifest",
        "api/public.toml",
        "--check",
        "--hash-seed",
    ]
    assert isinstance(last, ast.Name), ast.dump(last)
    assert last.id == "seed"
    seeds = [version.replace(".", "") for version in _python_versions()]
    assert len(set(seeds)) == len(seeds)
    assert api_regen.GATE_HASH_SEED not in seeds
