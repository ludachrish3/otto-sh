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

from scripts import gate_tip as gt
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
    text = (PROJECT_ROOT / "Makefile").read_text()
    prerequisites, recipe = _rule(text, "coverage")
    assert prerequisites == ["coverage-stamp-start", "coverage-python", "coverage-ts"], (
        prerequisites
    )
    assert recipe == ["@$(COVERAGE_STAMP) finish"], recipe
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
        f"include {PROJECT_ROOT / 'Makefile'}\n"
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
        cwd=PROJECT_ROOT,
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
    text = (PROJECT_ROOT / "Makefile").read_text()
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
    probe.write_text(f"include {PROJECT_ROOT / 'Makefile'}\n")
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
        cwd=PROJECT_ROOT,
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
    phony = _phony_targets((PROJECT_ROOT / "Makefile").read_text())
    missing = {"coverage", "coverage-stamp-start", "nox-release", "release"} - phony
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
        cwd=PROJECT_ROOT,
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
    """Early and failing: between check-python and nox-release, a bare `$(MAKE)` line."""
    text = (PROJECT_ROOT / "Makefile").read_text()
    _prerequisites, recipe = _rule(text, "release")
    stages = re.findall(r"\$\(MAKE\) ([a-z-]+)", " ".join(recipe))
    assert stages.index("budget-delta") == stages.index("check-python") + 1, stages
    assert stages.index("nox-release") == stages.index("budget-delta") + 1, stages
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
    assert "budget-delta" in _phony_targets((PROJECT_ROOT / "Makefile").read_text())


def test_gate_tip_targets_are_phony():
    phony = _phony_targets((PROJECT_ROOT / "Makefile").read_text())
    assert {"gate-tip", "coverage-gate-args"} <= phony


def _recipe(target: str) -> str:
    text = (PROJECT_ROOT / "Makefile").read_text()
    match = re.search(rf"^{re.escape(target)}:[^\n]*\n((?:\t[^\n]*\n)+)", text, re.MULTILINE)
    assert match, f"no recipe for {target}"
    return match.group(1).strip("\t\n")


def test_the_gate_tip_recipe_and_its_overrides_are_the_declared_wiring():
    assert _recipe("gate-tip") == (
        '@uv run python scripts/gate_tip.py --repo . --base "$(if $(filter command line,'
        '$(origin BASE)),$(BASE),origin/main)" $(GATE_TIP_OVERRIDES)'
    )
    text = (PROJECT_ROOT / "Makefile").read_text()
    assert (
        "GATE_TIP_OVERRIDES = $(foreach n,$(filter-out BASE,"
        "$(COVERAGE_STAMP_OVERRIDE_NAMES)),--overridden $(n))\n"
    ) in text
    assert _recipe("coverage-gate-args") == "@printf '%s\\n' $(COVERAGE_STAMP_GATE)"


def _make_with_uv_stub(tmp_path: Path, *args: str) -> "list[str]":
    """Run the real `make gate-tip` with `uv` stubbed to record its argv; return that argv."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    argv = tmp_path / "argv.txt"
    uv = bin_dir / "uv"
    uv.write_text(f'#!/bin/sh\nprintf \'%s\\n\' "$@" > "{argv}"\n')
    uv.chmod(uv.stat().st_mode | stat.S_IXUSR)
    dropped = {"MAKEFLAGS", "MFLAGS", "MAKELEVEL"}
    env = {k: v for k, v in os.environ.items() if k not in dropped}
    env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
    completed = subprocess.run(
        ["make", "--no-print-directory", "gate-tip", *args],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
        env=env,
    )
    assert completed.returncode == 0, completed.stderr
    return argv.read_text().splitlines()


def test_gate_tip_hands_every_command_line_variable_to_the_runner_as_overridden(tmp_path: Path):
    lines = _make_with_uv_stub(tmp_path, "X=1")
    assert lines[:3] == ["run", "python", "scripts/gate_tip.py"]
    assert lines[lines.index("--overridden") + 1] == "X"
    assert lines[lines.index("--base") + 1] == "origin/main"


def test_gate_tip_hands_a_release_knob_over_too(tmp_path: Path):
    lines = _make_with_uv_stub(tmp_path, "BUMP=minor")
    assert lines[lines.index("--overridden") + 1] == "BUMP"


def test_gate_tip_honours_base_from_the_command_line_and_does_not_call_it_an_override(
    tmp_path: Path,
):
    lines = _make_with_uv_stub(tmp_path, "BASE=upstream/main")
    assert lines[lines.index("--base") + 1] == "upstream/main"
    assert "--overridden" not in lines


def test_the_declared_coverage_gate_frames_as_four_option_value_lines():
    dropped = {"MAKEFLAGS", "MFLAGS", "MAKELEVEL"}
    env = {k: v for k, v in os.environ.items() if k not in dropped}
    completed = subprocess.run(
        ["make", "-s", "coverage-gate-args"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
        env=env,
    )
    assert completed.returncode == 0, completed.stderr
    lines = completed.stdout.split("\n")
    assert lines.pop() == ""
    assert len(lines) == 8, "a value holding a newline would break the shape"
    assert lines[0::2] == gt.COVERAGE_GATE_OPTIONS
    assert " " in lines[lines.index("--selection") + 1]
