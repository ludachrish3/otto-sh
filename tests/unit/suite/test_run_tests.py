"""run_tests: the one library entry point (spec §5.4, §5.5, §5.6, §3 Order)."""

import contextlib

import pytest

from otto import options
from otto.params import register_options
from otto.registry import RegistrationRefused
from otto.suite import NoTestsMatchedError, RunOptions, UnknownSelectionError, run_tests
from otto.suite.run import SuiteRunResult


def _passed_count(result: SuiteRunResult) -> int:
    """How many test cases passed across the run's JUnit files.

    ``SuiteRunResult.passed`` is the run's verdict (a bool), not a count, so the
    number of tests a selection actually ran is read from the JUnit XML the
    sessions wrote.
    """
    import xml.etree.ElementTree as ET

    count = 0
    for path in result.junit_paths:
        for case in ET.parse(path).iter("testcase"):  # noqa: S314 — the JUnit file this run wrote
            if not any(child.tag in {"failure", "error", "skipped"} for child in case):
                count += 1
    return count


_ORDERED = """
ORDER = []
class TestB:
    def test_2(self): ORDER.append("B2")
    def test_1(self): ORDER.append("B1")
class TestA:
    def test_2(self): ORDER.append("A2")
    def test_1(self): ORDER.append("A1")
def test_z_last():
    import pathlib, json
    pathlib.Path(__file__).with_name("order.json").write_text(json.dumps(ORDER))
"""


def test_no_random_runs_in_collection_order(one_project, tmp_path):
    project = one_project(files={"tests/test_order.py": _ORDERED})
    result = run_tests(
        names=["TestB", "TestA", "test_z_last"],
        run_options=RunOptions(random_order=False),
        output_dir=tmp_path / "out",
    )
    assert result.exit_code == 0
    assert (project / "tests/order.json").read_text() == '["B2", "B1", "A2", "A1"]'


def test_a_class_name_selects_its_tests(one_project, tmp_path):
    one_project(
        files={
            "tests/test_c.py": (
                "class TestC:\n    def test_1(self): pass\n    def test_2(self): pass\n"
            )
        }
    )
    result = run_tests(names=["TestC"], output_dir=tmp_path / "out")
    assert result.exit_code == 0
    assert _passed_count(result) == 2


def test_unknown_names_suggest(one_project, tmp_path):
    one_project(files={"tests/test_c.py": "def test_reboot(): pass\n"})
    with pytest.raises(UnknownSelectionError, match=r"test_rebot.*did you mean: test_reboot"):
        run_tests(names=["test_rebot"], output_dir=tmp_path / "out")


def test_names_or_markers_are_required(tmp_path):
    with pytest.raises(ValueError, match=r"at least one test name or run_options\.markers"):
        run_tests(output_dir=tmp_path / "out")


def test_a_marker_that_matches_nothing_raises(one_project, tmp_path):
    one_project(files={"tests/test_m.py": "def test_m(): pass\n"})
    with pytest.raises(NoTestsMatchedError):
        run_tests(run_options=RunOptions(markers="nosuchmarker"), output_dir=tmp_path / "out")


def test_options_reach_ctx_options_and_any_options_class_is_accepted(one_project, tmp_path):
    @options
    class FirmwareOpts:
        firmware: str = "latest"

    register_options(FirmwareOpts, verbs=["test"])
    one_project(
        files={
            "tests/test_o.py": (
                "from otto.params import verb_option_classes\n"
                "def test_o(ctx):\n"
                "    (origin,) = verb_option_classes('test')\n"
                "    assert ctx.options(origin.cls).firmware == '2.1'\n"
            )
        }
    )
    result = run_tests(
        names=["test_o"], options=[FirmwareOpts(firmware="2.1")], output_dir=tmp_path / "out"
    )
    assert result.exit_code == 0

    @options
    class Stray:
        x: int = 0

    result = run_tests(
        names=["test_o"],
        options=[FirmwareOpts(firmware="2.1"), Stray()],
        output_dir=tmp_path / "out2",
    )
    assert result.exit_code == 0


