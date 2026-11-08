"""The cross-Python matrix is tiered: the PRIMARY runs everything, the rest run the product.

``make nox`` runs the full suite on the PRIMARY (3.10, the floor), every
product test (the suite minus the ``interpreter_agnostic`` family) on the
CANARY (3.15, the newest), and a hostless leg on the versions between.
Measured on 2026-09-11, that hostless leg cost ~1,420 s of CPU per interior
Python, and 65 % of it was work whose subject is not otto-under-THIS-
interpreter at all: the ``tests/e2e`` tier (real ``otto`` subprocesses,
real ``pip`` venv builds) and the repository-policy guards that re-collect the
whole tree in a subprocess. Those tests prove the same thing on every
Python, so the PRIMARY runs everything; every other interpreter, the 3.15
canary included, deselects the ``interpreter_agnostic`` family (#566 Q2:
the family proves the same thing on every Python and stays in ``make
coverage`` on 3.10). The interior versions also drop the ``tests/e2e``
tier; the canary keeps it. Every catch-all session (``tests_unit``,
``tests_integration``, ``tests_all``) branches the same way. The hostless
selection is three sessions, one per tier: ``tests_hostless_unit`` (every
interpreter; the family deselected everywhere), ``tests_hostless_e2e`` (the
bookends; the family never mentioned — it is declared under ``tests/unit``
only) and ``tests_hostless_guards`` (the primary only; the family selected),
so the primary still runs the family exactly once.

This module pins the shape so the trim can neither widen (a middle leg that
grows back the e2e tier, or drops a clause its bookend twin carries) nor
leak (the marker reaching a primary expression, which would stop running
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

from scripts.gate_stamp_common import test_roots as pyproject_testpaths
from tests._fixtures.paths import CHECKOUT_ROOT, TESTS_ROOT
from tests.unit.test_tier_marker_invariants import (
    _module_pytestmark_names,
    _python_pytest_invocations,
    _string_constants,
    resolve_string,
)

pytestmark = pytest.mark.interpreter_agnostic

_NOXFILE = CHECKOUT_ROOT / "noxfile.py"
_MARKER = "interpreter_agnostic"
_UNIT_PAIR = ("HOSTLESS_UNIT_TEST_ARGS", "HOSTLESS_UNIT_SERIAL_ARGS")
_MIDDLE_PAIR = ("HOSTLESS_MIDDLE_TEST_ARGS", "HOSTLESS_MIDDLE_SERIAL_ARGS")
_E2E_PAIR = ("HOSTLESS_E2E_TEST_ARGS", "HOSTLESS_E2E_SERIAL_ARGS")
_E2E_CANARY_PAIR = ("HOSTLESS_E2E_CANARY_TEST_ARGS", "HOSTLESS_E2E_CANARY_SERIAL_ARGS")
_GUARDS = ("HOSTLESS_GUARDS_ARGS",)
_COVERAGE_FLAGS = ("--cov-append", "--cov-fail-under=", "--no-cov")

# The carriers whose cost is the reason the trim exists. Measured 2026-09-11
# on the 3.11 hostless leg: 85 s, 58 s, 31 s and 26 s. The shim differential
# (249 s) left the family on 2026-10-15 (#566 B1a: its subject is the
# interpreter); tests_unit_repeat keeps it out by --ignore instead.
# A carrier that loses its marker silently gives the interior legs that cost
# back; naming these four here makes that a red instead.
_LOAD_BEARING_CARRIERS = (
    "unit/test_support_matrix.py",
    "unit/test_conformance_bed.py",
    "unit/test_lane_invariants.py",
    "unit/test_tier_marker_invariants.py",
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
        resolved = [resolve_string(elt, constants) for elt in node.value.elts]
        out[node.targets[0].id] = [t for t in resolved if t is not None]
    return out


def _marker_expr(tokens: "list[str]") -> str:
    assert "-m" in tokens, f"no -m in {tokens}"
    return tokens[tokens.index("-m") + 1]


def _clauses(expr: str) -> "set[str]":
    return {clause.strip() for clause in expr.split(" and ")}


def _roots(tokens: "list[str]") -> "tuple[str, ...]":
    return tuple(t for t in tokens if t.startswith("tests/"))


def middle_trim_gaps(unit: str, middle: str) -> "list[str]":
    """Why *middle* is not the selection of its primary *unit* twin.

    Pure, so the red arm is testable without editing noxfile.py. The interior
    interpreters run the primary's unit selection uninstrumented: the same
    clauses exactly (a dropped exclusion is a widening — a lane that fetches
    artifacts or selects the bed; an added one runs less than the primary),
    ``not interpreter_agnostic`` among them (the family runs in the guards
    session only).
    """
    gaps: "list[str]" = []
    missing = _clauses(unit) - _clauses(middle)
    if missing:
        gaps.append(f"middle leg dropped clauses its unit twin carries: {sorted(missing)}")
    extra = _clauses(middle) - _clauses(unit)
    if extra:
        gaps.append(f"middle leg adds clauses its unit twin lacks: {sorted(extra)}")
    if f"not {_MARKER}" not in _clauses(middle):
        gaps.append(f"middle leg does not deselect `{_MARKER}`")
    return gaps


def _without_coverage(tokens: "list[str]") -> "list[str]":
    return [t for t in tokens if not t.startswith(_COVERAGE_FLAGS)]


@pytest.fixture(scope="module")
def noxfile_tree() -> ast.Module:
    return ast.parse(_NOXFILE.read_text())


def test_the_marker_is_registered() -> None:
    ini = tomllib.loads((CHECKOUT_ROOT / "pyproject.toml").read_text())
    names = [m.split(":", 1)[0].strip() for m in ini["tool"]["pytest"]["ini_options"]["markers"]]
    assert _MARKER in names, f"`{_MARKER}` is not registered in pyproject.toml (strict markers)"


def test_the_bookends_are_the_floor_and_the_ceiling(noxfile_tree: ast.Module) -> None:
    """BOOKEND_PYTHONS is exactly the oldest and newest of PYTHON_VERSIONS."""
    bundles = _bundles(noxfile_tree)
    versions = bundles["PYTHON_VERSIONS"]
    key = lambda v: tuple(int(p) for p in v.split("."))  # noqa: E731 — one-line sort key
    assert bundles["BOOKEND_PYTHONS"] == [min(versions, key=key), max(versions, key=key)]


def test_the_middle_pair_narrows_its_bookend_twin(noxfile_tree: ast.Module) -> None:
    """The interior pair is the primary's UNIT pair with only the coverage flags changed."""
    bundles = _bundles(noxfile_tree)
    for unit_name, middle_name in zip(_UNIT_PAIR, _MIDDLE_PAIR, strict=True):
        unit, middle = bundles[unit_name], bundles[middle_name]
        assert middle_trim_gaps(_marker_expr(unit), _marker_expr(middle)) == [], (
            unit_name,
            middle_name,
        )
        assert _roots(middle) == _roots(unit) == ("tests/unit",), (unit_name, middle_name)
        assert _without_coverage(middle) == _without_coverage(unit), (unit_name, middle_name)
        assert "--no-cov" in middle, f"{middle_name} still instruments coverage"
        assert "--cov-append" not in middle, f"{middle_name} appends to a report it never writes"


