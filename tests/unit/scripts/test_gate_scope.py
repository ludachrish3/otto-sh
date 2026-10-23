"""Pins for scripts/gate_scope.py — the diff classifier behind `make gate-scope` (#566)."""

import ast
import json
import os
import re
import subprocess
import sys
import types
from pathlib import Path

import pytest
from _pytest.mark.expression import Expression

from scripts import gate_scope as gs
from scripts import gate_tip as gt
from scripts.gate_fresh import GATED_TARGETS
from tests._fixtures.gitrepo import git_env
from tests._fixtures.paths import CHECKOUT_ROOT
from tests.unit import test_lane_invariants as _lane_invariants
from tests.unit.test_lane_invariants import (
    _PATHS_TOKEN,
    _Leg,
    _legs_including_markerless,
    _marker_matcher,
    _within,
    make_legs,
    make_reachable,
    makefile_live_recipe,
)
from tests.unit.test_tier_marker_invariants import (
    _makefile_marker_variables,
    _makefile_pytest_invocations,
    _python_pytest_invocations,
    _string_constants,
)

pytestmark = pytest.mark.interpreter_agnostic

# The memoised real-collection fixture: pytest registers a fixture per module by attribute
# name, so binding it here gives this module its own cache (and its own single collection).
collect_marker_sets = _lane_invariants.collect_marker_sets


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("docs/superpowers/specs/2026-10-14-x.md", gs.NONE),
        ("todo/anything.md", gs.NONE),
        ("AGENTS.md", gs.NONE),
        ("README.md", gs.DOCS),
        ("docs/contributing.md", gs.DOCS),
        ("docs/architecture/testing.md", gs.DOCS),
        ("docs/conf.py", None),
        ("docs/examples/getting-started/libs/gs_example/__init__.py", None),
        ("docs/examples/quickstart-shell/src/main.cpp", gs.DOCS),
        ("docs/examples/quickstart-shell/.otto/settings.toml", gs.DOCS),
        ("web/src/App.tsx", gs.WEB),
        ("web/src/__tests__/topology.test.ts", gs.WEB),
        ("web/src/api/types.gen.ts", None),
        ("web/package.json", None),
        ("web/vite.config.ts", None),
        ("tests/unit/cli/test_expose.py", gs.TESTS_LEAF),
        ("tests/unit/test_make_quick.py", gs.TESTS_LEAF),
        ("tests/e2e/cli/test_run.py", gs.TESTS_LEAF),
        ("tests/e2e/test_project_wide_conftest.py", gs.TESTS_LEAF),
        ("tests/unit/cli/conftest.py", gs.ESCALATE),
        ("tests/conftest.py", gs.ESCALATE),
        ("tests/integration/chaos/conftest.py", gs.ESCALATE),
        ("tests/integration/chaos/_driver.py", None),
        ("tests/_fixtures/gitrepo.py", gs.ESCALATE),
        ("tests/unit/host/survey/_fixtures/x.py", None),
        ("tests/e2e/_otto_subprocess.py", None),
        ("src/otto/cli/expose.py", gs.SRC_LEAF),
        ("src/otto/console.py", gs.SRC_LEAF),
        ("src/otto/kmodcov/kmodcov.c", gs.SRC_LEAF),
        ("src/otto/py.typed", gs.ESCALATE),
        ("src/otto/__init__.py", gs.ESCALATE),
        ("src/otto/cli/__init__.py", gs.ESCALATE),
        ("src/otto/host/survey/__init__.py", gs.ESCALATE),
        ("src/otto/registry.py", gs.ESCALATE),
        ("src/otto/_shim_complete.py", gs.ESCALATE),
        ("scripts/gate_fresh.py", gs.SCRIPTS),
        ("pyproject.toml", gs.ESCALATE),
        ("Makefile", gs.ESCALATE),
        ("noxfile.py", gs.ESCALATE),
        (".coveragerc", gs.ESCALATE),
        (".github/workflows/ci.yml", gs.ESCALATE),
        (".githooks/pre-push", gs.ESCALATE),
        ("api/public.toml", None),
        ("uv.lock", None),
    ],
)
def test_classify_path(path, expected):
    assert gs.classify_path(path) == expected


def test_a_single_star_crosses_directories_where_the_tables_rely_on_it():
    assert gs._match("tests/_fixtures/a/b.py", ("tests/_fixtures/*",))
    assert gs._match("src/otto/host/survey/__init__.py", ("src/otto/*/__init__.py",))
    assert gs._match("docs/superpowers/specs/a/b.md", gs.NONE_GLOBS)
    assert not gs._match("tests/e2e/test_project_wide_conftest.py", gs.TRIGGER_GLOBS)


def test_identities_are_class_qualified_and_classes_carry_identity():
    source = (
        "import pytest\n\n"
        "def test_a(): pass\n\n"
        "class TestX:\n"
        "    def test_b(self): pass\n"
        "    def helper(self): pass\n\n"
        "def helper(): pass\n"
    )
    assert gs.test_identities(source) == {"test_a", "TestX", "TestX.test_b"}


_MARKED = (
    "import pytest\n"
    "from pytest import mark\n"
    "pytestmark = [pytest.mark.interpreter_agnostic, pytest.mark.timeout(5)]\n\n"
    "@pytest.mark.timeout(600)\n"
    "@pytest.mark.parametrize('x', [1])\n"
    "@mark.slow\n"
    "def test_a(x): pass\n\n"
    "class TestX:\n"
    "    pytestmark: list = [pytest.mark.serial_timing]\n"
    "    @pytest.mark.skipif(False, reason='needs a browser and stability')\n"
    "    def test_b(self): pass\n"
)


def test_marker_names_read_marks_as_ast_not_substrings():
    names = gs.marker_names(_MARKED)
    assert names["pytestmark"] == {"interpreter_agnostic", "timeout"}
    assert names["test_a"] == {"timeout", "parametrize", "slow"}
    assert names["TestX"] == {"serial_timing"}
    assert names["TestX.test_b"] == {"skipif"}, "a reason string naming a marker is not a marker"
    assert gs.marker_names(None) == {}
    assert gs.marker_names("def test_(:\n") == {"<unparsable>": {"<unparsable>"}}


def test_marker_signature_keeps_the_arguments():
    signature = gs.marker_signature(_MARKED)
    assert signature["test_a"] == [
        "mark.slow",
        "pytest.mark.parametrize('x', [1])",
        "pytest.mark.timeout(600)",
    ]
    assert signature["TestX.test_b"] == [
        "pytest.mark.skipif(False, reason='needs a browser and stability')"
    ]
    assert signature["pytestmark"] == ["pytest.mark.interpreter_agnostic", "pytest.mark.timeout(5)"]
    assert gs.marker_signature(None) == {}


def test_structural_when_a_test_goes_or_a_mark_changes():
    base = "import pytest\n\ndef test_a(): pass\n\ndef test_b(): pass\n"
    assert not gs.is_structural(base, base + "\ndef test_c(): pass\n")
    assert gs.is_structural(base, "import pytest\n\ndef test_a(): pass\n")  # removal
    assert gs.is_structural(base, base.replace("test_b", "test_b2"))  # rename
    assert gs.is_structural(base, base.replace("def test_a", "@pytest.mark.slow\ndef test_a"))
    assert gs.is_structural(base, "import pytest\npytestmark = pytest.mark.slow\n" + base)
    assert gs.is_structural(base, None), "a deleted test module removes every test"
    assert not gs.is_structural(None, base), "a new module removes nothing"
    assert gs.is_structural(None, "def test_(:\n"), (
        "an unparsable new module is judged conservatively"
    )
    assert gs.is_structural(base, "def test_(:\n"), "an unparsable head is judged conservatively"


def test_a_changed_marker_argument_is_structural():
    """`timeout(5)` → `timeout(500)` and a changed parametrize case list change the
    marker signature, the spec's 'decorators unchanged'; a body edit does not."""
    base = (
        "import pytest\n@pytest.mark.timeout(5)\n@pytest.mark.parametrize('x', [1, 2])\n"
        "def test_a(x):\n    assert x\n"
    )
    assert gs.is_structural(base, base.replace("timeout(5)", "timeout(500)"))
    assert gs.is_structural(base, base.replace("[1, 2]", "[1, 2, 3]"))
    assert not gs.is_structural(base, base.replace("assert x", "assert x > 0"))
    skip = "import pytest\n@pytest.mark.skipif(False, reason='r')\ndef test_a(): pass\n"
    assert gs.is_structural(skip, skip.replace("skipif(False", "skipif(True"))
    assert gs.is_structural(skip, skip.replace("reason='r'", "reason='s'"))


def test_a_module_is_family_when_the_module_or_every_test_carries_the_mark():
    assert gs.carries_family_mark("import pytest\npytestmark = pytest.mark.interpreter_agnostic\n")
    assert gs.carries_family_mark(
        "import pytest\npytestmark = [pytest.mark.interpreter_agnostic, pytest.mark.timeout(5)]\n"
    )
    # every test carrying the mark itself is the family too: quick's legs deselect them all
    assert gs.carries_family_mark(
        "import pytest\n@pytest.mark.interpreter_agnostic\ndef test_a(): pass\n"
    )
    assert not gs.carries_family_mark(
        "import pytest\n@pytest.mark.interpreter_agnostic\ndef test_a(): pass\ndef test_b(): pass\n"
    )
    assert not gs.carries_family_mark("import pytest\n"), "no test: nothing for the guards"
    assert not gs.carries_family_mark(None)


def _reader(files: "dict[tuple[str, str], str]"):
    return lambda rev, path: files.get((rev, path))


def _scope(changes, files=None, head_paths=None):
    files = files or {}
    if head_paths is None:
        head_paths = {path for (rev, path) in files if rev == "head"}
        head_paths |= {c.path for c in changes if c.status != "D"}
    return gs.classify(changes, _reader(files), "base", "head", head_paths)


def test_a_leaf_test_change_runs_its_package():
    scope = _scope(
        [gs.Change("tests/unit/cli/test_expose.py", "M")],
        {
            ("base", "tests/unit/cli/test_expose.py"): "def test_a(): pass\n",
            ("head", "tests/unit/cli/test_expose.py"): "def test_a(): pass\ndef test_b(): pass\n",
        },
    )
    assert scope.classes == [gs.TESTS_LEAF]
    assert scope.quick_paths == ["tests/unit/cli"]
    assert scope.changed_paths == ["tests/unit/cli/test_expose.py"]
    assert scope.guard_files == []
    assert scope.unknown == []
    assert scope.full_at_commit == []
    assert scope.full_at_tip == []


def test_a_family_module_runs_the_guards_instead_of_quick():
    text = "import pytest\npytestmark = pytest.mark.interpreter_agnostic\ndef test_a(): pass\n"
    scope = _scope(
        [gs.Change("tests/unit/test_lane_invariants.py", "M")],
        {
            ("base", "tests/unit/test_lane_invariants.py"): text,
            ("head", "tests/unit/test_lane_invariants.py"): text + "def test_b(): pass\n",
        },
    )
    assert scope.classes == [gs.TESTS_LEAF]
    assert scope.guard_files == ["tests/unit/test_lane_invariants.py"]
    assert scope.quick_paths == []


def test_a_renamed_test_is_structural():
    scope = _scope(
        [gs.Change("tests/unit/cli/test_expose.py", "M")],
        {
            ("base", "tests/unit/cli/test_expose.py"): "def test_a(): pass\n",
            ("head", "tests/unit/cli/test_expose.py"): "def test_a2(): pass\n",
        },
    )
    assert scope.classes == [gs.TESTS_STRUCTURAL]


def test_a_renamed_file_is_a_delete_plus_an_add_and_structural():
    """The added file is leaf, the deleted one structural; structural dominates."""
    scope = _scope(
        [
            gs.Change("tests/unit/cli/test_old.py", "D"),
            gs.Change("tests/unit/cli/test_new.py", "A"),
        ],
        {
            ("base", "tests/unit/cli/test_old.py"): "def test_a(): pass\n",
            ("head", "tests/unit/cli/test_new.py"): "def test_a(): pass\n",
        },
        head_paths={"tests/unit/cli/test_new.py", "tests/unit/cli/__init__.py"},
    )
    assert scope.classes == [gs.TESTS_STRUCTURAL]
    assert scope.quick_paths == ["tests/unit/cli"]
    assert scope.changed_paths == ["tests/unit/cli/test_new.py", "tests/unit/cli/test_old.py"]
    assert scope.changed_files == ["tests/unit/cli/test_new.py"]


