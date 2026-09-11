"""The cross-Python matrix is tiered: bookends run everything, the middle runs the product.

``make nox`` runs the full suite on the PRIMARY (3.10, the floor) and CANARY
(3.14, the newest) interpreters, and a hostless leg on the versions between.
Measured on 2026-09-11, that hostless leg cost ~1,420 s of CPU per interior
Python, and 65 % of it was work whose subject is not otto-under-THIS-
interpreter at all: the ``tests/e2e`` tier (real ``otto`` subprocesses,
real ``pip`` venv builds), the repo-policy guards that re-collect the whole
tree in a subprocess, and the shim differential's 25,000-case corpus. Those
tests prove the same thing on every Python, so the interior versions now run
the ``tests/unit`` tier with the ``interpreter_agnostic`` marker deselected,
and the bookends keep the full hostless selection.

This module pins the shape so the trim can neither widen (a middle leg that
grows back the e2e tier, or drops a clause its bookend twin carries) nor
leak (the marker reaching a bookend expression, which would stop running
those tests ANYWHERE in the matrix). The marker's contract is stated by the
last guard: a carrier must declare it at module level and live under
``tests/unit``, the only tree the middle legs name — a carrier elsewhere is
a claim no lane ever reads.
"""

import ast
import re

import pytest

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # pragma: no cover - 3.10 only, otto's floor
    import tomli as tomllib

from tests._fixtures.paths import PROJECT_ROOT, TESTS_ROOT
from tests.unit.test_tier_marker_invariants import (
    _module_pytestmark_names,
    _python_pytest_invocations,
    _string_constants,
)

pytestmark = pytest.mark.interpreter_agnostic

_NOXFILE = PROJECT_ROOT / "noxfile.py"
_MARKER = "interpreter_agnostic"
_BOOKEND_PAIR = ("HOSTLESS_TEST_ARGS", "HOSTLESS_SERIAL_ARGS")
_MIDDLE_PAIR = ("HOSTLESS_MIDDLE_TEST_ARGS", "HOSTLESS_MIDDLE_SERIAL_ARGS")

# The carriers whose cost is the reason the trim exists. Measured 2026-09-11
# on the 3.11 hostless leg: 85 s, 58 s, 31 s, 26 s and 249 s respectively.
# A carrier that loses its marker silently gives the interior legs that cost
# back; naming these five here makes that a red instead.
_LOAD_BEARING_CARRIERS = (
    "unit/test_support_matrix.py",
    "unit/test_conformance_bed.py",
    "unit/test_lane_invariants.py",
    "unit/test_tier_marker_invariants.py",
    "unit/shim/test_differential.py",
)


def _bundles(tree: ast.Module) -> "dict[str, list[str]]":
    """Name -> literal string tokens for every module-level tuple/list assignment."""
    constants = _string_constants(tree)
    out: "dict[str, list[str]]" = {}
    for node in tree.body:
        if not (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and isinstance(node.value, (ast.Tuple, ast.List))
        ):
            continue
        tokens: "list[str]" = []
        for elt in node.value.elts:
            if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                tokens.append(elt.value)
            elif isinstance(elt, ast.Name) and elt.id in constants:
                tokens.append(constants[elt.id])
        out[node.targets[0].id] = tokens
    return out


def _marker_expr(tokens: "list[str]") -> str:
    assert "-m" in tokens, f"no -m in {tokens}"
    return tokens[tokens.index("-m") + 1]


def _clauses(expr: str) -> "set[str]":
    return {clause.strip() for clause in expr.split(" and ")}


def _roots(tokens: "list[str]") -> "tuple[str, ...]":
    return tuple(t for t in tokens if t.startswith("tests/"))