# How each session's bundles treat the family — the one place that decides it.
# A session not named here is a classic catch-all: its primary bundles never
# mention the marker (so the primary runs the family) and every non-primary
# bundle carries `not interpreter_agnostic` as its last clause.
_PRIMARY_ONLY = "primary-only"
_EXCLUDED = "excluded"  # every bundle, primary included, deselects it last
_SELECTED = "selected"  # the primary selects exactly the family
_UNDECLARED = "undeclared"  # no bundle mentions it: its roots hold no carrier
_FAMILY_POLICY = {
    "tests_hostless_unit": _EXCLUDED,
    "tests_hostless_e2e": _UNDECLARED,
    "tests_hostless_guards": _SELECTED,
}

_CATCH_ALL = {
    # session -> (primary bundles, non-primary bundle tuples)
    "tests_unit": (
        ("UNIT_TEST_ARGS", "UNIT_SERIAL_ARGS"),
        (("UNIT_NONPRIMARY_TEST_ARGS", "UNIT_NONPRIMARY_SERIAL_ARGS"),),
    ),
    "tests_integration": (
        ("INTEGRATION_TEST_ARGS", "INTEGRATION_SERIAL_ARGS"),
        (("INTEGRATION_NONPRIMARY_TEST_ARGS", "INTEGRATION_NONPRIMARY_SERIAL_ARGS"),),
    ),
    "tests_hostless_unit": (_UNIT_PAIR, (_MIDDLE_PAIR,)),
    "tests_hostless_e2e": (_E2E_PAIR, (_E2E_CANARY_PAIR,)),
    "tests_hostless_guards": (_GUARDS, ()),
    "tests_all": (
        ("TESTS_ALL_TEST_ARGS", "TESTS_ALL_SERIAL_ARGS"),
        (("TESTS_ALL_NONPRIMARY_TEST_ARGS", "TESTS_ALL_NONPRIMARY_SERIAL_ARGS"),),
    ),
}


def canary_gaps(primary: "list[str]", canary: "list[str]") -> "list[str]":
    """Why *canary* is not its *primary* twin minus coverage, and nothing else.

    Pure, so the red arm is testable without editing noxfile.py. The canary
    interpreter runs the primary e2e selection (paths, ``-m``, every other
    flag) with exactly one difference: the coverage flags are gone (it
    carries ``--no-cov`` and no gating flag). The family needs no clause
    here — it is declared under ``tests/unit`` only, so no e2e leg reaches it.
    """
    gaps: "list[str]" = []
    if _without_coverage(canary) != _without_coverage(primary):
        gaps.append(
            f"canary differs from its primary twin beyond coverage: {_without_coverage(canary)}"
        )
    if "--no-cov" not in canary:
        gaps.append("canary leg still instruments coverage")
    if any(t.startswith(("--cov-append", "--cov-fail-under=")) for t in canary):
        gaps.append("canary leg carries a coverage gate it never measures")
    return gaps