def test_a_deleted_test_module_is_structural_and_runs_its_surviving_package():
    scope = _scope(
        [gs.Change("tests/unit/cli/test_gone.py", "D")],
        {("base", "tests/unit/cli/test_gone.py"): "def test_a(): pass\n"},
        head_paths={"tests/unit/cli/test_other.py"},
    )
    assert scope.classes == [gs.TESTS_STRUCTURAL]
    assert scope.quick_paths == ["tests/unit/cli"]


def test_a_deleted_standalone_test_runs_its_surviving_root():
    scope = _scope(
        [gs.Change("tests/unit/test_gone.py", "D")],
        {("base", "tests/unit/test_gone.py"): "def test_a(): pass\n"},
        head_paths={"tests/unit/cli/test_other.py"},
    )
    assert scope.quick_paths == ["tests/unit"], "never a path that no longer exists"


def test_a_deleted_whole_package_runs_its_tree():
    scope = _scope(
        [gs.Change("tests/unit/gone/test_a.py", "D")],
        {("base", "tests/unit/gone/test_a.py"): "def test_a(): pass\n"},
        head_paths={"tests/unit/cli/test_other.py"},
    )
    assert scope.classes == [gs.TESTS_STRUCTURAL]
    assert scope.quick_paths == ["tests/unit"]


def test_a_package_left_with_only_an_init_climbs_to_its_tree():
    """A surviving `__init__.py` is not a surviving test: quick over the package would be empty."""
    scope = _scope(
        [gs.Change("tests/unit/gone/test_a.py", "D")],
        {("base", "tests/unit/gone/test_a.py"): "def test_a(): pass\n"},
        head_paths={"tests/unit/gone/__init__.py", "tests/unit/cli/test_other.py"},
    )
    assert scope.quick_paths == ["tests/unit"]


def test_a_deleted_tree_with_no_surviving_test_is_full():
    scope = _scope(
        [gs.Change("tests/unit/cli/test_a.py", "D")],
        {("base", "tests/unit/cli/test_a.py"): "def test_a(): pass\n"},
        head_paths={"tests/e2e/cli/test_b.py"},
    )
    assert scope.quick_paths == ["tests"], "tests/unit/cli and tests/unit hold no test; tests/ does"
    scope = _scope(
        [gs.Change("tests/unit/cli/test_a.py", "D")],
        {("base", "tests/unit/cli/test_a.py"): "def test_a(): pass\n"},
        head_paths={"src/otto/x.py"},
    )
    assert scope.full_at_commit == ["tests/unit/cli/test_a.py: no surviving test root"]
    assert scope.quick_paths == []


_FAMILY = "import pytest\npytestmark = pytest.mark.interpreter_agnostic\ndef test_g(): pass\n"


def test_an_emptied_standalone_module_is_not_its_own_quick_root():
    """Still present, but no test left: quick over the file alone would select nothing."""
    path = "tests/unit/test_emptied.py"
    scope = _scope(
        [gs.Change(path, "M")],
        {("base", path): "def test_a(): pass\n", ("head", path): "import pytest\n"},
        head_paths={path, "tests/unit/cli/test_other.py"},
    )
    assert scope.quick_paths == ["tests/unit"]


def test_a_package_left_with_only_family_modules_climbs_to_its_tree():
    """Quick's legs deselect `interpreter_agnostic`: a package whose surviving modules are
    all family-marked holds nothing quick would run, so the root climbs to the tree."""
    scope = _scope(
        [gs.Change("tests/unit/demo/test_plain.py", "D")],
        {
            ("base", "tests/unit/demo/test_plain.py"): "def test_a(): pass\n",
            ("head", "tests/unit/demo/test_guard.py"): _FAMILY,
        },
        head_paths={"tests/unit/demo/test_guard.py", "tests/unit/cli/test_other.py"},
    )
    assert scope.quick_paths == ["tests/unit"]


def test_a_src_tree_left_with_only_family_modules_drops_out_of_quick():
    head = {"src/otto/check/x.py", "tests/unit/check/test_g.py", "tests/unit/test_error_base.py"}
    scope = _scope(
        [gs.Change("src/otto/check/x.py", "M")],
        {
            ("head", "src/otto/check/x.py"): "x = 1\n",
            ("head", "tests/unit/check/test_g.py"): _FAMILY,
        },
        head_paths=head,
    )
    assert scope.full_at_commit == ["src/otto/check/x.py: none of its test trees survives at head"]
    assert scope.quick_paths == []


def test_a_module_whose_every_test_carries_the_family_mark_runs_the_guards():
    path = "tests/unit/result/test_all_agnostic.py"
    source = "import pytest\n" + "".join(
        f"@pytest.mark.interpreter_agnostic\ndef test_{n}(): pass\n" for n in "abc"
    )
    scope = _scope([gs.Change(path, "A")], {("head", path): source})
    assert scope.guard_files == [path]
    assert scope.quick_paths == []
    mixed = source + "def test_d(): pass\n"
    scope = _scope([gs.Change(path, "A")], {("head", path): mixed})
    assert scope.guard_files == []
    assert scope.quick_paths == ["tests/unit/result"]


def test_quicks_exclusions_are_the_recipes():
    makefile = (CHECKOUT_ROOT / "Makefile").read_text()
    clauses = _makefile_marker_variables(makefile)["M_QUICK"].split(" and ")
    assert all(clause.startswith("not ") for clause in clauses), clauses
    assert {clause.removeprefix("not ") for clause in clauses} == gs.QUICK_EXCLUDED


def test_a_quick_root_under_another_emitted_root_is_pruned():
    """pytest would dedup the files, but the line would mislead: the outer root runs both."""
    scope = _scope(
        [gs.Change("tests/unit/test_gone.py", "D"), gs.Change("tests/unit/cli/test_a.py", "M")],
        {
            ("base", "tests/unit/test_gone.py"): "def test_a(): pass\n",
            ("base", "tests/unit/cli/test_a.py"): "def test_a(): pass\n",
            ("head", "tests/unit/cli/test_a.py"): "def test_a(): pass\n",
        },
        head_paths={"tests/unit/cli/test_a.py"},
    )
    assert scope.quick_paths == ["tests/unit"]
    scope = _scope(
        [gs.Change("src/otto/snmp.py", "M")],
        {("head", "src/otto/snmp.py"): "x = 1\n"},
        head_paths={"src/otto/snmp.py", "tests/unit/cli/test_a.py", *gs.GUARD_RIDERS},
    )
    assert scope.quick_paths == ["tests/unit"], "the riders sit inside the unit tree"


def test_a_src_leaf_change_maps_to_its_unit_and_bed_trees_with_the_riders():
    head = {
        "tests/unit/host/test_x.py",
        "tests/integration/host/test_x.py",
        "tests/e2e/host/test_x.py",
        "tests/e2e/console/test_x.py",
        "tests/unit/test_error_base.py",
        "tests/unit/test_declared_additions.py",
        "tests/unit/init/test_init_templates.py",
        "tests/unit/import_budget/test_x.py",
        "tests/unit/api_snapshot/test_x.py",
        "src/otto/host/unix.py",
    }
    scope = _scope(
        [gs.Change("src/otto/host/unix.py", "M")],
        {("head", "src/otto/host/unix.py"): "x = 1\n"},
        head_paths=head,
    )
    assert scope.classes == [gs.SRC_LEAF]
    assert scope.src_packages == ["host"]
    assert scope.changed_files == ["src/otto/host/unix.py"]
    assert set(scope.quick_paths) == {
        "tests/unit/host",
        "tests/integration/host",
        "tests/e2e/host",
        "tests/e2e/console",
        *gs.GUARD_RIDERS,
    }
    assert scope.bed_lanes == ["conformance-bed"]


def test_a_src_tree_whose_tests_are_gone_drops_the_root_and_is_full_when_none_remain():
    head = {"src/otto/host/unix.py", "tests/unit/test_error_base.py"}
    scope = _scope(
        [gs.Change("src/otto/host/unix.py", "M"), gs.Change("tests/unit/host/test_x.py", "D")],
        {
            ("head", "src/otto/host/unix.py"): "x = 1\n",
            ("base", "tests/unit/host/test_x.py"): "def test_a(): pass\n",
        },
        head_paths=head,
    )
    assert scope.quick_paths == []
    assert scope.full_at_commit == [
        "src/otto/host/unix.py: none of its test trees survives at head"
    ]
    scope = _scope(
        [gs.Change("src/otto/check/x.py", "M")],
        {("head", "src/otto/check/x.py"): "x = 1\n"},
        head_paths={"src/otto/check/x.py"},
    )
    assert scope.full_at_commit == ["src/otto/check/x.py: none of its test trees survives at head"]
    assert scope.quick_paths == []


def test_a_src_tree_with_one_surviving_test_tree_keeps_only_that_root():
    head = {"src/otto/host/unix.py", "tests/unit/host/test_y.py", "tests/unit/test_error_base.py"}
    scope = _scope(
        [gs.Change("src/otto/host/unix.py", "M"), gs.Change("tests/unit/host/test_x.py", "D")],
        {
            ("head", "src/otto/host/unix.py"): "x = 1\n",
            ("base", "tests/unit/host/test_x.py"): "def test_a(): pass\n",
        },
        head_paths=head,
    )
    assert scope.full_at_commit == []
    assert scope.quick_paths == ["tests/unit/host", "tests/unit/test_error_base.py"]


def test_a_trigger_file_still_names_its_bed_lane():
    scope = _scope(
        [gs.Change("src/otto/host/__init__.py", "M")], {("head", "src/otto/host/__init__.py"): ""}
    )
    assert scope.classes == [gs.ESCALATE]
    assert scope.bed_lanes == ["conformance-bed"]
    assert scope.quick_paths == []


def test_three_src_packages_escalate():
    changes = [gs.Change(f"src/otto/{pkg}/x.py", "M") for pkg in ("cli", "host", "link")]
    head = {c.path for c in changes} | {
        "tests/unit/cli/test_a.py",
        "tests/unit/host/test_a.py",
        "tests/unit/link/test_a.py",
        "tests/e2e/cli/test_a.py",
        "tests/integration/host/test_a.py",
        "tests/e2e/host/test_a.py",
        "tests/e2e/console/test_a.py",
        "tests/e2e/test_link_impair_e2e.py",
        *gs.GUARD_RIDERS,
        "tests/unit/import_budget/test_a.py",
        "tests/unit/api_snapshot/test_a.py",
    }
    scope = _scope(changes, {("head", c.path): "x = 1\n" for c in changes}, head_paths=head)
    assert scope.classes == [gs.ESCALATE]
    assert scope.full_at_commit == ["3 src packages (> 2)"]
    assert scope.quick_paths == []


def test_a_scripts_change_has_no_quick_paths():
    scope = _scope(
        [gs.Change("scripts/gate_fresh.py", "M")], {("head", "scripts/gate_fresh.py"): "x = 1\n"}
    )
    assert scope.classes == [gs.SCRIPTS]
    assert scope.quick_paths == []


def test_the_union_keeps_every_class_and_an_unknown_path_is_full():
    scope = _scope([gs.Change("docs/overview.md", "M"), gs.Change("uv.lock", "M")])
    assert scope.classes == [gs.DOCS, gs.FULL]
    assert scope.unknown == ["uv.lock"]
    assert scope.full_at_commit == ["no row: uv.lock"]


def test_special_trees_route_to_their_lanes_and_never_to_quick():
    text = "def test_a(): pass\n"
    paths = [
        "tests/conformance/test_exec.py",
        "tests/busybox/test_applets.py",
        "tests/integration/busybox_bed/test_x.py",
        "tests/e2e/monitor/dashboard/test_live.py",
        "tests/e2e/monitor/dashboard/test_replay_soak.py",
        "tests/e2e/chaos/test_console_chaos.py",
    ]
    browser = "import pytest\npytestmark = pytest.mark.browser\ndef test_a(): pass\n"
    files = {
        (rev, p): browser if "dashboard/test_live" in p else text
        for p in paths
        for rev in ("base", "head")
    }
    scope = _scope([gs.Change(p, "M") for p in paths], files)
    assert scope.special == [
        "make busybox",
        "make chaos",
        "make chaos-embedded",
        "make conformance",
        "make coverage-unix",
        "make dashboard",
        "make dashboard-soak",
    ]
    assert scope.special_tip == [
        "make busybox",
        "make chaos",
        "make chaos-embedded",
        "make conformance-bed",
        "make dashboard",
        "make dashboard-soak",
    ]
    assert scope.full_at_tip == ["tests/integration/busybox_bed/test_x.py: full at the tip"]
    assert scope.full_at_commit == []
    assert scope.quick_paths == []


