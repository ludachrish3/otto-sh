"""Pins for `make quick` / `make quick-guards` — the scoped per-commit lanes (#566).

The recipe text is pinned by a reader; the behaviours the text cannot show
(the PATHS refusal, one empty leg tolerated, two empty legs refused, a non-5
failure aborting) are pinned by running make for real on tiny selections.
"""

import os
import re
import signal
import subprocess
from pathlib import Path

import pytest

from tests._fixtures.paths import PROJECT_ROOT
from tests.unit.test_lane_invariants import collect_marker_sets  # noqa: F401 - fixture
from tests.unit.test_make_release_lanes import _phony_targets, _rule

pytestmark = pytest.mark.interpreter_agnostic

_MAKEFILE = (PROJECT_ROOT / "Makefile").read_text()

M_QUICK = (
    "not stability and not busybox and not conformance and not kmodcov and not browser"
    " and not interpreter_agnostic"
)
# A plain unit module: ordinary tests, no family mark, no serial_timing test.
# Both quick legs would deselect a family-marked module (the stamp test is one).
_PLAIN_MODULE = "tests/unit/result/test_result.py"
# Per `make` call; the tests that make two calls allow 2x plus headroom, so the
# child is always the one that times out, never the parent mid-cleanup.
_MAKE_TIMEOUT = 240


def _variable(name: str) -> str:
    match = re.search(rf"^{re.escape(name)} (?:\?=|:=|=) (.+)$", _MAKEFILE, re.MULTILINE)
    assert match is not None, f"Makefile defines no `{name}`"
    return match.group(1)


def _definition_line(name: str) -> int:
    for number, line in enumerate(_MAKEFILE.splitlines(), 1):
        if re.match(rf"^{re.escape(name)} (?:\?=|:=|=) ", line):
            return number
    raise AssertionError(f"Makefile defines no `{name}`")


def test_m_quick_is_the_tier_exclusions_only():
    assert _variable("M_QUICK") == M_QUICK


def test_quick_empty_is_defined_after_the_junit_dir_it_expands():
    """A `:=` expands at definition: before JUNIT_DIR it would freeze `/quick/empty-legs`."""
    assert _variable("QUICK_EMPTY") == "$(JUNIT_DIR)/quick/empty-legs"
    assert _definition_line("QUICK_EMPTY") > _definition_line("JUNIT_DIR")


def test_quick_workers_is_computed_once_and_the_command_line_wins():
    """`:=` runs the nproc probe once, at parse; a `?=` would rerun it at every reference."""
    assert re.search(r"^QUICK_WORKERS := ", _MAKEFILE, re.MULTILINE), "QUICK_WORKERS is not `:=`"
    value = _variable("QUICK_WORKERS")
    assert value.startswith(
        "$(if $(filter command line,$(origin QUICK_WORKERS)),$(QUICK_WORKERS),$(shell "
    ), value


def test_the_real_runs_keep_out_of_the_parent_sessions_cache():
    """The child pytest would write `.pytest_cache` under the parent's feet: it runs without
    the cache provider, and a caller's own PYTEST_ADDOPTS is kept beside it."""
    assert _child_env({})["PYTEST_ADDOPTS"] == "-p no:cacheprovider"
    extra = _child_env({"PYTEST_ADDOPTS": "--no-such-flag"})
    assert extra["PYTEST_ADDOPTS"] == "-p no:cacheprovider --no-such-flag"