def test_the_canary_runs_the_middle_pair_and_the_e2e_pair_uninstrumented(
    noxfile_tree: ast.Module,
) -> None:
    """On the CANARY Python the unit session runs the interior pair and the e2e session
    runs the primary's e2e pair with coverage off — read per interpreter, as the
    lane gates read it."""
    bundles = _bundles(noxfile_tree)
    for primary_name, canary_name in zip(_E2E_PAIR, _E2E_CANARY_PAIR, strict=True):
        assert canary_gaps(bundles[primary_name], bundles[canary_name]) == [], canary_name
        assert _roots(bundles[canary_name]) == ("tests/e2e",), canary_name
    for session, pair in (
        ("tests_hostless_unit", _MIDDLE_PAIR),
        ("tests_hostless_e2e", _E2E_CANARY_PAIR),
    ):
        seen = _python_pytest_invocations(
            _NOXFILE, resolve_names=True, scope=session, python="3.15"
        )
        assert seen == [["pytest", *bundles[name]] for name in pair], (session, seen)
        for name in pair:
            assert instrumentation_gaps(name, bundles[name]) == [], bundles[name]


def test_the_canary_scanner_observes_red() -> None:
    primary = ["tests/e2e", "-m", "not busybox", "--cov-fail-under=0"]
    assert canary_gaps(primary, ["tests/e2e", "-m", "not busybox", "--no-cov"]) == []
    gaps = canary_gaps(primary, ["tests/e2e", "-m", f"not busybox and not {_MARKER}", "--no-cov"])
    assert len(gaps) == 1, gaps
    assert "beyond coverage" in gaps[0], gaps
    gaps = canary_gaps(primary, ["tests/e2e", "-m", "not busybox", "--cov-fail-under=94.75"])
    assert gaps == [
        "canary leg still instruments coverage",
        "canary leg carries a coverage gate it never measures",
    ]
    gaps = canary_gaps(primary, ["tests/unit", "-m", "not busybox", "--no-cov"])
    assert len(gaps) == 1, gaps
    assert "beyond coverage" in gaps[0], gaps


_TESTS_ALL_PRIMARY = ("TESTS_ALL_TEST_ARGS", "TESTS_ALL_SERIAL_ARGS")


def instrumentation_gaps(name: str, tokens: "list[str]") -> "list[str]":
    """Why bundle *name* is not uninstrumented: no ``--no-cov``, or a ``--cov-*`` flag."""
    gaps: "list[str]" = []
    if "--no-cov" not in tokens:
        gaps.append(f"{name} instruments coverage")
    if any(t.startswith("--cov-") for t in tokens):
        gaps.append(f"{name} carries a coverage flag it never applies")
    return gaps


def test_tests_all_runs_uninstrumented_on_the_primary_too(noxfile_tree: ast.Module) -> None:
    """The primary session is what `make coverage` gates; a report here has no reader (#566 C3)."""
    bundles = _bundles(noxfile_tree)
    for name in _TESTS_ALL_PRIMARY:
        assert instrumentation_gaps(name, bundles[name]) == [], bundles[name]


def test_the_instrumentation_reader_observes_red() -> None:
    assert instrumentation_gaps("X", ["-m", "not browser", "--no-cov"]) == []
    assert instrumentation_gaps("X", ["-m", "not browser"]) == ["X instruments coverage"]
    assert instrumentation_gaps("X", ["-m", "not browser", "--no-cov", "--cov-append"]) == [
        "X carries a coverage flag it never applies"
    ]
    assert instrumentation_gaps("X", ["--cov-fail-under=0"]) == [
        "X instruments coverage",
        "X carries a coverage flag it never applies",
    ]


def test_the_trim_scanner_observes_red() -> None:
    base = "not integration and not busybox and not serial_timing"
    unit = f"{base} and not {_MARKER}"
    assert middle_trim_gaps(unit, unit) == []
    (gap,) = middle_trim_gaps(unit, f"not integration and not serial_timing and not {_MARKER}")
    assert "dropped clauses" in gap
    assert "not busybox" in gap
    (gap,) = middle_trim_gaps(unit, f"{unit} and not stability")
    assert "adds clauses" in gap
    (gap,) = middle_trim_gaps(base, base)
    assert "does not deselect" in gap


def _names(*nodes: ast.AST) -> "set[str]":
    return {n.id for node in nodes for n in ast.walk(node) if isinstance(n, ast.Name)}


def _session_pythons(session: ast.FunctionDef) -> str:
    """The source of the session decorator's ``python=`` argument."""
    for dec in session.decorator_list:
        if isinstance(dec, ast.Call):
            for kw in dec.keywords:
                if kw.arg == "python":
                    return ast.unparse(kw.value)
    raise AssertionError(f"{session.name} declares no python=")


_HOSTLESS_PYTHONS = {
    "tests_hostless_unit": "PYTHON_VERSIONS",
    "tests_hostless_e2e": "list(BOOKEND_PYTHONS)",
    "tests_hostless_guards": "[PRIMARY_PYTHON]",
}
"""Each hostless session's ``python=`` source; which bundles each branch runs is
``_CATCH_ALL``'s, judged by :func:`catch_all_branch_gaps`."""


def _session_python_gaps(tree: ast.Module, name: str, pythons: str) -> "list[str]":
    """Why session *name* does not declare ``python=`` *pythons* (its source, unparsed)."""
    declared = _session_pythons(_session_def(tree, name))
    return [] if declared == pythons else [f"{name} runs on {declared}, not {pythons}"]