def test_a_mixed_stability_file_runs_both_lanes():
    """test_unix_host.py holds ordinary tests beside its `concurrency` ones: the lane
    selects only the latter (`stability-unit`), so quick over the package runs the rest."""
    text = "import pytest\n@pytest.mark.concurrency\ndef test_a(): pass\ndef test_b(): pass\n"
    scope = _scope(
        [gs.Change("tests/unit/host/test_unix_host.py", "M")],
        {
            ("base", "tests/unit/host/test_unix_host.py"): text,
            ("head", "tests/unit/host/test_unix_host.py"): text,
        },
    )
    assert scope.special == ["make stability-unit"]
    assert scope.special_tip == ["make stability-unit"]
    assert scope.quick_paths == ["tests/unit/host"]
    assert scope.classes == [gs.TESTS_LEAF]


def test_the_conformance_bed_witness_routes_to_the_bed_lane_only():
    text = (
        "import pytest\npytestmark = [pytest.mark.conformance, pytest.mark.conformance_bed]\n"
        "def test_a(): pass\n"
    )
    path = "tests/conformance/test_bed_opener_witness.py"
    scope = _scope([gs.Change(path, "M")], {("base", path): text, ("head", path): text})
    assert scope.special == ["make conformance-bed"]
    assert scope.special_tip == ["make conformance-bed"]
    assert scope.quick_paths == []


def test_a_two_lane_stability_file_names_both():
    text = "import pytest\npytestmark = pytest.mark.stability\ndef test_a(): pass\n"
    path = "tests/integration/host/test_host_stability_contract.py"
    scope = _scope([gs.Change(path, "M")], {("base", path): text, ("head", path): text})
    assert scope.special == ["make stability-embedded", "make stability-unix"]
    assert scope.quick_paths == [], "every test carries the lane mark: nothing is left for quick"


def test_a_marked_file_outside_the_tables_routes_by_its_mark():
    browser = "import pytest\n@pytest.mark.browser\ndef test_a(): pass\ndef test_b(): pass\n"
    scope = _scope(
        [gs.Change("tests/e2e/cov/test_new_browser.py", "A")],
        {("head", "tests/e2e/cov/test_new_browser.py"): browser},
    )
    assert scope.special == ["make dashboard"]
    assert scope.quick_paths == ["tests/e2e/cov"]
    stability = "import pytest\npytestmark = pytest.mark.stability\ndef test_a(): pass\n"
    scope = _scope(
        [gs.Change("tests/unit/host/test_new_soak.py", "A")],
        {("head", "tests/unit/host/test_new_soak.py"): stability},
    )
    assert scope.full_at_commit == [
        "tests/unit/host/test_new_soak.py: stability/chaos mark outside every lane's roots"
    ]
    assert scope.special == []
    assert scope.quick_paths == []


def test_a_pure_lane_file_runs_its_lane_only():
    """Every test carries `kmodcov` (module mark): quick's legs would select nothing → red."""
    pure = "import pytest\npytestmark = pytest.mark.kmodcov\ndef test_a(): pass\n"
    path = "tests/e2e/cov/test_kmodcov_toolchains_e2e.py"
    scope = _scope([gs.Change(path, "M")], {("base", path): pure, ("head", path): pure})
    assert scope.special == ["make kmodcov"]
    assert scope.quick_paths == []
    mixed = (
        "import pytest\n@pytest.mark.kmodcov\ndef test_a(): pass\n\n"
        "class TestPlain:\n    def test_b(self): pass\n"
    )
    scope = _scope([gs.Change(path, "M")], {("base", path): mixed, ("head", path): mixed})
    assert scope.special == ["make kmodcov"]
    assert scope.quick_paths == ["tests/e2e/cov"]


def test_a_special_file_that_is_structural_keeps_the_structural_class():
    scope = _scope(
        [gs.Change("tests/conformance/test_exec.py", "M")],
        {
            ("base", "tests/conformance/test_exec.py"): "def test_a(): pass\ndef test_b(): pass\n",
            ("head", "tests/conformance/test_exec.py"): "def test_a(): pass\n",
        },
    )
    assert scope.classes == [gs.TESTS_STRUCTURAL]
    assert scope.special == ["make conformance"]


def test_a_conftest_under_a_special_tree_adds_that_trees_lane_beside_the_escalation():
    """The full gate excludes the conformance and chaos tiers: a fixture change there owes
    the tree's own lane in both columns, as a test module in the tree would."""
    conformance = _scope([gs.Change("tests/conformance/conftest.py", "M")])
    assert conformance.classes == [gs.ESCALATE]
    assert conformance.special == ["make conformance"]
    assert conformance.special_tip == ["make conformance-bed"]
    chaos = _scope([gs.Change("tests/e2e/chaos/conftest.py", "M")])
    assert chaos.classes == [gs.ESCALATE]
    assert chaos.special == ["make chaos", "make chaos-embedded"]
    assert chaos.special_tip == ["make chaos", "make chaos-embedded"]
    helper = _scope([gs.Change("tests/e2e/chaos/_seed.py", "M")])
    assert helper.classes == [gs.FULL], "a helper keeps its own class"
    assert helper.special == ["make chaos", "make chaos-embedded"]
    plain = _scope([gs.Change("tests/unit/cli/conftest.py", "M")])
    assert plain.classes == [gs.ESCALATE]
    assert plain.special == []
    assert plain.special_tip == []


_ALIASED = (
    "import pytest\n"
    "_CASES = pytest.mark.parametrize('n', [1, 2])\n"
    "_A = pytest.mark.slow\n"
    "_B = pytest.mark.ssh\n"
    "_X = 3\n\n"
    "@_CASES\n"
    "def test_a(n): pass\n"
)


def test_a_changed_alias_definition_is_structural():
    assert gs.is_structural(_ALIASED, _ALIASED.replace("[1, 2]", "[1]"))
    assert not gs.is_structural(_ALIASED, _ALIASED.replace("_X = 3", "_X = 4"))


def test_swapping_one_mark_alias_for_another_is_structural():
    base = _ALIASED.replace("@_CASES", "@_A")
    assert gs.is_structural(base, base.replace("@_A", "@_B"))


def test_an_aliased_lane_mark_routes_like_a_direct_one():
    source = "import pytest\n_K = pytest.mark.kmodcov\n\n@_K\ndef test_a(): pass\n"
    assert gs.marker_names(source)["test_a"] == {"kmodcov"}
    path = "tests/e2e/cov/test_new_kmod.py"
    scope = _scope([gs.Change(path, "A")], {("head", path): source})
    assert scope.special == ["make kmodcov"]
    assert scope.quick_paths == []


_APPLIED_ALIAS = (
    "import pytest\n"
    "P = pytest.mark.parametrize\n"
    "_UNUSED = pytest.mark.parametrize('n', [1, 2])\n\n"
    "@P('x', [1, 2])\n"
    "def test_a(x): pass\n"
)


def test_an_alias_applied_as_a_call_keeps_its_own_arguments():
    """`@P("x", [1, 2])` with `P = pytest.mark.parametrize`: the call's arguments are the
    signature, so `[1, 2]` → `[1]` is structural, as it is for the spelled-out mark."""
    assert gs.marker_signature(_APPLIED_ALIAS)["test_a"] == ["pytest.mark.parametrize('x', [1, 2])"]
    assert gs.marker_names(_APPLIED_ALIAS)["test_a"] == {"parametrize"}
    assert gs.is_structural(_APPLIED_ALIAS, _APPLIED_ALIAS.replace("[1, 2])\ndef", "[1])\ndef"))


def test_an_unused_alias_is_not_part_of_the_signature():
    """No test applies `_UNUSED`: editing its arguments changes no test's marks."""
    edited = _APPLIED_ALIAS.replace("('n', [1, 2])", "('n', [1])")
    assert edited != _APPLIED_ALIAS
    assert not gs.is_structural(_APPLIED_ALIAS, edited)


def test_a_concatenated_pytestmark_is_fingerprinted_whole():
    """`A + B` and a starred list are not element lists the walker reads mark by mark:
    the whole assigned value (module names it references resolved) is the module slot,
    so any edit to it, or to a list it names, is structural."""
    added = "import pytest\npytestmark = [pytest.mark.slow] + [pytest.mark.timeout(5)]\n"
    added += "def test_a(): pass\n"
    assert gs.is_structural(added, added.replace("timeout(5)", "timeout(6)"))
    starred = (
        "import pytest\n_COMMON = [pytest.mark.timeout(5)]\n"
        "pytestmark = [*_COMMON, pytest.mark.slow]\ndef test_a(): pass\n"
    )
    assert gs.is_structural(starred, starred.replace("timeout(5)", "timeout(6)"))
    helper = "import pytest\ndef _marks(): return []\npytestmark = _marks()\ndef test_a(): pass\n"
    assert gs.is_structural(helper, helper.replace("pytestmark = _marks()", "pytestmark = []"))


@pytest.mark.parametrize(
    "pytestmark_source",
    [
        "_COMMON = [pytest.mark.kmodcov]\npytestmark = [*_COMMON, pytest.mark.slow]\n",
        "def _marks():\n    return [pytest.mark.kmodcov]\npytestmark = _marks()\n",
        "pytestmark = [pytest.mark.slow] + [pytest.mark.kmodcov]\n",
    ],
)
def test_an_unreadable_pytestmark_is_the_full_gate(pytestmark_source):
    """A mark the walker cannot read may be a lane mark quick deselects: the commit column
    is the full gate with the reason named, never quick over the package."""
    source = "import pytest\n" + pytestmark_source + "def test_a(): pass\n"
    assert gs._UNPARSABLE in gs.marker_names(source)["pytestmark"]
    path = "tests/unit/cov/test_new_unreadable.py"
    scope = _scope([gs.Change(path, "A")], {("head", path): source})
    assert scope.full_at_commit == [f"{path}: unreadable pytestmark"]
    assert scope.quick_paths == []


def test_an_unreadable_decorator_is_the_full_gate():
    """A decorator naming a module-level binding that is not a mark may stand for a lane
    mark the walker cannot see: the full gate, as for an unreadable pytestmark."""
    source = "import pytest\n_D = object()\n\n@_D\ndef test_a(): pass\n"
    path = "tests/unit/cov/test_new_decorated.py"
    scope = _scope([gs.Change(path, "A")], {("head", path): source})
    assert scope.full_at_commit == [f"{path}: unreadable pytestmark"]
    assert scope.quick_paths == []


@pytest.mark.parametrize(
    "marks", ["pytest.mark.kmodcov", "[pytest.mark.kmodcov]", "(pytest.mark.kmodcov,)"]
)
def test_a_parameter_level_lane_mark_routes_to_its_lane(marks):
    source = (
        "import pytest\n"
        f"@pytest.mark.parametrize('x', [pytest.param(1, marks={marks})])\n"
        "def test_a(x): pass\n"
    )
    assert "kmodcov" in gs.marker_names(source)["test_a"]
    path = "tests/unit/cov/test_new_param.py"
    scope = _scope([gs.Change(path, "A")], {("head", path): source})
    assert scope.special == ["make kmodcov"]
    assert scope.quick_paths == [], "every item carries kmodcov: quick's legs would select none"
    mixed = source.replace("[pytest.param(", "[2, pytest.param(")
    scope = _scope([gs.Change(path, "A")], {("head", path): mixed})
    assert scope.special == ["make kmodcov"]
    assert scope.quick_paths == ["tests/unit/cov"], "the unmarked parameter is quick's"


def test_a_parameter_without_marks_changes_nothing():
    plain = "import pytest\n@pytest.mark.parametrize('x', [1])\ndef test_a(x): pass\n"
    wrapped = plain.replace("[1]", "[pytest.param(1, id='one')]")
    assert gs.marker_names(wrapped) == gs.marker_names(plain) == {"test_a": {"parametrize"}}
    path = "tests/unit/cov/test_new_plain.py"
    scope = _scope([gs.Change(path, "A")], {("head", path): wrapped})
    assert scope.special == []
    assert scope.quick_paths == ["tests/unit/cov"]


