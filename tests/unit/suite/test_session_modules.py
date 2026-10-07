"""Each otto pytest session leaves no module of the repo's tests behind.

pytest's default import mode serves a test module, a conftest or a helper
beside them from ``sys.modules`` when a later session in the same process
imports the same name: an edit in between goes unseen (and is recorded as
fresh), and a second repo whose file has the same name collides with the
first. So a session evicts what it imported from the test directories, by
its own record of those modules, and nothing else. Nothing here evicts
anything by hand between two sessions: that is the behaviour under test.
"""

import os
import sys
from pathlib import Path

import pytest

from otto.config.collected_tests import classify, read_table
from otto.suite import run_tests
from otto.suite.run import selected_tests

_ONE = "def test_one():\n    pass\n"
_TWO = _ONE + "\ndef test_two():\n    pass\n"


def _save(path: Path, text: str) -> None:
    """Rewrite *path* with a stat that surely moved (a later mtime), as an editor's save would."""
    before = path.stat()
    path.write_text(text)
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns + 2_000_000_000))


def _repo(name: str | None = None):
    import otto.bootstrap

    repos = otto.bootstrap.get_repos()
    return repos[0] if name is None else next(r for r in repos if r.name == name)


def _recorded(root: Path, rel: str) -> list[str]:
    table = read_table(_repo())
    assert table is not None
    return sorted(t.name for t in table.files[str(root / rel)].tests)


def test_a_run_after_an_edit_runs_and_records_the_new_test(sut_repo, tmp_path):
    root = sut_repo(files={"tests/test_a.py": _ONE})
    assert run_tests(["test_one"], output_dir=tmp_path / "o1").exit_code == 0
    _save(root / "tests/test_a.py", _TWO)

    result = run_tests(["test_two"], output_dir=tmp_path / "o2")

    assert result.exit_code == 0
    assert _recorded(root, "tests/test_a.py") == ["test_one", "test_two"]


def test_a_listing_after_an_edit_records_and_lists_the_new_test(sut_repo):
    root = sut_repo(files={"tests/test_a.py": _ONE})
    selected_tests([])
    _save(root / "tests/test_a.py", _TWO)

    narrowed = selected_tests(["test_one"])
    assert [t.name for t in narrowed.tests["sut"]] == ["test_one"]
    assert _recorded(root, "tests/test_a.py") == ["test_one", "test_two"]
    assert classify(_repo(), read_table(_repo())).is_current

    listed = selected_tests([])
    assert [t.name for t in listed.tests["sut"]] == ["test_one", "test_two"]


def test_a_file_that_held_no_test_is_read_again_once_it_does(sut_repo):
    """A module with no test yet leaves no test function to name it: its collection does."""
    root = sut_repo(files={"tests/test_a.py": _ONE, "tests/test_later.py": "X = 1\n"})
    selected_tests([])
    _save(root / "tests/test_later.py", "X = 1\n\ndef test_later():\n    pass\n")

    listed = selected_tests(["test_later"])

    assert [t.name for t in listed.tests["sut"]] == ["test_later"]


def test_a_helper_edited_between_two_sessions_is_read_again(sut_repo, tmp_path):
    root = sut_repo(
        files={
            "tests/helpers.py": "class Base:\n    def test_h1(self):\n        pass\n",
            "tests/test_x.py": "from helpers import Base\n\nclass TestX(Base):\n    pass\n",
        }
    )
    assert run_tests(["TestX"], output_dir=tmp_path / "o1").exit_code == 0
    _save(
        root / "tests/helpers.py",
        "class Base:\n    def test_h1(self):\n        pass\n    def test_h2(self):\n        pass\n",
    )

    listed = selected_tests(["TestX"])

    assert [t.name for t in listed.tests["sut"]] == ["test_h1", "test_h2"]
    # Read again as it is now, so recorded at the stat it has now.
    assert classify(_repo(), read_table(_repo())).is_current


@pytest.mark.parametrize("package", [False, True], ids=["rootless", "package"])
def test_two_repos_with_the_same_test_file_name_both_list_and_both_run(
    two_sut_repos, tmp_path, package
):
    """Rootless, both are module ``test_dev``.

    In a package, both are ``devpkg.test_dev``, under ``devpkg`` and beside
    a ``devpkg.conftest``. (Not a ``tests`` package: that name is this
    suite's own.)
    """
    where = "tests/devpkg" if package else "tests"
    files = {
        "a": {f"{where}/test_dev.py": "def test_in_a():\n    pass\n"},
        "b": {f"{where}/test_dev.py": "def test_in_b():\n    pass\n"},
    }
    if package:
        for repo_files in files.values():
            repo_files[f"{where}/__init__.py"] = ""
            repo_files[f"{where}/conftest.py"] = "import pytest\n"
    two_sut_repos(a=files["a"], b=files["b"])

    listed = selected_tests([])

    assert listed.exit_code == 0
    assert {repo: [t.name for t in tests] for repo, tests in listed.tests.items()} == {
        "repo_a": ["test_in_a"],
        "repo_b": ["test_in_b"],
    }
    result = run_tests(["test_in_a", "test_in_b"], output_dir=tmp_path / "out")
    assert result.exit_code == 0
    assert len(result.junit_paths) == 2