def _assert_the_scanner_sees(tree: ast.Module, bundles: "tuple[str, ...]") -> None:
    """The invocation scan (what the lane-leg membership gate reads) sees every bundle,
    so a leg that selects nothing reddens there too."""
    seen = {
        (_marker_expr(tokens), _roots(tokens))
        for tokens in _python_pytest_invocations(_NOXFILE, resolve_names=True)
        if "-m" in tokens
    }
    resolved = _bundles(tree)
    for bundle in bundles:
        expected = (_marker_expr(resolved[bundle]), _roots(resolved[bundle]))
        assert expected in seen, f"the invocation scanner does not see {bundle}: {expected}"


def test_the_hostless_sessions_are_the_family_policys() -> None:
    assert set(_HOSTLESS_PYTHONS) == set(_FAMILY_POLICY)


@pytest.mark.parametrize("name", sorted(_HOSTLESS_PYTHONS))
def test_each_hostless_session_runs_on_its_own_pythons(noxfile_tree: ast.Module, name: str) -> None:
    """The unit tier on every interpreter, the e2e tier on the bookends, the guards on the
    primary; the scanner sees every bundle ``_CATCH_ALL`` gives the session (its branches
    are judged with every catch-all session's, by :func:`catch_all_branch_gaps`)."""
    assert _session_python_gaps(noxfile_tree, name, _HOSTLESS_PYTHONS[name]) == []
    primary, non_primary = _CATCH_ALL[name]
    _assert_the_scanner_sees(noxfile_tree, (*primary, *(b for pair in non_primary for b in pair)))


def test_the_hostless_session_reader_observes_red() -> None:
    tree = ast.parse(
        'PRIMARY_PYTHON = "3.10"\n'
        'A = ["tests/unit", "-m", "not busybox"]\n'
        'B = ["tests/unit", "-m", "not busybox and not interpreter_agnostic", "--no-cov"]\n'
        "@s(python=PYTHON_VERSIONS)\n"
        "def u(session):\n"
        "    if session.python == PRIMARY_PYTHON:\n"
        '        session.run("pytest", *A, *session.posargs)\n'
        "    else:\n"
        '        session.run("pytest", *B, *session.posargs)\n'
    )
    assert _session_python_gaps(tree, "u", "PYTHON_VERSIONS") == []
    assert _session_python_gaps(tree, "u", "[PRIMARY_PYTHON]") == [
        "u runs on PYTHON_VERSIONS, not [PRIMARY_PYTHON]"
    ]
    session, bundles = _session_def(tree, "u"), _bundles(tree)
    assert catch_all_branch_gaps(session, bundles, ("A",), (("B",),)) == []
    assert catch_all_branch_gaps(session, bundles, ("B",), (("A",),)) != [], "swapped branches"


def _session_def(tree: ast.Module, name: str) -> ast.FunctionDef:
    return next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name)


def _is_python_eq(test: ast.expr, name: str) -> bool:
    """``session.python == <name>`` exactly: one Eq, ``.python`` on the left, the Name right."""
    return (
        isinstance(test, ast.Compare)
        and isinstance(test.left, ast.Attribute)
        and test.left.attr == "python"
        and len(test.ops) == 1
        and isinstance(test.ops[0], ast.Eq)
        and isinstance(test.comparators[0], ast.Name)
        and test.comparators[0].id == name
    )


def _starred_bundles(stmts: "list[ast.stmt]") -> "list[tuple[str, ...]]":
    """The bundle NAMES each ``session.run("pytest", …)`` call in *stmts* stars.

    One tuple per call, in source order, holding every starred Name directly
    in that call's arguments (``*session.posargs`` is an attribute, not a
    Name, so it is not counted). Invocation boundaries are kept on purpose:
    one call starring two bundles is one pytest run in which the second
    ``-m`` wins, not a pair. A bundle reached through a local
    (``local = NAME; session.run(…, *local)``) is returned as the local's
    name, so it never equals the expected bundle and the branch mismatches.
    """
    out: "list[tuple[str, ...]]" = []
    for stmt in stmts:
        for node in ast.walk(stmt):
            if (
                isinstance(node, ast.Call)
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and node.args[0].value == "pytest"
            ):
                out.append(
                    tuple(
                        a.value.id
                        for a in node.args
                        if isinstance(a, ast.Starred) and isinstance(a.value, ast.Name)
                    )
                )
    return out


def _per_call(pair: "tuple[str, ...]") -> "list[tuple[str, ...]]":
    """*pair* as the per-call shape a branch must have: one bundle per pytest call."""
    return [(bundle,) for bundle in pair]


def _clause_list(tokens: "list[str]") -> "list[str]":
    return [c.strip() for c in _marker_expr(tokens).split(" and ")] if "-m" in tokens else []


def _excluded_gaps(bundle: str, tokens: "list[str]", runs_it: str) -> "list[str]":
    clauses = _clause_list(tokens)
    if f"not {_MARKER}" not in clauses:
        return [f"{bundle} {runs_it}"]
    if clauses[-1] != f"not {_MARKER}":
        return [f"{bundle} does not carry `not {_MARKER}` as its last clause"]
    return []


