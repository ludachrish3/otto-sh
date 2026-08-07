"""G12: the chaos and embedded-coverage e2e lanes fail loud or pass — never skip.

Both lanes certify a bed. A skip inside them is a retired lane hiding behind
green: the chaos suite "passes" while probing nothing, the embedded-coverage
run "passes" with no product built. That is the house host-down rule — never
skip on host-down, fail with a host-named error — extended to build and
config absence, and it is enforced here structurally: an AST scan of both
trees for the known skip spellings — ``pytest.skip`` / ``importorskip`` /
``mark.skip`` / ``mark.skipif`` (dotted, with or without the ``pytest.``
prefix) and the ``from pytest import skip|skipif|importorskip`` alias
alley — asserted to zero. The guard lives in the no-VM unit gate so it
fires on every PR, not just when the lanes run. It is a tripwire, not a
proof system; stated blind spots (all grepped zero in-tree at landing):
``import pytest as pt``, marker injection via
``request.node.add_marker("skip")`` strings, ``raise unittest.SkipTest``,
imperative ``pytest.xfail``, and skips inherited from out-of-tree parent
conftests or fixtures.

``tests/repo3/tests/test_embedded_coverage.py`` keeps its skip: repo3 is
fixture SUT data (a user-example repo the suite runs *as input*), not otto's
own tests — the same carve-out every tests-scoped ast-grep rule makes. The
*runner* of that lane (``tests/e2e/cov/test_embedded_coverage_e2e.py``) is in
scope and hard-fails on missing config or artifact instead.
"""

import ast

import pytest

from tests._fixtures.paths import PROJECT_ROOT, TESTS_ROOT

pytestmark = pytest.mark.hostless

_NO_SKIP_LANES = ("e2e/chaos", "e2e/cov")

# Dotted spellings that put a skip in a lane, and the bare names whose import
# from pytest would open an alias alley around the dotted scan.
_BANNED_DOTTED = {
    "pytest.skip",
    "pytest.importorskip",
    "pytest.mark.skip",
    "pytest.mark.skipif",
    # The bare forms catch `from pytest import mark` and `mark = pytest.mark`
    # rebindings at the USAGE site — the alias alley the import ban below
    # cannot see (fable's final-review find).
    "mark.skip",
    "mark.skipif",
}
_BANNED_PYTEST_IMPORTS = {"skip", "skipif", "importorskip"}


def _dotted(node: ast.AST) -> str:
    """Reconstruct a dotted name from an Attribute/Name chain ('' if neither)."""
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return ""
    parts.append(node.id)
    return ".".join(reversed(parts))


def _skip_sites(source: str, label: str) -> list[str]:
    """Return ``label:line: spelling`` for every skip construct in *source*."""
    sites: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Attribute):
            dotted = _dotted(node)
            if dotted in _BANNED_DOTTED:
                sites.append(f"{label}:{node.lineno}: {dotted}")
        elif isinstance(node, ast.ImportFrom) and node.module == "pytest":
            # `from pytest import skip` would make bare `skip(...)` invisible
            # to the dotted scan — ban the import itself.
            sites.extend(
                f"{label}:{node.lineno}: from pytest import {alias.name}"
                for alias in node.names
                if alias.name in _BANNED_PYTEST_IMPORTS
            )
    return sites


@pytest.mark.parametrize("lane", _NO_SKIP_LANES)
def test_lane_never_skips(lane: str) -> None:
    lane_dir = TESTS_ROOT / lane
    files = sorted(lane_dir.rglob("*.py"))
    # Anti-vacuity: an empty or moved lane must fail here, not pass silently —
    # the exact stale-skip shape this wave also retires from the tier-marker
    # invariants ("not created yet" guarding a tree that has existed for weeks).
    assert files, f"tests/{lane} has no Python files — the no-skip guard is scanning nothing"
    sites = [
        site for f in files for site in _skip_sites(f.read_text(), str(f.relative_to(PROJECT_ROOT)))
    ]
    assert not sites, (
        f"tests/{lane} must fail loud or pass — a skip there is a retired lane "
        "hiding behind green (G12; the host-down rule extended to build/config "
        "absence). Convert to pytest.fail naming what is missing:\n  " + "\n  ".join(sites)
    )


# Positive control: every banned spelling, embedded verbatim. If the detector
# goes blind to any of them, this fails before the lane scan can lie.
_CONTROL = """\
import pytest
from pytest import importorskip, mark


@pytest.mark.skipif(True, reason="control")
def test_a():
    pytest.skip("control")


@pytest.mark.skip
def test_b():
    tomli = pytest.importorskip("tomli")


@mark.skipif(True, reason="control")
def test_c():
    ...


@mark.skip
def test_d():
    ...
"""


def test_detector_sees_every_spelling() -> None:
    found = _skip_sites(_CONTROL, "control")
    spellings = {site.split(": ", 1)[1] for site in found}
    assert spellings == {
        "pytest.skip",
        "pytest.importorskip",
        "pytest.mark.skip",
        "pytest.mark.skipif",
        "mark.skip",
        "mark.skipif",
        "from pytest import importorskip",
    }, f"detector went blind to a spelling: {sorted(spellings)}"