def _lib_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, files: dict[str, str]) -> Path:
    """A repo whose ``pylib`` is a library on ``sys.path``, wired as the only repo."""
    from otto.config.repo import Repo
    from tests._fixtures.sutrepo import make_sut_repo

    sut = make_sut_repo(
        tmp_path / "sut", name="sut", tests=["tests"], extra='libs = ["pylib"]\n', files=files
    )
    repo = Repo(sut_dir=sut)
    monkeypatch.setattr("otto.bootstrap.get_repos", lambda: [repo])
    monkeypatch.syspath_prepend(str(sut / "pylib"))
    return sut


def _tag(tmp_path: Path, stem: str) -> str:
    """A module name no other test in the process uses."""
    return f"{stem}{abs(hash(tmp_path)) % 10**8}"


def test_a_wrapped_function_names_the_module_its_wrapper_is_defined_in(
    sut_repo, tmp_path, monkeypatch
):
    """``functools.wraps`` copies ``__module__``, so it names the wrapped function's module.

    The wrapper lives in the test directory; the module it names does not,
    and stays imported.
    """
    tag = _tag(tmp_path, "foreign")
    (tmp_path / "elsewhere").mkdir()
    (tmp_path / "elsewhere" / f"{tag}.py").write_text("def greet():\n    return 1\n")
    monkeypatch.syspath_prepend(str(tmp_path / "elsewhere"))
    sut_repo(
        files={
            "tests/wrapping.py": (
                "import functools\n\n"
                "def logged(f):\n"
                "    @functools.wraps(f)\n"
                "    def w(*a, **k):\n"
                "        return f(*a, **k)\n"
                "    return w\n"
            ),
            "tests/test_w.py": (
                # Neither the module nor the plain function is a global here:
                # only the wrapper names the module.
                "import importlib\nfrom wrapping import logged\n\n"
                f"greet = logged(importlib.import_module({tag!r}).greet)\n\n"
                "def test_one():\n    assert greet() == 1\n"
            ),
        }
    )
    try:
        foreign = __import__(tag)

        assert selected_tests([]).exit_code == 0

        assert sys.modules.get(tag) is foreign
        assert "wrapping" not in sys.modules
    finally:
        sys.modules.pop(tag, None)


def test_a_library_edited_in_process_leaves_the_record_stale_until_a_new_process(
    tmp_path, monkeypatch
):
    """A library is imported once per process: this process cannot see the edit.

    So its record keeps the library's stat from before the edit, and a new
    process (its library imported again, nothing seen yet) collects the edit.
    """
    import otto.suite.plugin

    tag = _tag(tmp_path, "lib")
    base = "class LBase:\n    def test_x(self):\n        pass\n"
    sut = _lib_repo(
        tmp_path,
        monkeypatch,
        {
            f"pylib/{tag}.py": base,
            "tests/test_d.py": f"from {tag} import LBase\n\nclass TestD(LBase):\n    pass\n",
        },
    )
    try:
        selected_tests([])
        _save(sut / f"pylib/{tag}.py", base + "    def test_y(self):\n        pass\n")

        selected_tests([])

        assert _recorded(sut, "tests/test_d.py") == ["test_x"]
        assert not classify(_repo(), read_table(_repo())).is_current

        sys.modules.pop(tag, None)
        monkeypatch.setattr(otto.suite.plugin, "_LIB_DEP_STATS", {})
        listed = selected_tests([])

        assert [t.name for t in listed.tests["sut"]] == ["test_x", "test_y"]
        assert _recorded(sut, "tests/test_d.py") == ["test_x", "test_y"]
        assert classify(_repo(), read_table(_repo())).is_current
    finally:
        sys.modules.pop(tag, None)


@pytest.mark.usefixtures("_generated_modules_evicted")
def test_a_session_keeps_what_it_imported_from_outside_the_test_directories(tmp_path, monkeypatch):
    """A repo library stays imported.

    The bootstrap imports the libraries, and what their init modules
    register must stay the one class a test reaches.
    """
    from otto.config.repo import Repo
    from tests._fixtures.sutrepo import make_sut_repo

    tag = f"lib{abs(hash(tmp_path)) % 10**8}"
    sut = make_sut_repo(
        tmp_path / "sut",
        name="sut",
        tests=["tests"],
        extra='libs = ["pylib"]\n',
        files={
            f"pylib/{tag}.py": "class Shared:\n    pass\n",
            "tests/test_l.py": f"from {tag} import Shared\n\ndef test_l():\n    assert Shared\n",
        },
    )
    repo = Repo(sut_dir=sut)
    monkeypatch.setattr("otto.bootstrap.get_repos", lambda: [repo])
    monkeypatch.syspath_prepend(str(sut / "pylib"))
    try:
        assert run_tests(["test_l"], output_dir=tmp_path / "out").exit_code == 0
        assert tag in sys.modules
        assert "test_l" not in sys.modules
    finally:
        sys.modules.pop(tag, None)