def test_quick_runs_two_uninstrumented_legs_and_tolerates_one_empty_leg():
    _prereqs, recipe = _rule(_MAKEFILE, "quick")
    legs = [line for line in recipe if "uv run pytest" in line]
    assert len(legs) == 2, recipe
    parallel, serial = legs
    assert '$(PATHS) -m "$(M_QUICK) and not serial_timing"' in parallel, parallel
    assert "-n $(QUICK_WORKERS)" in parallel, parallel
    assert "--no-cov" in parallel, parallel
    assert "$(call junitxml,quick)" in parallel, parallel
    assert '$(PATHS) -m "serial_timing and $(M_QUICK)"' in serial, serial
    assert "-n0" in serial, serial
    assert "--no-cov" in serial, serial
    assert "$(call junitxml,quick-serial)" in serial, serial
    for leg in legs:
        assert leg.startswith("@$(LEAK_DETECT) $(TIMEOUT_CMD) uv run pytest"), leg
        assert "|| { [ $$? -eq 5 ] &&" in leg, f"exit 5 must be tolerated per leg: {leg}"
        assert "-p no:randomly" not in leg, "the spec keeps random ordering on"
    assert any("$(error" in line and "PATHS" in line for line in recipe), recipe


def test_quick_guards_is_the_family_in_one_parallel_leg():
    _prereqs, recipe = _rule(_MAKEFILE, "quick-guards")
    legs = [line for line in recipe if "uv run pytest" in line]
    assert len(legs) == 1, recipe
    (leg,) = legs
    assert 'tests/unit -m "interpreter_agnostic"' in leg, leg
    assert "-n $(QUICK_WORKERS)" in leg, leg
    assert "--no-cov" in leg, leg
    assert "$(call junitxml,quick-guards)" in leg, leg


def test_the_new_lanes_are_phony():
    missing = {"quick", "quick-guards", "gate-scope"} - _phony_targets(_MAKEFILE)
    assert not missing, sorted(missing)


def _child_env(env_extra: "dict[str, str]") -> "dict[str, str]":
    """The real runs' environment: the parent's hand-off stripped, the cache provider off.

    The cache provider is off so the child never writes `.pytest_cache` while the parent
    session (often `make coverage`) is live in the same tree; a PYTEST_ADDOPTS the caller
    passes is appended after it.
    """
    dropped = ("PYTEST_", "COV_CORE_", "MAKEFLAGS", "MFLAGS", "MAKELEVEL")
    env = {k: v for k, v in os.environ.items() if not k.startswith(dropped)}
    env.update(env_extra)
    env["PYTEST_ADDOPTS"] = " ".join(
        part for part in ("-p no:cacheprovider", env_extra.get("PYTEST_ADDOPTS")) if part
    )
    return env


def _make(tmp_path: Path, *args: str, env_extra: "dict[str, str] | None" = None):
    """Run make for real, with the parent pytest's hand-off stripped and JUnit in tmp.

    PYTEST_* carries the xdist worker id and any ambient PYTEST_ADDOPTS,
    COV_CORE_* pytest-cov's subprocess hand-off, MAKEFLAGS/MFLAGS/MAKELEVEL an
    outer make's flags (this suite runs under `make coverage`). The child is
    its own session so a timeout kills make AND the pytest it spawned.
    """
    env = _child_env(env_extra or {})
    argv = [
        "make",
        "--no-print-directory",
        *args,
        f"JUNIT_DIR={tmp_path / 'junit'}",
        "QUICK_WORKERS=2",
    ]
    with subprocess.Popen(
        argv,
        cwd=PROJECT_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        start_new_session=True,
    ) as proc:
        try:
            out, err = proc.communicate(timeout=_MAKE_TIMEOUT)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            out, err = proc.communicate()
            raise AssertionError(
                f"make {' '.join(args)} exceeded {_MAKE_TIMEOUT}s:\n{out[-3000:]}\n{err[-3000:]}"
            ) from None
    return subprocess.CompletedProcess(argv, proc.returncode, out, err)


@pytest.mark.timeout(300)
def test_quick_refuses_without_paths(tmp_path: Path):
    """`PATHS=` on the command line beats any ambient PATHS in the environment."""
    completed = _make(tmp_path, "quick", "PATHS=")
    assert completed.returncode == 2, completed.stderr
    assert "PATHS" in completed.stderr, completed.stderr
    assert "gate-scope" in completed.stderr, completed.stderr
    assert "pytest" not in completed.stdout, "nothing may run before the refusal"
    assert not (tmp_path / "junit").exists(), "nothing may run before the refusal"