def test_nested_classes_merge_every_enclosing_class_mark():
    source = (
        "import pytest\n"
        "@pytest.mark.kmodcov\n"
        "class TestA:\n"
        "    class TestB:\n"
        "        def test_c(self): pass\n"
    )
    assert not gs._has_tests_outside(source, {"kmodcov"})
    plain = source.replace("@pytest.mark.kmodcov\n", "")
    assert gs._has_tests_outside(plain, {"kmodcov"})


def _tracked(prefix: str) -> "list[str]":
    """Tracked paths under *prefix*, NUL-delimited, with the suite's hermetic git env."""
    out = subprocess.run(
        ["git", "ls-files", "-z", "--", prefix],
        cwd=CHECKOUT_ROOT,
        capture_output=True,
        text=True,
        check=True,
        env=git_env(CHECKOUT_ROOT),
    ).stdout
    return [p for p in out.split("\0") if p]


def _head_paths() -> "set[str]":
    return set(_tracked("."))


def _at_head(changed: str, text: str):
    """A reader for the real tree: `text` for the changed path, the file on disk otherwise."""

    def read(_rev: str, path: str) -> "str | None":
        if path == changed:
            return text
        target = CHECKOUT_ROOT / path
        return target.read_text(encoding="utf-8") if target.is_file() else None

    return read


def test_every_src_path_has_a_row():
    missing = [
        path
        for path in _tracked("src/otto")
        if gs.classify_path(path) is None
        or (gs.classify_path(path) == gs.SRC_LEAF and gs._src_key(path) not in gs.UNIT_TREES)
    ]
    assert not missing, "src paths with no row (add to UNIT_TREES or TRIGGER_*):\n  " + "\n  ".join(
        missing
    )


def test_every_standalone_unit_module_named_for_a_package_is_in_its_row():
    """A standalone `tests/unit/test_<...>.py` whose name carries a package's key tests that
    package: a leaf change to the package must run it, so the key's row holds the file or a
    directory above it. Tokens are the basename split on `_` (`test`/`py` dropped)."""
    missing = []
    for path in _tracked("tests/unit"):
        parts = path.split("/")
        if len(parts) != _STANDALONE_DEPTH or not gs._is_test_module(path):
            continue
        tokens = set(parts[-1].removesuffix(".py").split("_")) - {"test", "py"}
        for key, row in gs.UNIT_TREES.items():
            if key in tokens and not any(path == r or path.startswith(r + "/") for r in row):
                missing.append(f"{key}: {path}")
    assert missing == [], "add each module to its package's UNIT_TREES row:\n  " + "\n  ".join(
        missing
    )


_STANDALONE_DEPTH = 3  # tests/unit/test_x.py


def _src_leaf_quick(path: str) -> "list[str]":
    """The `quick` roots a one-line change to `path` gets against the real tree."""
    scope = gs.classify(
        [gs.Change(path, "M")], _at_head(path, "x = 1\n"), "base", "head", _head_paths()
    )
    return scope.quick_paths


_RIDERS = [
    "tests/unit/api_snapshot",
    "tests/unit/import_budget",
    "tests/unit/init/test_init_templates.py",
    "tests/unit/test_declared_additions.py",
    "tests/unit/test_error_base.py",
]


def test_a_kmodcov_change_runs_its_direct_tests():
    # the row's family-marked modules (collate, kernel columns, matrix, provision) hold
    # nothing quick's legs select: `make quick-guards`, which a src-leaf commit runs, is theirs
    assert _src_leaf_quick("src/otto/kmodcov/library.py") == sorted(
        [
            "tests/e2e/cov",
            "tests/integration/cov",
            "tests/unit/cov",
            "tests/unit/host/test_kmod_tool_kind.py",
            "tests/unit/test_kmodcov_export.py",
            *_RIDERS,
        ]
    )


def test_a_fleet_change_runs_its_direct_tests():
    assert _src_leaf_quick("src/otto/fleet.py") == sorted(
        [
            "tests/unit/fleet",
            "tests/unit/test_lab_facade.py",
            *_RIDERS,
        ]
    )


def test_a_project_change_runs_its_direct_tests():
    # the row's interpreter-agnostic test_project_scaffold_policy.py is quick-guards', not quick's
    assert _src_leaf_quick("src/otto/project/actions.py") == sorted(
        [
            "tests/unit/project",
            "tests/unit/test_fake_project_fixture.py",
            "tests/unit/test_project_instruction_table.py",
            "tests/unit/test_project_scaffold_fixture.py",
            *_RIDERS,
        ]
    )


def test_a_cli_change_runs_its_direct_tests():
    assert _src_leaf_quick("src/otto/cli/expose.py") == sorted(
        [
            "tests/e2e/cli",
            "tests/unit/cli",
            "tests/unit/test_cli_exposed_output_dir.py",
            "tests/unit/test_utils_cli_markers.py",
            *_RIDERS,
        ]
    )


def test_every_tracked_test_module_classifies_to_a_runnable_scope():
    """Every test module, changed alone: a class, and either quick roots that hold a test
    at head, guard files, a special lane, or a stated full-gate reason — never nothing.

    This pins that an answer EXISTS; whether the answer's lanes select the module's items
    is the census's question (`test_every_collected_item_is_selected_by_the_oracles_commit_lanes`).
    """
    head = _head_paths()
    silent = []
    for path in _tracked("tests"):
        if not gs._is_test_module(path):
            continue
        text = (CHECKOUT_ROOT / path).read_text(encoding="utf-8")
        scope = gs.classify([gs.Change(path, "M")], _at_head(path, text), "base", "head", head)
        runs = scope.quick_paths or scope.guard_files or scope.special or scope.full_at_commit
        if not runs or any(not gs._holds_a_test(p, head) for p in scope.quick_paths):
            silent.append(f"{path}: {scope}")
    assert silent == [], "\n".join(silent)


def test_every_special_glob_matches_a_tracked_test_module():
    tracked = [p for p in _tracked("tests") if gs._is_test_module(p)]
    empty = [
        glob for glob, _a, _b in gs.SPECIAL_TREES if not any(gs._match(p, (glob,)) for p in tracked)
    ]
    assert empty == [], f"special rows matching no tracked test module: {empty}"


def test_every_other_tree_scripts_web_docs_and_build_file_has_a_row():
    probes = {
        "scripts/x.py",
        "web/src/x.ts",
        "docs/x.md",
        "README.md",
        "Makefile",
        "noxfile.py",
        "pyproject.toml",
        ".coveragerc",
        ".github/workflows/ci.yml",
        ".githooks/pre-push",
    }
    unknown = sorted(p for p in probes if gs.classify_path(p) is None)
    assert unknown == [], unknown


def test_every_path_and_lane_the_tables_name_exists():
    paths = (
        {p for trees in gs.UNIT_TREES.values() for p in trees}
        | set(gs.GUARD_RIDERS)
        | set(gs.STABILITY_FILES)
    )
    missing = sorted(p for p in paths if not (CHECKOUT_ROOT / p).exists())
    assert missing == [], f"rows naming paths that do not exist: {missing}"
    makefile = (CHECKOUT_ROOT / "Makefile").read_text()
    commands = [
        *(c for lanes in gs.STABILITY_FILES.values() for c in lanes),
        *gs.LANE_MARKERS.values(),
        *gs.FULL_GATE,
    ]
    commands += [c for _g, a, b in gs.SPECIAL_TREES for c in (*a, *b) if c != gs.FULL]
    lanes = {cmd.split()[1] for cmd in commands if cmd.startswith("make ")}
    lanes |= {lane for lanes_ in gs.BED_LANES.values() for lane in lanes_}
    missing_lanes = sorted(
        name for name in lanes if not re.search(rf"^{re.escape(name)}:", makefile, re.MULTILINE)
    )
    assert missing_lanes == [], missing_lanes
    assert set(gs.LANE_COVERS) >= {c for lanes in gs.STABILITY_FILES.values() for c in lanes} | set(
        gs.LANE_MARKERS.values()
    )


def test_the_stability_files_are_the_lanes_own():
    from tests.unit.test_lane_invariants import _STABILITY_UNIT_FILES, _STABILITY_UNIX_FILES

    assert {p for p, lanes in gs.STABILITY_FILES.items() if "make stability-unit" in lanes} == set(
        _STABILITY_UNIT_FILES
    )
    assert {p for p, lanes in gs.STABILITY_FILES.items() if "make stability-unix" in lanes} == set(
        _STABILITY_UNIX_FILES
    )


def test_removing_a_special_row_changes_the_routing(monkeypatch):
    """Control for the classifier: without the busybox row the tree's module is a plain leaf
    and routes to quick over itself (a tree-level file is its own root). The oracle census
    (`test_the_census_reddens_when_a_lane_row_is_removed`) is what turns that into a red:
    quick's legs deselect every `busybox` item."""
    monkeypatch.setattr(
        gs, "SPECIAL_TREES", [r for r in gs.SPECIAL_TREES if not r[0].startswith("tests/busybox/")]
    )
    head = _head_paths()
    path = next(p for p in _tracked("tests/busybox") if gs._is_test_module(p))
    scope = gs.classify(
        [gs.Change(path, "M")], _at_head(path, "def test_a(): pass\n"), "base", "head", head
    )
    assert scope.special == [], "the control did not change the routing"
    assert scope.quick_paths == [path], "the control did not change the routing"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True, env=git_env(repo)
    ).stdout


