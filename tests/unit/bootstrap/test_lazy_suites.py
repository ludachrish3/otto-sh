"""Repo test files load on demand: only for the commands that read suites."""

import sys

import pytest

from otto import bootstrap as bs
from otto.suite.register import SUITES
from tests.unit.bootstrap.conftest import write_repo_with_test_body

GOOD = (
    "from otto.suite import OttoSuite\n\n"
    "class TestLazy(OttoSuite):\n"
    "    def test_x(self):\n"
    "        pass\n"
)


@pytest.fixture(autouse=True)
def _fresh():
    bs.invalidate()
    yield
    bs.invalidate()


def test_bootstrap_imports_no_test_file(tmp_path, monkeypatch):
    monkeypatch.setenv("OTTO_SUT_DIRS", write_repo_with_test_body(tmp_path, "lazy", GOOD))
    bs.bootstrap()
    assert not any(m.startswith("_otto_suite_") for m in sys.modules)


def test_the_first_suites_read_after_bootstrap_loads_them(tmp_path, monkeypatch):
    monkeypatch.setenv("OTTO_SUT_DIRS", write_repo_with_test_body(tmp_path, "lazy", GOOD))
    bs.bootstrap()
    assert "TestLazy" in SUITES


def test_find_suite_loads_lazily_after_bootstrap(tmp_path, monkeypatch):
    from otto.suite.run import find_suite

    monkeypatch.setenv("OTTO_SUT_DIRS", write_repo_with_test_body(tmp_path, "lazy", GOOD))
    with pytest.raises(LookupError):
        find_suite("TestLazy")  # before bootstrap: documented LookupError, no discovery
    bs.bootstrap()
    assert find_suite("TestLazy").__name__ == "TestLazy"


def test_a_broken_test_file_is_contained_once_and_retried_after_invalidate(tmp_path, monkeypatch):
    monkeypatch.setenv("OTTO_SUT_DIRS", write_repo_with_test_body(tmp_path, "broken", "def (:\n"))
    result = bs.bootstrap()
    assert result.errors == []
    SUITES.names()
    SUITES.names()
    assert len(result.errors) == 1
    assert "failed to load test_broken.py" in str(result.errors[0])
    bs.invalidate()
    again = bs.bootstrap()
    SUITES.names()
    assert len(again.errors) == 1, "a failed test module lingered in sys.modules and was skipped"


def test_an_uncontainable_escape_mid_load_leaves_the_load_retryable(tmp_path, monkeypatch):
    """A KeyboardInterrupt out of a test file must not mark the suites as loaded.

    It is not containable, so it escapes the load. An embedder that catches it
    and reads SUITES again must get a real load, not the early return a
    loaded-for marker left behind by the interrupted one would give.
    """
    from pathlib import Path

    repo = write_repo_with_test_body(tmp_path, "interrupted", "raise KeyboardInterrupt\n")
    monkeypatch.setenv("OTTO_SUT_DIRS", repo)
    bs.bootstrap()
    with pytest.raises(KeyboardInterrupt):
        SUITES.names()

    (Path(repo) / "tests" / "test_interrupted.py").write_text(
        GOOD.replace("TestLazy", "TestAfterInterrupt")
    )
    assert "TestAfterInterrupt" in SUITES.names(), "the interrupted load was never retried"


def test_a_test_file_that_registers_an_instruction_is_refused(tmp_path, monkeypatch):
    body = "from otto.cli.run import instruction\n\n@instruction()\nasync def sneaky():\n    pass\n"
    monkeypatch.setenv("OTTO_SUT_DIRS", write_repo_with_test_body(tmp_path, "sneaky", body))
    result = bs.bootstrap()
    SUITES.names()
    assert len(result.errors) == 1
    assert "init module" in str(result.errors[0])
    assert "test_sneaky.py" in str(result.errors[0])
    from otto.instructions import INSTRUCTIONS

    assert "sneaky" not in INSTRUCTIONS


def test_the_harness_registry_snapshot_never_loads_test_files(tmp_path, monkeypatch):
    """Every test's autouse snapshot/restore reads SUITES; it must not import a SUT's test files."""
    from tests import conftest as root

    sentinel = tmp_path / "imported"
    body = f"from pathlib import Path\nPath({str(sentinel)!r}).write_text('x')\n" + GOOD
    monkeypatch.setenv("OTTO_SUT_DIRS", write_repo_with_test_body(tmp_path, "sentinel", body))
    bs.bootstrap()
    snapshot = root._snapshot_registries()
    # `frozenset(sys.modules)` means "evict nothing": every module is pre-existing.
    root._restore_registries(snapshot, frozenset(sys.modules))
    assert not sentinel.exists(), "the registry snapshot/restore ran the suites loader"
    assert "TestLazy" in SUITES  # a real read still loads
    assert sentinel.exists()


