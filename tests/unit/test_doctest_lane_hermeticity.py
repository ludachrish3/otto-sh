"""The ``src/otto`` doctest lanes must not depend on the shell that runs them.

``make doctest-src`` and the nox ``docs`` session run ``pytest --doctest-modules
src/otto``. Collecting ``src/otto`` does not load ``tests/conftest.py``, so the
suite's ``OTTO_*`` strip never runs there: a developer shell that sourced
``project_env`` hands the doctests ``OTTO_SUT_DIRS=tests/repo1,tests/repo2``.
That broke the pre-push gate on ``otto.examples.reservations_cli``:
``run_check`` reaches ``OttoContext.scopes``, which bootstraps those repos, and
repo1 declares a ``[project]`` scope, so the ``fleet of interest`` INFO line is
logged mid-example.

That log line is harmful because of the second defect. pyproject turns on
``log_cli`` at INFO, and pytest's live-log handler suspends and resumes global
capture around every record. The resume re-installs pytest's own stream as
``sys.stdout`` over doctest's ``_SpoofOut``, so everything the example prints
afterwards, its return value included, lands in pytest's captured stdout and
doctest reports "Got nothing".

Both lanes therefore strip ``OTTO_*`` and pass ``-o log_cli=false``. The
tests below run each lane's own pytest command under a hostile environment
rather than pinning its text.
"""

import os
import shlex
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from tests._fixtures.paths import PROJECT_ROOT

# The failing shape from the pre-push gate: two fixture repos, one of which
# declares a project scope, so reaching the repos logs at INFO.
_HOSTILE_OTTO_ENV = {
    "OTTO_SUT_DIRS": f"{PROJECT_ROOT / 'tests' / 'repo1'},{PROJECT_ROOT / 'tests' / 'repo2'}",
    "OTTO_LAB": "unix",
}


# Named explicitly because a probe under tmp_path has no pyproject above it, so
# pytest would run it without the repo's config, `log_cli` included, and the
# probe could not see that flag go missing. For `src/otto` this is the file
# pytest finds on its own.
_PYPROJECT = PROJECT_ROOT / "pyproject.toml"


def _hostile_env() -> dict[str, str]:
    return {**os.environ, **_HOSTILE_OTTO_ENV}


def _assert_green(proc: subprocess.CompletedProcess[str]) -> None:
    assert proc.returncode == 0, proc.stdout[-4000:] + proc.stderr[-2000:]


def _make_lane(target: str) -> list[str]:
    """``make doctest-src``'s own command, pointed at *target* under the repo's config.

    ``make -n`` expands the recipe under the hostile environment, so the
    ``OTTO_*`` strip resolves exactly as it would for a real invocation. Only
    the launcher changes: ``uv run`` would sync a project venv from inside the
    test process.
    """
    dry = subprocess.run(
        ["make", "-n", "doctest-src"],
        cwd=PROJECT_ROOT,
        env=_hostile_env(),
        capture_output=True,
        text=True,
        check=True,
    )
    [recipe] = [line for line in dry.stdout.splitlines() if "uv run pytest" in line]
    assert recipe.endswith(" src/otto"), recipe
    command = recipe.replace("uv run pytest", f"{shlex.quote(sys.executable)} -m pytest")
    config = shlex.quote(str(_PYPROJECT))
    return ["bash", "-c", command.removesuffix("src/otto") + f"-c {config} {shlex.quote(target)}"]