@pytest.mark.parametrize(
    ("names", "markers"), [(["test_ok"], ""), ([], "not slow")], ids=["by-name", "by-marker"]
)
def test_a_broken_nested_file_does_not_block_the_selected_test(
    one_project, tmp_path, caplog, names, markers
):
    """The selected test runs and the broken file is logged; the run exits 1, as pytest does.

    The first run of a project collects the whole tree, so the broken file is
    reached, and so is it on every marker-only run;
    ``--continue-on-collection-errors`` keeps it from stopping the selected
    test, and pytest's verdict for a run with a collection error is 1.
    """
    one_project(
        files={
            "tests/sub/conftest.py": (
                "import pytest\n@pytest.fixture\ndef nested_fixture():\n    return 7\n"
            ),
            "tests/sub/test_ok.py": (
                "def test_ok(nested_fixture):\n    assert nested_fixture == 7\n"
            ),
            "tests/sub/test_broken.py": "def test_broken(:\n",
        }
    )
    result = run_tests(
        names=names, run_options=RunOptions(markers=markers), output_dir=tmp_path / "out"
    )
    assert result.exit_code == 1
    assert _passed_count(result) == 1
    assert "test_broken.py" in caplog.text


@pytest.mark.parametrize("where", ["tests/test_reg.py", "tests/conftest.py"])
def test_registering_from_a_test_file_or_conftest_is_refused(one_project, tmp_path, where):
    body = (
        "from otto import options, register_options\n"
        "@options\nclass Late:\n    x: int = 0\n"
        "register_options(Late, verbs=['test'])\n"
    )
    files = {where: body}
    if where.endswith("conftest.py"):
        files["tests/test_reg.py"] = "def test_reg(): pass\n"
    else:
        files["tests/test_reg.py"] = body + "def test_reg(): pass\n"
    one_project(files=files)
    with pytest.raises(RegistrationRefused, match="init module"):
        run_tests(names=["test_reg"], output_dir=tmp_path / "out")


def test_a_test_file_that_first_imports_an_otto_module_is_not_refused(one_project, tmp_path):
    """otto's own import-time registration is otto's, even when a test file triggers it.

    A registry's defining module registers its built-ins (by reference) when
    imported: ``otto.host.binary_loader`` registers ``llext-hex``, and a test
    file may import it. The module is evicted first so the test file is its
    FIRST importer: the registration then runs while test files load, and
    must not be refused as the test file's.

    THE SECOND COPY IS COLLECTED BEFORE THE TEST ENDS. The re-import builds a
    second ``LOADER_CLASSES``, and once the original module is put back that
    copy is unreachable but alive: a module's globals, functions and classes
    form reference cycles, so only the cycle collector frees them. Until it
    runs, the copy stays in :func:`otto.registry.instances`, and the next
    test's ``_isolate_registries`` snapshot holds it again -- where a guard
    that walks every table (``test_every_builtin_reference_resolves``) finds a
    loader table whose ``BinaryLoader`` is not the one ``llext-hex`` subclasses.
    """
    import gc
    import sys

    import otto.host as host_pkg
    import otto.host.binary_loader as original
    from otto import registry as reg

    one_project(
        files={"tests/test_first.py": "import otto.host.binary_loader\ndef test_first(): pass\n"}
    )
    # Its own patch context, so the original module is back in sys.modules and on
    # otto.host before the collect. Nothing then refers to the re-imported copy
    # from outside; its own reference cycles (globals, functions, classes) keep
    # it alive until the cycle collector runs.
    try:
        with pytest.MonkeyPatch.context() as modules:
            modules.setattr(host_pkg, "binary_loader", original)
            modules.delitem(sys.modules, "otto.host.binary_loader")
            assert run_tests(names=["test_first"], output_dir=tmp_path / "out").exit_code == 0
            origin = sys.modules["otto.host.binary_loader"].LOADER_CLASSES.origin(name="llext-hex")
            first_importer = sys.modules["otto.host.binary_loader"] is not original
    finally:
        gc.collect()
    assert first_importer, "the test file was not the first importer"
    assert origin == "otto.host.binary_loader"
    loaders = [t for t in reg.instances() if t.kind == original.LOADER_CLASSES.kind]
    assert loaders == [original.LOADER_CLASSES]


def test_two_projects_get_a_project_layer(two_projects, tmp_path):
    two_projects(
        a={"tests/test_a.py": "def test_same(test_dir):\n    (test_dir / 'f').write_text('a')\n"},
        b={"tests/test_b.py": "def test_same(test_dir):\n    (test_dir / 'f').write_text('b')\n"},
    )
    out = tmp_path / "out"
    assert run_tests(names=["test_same"], output_dir=out).exit_code == 0
    assert (out / "project_a/test_a/test_same/f").read_text() == "a"
    assert (out / "project_b/test_b/test_same/f").read_text() == "b"