def _repo(tmp_path: Path) -> Path:
    """main with two commits; a feature branch with two commits on top; origin/main is main."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "docs").mkdir()
    (repo / "tests/unit/cli").mkdir(parents=True)
    (repo / "src/otto/cli").mkdir(parents=True)
    (repo / "tests/e2e/cli").mkdir(parents=True)
    (repo / "docs/a.md").write_text("a\n")
    (repo / "tests/unit/cli/test_a.py").write_text("def test_a(): pass\n")
    (repo / "tests/e2e/cli/test_e.py").write_text("def test_e(): pass\n")
    (repo / "src/otto/cli/x.py").write_text("x = 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    (repo / "README.md").write_text("r\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base 2")
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    _git(repo, "checkout", "-qb", "feature")
    (repo / "docs/a.md").write_text("a b\n")
    _git(repo, "commit", "-qam", "docs")
    (repo / "src/otto/cli/x.py").write_text("x = 2\n")
    (repo / "tests/unit/cli/test_a.py").write_text("def test_a(): pass\ndef test_b(): pass\n")
    _git(repo, "commit", "-qam", "src+test")
    return repo


def _closed_environ(monkeypatch: pytest.MonkeyPatch, repo: Path) -> "dict[str, str]":
    """Replace the WHOLE ambient environment with `git_env(repo)` for the test's duration.

    `git_env` is complete and must never be merged over os.environ (gitrepo.py:39-40):
    an ambient GIT_DIR, GIT_WORK_TREE or GIT_CONFIG_* would otherwise reach every git the
    script — and gate_fresh's `tree_state`, which takes no env — spawns in-process.
    """
    closed = git_env(repo)
    for key in list(os.environ):
        monkeypatch.delenv(key, raising=False)
    for key, value in closed.items():
        monkeypatch.setenv(key, value)
    return closed


def _main(monkeypatch: pytest.MonkeyPatch, repo: Path, *args: str) -> int:
    """The script's main under a closed environment: its own spawns get `env=`, and the
    spawns it cannot parameterize (`tree_state`) inherit the same closed os.environ."""
    closed = _closed_environ(monkeypatch, repo)
    return gs.main(["--repo", str(repo), *args], env=closed)


_SCOPE_FIELDS = [
    "quick", "guards", "packages", "files", "paths", "special", "special_tip", "bed",
    "unknown", "full_commit", "full_tip",
]  # fmt: skip


def _s(classes, **fields):
    """A Scope by keyword, in field order; `paths` defaults to `files` (nothing deleted)."""
    unknown = set(fields) - set(_SCOPE_FIELDS)
    if unknown:
        raise TypeError(f"unknown Scope field(s): {sorted(unknown)}")
    given = {name: list(fields.get(name, [])) for name in _SCOPE_FIELDS}
    if "paths" not in fields:
        given["paths"] = list(given["files"])
    return gs.Scope(list(classes), *(given[name] for name in _SCOPE_FIELDS))


def test_commands_for_each_class():
    base = "origin/main"
    cb = "make check-breaking"
    assert gs.commands(_s([gs.NONE]), tip=False, base=base) == [cb]
    assert gs.commands(_s([gs.NONE]), tip=True, base=base) == [cb]
    assert gs.commands(_s([gs.NONE]), tip=False, base="main") == [
        "make check-breaking RANGE=main..HEAD"
    ]
    docs = _s([gs.DOCS], files=["docs/a.md"])
    assert gs.commands(docs, tip=False, base=base) == [cb, "make docs"]
    assert gs.commands(docs, tip=True, base=base) == [cb, "make gate-fresh"]
    web = _s([gs.WEB], files=["web/src/App.tsx"])
    assert gs.commands(web, tip=False, base=base) == [cb, "make check-ts test-ts"]
    assert gs.commands(web, tip=True, base=base) == [
        cb,
        "make check-ts coverage-ts",
        "make gate-fresh",
    ]
    leaf = _s([gs.TESTS_LEAF], quick=["tests/unit/cli"], files=["tests/unit/cli/test_a.py"])
    assert gs.commands(leaf, tip=False, base=base) == [
        cb,
        'make quick PATHS="tests/unit/cli"',
        "uv run ruff check tests/unit/cli/test_a.py",
        "make lint-arch",
    ]
    assert gs.commands(leaf, tip=True, base=base) == [
        cb,
        'make quick PATHS="tests/unit/cli"',
        "uv run ruff check tests/unit/cli/test_a.py",
        "make lint-arch",
        "make quick-guards",
        "make gate-fresh",
    ]
    structural = _s(
        [gs.TESTS_STRUCTURAL], quick=["tests/unit/cli"], files=["tests/unit/cli/test_a.py"]
    )
    assert gs.commands(structural, tip=False, base=base) == [
        cb,
        'make quick PATHS="tests/unit/cli"',
        "uv run ruff check tests/unit/cli/test_a.py",
        "make lint-arch",
        "make quick-guards",
    ]
    guard = _s(
        [gs.TESTS_LEAF],
        guards=["tests/unit/test_lane_invariants.py"],
        files=["tests/unit/test_lane_invariants.py"],
    )
    assert gs.commands(guard, tip=False, base=base) == [
        cb,
        "uv run ruff check tests/unit/test_lane_invariants.py",
        "make lint-arch",
        "make quick-guards",
    ]
    src = _s(
        [gs.SRC_LEAF],
        quick=["tests/unit/host", *gs.GUARD_RIDERS],
        packages=["host"],
        files=["src/otto/host/unix.py"],
        bed=["conformance-bed"],
    )
    assert gs.commands(src, tip=False, base=base) == [
        cb,
        f'make quick PATHS="{" ".join(["tests/unit/host", *gs.GUARD_RIDERS])}"',
        "uv run ty check src/otto/host/unix.py",
        "uv run ruff check src/otto/host/unix.py",
        "make lint-arch",
        "make quick-guards",
    ]
    assert gs.commands(src, tip=True, base=base) == [cb, *gs.FULL_GATE, "make conformance-bed"]
    scripts = _s([gs.SCRIPTS], files=["scripts/gate_fresh.py"])
    assert gs.commands(scripts, tip=False, base=base) == [
        cb,
        "make quick-guards",
        "uv run ty check --extra-search-path . scripts/gate_fresh.py",
        "uv run ruff check scripts/gate_fresh.py",
    ]
    assert gs.commands(scripts, tip=True, base=base) == [cb, *gs.FULL_GATE]
    esc = _s([gs.ESCALATE], files=["Makefile"], bed=["stability-unix"])
    assert gs.commands(esc, tip=False, base=base) == [
        cb,
        *gs.FULL_GATE,
        "make stability-unix",
        "make quick-guards",
    ]
    assert gs.commands(esc, tip=True, base=base) == [cb, *gs.FULL_GATE, "make stability-unix"]
    conftest = _s([gs.ESCALATE], files=["tests/conftest.py"])
    assert gs.commands(conftest, tip=False, base=base) == [
        cb,
        *gs.FULL_GATE,
        "make nox-unit-repeat",
    ]
    # the landing is one squashed commit and nobody re-runs the oracle on it: the tip owes
    # the repeat rider too (quick-guards stays commit-only — `make coverage` runs the family)
    assert gs.commands(conftest, tip=True, base=base) == [
        cb,
        *gs.FULL_GATE,
        "make nox-unit-repeat",
    ]
    deleted_conftest = _s([gs.ESCALATE], files=[], paths=["tests/unit/cli/conftest.py"])
    assert gs.commands(deleted_conftest, tip=False, base=base) == [
        cb,
        *gs.FULL_GATE,
        "make nox-unit-repeat",
    ]
    assert gs.commands(deleted_conftest, tip=True, base=base) == [
        cb,
        *gs.FULL_GATE,
        "make nox-unit-repeat",
    ]
    deleted_fixture = _s([gs.ESCALATE], files=[], paths=["tests/_fixtures/old.py"])
    assert gs.commands(deleted_fixture, tip=False, base=base)[-1] == "make nox-unit-repeat"
    registry = _s([gs.ESCALATE], files=["src/otto/registry.py"])
    assert gs.commands(registry, tip=False, base=base)[-1] == "make nox-unit-repeat"
    assert gs.commands(registry, tip=True, base=base)[-1] == "make nox-unit-repeat"
    full = _s(
        [gs.DOCS, gs.FULL],
        files=["docs/a.md", "uv.lock"],
        unknown=["uv.lock"],
        full_commit=["no row: uv.lock"],
    )
    assert gs.commands(full, tip=False, base=base) == [cb, *gs.FULL_GATE], (
        "the full gate already runs `make docs`"
    )
    special = _s(
        [gs.TESTS_LEAF],
        files=["tests/conformance/test_x.py"],
        special=["make conformance"],
        special_tip=["make conformance-bed"],
    )
    assert gs.commands(special, tip=False, base=base) == [
        cb,
        "uv run ruff check tests/conformance/test_x.py",
        "make lint-arch",
        "make conformance",
    ]
    assert gs.commands(special, tip=True, base=base) == [
        cb,
        "uv run ruff check tests/conformance/test_x.py",
        "make lint-arch",
        "make quick-guards",
        "make conformance-bed",
        "make gate-fresh",
    ]
    tip_full = _s(
        [gs.TESTS_LEAF],
        files=["tests/integration/busybox_bed/test_x.py"],
        special=["make coverage-unix"],
        full_tip=["tests/integration/busybox_bed/test_x.py: full at the tip"],
    )
    assert gs.commands(tip_full, tip=False, base=base) == [
        cb,
        "uv run ruff check tests/integration/busybox_bed/test_x.py",
        "make lint-arch",
        "make coverage-unix",
    ]
    assert gs.commands(tip_full, tip=True, base=base) == [cb, *gs.FULL_GATE]


def test_a_full_column_keeps_the_special_lanes():
    """`make coverage` excludes the busybox/conformance/stability tiers (M_COVERAGE):
    a Makefile change beside a conformance test change still owes `make conformance`."""
    base = "origin/main"
    both = _s(
        [gs.ESCALATE, gs.TESTS_LEAF],
        files=["Makefile", "tests/conformance/test_x.py"],
        special=["make conformance"],
        special_tip=["make conformance-bed"],
    )
    assert gs.commands(both, tip=False, base=base) == [
        cb := "make check-breaking",
        *gs.FULL_GATE,
        "make quick-guards",
        "make conformance",
    ]
    assert gs.commands(both, tip=True, base=base) == [cb, *gs.FULL_GATE, "make conformance-bed"]


def test_the_union_composes_every_class_before_deduplication():
    base = "origin/main"
    mixed = _s(
        [gs.SRC_LEAF, gs.WEB, gs.DOCS],
        quick=["tests/unit/cli"],
        packages=["cli"],
        files=["src/otto/cli/x.py", "web/src/App.tsx", "docs/a.md"],
    )
    at_tip = gs.commands(mixed, tip=True, base=base)
    assert at_tip == ["make check-breaking", *gs.FULL_GATE, "make check-ts coverage-ts"], at_tip
    at_commit = gs.commands(mixed, tip=False, base=base)
    assert at_commit == [
        "make check-breaking",
        'make quick PATHS="tests/unit/cli"',
        "uv run ty check src/otto/cli/x.py",
        "uv run ruff check src/otto/cli/x.py",
        "make lint-arch",
        "make quick-guards",
        "make docs",
        "make check-ts test-ts",
    ], at_commit
    escalated = _s([gs.ESCALATE, gs.WEB], files=["Makefile", "web/src/App.tsx"])
    assert gs.commands(escalated, tip=False, base=base) == [
        "make check-breaking",
        *gs.FULL_GATE,
        "make quick-guards",
        "make check-ts test-ts",
    ]
    assert gs.commands(escalated, tip=True, base=base) == [
        "make check-breaking",
        *gs.FULL_GATE,
        "make check-ts coverage-ts",
    ]


# Lines outside the census's scope: check-breaking, docs, gate-fresh and check-ts run no
# Python test leg this census reads (gate-fresh re-runs CI's lanes in a throwaway worktree;
# check-ts is the TypeScript gate). `check-python` and `lint-arch` DO reach pytest (the
# ast-grep rule tests, `lint-arch`) and are walked like any lane.
_OUT_OF_SCOPE_LINES = frozenset({"check-breaking", "docs", "gate-fresh", "check-ts"})
_CANARY = _string_constants(ast.parse((CHECKOUT_ROOT / "noxfile.py").read_text()))["CANARY_PYTHON"]
_HOSTLESS_LINE = f"nox -s tests_hostless-{_CANARY}"
# The emitted lines that run no pytest: the type checker and the linter over changed files.
_NO_PYTEST_PREFIXES = ("uv run ty check ", "uv run ruff check ")


def _quick_legs(makefile: str, roots: "list[str]") -> "list[_Leg]":
    """`make quick PATHS="<roots>"`'s legs, read from the quick target's live recipe.

    `$(PATHS)` is substituted with the concrete roots, and the invocations go through the
    same reader as every reached lane, so a `-k`, `--deselect` or `--ignore` in the recipe is
    refused and an edited `-m` is what the census evaluates.
    """
    invocations = []
    for tokens in _makefile_pytest_invocations("\n".join(makefile_live_recipe(makefile, "quick"))):
        assert _PATHS_TOKEN in tokens, f"a quick leg without {_PATHS_TOKEN}: {' '.join(tokens)}"
        at = tokens.index(_PATHS_TOKEN)
        invocations.append([*tokens[:at], *roots, *tokens[at + 1 :]])
    assert invocations, "the census found no pytest leg in the quick recipe"
    return _legs_including_markerless(
        "Makefile:quick", invocations, _makefile_marker_variables(makefile)
    )


def _oracle_legs(lines: "list[str]", makefile: str) -> "list[_Leg]":
    """The pytest legs the oracle's commands run.

    `make quick` is read from its recipe in *makefile* (`$(PATHS)` substituted); the
    hostless nox session on the canary is read from noxfile.py with the interpreter
    RESOLVED (`make_legs("nox-hostless")` cannot: the `nox-hostless` target names the
    session without one and the reader refuses an unresolved ladder); every other
    `make <x>` is the legs `make <x>` reaches (nox sessions included). Only the ty and ruff
    lines run no pytest. A line this reader does not know is an error, never a skip.
    """
    legs: "list[_Leg]" = []
    for line in lines:
        if line.startswith('make quick PATHS="'):
            legs += _quick_legs(makefile, line[len('make quick PATHS="') : -1].split())
        elif line == _HOSTLESS_LINE:
            legs += _legs_including_markerless(
                f"noxfile.py:tests_hostless@{_CANARY}",
                _python_pytest_invocations(
                    CHECKOUT_ROOT / "noxfile.py",
                    resolve_names=True,
                    scope="tests_hostless",
                    python=_CANARY,
                ),
            )
        elif line.startswith("make ") and line.split()[1] not in _OUT_OF_SCOPE_LINES:
            legs += make_legs(line.split()[1])
        elif line.startswith(_NO_PYTEST_PREFIXES) or (
            line.startswith("make ") and line.split()[1] in _OUT_OF_SCOPE_LINES
        ):
            continue
        else:
            raise AssertionError(
                f"the census does not know what `{line}` runs — teach _oracle_legs"
            )
    return legs


def _uncovered(path: str, lines: "list[str]", collect, makefile: str) -> "list[str]":
    """Node ids collected in *path* that no emitted leg selects — full gate or not."""
    compiled = [(leg, Expression.compile(leg.expr)) for leg in _oracle_legs(lines, makefile)]
    gaps = []
    for item in collect(()):
        item_path = item.nodeid.split("::", 1)[0]
        if item_path != path:
            continue
        matcher = _marker_matcher(item.markers)
        if not any(
            _within(item_path, leg.roots) and expr.evaluate(matcher) for leg, expr in compiled
        ):
            gaps.append(item.nodeid)
    return gaps


@pytest.mark.timeout(600)
@pytest.mark.xdist_group("lane_leg_membership")
def test_every_collected_item_is_selected_by_the_oracles_commit_lanes(collect_marker_sets):
    """THE CENSUS. For every tracked test module changed alone, the commit column's
    legs — the full gate's included — select every item pytest collects in it."""
    head = _head_paths()
    makefile = (CHECKOUT_ROOT / "Makefile").read_text()
    gaps = []
    for path in _tracked("tests"):
        if not gs._is_test_module(path):
            continue
        text = (CHECKOUT_ROOT / path).read_text(encoding="utf-8")
        scope = gs.classify([gs.Change(path, "M")], _at_head(path, text), "base", "head", head)
        lines = gs.commands(scope, tip=False, base="origin/main")
        gaps += [
            f"{nodeid}  ← {lines}"
            for nodeid in _uncovered(path, lines, collect_marker_sets, makefile)
        ]
    assert gaps == [], f"{len(gaps)} item(s) no emitted lane selects:\n  " + "\n  ".join(gaps[:60])