def _nox_lane(target: str, monkeypatch: pytest.MonkeyPatch) -> tuple[list[str], dict[str, str]]:
    """The nox ``docs`` session's pytest leg, pointed at *target* under the repo's config.

    The session body is called with a recording double, which yields the argv
    and the ``env`` mapping it hands ``session.run``. nox merges that mapping
    over the outer environment and treats a ``None`` value as "unset", so the
    same merge is applied here.
    """
    sys.path.insert(0, str(PROJECT_ROOT))
    try:
        import noxfile
    finally:
        sys.path.remove(str(PROJECT_ROOT))

    for name, value in _HOSTILE_OTTO_ENV.items():
        monkeypatch.setenv(name, value)
    calls: list[tuple[tuple[str, ...], dict]] = []

    class _RecordingSession:
        def run(self, *args: str, **kwargs: object) -> None:
            calls.append((args, kwargs))

    noxfile.docs.func.__wrapped__(_RecordingSession())
    [(argv, kwargs)] = [(args, kw) for args, kw in calls if args[:1] == ("pytest",)]
    assert argv[-1] == "src/otto", argv
    overlay = kwargs.get("env") or {}
    assert isinstance(overlay, dict)
    merged = {**os.environ, **overlay}
    env = {name: value for name, value in merged.items() if value is not None}
    return [sys.executable, "-m", *argv[:-1], "-c", str(_PYPROJECT), target], env


def _run_lane(
    lane: str, target: str, monkeypatch: pytest.MonkeyPatch
) -> subprocess.CompletedProcess[str]:
    if lane == "make":
        argv, env = _make_lane(target), _hostile_env()
    else:
        argv, env = _nox_lane(target, monkeypatch)
    return subprocess.run(
        argv, cwd=PROJECT_ROOT, env=env, capture_output=True, text=True, check=False
    )


@pytest.mark.parametrize("lane", ["make", "nox"])
def test_the_src_doctests_are_green_under_an_ambient_otto_env(lane, monkeypatch) -> None:
    """The lane, run for real over ``src/otto``, from a shell that sourced ``project_env``."""
    _assert_green(_run_lane(lane, "src/otto", monkeypatch))


@pytest.mark.parametrize("lane", ["make", "nox"])
def test_the_lane_strips_otto_and_keeps_a_logging_doctests_output(
    lane, tmp_path, monkeypatch
) -> None:
    """Each half of the fix holds on its own, whatever ``src/otto`` happens to log today.

    The test above goes green on either half alone, so it cannot see one of
    them go missing. This points the lane at a probe that needs both: it logs
    at INFO (lost output without ``log_cli=false``) and then prints the
    ``OTTO_*`` names it can see (non-empty without the strip).
    """
    probe = tmp_path / "otto_lane_probe.py"
    probe.write_text(
        textwrap.dedent(
            '''
            """
            >>> import logging, os
            >>> logging.getLogger("otto").info("mid-example")
            >>> sorted(name for name in os.environ if name.startswith("OTTO_"))
            []
            """
            '''
        )
    )
    _assert_green(_run_lane(lane, str(probe), monkeypatch))


def test_live_logging_alone_swallows_a_logging_doctests_output(tmp_path: Path) -> None:
    """``-o log_cli=false`` is needed on its own, not only as cover for the env strip.

    A doctest example that logs at INFO and then prints fails under
    ``log_cli=true`` with no otto environment involved, and passes with live
    logging off. It is also the control for the lane probe above, which relies
    on this behaviour to notice a dropped flag: if pytest ever stops
    re-installing its capture stream over doctest's, this fails first and says
    why.
    """
    module = tmp_path / "logs_then_prints.py"
    module.write_text(
        textwrap.dedent(
            '''
            """
            >>> import logging
            >>> logging.getLogger("demo").info("mid-example")
            >>> print("visible")
            visible
            """
            '''
        )
    )

    def run(log_cli: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "-p",
                "no:cacheprovider",
                "-p",
                "no:tach",
                "--rootdir",
                str(tmp_path),
                "-c",
                os.devnull,
                "-o",
                "log_level=INFO",
                "-o",
                f"log_cli={log_cli}",
                "--doctest-modules",
                str(module),
            ],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            check=False,
        )

    live = run("true")
    assert live.returncode == 1, live.stdout
    assert "Got nothing" in live.stdout, live.stdout
    _assert_green(run("false"))