def test_a_test_file_that_first_imports_an_otto_module_loads_cleanly(tmp_path, monkeypatch):
    """otto's own import-time registration is otto's, even when a test file triggers it.

    ``otto.host.llext_kind`` registers the ``llext`` product kind when imported,
    and SUT test files import it. Bootstrap already imports it, so the module is
    evicted to make the test file its FIRST importer: the registration then runs
    inside the suite-loading phase, and must not be refused as the test file's.
    """
    import otto.host as host_pkg
    from otto.host.product import PRODUCT_KINDS
    from otto.registry import suspend_loaders

    monkeypatch.setattr(host_pkg, "llext_kind", host_pkg.llext_kind)  # restored at teardown
    body = "import otto.host.llext_kind\n" + GOOD.replace("TestLazy", "TestLlextFirst")
    monkeypatch.setenv("OTTO_SUT_DIRS", write_repo_with_test_body(tmp_path, "llextfirst", body))
    result = bs.bootstrap()
    monkeypatch.delitem(sys.modules, "otto.host.llext_kind")
    with suspend_loaders():
        PRODUCT_KINDS.unregister("llext")  # the registry isolation fixture restores it
    assert "TestLlextFirst" in SUITES
    assert result.errors == []
    assert "otto.host.llext_kind" in sys.modules, "the test file was not the first importer"
    assert PRODUCT_KINDS.origin("llext") == "otto.host.llext_kind"


def test_a_suites_read_inside_a_leaf_still_prints_its_finding_once(tmp_path):
    """The catch-all render after ``app()``: a leaf that reads SUITES late still warns.

    ``app`` is replaced by a stand-in leaf that resolves suites the way a library
    call to ``find_suite`` would, after every other render site has passed. The
    argv is not root help, so no cache rebuild renders it first.
    """
    import os
    import subprocess

    from tests._fixtures.paths import PROJECT_ROOT

    script = (
        "import otto.cli.main as m\n"
        "from otto.suite.register import SUITES\n"
        "m.app = lambda: SUITES.names()\n"
        "m.entry()\n"
    )
    env = {k: v for k, v in os.environ.items() if not k.startswith("OTTO_")}
    env.update(
        OTTO_SUT_DIRS=str(PROJECT_ROOT / "tests" / "repo_broken"),
        OTTO_HOME=str(tmp_path / "home"),
    )
    p = subprocess.run(
        [sys.executable, "-c", script, "leaf"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert p.returncode == 0, p.stderr
    assert p.stderr.count("failed to load test_syntax_error.py") == 1, p.stderr


def test_find_suite_names_the_test_files_that_failed_to_load(tmp_path, monkeypatch):
    """A library caller gets no warning line, so the unknown-name error says why."""
    from otto.suite.run import find_suite

    monkeypatch.setenv(
        "OTTO_SUT_DIRS", write_repo_with_test_body(tmp_path, "brokenlib", "def (:\n")
    )
    with pytest.raises(LookupError) as before:
        find_suite("TestGone")
    assert "failed to load" not in str(before.value), "must not bootstrap to answer"
    bs.bootstrap()
    with pytest.raises(LookupError, match=r"some test files failed to load: test_brokenlib\.py"):
        find_suite("TestGone")


def test_find_suite_error_is_unchanged_when_nothing_failed(tmp_path, monkeypatch):
    from otto.suite.run import find_suite

    monkeypatch.setenv("OTTO_SUT_DIRS", write_repo_with_test_body(tmp_path, "lazy", GOOD))
    bs.bootstrap()
    with pytest.raises(LookupError) as excinfo:
        find_suite("TestGone")
    assert "failed to load" not in str(excinfo.value)


def test_an_init_typo_ending_in_py_is_not_reported_as_a_broken_test_file(tmp_path, monkeypatch):
    """``init = ["foo.py"]`` (a filename typo'd where a dotted module path
    belongs) must not be reported as "some test files failed to load".

    The failed import is an ordinary ``BootstrapError`` whose ``source`` is
    the misspelled ``init`` entry itself — it happens to end in ``.py`` too,
    which is exactly what would fool a ``source.endswith(".py")`` check. No
    test file was ever touched here, so ``is_test_file`` (set only by
    ``load_test_suites``) must stay false and keep this out of the clause.
    """
    from otto.suite.run import find_suite
    from tests._fixtures.sutrepo import make_sut_repo

    repo = make_sut_repo(tmp_path / "typo", name="typo", extra='init = ["not_a_real_module.py"]')
    monkeypatch.setenv("OTTO_SUT_DIRS", str(repo))
    bs.bootstrap()
    errs = bs.bootstrap().errors
    assert any(e.source == "not_a_real_module.py" and not e.is_test_file for e in errs), errs
    with pytest.raises(LookupError) as excinfo:
        find_suite("TestGone")
    assert "failed to load" not in str(excinfo.value)