def test_lab_less_context_carries_switches(tmp_path):
    """The sentinel context holds -I/-E and the output dir, and is not installed."""
    from otto.run_context import LIBRARY_LAB_NAME, try_get_context
    from otto.suite.run import lab_less_context

    ctx = lab_less_context(include=["a"], exclude=["b"], output_dir=tmp_path)
    assert ctx.lab.name == LIBRARY_LAB_NAME
    assert ctx.include_projects == ("a",)
    assert ctx.exclude_projects == ("b",)
    assert ctx.policy.output_dir == tmp_path
    assert try_get_context() is None


def _record_context(monkeypatch, *, fail=False):
    """Patch ``_collect_listing`` to record the context it sees; return the record list."""
    from otto.run_context import try_get_context
    from otto.suite import run as run_mod

    seen = []

    def fake(names, markers):
        seen.append(try_get_context())
        if fail:
            raise RuntimeError("boom")
        return "listing"

    monkeypatch.setattr(run_mod, "_collect_listing", fake)
    return seen


def test_selected_tests_borrows_a_lab_less_context_for_the_call(monkeypatch):
    from otto.run_context import LIBRARY_LAB_NAME, try_get_context
    from otto.suite.run import selected_tests

    seen = _record_context(monkeypatch)
    assert selected_tests([], include=["a"], exclude=["b"]) == "listing"
    (ctx,) = seen
    assert ctx.lab.name == LIBRARY_LAB_NAME
    assert ctx.include_projects == ("a",)
    assert ctx.exclude_projects == ("b",)
    assert try_get_context() is None


def test_selected_tests_unbinds_the_borrowed_context_when_collection_raises(monkeypatch):
    from otto.run_context import try_get_context
    from otto.suite.run import selected_tests

    _record_context(monkeypatch, fail=True)
    with pytest.raises(RuntimeError):
        selected_tests([], include=["a"])
    assert try_get_context() is None


def test_selected_tests_uses_the_installed_context_and_ignores_switches(monkeypatch):
    from otto.run_context import reset_context, set_context, try_get_context
    from otto.suite.run import lab_less_context, selected_tests

    installed = lab_less_context(include=["x"])
    token = set_context(installed)
    try:
        seen = _record_context(monkeypatch)
        selected_tests([], include=["a"], exclude=["b"])
        assert seen == [installed]
        assert installed.include_projects == ("x",)
        assert try_get_context() is installed
    finally:
        reset_context(token)


# -- participation: a run and a listing search only the projects that take part ------


NO_COV = RunOptions(cov=False)
"""Coverage off: the seeded verdicts over the empty sentinel lab would refuse the fleet walk."""


def _two(two_projects):
    return two_projects(
        a={"tests/test_a.py": "def test_a():\n    pass\n"},
        b={"tests/test_b.py": "def test_b():\n    pass\n"},
    )


@contextlib.contextmanager
def _scoped(*, scopes=None, include=(), exclude=(), lab=None):
    """Install a lab-less context holding *scopes* verdicts and the -I/-E switches."""
    from otto.run_context import reset_context, set_context
    from otto.suite.run import lab_less_context
    from tests._fixtures.bootstrap_seam import seed_scope_verdicts

    ctx = lab_less_context(include=include, exclude=exclude)
    seed_scope_verdicts(ctx, scopes or {})
    token = set_context(ctx)
    try:
        yield ctx
    finally:
        reset_context(token)


def _out_of_scope(name):
    from tests._fixtures.scoping import verdict

    return {name: verdict(name, excluded=True)}


def _read_known_spy(monkeypatch):
    from otto.suite import run as run_mod

    seen = []
    real = run_mod._read_known

    def spy(projects, home):
        seen.extend(p.name for p in projects)
        return real(projects, home)

    monkeypatch.setattr(run_mod, "_read_known", spy)
    return seen


def test_inactive_project_tests_not_collected(two_projects, monkeypatch):
    from otto.suite.run import selected_tests

    _two(two_projects)
    seen = _read_known_spy(monkeypatch)
    with _scoped(scopes=_out_of_scope("project_b")):
        listing = selected_tests([])
    assert seen == ["project_a"]
    assert set(listing.tests) == {"project_a"}
    assert listing.projects == ["project_a"]
    (line,) = listing.not_searched
    assert "project project_b was not searched" in line
    assert "-I project_b" in line


def test_exclude_drops_tests(two_projects):
    from otto.suite.run import selected_tests

    _two(two_projects)
    listing = selected_tests([], exclude=["project_b"])
    assert set(listing.tests) == {"project_a"}
    (line,) = listing.not_searched
    assert "-E project_b" in line