def middle_trim_gaps(bookend: str, middle: str) -> "list[str]":
    """Why *middle* is not a strict narrowing of its *bookend* twin.

    Pure, so the red arm is testable without editing noxfile.py: the middle
    expression must carry every clause the bookend one does (a dropped
    exclusion is a widening — a lane that fetches artifacts or selects the
    bed), plus ``not interpreter_agnostic`` (the trim itself), and the bookend
    must not carry the marker at all.
    """
    gaps: "list[str]" = []
    missing = _clauses(bookend) - _clauses(middle)
    if missing:
        gaps.append(f"middle leg dropped clauses its bookend twin carries: {sorted(missing)}")
    if f"not {_MARKER}" not in _clauses(middle):
        gaps.append(f"middle leg does not deselect `{_MARKER}`")
    if _MARKER in bookend:
        gaps.append(f"bookend leg mentions `{_MARKER}` — those tests would run nowhere")
    return gaps


@pytest.fixture(scope="module")
def noxfile_tree() -> ast.Module:
    return ast.parse(_NOXFILE.read_text())


def test_the_marker_is_registered() -> None:
    ini = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text())
    names = [m.split(":", 1)[0].strip() for m in ini["tool"]["pytest"]["ini_options"]["markers"]]
    assert _MARKER in names, f"`{_MARKER}` is not registered in pyproject.toml (strict markers)"


def test_the_bookends_are_the_floor_and_the_ceiling(noxfile_tree: ast.Module) -> None:
    """BOOKEND_PYTHONS is exactly the oldest and newest of PYTHON_VERSIONS."""
    bundles = _bundles(noxfile_tree)
    versions = bundles["PYTHON_VERSIONS"]
    key = lambda v: tuple(int(p) for p in v.split("."))  # noqa: E731 — one-line sort key
    assert bundles["BOOKEND_PYTHONS"] == [min(versions, key=key), max(versions, key=key)]


def test_the_middle_pair_narrows_its_bookend_twin(noxfile_tree: ast.Module) -> None:
    bundles = _bundles(noxfile_tree)
    for bookend_name, middle_name in zip(_BOOKEND_PAIR, _MIDDLE_PAIR, strict=True):
        bookend, middle = bundles[bookend_name], bundles[middle_name]
        assert middle_trim_gaps(_marker_expr(bookend), _marker_expr(middle)) == [], (
            bookend_name,
            middle_name,
        )
        assert _roots(middle) == ("tests/unit",), f"{middle_name} names {_roots(middle)}"
        assert "tests/e2e" in _roots(bookend), f"{bookend_name} lost the e2e tier"
        assert "--no-cov" in middle, f"{middle_name} still instruments coverage"
        assert "--cov-append" not in middle, f"{middle_name} appends to a report it never writes"


def test_the_trim_scanner_observes_red() -> None:
    bookend = "not integration and not busybox and not serial_timing"
    assert middle_trim_gaps(bookend, f"{bookend} and not {_MARKER}") == []
    (gap,) = middle_trim_gaps(bookend, f"not integration and not serial_timing and not {_MARKER}")
    assert "dropped clauses" in gap
    assert "not busybox" in gap
    (gap,) = middle_trim_gaps(bookend, bookend)
    assert "does not deselect" in gap
    (gap,) = middle_trim_gaps(f"{bookend} and not {_MARKER}", f"{bookend} and not {_MARKER}")
    assert "would run nowhere" in gap


def test_the_hostless_session_runs_each_pair_on_its_own_pythons(
    noxfile_tree: ast.Module,
) -> None:
    """``tests_hostless`` branches on BOOKEND_PYTHONS and runs all four bundles.

    Read from the session's own AST rather than from the invocation scan,
    which flattens both branches into one list and so cannot tell a session
    that runs the right pair per Python from one that runs all four always.
    """
    session = next(
        node
        for node in noxfile_tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "tests_hostless"
    )
    names = {node.id for node in ast.walk(session) if isinstance(node, ast.Name)}
    assert "BOOKEND_PYTHONS" in names, "tests_hostless no longer selects its args by Python"
    for bundle in (*_BOOKEND_PAIR, *_MIDDLE_PAIR):
        assert bundle in names, f"tests_hostless does not run {bundle}"
    # And the invocation scan (what the lane-leg membership gate reads) sees
    # both pairs, so an interior leg that selects nothing reddens there too.
    seen = {
        (_marker_expr(tokens), _roots(tokens))
        for tokens in _python_pytest_invocations(_NOXFILE, resolve_names=True)
        if "-m" in tokens
    }
    bundles = _bundles(noxfile_tree)
    for bundle in (*_BOOKEND_PAIR, *_MIDDLE_PAIR):
        expected = (_marker_expr(bundles[bundle]), _roots(bundles[bundle]))
        assert expected in seen, f"the invocation scanner does not see {bundle}: {expected}"