def _family_gaps(bundle: str, tokens: "list[str]", policy: str, *, primary: bool) -> "list[str]":
    """Why *bundle* does not treat the family as its session's *policy* says."""
    mentions = "-m" in tokens and _MARKER in _marker_expr(tokens)
    if policy == _UNDECLARED:
        if mentions:
            return [f"{bundle} mentions `{_MARKER}` — the family is declared under tests/unit only"]
        return []
    if not primary:
        return _excluded_gaps(bundle, tokens, "runs the family off the primary")
    if policy == _EXCLUDED:
        return _excluded_gaps(bundle, tokens, "runs the family — its session excludes it")
    if policy == _SELECTED:
        if _MARKER not in _clause_list(tokens):
            return [f"{bundle} does not select `{_MARKER}`"]
        return []
    if mentions:
        return [f"{bundle} mentions `{_MARKER}` — the primary must run the family"]
    return []


def catch_all_branch_gaps(
    session: ast.FunctionDef,
    bundles: "dict[str, list[str]]",
    primary_bundles: "tuple[str, ...]",
    non_primary: "tuple[tuple[str, ...], ...]",
    policy: str = _PRIMARY_ONLY,
) -> "list[str]":
    """Why *session* does not run its primary bundles on the primary and a non-primary set off it.

    Pure over the AST, so the red arm is testable on synthetic source. A
    session with no non-primary sets runs on the primary only: its body has no
    ladder and its calls are exactly the primary bundles. Otherwise the ladder
    must be ``if session.python == PRIMARY_PYTHON:`` (an Eq against that Name — a
    ``!=`` or an ``in`` would swap the bodies' meaning and the reader refuses it).
    Every ``session.run("pytest", …)`` call stars exactly one bundle Name, and
    the calls of the primary body, in order, are exactly the primary bundles; every
    other branch (an ``elif session.python == <Name>:`` or the final ``else``)
    runs exactly one non-primary set the same way, and together the
    non-primary branches run every non-primary set. Every non-primary bundle
    carries ``--no-cov`` and no coverage gate. The family is judged by
    *policy* (see ``_FAMILY_POLICY``): by default no primary bundle mentions
    the marker and every non-primary one carries ``not interpreter_agnostic``
    as its LAST ``-m`` clause.
    """
    gaps: "list[str]" = []
    ladders = [n for n in session.body if isinstance(n, ast.If)]
    branches: "list[list[tuple[str, ...]]]" = []
    if not non_primary:
        if ladders:
            return [f"{session.name}: a primary-only session branches on {len(ladders)} `if`"]
        primary = _starred_bundles(session.body)
    else:
        if len(ladders) != 1:
            return [f"{session.name}: expected one top-level `if`, found {len(ladders)}"]
        ladder = ladders[0]
        if not _is_python_eq(ladder.test, "PRIMARY_PYTHON"):
            gaps.append(
                f"{session.name}: the ladder does not test `session.python == PRIMARY_PYTHON`"
            )
        rest = ladder.orelse
        while rest:
            if len(rest) == 1 and isinstance(rest[0], ast.If):
                branch = rest[0]
                comparator = (
                    branch.test.comparators[0] if isinstance(branch.test, ast.Compare) else None
                )
                if not (
                    isinstance(comparator, ast.Name) and _is_python_eq(branch.test, comparator.id)
                ):
                    gaps.append(f"{session.name}: an elif that is not `session.python == <Name>`")
                branches.append(_starred_bundles(branch.body))
                rest = branch.orelse
            else:
                branches.append(_starred_bundles(rest))
                rest = []
        primary = _starred_bundles(ladder.body)
    for call in (*primary, *(call for calls in branches for call in calls)):
        if len(call) != 1:
            gaps.append(
                f"{session.name}: one pytest call stars {list(call)} — "
                "exactly one bundle per call (a second -m overrides the first)"
            )
    if primary != _per_call(primary_bundles):
        gaps.append(
            f"{session.name}: PRIMARY branch runs {primary}, not {_per_call(primary_bundles)}"
        )
    expected = [_per_call(bundle_set) for bundle_set in non_primary]
    if sorted(branches) != sorted(expected):
        gaps.append(f"{session.name}: non-primary branches run {branches}, not {expected}")
    for bundle in primary_bundles:
        if bundle in bundles:
            gaps += _family_gaps(bundle, bundles[bundle], policy, primary=True)
    for bundle_set in non_primary:
        for bundle in bundle_set:
            tokens = bundles.get(bundle, [])
            gaps += _family_gaps(bundle, tokens, policy, primary=False)
            if "--no-cov" not in tokens:
                gaps.append(f"{bundle} instruments coverage off the primary")
            if any(t.startswith(("--cov-append", "--cov-fail-under=")) for t in tokens):
                gaps.append(f"{bundle} carries a coverage gate it never measures")
    return gaps


