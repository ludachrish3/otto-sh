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
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest
import tomli

import scripts.release_record as rr
import scripts.release_tag
from scripts import gate_tip as gt
from tests._fixtures.fake_gh import GhReply, gh_calls, install_fake_gh
from tests._fixtures.gitrepo import TmpGitRepo, git_env
from tests._fixtures.lease_env import lease_free_environ
from tests._fixtures.paths import CHECKOUT_ROOT

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


def _run_make(
    stub: Path, *args: str, env: "dict[str, str] | None" = None
) -> "subprocess.CompletedProcess[str]":
    # An outer make (this suite under `make coverage`) exports MAKEFLAGS,
    # MFLAGS and MAKELEVEL; dropped so its flags cannot leak into the recipe.
    # OTTO_CONFORMANCE_CELLS is a declared ambient opt-in the conftest leaves
    # alone; dropped so the campaign's `cells=all` can only come from the recipe.
    # The leased targets (stability, release-campaign) take the bed lease: in a root
    # beside the stub, never the real cell's, and in a session of their own so the
    # lease's process group is the make's, not this test's. *env* is laid over the rest.
    dropped = {"MAKEFLAGS", "MFLAGS", "MAKELEVEL", "OTTO_CONFORMANCE_CELLS"}
    lease_free = lease_free_environ(BED_LEASE_ROOT=str(stub.parent / "bed-lease"))
    env = {**{k: v for k, v in lease_free.items() if k not in dropped}, **(env or {})}
    return subprocess.run(
        ["make", "--no-print-directory", *args, f"MAKE={stub}"],
        cwd=CHECKOUT_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
        env=env,
        start_new_session=True,
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


# No chaos lane: `chaos` / `chaos-embedded` are on demand only (owner
# decision 2026-10-15, reason beside _RELEASE_EXEMPT_LANES in
# test_lane_invariants.py). The equality below is also the pin that they stay out.
_CAMPAIGN = ("stability", "dashboard-soak", "conformance", "busybox")


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


# `make coverage` is stamped (scripts/coverage_stamp.py): the stamp's prologue
# is the first prerequisite, its epilogue the one recipe line, so a red leg
# (make aborts) never reaches `finish` and the stamp `start` deleted stays
# deleted. The script's own behaviour is pinned in
# tests/unit/scripts/test_coverage_stamp.py; this is the wiring.
#
# ANY command-line variable marks the run (the legs expand far more than the
# four gate variables — junitxml, LEAK_DETECT, TIMEOUT_CMD, …), so the
# samples are a few gate variables, two recipe fragments and an arbitrary
# name; BUMP, NEW_VERSION and BUDGET_APPROVE, the release's own knobs, are the
# allowlist.
_OVERRIDE_SAMPLES = (
    "COVERAGE_THRESHOLD",
    "PYTEST_ADDOPTS",
    "junitxml",
    "LEAK_DETECT",
    "ANY_NAME_AT_ALL",
)


def _overridden(stdout: str) -> "list[str]":
    """The names a dry-run's `coverage_stamp.py start` line marks `--overridden`, sorted."""
    return sorted(re.findall(r"--overridden (\S+)", stdout))


def _rule(text: str, target: str) -> "tuple[list[str], list[str]]":
    """(prerequisites, recipe lines) of *target*'s rule WITH a recipe.

    Comments and `##` help are dropped. A target may own several rule lines
    (`coverage: export override PYTEST_ADDOPTS :=` is a target-specific
    variable, no recipe follows it); the one with the
    recipe is the rule. The recipe group is `+`, so a recipe-less line never
    matches and the search moves on to the next `target:` line.
    """
    live = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    pattern = rf"^{re.escape(target)}:([^#\n]*)(?:##[^\n]*)?\n((?:\t.*\n)+)"
    match = re.search(pattern, live + "\n", re.MULTILINE)
    assert match is not None, f"Makefile has no `{target}` rule with a recipe"
    return match.group(1).split(), [
        line.strip() for line in match.group(2).splitlines() if line.strip()
    ]


def test_the_rule_reader_skips_a_target_specific_variable_line():
    text = "x: export override V :=\nx: a b ## help\n\t@one\n\t@two\n\ny:\n\t@none\n"
    assert _rule(text, "x") == (["a", "b"], ["@one", "@two"])
    assert _rule(text, "y") == ([], ["@none"])
    with pytest.raises(AssertionError, match="no `z` rule"):
        _rule("z: export V := 1\n", "z")


def test_make_coverage_is_stamped_at_its_start_and_its_end():
    text = (CHECKOUT_ROOT / "Makefile").read_text()
    prerequisites, recipe = _rule(text, "coverage")
    assert prerequisites == ["coverage-stamp-start", "coverage-python", "coverage-ts"], (
        prerequisites
    )
    assert recipe == ["@$(COVERAGE_STAMP) mark coverage-ts", "@$(COVERAGE_STAMP) finish"], recipe
    _, legs = _rule(text, "coverage-python")
    assert legs[0] == "$(call bed_acquire,coverage-python)", legs[0]
    assert legs[-1] == "$(call bed_release)", legs[-1]
    assert legs[1] == legs[-2] == "$(call bed_reap)", legs
    legs = legs[2:-2]  # inside the lease and the reap (test_lane_invariants pins those lines)
    marks = [line for line in legs if "$(COVERAGE_STAMP) mark" in line]
    assert marks == [
        "@$(COVERAGE_STAMP) mark dashboard",
        "@$(COVERAGE_STAMP) mark coverage-python",
        "@$(COVERAGE_STAMP) mark coverage-python-serial",
    ], marks
    assert legs[0] == marks[0], "the dashboard prerequisite ends where the recipe begins"
    pytest_lines = [i for i, line in enumerate(legs) if "uv run pytest" in line]
    assert pytest_lines[0] == legs.index(marks[1]) - 1, "the parallel leg is marked right after"
    assert legs[-1] == marks[2], "the serial leg's mark closes the recipe, after unit-outliers"
    assert pytest_lines[1] < legs.index(marks[2])
    dist, start = _rule(text, "coverage-stamp-start")
    assert dist == ["$(DASHBOARD_DIST)", "$(COVAPP_DIST)"], (
        "the assets digest is taken at start, so the dist rules must have run before it"
    )
    assert start == [
        (
            "@$(COVERAGE_STAMP) start $(COVERAGE_STAMP_GATE)"
            ' --workers "$(COVERAGE_WORKERS)" $(COVERAGE_STAMP_OVERRIDES)'
        )
    ], start
    # `override` is load-bearing: a plain target-specific `export VAR :=` loses
    # to `make coverage PYTEST_ADDOPTS=--no-cov` (measured, GNU make 4.3).
    assert re.search(r"^coverage: export override PYTEST_ADDOPTS :=\s*$", text, re.MULTILINE), (
        "an ambient or command-line PYTEST_ADDOPTS could narrow the selection the stamp names; "
        "`make coverage` must clear it with `export override`"
    )
    gate = (
        r'^COVERAGE_STAMP_GATE = --threshold "\$\(COVERAGE_THRESHOLD\)" '
        r'--selection "\$\(M_COVERAGE\)" '
        r'--browsers "\$\(DASHBOARD_BROWSERS\)" --primary "\$\(NOX_PRIMARY\)"\s*$'
    )
    assert re.search(gate, text, re.MULTILINE)
    # Every command-line variable, by name, read from .VARIABLES by its
    # `command line` origin at parse time — before an `export override` can
    # turn the origin into `override`, and without cutting a multi-word value
    # as a split of $(MAKEOVERRIDES) does; MAKEFLAGS carries the names into the
    # `$(MAKE) coverage` fallback.
    for line in (
        r"^COVERAGE_STAMP_HARMLESS := BUMP NEW_VERSION BUDGET_APPROVE\s*$",
        (
            r"^COVERAGE_STAMP_OVERRIDE_NAMES := \$\(sort \$\(foreach v,\$\(\.VARIABLES\),"
            r"\$\(if \$\(filter command line,\$\(origin \$\(v\)\)\),\$\(v\)\)\)\)\s*$"
        ),
        (
            r"^COVERAGE_STAMP_OVERRIDES = \$\(foreach n,\$\(filter-out "
            r"\$\(COVERAGE_STAMP_HARMLESS\),\$\(COVERAGE_STAMP_OVERRIDE_NAMES\)\),"
            r"--overridden \$\(n\)\)\s*$"
        ),
    ):
        assert re.search(line, text, re.MULTILINE), f"the Makefile lost {line!r}"


def test_a_command_line_pytest_addopts_does_not_reach_the_stamped_legs(tmp_path: Path):
    """Neither an environment nor a COMMAND-LINE `PYTEST_ADDOPTS` reaches the prerequisites.

    The probe Makefile includes the real one and appends one prerequisite to
    `coverage` whose `+`-prefixed recipe runs even under `-n` and prints the
    variable; `coverage`'s own target-specific `export override` is what the
    probe inherits, so this proves the real line. Under `-n` every pytest leg
    is printed, not run; the `$(MAKE)` lines (dist, TS stamp) run the stub.
    Measured on GNU make 4.3: without `override` this prints `ADDOPTS=[--no-cov]`.
    """
    stub, _log = _stub_make(tmp_path, {})
    probe = tmp_path / "Probe.mk"
    probe.write_text(
        f"include {CHECKOUT_ROOT / 'Makefile'}\n"
        "coverage: addopts-probe\n"
        "addopts-probe:\n"
        '\t+@echo "ADDOPTS=[$$PYTEST_ADDOPTS]"\n'
    )
    env = {k: v for k, v in os.environ.items() if k not in {"MAKEFLAGS", "MFLAGS", "MAKELEVEL"}}
    env["PYTEST_ADDOPTS"] = "-k nothing"
    completed = subprocess.run(
        [
            "make",
            "--no-print-directory",
            "-n",
            "-f",
            str(probe),
            "coverage",
            "PYTEST_ADDOPTS=--no-cov",
            f"MAKE={stub}",
        ],
        cwd=CHECKOUT_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
        env=env,
    )
    assert completed.returncode == 0, completed.stderr
    assert "ADDOPTS=[]" in completed.stdout, completed.stdout
    assert "--no-cov" not in completed.stdout.replace("PYTEST_ADDOPTS=--no-cov", ""), (
        completed.stdout
    )


def test_every_coverage_python_leg_selects_m_coverage():
    """The stamp records `$(M_COVERAGE)` as the selection; the legs must run exactly that."""
    text = (CHECKOUT_ROOT / "Makefile").read_text()
    _, legs = _rule(text, "coverage-python")
    selections = re.findall(r'-m "([^"]+)"', "\n".join(legs))
    assert selections == [
        "$(M_COVERAGE) and not serial_timing",
        "serial_timing and $(M_COVERAGE)",
    ], selections


@pytest.mark.parametrize("variable", _OVERRIDE_SAMPLES)
def test_a_command_line_override_reaches_the_stamp_start(tmp_path: Path, variable: str):
    """`make coverage COVERAGE_THRESHOLD=50` is green at a gate the stamp would misdescribe.

    `make -n` prints the expanded command without running it (the dist rules'
    `$(MAKE) web`, if stale, runs the stub), so this proves the
    command-line-origin reading of .VARIABLES for real. The stub's own
    `MAKE=<stub>` is a command-line variable too, so it is always marked —
    proof, every time, that an arbitrary name is caught — and the other
    samples never are.
    """
    stub, _log = _stub_make(tmp_path, {})
    with_override = _run_make(stub, "-n", "coverage-stamp-start", f"{variable}=zzz")
    assert with_override.returncode == 0, with_override.stderr
    assert _overridden(with_override.stdout) == sorted(["MAKE", variable]), with_override.stdout
    plain = _run_make(stub, "-n", "coverage-stamp-start")
    assert _overridden(plain.stdout) == ["MAKE"], plain.stdout
    assert "coverage_stamp.py start" in plain.stdout


def test_the_release_s_own_knobs_do_not_withhold_the_stamp(tmp_path: Path):
    """`make release BUMP=minor` must still be able to trust (and make) a stamp."""
    stub, _log = _stub_make(tmp_path, {})
    completed = _run_make(stub, "-n", "coverage-stamp-start", "BUMP=minor", "NEW_VERSION=1.2.3")
    assert completed.returncode == 0, completed.stderr
    assert "coverage_stamp.py start" in completed.stdout, completed.stdout
    assert _overridden(completed.stdout) == ["MAKE"], completed.stdout


def test_a_command_line_variable_is_marked_inside_the_coverage_context(tmp_path: Path):
    """`make coverage PYTEST_ADDOPTS=…`: the invocation a per-name `$(origin …)` test got wrong.

    `coverage`'s target-specific `export override PYTEST_ADDOPTS :=` is
    inherited by `coverage-stamp-start` and turns the variable's origin into
    `override`, so an origin test made at recipe time saw no command-line value
    there; the names read from .VARIABLES at parse time still list it. The
    probe includes the real Makefile and dry-runs `coverage` itself, so the
    start line printed is the one a real `make coverage PYTEST_ADDOPTS=--no-cov`
    would run.
    """
    stub, _log = _stub_make(tmp_path, {})
    probe = tmp_path / "Probe.mk"
    probe.write_text(f"include {CHECKOUT_ROOT / 'Makefile'}\n")
    env = {k: v for k, v in os.environ.items() if k not in {"MAKEFLAGS", "MFLAGS", "MAKELEVEL"}}
    completed = subprocess.run(
        [
            "make",
            "--no-print-directory",
            "-n",
            "-f",
            str(probe),
            "coverage",
            "PYTEST_ADDOPTS=--no-cov",
            f"MAKE={stub}",
        ],
        cwd=CHECKOUT_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
        env=env,
    )
    assert completed.returncode == 0, completed.stderr
    starts = [line for line in completed.stdout.splitlines() if "coverage_stamp.py start" in line]
    assert len(starts) == 1, completed.stdout
    assert "--overridden PYTEST_ADDOPTS" in starts[0], starts[0]


def _phony_targets(text: str) -> "set[str]":
    """Every whole token of the Makefile's `.PHONY:` lines, backslash continuations joined."""
    joined = text.replace("\\\n", " ")
    tokens: "set[str]" = set()
    for match in re.finditer(r"^\.PHONY:([^\n]*)", joined, re.MULTILINE):
        tokens.update(match.group(1).split())
    return tokens


def test_every_stamp_and_release_target_is_phony():
    """A file named `coverage` or `release` in the checkout must not make its target up to date."""
    assert "nox-release" not in _phony_targets(".PHONY: nox-releasenox-full\n"), (
        "the reader must take whole tokens, not substrings of a merged one"
    )
    assert "b" in _phony_targets(".PHONY: a \\\n\tb\n"), "a continued .PHONY line must be read"
    phony = _phony_targets((CHECKOUT_ROOT / "Makefile").read_text())
    missing = {
        "coverage",
        "coverage-stamp-start",
        "nox-release",
        "release",
        "release-preflight",
    } - phony
    assert not missing, f"not .PHONY: {sorted(missing)}"


def test_an_environment_value_is_not_an_override(tmp_path: Path):
    """`?=` takes an environment value as the declared default.

    Only the command line marks the run: the stub's `MAKE=<stub>` is on it,
    so it is marked; `DASHBOARD_BROWSERS`, from the environment, is not.
    """
    stub, _log = _stub_make(tmp_path, {})
    env = {k: v for k, v in os.environ.items() if k not in {"MAKEFLAGS", "MFLAGS", "MAKELEVEL"}}
    env["DASHBOARD_BROWSERS"] = "firefox"
    completed = subprocess.run(
        ["make", "--no-print-directory", "-n", "coverage-stamp-start", f"MAKE={stub}"],
        cwd=CHECKOUT_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
        env=env,
    )
    assert completed.returncode == 0, completed.stderr
    assert '--browsers "firefox"' in completed.stdout, completed.stdout
    assert "--overridden DASHBOARD_BROWSERS" not in completed.stdout, completed.stdout
    assert "--overridden MAKE" in completed.stdout, completed.stdout


# --- the import-budget delta stage -----------------------------------------


def _delta_line(stdout: str) -> str:
    [line] = [ln for ln in stdout.splitlines() if "import_budget_delta.py" in ln]
    return line


def test_the_release_runs_the_budget_delta_right_after_check_python():
    """Early and failing: right after check-python, before coverage, a bare `$(MAKE)` line."""
    text = (CHECKOUT_ROOT / "Makefile").read_text()
    _prerequisites, recipe = _rule(text, "release")
    stages = re.findall(r"\$\(MAKE\) ([a-z-]+)", " ".join(recipe))
    assert stages.index("budget-delta") == stages.index("check-python") + 1, stages
    # The coverage stamp check (its `$(MAKE) coverage` fallback) sits between them.
    assert stages.index("coverage") == stages.index("budget-delta") + 1, stages
    assert stages.index("nox-release") == stages.index("coverage") + 1, stages
    # A link of the `&&` chain, continued: its failure stops everything after it.
    [line] = [ln for ln in recipe if "budget-delta" in ln]
    assert line == "&& $(MAKE) budget-delta \\", line
    previous = recipe[recipe.index(line) - 1]
    assert previous.endswith(" \\"), previous


def test_budget_delta_measures_the_nox_interpreters_primary_first(tmp_path: Path):
    stub, _log = _stub_make(tmp_path, {})
    completed = _run_make(stub, "-n", "budget-delta")
    assert completed.returncode == 0, completed.stderr
    line = _delta_line(completed.stdout)
    assert " measure " in line
    assert "--minors 3.10 3.11 3.12 3.13 3.14" in line, line
    assert "--approve" not in line


def test_the_approval_variable_reaches_the_script_quoted_from_the_command_line_only(
    tmp_path: Path, monkeypatch
):
    stub, _log = _stub_make(tmp_path, {})
    given = _run_make(stub, "-n", "budget-delta", "BUDGET_APPROVE=help@3.14=stdlib+4 run=otto:*+1")
    assert "--approve 'help@3.14=stdlib+4' 'run=otto:*+1'" in _delta_line(given.stdout)
    monkeypatch.setenv("BUDGET_APPROVE", "help")
    assert "--approve" not in _delta_line(_run_make(stub, "-n", "budget-delta").stdout), (
        "an environment value is not an approval"
    )
    empty = _run_make(stub, "-n", "budget-delta", "BUDGET_APPROVE=")
    assert "--approve" not in _delta_line(empty.stdout), (
        "an empty command-line value approves nothing"
    )


def test_the_approval_variable_is_the_release_s_own_knob_even_with_spaces(tmp_path: Path):
    """`make release BUDGET_APPROVE="a=x+4 b=y+1"` must still trust (and make) the stamp."""
    stub, _log = _stub_make(tmp_path, {})
    completed = _run_make(
        stub, "-n", "coverage-stamp-start", "BUDGET_APPROVE=help=stdlib+4 run=otto:*+1"
    )
    assert completed.returncode == 0, completed.stderr
    assert _overridden(completed.stdout) == ["MAKE"], completed.stdout


def test_a_multi_word_override_of_any_other_name_is_marked_once(tmp_path: Path):
    """The old `$(MAKEOVERRIDES)` splitter turned `A=x y=z` into names `A` and `y`."""
    stub, _log = _stub_make(tmp_path, {})
    completed = _run_make(stub, "-n", "coverage-stamp-start", "PYTEST_ADDOPTS=-k x y=z")
    assert _overridden(completed.stdout) == ["MAKE", "PYTEST_ADDOPTS"], completed.stdout


def test_budget_delta_is_phony():
    assert "budget-delta" in _phony_targets((CHECKOUT_ROOT / "Makefile").read_text())


def test_gate_tip_targets_are_phony():
    phony = _phony_targets((CHECKOUT_ROOT / "Makefile").read_text())
    assert {"gate-tip", "gate-status", "land"} <= phony


def _recipe(target: str) -> str:
    text = (CHECKOUT_ROOT / "Makefile").read_text()
    match = re.search(rf"^{re.escape(target)}:[^\n]*\n((?:\t[^\n]*\n)+)", text, re.MULTILINE)
    assert match, f"no recipe for {target}"
    return match.group(1).strip("\t\n")


def test_the_gate_tip_recipe_and_its_overrides_are_the_declared_wiring():
    assert _recipe("gate-tip") == (
        "@uv run --locked python scripts/gate_tip.py --repo . --lab $(LAB_FILE) "
        f"--remote {gt.CANONICAL_REMOTE} "
        '--base "$(if $(filter command line,$(origin BASE)),$(BASE),)" '
        "$(GATE_TIP_OVERRIDES)"
    )
    text = (CHECKOUT_ROOT / "Makefile").read_text()
    assert (
        "GATE_TIP_OVERRIDES = $(foreach n,$(filter-out BASE $(COVERAGE_STAMP_HARMLESS),"
        "$(COVERAGE_STAMP_OVERRIDE_NAMES)),--overridden $(n))\n"
    ) in text


@pytest.mark.parametrize("target", ["gate-tip", "gate-status"])
def test_the_gate_entry_points_pass_the_canonical_remote(target):
    """The Makefile names the remote the runner's default and the hook's note call canonical."""
    assert f" --remote {gt.CANONICAL_REMOTE} " in f"{_recipe(target)} "
    assert _recipe(target).count("--remote") == 1


def _make_with_uv_stub(tmp_path: Path, *args: str) -> "list[str]":
    """Run the real `make gate-tip` with `uv` stubbed to record its argv; return that argv."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    argv = tmp_path / "argv.txt"
    _uv_stub(bin_dir / "uv", f'printf \'%s\\n\' "$@" > "{argv}"')
    dropped = {"MAKEFLAGS", "MFLAGS", "MAKELEVEL"}
    lease_free = lease_free_environ(BED_LEASE_ROOT=str(tmp_path / "bed-lease"))
    env = {k: v for k, v in lease_free.items() if k not in dropped}
    env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
    completed = subprocess.run(
        ["make", "--no-print-directory", "gate-tip", *args],
        cwd=CHECKOUT_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
        env=env,
        start_new_session=True,
    )
    assert completed.returncode == 0, completed.stderr
    return argv.read_text().splitlines()


def test_gate_tip_hands_every_command_line_variable_to_the_runner_as_overridden(tmp_path: Path):
    lines = _make_with_uv_stub(tmp_path, "X=1")
    assert lines[:4] == ["run", "--locked", "python", "scripts/gate_tip.py"]
    assert "--pid" not in lines
    assert lines[lines.index("--remote") + 1] == "origin"
    assert lines[lines.index("--lab") + 1] == "tests/_fixtures/lab_data/tech1/lab.json"
    assert lines[lines.index("--overridden") + 1] == "X"
    assert lines[lines.index("--base") + 1] == "", "the empty default: the runner fetches main"


def test_the_release_s_knobs_do_not_reach_gate_tip_as_overrides(tmp_path: Path):
    lines = _make_with_uv_stub(tmp_path, "BUMP=minor")
    assert "--overridden" not in lines


def test_gate_tip_honours_base_from_the_command_line_and_does_not_call_it_an_override(
    tmp_path: Path,
):
    lines = _make_with_uv_stub(tmp_path, "BASE=upstream/main")
    assert lines[lines.index("--base") + 1] == "upstream/main"
    assert "--overridden" not in lines


# --- the release's order: cheap stages first (#660, #661) -------------------


def _stub_stamp(tmp_path: Path, code: int) -> Path:
    """A `coverage_stamp.py` stand-in that answers every `check` with *code*."""
    stub = tmp_path / "stamp-stub"
    stub.write_text(f"#!/bin/sh\nexit {code}\n", encoding="utf-8")
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
    return stub


def _uv_stub(path: Path, body: str) -> None:
    """Write a `uv` stand-in that runs *body* for every call but the bed lease's.

    The lease macro runs through `uv run python scripts/bed_lease.py`; a stub
    that swallowed it would end the lane on the lease (or record the lease's
    argv instead of the lane's), so that one call runs on this interpreter,
    against the root the caller's `BED_LEASE_ROOT` names. The script is this
    checkout's, whatever directory the recipe runs in.
    """
    path.write_text(
        "#!/bin/sh\n"
        'case "$1 $2 $3" in "run python scripts/bed_lease.py") shift 3; '
        f'exec "{sys.executable}" "{CHECKOUT_ROOT}/scripts/bed_lease.py" "$@" ;; esac\n'
        f"{body}\n",
        encoding="utf-8",
    )
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


_RECORD_CALL = '"run python scripts/release_record.py"'
"""The `case` word of a `uv` stub's dispatch on the release record's calls."""


def _run_release(tmp_path: Path, stub: Path, stamp: int) -> "subprocess.CompletedProcess[str]":
    """Run the real `release` recipe with `$(MAKE)` and the stamp check stubbed.

    `VENV_BIN` is the recipe's own PATH prefix, so the guard stubs here shadow
    the real `uv`, `git-cliff` and `bump-my-version`: even a regressed recipe
    that ran past every stub could not bump, commit or tag this checkout. Each
    guard logs its name to :func:`_guard_log` and fails. The release record's
    calls are the exception: `uv` appends `release_record-<verb>` to the make
    stub's log, so they read as stages, and succeeds (this checkout's
    `reports/` is not a test's to write).
    """
    guard = tmp_path / "guard"
    guard.mkdir()
    guard_log = _guard_log(tmp_path)
    calls_log = stub.parent / "calls.log"
    record = (
        f'case "$1 $2 $3" in {_RECORD_CALL}) '
        f'echo "release_record-$4" >> "{calls_log}"; exit 0 ;; esac\n'
    )
    for tool in ("uv", "git-cliff", "bump-my-version"):
        prefix = record if tool == "uv" else ""
        _uv_stub(guard / tool, f'{prefix}echo {tool} >> "{guard_log}"\nexit 97')
    return _run_make(
        stub,
        "release",
        f"COVERAGE_STAMP={_stub_stamp(tmp_path, stamp)}",
        f"VENV_BIN={guard}",
    )


def _guard_log(tmp_path: Path) -> Path:
    return tmp_path / "guard.log"


def _calls(log: Path) -> "list[str]":
    return _targets_called(log) if log.exists() else []


def test_a_stamp_refusal_stops_the_release_before_anything_runs(tmp_path: Path):
    """Exit 2 is a refusal no stamp can cure: it is said before anything is built or cleared."""
    stub, log = _stub_make(tmp_path, {})

    completed = _run_release(tmp_path, stub, 2)

    assert completed.returncode != 0
    assert _calls(log) == []
    assert not _guard_log(tmp_path).exists()


def test_the_cheap_stages_run_before_coverage_and_a_red_coverage_stops_the_rest(tmp_path: Path):
    """No stamp (exit 1) and a coverage that cannot stamp: the release stops there.

    It also proves the order: everything cheap has already run, and nothing
    expensive after coverage has started.
    """
    stub, log = _stub_make(tmp_path, {})

    completed = _run_release(tmp_path, stub, 1)

    assert completed.returncode != 0
    assert _calls(log) == [
        "release-preflight",
        "release_record-pending",
        "clean-dist",
        "web-install",
        "docs",
        "check-python",
        "budget-delta",
        "coverage",
    ]
    assert "no valid coverage stamp" in completed.stdout, "the wait for coverage is said up front"
    assert not _guard_log(tmp_path).exists()


def test_a_valid_stamp_skips_coverage_and_the_release_goes_on(tmp_path: Path):
    """Exit 0, read once at the start, is trusted at the coverage stage.

    nox-release is failed so the run stops right after the stage under test.
    """
    stub, log = _stub_make(tmp_path, {"nox-release": 1})

    completed = _run_release(tmp_path, stub, 0)

    assert completed.returncode != 0
    calls = _calls(log)
    assert "coverage" not in calls, calls
    assert calls[-2:] == ["budget-delta", "nox-release"], calls
    assert "no valid coverage stamp" not in completed.stdout
    assert not _guard_log(tmp_path).exists()


def test_a_red_preflight_stops_the_release_before_anything_else(tmp_path: Path):
    stub, log = _stub_make(tmp_path, {"release-preflight": 1})

    completed = _run_release(tmp_path, stub, 1)

    assert completed.returncode != 0
    assert _calls(log) == ["release-preflight"]
    assert not _guard_log(tmp_path).exists()


def test_a_red_early_docs_build_stops_the_release_before_coverage(tmp_path: Path):
    stub, log = _stub_make(tmp_path, {"docs": 1})

    completed = _run_release(tmp_path, stub, 1)

    assert completed.returncode != 0
    assert _calls(log)[-1] == "docs"
    assert "coverage" not in _calls(log)


def test_the_release_builds_the_docs_again_after_it_re_measures_the_matrices():
    """The early build is a probe; the final one renders the committed matrices."""
    _prerequisites, recipe = _rule((CHECKOUT_ROOT / "Makefile").read_text(), "release")
    stages = re.findall(r"\$\(MAKE\) ([a-z-]+)", " ".join(recipe))
    assert stages.count("docs") == 2, stages
    last_docs = len(stages) - 1 - stages[::-1].index("docs")
    assert stages.index("release-kmodcov-matrix") < last_docs, stages
    assert stages.index("release-campaign") < last_docs, stages


# --- the release's landing: a certified record and a pull request ------------


@dataclass
class _Tail:
    """One run of the real `release` recipe in a scratch repo (:func:`_run_release_tail`)."""

    completed: "subprocess.CompletedProcess[str]"
    calls: "list[str]"
    """The `$(MAKE)` goals, in order."""
    repo: TmpGitRepo
    start_sha: str
    """`origin/release/next` before the run."""
    gh_calls: "list[list[str]]"
    guard_log: str
    """What the `uv`, `git-cliff` and `bump-my-version` stand-ins were asked, one line each."""


def _remote_sha(repo: TmpGitRepo, branch: str) -> str:
    return repo.git("ls-remote", "origin", f"refs/heads/{branch}").split()[0]


def _scratch_release_repo(root: Path) -> TmpGitRepo:
    """`release/next` at main's tip, both pushed to a bare `origin`; 0.18.1, untagged."""
    repo = TmpGitRepo(root / "scratch")
    repo.write("pyproject.toml", '[tool.bumpversion]\ncurrent_version = "0.18.1"\ntag = false\n')
    repo.write("CHANGELOG.md", "# unchanged\n")
    repo.commit("base")
    bare = root / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True, env=git_env(root))
    repo.git("remote", "add", "origin", str(bare))
    repo.git("push", "-q", "origin", "main")
    repo.git("checkout", "-q", "-b", "release/next")
    repo.git("push", "-q", "-u", "origin", "release/next")
    return repo


def _script(path: Path, body: str) -> None:
    path.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _tail_guard(
    guard: Path,
    *,
    census: str,
    changelog: Path,
    guard_log: Path,
    red_body: bool = False,
    lose_origin: "Path | None" = None,
) -> None:
    """The tail's tools: the real record script, a fixed census, and stand-ins for the rest.

    `git-cliff` writes *changelog* to its `-o` path (or prints it; with *red_body*, its
    `--unreleased` call, the pull request's body, prints half and fails); `bump-my-version`
    rewrites `current_version` and commits, never tagging, as `tag = false` says. With
    *lose_origin*, that bare repository is deleted just before the record's `landing`
    call, so the real script's fetch of main fails there.
    """
    lose = f'if [ "$4" = landing ]; then rm -rf "{lose_origin}"; fi; ' if lose_origin else ""
    _uv_stub(
        guard / "uv",
        'case "$1 $2 $3" in\n'
        f'  "run python scripts/release_bump.py") echo "{census}"; exit 0 ;;\n'
        f'  {_RECORD_CALL}) {lose}shift 3; PYTHONPATH="{CHECKOUT_ROOT}" exec "{sys.executable}" '
        f'"{CHECKOUT_ROOT}/scripts/release_record.py" "$@" ;;\n'
        "esac\n"
        f'echo "uv $*" >> "{guard_log}"\n'
        "exit 97",
    )
    _script(
        guard / "git-cliff",
        f'echo "git-cliff $*" >> "{guard_log}"\n'
        + (
            'case " $* " in *" --unreleased "*) echo "# half a body"; exit 1 ;; esac\n'
            if red_body
            else ""
        )
        + 'out=""\n'
        'while [ $# -gt 0 ]; do if [ "$1" = -o ]; then out="$2"; fi; shift; done\n'
        f'if [ -n "$out" ]; then cp "{changelog}" "$out"; else cat "{changelog}"; fi',
    )
    _script(
        guard / "bump-my-version",
        f'echo "bump-my-version $*" >> "{guard_log}"\n'
        'new=""\n'
        'while [ $# -gt 0 ]; do if [ "$1" = --new-version ]; then new="$2"; fi; shift; done\n'
        'sed -i "s/^current_version = .*/current_version = \\"$new\\"/" pyproject.toml\n'
        'git commit -qam "bump: v$new"',
    )
    install_fake_gh(guard, [GhReply(["pr", "create"], stdout="https://x/pull/1\n")])


def _run_release_tail(
    tmp_path: Path,
    *,
    census: str,
    changelog: str,
    make_statuses: "dict[str, int] | None" = None,
    repo: "TmpGitRepo | None" = None,
    red_body: bool = False,
    lose_origin: bool = False,
) -> _Tail:
    """Run the real `release` recipe in a scratch repo, every `$(MAKE)` goal stubbed.

    `-C <scratch>` makes every `git`, `reports/` and `pyproject.toml` the recipe
    touches the scratch repo's; the lease root is the stub's, so the real cell is
    never touched. *repo* reuses an earlier call's scratch repo for a second run;
    *red_body* fails the `git-cliff` call that renders the pull request's body;
    *lose_origin* deletes the scratch origin just before the `landing` probe.
    """
    run_dir = tmp_path / f"run-{len(list(tmp_path.glob('run-*')))}"
    run_dir.mkdir()
    repo = repo or _scratch_release_repo(tmp_path)
    start_sha = _remote_sha(repo, "release/next")
    stub, log = _stub_make(run_dir, make_statuses or {})
    guard = run_dir / "guard"
    guard.mkdir()
    source = run_dir / "changelog.md"
    source.write_text(changelog, encoding="utf-8")
    guard_log = _guard_log(run_dir)
    _tail_guard(
        guard,
        census=census,
        changelog=source,
        guard_log=guard_log,
        red_body=red_body,
        lose_origin=tmp_path / "origin.git" if lose_origin else None,
    )
    completed = _run_make(
        stub,
        "-C",
        str(repo.root),
        "-f",
        str(CHECKOUT_ROOT / "Makefile"),
        "release",
        f"LAB_FILE={CHECKOUT_ROOT}/tests/_fixtures/lab_data/tech1/lab.json",
        f"COVERAGE_STAMP={_stub_stamp(run_dir, 0)}",
        f"VENV_BIN={guard}",
        # Merged, not verbatim: the recipe's Python children need the session's PATH and HOME.
        env={k: v for k, v in git_env(repo.root).items() if k not in {"PATH", "HOME"}},
    )
    return _Tail(
        completed=completed,
        calls=_calls(log),
        repo=repo,
        start_sha=start_sha,
        gh_calls=gh_calls(guard),
        guard_log=guard_log.read_text() if guard_log.exists() else "",
    )


def _state(repo: TmpGitRepo) -> str:
    record = rr.read(repo.root)
    assert record is not None
    return record.state


def test_a_recertification_with_an_unchanged_changelog_certifies_the_tip_and_merges_nothing(
    tmp_path: Path,
):
    t = _run_release_tail(tmp_path, census="0.18.1", changelog="# unchanged\n")
    assert t.completed.returncode == 0, t.completed.stdout + t.completed.stderr
    assert _state(t.repo) == "tip-certified"
    assert "run: make release-tag" in t.completed.stdout
    assert "gate-tip" not in t.calls
    assert t.gh_calls == []
    assert t.repo.git("tag", "--list") == ""
    assert _remote_sha(t.repo, "release/next") == t.start_sha


def test_a_recertification_with_a_regenerated_changelog_commits_it_and_lands_through_a_pull_request(
    tmp_path: Path,
):
    t = _run_release_tail(tmp_path, census="0.18.1", changelog="# regenerated\n")
    assert t.completed.returncode == 0, t.completed.stdout + t.completed.stderr
    assert t.calls[-2:] == ["gate-tip", "gate-status"]
    assert t.gh_calls[0][:3] == ["pr", "create", "--title"]
    assert t.gh_calls[0][-2:] == ["--body-file", "-"], "the body arrives on stdin: no temp file"
    assert "chore(release): v0.18.1" in t.gh_calls[0]
    record = rr.read(t.repo.root)
    assert record is not None
    assert (record.state, record.url) == ("landed", "https://x/pull/1")
    subject = t.repo.git("log", "-1", "--format=%s").strip()
    assert subject == "docs(changelog): regenerate for v0.18.1"
    assert _remote_sha(t.repo, "release/next") == t.repo.git("rev-parse", "HEAD").strip()


def test_a_higher_census_bumps_without_a_tag_and_lands_through_a_pull_request(tmp_path: Path):
    t = _run_release_tail(tmp_path, census="0.18.2", changelog="# 0.18.2\n")
    assert t.completed.returncode == 0, t.completed.stdout + t.completed.stderr
    assert 'current_version = "0.18.2"' in (t.repo.root / "pyproject.toml").read_text()
    assert t.repo.git("tag", "--list") == ""
    assert _state(t.repo) == "landed"
    assert "chore(release): v0.18.2" in t.gh_calls[0]
    assert t.calls[-2:] == ["gate-tip", "gate-status"]


def test_a_census_below_the_untagged_bump_stops_before_the_changelog(tmp_path: Path):
    t = _run_release_tail(tmp_path, census="0.18.0", changelog="x")
    assert t.completed.returncode != 0
    assert _state(t.repo) == "pending"
    assert "git-cliff" not in t.guard_log
    assert "already carries the untagged bump" in t.completed.stderr


def test_a_red_wheel_check_leaves_the_record_pending_and_pushes_nothing(tmp_path: Path):
    t = _run_release_tail(
        tmp_path, census="0.18.2", changelog="x", make_statuses={"wheel-check": 1}
    )
    assert t.completed.returncode != 0
    assert _state(t.repo) == "pending"
    assert t.gh_calls == []
    assert "gate-tip" not in t.calls
    assert _remote_sha(t.repo, "release/next") == t.start_sha


def test_a_failed_recertification_invalidates_the_earlier_certificate(tmp_path: Path, capsys):
    # spec: a run that starts writes `pending` over whatever stood; a run that dies early
    # leaves it there, and release-tag refuses it
    t = _run_release_tail(tmp_path, census="0.18.1", changelog="# unchanged\n")  # certified
    assert _state(t.repo) == "tip-certified"
    again = _run_release_tail(
        tmp_path,
        census="0.18.1",
        changelog="# unchanged\n",
        make_statuses={"wheel-check": 1},
        repo=t.repo,
    )  # a second run in the same scratch repo
    assert again.completed.returncode != 0
    assert _state(t.repo) == "pending"
    rc = scripts.release_tag.release_tag(
        t.repo.root,
        remote="origin",
        required=[],
        wait_s=0,
        interval=1,
        gh=lambda args: pytest.fail(f"gh called: {args}"),
    )
    # judged before the remote: the filesystem origin is never resolved
    assert rc == 1
    assert "the record is pending; the last make release did not finish" in capsys.readouterr().err


def test_a_red_pull_request_body_leaves_the_record_certified_and_opens_nothing(tmp_path: Path):
    """A failing git-cliff for the body stops the chain: no pull request with half a body."""
    t = _run_release_tail(tmp_path, census="0.18.2", changelog="x", red_body=True)
    assert t.completed.returncode != 0
    assert "--unreleased" in t.guard_log, "the body's git-cliff call is the one that failed"
    assert _state(t.repo) == "certified"
    assert t.gh_calls == []


def test_a_red_gate_tip_after_certification_leaves_the_record_certified_and_opens_nothing(
    tmp_path: Path,
):
    t = _run_release_tail(tmp_path, census="0.18.2", changelog="x", make_statuses={"gate-tip": 1})
    assert t.completed.returncode != 0
    assert _state(t.repo) == "certified"
    assert t.gh_calls == []
    assert _remote_sha(t.repo, "release/next") == t.start_sha


def test_a_landing_probe_that_cannot_fetch_main_stops_with_the_record_certified(tmp_path: Path):
    """A failed fetch is not "something to merge": the chain stops, nothing is pushed."""
    t = _run_release_tail(tmp_path, census="0.18.1", changelog="# unchanged\n", lose_origin=True)
    assert t.completed.returncode != 0
    assert _state(t.repo) == "certified"
    assert "gate-tip" not in t.calls
    assert t.gh_calls == []
    assert "release-record: origin has no main" in t.completed.stderr


_TAIL_SEQUENCE = [
    "release_bump.py",
    "release_record.py mode",
    "git-cliff --tag",
    "bump-my-version bump",
    "$(MAKE) wheel-check",
    "release_record.py certify",
    "release_record.py landing --remote origin",
    '[ "$$NEXT" = tip-certified ]',
    "release_record.py tip-certified",
    "$(MAKE) gate-tip",
    "git push -u origin",
    "$(MAKE) gate-status",
    "--unreleased --strip header",
    'gh pr create --title "chore(release): v$$NEW_VERSION" --body-file',
    "release_record.py landed",
]


def test_the_release_tail_is_the_declared_landing_sequence():
    recipe = _recipe("release")
    second_docs = recipe.index("$(MAKE) docs", recipe.index("$(MAKE) docs") + 1)
    tail = recipe[second_docs:]
    at = [tail.find(step) for step in _TAIL_SEQUENCE]
    assert -1 not in at, [s for s, i in zip(_TAIL_SEQUENCE, at, strict=True) if i == -1]
    assert at == sorted(at), sorted(zip(at, _TAIL_SEQUENCE, strict=True))
    assert recipe.index("release_record.py pending") < recipe.index("clean-dist")
    assert "--follow-tags" not in recipe
    assert "git tag" not in recipe
    assert recipe.splitlines()[-1].strip() == "$(call bed_release)"


def test_bump_my_version_does_not_tag():
    with (CHECKOUT_ROOT / "pyproject.toml").open("rb") as handle:
        assert tomli.load(handle)["tool"]["bumpversion"]["tag"] is False


def test_release_tag_is_phony_and_locked():
    assert "release-tag" in _phony_targets((CHECKOUT_ROOT / "Makefile").read_text())
    assert (
        _recipe("release-tag") == "@uv run --locked python scripts/release_tag.py --remote origin"
    )
