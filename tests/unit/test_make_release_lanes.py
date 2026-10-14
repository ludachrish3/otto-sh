"""The make targets a release relies on, exercised for real with `$(MAKE)` stubbed.

GNU make lets a command-line `MAKE=<path>` override the recursive-make
variable, so a recipe's `$(MAKE) <target>` lines run a stub that records the
target it was asked for and fails the ones a test names — the recipe runs
exactly as a user's would, and the test sees what it invoked and what it
returned.

`make stability` (#566): before this landed, tier 1 ran under `-@` (its exit
discarded) and a red tier 2 aborted the recipe before tiers 2b and 3 ran, so a
release could not rely on it to either run everything or say no.
"""

import os
import re
import stat
import subprocess
from pathlib import Path

import pytest

from tests._fixtures.paths import PROJECT_ROOT

pytestmark = pytest.mark.interpreter_agnostic

_TIERS = ("stability-unit", "stability-unix", "stability-tunnel", "stability-embedded")
_HELPER = "lab-reachability"


def _stub_make(tmp_path: Path, failing: "dict[str, int]") -> "tuple[Path, Path]":
    """A `make` stand-in: logs "$*" (and OTTO_CONFORMANCE_CELLS) per call.

    Exits *failing*[target] for a named target, 0 otherwise.
    """
    log = tmp_path / "calls.log"
    stub = tmp_path / "make-stub"
    cases = "".join(f'  *" {target} "*) exit {code} ;;\n' for target, code in failing.items())
    stub.write_text(
        "#!/bin/sh\n"
        f'printf "%s cells=%s\\n" "$*" "${{OTTO_CONFORMANCE_CELLS:-unset}}" >> "{log}"\n'
        f'case " $* " in\n{cases}esac\n'
        "exit 0\n",
        encoding="utf-8",
    )
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
    return stub, log


def _run_make(stub: Path, *args: str) -> "subprocess.CompletedProcess[str]":
    # An outer make (this suite under `make coverage`) exports MAKEFLAGS,
    # MFLAGS and MAKELEVEL; dropped so its flags cannot leak into the recipe.
    # OTTO_CONFORMANCE_CELLS is a declared ambient opt-in the conftest leaves
    # alone; dropped so the campaign's `cells=all` can only come from the recipe.
    dropped = {"MAKEFLAGS", "MFLAGS", "MAKELEVEL", "OTTO_CONFORMANCE_CELLS"}
    env = {k: v for k, v in os.environ.items() if k not in dropped}
    return subprocess.run(
        ["make", "--no-print-directory", *args, f"MAKE={stub}"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
        env=env,
    )


def _targets_called(log: Path) -> "list[str]":
    """The target of each stub call, in order: the first "$*" token that is not an option."""
    targets: "list[str]" = []
    for line in log.read_text(encoding="utf-8").splitlines():
        targets.append(next(token for token in line.split() if not token.startswith("-")))
    return targets


def _tiers_called(log: Path) -> "list[str]":
    return [target for target in _targets_called(log) if target != _HELPER]


@pytest.mark.parametrize("failing", _TIERS)
def test_a_red_tier_fails_stability_only_after_every_tier_ran(tmp_path: Path, failing: str):
    """Any one red tier: all four still run, the target exits non-zero and names it."""
    stub, log = _stub_make(tmp_path, {failing: 1})
    completed = _run_make(stub, "stability")
    assert _tiers_called(log) == list(_TIERS), _tiers_called(log)
    assert completed.returncode != 0, completed.stdout + completed.stderr
    assert f"stability: FAILED tier(s): {failing}" in completed.stderr, completed.stderr


def test_two_red_tiers_are_both_named(tmp_path: Path):
    """Aggregation, and a `timeout`-style 124 counts like an assertion's 1."""
    stub, log = _stub_make(tmp_path, {"stability-unix": 124, "stability-embedded": 1})
    completed = _run_make(stub, "stability")
    assert _tiers_called(log) == list(_TIERS)
    assert completed.returncode != 0
    expected = "stability: FAILED tier(s): stability-unix stability-embedded"
    assert expected in completed.stderr, completed.stderr


def test_every_tier_green_is_exit_zero_and_the_ping_check_still_runs(tmp_path: Path):
    stub, log = _stub_make(tmp_path, {})
    completed = _run_make(stub, "stability")
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert _tiers_called(log) == list(_TIERS)
    assert _HELPER in _targets_called(log), "the lab ping check left the recipe?"
    assert "stability: FAILED tier(s)" not in completed.stderr, completed.stderr
    assert "stability: every tier green" in completed.stdout, completed.stdout


def test_the_tiers_run_at_the_count_the_caller_gave(tmp_path: Path):
    """`make stability COUNT=1` is how the release campaign runs it: COUNT must reach every tier."""
    stub, log = _stub_make(tmp_path, {})
    completed = _run_make(stub, "stability", "COUNT=1")
    assert completed.returncode == 0, completed.stdout + completed.stderr
    tier_lines = [
        line
        for line in log.read_text(encoding="utf-8").splitlines()
        if any(t in line.split() for t in _TIERS)
    ]
    assert len(tier_lines) == len(_TIERS), tier_lines
    assert all("COUNT=1" in line for line in tier_lines), tier_lines


_CAMPAIGN = ("stability", "dashboard-soak", "conformance", "busybox", "chaos", "chaos-embedded")


def test_the_campaign_runs_every_lane_in_order_when_all_are_green(tmp_path: Path):
    stub, log = _stub_make(tmp_path, {})
    completed = _run_make(stub, "release-campaign")
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert _targets_called(log) == list(_CAMPAIGN), _targets_called(log)
    lines = log.read_text(encoding="utf-8").splitlines()
    stability = next(line for line in lines if line.startswith("stability "))
    assert "COUNT=1" in stability, stability
    conformance = next(line for line in lines if line.startswith("conformance "))
    assert "cells=all" in conformance, conformance
    others = [line for line in lines if not line.startswith("conformance ")]
    assert all(line.endswith("cells=unset") for line in others), others


def test_dashboard_all_runs_the_dashboard_on_every_engine(tmp_path: Path):
    """`make dashboard-all` is `dashboard` with the three-engine override (the release's lane).

    The census sees the hermetic collection only, so an engine dropped from the
    override would lose its parametrisations without a row changing; this is
    the pin for the override itself.
    """
    stub, log = _stub_make(tmp_path, {})
    completed = _run_make(stub, "dashboard-all")
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert _targets_called(log) == ["dashboard"], _targets_called(log)
    (line,) = log.read_text(encoding="utf-8").splitlines()
    override = re.search(r"DASHBOARD_BROWSERS=(.*?) cells=", line)
    assert override, line
    assert set(override.group(1).split()) == {"chromium", "firefox", "webkit"}, line


@pytest.mark.parametrize("lane", _CAMPAIGN)
@pytest.mark.parametrize("code", [1, 5])
def test_a_red_or_empty_lane_aborts_the_campaign_and_nothing_after_it_runs(
    tmp_path: Path, lane: str, code: int
):
    """Exit 1 (a failure) and exit 5 (pytest collected nothing) both stop the release there."""
    stub, log = _stub_make(tmp_path, {lane: code})
    completed = _run_make(stub, "release-campaign")
    assert completed.returncode != 0, completed.stdout + completed.stderr
    assert _targets_called(log) == list(_CAMPAIGN[: _CAMPAIGN.index(lane) + 1]), _targets_called(
        log
    )
