"""run_tests: the one library entry point (spec §5.4, §5.5, §5.6, §3 Order)."""

import pytest

from otto import options
from otto.params import OptionsRegistrationError, register_options
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


def test_no_random_runs_in_collection_order(sut_repo, tmp_path):
    repo = sut_repo(files={"tests/test_order.py": _ORDERED})
    result = run_tests(
        ["TestB", "TestA", "test_z_last"],
        run_options=RunOptions(random_order=False),
        output_dir=tmp_path / "out",
    )
    assert result.exit_code == 0
    assert (repo / "tests/order.json").read_text() == '["B2", "B1", "A2", "A1"]'


def test_a_class_name_selects_its_tests(sut_repo, tmp_path):
    sut_repo(
        files={
            "tests/test_c.py": (
                "class TestC:\n    def test_1(self): pass\n    def test_2(self): pass\n"
            )
        }
    )
    result = run_tests(["TestC"], output_dir=tmp_path / "out")
    assert result.exit_code == 0
    assert _passed_count(result) == 2


def test_unknown_names_suggest(sut_repo, tmp_path):
    sut_repo(files={"tests/test_c.py": "def test_reboot(): pass\n"})
    with pytest.raises(UnknownSelectionError, match=r"test_rebot.*did you mean: test_reboot"):
        run_tests(["test_rebot"], output_dir=tmp_path / "out")


def test_names_or_markers_are_required(tmp_path):
    with pytest.raises(ValueError, match=r"at least one test name or run_options\.markers"):
        run_tests(output_dir=tmp_path / "out")


def test_a_marker_that_matches_nothing_raises(sut_repo, tmp_path):
    sut_repo(files={"tests/test_m.py": "def test_m(): pass\n"})
    with pytest.raises(NoTestsMatchedError):
        run_tests(run_options=RunOptions(markers="nosuchmarker"), output_dir=tmp_path / "out")


def test_options_reach_ctx_options_and_unregistered_instances_refuse(sut_repo, tmp_path):
    @options
    class FirmwareOpts:
        firmware: str = "latest"

    register_options(FirmwareOpts, verbs=["test"])
    sut_repo(
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
        ["test_o"], options=[FirmwareOpts(firmware="2.1")], output_dir=tmp_path / "out"
    )
    assert result.exit_code == 0

    @options
    class Stray:
        x: int = 0

    with pytest.raises(OptionsRegistrationError, match="Stray is not registered"):
        run_tests(["test_o"], options=[Stray()], output_dir=tmp_path / "out2")


@pytest.mark.parametrize(
    ("names", "markers"), [(["test_ok"], ""), ([], "not slow")], ids=["by-name", "by-marker"]
)
def test_a_broken_nested_file_does_not_block_the_selected_test(
    sut_repo, tmp_path, caplog, names, markers
):
    """The selected test runs and the broken file is logged; the run exits 1, as pytest does.

    The first run of a repo collects the whole tree, so the broken file is
    reached, and so is it on every marker-only run;
    ``--continue-on-collection-errors`` keeps it from stopping the selected
    test, and pytest's verdict for a run with a collection error is 1.
    """
    sut_repo(
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
    result = run_tests(names, run_options=RunOptions(markers=markers), output_dir=tmp_path / "out")
    assert result.exit_code == 1
    assert _passed_count(result) == 1
    assert "test_broken.py" in caplog.text


@pytest.mark.parametrize("where", ["tests/test_reg.py", "tests/conftest.py"])
def test_registering_from_a_test_file_or_conftest_is_refused(sut_repo, tmp_path, where):
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
    sut_repo(files=files)
    with pytest.raises(RegistrationRefused, match="init module"):
        run_tests(["test_reg"], output_dir=tmp_path / "out")


def test_a_test_file_that_first_imports_an_otto_module_is_not_refused(sut_repo, tmp_path):
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

    sut_repo(
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
            assert run_tests(["test_first"], output_dir=tmp_path / "out").exit_code == 0
            origin = sys.modules["otto.host.binary_loader"].LOADER_CLASSES.origin("llext-hex")
            first_importer = sys.modules["otto.host.binary_loader"] is not original
    finally:
        gc.collect()
    assert first_importer, "the test file was not the first importer"
    assert origin == "otto.host.binary_loader"
    loaders = [t for t in reg.instances() if t.kind == original.LOADER_CLASSES.kind]
    assert loaders == [original.LOADER_CLASSES]


def test_two_repos_get_a_repo_layer(two_sut_repos, tmp_path):
    two_sut_repos(
        a={"tests/test_a.py": "def test_same(test_dir):\n    (test_dir / 'f').write_text('a')\n"},
        b={"tests/test_b.py": "def test_same(test_dir):\n    (test_dir / 'f').write_text('b')\n"},
    )
    out = tmp_path / "out"
    assert run_tests(["test_same"], output_dir=out).exit_code == 0
    assert (out / "repo_a/test_a/test_same/f").read_text() == "a"
    assert (out / "repo_b/test_b/test_same/f").read_text() == "b"