def test_every_catch_all_session_runs_the_family_on_the_primary_only(
    noxfile_tree: ast.Module,
) -> None:
    """Each catch-all session runs the family on PRIMARY_PYTHON only.

    The primary pair on PRIMARY_PYTHON (family included, never mentioned), a
    non-primary pair on every other interpreter, as starred NAMES. Read from
    the AST per branch (the flat invocation scan cannot tell the right pair per
    Python from all pairs always), then cross-checked against the scanner's own
    per-interpreter read, which is what the lane gates consume.
    """
    bundles = _bundles(noxfile_tree)
    for name, (primary_bundles, non_primary) in _CATCH_ALL.items():
        policy = _FAMILY_POLICY.get(name, _PRIMARY_ONLY)
        session = _session_def(noxfile_tree, name)
        assert (
            catch_all_branch_gaps(session, bundles, primary_bundles, non_primary, policy) == []
        ), name

        def exprs_on(python: str, name: str = name) -> "set[str]":
            return {
                _marker_expr(t)
                for t in _python_pytest_invocations(
                    _NOXFILE, resolve_names=True, scope=name, python=python
                )
                if "-m" in t
            }

        primary = exprs_on("3.10")
        assert primary == {_marker_expr(bundles[b]) for b in primary_bundles}, name
        if policy in (_PRIMARY_ONLY, _UNDECLARED):
            assert not any(_MARKER in e for e in primary), (name, primary)
        if not non_primary:
            continue
        # only the session's own interpreters: a read at a python it never runs on
        # falls into the `else` branch and proves nothing
        bookends = "BOOKEND_PYTHONS" in _session_pythons(session)
        versions = bundles["BOOKEND_PYTHONS" if bookends else "PYTHON_VERSIONS"]
        for python in [v for v in versions if v != "3.10"]:
            exprs = exprs_on(python)
            assert exprs, (name, python)
            if policy == _UNDECLARED:
                assert not any(_MARKER in e for e in exprs), (name, python, exprs)
            else:
                assert all(f"not {_MARKER}" in _clauses(e) for e in exprs), (name, python, exprs)
    # The family runs exactly once on the primary: one session selects it.
    assert [n for n, p in _FAMILY_POLICY.items() if p == _SELECTED] == ["tests_hostless_guards"]


_SYNTHETIC = """
PRIMARY_PYTHON = "3.10"
CANARY_PYTHON = "3.15"
P_TEST_ARGS = ("tests/unit", "-m", "not busybox and not serial_timing")
P_SERIAL_ARGS = ("tests/unit", "-m", "serial_timing and not busybox", "-n0", "--cov-append")
N_TEST_ARGS = (
    "tests/unit",
    "-m",
    "not busybox and not serial_timing and not interpreter_agnostic",
    "--no-cov",
)
N_SERIAL_ARGS = (
    "tests/unit",
    "-m",
    "serial_timing and not busybox and not interpreter_agnostic",
    "-n0",
    "--no-cov",
)
def s(session):
    if session.python {op} PRIMARY_PYTHON:
        session.run("pytest", *{a}, *session.posargs)
        session.run("pytest", *{b}, *session.posargs)
{elif_}    else:
{non_primary}
"""

_SYNTHETIC_ELIF = """\
    elif {test}:
        session.run("pytest", *N_TEST_ARGS, *session.posargs)
        session.run("pytest", *N_SERIAL_ARGS, *session.posargs)
"""


def _synthetic(
    op: str = "==",
    a: str = "P_TEST_ARGS",
    b: str = "P_SERIAL_ARGS",
    c: str = "N_TEST_ARGS",
    d: str = "N_SERIAL_ARGS",
    alias: str = "",
    elif_test: str = "",
    one_call: bool = False,
) -> "tuple[ast.FunctionDef, dict[str, list[str]]]":
    """A one-session noxfile; *elif_test* adds an ``elif`` branch running the N pair.

    *one_call* renders the non-primary branch as ONE call starring both
    bundles — the shape whose second ``-m`` silently wins at runtime.
    """
    if one_call:
        non_primary = f'        session.run("pytest", *{c}, *{d}, *session.posargs)'
    else:
        non_primary = (
            f'        {alias}session.run("pytest", *{c}, *session.posargs)\n'
            f'        session.run("pytest", *{d}, *session.posargs)'
        )
    elif_ = _SYNTHETIC_ELIF.format(test=elif_test) if elif_test else ""
    tree = ast.parse(_SYNTHETIC.format(op=op, a=a, b=b, elif_=elif_, non_primary=non_primary))
    return _session_def(tree, "s"), _bundles(tree)


_ONE_CALL_GAP = "one pytest call stars ['N_TEST_ARGS', 'N_SERIAL_ARGS']"