def test_include_restores_out_of_scope_tests(two_projects):
    from otto.suite.run import selected_tests

    _two(two_projects)
    with _scoped(scopes=_out_of_scope("project_b"), include=("project_b",)):
        listing = selected_tests([])
    assert set(listing.tests) == {"project_a", "project_b"}
    assert listing.not_searched == []


@contextlib.contextmanager
def _real_merged_lab(tmp_path, *, patterns):
    """Install a context over a real ``a+b`` lab; ``patterns`` maps project name to lab_patterns.

    The projects are real (settings parsed, ``lab_patterns`` declared, ``None`` for
    none) and the verdicts come from the real resolver (``scopes_of``), not a seed.
    """
    from otto.config.project import Project
    from otto.run_context import OttoContext, reset_context, set_context
    from tests._fixtures.bootstrap_seam import fake_bootstrap_result
    from tests._fixtures.fleet import _lab, _toml_list
    from tests._fixtures.project_scaffold import make_project

    projects = []
    for name, labs in patterns.items():
        extra = "" if labs is None else f"[project]\nlab_patterns = {_toml_list(labs)}\n"
        root = make_project(
            tmp_path / name,
            name=name,
            tests=["tests"],
            extra=extra,
            files={f"tests/test_{name}.py": "def test_same():\n    pass\n"},
        )
        projects.append(Project(root=root))
    lab = _lab(("e1", "a"), ("e2", "b"))
    ctx = OttoContext(lab=lab, bootstrap=fake_bootstrap_result(projects))
    token = set_context(ctx)
    try:
        yield ctx
    finally:
        reset_context(token)


@pytest.mark.usefixtures("_generated_modules_evicted")
def test_merged_lab_lab_tier_runs_project(tmp_path):
    """Review Focus 1: ``lab_patterns=["a"]`` takes part under the merged ``a+b`` lab."""
    from otto.config.scope import participating
    from otto.suite.run import selected_tests

    with _real_merged_lab(tmp_path, patterns={"in_a": ["a"], "elsewhere": ["c"]}) as ctx:
        assert ctx.lab.name == "a+b"
        assert participating(ctx.projects, ctx=ctx).tier == "lab"
        listing = selected_tests(["test_same"])
        result = run_tests(names=["test_same"], run_options=NO_COV, output_dir=tmp_path / "o")
    assert set(listing.tests) == {"in_a"}
    assert listing.projects == ["in_a"]
    assert result.exit_code == 0
    assert [p.name for p in result.junit_paths] == ["junit_in_a.xml"]


@pytest.mark.usefixtures("_generated_modules_evicted")
def test_undeclared_project_always_searched(tmp_path):
    """Review Focus 4: an undeclared project is listed and run beside a dropped declared one."""
    from otto.suite.run import selected_tests

    with _real_merged_lab(tmp_path, patterns={"plain": None, "declared": ["c"]}):
        listing = selected_tests(["test_same"])
        result = run_tests(names=["test_same"], run_options=NO_COV, output_dir=tmp_path / "o")
    assert set(listing.tests) == {"plain"}
    assert len(listing.not_searched) == 1
    assert "project declared was not searched" in listing.not_searched[0]
    assert [p.name for p in result.junit_paths] == ["junit_plain.xml"]


@pytest.mark.usefixtures("_generated_modules_evicted")
def test_run_tests_not_searched_lines_survive(tmp_path, caplog):
    """A run still names each project that does not take part, whatever ``ctx.projects`` holds.

    The run judges its layout and its skip lines over every parsed project
    (``ctx.unscoped``), so narrowing ``ctx.projects`` to the participants loses
    neither the per-project layout nor the ``not searched`` line.
    """
    import logging

    with _real_merged_lab(tmp_path, patterns={"in_a": ["a"], "elsewhere": ["c"]}):
        with caplog.at_level(logging.WARNING):
            result = run_tests(names=["test_same"], run_options=NO_COV, output_dir=tmp_path / "o")
        said = [
            r.getMessage()
            for r in caplog.records
            if r.levelno == logging.WARNING and "elsewhere" in r.getMessage()
        ]
        with pytest.raises(UnknownSelectionError) as caught:
            run_tests(names=["test_absent"], run_options=NO_COV, output_dir=tmp_path / "p")
    assert [p.name for p in result.junit_paths] == ["junit_in_a.xml"]
    assert len(said) == 1
    assert "skipping it for test" in said[0]
    (line,) = caught.value.not_searched
    assert "project elsewhere was not searched" in line


