"""End-to-end test for the ``bootstrap()``/``run_tests()`` library path.

Drives a plain Python script — no ``otto`` CLI involved at all — that mirrors
the library walkthrough in ``docs/cookbook/python-library.md``:
``bootstrap()``, then ``run_tests()`` by name with an instance of a class
registered for ``test``. This is the flow
that caught the extraction's only shipped bug (the library run assuming a
CLI-installed context was already active); this test regresses it directly,
with the CLI layer entirely out of the picture.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from tests.e2e._otto_subprocess import PROJECT_ROOT, REPO_E2E, otto_subprocess_env

pytestmark = pytest.mark.hostless

# Script run via `sys.executable <path>` (not `otto`): bootstrap the
# composition root, run the fixture class by name (as a config-driven caller
# would), and report back everything the parent test needs as one JSON line
# on stdout.
_SCRIPT = """
import json
import os
from pathlib import Path

from otto.bootstrap import bootstrap
from otto.context import try_get_context
from otto.suite import run_tests

output_dir = Path(os.environ["OTTO_TEST_OUTPUT_DIR"])

bootstrap()
# Importable once bootstrap() has put the repo's libs on sys.path.
from repo_e2e_instructions.options import E2EFixtureOptions

r = run_tests(
    ["TestE2EFixture"],
    options=[E2EFixtureOptions(label="library")],
    output_dir=output_dir,
)

print(json.dumps({
    "passed": r.passed,
    "exit_code": r.exit_code,
    "junit_exists": r.junit_paths[0].exists(),
    "context_none_after": try_get_context() is None,
}))
"""


def _run_library_script(
    tmp_path: Path,
    *,
    output_dir: Path,
    extra_env: dict[str, str] | None = None,
    timeout: int = 60,
) -> subprocess.CompletedProcess[str]:
    """Run :data:`_SCRIPT` as a plain ``python`` subprocess (no otto binary).

    Takes the exact env :func:`tests.e2e._otto_subprocess.run_otto` would build
    (``OTTO_SUT_DIRS``, subprocess-coverage wiring, the ``-p no:tach`` scar —
    the script's ``run_tests`` drives ``pytest.main`` in-process, which is the
    #193 shape) but drives ``sys.executable`` against a script file, since this
    is a library-only flow with no CLI entry point involved.
    """
    script_path = tmp_path / "run_tests_script.py"
    script_path.write_text(_SCRIPT)

    env = otto_subprocess_env(
        sut_dirs=REPO_E2E,
        extra_env={"OTTO_TEST_OUTPUT_DIR": str(output_dir), **(extra_env or {})},
    )

    return subprocess.run(
        [sys.executable, str(script_path)],
        env=env,
        capture_output=True,
        text=True,
        cwd=str(PROJECT_ROOT),
        timeout=timeout,
        check=False,
    )


def test_library_run_tests_pass(tmp_path: Path) -> None:
    """``bootstrap()`` -> ``run_tests()`` on a passing class.

    The driving script exits 0; the printed :class:`SuiteRunResult` reports
    ``passed``, a real JUnit file was written, and the library-installed
    context was torn down (``try_get_context()`` is ``None`` again).
    """
    output_dir = tmp_path / "output"
    output_dir.mkdir()

    result = _run_library_script(tmp_path, output_dir=output_dir)

    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["passed"] is True
    assert payload["exit_code"] == 0
    assert payload["junit_exists"] is True
    assert payload["context_none_after"] is True


def test_library_run_tests_fail(tmp_path: Path) -> None:
    """``OTTO_E2E_FAIL=1`` fails the test — but the driving SCRIPT still exits 0.

    ``run_tests`` never raises for a red test; it reports the failure in the
    returned :class:`SuiteRunResult`. Only the plain Python script's own logic
    (absent here) would turn that into a nonzero process exit — mirroring how
    a real caller decides whether/how to propagate ``r.exit_code``.
    """
    output_dir = tmp_path / "output"
    output_dir.mkdir()

    result = _run_library_script(tmp_path, output_dir=output_dir, extra_env={"OTTO_E2E_FAIL": "1"})

    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["passed"] is False
    assert payload["exit_code"] == 1
    assert payload["junit_exists"] is True
    assert payload["context_none_after"] is True