@pytest.mark.xdist_group("lane_leg_membership")
def test_the_census_reddens_when_a_lane_row_is_removed(collect_marker_sets, monkeypatch):
    """Negative control through the SAME checker: without the busybox row (the tree row AND the
    `busybox` mark route that would otherwise still reach the lane) the module routes to
    quick, whose legs deselect every `busybox` item — the census must report them."""
    monkeypatch.setattr(
        gs, "SPECIAL_TREES", [r for r in gs.SPECIAL_TREES if not r[0].startswith("tests/busybox/")]
    )
    monkeypatch.setattr(
        gs, "LANE_MARKERS", {k: v for k, v in gs.LANE_MARKERS.items() if k != "busybox"}
    )
    head = _head_paths()
    makefile = (CHECKOUT_ROOT / "Makefile").read_text()
    path = next(p for p in _tracked("tests/busybox") if gs._is_test_module(p))
    text = (CHECKOUT_ROOT / path).read_text(encoding="utf-8")
    scope = gs.classify([gs.Change(path, "M")], _at_head(path, text), "base", "head", head)
    lines = gs.commands(scope, tip=False, base="origin/main")
    assert any(line.startswith("make quick PATHS") for line in lines)
    assert "make busybox" not in lines
    gaps = _uncovered(path, lines, collect_marker_sets, makefile)
    assert gaps, "the control removed the row but the census saw no gap — it proves nothing"


@pytest.mark.xdist_group("lane_leg_membership")
def test_the_busybox_tree_row_and_the_mark_route_are_redundant_on_purpose(
    collect_marker_sets, monkeypatch
):
    """Removing only the TREE row leaves the `busybox` mark route: the module still emits
    `make busybox` and the census finds no gap. Each route alone is enough, deliberately."""
    monkeypatch.setattr(
        gs, "SPECIAL_TREES", [r for r in gs.SPECIAL_TREES if not r[0].startswith("tests/busybox/")]
    )
    head = _head_paths()
    makefile = (CHECKOUT_ROOT / "Makefile").read_text()
    path = next(p for p in _tracked("tests/busybox") if gs._is_test_module(p))
    text = (CHECKOUT_ROOT / path).read_text(encoding="utf-8")
    scope = gs.classify([gs.Change(path, "M")], _at_head(path, text), "base", "head", head)
    lines = gs.commands(scope, tip=False, base="origin/main")
    assert "make busybox" in lines
    assert _uncovered(path, lines, collect_marker_sets, makefile) == []


@pytest.mark.xdist_group("lane_leg_membership")
def test_the_census_reddens_when_a_full_column_hides_a_lane(collect_marker_sets, monkeypatch):
    """Second control: without the chaos row a chaos module is a stray mark → the FULL
    column. The console chaos module carries `stability` (test_console_chaos.py:90-93), which
    M_COVERAGE and M_HOSTLESS exclude (M_COVERAGE, M_HOSTLESS), so the full gate's own legs select
    none of its items: the census must report them. A full column is never a vacuous pass."""
    monkeypatch.setattr(
        gs,
        "SPECIAL_TREES",
        [r for r in gs.SPECIAL_TREES if not r[0].startswith("tests/e2e/chaos/")],
    )
    head = _head_paths()
    makefile = (CHECKOUT_ROOT / "Makefile").read_text()
    path = "tests/e2e/chaos/test_console_chaos.py"
    text = (CHECKOUT_ROOT / path).read_text(encoding="utf-8")
    scope = gs.classify([gs.Change(path, "M")], _at_head(path, text), "base", "head", head)
    lines = gs.commands(scope, tip=False, base="origin/main")
    assert "make coverage" in lines, lines
    assert "make chaos" not in lines, lines
    gaps = _uncovered(path, lines, collect_marker_sets, makefile)
    assert gaps, "the full column hid the chaos lane and the census saw no gap — it proves nothing"


def _quick_routed(path: str) -> "list[str]":
    """The commit column for a one-module change, read from disk like the census."""
    text = (CHECKOUT_ROOT / path).read_text(encoding="utf-8")
    scope = gs.classify([gs.Change(path, "M")], _at_head(path, text), "base", "head", _head_paths())
    return gs.commands(scope, tip=False, base="origin/main")


_QUICK_CONTROL = "tests/unit/cli/test_cache_cli.py"
_PARALLEL_SELECTOR = '-m "$(M_QUICK) and not serial_timing" --no-cov'


@pytest.mark.xdist_group("lane_leg_membership")
def test_the_census_reads_quicks_recipe_not_a_copy_of_it(collect_marker_sets):
    """Negative controls through the SAME checker: quick's legs come from its recipe, so an
    edit to the recipe's selection reaches the census. A selector that matches nothing
    leaves the module's items uncovered; a narrowing flag (`-k`) is refused outright."""
    lines = _quick_routed(_QUICK_CONTROL)
    assert any(line.startswith('make quick PATHS="') for line in lines), lines
    makefile = (CHECKOUT_ROOT / "Makefile").read_text()
    assert _PARALLEL_SELECTOR in makefile
    assert _uncovered(_QUICK_CONTROL, lines, collect_marker_sets, makefile) == []
    emptied = makefile.replace(
        _PARALLEL_SELECTOR, '-m "$(M_QUICK) and not serial_timing and no_such_mark" --no-cov'
    )
    gaps = _uncovered(_QUICK_CONTROL, lines, collect_marker_sets, emptied)
    assert gaps, "the recipe's parallel leg selects nothing and the census saw no gap"
    narrowed = makefile.replace(_PARALLEL_SELECTOR, f"{_PARALLEL_SELECTOR} -k nothing_matches_this")
    with pytest.raises(AssertionError, match="narrows"):
        _uncovered(_QUICK_CONTROL, lines, collect_marker_sets, narrowed)


def test_the_census_refuses_a_line_it_cannot_read():
    """Only the ty and ruff lines run no pytest; any other `uv run` line is unknown."""
    makefile = (CHECKOUT_ROOT / "Makefile").read_text()
    assert _oracle_legs(["uv run ty check x.py", "uv run ruff check x.py"], makefile) == []
    with pytest.raises(AssertionError, match="does not know"):
        _oracle_legs(["uv run pytest tests/unit"], makefile)


def test_the_full_gate_is_pinned_literally():
    """Written out, not derived: dropping a line from the production constant reddens this."""
    assert gs.FULL_GATE == [
        "make coverage",
        "nox -s tests_hostless-3.14",
        "make check-python",
        "make docs",
        "make gate-fresh",
    ]
    full = _s([gs.FULL], files=["uv.lock"], unknown=["uv.lock"], full_commit=["no row: uv.lock"])
    assert gs.commands(full, tip=False, base="origin/main") == [
        "make check-breaking",
        "make coverage",
        "nox -s tests_hostless-3.14",
        "make check-python",
        "make docs",
        "make gate-fresh",
    ]


def test_the_full_gates_hostless_session_runs_on_the_canary():
    """A canary bump in noxfile.py renames the session: the oracle must follow it."""
    canary = _string_constants(ast.parse((CHECKOUT_ROOT / "noxfile.py").read_text()))[
        "CANARY_PYTHON"
    ]
    assert [c for c in gs.FULL_GATE if c.startswith("nox -s tests_hostless-")] == [
        f"nox -s tests_hostless-{canary}"
    ]


def test_no_special_tree_is_ever_handed_to_quick():
    """Both quick legs deselect the busybox, conformance and stability trees by marker (and
    browser items by `not browser`): a `quick` over such a tree runs and selects nothing, and
    two empty legs are red. Every tracked module, alone."""
    head = _head_paths()
    offenders = []
    for path in _tracked("tests"):
        if not gs._is_test_module(path):
            continue
        text = (CHECKOUT_ROOT / path).read_text(encoding="utf-8")
        scope = gs.classify([gs.Change(path, "M")], _at_head(path, text), "base", "head", head)
        quick_lines = [
            c
            for c in gs.commands(scope, tip=False, base="origin/main")
            if c.startswith("make quick PATHS")
        ]
        roots = {r for c in quick_lines for r in c[len('make quick PATHS="') : -1].split()}
        for glob, _a, _b in gs.SPECIAL_TREES:
            if gs._match(path, (glob,)) and any(_within(path, (r,)) for r in roots):
                offenders.append(f"{path} → {quick_lines}")
    assert offenders == [], "\n".join(offenders)


def test_the_browser_trees_route_by_source_marks_not_by_a_tree_row():
    """`test_marking.py` carries `browser` in source, so the mark route sends it to
    `make dashboard` and quick never sees it. `test_harness.py` is hostless: a plain leaf
    whose surviving package root is `tests/e2e/monitor` (the nearest enclosing root with a
    test module; the dashboard directory is below the package depth), where quick's
    `not browser` deselects the browser items."""
    head = _head_paths()
    browser_path = "tests/e2e/monitor/dashboard/test_marking.py"
    plain_path = "tests/e2e/monitor/dashboard/test_harness.py"
    texts = {p: (CHECKOUT_ROOT / p).read_text(encoding="utf-8") for p in (browser_path, plain_path)}

    def scope_of(path):
        return gs.classify(
            [gs.Change(path, "M")], _at_head(path, texts[path]), "base", "head", head
        )

    browser, plain = scope_of(browser_path), scope_of(plain_path)
    assert browser.special == ["make dashboard"]
    assert browser.quick_paths == []
    assert plain.special == []
    assert plain.quick_paths == ["tests/e2e/monitor"]


def test_a_scripts_path_gets_the_repo_root_on_its_ty_line_and_tests_get_none():
    base = "origin/main"
    mixed = _s(
        [gs.SRC_LEAF, gs.SCRIPTS, gs.TESTS_LEAF],
        quick=["tests/unit/cli"],
        packages=["cli"],
        files=["src/otto/cli/x.py", "scripts/gate_fresh.py", "tests/unit/cli/test_a.py"],
    )
    lines = gs.commands(mixed, tip=False, base=base)
    assert "uv run ty check src/otto/cli/x.py" in lines
    assert "uv run ty check --extra-search-path . scripts/gate_fresh.py" in lines
    assert not [line for line in lines if "ty check" in line and "tests/" in line]


