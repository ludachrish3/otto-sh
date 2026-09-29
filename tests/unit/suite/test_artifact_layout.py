"""Artifact directories mirror the pytest test ID (spec §5.3; Review Focus 5)."""

import pytest

from tests.unit.suite._inner import run_inner

pytest_plugins = ["pytester"]

_BODY = """
import pytest

def test_plain(module_dir, test_dir):
    assert module_dir.name == "test_lay"
    assert test_dir == module_dir / "test_plain"

class TestOuter:
    def test_in_class(self, module_dir, test_dir):
        assert test_dir == module_dir / "TestOuter" / "test_in_class"

    class TestInner:
        def test_nested(self, module_dir, test_dir):
            assert test_dir == module_dir / "TestOuter" / "TestInner" / "test_nested"

@pytest.mark.parametrize("v", ["a-b"])
def test_param(test_dir, v):
    assert test_dir.name == "test_param_a-b_"
"""


def test_layout_for_functions_classes_nested_classes_and_params(pytester, otto_plugins):
    run_inner(pytester, otto_plugins, test_lay=_BODY).assert_outcomes(passed=4)


def test_module_dir_is_truly_module_scoped(pytester, otto_plugins):
    """A module-scoped fixture can depend on ``module_dir`` with no ``ScopeMismatch``.

    Pins that ``module_dir`` really is ``scope="module"`` — pytest refuses a
    module-scoped fixture that depends on anything NARROWER (e.g. class- or
    function-scoped), raising ``ScopeMismatch`` at collection; this is red if
    ``module_dir`` ever regresses to a tighter scope.
    """
    body = """
import pytest

@pytest.fixture(scope="module")
def shared(module_dir):
    return module_dir

def test_a(shared):
    assert shared.name == "test_module_dir_scope_probe"

def test_b(shared):
    assert shared.name == "test_module_dir_scope_probe"
"""
    # A module name no other test file uses: in one process a plain
    # ``test_scope`` collides with tests/unit/config/test_scope.py's module.
    run_inner(pytester, otto_plugins, test_module_dir_scope_probe=body).assert_outcomes(passed=2)


def test_nothing_is_created_unless_requested(pytester, otto_plugins, otto_output_dir):
    result = run_inner(pytester, otto_plugins, test_quiet="def test_q():\n    pass\n")
    result.assert_outcomes(passed=1)
    assert not (otto_output_dir / "test_quiet").exists()


def test_stability_iterations_nest_under_test_dir(
    pytester, otto_plugins_iterations_2, otto_output_dir
):
    # OttoPlugin.pytest_runtest_protocol reports each iteration's call phase
    # separately (that is how the stability report's per-test pass rate is
    # built), so 2 iterations of one test show as 2 passed — not a Task 8
    # concern, just the pre-existing stability-mode reporting shape.
    run_inner(
        pytester,
        otto_plugins_iterations_2,
        test_it="def test_i(test_dir):\n    (test_dir / 'x').write_text('1')\n",
    ).assert_outcomes(passed=2)
    base = otto_output_dir / "test_it" / "test_i"
    assert (base / "iteration_1" / "x").exists()
    assert (base / "iteration_2" / "x").exists()


def test_iterations_create_no_dirs_when_test_dir_is_never_requested(
    pytester, otto_plugins_iterations_2, otto_output_dir
):
    """Stability mode keeps the create-on-request rule — red if the repeat loop mkdirs eagerly."""
    run_inner(
        pytester, otto_plugins_iterations_2, test_iter_quiet="def test_q():\n    pass\n"
    ).assert_outcomes(passed=2)
    assert not (otto_output_dir / "test_iter_quiet").exists()


def test_a_single_iteration_is_still_iteration_1(pytester, otto_plugins, otto_output_dir):
    """``--iterations 1`` is stability mode too: the artifacts are numbered."""
    from otto.suite.plugin import OttoPlugin

    otto_plugins[0] = OttoPlugin(iterations=1)
    run_inner(
        pytester,
        otto_plugins,
        test_once="def test_o(test_dir):\n    (test_dir / 'x').write_text('1')\n",
    ).assert_outcomes(passed=1)
    assert (otto_output_dir / "test_once" / "test_o" / "iteration_1" / "x").exists()


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("test_foo[router-True]", "test_foo_router-True_"),
        ("test/foo", "test_foo"),
        ('a[b]<c>d:e"f|g?h*i\\j/k', "a_b__c_d_e_f_g_h_i_j_k"),
        ("test_simple_name", "test_simple_name"),
        ("", ""),
        ("test_foo-bar_baz", "test_foo-bar_baz"),
    ],
)
def test_sanitize_node_name_replaces_only_filesystem_unsafe_characters(name, expected):
    from otto.suite.layout import sanitize_node_name

    assert sanitize_node_name(name) == expected


def test_module_dir_falls_back_to_the_stem_with_no_test_roots(tmp_path):
    """No ``test_roots`` at all keeps the pre-fix-round-1 flat ``<root>/<stem>`` shape."""
    from otto.suite.layout import ArtifactLayout

    layout = ArtifactLayout(root=tmp_path / "repo_a")
    assert layout.module_dir(tmp_path / "tests" / "test_x.py") == tmp_path / "repo_a" / "test_x"


