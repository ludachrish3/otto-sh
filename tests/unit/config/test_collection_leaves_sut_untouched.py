"""Collecting a SUT's tests writes nothing into the SUT (#456).

An ``otto test`` TAB seeds or refreshes the collected-tests table by running
a pytest collection in a child process. On a SUT with no pytest config of its
own, that collection used to leave ``.pytest_cache/`` and ``__pycache__/`` behind in the user's
repo, from nothing more than pressing TAB, and the new directories moved the
mtime of a directory the completion cache keys on.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

from _pytest.assertion.rewrite import PYC_TAIL

from otto.config.completion_cache import COLLECTED_TESTS_KEY, DUMP_TESTS_ENV_VAR
from tests._fixtures.generated_repo import generate_repo

_CHILD = """
import contextlib
import sys

# Inside otto's pytest sessions, a real shell: no pycache prefix (tests/conftest.py
# exports PYTHONPYCACHEPREFIX to every child, so it is taken away here) and
# bytecode writing on. Outside the sessions bytecode writing is off: otto's
# own modules must write nothing (a __pycache__ under src/otto would fail the
# suite). What is imported before any session -- the bootstrap's init modules
# included -- is deliberately out of this test's scope: it checks what the
# collection writes, and a SUT's init modules are the user's to import.
sys.pycache_prefix = None
sys.dont_write_bytecode = True

import otto.suite.run as run

real_outside_the_repo = run._outside_the_repo


@contextlib.contextmanager
def writing_bytecode(**kwargs):
    sys.dont_write_bytecode = False
    try:
        with real_outside_the_repo(**kwargs) as args:
            yield args
    finally:
        sys.dont_write_bytecode = True


run._outside_the_repo = writing_bytecode
sys.argv = ["otto"]
from otto.cli.main import entry

entry()
"""


def _files(root: Path) -> list[str]:
    return sorted(str(p.relative_to(root)) for p in root.rglob("*"))


def test_the_tab_time_collection_leaves_the_sut_file_set_unchanged(tmp_path):
    repo = generate_repo(tmp_path, files=4, dirs=2)
    assert not list(repo.rglob("pytest.ini")), "the repo must have no pytest config of its own"
    before = _files(repo)
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith("OTTO_") and k != "PYTHONDONTWRITEBYTECODE"
    }
    env.update(OTTO_SUT_DIRS=str(repo), OTTO_HOME=str(tmp_path / "home"))
    env[DUMP_TESTS_ENV_VAR] = "1"

    child = subprocess.run(
        [sys.executable, "-c", _CHILD, str(repo)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )

    assert child.returncode == 0, child.stderr
    assert child.stdout == ""
    [cache] = (tmp_path / "home").rglob("completion_cache.json")
    table = json.loads(cache.read_text())[COLLECTED_TESTS_KEY][str(repo)]
    names = {name for record in table["files"].values() for _classes, name in record["tests"]}
    assert "test_x" in names, "the child collected nothing, so it proves nothing"
    assert _files(repo) == before
    [pycache] = (tmp_path / "home").glob("*/pycache")
    assert list(pycache.rglob(f"*{PYC_TAIL}")), "the collection wrote its bytecode, under OTTO_HOME"
