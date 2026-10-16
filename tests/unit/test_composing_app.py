"""The public ``otto.app`` composes otto's repos, as the console script does.

``otto.app`` is the command tree under its public name (``otto.cli.app`` behind the
root alias). A program that drives it, through ``typer.testing.CliRunner`` or by
calling it, never passes through the console script's ``entry()``, which loads the
composition root first. A root-level eager option such as ``--list-labs``, and a
root option's completer, read the repos before the root command's body runs. Resolving
``app`` stays root-free; those callbacks and completers import the root when they are
called, so the read composes.

Each case runs in a fresh interpreter, as in ``tests/unit/test_composing_entry_points.py``:
in-process, a root conftest fixture or an earlier test may already have loaded the
root, which would hide a missing load.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

from tests._fixtures.paths import TESTS_ROOT

_REPO1 = TESTS_ROOT / "repo1"

_LIST_LABS = """
import json
import otto
from typer.testing import CliRunner

result = CliRunner().invoke(otto.app, ["--list-labs"])
print(json.dumps([result.exit_code, repr(result.exception), result.output]))
"""

_SELECTED_TESTS = """
import json
from otto.suite.run import selected_tests

try:
    selected_tests(["no_such_test"])
except Exception as exc:
    print(json.dumps([type(exc).__name__]))
"""

_COMPLETE_LAB = """
import json
import otto
from typer.testing import CliRunner

env = {"_OTTO_COMPLETE": "complete_bash", "COMP_WORDS": "otto --lab ", "COMP_CWORD": "2"}
result = CliRunner().invoke(otto.app, [], prog_name="otto", env=env)
print(json.dumps([result.exit_code, repr(result.exception), result.output]))
"""


def _fresh(tmp_path: Path, source: str) -> "list[object]":
    """Run *source* in a new interpreter over ``tests/repo1``; return its last JSON line."""
    env = {
        **os.environ,
        "OTTO_SUT_DIRS": str(_REPO1),
        "OTTO_HOME": str(tmp_path / "home"),
    }
    env.pop("PYTEST_ADDOPTS", None)
    out = subprocess.run(
        [sys.executable, "-c", source],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
        env=env,
        cwd=tmp_path,
    )
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_otto_app_list_labs_composes_and_prints_the_labs(tmp_path: Path) -> None:
    exit_code, exception, output = _fresh(tmp_path, _LIST_LABS)
    assert [exit_code, exception] == [0, "None"]
    assert isinstance(output, str)
    for lab in ["busybox", "embedded", "unix"]:
        assert lab in output


def test_otto_app_completes_the_root_lab_option_with_the_labs(tmp_path: Path) -> None:
    exit_code, exception, output = _fresh(tmp_path, _COMPLETE_LAB)
    assert [exit_code, exception] == [0, "None"]
    assert isinstance(output, str)
    assert output.split() == ["busybox", "embedded", "unix"]


def test_selected_tests_composes_when_called_with_no_run_installed(tmp_path: Path) -> None:
    """Called directly, it composes the repos and gets as far as the unknown name."""
    assert _fresh(tmp_path, _SELECTED_TESTS) == ["UnknownSelectionError"]
