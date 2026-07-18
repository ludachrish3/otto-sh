"""CI's web-quality job must invoke the TS gates, not re-list their internals.

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
from pathlib import Path

import yaml

_REPO = Path(__file__).resolve().parent.parent.parent
_MAKEFILE = (_REPO / "Makefile").read_text()


def _web_quality_runs() -> list[str]:
    ci = yaml.safe_load((_REPO / ".github" / "workflows" / "ci.yml").read_text())
    steps = ci["jobs"]["web-quality"]["steps"]
    return [step["run"] for step in steps if "run" in step]


def test_ci_invokes_the_ts_gates_not_their_internals() -> None:
    runs = _web_quality_runs()
    assert runs == ["make check-ts coverage-ts-unit"], (
        "CI's web-quality job must invoke `make check-ts coverage-ts-unit` — "
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


def test_coverage_ts_unit_runs_the_vitest_floor() -> None:
    cov = re.search(r"^coverage-ts-unit:.*\n\t(.+)$", _MAKEFILE, re.MULTILINE)
    assert cov, "no `coverage-ts-unit` target in the Makefile"
    assert "npm run test:coverage" in cov.group(1), (
        "`coverage-ts-unit` must enforce the vitest unit-tier coverage floor"
    )
