"""Drift guards for the tier<->marker contract (Spec §5.3).

Run in the no-VM unit gate. G1 proves the integration/ auto-stamp hook fires;
G2 proves no VM-only marker leaks into the unit tier. G3 proves the e2e/
auto-stamp mirror. G4 proves no catch-all nox session sweeps the bed-hostile
stability tier into a parallel run. G5 proves every chaos-lane module carries
both the `chaos` and `stability` markers. G6 proves the two positive
stability Make legs (and the `repeat` soak, which isn't path-restricted
either) can't co-select the chaos lane. G7 proves the resource-slice legs
(Makefile `M_UNIX`/`M_EMBEDDED` and nox's `tests_unix`/`tests_embedded`) —
which share a resource marker (`integration`/`embedded`) with the chaos lane
and, like the stability legs G6 covers, are bare positive selectors no
catch-all's `not stability` protects — exclude BOTH bed-hostile tiers, not
just one.
"""

import ast
import re
from itertools import pairwise
from pathlib import Path

import pytest

from tests._fixtures.paths import PROJECT_ROOT, TESTS_ROOT

_UNIT = TESTS_ROOT / "unit"
_NOXFILE = PROJECT_ROOT / "noxfile.py"

# Markers that mean "needs a VM" — must never appear on a unit-tier test.
_VM_MARKERS = {"integration", "embedded", "hops"}


def test_integration_conftest_autostamps_integration():
    """G1: the integration/ conftest stamps `integration` by directory."""
    from tests.integration import conftest as integ

    integ_root = Path(integ.__file__).parent

    class _FakeItem:
        def __init__(self, path: Path) -> None:
            self.path = path
            self.added: list[str] = []

        def add_marker(self, marker) -> None:
            self.added.append(getattr(marker, "name", str(marker)))

    item = _FakeItem(integ_root / "host" / "test_example.py")
    integ.pytest_collection_modifyitems(config=None, items=[item])
    assert "integration" in item.added


def _module_and_decorator_markers(path: Path) -> set[str]:
    """Marker names referenced by decorators or module-level `pytestmark`."""
    tree = ast.parse(path.read_text())
    found: set[str] = set()
    for node in ast.walk(tree):
        # @pytest.mark.<name>
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Attribute)
            and getattr(node.value, "attr", None) == "mark"
        ):
            found.add(node.attr)
    return found


def test_unit_tier_has_no_vm_markers():
    """G2: no test file under tests/unit/ references a VM-only marker."""
    offenders: list[str] = [
        str(path.relative_to(TESTS_ROOT))
        for path in _UNIT.rglob("test_*.py")
        if _VM_MARKERS & _module_and_decorator_markers(path)
    ]
    assert not offenders, f"VM markers found under tests/unit/: {offenders}"


def _nox_marker_expressions() -> list[str]:
    """Every marker expression passed via ``-m`` anywhere in noxfile.py.

    Scans argument sequences (call args, tuple/list literals — the latter
    catches shared arg bundles like ``HOSTLESS_TEST_ARGS``) for a ``"-m"``
    constant and takes the string that follows it; a name reference is
    resolved from module-level assignments (``DASHBOARD_MARKER_EXPR``).
    """
    tree = ast.parse(_NOXFILE.read_text())
    assigns: dict[str, str] = {
        node.targets[0].id: node.value.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    }
    exprs: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            seq = node.args
        elif isinstance(node, (ast.Tuple, ast.List)):
            seq = node.elts
        else:
            continue
        for flag, value in pairwise(seq):
            if not (isinstance(flag, ast.Constant) and flag.value == "-m"):
                continue
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                exprs.append(value.value)
            elif isinstance(value, ast.Name) and value.id in assigns:
                exprs.append(assigns[value.id])
    return exprs


def test_catchall_nox_sessions_exclude_stability():
    """G4: negation-only nox selections must exclude the stability tier.

    The stability tests are bed-HOSTILE by design (e.g. the SIGSTOP-wedge
    test stops tomato's sshd listener for tens of seconds), so they may only
    run where they own the bed: the dedicated `make stability-tunnel` lane
    (which selects nothing else, and whose single xdist_group serializes
    them). A catch-all session expression — one built purely from negations,
    which selects *everything else* — sweeps them into a parallel run where
    another worker's concurrent ssh to the wedged host times out (the
    2026-07-19 hop-test failures: 5 of 6 tests_all sessions across two
    checkouts). Expressions with a positive selector (e.g. "browser and not
    soak") can't co-select stability and are exempt.
    """
    catchall = [
        expr
        for expr in _nox_marker_expressions()
        if all(clause.strip().startswith("not ") for clause in expr.split(" and "))
    ]
    assert catchall, "no catch-all -m expressions found in noxfile.py (guard misparse?)"
    offenders = [expr for expr in catchall if "not stability" not in expr]
    assert not offenders, f"catch-all nox marker expressions missing 'not stability': {offenders}"


def _module_pytestmark_names(tree: ast.Module) -> set[str]:
    """Marker names stamped via a module-level ``pytestmark`` assignment.

    Collects both the plain-attribute form (``pytest.mark.chaos``) and the
    called form (``pytest.mark.timeout(300)``, ``pytest.mark.xdist_group(...)``)
    — ``ast.walk`` over the assign node visits a ``Call``'s ``func`` too, so
    both shapes land in the same walk.
    """
    names: set[str] = set()
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == "pytestmark" for t in node.targets)
        ):
            continue
        for mark in ast.walk(node):
            if (
                isinstance(mark, ast.Attribute)
                and isinstance(mark.value, ast.Attribute)
                and isinstance(mark.value.value, ast.Name)
                and mark.value.value.id == "pytest"
                and mark.value.attr == "mark"
            ):
                names.add(mark.attr)
    return names


