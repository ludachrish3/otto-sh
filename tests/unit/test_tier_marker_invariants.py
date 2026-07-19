"""Drift guards for the tier<->marker contract (Spec §5.3).

Run in the no-VM unit gate. G1 proves the integration/ auto-stamp hook fires;
G2 proves no VM-only marker leaks into the unit tier. G3 proves the e2e/
auto-stamp mirror. G4 proves no catch-all nox session sweeps the bed-hostile
stability tier into a parallel run.
"""

import ast
from itertools import pairwise
from pathlib import Path

_TESTS = Path(__file__).resolve().parents[1]  # tests/
_UNIT = _TESTS / "unit"
_NOXFILE = _TESTS.parent / "noxfile.py"

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
        str(path.relative_to(_TESTS))
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