def test_every_guard_family_is_reached_by_a_src_leaf_commit():
    """Each tree-wide guard rides `quick` (GUARD_RIDERS) or is family-marked (quick-guards)."""
    families = [
        "tests/unit/test_error_base.py",
        "tests/unit/test_declared_additions.py",
        "tests/unit/init/test_init_templates.py",
        "tests/unit/import_budget",
        "tests/unit/api_snapshot",
    ]
    scope = gs.classify(
        [gs.Change("src/otto/cli/expose.py", "M")],
        _at_head("src/otto/cli/expose.py", "x = 1\n"),
        "base",
        "head",
        _head_paths(),
    )
    lines = gs.commands(scope, tip=False, base="origin/main")
    quick = next(c for c in lines if c.startswith("make quick PATHS"))
    assert "make quick-guards" in lines
    unreached = []
    for family in families:
        target = CHECKOUT_ROOT / family
        modules = sorted(target.rglob("test_*.py")) if target.is_dir() else [target]
        for module in modules:
            rel = str(module.relative_to(CHECKOUT_ROOT))
            rides = (
                any(rel == r or rel.startswith(r + "/") for r in gs.GUARD_RIDERS)
                and family in quick
            )
            guarded = gs.carries_family_mark(module.read_text(encoding="utf-8"))
            if not (rides or guarded):
                unreached.append(rel)
    assert unreached == [], unreached


def test_main_classifies_the_commit_and_the_tip(tmp_path: Path, capsys, monkeypatch):
    repo = _repo(tmp_path)
    rc = _main(monkeypatch, repo, "--base", "origin/main")
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "this commit (HEAD^1..HEAD): src-leaf, tests-leaf" in out
    quick = [line for line in out.splitlines() if 'make quick PATHS="' in line]
    assert quick, out
    assert "tests/unit/cli" in quick[0], out
    assert "tests/e2e/cli" in quick[0], out
    assert "tip (" in out
    assert "docs, src-leaf, tests-leaf" in out
    assert "make coverage" in out.split("tip (")[1]


def test_main_refuses_a_dirty_tracked_tree(tmp_path: Path, capsys, monkeypatch):
    repo = _repo(tmp_path)
    (repo / "docs/a.md").write_text("dirty\n")
    assert _main(monkeypatch, repo) == 2
    assert "commit first" in capsys.readouterr().err


def test_main_fails_loud_without_the_base_or_a_repository(tmp_path: Path, capsys, monkeypatch):
    repo = _repo(tmp_path)
    assert _main(monkeypatch, repo, "--base", "origin/nowhere") == 1
    assert "origin/nowhere" in capsys.readouterr().err
    (tmp_path / "plain").mkdir()
    assert _main(monkeypatch, tmp_path / "plain") == 1
    assert "gate-scope:" in capsys.readouterr().err


def test_an_ambient_git_dir_cannot_reach_the_script(tmp_path: Path, capsys, monkeypatch):
    """Regression for the closed environment: a hostile GIT_DIR in the test process must
    not steer the script's own git at another repository. `main` is called directly with
    `env=git_env(repo)` and WITHOUT `_closed_environ`, so the hostile variables stay in
    os.environ: only the script's `env=` on each spawn keeps them out. `tree_state` takes
    no env (it legitimately inherits the ambient one), so it is stubbed clean."""
    repo = _repo(tmp_path)
    monkeypatch.setattr(gs, "tree_state", lambda _repo: types.SimpleNamespace(tracked_dirty=[]))
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "elsewhere" / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(tmp_path / "elsewhere"))
    rc = gs.main(["--repo", str(repo), "--base", "origin/main"], env=git_env(repo))
    out = capsys.readouterr()
    assert rc == 0, out.err
    assert "this commit (HEAD^1..HEAD): src-leaf, tests-leaf" in out.out


