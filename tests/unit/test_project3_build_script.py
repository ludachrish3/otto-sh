"""``tests/project3/product/build.sh``: it names the otto whose shipped files it consumes."""

import subprocess
from pathlib import Path

from tests._fixtures.paths import CHECKOUT_ROOT

BUILD_SH = CHECKOUT_ROOT / "tests" / "project3" / "product" / "build.sh"


def test_build_refuses_without_an_otto_rather_than_falling_back_to_path(tmp_path: Path):
    """An unset OTTO stops the script before any build step, naming the variable."""
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)}
    done = subprocess.run(
        ["bash", str(BUILD_SH), str(tmp_path / "build")],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode != 0
    assert "OTTO" in done.stderr
    assert "west build" not in done.stdout


def _build(tmp_path: Path, **env: str) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(
        ["bash", str(BUILD_SH), str(tmp_path / "build")],
        env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), **env},
        capture_output=True,
        text=True,
        check=False,
    )


def test_the_refusal_does_not_suggest_path_resolution(tmp_path: Path):
    """The hint names the otto beside the interpreter, never whichever `otto` PATH finds."""
    done = _build(tmp_path)
    assert "command -v otto" not in done.stderr
    assert ".venv/bin/otto" in done.stderr


def test_an_otto_that_is_not_executable_is_refused_by_name(tmp_path: Path):
    done = _build(tmp_path, OTTO=str(tmp_path / "no-such-otto"))
    assert done.returncode != 0
    assert "no-such-otto" in done.stderr
    assert "not executable" in done.stderr


def test_an_otto_with_no_python_beside_it_is_refused_by_name(tmp_path: Path):
    """The starter is located through the otto's own interpreter; a bare symlink has none."""
    bare = tmp_path / "bin" / "otto"
    bare.parent.mkdir()
    bare.symlink_to("/bin/true")
    done = _build(tmp_path, OTTO=str(bare))
    assert done.returncode == 1
    assert "no python beside" in done.stderr
    assert str(bare) in done.stderr
