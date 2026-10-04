"""A named run executes every test pytest collects for its names, warm cache or cold (#592).

The test-names cache serves tab completion. It must never narrow what a run
collects: a test whose existence changed without any Python file the cache
watches changing (a JSON file, an environment variable, a plain value
imported from another module, a new same-named test in another repo) must
run on the next warm run, exactly as native pytest would run it.

Every scenario primes the cache with an unfiltered ``--list-tests``, changes
only what the cache cannot see, then runs warm and compares the tests otto
executed (its JUnit) with what native ``pytest -k`` executes on the same
files.
"""

import os
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from tests._fixtures.sutrepo import make_sut_repo
from tests.e2e._otto_subprocess import run_otto
from tests.e2e._selection_fixtures import LAB_DATA_DIR, SETTINGS_EXTRA

pytestmark = pytest.mark.hostless

_STATIC = """\
def test_probe():
    assert True
"""

_FROM_JSON = """\
import json
from pathlib import Path

if json.loads((Path(__file__).parent.parent / "enabled.json").read_text()):
    def {name}():
        assert False, "generated test must run"
"""

_FROM_ENV = """\
import os

if os.environ.get("OTTO_592_FLAG"):
    def test_probe():
        assert False, "generated test must run"
"""

_FROM_PLAIN_VALUE = """\
from cfg import FLAG

if FLAG:
    def test_probe():
        assert False, "generated test must run"
"""


def _repo(root: Path, name: str, files: dict[str, str], *, with_lab: bool = True) -> Path:
    extra = SETTINGS_EXTRA.format(lab_data_dir=LAB_DATA_DIR) if with_lab else ""
    files = {"enabled.json": "false", **files}
    return make_sut_repo(root / name, name=name, tests=["tests"], extra=extra, files=files)


def _executed(junit_files: list[Path]) -> set[tuple[str, str]]:
    """(module, test) pairs a run's JUnit files say executed."""
    executed = set()
    for junit in junit_files:
        root = ET.parse(junit).getroot()  # noqa: S314 — written by our own subprocess
        for case in root.iter("testcase"):
            module = case.get("classname", "").rsplit(".", 1)[-1]
            executed.add((module, case.get("name", "")))
    return executed


def _otto_run(
    xdir: Path, repos: list[Path], name: str, extra_env: dict[str, str] | None = None
) -> tuple[int, set[tuple[str, str]]]:
    env = {"OTTO_SUT_DIRS": os.pathsep.join(str(r) for r in repos), **(extra_env or {})}
    r = run_otto(["test", name, "--no-cov", "--no-random"], xdir=xdir, lab="unix", extra_env=env)
    runs = sorted(p for p in (xdir / "test").iterdir() if p.is_dir())
    return r.returncode, _executed(sorted(runs[-1].rglob("junit*.xml")))


def _prime(xdir: Path, repos: list[Path], extra_env: dict[str, str] | None = None) -> None:
    env = {"OTTO_SUT_DIRS": os.pathsep.join(str(r) for r in repos), **(extra_env or {})}
    r = run_otto(["test", "--list-tests"], xdir=xdir, extra_env=env)
    assert r.returncode == 0, r.stdout + r.stderr


def _native(
    repos: list[Path], name: str, tmp_path: Path, extra_env: dict[str, str] | None = None
) -> tuple[int, set[tuple[str, str]]]:
    """What plain pytest executes for ``-k name`` over each repo's tests."""
    rc, executed = 0, set()
    for repo in repos:
        junit = tmp_path / f"native-{repo.name}.xml"
        env = {k: v for k, v in os.environ.items() if not k.startswith(("PYTEST_", "COV"))}
        r = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "tests",
                "-k",
                name,
                f"--junitxml={junit}",
                "-p",
                "no:randomly",
                "-p",
                "no:cacheprovider",
                "-p",
                "no:tach",
                "-q",
            ],
            cwd=repo,
            env={**env, "PYTHONDONTWRITEBYTECODE": "1", **(extra_env or {})},
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        rc = max(rc, r.returncode)
        executed |= _executed([junit])
    return rc, executed