def test_the_catch_all_scanner_observes_red() -> None:
    pp, npp = ("P_TEST_ARGS", "P_SERIAL_ARGS"), (("N_TEST_ARGS", "N_SERIAL_ARGS"),)
    assert catch_all_branch_gaps(*_synthetic(), pp, npp) == []
    (gap,) = catch_all_branch_gaps(*_synthetic(op="!="), pp, npp)
    assert "does not test" in gap
    swapped = _synthetic(a="N_TEST_ARGS", b="N_SERIAL_ARGS", c="P_TEST_ARGS", d="P_SERIAL_ARGS")
    gaps = catch_all_branch_gaps(*swapped, pp, npp)
    assert any("PRIMARY branch runs" in g for g in gaps), gaps
    assert any("non-primary branches run" in g for g in gaps), gaps
    aliased = _synthetic(alias="local = N_TEST_ARGS\n        ", c="local")
    gaps = catch_all_branch_gaps(*aliased, pp, npp)
    assert any("non-primary branches run" in g for g in gaps), gaps
    # One call starring both bundles: at runtime the second -m wins and only
    # the serial leg runs. Read flat across calls, it would pass as the pair.
    gaps = catch_all_branch_gaps(*_synthetic(one_call=True), pp, npp)
    assert any(_ONE_CALL_GAP in g for g in gaps), gaps
    assert any("non-primary branches run" in g for g in gaps), gaps
    # Two top-level ladders.
    session, bundles = _synthetic()
    session.body = session.body * 2
    assert catch_all_branch_gaps(session, bundles, pp, npp) == [
        "s: expected one top-level `if`, found 2"
    ]
    # An elif against a string literal, not a Name; the Name form is green.
    two = npp * 2
    canary = _synthetic(elif_test="session.python == CANARY_PYTHON")
    assert catch_all_branch_gaps(*canary, pp, two) == []
    (gap,) = catch_all_branch_gaps(*_synthetic(elif_test='session.python == "3.12"'), pp, two)
    assert "an elif that is not" in gap
    # The bundle contents, one mutation each, each naming its own gap only.
    tail = f"not busybox and not {_MARKER}"
    for name, tokens, expected in (
        (
            "N_SERIAL_ARGS",
            ["tests/unit", "-m", "serial_timing and not busybox", "-n0", "--no-cov"],
            "N_SERIAL_ARGS runs the family off the primary",
        ),
        (
            "P_TEST_ARGS",
            ["tests/unit", "-m", f"not busybox and not serial_timing and not {_MARKER}"],
            f"P_TEST_ARGS mentions `{_MARKER}` — the primary must run the family",
        ),
        (
            "N_TEST_ARGS",
            ["tests/unit", "-m", tail],
            "N_TEST_ARGS instruments coverage off the primary",
        ),
        (
            "N_TEST_ARGS",
            ["tests/unit", "-m", tail, "--no-cov", "--cov-append"],
            "N_TEST_ARGS carries a coverage gate it never measures",
        ),
        (
            "N_TEST_ARGS",
            ["tests/unit", "-m", tail, "--no-cov", "--cov-fail-under=92"],
            "N_TEST_ARGS carries a coverage gate it never measures",
        ),
        (
            "N_TEST_ARGS",
            [
                "tests/unit",
                "-m",
                f"not busybox and not {_MARKER} and not serial_timing",
                "--no-cov",
            ],
            f"N_TEST_ARGS does not carry `not {_MARKER}` as its last clause",
        ),
    ):
        session, bundles = _synthetic()
        bundles[name] = tokens
        assert catch_all_branch_gaps(session, bundles, pp, npp) == [expected], (name, tokens)


def test_the_family_policy_reader_observes_red() -> None:
    """The per-session policies, each with one mutation naming its own gap only."""
    pp, npp = ("P_TEST_ARGS", "P_SERIAL_ARGS"), (("N_TEST_ARGS", "N_SERIAL_ARGS"),)
    # UNDECLARED (the e2e session): no bundle mentions the family.
    session, bundles = _synthetic()
    for name in npp[0]:
        bundles[name] = [
            t.replace(f" and not {_MARKER}", "") if t.startswith(("not", "serial")) else t
            for t in bundles[name]
        ]
    assert catch_all_branch_gaps(session, bundles, pp, npp, _UNDECLARED) == []
    bundles["P_TEST_ARGS"] = ["tests/e2e", "-m", f"not busybox and not {_MARKER}"]
    assert catch_all_branch_gaps(session, bundles, pp, npp, _UNDECLARED) == [
        f"P_TEST_ARGS mentions `{_MARKER}` — the family is declared under tests/unit only"
    ]
    # EXCLUDED (the unit session): the primary deselects it too.
    session, bundles = _synthetic()
    bundles["P_TEST_ARGS"] = ["tests/unit", "-m", f"not busybox and not {_MARKER}"]
    bundles["P_SERIAL_ARGS"] = ["tests/unit", "-m", f"serial_timing and not {_MARKER}"]
    assert catch_all_branch_gaps(session, bundles, pp, npp, _EXCLUDED) == []
    bundles["P_SERIAL_ARGS"] = ["tests/unit", "-m", "serial_timing"]
    assert catch_all_branch_gaps(session, bundles, pp, npp, _EXCLUDED) == [
        "P_SERIAL_ARGS runs the family — its session excludes it"
    ]
    # SELECTED (the guards session): one leg, no ladder, the family selected.
    guards = ast.parse(
        "def g(session):\n"
        '    session.run("pytest", *G_ARGS, *session.posargs)\n'
        "def h(session):\n"
        "    if session.python == PRIMARY_PYTHON:\n"
        '        session.run("pytest", *G_ARGS, *session.posargs)\n'
    )
    g_bundles = {"G_ARGS": ["tests/unit", "-m", f"not busybox and {_MARKER}"]}
    assert (
        catch_all_branch_gaps(_session_def(guards, "g"), g_bundles, ("G_ARGS",), (), _SELECTED)
        == []
    )
    assert catch_all_branch_gaps(
        _session_def(guards, "h"), g_bundles, ("G_ARGS",), (), _SELECTED
    ) == ["h: a primary-only session branches on 1 `if`"]
    g_bundles["G_ARGS"] = ["tests/unit", "-m", f"not busybox and not {_MARKER}"]
    assert catch_all_branch_gaps(
        _session_def(guards, "g"), g_bundles, ("G_ARGS",), (), _SELECTED
    ) == [f"G_ARGS does not select `{_MARKER}`"]


