"""A pytest session a test starts never shares the default temp root with other sessions.

pytest's default basetemp is a numbered dir under ``<temproot>/pytest-of-<user>``.
At session finish, a session that made one deletes every older unlocked sibling
but the newest few. A session's lock goes with it, so once the hostless lane's
main pass exits, its whole temp tree becomes deletable: the first child session
of the next pass to finish paid for deleting it, inside its test's budget, and
the nightly 3.10 leg timed out three nights running (#814). The root conftest's
``_hermetic_child_temproot`` points ``PYTEST_DEBUG_TEMPROOT`` at a per-process
directory under basetemp, so a child started from ``os.environ`` has no
siblings but other children of the same process. A child whose env is built
from scratch, the e2e harness's, gets the variable only through its builder.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.e2e._otto_subprocess import coverage_subprocess_env, otto_subprocess_env

pytestmark = pytest.mark.interpreter_agnostic

_PROBE = """\
import os


def test_probe(tmp_path_factory):
    with open(os.environ["PROBE_OUT"], "w", encoding="utf-8") as out:
        out.write(str(tmp_path_factory.getbasetemp()))
"""


@pytest.mark.parametrize("workers", [["-n0"], ["-n", "1"]], ids=["n0", "xdist"])
def test_a_child_session_takes_its_basetemp_under_this_process(
    tmp_path: Path, tmp_path_factory: pytest.TempPathFactory, workers: "list[str]"
) -> None:
    """A child, xdist or not, numbers its basetemp under this process's basetemp."""
    (tmp_path / "test_probe.py").write_text(_PROBE)
    out = tmp_path / "basetemp"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            "-o",
            "addopts=",
            "--rootdir",
            str(tmp_path),
            *workers,
            str(tmp_path / "test_probe.py"),
        ],
        env={**os.environ, "PROBE_OUT": str(out)},
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    ours = tmp_path_factory.getbasetemp().resolve()
    assert ours in Path(out.read_text()).resolve().parents


_BUILDERS = {
    "coverage": lambda xdir: coverage_subprocess_env(),
    # An xdir keeps the builder off its per-process fallback home.
    "otto": lambda xdir: otto_subprocess_env(xdir=xdir),
}


@pytest.mark.parametrize("builder", sorted(_BUILDERS))
def test_a_child_env_built_from_scratch_keeps_the_temp_root(
    builder: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Both e2e env builders pass the temp root through to their child."""
    monkeypatch.setenv("PYTEST_DEBUG_TEMPROOT", str(tmp_path / "sentinel"))
    env = _BUILDERS[builder](tmp_path)
    assert env["PYTEST_DEBUG_TEMPROOT"] == str(tmp_path / "sentinel")