def test_the_isolation_repeat_deselects_the_marker(noxfile_tree: ast.Module) -> None:
    """``tests_unit_repeat`` exists to catch state leaking between tests; the
    interpreter-agnostic guards hold no registry state and the differential
    is one test, so repeating them buys nothing — and CI measured that job at
    22 min, the longest in the workflow, with the differential alone ~8 min
    of it. The session still selects by path and still carries every
    exclusion it had; this pins only that the marker joined them.
    """
    session = next(
        node
        for node in noxfile_tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "tests_unit_repeat"
    )
    exprs = [
        _marker_expr(tokens)
        for tokens in _python_pytest_invocations(_NOXFILE, resolve_names=True)
        if "-m" in tokens and "--count=2" in tokens
    ]
    assert len(exprs) == 1, f"expected one --count=2 invocation, found {exprs}"
    assert f"not {_MARKER}" in _clauses(exprs[0]), exprs[0]
    assert "tests/unit" in {
        node.value for node in ast.walk(session) if isinstance(node, ast.Constant)
    }, "tests_unit_repeat no longer names tests/unit"


def test_the_make_nox_target_keeps_the_middle_on_the_hostless_session() -> None:
    """``make nox`` is where the trim pays: its interior legs must stay tests_hostless."""
    text = (PROJECT_ROOT / "Makefile").read_text()
    live = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    recipe = re.search(r"^nox:.*?\n((?:\t.*\n)+)", live, re.MULTILINE)
    assert recipe is not None, "Makefile has no `nox` recipe"
    assert "tests_hostless-$(v)" in recipe.group(1), recipe.group(1)
    assert re.search(r"^NOX_MIDDLE\s*:?=\s*3\.11 3\.12 3\.13\s*$", live, re.MULTILINE), (
        "NOX_MIDDLE is no longer the three interior versions"
    )


def test_every_carrier_declares_the_marker_at_module_level_under_tests_unit() -> None:
    # Walk pytest's own roots, not all of tests/: the fixture repos beside
    # them (tests/repo_broken) hold test_*.py files that do not parse, on
    # purpose, and no lane ever collects them.
    ini = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text())
    roots = [PROJECT_ROOT / p for p in ini["tool"]["pytest"]["ini_options"]["testpaths"]]
    carriers = {
        path
        for root in roots
        for path in root.rglob("test_*.py")
        if _MARKER in _module_pytestmark_names(ast.parse(path.read_text()))
    }
    assert carriers, f"no module carries `{_MARKER}` (guard misparse?)"
    outside = sorted(
        str(p.relative_to(TESTS_ROOT)) for p in carriers if TESTS_ROOT / "unit" not in p.parents
    )
    assert not outside, (
        f"`{_MARKER}` carriers outside tests/unit: {outside}. The middle legs name only "
        f"tests/unit, so the marker is a no-op claim anywhere else — move the test or drop it."
    )
    # Per-function decorators are deliberately not honoured: the trim is a
    # statement about a MODULE's subject, and a function-level stamp on one
    # test inside a product module would read as the module opting out.
    decorated = sorted(
        str(path.relative_to(TESTS_ROOT))
        for path in (TESTS_ROOT / "unit").rglob("test_*.py")
        if path not in carriers and re.search(rf"pytest\.mark\.{_MARKER}\b", path.read_text())
    )
    assert not decorated, f"`{_MARKER}` used below module level in: {decorated}"
    missing = [c for c in _LOAD_BEARING_CARRIERS if TESTS_ROOT / c not in carriers]
    assert not missing, f"load-bearing carriers lost the marker: {missing}"