def test_chaos_modules_carry_chaos_and_stability():
    """G5: every module under tests/e2e/chaos declares BOTH markers.

    The lane's exclusion from default gates rides entirely on the module-level
    ``stability`` stamp (every catch-all already says ``not stability``); the
    positive ``chaos`` stamp is what the opt-in lane selects. A module missing
    either silently joins gates it must never join, or silently drops out of
    the lane. AST-scan pytestmark like the e2e resource-marker rule does at
    runtime — this guard runs in the no-VM unit gate, so it fires on every PR.
    """
    chaos_dir = TESTS_ROOT / "e2e" / "chaos"
    if not chaos_dir.is_dir():
        pytest.skip("tests/e2e/chaos not created yet")
    offenders = []
    for mod in sorted(chaos_dir.glob("test_*.py")):
        tree = ast.parse(mod.read_text())
        marks = _module_pytestmark_names(tree)
        missing = {"chaos", "stability"} - marks
        if missing:
            offenders.append(f"{mod.name}: missing {sorted(missing)}")
    assert not offenders, "chaos modules missing required markers:\n  " + "\n  ".join(offenders)


def test_stability_make_legs_exclude_chaos():
    """G6: the positive stability selectors must not co-select the chaos lane.

    ``stability-unix`` (``stability and integration and not embedded and not
    hops``) and ``stability-embedded`` (``stability and embedded``) would both
    match a double-stamped chaos module; chaos scenarios reboot and blackhole
    the bed, so riding a stability soak would wreck it mid-run. G4 covers
    noxfile catch-alls; this covers the Makefile legs that aren't. ``repeat``
    is included too: unlike the coverage-gated Make legs, it isn't
    path-restricted to tests/unit (it runs the full local suite — unit,
    integration, e2e — under pytest-repeat), so its ``-m`` expression is a
    catch-all in the same sense as noxfile's G4 targets and needs the same
    exclusion.
    """
    makefile = (PROJECT_ROOT / "Makefile").read_text()
    for leg in ("stability-unix", "stability-embedded", "repeat"):
        recipe = makefile.split(f"\n{leg}:", 1)[1].split("\n\n", 1)[0]
        m_exprs = re.findall(r'-m\s+"([^"]+)"', recipe)
        assert m_exprs, f"{leg}: no -m expression found (recipe reshaped? update G6)"
        offenders = [e for e in m_exprs if "not chaos" not in e]
        assert not offenders, f"{leg}: -m expressions missing 'not chaos': {offenders}"


def test_resource_slice_legs_exclude_stability_and_chaos():
    """G7: the resource-slice legs must exclude BOTH bed-hostile tiers.

    ``M_UNIX`` (Makefile, backing ``coverage-unix`` / nox's ``tests_unix``)
    and ``M_EMBEDDED`` (backing ``coverage-embedded`` / ``tests_embedded``)
    are bare positive selectors on the same resource marker
    (``integration``/``embedded``) the chaos lane's modules are also stamped
    with — no catch-all's ``not stability`` (G4) protects them, the same gap
    G6 closed for the stability legs. Without ``not stability and not chaos``
    on both, a resource-slice run co-selects chaos scenarios that soft-reboot
    the leased host and blackhole SSH mid-suite. Covers the Makefile vars
    directly (parsed like G6's recipe scrape) and the two nox sessions (via
    G4's ``_nox_marker_expressions()`` scraper) — both hand-editable
    surfaces, so both need their own pin.
    """
    makefile = (PROJECT_ROOT / "Makefile").read_text()
    for var in ("M_UNIX", "M_EMBEDDED"):
        m = re.search(rf"^{var} := (.+)$", makefile, re.MULTILINE)
        assert m, f"{var} definition not found in Makefile (reshaped? update G7)"
        expr = m.group(1)
        missing = [clause for clause in ("not stability", "not chaos") if clause not in expr]
        assert not missing, f"{var} missing {missing}: {expr!r}"

    # Distinguish tests_unix's and tests_embedded's expressions from the
    # other -m expressions in noxfile.py (several share the substrings
    # "integration"/"embedded") by their leading clause: tests_unix's starts
    # with a bare positive "integration", tests_embedded's is bare
    # "embedded" (as opposed to e.g. chaos_embedded's "chaos and embedded",
    # whose leading clause is "chaos").
    exprs = _nox_marker_expressions()
    unix_exprs = [e for e in exprs if e.split(" and ")[0].strip() == "integration"]
    embedded_exprs = [e for e in exprs if e.split(" and ")[0].strip() == "embedded"]
    assert unix_exprs, "no nox -m expr led by 'integration' (tests_unix reshaped? update G7)"
    assert embedded_exprs, "no nox -m expr led by 'embedded' (tests_embedded reshaped? update G7)"
    for label, found in (("tests_unix", unix_exprs), ("tests_embedded", embedded_exprs)):
        for expr in found:
            missing = [clause for clause in ("not stability", "not chaos") if clause not in expr]
            assert not missing, f"{label} -m expression missing {missing}: {expr!r}"


def test_e2e_conftest_autostamps_e2e():
    """G3: the e2e/ conftest stamps `e2e` by directory (mirrors G1)."""
    from tests.e2e import conftest as e2e

    e2e_root = Path(e2e.__file__).parent

    class _FakeItem:
        def __init__(self, path: Path) -> None:
            self.path = path
            self.added: list[str] = []

        def add_marker(self, marker) -> None:
            self.added.append(getattr(marker, "name", str(marker)))

    item = _FakeItem(e2e_root / "config" / "test_example.py")
    e2e.pytest_collection_modifyitems(config=None, items=[item])
    assert "e2e" in item.added