def _xdir(tmp_path: Path) -> Path:
    xdir = tmp_path / "xdir"
    xdir.mkdir()
    return xdir


def test_a_same_named_test_external_data_adds_runs_warm(tmp_path: Path) -> None:
    """The issue's reproduction: JSON turns on a failing test_probe beside a passing one."""
    repo = _repo(
        tmp_path,
        "sut",
        {
            "tests/test_static.py": _STATIC,
            "tests/test_generated.py": _FROM_JSON.format(name="test_probe"),
        },
    )
    xdir = _xdir(tmp_path)
    _prime(xdir, [repo])
    (repo / "enabled.json").write_text("true")

    rc, executed = _otto_run(xdir, [repo], "test_probe")
    native_rc, native = _native([repo], "test_probe", tmp_path)

    assert native == {("test_static", "test_probe"), ("test_generated", "test_probe")}
    assert (rc, executed) == (native_rc, native) == (1, native)


def test_a_same_named_test_in_another_repo_runs_warm(tmp_path: Path) -> None:
    """A name the cache places in one repo still runs where another repo now holds it."""
    repo_a = _repo(tmp_path, "repoA", {"tests/test_static_a.py": _STATIC})
    repo_b = _repo(
        tmp_path,
        "repoB",
        {"tests/test_generated_b.py": _FROM_JSON.format(name="test_probe")},
        with_lab=False,
    )
    xdir = _xdir(tmp_path)
    _prime(xdir, [repo_a, repo_b])
    (repo_b / "enabled.json").write_text("true")

    rc, executed = _otto_run(xdir, [repo_a, repo_b], "test_probe")

    assert executed == {("test_static_a", "test_probe"), ("test_generated_b", "test_probe")}
    assert rc == 1


def test_a_name_the_cache_never_saw_runs_where_external_data_generates_it(
    tmp_path: Path,
) -> None:
    """A generated name no fresh record holds is found, not refused as unknown."""
    repo = _repo(
        tmp_path,
        "sut",
        {
            "tests/test_static.py": _STATIC,
            "tests/test_generated.py": _FROM_JSON.format(name="test_brand_new"),
        },
    )
    xdir = _xdir(tmp_path)
    _prime(xdir, [repo])
    (repo / "enabled.json").write_text("true")

    rc, executed = _otto_run(xdir, [repo], "test_brand_new")

    assert executed == {("test_generated", "test_brand_new")}
    assert rc == 1


def test_an_environment_driven_test_runs_warm(tmp_path: Path) -> None:
    """A test an environment variable turns on runs once it is set, as in native pytest."""
    repo = _repo(
        tmp_path,
        "sut",
        {"tests/test_static.py": _STATIC, "tests/test_generated.py": _FROM_ENV},
    )
    xdir = _xdir(tmp_path)
    _prime(xdir, [repo])
    flag = {"OTTO_592_FLAG": "1"}

    rc, executed = _otto_run(xdir, [repo], "test_probe", extra_env=flag)
    native_rc, native = _native([repo], "test_probe", tmp_path, extra_env=flag)

    assert native == {("test_static", "test_probe"), ("test_generated", "test_probe")}
    assert (rc, executed) == (native_rc, native)


def test_a_test_a_plain_imported_value_turns_on_runs_warm(tmp_path: Path) -> None:
    """``from cfg import FLAG``: editing cfg turns on a test in an unchanged test file."""
    repo = _repo(
        tmp_path,
        "sut",
        {
            "tests/test_static.py": _STATIC,
            "tests/test_generated.py": _FROM_PLAIN_VALUE,
            "tests/cfg.py": "FLAG = False\n",
        },
    )
    xdir = _xdir(tmp_path)
    _prime(xdir, [repo])
    (repo / "tests" / "cfg.py").write_text("FLAG = True\n")

    rc, executed = _otto_run(xdir, [repo], "test_probe")
    native_rc, native = _native([repo], "test_probe", tmp_path)

    assert native == {("test_static", "test_probe"), ("test_generated", "test_probe")}
    assert (rc, executed) == (native_rc, native)