def test_the_commit_range_is_the_first_parent(tmp_path: Path, capsys, monkeypatch):
    repo = _repo(tmp_path)
    _git(repo, "checkout", "-q", "main")
    (repo / "docs/b.md").write_text("b\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "main moves")
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    _git(repo, "checkout", "-q", "feature")
    _git(repo, "merge", "-q", "--no-edit", "main")
    assert _main(monkeypatch, repo) == 0
    out = capsys.readouterr().out
    # the merge brought docs/b.md only; a wrong range (merge-base..HEAD, HEAD^2..HEAD) would
    # add src-leaf and tests-leaf, and the classes are sorted, so only the whole line pins it
    assert "this commit (HEAD^1..HEAD): docs \u2014 1 file(s) at head" in out.splitlines(), out


def test_a_head_at_the_merge_base_has_an_empty_tip(tmp_path: Path, capsys, monkeypatch):
    repo = _repo(tmp_path)
    _git(repo, "checkout", "-q", "main")
    assert _main(monkeypatch, repo, "--base", "main") == 0
    out = capsys.readouterr().out
    assert "tip: HEAD is the merge base with main — nothing to land" in out


def test_a_non_default_base_reaches_every_line_that_runs_check_breaking(
    tmp_path: Path, capsys, monkeypatch
):
    """Without origin/main, `--base main` must govern the nested check-breaking runs too:
    lint-arch and check-python reach it as a prerequisite, gate-fresh through its nested
    make. The default base adds nothing, so the common case's lines are unchanged."""
    repo = _repo(tmp_path)
    _git(repo, "update-ref", "-d", "refs/remotes/origin/main")
    assert _main(monkeypatch, repo, "--base", "main") == 0
    lines = [line.strip() for line in capsys.readouterr().out.splitlines()]
    ranged = [line for line in lines if line.split()[:2] in _RANGED_TARGET_LINES]
    assert {line.split()[1] for line in ranged} == set(gs.RANGE_TARGETS), lines
    assert all(line.endswith(" RANGE=main..HEAD") for line in ranged), ranged
    (tmp_path / "default").mkdir()
    default = _repo(tmp_path / "default")
    assert _main(monkeypatch, default) == 0
    assert "RANGE=" not in capsys.readouterr().out


_RANGED_TARGET_LINES = [
    ["make", "check-breaking"],
    ["make", "lint-arch"],
    ["make", "check-python"],
    ["make", "gate-fresh"],
]


def test_the_ranged_targets_are_the_ones_that_reach_check_breaking():
    """`gate-fresh` reaches lint-arch through scripts/gate_fresh.py's own make, which inherits
    a command-line RANGE through MAKEFLAGS; the rest reach check-breaking as a prerequisite."""
    makefile = (CHECKOUT_ROOT / "Makefile").read_text()
    emitted = {line.split()[1] for line in gs.FULL_GATE if line.startswith("make ")}
    emitted |= {"check-breaking", "lint-arch", "quick", "quick-guards", "nox-unit-repeat"}
    reaching = {t for t in emitted if "check-breaking" in make_reachable(t, makefile)}
    assert reaching | {"gate-fresh"} == set(gs.RANGE_TARGETS)
    assert "lint-arch" in GATED_TARGETS


def test_a_head_without_a_parent_is_refused(tmp_path: Path, capsys, monkeypatch):
    repo = tmp_path / "root"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "README.md").write_text("r\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "root")
    assert _main(monkeypatch, repo, "--base", "main") == 2
    assert "gate-scope: HEAD has no parent; nothing to classify per commit" in (
        capsys.readouterr().err
    )


def test_the_script_runs_as_a_file_from_the_repo_root(tmp_path: Path):
    """`python scripts/gate_scope.py` (what `uv run python …` in the Makefile executes) puts
    scripts/ first on sys.path; the script must establish the repo root itself to import
    scripts.gate_fresh. The interpreter is this venv's own (`sys.executable`, absolute) and
    the environment is the closed `git_env(repo)` verbatim — `uv` itself is not under test:
    with HOME confined it would rebuild its cache."""
    repo = _repo(tmp_path)
    completed = subprocess.run(
        [sys.executable, str(CHECKOUT_ROOT / "scripts/gate_scope.py"), "--repo", str(repo)],
        cwd=CHECKOUT_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
        env=git_env(repo),
    )
    assert completed.returncode == 0, completed.stderr
    assert "this commit (HEAD^1..HEAD)" in completed.stdout


def _real_reader_sources() -> "dict[tuple[str, str], str]":
    return {("head", p): (CHECKOUT_ROOT / p).read_text() for p in gs.MATRIX_READERS}


def test_a_measured_matrix_routes_its_readers_as_tests_leaf_changes():
    for path in gs.MEASURED_ROWS:
        assert gs.classify_path(path) == gs.TESTS_LEAF
    scope = _scope(
        [gs.Change("schemas/support_matrix.json", "M")],
        _real_reader_sources(),
        head_paths=set(gs.MATRIX_READERS) | {"schemas/support_matrix.json"},
    )
    assert scope.classes == [gs.TESTS_LEAF]
    family = [
        p for p in gs.MATRIX_READERS if gs.carries_family_mark((CHECKOUT_ROOT / p).read_text())
    ]
    assert scope.guard_files == family
    assert scope.quick_paths == [p for p in gs.MATRIX_READERS if p not in family]
    assert scope.full_at_commit == []
    assert scope.unknown == []
    tip = gs.commands(scope, tip=True, base="origin/main")
    assert tip[0] == "make check-breaking"
    assert "make quick-guards" in tip
    assert any(
        line.startswith('make quick PATHS="tests/unit/test_matrix_downgrades.py') for line in tip
    )
    assert "make gate-fresh" in tip
    assert "make coverage" not in tip


def test_a_measured_matrix_whose_readers_are_gone_is_a_full_gate():
    scope = _scope(
        [gs.Change("schemas/support_matrix.json", "M")],
        {},
        head_paths={"schemas/support_matrix.json"},
    )
    assert scope.quick_paths == []
    assert scope.guard_files == []
    assert any("no surviving reader" in reason for reason in scope.full_at_commit)


def test_the_matrix_reader_census_separates_readers_from_gate_pins():
    support = 'X = "schemas/support_matrix.json"\n'
    kmodcov = 'X = "schemas/kmodcov_matrix.json"\n'
    sources = {
        "tests/unit/test_new_reader.py": "import json\n" + kmodcov,
        "tests/unit/scripts/test_gate_pin.py": "from scripts import gate_scope\n" + support,
        "tests/unit/scripts/test_runner_pin.py": "from scripts.gate_tip import goals\n" + support,
        "tests/unit/scripts/test_both_pin.py": (
            "from scripts import gate_fresh, gate_scope as gs, gate_tip as gt\n" + support
        ),
        "tests/unit/scripts/test_dotted_pin.py": "import scripts.gate_tip\n" + kmodcov,
        "tests/unit/test_unparsable.py": 'def test_a(:\n    "schemas/support_matrix.json"\n',
        "tests/unit/test_unrelated.py": "def test_a(): pass\n",
        "tests/_fixtures/matrix.py": support,
    }
    assert gs.matrix_readers(sources) == [
        "tests/unit/test_new_reader.py",
        "tests/unit/test_unparsable.py",
    ]


def test_the_matrix_readers_row_is_every_reader_in_the_test_tree():
    """A new reader of either matrix must join the row, or the row under-gates it."""
    sources = {
        p: (CHECKOUT_ROOT / p).read_text()
        for p in _head_paths()
        if p.startswith("tests/") and p.endswith(".py")
    }
    assert gs.matrix_readers(sources) == gs.MATRIX_READERS


FAMILY_MODULE = (
    "import pytest\npytestmark = pytest.mark.interpreter_agnostic\n\n\ndef test_b(): pass\n"
)


def _tests_only_repo(tmp_path: Path) -> "tuple[Path, str, str]":
    """Build base, tip and HEAD commits; return (repo, base_sha, tip_sha).

    base: one unit module; tip: adds a plain second module; HEAD: the SAME module, now wholly
    interpreter_agnostic — so the oracle routes test_b.py to `make quick` when read at the tip and
    to `make quick-guards` when read at HEAD.
    """
    repo = tmp_path / "trepo"
    (repo / "tests/unit/cli").mkdir(parents=True)
    _git(repo, "init", "-q", "-b", "main")
    (repo / "tests/unit/cli/test_a.py").write_text("def test_a(): pass\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    base = _git(repo, "rev-parse", "HEAD").strip()
    (repo / "tests/unit/cli/test_b.py").write_text("def test_b(): pass\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "tip")
    tip = _git(repo, "rev-parse", "HEAD").strip()
    (repo / "tests/unit/cli/test_b.py").write_text(FAMILY_MODULE)
    (repo / "tests/unit/other").mkdir()
    (repo / "tests/unit/other/test_c.py").write_text("def test_c(): pass\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "after the tip: test_b joins the family, test_c appears elsewhere")
    return repo, base, tip


def test_head_tree_and_tip_commands_read_the_given_revision(tmp_path):
    repo, base, tip = _tests_only_repo(tmp_path)
    env = git_env(repo)
    assert "tests/unit/other/test_c.py" not in gs.head_tree(repo, env, tip)
    assert "tests/unit/other/test_c.py" in gs.head_tree(repo, env, "HEAD")
    # the oracle's tests-leaf tip shape: check-breaking, quick over the surviving roots, ruff per
    # module, lint-arch, quick-guards (every tests-leaf tip), gate-fresh — see commands()
    assert gs.tip_commands(repo, base, tip, env) == [
        f"make check-breaking RANGE={base}..HEAD",
        'make quick PATHS="tests/unit/cli"',
        "uv run ruff check tests/unit/cli/test_b.py",
        f"make lint-arch RANGE={base}..HEAD",
        "make quick-guards",
        f"make gate-fresh RANGE={base}..HEAD",
    ]
    # at HEAD, test_b.py is a family module (guards, no quick root of its own) and the only
    # quick root is test_c.py's package: reading sources at the tip is what keeps tests/unit/cli
    assert gs.tip_commands(repo, base, "HEAD", env) == [
        f"make check-breaking RANGE={base}..HEAD",
        'make quick PATHS="tests/unit/other"',
        "uv run ruff check tests/unit/cli/test_b.py",
        "uv run ruff check tests/unit/other/test_c.py",
        f"make lint-arch RANGE={base}..HEAD",
        "make quick-guards",
        f"make gate-fresh RANGE={base}..HEAD",
    ]


def test_tip_commands_read_the_tree_at_the_given_revision(tmp_path):
    """The tip's paths decide routing: a reader deleted after the tip still routes at the tip."""
    repo = tmp_path / "mrepo"
    reader = "tests/unit/test_support_matrix.py"
    matrix = "schemas/support_matrix.json"
    (repo / "tests/unit").mkdir(parents=True)
    (repo / "schemas").mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / reader).write_text("def test_a(): pass\n")
    (repo / matrix).write_text("{}\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    base = _git(repo, "rev-parse", "HEAD").strip()
    (repo / matrix).write_text('{"measured": 1}\n')
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "tip: the matrix is measured")
    tip = _git(repo, "rev-parse", "HEAD").strip()
    (repo / reader).unlink()
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "after the tip: the only reader goes")
    env = git_env(repo)
    # at the tip the reader exists: a tests-leaf route over it
    assert gs.tip_commands(repo, base, tip, env) == [
        f"make check-breaking RANGE={base}..HEAD",
        'make quick PATHS="tests/unit/test_support_matrix.py"',
        f"make lint-arch RANGE={base}..HEAD",
        "make quick-guards",
        f"make gate-fresh RANGE={base}..HEAD",
    ]
    # at HEAD it is gone: the measured matrix has no surviving reader, so the full gate
    assert gs.tip_commands(repo, base, "HEAD", env) == [
        f"make check-breaking RANGE={base}..HEAD",
        "make coverage",
        "nox -s tests_hostless-3.14",
        f"make check-python RANGE={base}..HEAD",
        "make docs",
        f"make gate-fresh RANGE={base}..HEAD",
    ]


def test_the_report_ends_with_the_gate_tip_remedy_only_when_something_lands():
    scope = _scope([gs.Change("docs/a.md", "M")])
    text = gs.render(
        scope,
        scope,
        base="origin/main",
        merge_base="abc",
        commit_range="a..b",
        tip_range="a..b",
        count=1,
    )
    assert text.rstrip().endswith("run them and stamp the tree: make gate-tip")
    text = gs.render(
        scope,
        None,
        base="origin/main",
        merge_base="abc",
        commit_range="a..b",
        tip_range="a..b",
        count=0,
    )
    assert "gate-tip" not in text


# --- a release's own non-code edits (#660) ---------------------------------

_PYPROJECT = (CHECKOUT_ROOT / "pyproject.toml").read_text()
_LOCK = (CHECKOUT_ROOT / "uv.lock").read_text()
_VERSION = gs._BUMP_CONFIG_VERSION.search(_PYPROJECT)[1]


def _bump(text: str, new: str = "99.0.0") -> str:
    """bump-my-version's own edit, spelled as its config spells it."""
    return text.replace(f'version = "{_VERSION}"', f'version = "{new}"')


def _bump_lock(text: str, new: str = "99.0.0") -> str:
    entry = 'name = "otto-sh"\nversion = "{}"'
    return text.replace(entry.format(_VERSION), entry.format(new))


def _one(path: str, base: str, head: str, status: str = "M"):
    return _scope([gs.Change(path, status)], {("base", path): base, ("head", path): head})


def test_a_version_only_pyproject_bump_needs_no_lane():
    bumped = _bump(_PYPROJECT)
    assert bumped.count('version = "99.0.0"') == 2, "the bump rewrites version and current_version"
    scope = _one("pyproject.toml", _PYPROJECT, bumped)
    assert scope.classes == [gs.NONE]
    assert scope.changed_paths == [], "a release edit must not trigger the build-file rider"
    assert gs.commands(scope, tip=True, base="origin/main") == ["make check-breaking"]
    assert gs.commands(scope, tip=False, base="origin/main") == ["make check-breaking"]


@pytest.mark.parametrize(
    "extra",
    [
        lambda t: t.replace("[tool.bumpversion]", "[tool.bumpversion]\nallow_dirty = true"),
        lambda t: t + "\n# a trailing comment\n",
        lambda t: t.replace('version = "99.0.0"', 'version = "99.0.1"', 1),
    ],
    ids=["tool-setting", "comment", "versions-disagree"],
)
def test_a_bump_with_any_other_pyproject_edit_is_classified_as_before(extra):
    scope = _one("pyproject.toml", _PYPROJECT, extra(_bump(_PYPROJECT)))
    assert gs.NONE not in scope.classes
    assert gs.ESCALATE in scope.classes
    assert "make coverage" in gs.commands(scope, tip=True, base="origin/main")


def test_a_pyproject_edit_that_moves_no_version_is_not_a_release_edit():
    scope = _one("pyproject.toml", _PYPROJECT, _PYPROJECT + "\n")
    assert gs.ESCALATE in scope.classes


def test_otto_s_own_lock_version_bump_needs_no_lane_but_any_other_lock_edit_does():
    assert _one("uv.lock", _LOCK, _bump_lock(_LOCK)).classes == [gs.NONE]
    moved_dep = _bump_lock(_LOCK).replace(
        'name = "rich"\nversion = "', 'name = "rich"\nversion = "0', 1
    )
    assert moved_dep != _bump_lock(_LOCK), "the fixture must really move a dependency"
    scope = _one("uv.lock", _LOCK, moved_dep)
    assert scope.classes == [gs.FULL]


def test_a_matrix_re_dated_and_nothing_else_needs_no_lane():
    base = json.dumps(
        {"format": 1, "cells": {"s": {"p": {"verdict": "measured-ok", "as_of": "2026-10-18"}}}}
    )
    redated = base.replace("2026-10-18", "2026-10-19")
    scope = _one("schemas/support_matrix.json", base, redated)
    assert scope.classes == [gs.NONE]
    changed_verdict = redated.replace("measured-ok", "measured-broken")
    scope = _scope(
        [gs.Change("schemas/support_matrix.json", "M")],
        {
            ("base", "schemas/support_matrix.json"): base,
            ("head", "schemas/support_matrix.json"): changed_verdict,
            **_real_reader_sources(),
        },
        head_paths=set(gs.MATRIX_READERS) | {"schemas/support_matrix.json"},
    )
    assert scope.classes == [gs.TESTS_LEAF], "a verdict change still runs the matrix readers"


@pytest.mark.parametrize(
    "as_of",
    [None, "not-a-date", {"d": "2026-10-19"}, "2026-10-19\n", "drop"],
    ids=["null", "not-a-date", "a-dict", "trailing-newline", "deleted"],
)
def test_a_matrix_whose_as_of_is_no_longer_a_date_is_not_a_release_edit(as_of):
    cell = {"verdict": "measured-ok", "as_of": "2026-10-18"}
    base = json.dumps({"cells": {"s": cell}})
    head_cell = {"verdict": "measured-ok"} if as_of == "drop" else {**cell, "as_of": as_of}
    assert not gs._redated_matrix(base, json.dumps({"cells": {"s": head_cell}}))


def test_a_version_that_spans_lines_is_not_a_release_edit():
    broken = _bump(_PYPROJECT, new="99.0.0\nX")
    assert not gs._bumped_pyproject(_PYPROJECT, broken)
    assert not gs._bumped_lock(_LOCK, _bump_lock(_LOCK, new="99.0.0\nX"))


def test_an_unparsable_matrix_is_not_a_release_edit():
    assert not gs._redated_matrix("{", "{")


def test_an_added_or_deleted_release_file_is_never_a_release_edit():
    for status in ("A", "D"):
        change = gs.Change("pyproject.toml", status)
        files = {
            ("base", "pyproject.toml"): _PYPROJECT,
            ("head", "pyproject.toml"): _bump(_PYPROJECT),
        }
        assert not gs.release_owned(change, _reader(files), "base", "head")


def test_a_whole_release_range_needs_only_live_lanes():
    """The 0.17.0 shape: two matrix re-dates and the bump commit with its CHANGELOG."""
    base_matrix = json.dumps({"cells": {"a": {"as_of": "2026-10-18"}}})
    head_matrix = base_matrix.replace("18", "19")
    files = {
        ("base", "pyproject.toml"): _PYPROJECT,
        ("head", "pyproject.toml"): _bump(_PYPROJECT),
        ("base", "uv.lock"): _LOCK,
        ("head", "uv.lock"): _bump_lock(_LOCK),
        ("base", "CHANGELOG.md"): "# Changelog\n",
        ("head", "CHANGELOG.md"): "# Changelog\n\n## [99.0.0]\n",
        ("base", "schemas/support_matrix.json"): base_matrix,
        ("head", "schemas/support_matrix.json"): head_matrix,
        ("base", "schemas/kmodcov_matrix.json"): base_matrix,
        ("head", "schemas/kmodcov_matrix.json"): head_matrix,
    }
    changes = [gs.Change(path, "M") for (rev, path) in files if rev == "head"]
    tip = gs.commands(_scope(changes, files), tip=True, base="origin/main")
    assert tip == ["make check-breaking", "make gate-fresh"]
    assert all(gt.is_live(line) for line in tip)


@pytest.mark.parametrize(
    "path",
    [
        "scripts/refresh_docs_captures.py",
        "scripts/check_docs_examples.py",
        "scripts/build_example_archives.py",
    ],
)
def test_a_docs_harness_script_also_runs_the_docs_lane(path):
    scope = _scope([gs.Change(path, "M")], {("head", path): "x = 1\n", ("base", path): "x = 0\n"})
    assert scope.classes == [gs.SCRIPTS]
    assert "make docs" in gs.commands(scope, tip=False, base="origin/main")


def test_an_example_project_change_runs_the_docs_lane_not_the_full_gate():
    path = "docs/examples/quickstart-shell/src/main.cpp"
    scope = _scope([gs.Change(path, "M")])
    assert scope.classes == [gs.DOCS]
    assert scope.full_at_commit == []
    assert gs.commands(scope, tip=False, base="origin/main") == [
        "make check-breaking",
        "make docs",
    ]


@pytest.mark.parametrize(
    "path",
    [
        "src/otto/host/userland.py",
        "docs/examples/getting-started/captures.toml",
        "docs/examples/getting-started/libs/x.py",
        "docs/examples/getting-started-inventory/.otto/settings.toml",
        "scripts/refresh_docs_captures.py",
    ],
)
def test_a_change_the_docs_captures_read_runs_their_drift_check_at_the_tip(path):
    """The captures record otto's output on the bed: any src or example change can move them."""
    scope = _scope([gs.Change(path, "M")], {("head", path): "x = 1\n", ("base", path): "x = 0\n"})
    assert "make docs-captures-check" in gs.commands(scope, tip=True, base="origin/main")
    assert "make docs-captures-check" not in gs.commands(scope, tip=False, base="origin/main")


def test_a_docs_page_change_does_not_run_the_captures_drift_check():
    scope = _scope([gs.Change("docs/configuration/settings.md", "M")])
    assert "make docs-captures-check" not in gs.commands(scope, tip=True, base="origin/main")