@pytest.mark.timeout(300)
@pytest.mark.xdist_group("make_quick_real_runs")
def test_quick_refuses_an_ambient_paths(tmp_path: Path):
    """PATHS is honoured from the command line only: one exported by a shell or an outer
    tool is not a request to run those paths."""
    completed = _make(tmp_path, "quick", env_extra={"PATHS": _PLAIN_MODULE})
    assert completed.returncode == 2, completed.stdout[-2000:] + completed.stderr[-2000:]
    assert "PATHS" in completed.stderr, completed.stderr
    assert "pytest" not in completed.stdout, "nothing may run before the refusal"
    assert not (tmp_path / "junit").exists(), "nothing may run before the refusal"


@pytest.mark.timeout(120)
def test_gate_scope_reads_base_from_the_command_line_only(tmp_path: Path):
    """`make -n` prints the one recipe line without running it."""
    ambient = _make(tmp_path, "-n", "gate-scope", env_extra={"BASE": "from-env"})
    assert ambient.returncode == 0, ambient.stderr
    assert '--base "origin/main"' in ambient.stdout, ambient.stdout
    given = _make(tmp_path, "-n", "gate-scope", "BASE=main", env_extra={"BASE": "from-env"})
    assert given.returncode == 0, given.stderr
    assert '--base "main"' in given.stdout, given.stdout


@pytest.mark.timeout(600)
@pytest.mark.xdist_group("make_quick_real_runs")
def test_quick_tolerates_one_empty_leg_but_not_two(tmp_path: Path):
    one = _make(tmp_path, "quick", f"PATHS={_PLAIN_MODULE}")
    assert one.returncode == 0, one.stdout[-3000:] + one.stderr[-3000:]
    assert "quick: the serial leg selected no test" in one.stdout, one.stdout[-3000:]
    assert "quick: the parallel leg selected no test" not in one.stdout, one.stdout[-3000:]
    report = (tmp_path / "junit/quick/quick.xml").read_text()
    match = re.search(r'tests="(\d+)"', report)
    assert match is not None, report[:300]
    assert int(match.group(1)) > 0, "the parallel leg ran tests"
    both = _make(tmp_path, "quick", "PATHS=tests/_fixtures/paths.py")
    assert both.returncode == 2, both.stdout[-3000:]
    assert "neither leg selected a test" in both.stderr, both.stderr


@pytest.mark.timeout(300)
@pytest.mark.xdist_group("make_quick_real_runs")
def test_a_failure_that_is_not_exit_5_aborts_the_lane(tmp_path: Path):
    """pytest's usage error (exit 4) is not tolerated: the shell group yields 1, make stops."""
    failed = _make(
        tmp_path, "quick", f"PATHS={_PLAIN_MODULE}", env_extra={"PYTEST_ADDOPTS": "--no-such-flag"}
    )
    assert failed.returncode == 2, failed.stdout[-2000:] + failed.stderr[-2000:]
    assert "neither leg selected a test" not in failed.stderr
    assert "unrecognized arguments" in failed.stderr, failed.stderr[-2000:]
    assert "serial_timing twin" not in failed.stdout, "make must stop at the first red leg"


@pytest.mark.xdist_group("lane_leg_membership")
def test_the_family_has_no_serial_timing_member(collect_marker_sets) -> None:  # noqa: F811
    """`quick-guards` is one parallel leg: a serial_timing guard would error in a worker."""
    both = [
        item.nodeid
        for item in collect_marker_sets(("tests/unit",))
        if {"interpreter_agnostic", "serial_timing"} <= item.markers
    ]
    assert not both, (
        "give `quick-guards` a -n0 twin before marking one of these:\n  " + "\n  ".join(both)
    )
