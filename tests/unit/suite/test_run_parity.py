"""One input, three entry points: the orchestrator walks, ``run_tests`` and ``selected_tests``.

Which projects take part is one decision (:func:`otto.config.scope.participating`);
a run, a listing and a lifecycle walk must keep the same projects for the same
context.
"""

import pytest

from otto.project.orchestrator import _applicable
from otto.suite.run import run_tests, selected_tests
from tests._fixtures.scoping import verdict
from tests.unit.suite.test_run_tests import NO_COV, _scoped


def _cases():
    return {
        "out_of_scope": {"scopes": {"project_b": verdict("project_b", excluded=True)}},
        "host_starved": {"scopes": {"project_b": verdict("project_b", universe=())}},
        "exclude": {"exclude": ("project_b",)},
        "include": {
            "scopes": {"project_b": verdict("project_b", excluded=True)},
            "include": ("project_b",),
        },
        "undeclared": {"scopes": {"project_b": verdict("project_b", declared=False)}},
    }


@pytest.mark.parametrize("case", list(_cases()))
def test_one_input_three_entry_points(case, two_projects, tmp_path):
    two_projects(
        a={"tests/test_a.py": "def test_same():\n    pass\n"},
        b={"tests/test_b.py": "def test_same():\n    pass\n"},
    )
    with _scoped(**_cases()[case]) as ctx:
        walked = [
            p.name for p in _applicable(ctx, ctx.projects, "test", require_dependencies=False)
        ]
        listed = list(selected_tests([]).tests)
        ran = [
            p.stem.removeprefix("junit_")
            for p in run_tests(
                names=["test_same"], run_options=NO_COV, output_dir=tmp_path / "o"
            ).junit_paths
        ]
    assert walked == listed
    assert ran == walked
