"""CI's check-ts job must invoke the TS gates, not re-list their internals.

The job used to hand-list `web-check`'s sub-targets and the list silently
drifted from the gate it was copying: `biome lint` + `biome format` do NOT
report Biome's ASSIST actions (organize-imports), so unsorted imports passed
CI while failing `biome check`. The web-check umbrella was later folded into
the language-parity family (spec 2026-07-17-makefile-quality-parity): the
job now calls `check-ts` (whose lint leg IS `biome check`) plus the vitest
unit floor `coverage-ts-unit`. These pins keep both the CI invocation and
the Makefile chain from drifting back to something weaker.
"""

import json
import re

import pytest
import yaml

from tests._fixtures.paths import PROJECT_ROOT

pytestmark = pytest.mark.interpreter_agnostic

_REPO = PROJECT_ROOT
_MAKEFILE = (_REPO / "Makefile").read_text()


def _check_ts_job_runs() -> list[str]:
    ci = yaml.safe_load((_REPO / ".github" / "workflows" / "ci.yml").read_text())
    steps = ci["jobs"]["check-ts"]["steps"]
    return [step["run"] for step in steps if "run" in step]


def test_ci_invokes_the_ts_gates_not_their_internals() -> None:
    runs = _check_ts_job_runs()
    assert runs == ["make check-ts coverage-ts-unit"], (
        "CI's check-ts job must invoke `make check-ts coverage-ts-unit` — "
        "the browserless TS gates — in ONE step, not re-list any gate's "
        f"internals (drift risk). Got: {runs!r}"
    )


def test_check_ts_chain_reaches_biome_check() -> None:
    """Pins the chain: check-ts -> lint-ts -> `npm run check` (biome check)."""
    check_ts = re.search(r"^check-ts:([^\n#]*)", _MAKEFILE, re.MULTILINE)
    assert check_ts, "no `check-ts` target in the Makefile"
    assert "lint-ts" in check_ts.group(1), (
        "`check-ts` no longer depends on `lint-ts`, so CI is not running the "
        "authoritative Biome gate"
    )
    lint_ts = re.search(r"^lint-ts:.*(?:\n\t.+)+", _MAKEFILE, re.MULTILINE)
    assert lint_ts, "no `lint-ts` target in the Makefile"
    assert "npm run check" in lint_ts.group(0), (
        "`lint-ts` must run `npm run check` (biome check = rules + format + "
        "assists); anything weaker reopens the organize-imports gap"
    )
    assert "npm run knip" in lint_ts.group(0), (
        "`lint-ts` must also run knip — the project-scope unused-code parity "
        "for what ruff already does on the Python side"
    )
    package_json = json.loads((_REPO / "web" / "package.json").read_text())
    assert package_json["scripts"]["check"].startswith("biome check"), (
        "web/package.json's `check` script no longer runs `biome check` — "
        "the Makefile chain now bottoms out in something weaker"
    )


def test_check_ts_gates_the_vendored_untitledui_tree() -> None:
    """Pins check-ts's vendored-source leg: the never-hand-edited gate.

    Untitled UI is copy-in source, so scripts/check_untitledui_drift.sh --
    the weekly, networked half of the contract -- cannot tell "upstream
    changed" from "we edited it": it is a one-directional content diff, so a
    local hand-edit reads there as UPSTREAM drift, forever, under a title
    naming the wrong culprit (issue #177). The cheap half is a network-free
    contentHash recompute, and it only earns its keep by running on every
    push -- i.e. by staying wired into check-ts, which is what CI invokes.
    Spans the whole recipe, not just the prerequisite line, for the same
    reason test_coverage_ts_unit_runs_the_vitest_floor does.
    """
    check_ts = re.search(r"^check-ts:.*(?:\n\t.+)+", _MAKEFILE, re.MULTILINE)
    assert check_ts, "no `check-ts` target with a recipe in the Makefile"
    assert "scripts/check_untitledui_hash.sh" in check_ts.group(0), (
        "`check-ts` must run scripts/check_untitledui_hash.sh — without it a "
        "hand-edit to web/src/components/** is only ever reported by the "
        "WEEKLY drift check, as upstream drift, under the wrong title"
    )
    script = _REPO / "scripts" / "check_untitledui_hash.sh"
    assert script.is_file(), f"{script} is referenced by check-ts but missing"
    assert script.stat().st_mode & 0o111, (
        f"{script} is not executable, so check-ts's recipe cannot run it"
    )


def test_coverage_ts_unit_runs_the_vitest_floor() -> None:
    # Spans the WHOLE recipe (like test_check_ts_chain_reaches_biome_check
    # above), not just its first line: every Makefile recipe now opens with a
    # $(SAY) banner, so a first-line-only match would read the banner and miss
    # the command it is announcing.
    cov = re.search(r"^coverage-ts-unit:.*(?:\n\t.+)+", _MAKEFILE, re.MULTILINE)
    assert cov, "no `coverage-ts-unit` target in the Makefile"
    assert "scripts/build_web_no_warnings.sh test:coverage" in cov.group(0), (
        "`coverage-ts-unit` must enforce the vitest unit-tier coverage floor, "
        "through the no-stderr warnings gate"
    )


def _vitest_scripts() -> list[str]:
    """web/package.json scripts that run vitest (test, test:coverage, ...)."""
    scripts = json.loads((_REPO / "web" / "package.json").read_text())["scripts"]
    names = sorted(name for name, command in scripts.items() if "vitest" in command)
    assert names, "no web/package.json script runs vitest any more"
    return names


def test_every_vitest_run_goes_through_the_warnings_gate() -> None:
    """vitest.setup.ts fails a test on console.warn, but process.emitWarning,
    beforeAll/afterAll output and vitest's own messages slip past it; only the
    no-stderr gate (scripts/build_web_no_warnings.sh) catches those.

    Any recipe line that could start vitest counts: a direct `vitest`/`npx
    vitest`, `npm test`/`npm t`, or `npm run`/`npm run-script` of a script
    that runs vitest, whatever flags (`--prefix web`, ...) sit in between.
    """
    names = "|".join(re.escape(name) for name in _vitest_scripts())
    runs_vitest = re.compile(
        # vitest as a command word (bare, npx, node_modules/.bin/), not as
        # part of a file name such as reports/ts-cov/final/vitest.json
        r"(?<![\w.-])vitest(?![\w.-])"
        r"|\bnpm\b.*\b(?:test|t)\b"
        rf"|\bnpm\b.*\brun(?:-script)?\s+(?:{names})(?![\w:-])"
    )
    recipe_lines = re.findall(r"^\t.*$", _MAKEFILE, re.MULTILINE)
    bare = [
        line.strip()
        for line in recipe_lines
        if "$(SAY)" not in line
        and runs_vitest.search(line)
        and "scripts/build_web_no_warnings.sh" not in line
    ]
    assert not bare, (
        "a Makefile recipe runs the vitest suite outside the no-stderr gate; "
        f"route it through scripts/build_web_no_warnings.sh instead: {bare!r}"
    )
    gated = re.findall(r"^\t@scripts/build_web_no_warnings\.sh (\S+)$", _MAKEFILE, re.MULTILINE)
    assert set(_vitest_scripts()) <= set(gated), (
        f"not every vitest script runs through the gate: {gated!r}"
    )