def test_host_starved_project_not_searched(two_projects):
    from otto.suite.run import selected_tests
    from tests._fixtures.scoping import verdict

    _two(two_projects)
    with _scoped(scopes={"project_b": verdict("project_b", universe=())}):
        listing = selected_tests([])
    assert set(listing.tests) == {"project_a"}
    (line,) = listing.not_searched
    assert "host_patterns" in line


def test_multi_layout_stable_when_one_project_dropped(two_projects, tmp_path):
    _two(two_projects)
    out = tmp_path / "out"
    with _scoped(scopes=_out_of_scope("project_b")):
        result = run_tests(names=["test_a"], run_options=NO_COV, output_dir=out)
    assert result.exit_code == 0
    assert [p.name for p in result.junit_paths] == ["junit_project_a.xml"]


@pytest.mark.parametrize("cov", [True, None, False])
def test_all_inactive_raises_before_coverage(two_projects, tmp_path, monkeypatch, cov):
    from otto.suite import run as run_mod

    _two(two_projects)

    def boom(*a, **k):
        raise AssertionError("resolve_coverage ran")

    monkeypatch.setattr(run_mod, "resolve_coverage", boom)
    scopes = {**_out_of_scope("project_a"), **_out_of_scope("project_b")}
    with _scoped(scopes=scopes), pytest.raises(NoTestsMatchedError) as caught:
        run_tests(names=["test_a"], run_options=RunOptions(cov=cov), output_dir=tmp_path / "o")
    assert len(caught.value.not_searched) == 2
    text = str(caught.value)
    assert text.startswith("No tests matched the selection.\n")
    assert "project project_a was not searched" in text
    assert "project project_b was not searched" in text


def test_unknown_name_lists_not_searched_projects(two_projects, tmp_path):
    _two(two_projects)
    with _scoped(scopes=_out_of_scope("project_b")), pytest.raises(UnknownSelectionError) as caught:
        run_tests(names=["test_b"], run_options=NO_COV, output_dir=tmp_path / "o")
    text = str(caught.value)
    assert text.startswith("no collected test matches: 'test_b'")
    assert "\nproject project_b was not searched" in text
    assert len(caught.value.not_searched) == 1
    assert caught.value.param_hint == "NAMES"


def test_did_you_mean_suggests_only_searched_projects(two_projects, tmp_path):
    _two(two_projects)
    with _scoped(scopes=_out_of_scope("project_b")), pytest.raises(UnknownSelectionError) as caught:
        run_tests(names=["test_bb"], run_options=NO_COV, output_dir=tmp_path / "o")
    first = str(caught.value).splitlines()[0]
    assert "did you mean: " in first, first
    suggested = first.split("did you mean: ", 1)[1].split("?)", 1)[0].split(", ")
    assert "test_a" in suggested  # the searched project's test is offered
    assert "test_b" not in suggested  # the left-out project's is not


def test_run_announces_skip_listing_does_not(two_projects, tmp_path, caplog):
    import logging

    from otto.suite.run import selected_tests

    _two(two_projects)
    with _scoped(scopes=_out_of_scope("project_b")):
        with caplog.at_level(logging.WARNING):
            selected_tests([])
        assert not [r for r in caplog.records if "project_b" in r.getMessage()]
        run_tests(names=["test_a"], run_options=NO_COV, output_dir=tmp_path / "o")
    said = [
        r.getMessage()
        for r in caplog.records
        if r.levelno == logging.WARNING and "project_b" in r.getMessage()
    ]
    assert len(said) == 1
    assert "skipping it for test" in said[0]


def test_listing_projects_include_participant_without_tests(two_projects):
    from otto.suite.run import selected_tests

    two_projects(
        a={"tests/test_a.py": "def test_a():\n    pass\n"},
        b={"README": "no tests directory here\n"},
    )
    listing = selected_tests([])
    assert listing.projects == ["project_a", "project_b"]
    assert set(listing.tests) == {"project_a"}


def test_coverage_config_reads_every_project(two_projects, tmp_path, monkeypatch):
    from otto.suite import run as run_mod

    _two(two_projects)
    given = []

    def spy(opts, projects, *, command):
        given.append([p.name for p in projects])
        return opts

    monkeypatch.setattr(run_mod, "resolve_coverage", spy)
    with _scoped(scopes=_out_of_scope("project_b")):
        run_tests(names=["test_a"], output_dir=tmp_path / "o")
    assert given == [["project_a", "project_b"]]