def test_non_primary_serial_twins_carry_the_trim(noxfile_tree: ast.Module) -> None:
    """A serial twin's clauses are its parallel twin's with `serial_timing` flipped.

    So the trim cannot be dropped on the serial leg alone (the family's
    serial_timing members would then run on every interpreter). Only real
    pairs are read: the guards session's single leg has no twin.
    """
    bundles = _bundles(noxfile_tree)
    for primary_bundles, non_primary in _CATCH_ALL.values():
        pairs = [b for b in (primary_bundles, *non_primary) if len(b) == 2]
        for test_name, serial_name in pairs:
            parallel = _clauses(_marker_expr(bundles[test_name]))
            serial = _clauses(_marker_expr(bundles[serial_name]))
            assert serial == (parallel - {"not serial_timing"}) | {"serial_timing"}, (
                test_name,
                serial_name,
            )


_DIFFERENTIAL = "tests/unit/shim/test_differential.py"


def test_the_isolation_repeat_deselects_the_marker_and_ignores_the_differential(
    noxfile_tree: ast.Module,
) -> None:
    """``tests_unit_repeat`` exists to catch state leaking between tests; the
    interpreter-agnostic guards hold no registry state, so repeating them buys
    nothing — CI measured that job at 22 min, the longest in the workflow. The
    shim differential is no longer in the family (its result DOES depend on the
    interpreter: it runs the completion shim the user's Python executes), so
    the session keeps it out by path instead: one test, ~4 min single-process,
    ~8 min repeated. The session still selects by path and still carries every
    exclusion it had; this pins the marker clause and the ignore.
    """
    session = next(
        node
        for node in noxfile_tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "tests_unit_repeat"
    )
    repeats = [
        tokens
        for tokens in _python_pytest_invocations(_NOXFILE, resolve_names=True)
        if "-m" in tokens and "--count=2" in tokens
    ]
    assert len(repeats) == 1, f"expected one --count=2 invocation, found {repeats}"
    (tokens,) = repeats
    assert f"not {_MARKER}" in _clauses(_marker_expr(tokens)), tokens
    assert f"--ignore={_DIFFERENTIAL}" in tokens, (
        f"tests_unit_repeat no longer ignores {_DIFFERENTIAL}: the differential left the "
        "family (it is interpreter-dependent), so only the ignore keeps it out of the repeat"
    )
    assert "tests/unit" in {
        node.value for node in ast.walk(session) if isinstance(node, ast.Constant)
    }, "tests_unit_repeat no longer names tests/unit"


def test_the_shim_tests_are_not_in_the_family() -> None:
    """Both results depend on the interpreter — the opposite of the marker's meaning."""
    for rel in (_DIFFERENTIAL, "tests/unit/shim/test_refresh_spawn.py"):
        names = _module_pytestmark_names(ast.parse((CHECKOUT_ROOT / rel).read_text()))
        assert _MARKER not in names, f"{rel} carries `{_MARKER}` again"


def test_the_make_nox_target_keeps_the_middle_on_the_hostless_session() -> None:
    """``make nox`` is where the trim pays: its interior legs must stay tests_hostless_unit."""
    text = (CHECKOUT_ROOT / "Makefile").read_text()
    live = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    recipe = re.search(r"^nox:.*?\n((?:\t.*\n)+)", live, re.MULTILINE)
    assert recipe is not None, "Makefile has no `nox` recipe"
    assert "tests_hostless_unit-$(v)" in recipe.group(1), recipe.group(1)
    assert re.search(r"^NOX_MIDDLE\s*:?=\s*3\.11 3\.12 3\.13 3\.14\s*$", live, re.MULTILINE), (
        "NOX_MIDDLE is no longer the four interior versions"
    )


def test_the_make_nox_release_target_runs_no_primary_session() -> None:
    """``make nox-release`` is the release's matrix once `make coverage` has stamped the tree.

    The primary's full session is the selection the stamp already proves,
    so the target runs only the canary's bed session (the hostless halves
    are CI's) and nothing on ``$(NOX_PRIMARY)`` (#566 ruling 4).
    """
    text = (CHECKOUT_ROOT / "Makefile").read_text()
    live = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    recipe = re.search(r"^nox-release:.*?\n((?:\t.*\n)+)", live, re.MULTILINE)
    assert recipe is not None, "Makefile has no `nox-release` recipe"
    body = recipe.group(1)
    assert "tests_bed-$(NOX_CANARY)" in body, body
    assert "$(NOX_PRIMARY)" not in body, body
    assert "tests_bed-3.10" not in body, body
    assert "tests_all-3.10" not in body, body
    assert not re.search(r"\btests_all\b(?!-)", body), (
        f"a bare tests_all would run every interpreter: {body}"
    )


def test_every_carrier_declares_the_marker_at_module_level_under_tests_unit() -> None:
    # Walk pytest's own roots, not all of tests/: the fixture projects beside
    # them (tests/project_broken) hold test_*.py files that do not parse, on
    # purpose, and no lane ever collects them.
    roots = [CHECKOUT_ROOT / p for p in pyproject_testpaths(CHECKOUT_ROOT)]
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