# ── module_dir / test_roots (controller ruling, fix round 1) ─────────────────
#
# Two modules sharing a basename in different packaged test directories used
# to clobber each other's module_dir — both landed on <out>/test_same. These
# pin the fix: module_dir mirrors the module's path relative to the DEEPEST
# containing test_roots entry, suffix dropped.


def test_module_dir_disambiguates_same_stem_modules_in_different_packages(tmp_path):
    from otto.suite.layout import ArtifactLayout

    test_root = tmp_path / "tests"
    (test_root / "pa").mkdir(parents=True)
    (test_root / "pb").mkdir(parents=True)
    layout = ArtifactLayout(root=tmp_path / "out", test_roots=[test_root])
    assert layout.module_dir(test_root / "pa" / "test_same.py") == (
        tmp_path / "out" / "pa" / "test_same"
    )
    assert layout.module_dir(test_root / "pb" / "test_same.py") == (
        tmp_path / "out" / "pb" / "test_same"
    )


def test_module_dir_flat_module_keeps_the_stem(tmp_path):
    """A module directly IN the test root keeps the unchanged flat ``<root>/<stem>`` shape."""
    from otto.suite.layout import ArtifactLayout

    test_root = tmp_path / "tests"
    test_root.mkdir()
    layout = ArtifactLayout(root=tmp_path / "out", test_roots=[test_root])
    assert layout.module_dir(test_root / "test_x.py") == tmp_path / "out" / "test_x"


def test_module_dir_outside_every_test_root_falls_back_to_the_stem(tmp_path):
    from otto.suite.layout import ArtifactLayout

    test_root = tmp_path / "tests"
    test_root.mkdir()
    layout = ArtifactLayout(root=tmp_path / "out", test_roots=[test_root])
    assert layout.module_dir(tmp_path / "elsewhere" / "test_y.py") == tmp_path / "out" / "test_y"


def test_module_dir_nested_package_dirs(tmp_path):
    from otto.suite.layout import ArtifactLayout

    test_root = tmp_path / "tests"
    (test_root / "a" / "b").mkdir(parents=True)
    layout = ArtifactLayout(root=tmp_path / "out", test_roots=[test_root])
    assert layout.module_dir(test_root / "a" / "b" / "test_x.py") == (
        tmp_path / "out" / "a" / "b" / "test_x"
    )


def test_module_dir_picks_the_deepest_containing_test_root(tmp_path):
    """A test root nested inside another wins over its ancestor."""
    from otto.suite.layout import ArtifactLayout

    outer = tmp_path / "tests"
    inner = outer / "sub"
    inner.mkdir(parents=True)
    layout = ArtifactLayout(root=tmp_path / "out", test_roots=[outer, inner])
    # Relative to `inner` (the deepest match): just the stem, flat.
    assert layout.module_dir(inner / "test_x.py") == tmp_path / "out" / "test_x"


def test_module_dir_ignores_a_test_root_that_is_not_a_directory(tmp_path):
    """A configured test root that doesn't exist on disk never wins containment."""
    from otto.suite.layout import ArtifactLayout

    missing_root = tmp_path / "does_not_exist"
    layout = ArtifactLayout(root=tmp_path / "out", test_roots=[missing_root])
    assert layout.module_dir(missing_root / "test_x.py") == tmp_path / "out" / "test_x"


def test_same_stem_modules_end_to_end_via_test_dir(pytester, otto_output_dir):
    """The reviewer's pa/pb probe shape, through a real inner pytest session and the
    module_dir/test_dir fixtures — not just the ArtifactLayout unit tests above."""
    from otto.config.lab import Lab
    from otto.context import OttoContext, reset_context, set_context
    from otto.suite.layout import ArtifactLayout
    from otto.suite.plugin import OttoPlugin
    from otto.suite.pytest_plugin import OttoFixturesPlugin
    from tests.unit.suite._inner import INNER_ARGS

    for pkg in ("pa", "pb"):
        pkg_dir = pytester.path / pkg
        pkg_dir.mkdir()
        (pkg_dir / "__init__.py").write_text("")
        (pkg_dir / "test_same.py").write_text(
            "def test_one(test_dir):\n    (test_dir / 'f').write_text('" + pkg + "')\n"
        )
    layout = ArtifactLayout(root=otto_output_dir, test_roots=[pytester.path])
    plugins = [OttoPlugin(), OttoFixturesPlugin(layout=layout)]
    token = set_context(OttoContext(lab=Lab(name="_test_stub"), output_dir=otto_output_dir))
    try:
        result = pytester.runpytest_inprocess(*INNER_ARGS, plugins=plugins)
        result.assert_outcomes(passed=2)
    finally:
        reset_context(token)
    assert (otto_output_dir / "pa" / "test_same" / "test_one" / "f").read_text() == "pa"
    assert (otto_output_dir / "pb" / "test_same" / "test_one" / "f").read_text() == "pb"
