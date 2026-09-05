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
just one. The G8/G9 families carry the same rules for the BusyBox artifact
tier and the G10/G11 families for the host-contract conformance tier; each of
those states its own reasoning on the test itself.

G11i-G11l are the same family's answer to a hazard the marker cannot see at
all: the conformance tree runs HERMETIC or against the REAL BED depending on
one environment variable, so `-m` reasoning decides which tests a lane
collects and says nothing about what they then reach. Those four are about
the venue knob rather than the marker.
"""

import ast
import contextlib
import os
import re
import shlex
import subprocess
import sys
from itertools import pairwise
from pathlib import Path

import yaml

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # pragma: no cover - 3.10 only, otto's floor
    import tomli as tomllib

from tests._fixtures.paths import PROJECT_ROOT, TESTS_ROOT

_UNIT = TESTS_ROOT / "unit"
_NOXFILE = PROJECT_ROOT / "noxfile.py"
_STABILITY_CAMPAIGN = PROJECT_ROOT / "scripts" / "stability_campaign.py"

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


def test_busybox_conftest_autostamps_busybox():
    """G9: the tests/busybox/ conftest stamps `busybox` by directory.

    The stamp is what makes the tier's exclusion from the catch-all lanes
    (G8) a property of the DIRECTORY rather than of each author's memory: a
    new file here that forgot ``@pytest.mark.busybox`` would otherwise ride
    the ordinary CI gates and fetch ~5 MB from busybox.net on every cold
    cache. Same shape as G1 and G3, because it is the same mechanism and its
    silent failure would be just as invisible.
    """
    from tests.busybox import conftest as bb

    bb_root = Path(bb.__file__).parent

    class _FakeItem:
        def __init__(self, path: Path) -> None:
            self.path = path
            self.added: list[str] = []

        def add_marker(self, marker) -> None:
            self.added.append(getattr(marker, "name", str(marker)))

    item = _FakeItem(bb_root / "test_example.py")
    bb.pytest_collection_modifyitems(config=None, items=[item])
    assert "busybox" in item.added

    outsider = _FakeItem(TESTS_ROOT / "unit" / "test_example.py")
    bb.pytest_collection_modifyitems(config=None, items=[outsider])
    assert "busybox" not in outsider.added, (
        "the stamp must be scoped to its own tree — a hook that marks every "
        "item it is handed would deselect the whole suite from the default lanes"
    )


def _decorator_marker_names(node: "ast.FunctionDef | ast.AsyncFunctionDef") -> set[str]:
    """Marker names in one function's decorator list (``@pytest.mark.<name>``).

    Walks each decorator rather than matching its top node, so the called form
    (``@pytest.mark.parametrize(...)``) is seen alongside the plain one.
    """
    names: set[str] = set()
    for decorator in node.decorator_list:
        for sub in ast.walk(decorator):
            if (
                isinstance(sub, ast.Attribute)
                and isinstance(sub.value, ast.Attribute)
                and sub.value.attr == "mark"
            ):
                names.add(sub.attr)
    return names


def test_every_busybox_test_declares_the_marker_it_is_also_stamped_with():
    """G9b: ``tests/busybox/conftest.py`` calls those decorators LOAD-BEARING.

    Nothing enforced it. Deleting all three left the tier at 13 passed, because
    the directory stamp (G9) covers for them — which is exactly the redundancy
    the conftest's own note asks a reader NOT to simplify away, and an
    unenforced "do not" is a comment with a countdown on it.

    The reasons are the conftest's, and each survives only while the decorators
    do: the stamp's effect depends on collection-hook ORDER, which in this repo
    varies with the invocation shape, while a decorator does not; and the stamp
    cannot survive its own file, so deleting or renaming that conftest silently
    returns every test here to the catch-all lanes that fetch from busybox.net.

    Stated over test FUNCTIONS, not modules: a module-level check passes while
    one of three decorators is gone, and one silently-unmarked test in a
    conftest-less future is the whole failure. A module-level ``pytestmark``
    satisfies it too — that is the same declaration written once.
    """
    tier = TESTS_ROOT / "busybox"
    assert tier.is_dir(), "tests/busybox vanished — the G9b decorator guard is scanning nothing"

    seen = 0
    offenders: list[str] = []
    for module in sorted(tier.rglob("test_*.py")):
        tree = ast.parse(module.read_text())
        if "busybox" in _module_pytestmark_names(tree):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if not node.name.startswith("test_"):
                continue
            seen += 1
            if "busybox" not in _decorator_marker_names(node):
                offenders.append(f"{module.relative_to(TESTS_ROOT)}::{node.name}")
    assert seen, "no test function found under tests/busybox (guard misparse?)"
    assert not offenders, (
        f"these tests are stamped `busybox` by tests/busybox/conftest.py but do "
        f"not say so themselves: {offenders}. The stamp depends on collection-hook "
        f"order and cannot survive its own file being renamed; the decorator is "
        f"what still holds in those cases. Add `@pytest.mark.busybox`, or a "
        f"module-level `pytestmark`."
    )


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


def _string_constants(tree: ast.Module) -> "dict[str, str]":
    """Name -> value for every ``NAME = "..."`` assignment in *tree*.

    Walks the whole tree rather than only ``tree.body``, so a constant
    assigned inside a function is resolved too; every such name in this
    repo's build files is in fact module-level (``DASHBOARD_MARKER_EXPR``,
    ``PRIMARY_PYTHON``, ``DEEP_PYTHON``).
    """
    return {
        node.targets[0].id: node.value.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    }


def _python_marker_expressions(source: Path) -> list[str]:
    """Every marker expression passed via ``-m`` anywhere in *source*.

    Scans argument sequences (call args, tuple/list literals — the latter
    catches shared arg bundles like ``HOSTLESS_TEST_ARGS``, and the ``Tier``
    argv lists in scripts/stability_campaign.py) for a ``"-m"`` constant and
    takes the string that follows it; a name reference is resolved from
    module-level assignments (``DASHBOARD_MARKER_EXPR``).

    Takes a path rather than reading noxfile.py directly because noxfile.py is
    not the only Python file that builds pytest lanes: the stability campaign
    driver spells its own, and a guard that cannot see a file cannot protect
    it.
    """
    tree = ast.parse(source.read_text())
    assigns = _string_constants(tree)
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


def _nox_marker_expressions() -> list[str]:
    """Every ``-m`` expression in noxfile.py."""
    return _python_marker_expressions(_NOXFILE)


def test_catchall_nox_sessions_exclude_stability():
    """G4: negation-only nox selections must exclude the stability tier.

    The stability tests are bed-HOSTILE by design (e.g. the SIGSTOP-wedge
    test stops test2's sshd listener for tens of seconds), so they may only
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
    # Not a skip: the tree has existed since the chaos-hardening work landed,
    # so its absence now means the lane moved and this guard is scanning
    # nothing — the stale "not created yet" skip would hide that forever.
    assert chaos_dir.is_dir(), "tests/e2e/chaos vanished — the G5 marker guard is scanning nothing"
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


def _makefile_marker_variables(text: str) -> "dict[str, str]":
    """The ``M_* := ...`` marker-expression variables the Makefile's lanes share."""
    return dict(re.findall(r"^(M_[A-Z_]+) := (.+)$", text, re.MULTILINE))


def _expand_makefile_variables(expr: str, variables: "dict[str, str]") -> str:
    """*expr* with every ``$(M_*)`` reference replaced by its definition."""
    for name, value in variables.items():
        expr = expr.replace(f"$({name})", value)
    return expr


def _makefile_marker_expressions(text: str) -> list[str]:
    """Every ``-m`` expression in the Makefile, with ``M_*`` variables expanded.

    Comment lines are dropped first: the marker-policy prose above ``M_UNIX``
    quotes real expressions, and a scanner that reads documentation as
    configuration fails on the wrong thing.
    """
    variables = _makefile_marker_variables(text)
    live = "\n".join(line for line in text.splitlines() if not line.strip().startswith("#"))
    return [
        _expand_makefile_variables(raw, variables) for raw in re.findall(r'-m\s+"([^"]+)"', live)
    ]


def _catchall(exprs: "list[str]") -> list[str]:
    """The expressions built purely from negations — they select everything else."""
    return [e for e in exprs if all(c.strip().startswith("not ") for c in e.split(" and "))]


def test_catchall_lanes_exclude_busybox():
    """G8: no default lane may fetch from busybox.net.

    The `busybox` tier downloads ~5 MB of real artifacts from busybox.net and
    RAISES rather than skipping when it cannot get them (that refusal is
    deliberate — see tests/_fixtures/busybox.py — because a silent skip is how
    the tier's coverage would evaporate unnoticed). Both halves of that are
    fine in a dedicated lane and wrong in a default one: left selected, an
    upstream outage reds the ordinary CI gate, and every cold-cache run pays
    the download.

    An all-negation expression selects everything else and so needs the clause
    spelled out — same rule and same reasoning as G4's `not stability`,
    applied to both build files because both hand-edit their own lanes.

    This guard once also argued that positive selectors need no exclusion
    "by construction", because the marked tests carried no other resource
    marker. That stopped being true when Tier 1 landed under
    tests/integration/ and was auto-stamped `integration`. The claim now
    belongs to G8d, which derives what each lane can select instead of
    reasoning about the shape of its expression; this guard keeps only the
    catch-all half, whose message names the failure precisely.
    """
    makefile = _makefile_marker_expressions((PROJECT_ROOT / "Makefile").read_text())
    nox = _nox_marker_expressions()
    for label, exprs in (("Makefile", makefile), ("noxfile.py", nox)):
        catchall = _catchall(exprs)
        assert catchall, f"no catch-all -m expressions found in {label} (guard misparse?)"
        offenders = [e for e in catchall if "not busybox" not in e]
        assert not offenders, (
            f"{label} catch-all lanes missing 'not busybox' — each would fetch "
            f"real artifacts from busybox.net on every cold cache: {offenders}"
        )


def test_the_busybox_tier_still_has_a_lane():
    """G8b: excluding a tier everywhere and running it nowhere is not a fix.

    G8 removes `busybox` from every default lane, which on its own would
    delete the tier's coverage while every gate stayed green — the exact
    failure the fixture's raise-don't-skip rule exists to prevent, arrived at
    from the other side. Pin the opt-in lane that G8 presupposes.
    """
    makefile = (PROJECT_ROOT / "Makefile").read_text()
    positive = [e for e in _makefile_marker_expressions(makefile) if "busybox" in e.split(" and ")]
    assert positive, (
        "no Makefile lane positively selects `-m busybox`, so the tier G8 "
        "excludes from every default lane now runs nowhere"
    )


def _autostamped_markers() -> "dict[str, list[Path]]":
    """Marker -> EVERY directory whose conftest stamps it on the items below.

    Derived from the conftests instead of listed here, so a tier that starts
    auto-stamping is covered the day it lands rather than the day someone
    remembers this guard exists.

    A list per marker, not one directory. The single-directory version was
    wrong in the direction that hides things: with `dict[str, Path]` only the
    last-sorted conftest survived, so a SECOND directory stamping the same
    marker evicted the first and both G8d and G8e went quiet about it — a
    stamped-only tier outside `testpaths` passed both. That is not
    hypothetical: a second `tests/busybox/*/conftest.py` was planned while the
    artifact tier still had sub-tiers, and adding it would have evicted
    `tests/busybox`, leaving the current tier
    visible to these guards only because every module still happens to type
    `@pytest.mark.busybox`.
    """
    stamped: "dict[str, list[Path]]" = {}
    for conftest in sorted(TESTS_ROOT.rglob("conftest.py")):
        for marker in re.findall(r'item\.add_marker\(\s*"(\w+)"\s*\)', conftest.read_text()):
            stamped.setdefault(marker, []).append(conftest.parent)
    return stamped


def _marker_sets_of_modules_carrying(marker: str) -> "list[set[str]]":
    """Every distinct marker set a *marker*-carrying module ends up with.

    A module's markers are what it writes plus what its location stamps on it,
    and BOTH halves decide membership here: a module counts as carrying
    *marker* when it says so in its own text OR when a conftest above it
    stamps that marker by directory. Text alone was not enough — the tier this
    guard exists for is now stamped by ``tests/busybox/conftest.py``, so a file
    there that never types the marker still runs in the lane, and a guard that
    could not see it would go quiet exactly when the stamp did its job.

    Kept as separate sets rather than unioned, because a union would credit
    every location with every other location's markers and report lanes that
    cannot in fact reach the tier: ``tests/unit/host/`` stamps nothing, while
    ``tests/busybox/`` stamps ``busybox``.
    """
    stamped = _autostamped_markers()
    assert stamped, "no conftest stamps a marker by directory (guard misparse?)"
    needle = f"pytest.mark.{marker}"
    sets = []
    for module in sorted(TESTS_ROOT.rglob("test_*.py")):
        inherited = {
            name for name, roots in stamped.items() if any(root in module.parents for root in roots)
        }
        if marker not in inherited and needle not in module.read_text():
            continue
        sets.append({marker} | inherited)
    assert sets, f"nothing carries the {marker} marker (tier deleted? guard misparse?)"
    return sets


def _can_select(expr: str, markers: "set[str]") -> bool:
    """Whether the ``-m`` conjunction *expr* selects a test carrying *markers*."""
    terms = [term.strip() for term in expr.split(" and ")]
    positive = {term for term in terms if not term.startswith("not ")}
    negative = {term[len("not ") :].strip() for term in terms if term.startswith("not ")}
    return positive <= markers and not (negative & markers)


def test_no_lane_but_the_busybox_lane_can_select_the_busybox_tier():
    """G8d: G8's premise is only true while the tier stays where it is.

    G8 excludes `busybox` from the catch-alls and argued the positive
    selectors need no exclusion "by construction", because the marked tests
    carry no other resource marker. Tier 1 falsified that for one commit: it
    was first written to ``tests/integration/busybox/``, where the integration
    conftest's auto-stamp (G1) added ``integration`` to all fifteen of its
    tests, and ``M_UNIX`` / nox's ``tests_unix`` — bare positive selectors on
    ``integration``, which no catch-all's exclusion protects — began selecting
    them. Measured, not predicted: a ``--collect-only`` under M_UNIX's
    expression returned all five of Tier 1's parametrisations, putting a ~5 MB
    busybox.net fetch, and a fixture that RAISES rather than skips when
    upstream is down, inside the bed lane.

    The tier moved to ``tests/busybox/`` and the premise became true again, so
    no lane carries a `not busybox` clause on its account today. That is
    exactly why this guard is worth keeping and why it is written the way it
    is: the fix was a FILE MOVE, which nothing in a marker expression records,
    and the next tier dropped under an auto-stamping directory would reopen
    the hole silently. The rule is therefore stated over what a lane can
    actually SELECT rather than over the shape of its expression — a lane
    reaches the tier when every positive term is a marker the tier carries and
    no negated term is. That subsumes G8's catch-all rule (positives are empty
    there, so only the negation answers), spares lanes that merely look
    dangerous (``stability and integration and ...`` cannot reach a tier
    carrying no ``stability``), and re-derives its premise on every run
    instead of remembering it.
    """
    tier = _marker_sets_of_modules_carrying("busybox")
    surfaces = (
        ("Makefile", _makefile_marker_expressions((PROJECT_ROOT / "Makefile").read_text())),
        ("noxfile.py", _nox_marker_expressions()),
        # Not a live offender today (`-m concurrency` and `-m "stability and
        # integration and not embedded"` reach no busybox-marked test), and
        # that is exactly why it is listed: a lane this guard cannot SEE is
        # protected only by nobody having written the wrong selector there yet.
        ("scripts/stability_campaign.py", _python_marker_expressions(_STABILITY_CAMPAIGN)),
    )
    offenders = []
    for label, exprs in surfaces:
        assert exprs, f"no -m expressions found in {label} (guard misparse?)"
        for expr in exprs:
            # The opt-in lane is the one place selecting the tier is the point.
            if "busybox" in {term.strip() for term in expr.split(" and ")}:
                continue
            if any(_can_select(expr, markers) for markers in tier):
                offenders.append(f"{label}: {expr!r}")
    assert not offenders, (
        f"these lanes reach the BusyBox tier without asking for it, so each "
        f"fetches real artifacts over the network and fails hard when upstream "
        f"is down: {offenders}. Add `not busybox`, or move the tier out of a "
        f"directory whose conftest stamps a marker they select."
    )


def _directories_carrying(marker: str) -> "set[Path]":
    """Directories holding a test module that ends up *marker*-marked."""
    stamped = _autostamped_markers()
    needle = f"pytest.mark.{marker}"
    return {
        module.parent
        for module in TESTS_ROOT.rglob("test_*.py")
        if needle in module.read_text()
        or any(root in module.parents for root in stamped.get(marker, []))
    }


def test_the_busybox_tier_is_visible_to_a_pathless_run():
    """G8e: a tier absent from `testpaths` is deleted from CI, silently and green.

    `make busybox` is `pytest -m busybox` with NO path argument, and a
    path-less invocation collects `testpaths` and nothing else. So a tier
    directory that is not under one of those roots is not merely deselected —
    it is never collected, the lane reports a smaller number, and every gate
    stays green. Measured while moving Tier 1 out of tests/integration/: with
    `tests/busybox` missing from `testpaths`, `make busybox` collected and
    passed FIVE tests instead of twenty, and nothing anywhere said so.

    G8b and G8c pin that the lane exists and that CI invokes it. Neither can
    see this, because both reason about the lane rather than about what the
    lane can reach. Derived from where the marked modules actually live, so a
    directory that starts carrying marked modules is covered the day it lands.
    """
    ini = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text())
    roots = [PROJECT_ROOT / p for p in ini["tool"]["pytest"]["ini_options"]["testpaths"]]
    assert roots, "no testpaths in pyproject.toml (guard misparse?)"

    directories = _directories_carrying("busybox")
    assert directories, "no directory holds a busybox-marked module (guard misparse?)"
    invisible = sorted(
        str(d.relative_to(PROJECT_ROOT))
        for d in directories
        if not any(root == d or root in d.parents for root in roots)
    )
    assert not invisible, (
        f"{invisible} hold busybox-marked tests but lie outside `testpaths`, so a "
        f"path-less `pytest -m busybox` never collects them — the lane would pass "
        f"while running a subset of the tier it names"
    )


# Options that take their value as the NEXT argv token, so that token is not a
# path however much it looks like one. Only `-m`-less invocations are scanned
# below, and for those the direction of a miss matters: an unlisted
# value-taking option whose value happens to exist on disk (`--output
# reports/playwright`) would turn a PATH-LESS invocation — which collects all
# of `testpaths`, the widest reach there is — into one that looks scoped to a
# harmless directory. That is the one error that hides an offender, so the
# list is of every such option this repo's lanes actually pass.
_VALUE_FLAGS = frozenset(
    {
        "-c",
        "-k",
        "-m",
        "-n",
        "-o",
        "-p",
        "--browser",
        "--cov-report",
        "--count",
        "--deselect",
        "--ignore",
        "--junitxml",
        "--output",
        "--repeat-scope",
    }
)


def _python_pytest_invocations(source: Path, *, resolve_names: bool = False) -> "list[list[str]]":
    """Every ``pytest`` argv built in *source*, each kept whole as its literal tokens.

    A second pass over the same syntax :func:`_python_marker_expressions`
    walks, deliberately not an extension of it. That helper answers "which
    marker expressions exist here", and a list of expressions cannot
    distinguish a lane that HAS no ``-m`` from a lane the scanner never saw —
    which is precisely the distinction G8f is about. Keeping each invocation
    whole makes the ABSENCE of a flag readable.

    ``*BUNDLE`` arguments are expanded from module-level tuple/list
    assignments (``HOSTLESS_TEST_ARGS``). Non-literal arguments are dropped
    (``_junitxml(session, ...)``, ``*session.posargs``, and by default
    ``DASHBOARD_MARKER_EXPR``); every way that can be wrong is wrong in the
    loud direction FOR G8f — a dropped path makes an invocation look path-less
    and so WIDER, and a dropped ``-m`` puts it in front of that guard rather
    than past it.

    ``resolve_names=True`` additionally substitutes string constants for their
    bare-name references (:func:`_string_constants`). Off by default because
    dropping them is the safe direction for G8f's presence question, and ON
    for the caller that needs a flag's VALUE and not just its presence: the
    lane-leg membership gate in ``tests/unit/test_lane_invariants.py``, where
    a dropped ``DASHBOARD_MARKER_EXPR`` leaves ``-m`` adjacent to
    ``"--browser"`` and the next token would be read as the marker expression.
    """
    tree = ast.parse(source.read_text())
    constants = _string_constants(tree) if resolve_names else {}
    bundles: "dict[str, list[ast.expr]]" = {
        node.targets[0].id: list(node.value.elts)
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
        and isinstance(node.value, (ast.Tuple, ast.List))
    }

    def literals(seq: "list[ast.expr]") -> "list[str]":
        out: "list[str]" = []
        for elt in seq:
            if isinstance(elt, ast.Starred) and isinstance(elt.value, ast.Name):
                out += [
                    e.value
                    for e in bundles.get(elt.value.id, [])
                    if isinstance(e, ast.Constant) and isinstance(e.value, str)
                ]
            elif isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                out.append(elt.value)
            elif isinstance(elt, ast.Name) and elt.id in constants:
                out.append(constants[elt.id])
        return out

    found: "list[list[str]]" = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            seq = list(node.args)
        elif isinstance(node, (ast.Tuple, ast.List)):
            seq = list(node.elts)
        else:
            continue
        tokens = literals(seq)
        if "pytest" in tokens:
            found.append(tokens)
    return found


def _makefile_pytest_invocations(text: str) -> "list[list[str]]":
    """Every ``pytest`` argv in the Makefile's recipes, each kept whole.

    Backslash continuations are joined first — the soak lanes spell one
    invocation across six lines — and comment lines dropped, for the same
    reason :func:`_makefile_marker_expressions` drops them: the marker-policy
    prose quotes real invocations, and a scanner that reads documentation as
    configuration fails on the wrong thing.
    """
    lines: "list[str]" = []
    pending = ""
    for raw in text.splitlines():
        if raw.strip().startswith("#"):
            continue
        stripped = raw.rstrip()
        if stripped.endswith("\\"):
            pending += stripped[:-1] + " "
            continue
        lines.append(pending + raw)
        pending = ""

    found: "list[list[str]]" = []
    for line in lines:
        if not re.search(r"\bpytest\b", line):
            continue
        try:
            tokens = shlex.split(line)
        except ValueError:  # pragma: no cover - unbalanced quotes would be a Make error too
            continue
        if "pytest" in tokens:
            found.append(tokens)
    return found


def _selected_roots(tokens: "list[str]") -> "list[Path]":
    """The paths an invocation restricts collection to; empty means path-less."""
    roots: "list[Path]" = []
    rest = tokens[tokens.index("pytest") + 1 :]
    skip = False
    for token in rest:
        if skip:
            skip = False
            continue
        if token in _VALUE_FLAGS:
            skip = True
            continue
        if token.startswith("-"):
            continue
        candidate = PROJECT_ROOT / token
        if candidate.exists():
            roots.append(candidate)
    return roots


def _modules_carrying(marker: str) -> "set[Path]":
    """Test modules that end up *marker*-marked — by their own decorators or a stamp.

    The module's own half is decided by AST (``_module_and_decorator_markers``)
    and not by a text needle. ``_directories_carrying``, which G8e uses, greps
    for the string and therefore counts THIS file, whose only mentions of
    ``pytest.mark.busybox`` are in prose. Harmless there — G8e asks whether a
    directory sits inside ``testpaths`` and ``tests/unit`` does — and not
    harmless here, where the same false positive would condemn every
    path-selected lane that touches ``tests/unit`` for a docstring. The text
    scan in G8d/G8e — and in their G11d/G11e analogues, which since this file
    grew conformance prose count ``tests/unit`` as a conformance-carrying
    directory for exactly that reason — is recorded debt; it is not inherited.
    """
    stamped = _autostamped_markers().get(marker, [])
    needle = f"pytest.mark.{marker}"
    found: "set[Path]" = set()
    for module in TESTS_ROOT.rglob("test_*.py"):
        # Short-circuit order matters for cost, not correctness: this walks
        # every test module in the tree, and the text needle gates the parse so
        # only the handful that mention the marker are read as syntax.
        if any(root in module.parents for root in stamped) or (
            needle in module.read_text() and marker in _module_and_decorator_markers(module)
        ):
            found.add(module)
    return found


def test_a_lane_that_selects_by_path_cannot_reach_the_busybox_tier():
    """G8f: G8, G8b, G8c, G8d and G8e all reason over ``-m``. A lane need not have one.

    ``tests_unit_repeat`` did not. It ran ``pytest tests/unit`` with no marker
    expression at all, selecting purely by PATH, and
    ``tests/unit/host/test_busybox_artifacts.py`` holds the tier's five
    network-fetching parametrisations. So a job on every push and every PR — a
    member of ``report-failure``'s ``needs`` — downloaded ~5 MB from
    busybox.net on every cold cache and would have gone red on an upstream
    outage. Measured, not predicted: the lane's exact invocation against an
    empty ``OTTO_BUSYBOX_CACHE`` reported ``10 passed`` and left all five
    artifacts in the cache.

    None of the five guards above could see it, and not one of them was
    written badly: each derives what a MARKER EXPRESSION can select, and an
    invocation with no marker expression is outside that domain by
    construction. This one is stated over the complementary half — what a
    lane's PATHS can reach when nothing deselects anything — so the family
    covers both ways a lane can pick tests. A path-less invocation is the
    widest case of all, not the narrowest: it collects ``testpaths``, which is
    where the tier deliberately lives (G8e).

    The rule is deliberately about reach rather than about the fix: adding
    ``-m "not busybox"`` satisfies it, and so does moving the marked modules
    out of every path such a lane names.

    SURFACE BOUND, stated here because a guard that reads as complete is how
    the next offender gets in. This parses ``Makefile``, ``noxfile.py`` and
    ``scripts/stability_campaign.py``. It does NOT see a raw ``pytest``
    written directly into a GitHub workflow step, and two such invocations
    are live right now — the ``chaos-tier2`` and ``chaos-docker`` jobs in
    ``.github/workflows/nightly.yml``, which run ``pytest`` on a path
    directly rather than through a make target. Named by job rather than by
    line, because the line moved the first time anything was inserted above
    them. Neither reaches this tier today, which is why the tree is
    compliant, but they are the shape this guard cannot judge, not a
    hypothetical one. It
    also misses a ``pytest`` buried inside ``bash -c "..."``, which shlex
    hands over as a single token; a ``$(VAR)``-supplied pytest IS caught.
    Widening the surface to the workflows is the obvious next step if a
    marked tier ever needs to run there.
    """
    ini = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text())
    testpaths = [PROJECT_ROOT / p for p in ini["tool"]["pytest"]["ini_options"]["testpaths"]]
    assert testpaths, "no testpaths in pyproject.toml (guard misparse?)"

    carrying = _modules_carrying("busybox")
    assert carrying, "no module carries the busybox marker (tier deleted? guard misparse?)"

    surfaces = (
        ("Makefile", _makefile_pytest_invocations((PROJECT_ROOT / "Makefile").read_text())),
        ("noxfile.py", _python_pytest_invocations(_NOXFILE)),
        ("scripts/stability_campaign.py", _python_pytest_invocations(_STABILITY_CAMPAIGN)),
    )
    markerless: "list[tuple[str, list[str]]]" = []
    for label, invocations in surfaces:
        assert invocations, f"no pytest invocation found in {label} (guard misparse?)"
        # A ``--collect-only`` lane imports every module it names and executes
        # none: the tier's side effects — the artifact download here, the
        # throwaway sshd in G11f — live in test bodies and fixtures, which
        # collection never reaches. ``collect-check`` is such a lane on purpose
        # (a forgotten ``git add`` of a marked module is exactly what it exists
        # to catch), so it is exempt from a rule about what a lane RUNS.
        markerless += [
            (label, tokens)
            for tokens in invocations
            if "-m" not in tokens and "--collect-only" not in tokens
        ]
    # Without this the guard is green whenever the scanners break, which is the
    # one failure mode a scanner-based rule really has. Two `-m`-less lanes
    # exist today after the collect-only exemption (both `pytest ... src/otto`
    # doctest runs), so an empty result
    # means the parse stopped seeing invocations, not that the repo stopped
    # having them.
    assert markerless, (
        "no `-m`-less pytest invocation found in any surface (guard misparse?) — "
        "this rule is about the lanes marker reasoning cannot see, so with none "
        "of them in view it proves nothing"
    )

    offenders: "list[str]" = []
    for label, tokens in markerless:
        roots = _selected_roots(tokens) or testpaths
        reached = sorted(
            {
                str(module.relative_to(PROJECT_ROOT))
                for module in carrying
                for root in roots
                if root == module or root in module.parents
            }
        )
        if reached:
            argv = " ".join(tokens[tokens.index("pytest") :])
            offenders.append(f"{label}: `{argv}` reaches {reached}")
    assert not offenders, (
        f"these lanes pick their tests by PATH with no `-m` expression, so "
        f"nothing deselects the BusyBox tier: each one downloads ~5 MB of real "
        f"artifacts on a cold cache and fails hard when upstream is down. "
        f'{offenders}. Add `-m "not busybox"`, or move the marked modules out '
        f"of the paths the lane names."
    )


def _makefile_recipes() -> "dict[str, str]":
    """Target name -> its recipe text, for every Makefile target with a recipe."""
    makefile = (PROJECT_ROOT / "Makefile").read_text()
    blocks = re.finditer(r"^([a-zA-Z0-9_.-]+):[^\n]*\n((?:\t[^\n]*\n)+)", makefile, re.MULTILINE)
    return {block.group(1): block.group(2) for block in blocks}


def _makefile_targets_selecting(marker: str) -> "list[str]":
    """Names of the Makefile targets whose recipe positively selects *marker*.

    Derived from the recipe text rather than hardcoded, so renaming the lane
    moves the guard with it instead of quietly leaving it pinned to a target
    that no longer exists.

    Positive membership is decided TERM BY TERM -- the same reading G8b and
    G11b already use on the same expressions -- and not by matching the whole
    `-m "..."` string. The whole-string form was here first and was wrong in
    the direction that hides a lane: it recognised `-m "conformance"` and
    stopped recognising the SAME lane the moment it was refined to
    `-m "conformance and not conformance_bed"`, which made G11c report that no
    Makefile target selected the tier at all while the target sat unchanged
    two lines above its own recipe. Measured -- that is exactly how this
    widening was found.

    Deliberately does NOT expand `$(M_*)` references the way
    `_makefile_marker_expressions` does. A lane that selects a tier through a
    shared variable is a catch-all reaching it by accident, which is G8/G11's
    subject; this helper answers "which lane IS the tier's own", and an
    expanded `M_UNIX` would start answering that with `coverage-unix`.
    """
    selecting = []
    for target, recipe in _makefile_recipes().items():
        for expr in re.findall(r'-m\s+"([^"]*)"', recipe):
            if marker in {term.strip() for term in expr.split(" and ")}:
                selecting.append(target)
                break
    return selecting


def _workflows() -> "dict[str, dict]":
    """Every workflow under `.github/workflows/`, by filename."""
    root = PROJECT_ROOT / ".github" / "workflows"
    return {path.name: yaml.safe_load(path.read_text()) for path in sorted(root.glob("*.yml"))}


def _workflow_jobs_invoking(target: str) -> "list[tuple[str, str]]":
    """`(workflow filename, job name)` for each job whose `run` step calls `make <target>`.

    Searched across every workflow rather than inside one this function names,
    because WHICH workflow a lane runs in is a scheduling decision and not part
    of the property being guarded: the BusyBox tier runs per-push in `ci.yml`,
    the conformance tier runs nightly because its cell draw is random per run,
    and either could move without the tier's coverage changing at all. A guard
    pinned to a filename would go red on that move instead of following it.

    Word-boundary on both sides, with `(?![-\\w])` closing the right-hand
    side: `make busybox-cache` primes the cache and runs no tests, so a plain
    substring match would report the tier covered by the one target that most
    looks like the lane and least is it. That is a live case rather than a
    hypothetical one — nightly's `conformance-hermetic` job runs
    `make busybox-cache` and then `make conformance`, so a loose match there
    would credit the BusyBox lane to a job that never runs it.
    """
    pattern = rf"\bmake\b[^\n&|;]*\b{re.escape(target)}\b(?![-\w])"
    return [
        (filename, job_name)
        for filename, workflow in _workflows().items()
        for job_name, job in workflow.get("jobs", {}).items()
        if any(re.search(pattern, step["run"]) for step in job.get("steps", []) if "run" in step)
    ]


def _jobs_outside_failure_reporting(running: "list[tuple[str, str]]") -> "list[str]":
    """`workflow:job` for each invoking job whose own workflow would not report its failure.

    Both workflows that run an opt-in tier today carry a `report-failure` job
    wired to a `needs` list — that list is how a broken `main` opens an issue
    instead of waiting for someone to watch the Actions tab. Two ways a job
    falls outside it, and a guard that checked only the second would pass
    vacuously on a workflow that has no reporter at all, so both are named.
    """
    workflows = _workflows()
    offenders: "list[str]" = []
    for filename, job_name in running:
        reporter = workflows[filename]["jobs"].get("report-failure")
        if reporter is None:
            offenders.append(f"{filename}:{job_name} (that workflow has no report-failure job)")
            continue
        needs = reporter.get("needs") or []
        if isinstance(needs, str):
            needs = [needs]
        if job_name not in needs:
            offenders.append(f"{filename}:{job_name} (absent from report-failure's `needs`)")
    return offenders


def _advisory_jobs(running: "list[tuple[str, str]]") -> "list[str]":
    """`workflow:job` for each invoking job that reports success whatever it finds.

    A job can be present, wired into `needs`, and still not matter:
    `continue-on-error: true` is the cheapest possible way to neutralise a tier
    while every other assertion about it stays green.
    """
    workflows = _workflows()
    return [
        f"{filename}:{job_name}"
        for filename, job_name in running
        if workflows[filename]["jobs"][job_name].get("continue-on-error") is True
    ]


def test_ci_runs_the_busybox_lane():
    """G8c: a lane nothing invokes is the same evaporated coverage as no lane.

    G8b pins that an opt-in Makefile lane EXISTS; it cannot see whether
    anything ever runs it, and for a while nothing did. The tier needs no qemu
    on an x86_64 runner — `ubuntu-latest` executes the artifacts natively — so
    there was never a technical reason for the gap, only the order the work
    landed in. Asserted against the workflows rather than against a job name of
    this test's choosing: the lane is derived from the Makefile and the
    invocation is looked for across every job of every workflow, so renaming
    either end — or moving the job between workflows — moves the guard instead
    of breaking it.

    Also pins the job into its workflow's `report-failure` `needs`. That list
    is how a broken `main` opens an issue instead of waiting for someone to
    watch the Actions tab; a job absent from it fails silently in exactly the
    case the reporting exists for.
    """
    lanes = _makefile_targets_selecting("busybox")
    assert lanes, "no Makefile target positively selects `-m busybox` (G8b covers this)"

    running = sorted({pair for lane in lanes for pair in _workflow_jobs_invoking(lane)})
    assert running, (
        f"no CI job invokes any of {lanes} — every default lane excludes "
        f"`-m busybox` (G8), so the whole tier runs nowhere in CI"
    )

    unreported = _jobs_outside_failure_reporting(running)
    assert not unreported, (
        f"{unreported} run the BusyBox lane but their failure opens no tracking "
        f"issue, so a broken main waits for someone to watch the Actions tab"
    )

    advisory = _advisory_jobs(running)
    assert not advisory, (
        f"{advisory} run the BusyBox lane with `continue-on-error: true`, so the "
        f"tier can fail without failing anything — decorative, not blocking"
    )


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
    # The e2e hook is a wrapper (wrapper=True): the stamp runs pre-yield, the
    # offender re-append post-yield — drive both halves like pluggy would.
    gen = e2e.pytest_collection_modifyitems(config=None, items=[item])
    next(gen)
    with contextlib.suppress(StopIteration):
        gen.send(None)
    assert "e2e" in item.added


# --- The host-contract conformance tier (tests/conformance/) ------------------
#
# The BusyBox guards above are the template, and the resource is different:
# this tier stands up a throwaway non-root sshd on 127.0.0.1 and runs the five
# pinned BusyBox artifacts as local subprocesses, one cell at a time. What makes
# it need the same family of guards is the shape of its exclusion, not its cost:
# a conformance test carries none of the markers M_HOSTLESS negates, so it
# satisfies every clause of every catch-all and is SELECTED by the ordinary
# gates unless `not conformance` is spelled out. That became live the moment
# `tests/conformance` joined `testpaths`, which it had to — see G11e.
#
# G11c carries the BusyBox tier's "CI actually runs the lane" rule here, and it
# is younger than the rest of this family: it was left out on purpose while no
# workflow under .github/workflows mentioned `conformance` at all, since a
# guard asserting CI ran the tier would then have been red on arrival. The
# nightly `conformance-hermetic` job closed that, so the rule is now a test
# rather than a note explaining its own absence.
#
# One difference from G8c's tier, and it is a property of the lane rather than
# an accident of where the work landed: this one is NIGHTLY, not per-push,
# because each run draws its cells at random off the session seed, so a
# per-push gate could fail an unrelated PR on pre-existing breakage in a cell
# nothing had drawn before. G11c therefore asserts that SOME workflow job runs
# the lane, never that a particular workflow does.


def test_conformance_conftest_autostamps_conformance():
    """G10: the tests/conformance/ conftest stamps `conformance` by directory.

    Same mechanism as G1/G3/G9 and the same invisible failure: an unmarked new
    file here rides every catch-all lane, and the first thing it does is bind a
    listening sshd and exec real BusyBox binaries inside `make coverage`.

    Importing that conftest is cheap and offline, which is why this guard can
    live in the no-VM unit gate at all: it resolves the cell space at import
    (measured 0.22s here) and `tests/conformance/_cells.py` deliberately defers
    every busybox.net fetch to the opener, so nothing is downloaded by the
    import alone.

    Read the name narrowly: this calls the hook, so it proves what the hook
    does and NOT that the stamp lands before pytest deselects on the marker.
    That ordering was measured separately, by dropping an unmarked throwaway
    module into this tree — it was deselected by `-m "not conformance"` under a
    path-named run, under a path-less run, and under a path-less run with
    `testpaths` narrowed to this tree — and it cannot be pinned by a committed
    guard, because the hostile condition it needs is an unmarked module here,
    which G10b forbids. G10b is what makes the ordering moot; G11g is what
    watches a real collection.
    """
    from tests.conformance import conftest as conf

    conf_root = Path(conf.__file__).parent

    class _FakeItem:
        def __init__(self, path: Path) -> None:
            self.path = path
            self.added: list[str] = []

        def add_marker(self, marker) -> None:
            self.added.append(getattr(marker, "name", str(marker)))

    item = _FakeItem(conf_root / "test_example.py")
    conf.pytest_collection_modifyitems(config=None, items=[item])
    assert "conformance" in item.added

    outsider = _FakeItem(TESTS_ROOT / "unit" / "test_example.py")
    conf.pytest_collection_modifyitems(config=None, items=[outsider])
    assert "conformance" not in outsider.added, (
        "the stamp must be scoped to its own tree — a hook that marks every "
        "item it is handed would deselect the whole suite from the default lanes"
    )


def test_every_conformance_test_declares_the_marker_it_is_also_stamped_with():
    """G10b: `tests/conformance/conftest.py` calls those `pytestmark`s LOAD-BEARING.

    That conftest asks a reader not to simplify them away as redundant with the
    stamp, and its reasons survive only while they are there: the stamp cannot
    outlive its own file, so deleting or renaming that conftest silently returns
    every contract here to the catch-all lanes; and the stamp's effect depends
    on collection-hook ORDER, which in this repo varies with the invocation
    shape, while a `pytestmark` is attached at item construction and does not.
    G9b exists because the same request went unenforced for the BusyBox tier and
    deleting the decorators left it green.

    Stated over test FUNCTIONS but satisfied by a module-level `pytestmark`,
    which is how all three contract modules declare it today: the count is what
    keeps the guard honest when a module stops declaring it and its functions
    have to answer one at a time. Written to count every test function it walks
    rather than skipping a module that declares the marker for them, so the
    premise assertion below measures the tier and not the leftovers.
    """
    tier = TESTS_ROOT / "conformance"
    assert tier.is_dir(), (
        "tests/conformance vanished — the G10b declaration guard is scanning nothing"
    )

    seen = 0
    offenders: list[str] = []
    for module in sorted(tier.rglob("test_*.py")):
        tree = ast.parse(module.read_text())
        declared_for_the_module = "conformance" in _module_pytestmark_names(tree)
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if not node.name.startswith("test_"):
                continue
            seen += 1
            if not declared_for_the_module and "conformance" not in _decorator_marker_names(node):
                offenders.append(f"{module.relative_to(TESTS_ROOT)}::{node.name}")
    assert seen, "no test function found under tests/conformance (guard misparse?)"
    assert not offenders, (
        f"these tests are stamped `conformance` by tests/conformance/conftest.py "
        f"but do not say so themselves: {offenders}. The stamp depends on "
        f"collection-hook order and cannot survive its own file being renamed; "
        f"the declaration is what still holds in those cases. Add a module-level "
        f"`pytestmark`, or `@pytest.mark.conformance` per test."
    )


def test_catchall_lanes_exclude_conformance():
    """G11: no default lane may stand up an sshd and run BusyBox subprocesses.

    The mirror of G8, with the clause that makes it necessary rather than
    merely tidy: the `conformance` marker is the ONLY thing that keeps this tree
    out of a negation-only selector. A conformance test carries no
    integration/embedded/stability/browser/busybox marker, so every `not X` in
    an existing catch-all is already satisfied and the tree is selected by
    default — unlike the BusyBox tier, which at least shares no directory with
    the lanes that could reach it.

    Both surfaces, because both are hand-edited and they are not checked
    against each other anywhere else: `nox -s tests_hostless` runs on five
    Pythons in CI, so a clause present in the Makefile and missing from
    noxfile.py is an escape that only CI would find.
    """
    makefile = _makefile_marker_expressions((PROJECT_ROOT / "Makefile").read_text())
    nox = _nox_marker_expressions()
    for label, exprs in (("Makefile", makefile), ("noxfile.py", nox)):
        catchall = _catchall(exprs)
        assert catchall, f"no catch-all -m expressions found in {label} (guard misparse?)"
        offenders = [e for e in catchall if "not conformance" not in e]
        assert not offenders, (
            f"{label} catch-all lanes missing 'not conformance' — each would bind "
            f"a throwaway sshd on 127.0.0.1 and exec real BusyBox artifacts as "
            f"subprocesses, once per drawn cell: {offenders}"
        )


def test_the_conformance_tier_still_has_a_lane():
    """G11b: excluding a tier everywhere and running it nowhere is not a fix.

    G11's mirror of G8b, and it stops at existence on purpose: whether anything
    ever RUNS that lane is a separate question with a separate failure mode,
    and G11c is what asks it.
    """
    makefile = (PROJECT_ROOT / "Makefile").read_text()
    positive = [
        e for e in _makefile_marker_expressions(makefile) if "conformance" in e.split(" and ")
    ]
    assert positive, (
        "no Makefile lane positively selects `-m conformance`, so the tier G11 "
        "excludes from every default lane now runs nowhere"
    )


def test_ci_runs_the_conformance_lane():
    """G11c: a lane nothing invokes is the same evaporated coverage as no lane.

    G8c's rule for this tier, and the same asymmetry with its sibling: G11b
    pins that an opt-in Makefile lane exists, which is a claim about the
    Makefile alone and stays green while nothing anywhere runs it. That was
    literally the state of this tier for three commits — every catch-all in
    both build files excluded it (G11) and no workflow mentioned it — which is
    a tier excluded everywhere and run nowhere, the deletion G11b exists to
    forbid, one level up.

    Derived at both ends rather than asserted against names of this test's
    choosing: the lane comes out of the Makefile and the invocation is looked
    for across every job of every workflow. So the job may be renamed, and the
    lane may be renamed, and it may move between workflows, and the guard
    follows it — which matters more here than for the BusyBox tier, because
    WHERE this lane runs is not settled the way that one's is. It is nightly
    today for a stated reason (the draw is random per run, so a per-push gate
    could fail an unrelated PR on pre-existing breakage in a never-drawn
    cell), and item 5 of the same workstream adds a second failure condition
    to that job.

    The `report-failure` half is not decoration on a nightly job — it is the
    half that matters MORE at night. Nobody watches a 06:00 UTC run; the
    tracking issue is the only thing that turns a red nightly into something
    anyone sees. And `continue-on-error: true` is named for G8c's reason: a
    job can be present, wired into `needs`, and still report success whatever
    it finds.
    """
    lanes = _makefile_targets_selecting("conformance")
    assert lanes, "no Makefile target positively selects `-m conformance` (G11b covers this)"

    running = sorted({pair for lane in lanes for pair in _workflow_jobs_invoking(lane)})
    assert running, (
        f"no workflow job invokes any of {lanes} — every default lane excludes "
        f"`-m conformance` (G11), so the host-contract conformance tier is "
        f"excluded everywhere and runs nowhere in CI"
    )

    unreported = _jobs_outside_failure_reporting(running)
    assert not unreported, (
        f"{unreported} run the conformance lane but their failure opens no "
        f"tracking issue — and this lane runs at night, where the issue is the "
        f"only thing anyone sees"
    )

    advisory = _advisory_jobs(running)
    assert not advisory, (
        f"{advisory} run the conformance lane with `continue-on-error: true`, so "
        f"a contract violation fails nothing — decorative, not blocking"
    )


def test_no_lane_but_the_conformance_lane_can_select_the_conformance_tier():
    """G11d: stated over what a lane can SELECT, not over the shape of its expression.

    G8d's rule applied to this tier, and the reason it is not redundant with
    G11 is the reason it was not redundant with G8: G11 only inspects the
    negation-only expressions, so a POSITIVE selector that happens to share a
    marker with this tree is outside its domain. That is not hypothetical for
    the family — Tier 1 of the BusyBox suite was auto-stamped `integration` for
    one commit and `M_UNIX` began selecting it — and the fix there was a FILE
    MOVE, which nothing in a marker expression records. Measured today: the
    modules here end up carrying `conformance` and nothing else, so no lane
    outside the opt-in one can reach them; that premise is re-derived on every
    run rather than remembered.

    Covers `scripts/stability_campaign.py` alongside the two build files for
    G8d's stated reason: a lane this guard cannot see is protected only by
    nobody having written the wrong selector there yet.
    """
    tier = _marker_sets_of_modules_carrying("conformance")
    surfaces = (
        ("Makefile", _makefile_marker_expressions((PROJECT_ROOT / "Makefile").read_text())),
        ("noxfile.py", _nox_marker_expressions()),
        ("scripts/stability_campaign.py", _python_marker_expressions(_STABILITY_CAMPAIGN)),
    )
    offenders = []
    for label, exprs in surfaces:
        assert exprs, f"no -m expressions found in {label} (guard misparse?)"
        for expr in exprs:
            # The opt-in lane is the one place selecting the tier is the point.
            if "conformance" in {term.strip() for term in expr.split(" and ")}:
                continue
            if any(_can_select(expr, markers) for markers in tier):
                offenders.append(f"{label}: {expr!r}")
    assert not offenders, (
        f"these lanes reach the conformance tier without asking for it, so each "
        f"stands up a throwaway sshd and runs the pinned BusyBox artifacts as "
        f"subprocesses: {offenders}. Add `not conformance`, or move the tier out "
        f"of a directory whose conftest stamps a marker they select."
    )


def test_the_conformance_tier_is_visible_to_a_pathless_run():
    """G11e: a tier absent from `testpaths` is not excluded, it is missing.

    `make conformance` is `pytest -m conformance` with NO path argument, and a
    path-less invocation collects `testpaths` and nothing else. G8e records the
    BusyBox version of this failure as a silent one — the lane reported a
    smaller number and stayed green. This tier's version is LOUD and that is
    worth writing down, because it changes what to look for: with
    `tests/conformance` absent, the lane selects nothing at all, and pytest
    exits 5 on an empty selection, which aborts the make recipe. Measured
    during Task 1 of this workstream: `no tests collected (7901 deselected)`,
    exit 5.

    Both failures have the same fix and the same guard, so this asserts
    membership rather than either symptom. Derived from where the marked
    modules actually live, so a new directory carrying them is covered the day
    it lands.
    """
    ini = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text())
    roots = [PROJECT_ROOT / p for p in ini["tool"]["pytest"]["ini_options"]["testpaths"]]
    assert roots, "no testpaths in pyproject.toml (guard misparse?)"

    directories = _directories_carrying("conformance")
    assert directories, "no directory holds a conformance-marked module (guard misparse?)"
    invisible = sorted(
        str(d.relative_to(PROJECT_ROOT))
        for d in directories
        if not any(root == d or root in d.parents for root in roots)
    )
    assert not invisible, (
        f"{invisible} hold conformance-marked tests but lie outside `testpaths`, "
        f"so a path-less `pytest -m conformance` never collects them — the lane "
        f"selects nothing, pytest exits 5, and `make conformance` aborts"
    )


def test_a_lane_that_selects_by_path_cannot_reach_the_conformance_tier():
    """G11f: every guard above reasons over `-m`. A lane need not have one.

    G8f's rule, and this tier is more exposed to it than the BusyBox one: a
    path-LESS invocation with no marker expression collects all of `testpaths`,
    which since Task 3 includes `tests/conformance`. So the widest possible lane
    is the one none of G11/G11d/G11e can judge.

    Inherits G8f's surface bound verbatim: this parses `Makefile`,
    `noxfile.py` and `scripts/stability_campaign.py`, and does NOT see a raw
    `pytest` written into a GitHub workflow step or one buried inside
    `bash -c "..."`.
    """
    ini = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text())
    testpaths = [PROJECT_ROOT / p for p in ini["tool"]["pytest"]["ini_options"]["testpaths"]]
    assert testpaths, "no testpaths in pyproject.toml (guard misparse?)"

    carrying = _modules_carrying("conformance")
    assert carrying, "no module carries the conformance marker (tier deleted? guard misparse?)"

    surfaces = (
        ("Makefile", _makefile_pytest_invocations((PROJECT_ROOT / "Makefile").read_text())),
        ("noxfile.py", _python_pytest_invocations(_NOXFILE)),
        ("scripts/stability_campaign.py", _python_pytest_invocations(_STABILITY_CAMPAIGN)),
    )
    markerless: "list[tuple[str, list[str]]]" = []
    for label, invocations in surfaces:
        assert invocations, f"no pytest invocation found in {label} (guard misparse?)"
        # A ``--collect-only`` lane imports every module it names and executes
        # none: the tier's side effects — the artifact download here, the
        # throwaway sshd in G11f — live in test bodies and fixtures, which
        # collection never reaches. ``collect-check`` is such a lane on purpose
        # (a forgotten ``git add`` of a marked module is exactly what it exists
        # to catch), so it is exempt from a rule about what a lane RUNS.
        markerless += [
            (label, tokens)
            for tokens in invocations
            if "-m" not in tokens and "--collect-only" not in tokens
        ]
    assert markerless, (
        "no `-m`-less pytest invocation found in any surface (guard misparse?) — "
        "this rule is about the lanes marker reasoning cannot see, so with none "
        "of them in view it proves nothing"
    )

    offenders: "list[str]" = []
    for label, tokens in markerless:
        roots = _selected_roots(tokens) or testpaths
        reached = sorted(
            {
                str(module.relative_to(PROJECT_ROOT))
                for module in carrying
                for root in roots
                if root == module or root in module.parents
            }
        )
        if reached:
            argv = " ".join(tokens[tokens.index("pytest") :])
            offenders.append(f"{label}: `{argv}` reaches {reached}")
    assert not offenders, (
        f"these lanes pick their tests by PATH with no `-m` expression, so "
        f"nothing deselects the conformance tier: each stands up a throwaway "
        f"sshd and runs the pinned BusyBox artifacts as subprocesses. "
        f'{offenders}. Add `-m "not conformance"`, or move the marked modules '
        f"out of the paths the lane names."
    )


def test_the_hostless_catchall_collects_the_conformance_tree_and_deselects_it():
    """G11g: the one reading no static scan can take — a real pathless collection.

    Every guard above reads build files and conftests as TEXT. None of them can
    see whether the marker is actually attached by the time pytest deselects on
    it, and that ordering is not obvious: `tests/conformance/conftest.py` is an
    *initial* conftest under any path-less run (it is reached through
    `testpaths`), initial conftests register early and therefore run LATE among
    same-hook plugins, and the mark plugin's deselection is one of those hooks.
    The conftest's own note records that this repo's collection-hook order
    varies with the invocation shape, which is why it refuses to stamp
    `xdist_group`.

    So this runs `make coverage`'s own selector — `M_HOSTLESS`, read out of the
    Makefile rather than retyped — in a subprocess, path-less, exactly as the
    gate does, and asserts BOTH halves of the property that matters:

    - no `tests/conformance/` node id survives the selection, and
    - the tree was nevertheless COLLECTED, evidenced by the draw line the
      conformance conftest logs at session start.

    Both, because "excluded" and "invisible" are the same green from one side.
    Dropping `tests/conformance` from `testpaths` would satisfy the first
    assertion alone while deleting the tier from the repo; G11e catches that
    statically and this catches it in the shape the lane actually runs.

    A subprocess and not an in-process `Config`: this suite's own `addopts`
    would be inherited, and the property is about a whole collection, not about
    an ini value. `PYTEST_ADDOPTS` is cleared for the child for the same reason
    Task 4's end-to-end sampler test clears it — an ambient `-n auto` would put
    the draw line on a worker whose log never reaches this pipe. `-n0` for the
    same reason.
    """
    variables = _makefile_marker_variables((PROJECT_ROOT / "Makefile").read_text())
    hostless = variables.get("M_HOSTLESS")
    assert hostless, "M_HOSTLESS not found in the Makefile (renamed? update G11g)"

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-m",
            hostless,
            "--collect-only",
            "-q",
            "-n0",
            "--no-cov",
            "-p",
            "no:cacheprovider",
        ],
        cwd=str(PROJECT_ROOT),
        env={**os.environ, "PYTEST_ADDOPTS": ""},
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    output = result.stdout + result.stderr
    assert result.returncode == 0, f"collection failed (rc={result.returncode}):\n{output}"

    selected = [line for line in output.splitlines() if line.startswith("tests/conformance/")]
    assert not selected, (
        f"M_HOSTLESS ({hostless!r}) selected {len(selected)} conformance tests, so "
        f"`make coverage` would stand up a throwaway sshd and run the pinned "
        f"BusyBox artifacts as subprocesses. First few: {selected[:3]}"
    )

    drew = [line for line in output.splitlines() if "conformance: drew " in line]
    assert len(drew) == 1, (
        f"the conformance tree logged {len(drew)} draws under a path-less "
        f"collection, expected exactly 1 — with none, the tree was never "
        f"collected at all and the assertion above passed by the tier being "
        f"MISSING rather than excluded (see G11e):\n{output[-2000:]}"
    )


def test_no_labless_lane_can_select_the_bed_marked_conformance_tests():
    """G11h: the `conformance` marker keeps the tree out of DEFAULT gates only.

    It cannot keep anything out of `make conformance`, which selects that
    marker on purpose — and that lane runs nightly in CI with no lab attached.
    Almost everything under tests/conformance/ is safe there because the tree
    is HERMETIC unless `OTTO_CONFORMANCE_BED=1` is set, so the venue knob is
    what decides whether a contract reaches hardware.

    The bed openers' witness is the exception the knob does not govern: it
    opens a real lab VM on every run, whichever venue is selected, because an
    opener verified only by a probe someone deleted is the defect it exists to
    prevent. So it carries `conformance_bed` and the hermetic lane subtracts
    it, and this is the guard on that subtraction — stated over what each lane
    can SELECT, the G8d/G11d reading, rather than over the shape of any one
    expression.

    THE EXEMPTION IS THE VENUE KNOB, NOT A TARGET NAME. A lane whose recipe
    sets `OTTO_CONFORMANCE_BED` is asking for the bed by definition and may
    select these tests; item 5 of this workstream adds exactly such a target.
    Keying on the knob rather than on `conformance-bed` means that target is
    covered the day it lands and cannot be spelled into an exemption by
    accident.

    NOT PAIRED with a "the bed-marked tests still have a lane" mirror, and the
    omission is deliberate rather than forgotten: no lane runs them yet (item
    5 is where `make conformance-bed` lands), so that guard would be red on
    arrival — the same reason G11c was left out until the nightly job existed.
    It belongs with the target, not ahead of it.
    """
    bed_sets = _marker_sets_of_modules_carrying("conformance_bed")
    assert bed_sets, "no module carries `conformance_bed` (guard misparse?)"

    recipes = _makefile_recipes()
    lanes = _makefile_targets_selecting("conformance")
    assert lanes, "no Makefile target positively selects `-m conformance` (G11b covers this)"

    offenders = []
    for lane in lanes:
        recipe = recipes[lane]
        if "OTTO_CONFORMANCE_BED" in recipe:
            continue
        offenders += [
            f"{lane}: {expr!r}"
            for expr in re.findall(r'-m\s+"([^"]*)"', recipe)
            if any(_can_select(expr, markers) for markers in bed_sets)
        ]
    assert not offenders, (
        f"these lanes select the conformance tier without asking for the bed, yet can "
        f"reach the `conformance_bed` tests, which open a real lab VM on every run: "
        f"{offenders}. That lane runs in CI with no lab. Add `and not conformance_bed`, "
        f"or set OTTO_CONFORMANCE_BED in the recipe if the lane really does want the bed."
    )


# --- The bed VENUE of the conformance tier (OTTO_CONFORMANCE_BED) -------------
#
# Everything above reasons about the `conformance` MARKER, which decides which
# tests a lane collects. The guards below reason about the venue KNOB, which
# decides what those tests reach when they run, and the two are independent:
# the whole tree is hermetic under `make conformance` and drives real lab
# hardware — Linux VMs, five BusyBox guests over telnet, and Zephyr guests
# whose consoles serve exactly ONE client — under `make conformance-bed`. The
# same test id means both things depending on one environment variable.
#
# So the marker guards cannot see this hazard at all. A default lane that set
# the knob would still deselect every conformance item (G11 holds), and would
# still resolve the BED space at collection: `tests/conformance/conftest.py`
# calls `resolve_space()` at import, which under the knob reads the bed's lab
# data and builds a host per element through otto's factory. That is on the collection
# path of every path-less run in the repo, so a stray knob makes `make
# coverage` depend on lab data it has no business reading, and a CI job that
# set it would fail collection for a reason that says nothing about otto.


def _makefile_simple_variables(text: str) -> "dict[str, str]":
    """Every ``NAME := value`` / ``NAME ?= value`` definition in the Makefile.

    Wider than :func:`_makefile_marker_variables`, which sees only the shared
    ``M_*`` marker expressions, because a lane's ENVIRONMENT is spelled with
    ordinary variables (``CONFORMANCE_CELLS``, ``LEAK_DETECT``) and a scanner
    that could not expand them would read a recipe's env as the literal text
    ``$(CONFORMANCE_CELLS)``.

    Recursive ``=`` assignments are deliberately out: nothing in this file's
    lanes uses one, and expanding a recursive definition correctly means
    implementing make's deferred evaluation. A missed variable surfaces as an
    unexpanded ``$(...)`` that :func:`_makefile_recipe_env` refuses rather than
    passes on.
    """
    return dict(re.findall(r"^([A-Z][A-Z0-9_]*)\s*[:?]=\s*(.*?)\s*$", text, re.MULTILINE))


def _expand_makefile_refs(value: str, variables: "dict[str, str]") -> str:
    """*value* with every ``$(NAME)`` reference to a known variable substituted.

    Iterated, because these definitions nest (``TIMEOUT_CMD`` names
    ``PYTEST_TIMEOUT``), and bounded so a variable that referenced itself
    cannot spin. Unknown references — ``$(call ...)``, ``$(1)``, a function
    call — are left in place ON PURPOSE: this returns text for a caller to
    judge, and a silent drop is how a scanner starts reading a recipe that is
    not there.
    """
    for _ in range(10):
        expanded = re.sub(
            r"\$\(([A-Z][A-Z0-9_]*)\)", lambda m: variables.get(m.group(1), m.group(0)), value
        )
        if expanded == value:
            return expanded
        value = expanded
    return value


def _makefile_recipe_env(recipe: str, variables: "dict[str, str]") -> "dict[str, str]":
    """The ``NAME=value`` environment a recipe prefixes its command with.

    Upper-case names only, which is what separates an environment assignment
    from the option spellings that share its shape: ``--kill-after=10s``,
    ``--junitxml=...`` and ``--randomly-seed=N`` are all lower-case, and a
    pattern that matched them would hand a caller flags to put in ``env``.

    The WHOLE recipe is expanded before the scan, not just the values, so a
    lane that exports through a shared variable is seen exporting it -- the
    way ``LEAK_DETECT`` arms the asyncio leak detector across a dozen lanes
    without any of them naming ``OTTO_DETECT_ASYNCIO_LEAKS``. A reference this file's
    definitions cannot resolve survives into the value it landed in, so a
    caller that needs a real value can see that it did not get one instead of
    exporting ``$(SOMETHING)`` to a child process.
    """
    joined = _expand_makefile_refs(recipe.replace("\\\n", " "), variables)
    return dict(re.findall(r"(?<![-\w.])([A-Z][A-Z0-9_]*)=(\S+)", joined))


def _makefile_targets_setting(variable: str) -> "dict[str, str]":
    """Target name -> recipe, for every Makefile target whose recipe exports *variable*.

    Derived rather than named, for :func:`_makefile_targets_selecting`'s
    reason: the lane may be renamed and the guards should follow it. Reads the
    EXPANDED recipe, so a lane that set the knob through a shared variable —
    the way ``LEAK_DETECT`` arms the leak detector — is seen as setting it.
    """
    variables = _makefile_simple_variables((PROJECT_ROOT / "Makefile").read_text())
    return {
        target: recipe
        for target, recipe in _makefile_recipes().items()
        if variable in _makefile_recipe_env(recipe, variables)
    }


def test_the_bed_venue_of_the_conformance_tier_has_a_lane():
    """G11i: G11h's missing mirror — the one it says belongs with the target.

    G11h forbids a lab-less lane from selecting the `conformance_bed` tests
    and records, in its own docstring, that it ships without the paired "and
    they still have a lane" guard because at the time nothing ran them: that
    guard would have been red on arrival, the same reason G11c waited for
    nightly's job to exist. `make conformance-bed` is the lane, so the mirror
    lands with it.

    TWO HALVES, because the marker and the venue knob are different claims and
    a lane can satisfy either alone. A lane that selects the bed-marked tests
    but never sets `OTTO_CONFORMANCE_BED` runs the openers' witness against
    real hardware while every OTHER contract in the tree stays hermetic — so
    `tests/conformance/_bed.py`, `_lab_context.py` and the whole bed resolver
    would be code no lane exercises. A lane that sets the knob but cannot
    reach the bed-marked tests is the deletion G11b forbids one level up.

    Stated over what the lane can SELECT (the G8d/G11d/G11h reading) rather
    than over the shape of its expression, and over what its recipe EXPORTS
    rather than over its name.
    """
    bed_sets = _marker_sets_of_modules_carrying("conformance_bed")
    assert bed_sets, "no module carries `conformance_bed` (guard misparse?)"

    lanes = _makefile_targets_setting("OTTO_CONFORMANCE_BED")
    assert lanes, (
        "no Makefile lane sets OTTO_CONFORMANCE_BED, so the conformance suite's "
        "BED venue — tests/conformance/_bed.py and everything it resolves from "
        "the bed's own lab data — is code no lane runs. `resolve_space()` "
        "dispatches to it and nothing asks"
    )

    reaching = [
        target
        for target, recipe in lanes.items()
        for expr in re.findall(r'-m\s+"([^"]*)"', recipe)
        if any(_can_select(expr, markers) for markers in bed_sets)
    ]
    assert reaching, (
        f"{sorted(lanes)} set OTTO_CONFORMANCE_BED but none of them can select the "
        f"`conformance_bed` tests {bed_sets} — the bed venue's own lane subtracts "
        f"the tests written to prove its openers work"
    )


def _lines_mentioning(text: str, name: str) -> "list[str]":
    """Non-comment lines of *text* naming *name*, whatever shape they set it in.

    COMMENT LINES ARE DROPPED, for :func:`_makefile_marker_expressions`'
    reason: the venue policy is documented in prose that quotes the real
    spelling, and a scanner reading documentation as configuration fails on
    the wrong thing. Measured -- this caught ``nightly.yml``'s own paragraph
    explaining why the bed venue is absent from CI, which is the opposite of
    a workflow setting it.

    A BARE MENTION AND NOT AN ASSIGNMENT SHAPE, and that is a correction a
    mutation forced. The first version of this matched ``name=`` or ``name:``,
    which reads every shell prefix and every YAML ``env:`` key -- and was
    BLIND to the ordinary Python spelling. Injecting
    ``session.env["OTTO_CONFORMANCE_BED"] = "1"`` into ``noxfile.py``'s
    hostless session left the guard GREEN, because the ``"]`` sits between the
    name and the ``=``. Widening to a mention covers ``environ[...] = ...``,
    ``monkeypatch.setenv(...)``, ``env={...: ...}`` and any spelling nobody
    has thought of yet.

    STATED BOUND, and it is the conservative direction on purpose: a
    non-comment DOCSTRING naming the knob on one of these surfaces is
    reported too. For a variable whose failure mode is a default gate driving
    real lab hardware, a guard that over-reports and is argued with beats one
    that under-reports and is trusted.
    """
    return [line for line in text.splitlines() if not line.strip().startswith("#") and name in line]


def test_only_a_conformance_lane_may_set_the_bed_venue_knob():
    """G11j: the exclusion half — no default gate may start driving the bed.

    The marker guards above cannot make this statement. `not conformance`
    deselects every item in the tree, so a catch-all that ALSO set
    `OTTO_CONFORMANCE_BED` would run nothing from it and stay green — while
    every path-less run in the repo paid for a bed-venue `resolve_space()` at
    collection, because `tests/conformance/conftest.py` resolves the space at
    IMPORT and `testpaths` puts that import on the collection path of `make
    coverage`. Measured under the knob: the bed space reads the bed's lab data
    and builds a host through otto's factory for every element it names.

    THE RULE IS ASYMMETRIC ACROSS SURFACES, and deliberately so. A Makefile
    lane MAY set the knob if it positively selects `-m conformance` — that is
    what `make conformance-bed` is, and G11k is what then keeps CI away from
    it. The other three surfaces may not set it at all: `noxfile.py`'s
    sessions are the five-Python CI matrix, `scripts/stability_campaign.py`
    drives soaks, and a GitHub workflow has no lab to reach in the first
    place. None of them has a reason to want the bed, so `mention it and you
    are wrong` is the honest rule there rather than a shape check.

    All four surfaces, and the ones beyond the build files are here for the
    reason G8d gives for reading `scripts/stability_campaign.py`: a lane this
    guard cannot see is protected only by nobody having written the wrong line
    there yet. Every workflow is read, not the one this test would have named:
    the knob could be set in a step's `env:`, in a job's, in the workflow's,
    or inside a `run:` line.
    """
    knob = "OTTO_CONFORMANCE_BED"

    makefile = (PROJECT_ROOT / "Makefile").read_text()
    variables = _makefile_simple_variables(makefile)
    offenders = [
        f"Makefile: {target}"
        for target, recipe in _makefile_recipes().items()
        if knob in _makefile_recipe_env(recipe, variables)
        and not any(
            "conformance" in {term.strip() for term in expr.split(" and ")}
            for expr in re.findall(r'-m\s+"([^"]*)"', recipe)
        )
    ]

    workflows = sorted((PROJECT_ROOT / ".github" / "workflows").glob("*.yml"))
    assert workflows, "no workflows found under .github/workflows (guard misparse?)"
    for label, path in (
        ("noxfile.py", _NOXFILE),
        ("scripts/stability_campaign.py", _STABILITY_CAMPAIGN),
        *((f".github/workflows/{path.name}", path) for path in workflows),
    ):
        offenders += [
            f"{label}: {line.strip()}" for line in _lines_mentioning(path.read_text(), knob)
        ]

    assert not offenders, (
        f"{knob} reaches a lane that did not ask for the bed: {offenders}. It drives "
        f"real lab hardware over a hop — Linux VMs, five BusyBox guests over telnet, "
        f"and Zephyr consoles that serve exactly one client — and none of these has a "
        f"lab; in CI there is none to have. A Makefile lane may set it only if it "
        f"positively selects `-m conformance` (`make conformance-bed` is that lane); "
        f"noxfile.py, scripts/stability_campaign.py and the workflows may not name it "
        f"outside a comment at all."
    )


def test_no_ci_workflow_invokes_the_bed_venues_lane():
    """G11k: the bed lane is the one lane CI must NOT run, and G11c is why.

    G11c asserts that some workflow job runs "the conformance lane", derived
    from `_makefile_targets_selecting("conformance")` — which since this item
    returns TWO targets, the hermetic one and the bed one. That guard is a
    union over them, so it is satisfied by either, and nightly's
    `conformance-hermetic` job satisfies it today. Nothing in it would object
    if the bed lane were the one CI ran, and CI has no lab: the run would fail
    at the first host it could not reach, or wedge a Zephyr console it could.

    This is the missing half, and it is stated the same derived way — the lane
    is whichever target exports the venue knob, and the search is across every
    workflow rather than inside one this test names, so the guard follows a
    renamed lane or a moved job instead of going red on it.
    """
    lanes = sorted(_makefile_targets_setting("OTTO_CONFORMANCE_BED"))
    assert lanes, "no Makefile lane sets OTTO_CONFORMANCE_BED (G11i covers this)"

    running = sorted({(lane, *pair) for lane in lanes for pair in _workflow_jobs_invoking(lane)})
    assert not running, (
        f"a workflow job runs the conformance suite's BED lane: {running}. CI has no "
        f"lab — no test1, no BusyBox guests, no Zephyr consoles — so every cell would "
        f"fail to open for the one reason that says nothing about otto. The bed venue "
        f"is a dev-VM lane; nightly's hermetic `make conformance` is what CI runs."
    )


def test_the_bed_lane_collects_the_bed_venue():
    """G11l: G11g's reading, taken on the other lane — a real collection.

    Every guard above this one reads build files as TEXT, and text cannot
    answer the question that matters here: whether the lane, run as written,
    reaches the venue it exists for. G11g takes that reading for the hostless
    catch-all and asserts BOTH halves of its property — nothing from the tree
    selected, and the tree nevertheless collected — because "excluded" and
    "invisible" are the same green from one side. The bed lane's halves are
    the mirror image: something from the tree IS selected, and what it
    resolved is the BED space rather than the hermetic one.

    Both are needed. A lane that collected nothing would exit 5 and abort the
    make recipe (G11e records that failure for this tier, measured: `no tests
    collected (7901 deselected)`), so the count half is not decoration. And a
    bed lane that quietly resolved the HERMETIC space would pass every other
    assertion in this file while running `make conformance` under a second
    name — the venue knob is one environment variable, and the failure of
    setting it wrongly is a green run that certified a loopback `sshd`.

    THE LANE IS READ OUT OF THE MAKEFILE, not retyped: the target is whichever
    one exports the venue knob, the child's environment is that recipe's own
    `NAME=value` prefix expanded through the Makefile's variables, and the
    marker expression is the recipe's own. A retyped copy is a test of the
    copy. An unexpandable `$(...)` fails loudly rather than being exported to
    a child as literal text.

    NO COUNT IS PINNED. The bed collection is seed-dependent whenever the lane
    samples (Task 4c measured 51 at `--randomly-seed=1` and 49 at 7 for the
    default budget of 8), and the exhaustive figure moves with the lab's own
    data. What is asserted instead survives both: that every host KIND the
    resolver reports is present in what the lane collected. That is the
    crossing this venue exists for — a contract asserted over a Linux VM, a
    BusyBox guest over telnet and a Zephyr console — and it is the property a
    narrowed budget would silently take away.

    A subprocess and not an in-process `Config`, for G11g's reason: this
    suite's own `addopts` would be inherited. `-n0` and an emptied
    `PYTEST_ADDOPTS` keep the draw line on the process whose output this
    reads.
    """
    lanes = _makefile_targets_setting("OTTO_CONFORMANCE_BED")
    assert lanes, "no Makefile lane sets OTTO_CONFORMANCE_BED (G11i covers this)"
    variables = _makefile_simple_variables((PROJECT_ROOT / "Makefile").read_text())
    for target, recipe in sorted(lanes.items()):
        _assert_lane_collects_the_bed_venue(target, recipe, variables)


def _assert_lane_collects_the_bed_venue(
    target: str, recipe: str, variables: "dict[str, str]"
) -> None:
    """One knob-setting lane's collection, asserted. See G11l for the reasoning.

    Split out of the test so the guard covers EVERY lane that sets the venue
    knob rather than a first-sorted one -- there is one today, and a second
    would otherwise be the lane nobody checked.
    """
    from tests.conformance._bed import bed_space

    lane_env = _makefile_recipe_env(recipe, variables)
    unresolved = {name: value for name, value in lane_env.items() if "$(" in value}
    assert not unresolved, (
        f"`make {target}` sets {unresolved} and this guard could not expand it, so it "
        f"cannot run the lane the Makefile describes — teach "
        f"`_makefile_simple_variables` the definition rather than dropping the value"
    )

    expressions = re.findall(r'-m\s+"([^"]*)"', recipe)
    assert len(expressions) == 1, (
        f"`make {target}` has {len(expressions)} `-m` expressions {expressions}; this "
        f"guard runs the lane's selector and does not know which one to take"
    )

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-m",
            expressions[0],
            "--collect-only",
            "-q",
            "-n0",
            "--no-cov",
            "-p",
            "no:cacheprovider",
        ],
        cwd=str(PROJECT_ROOT),
        env={**os.environ, "PYTEST_ADDOPTS": "", **lane_env},
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    output = result.stdout + result.stderr
    assert result.returncode == 0, (
        f"`make {target}`'s selector failed to collect (rc={result.returncode}). "
        f"An empty selection exits 5, which aborts the make recipe:\n{output[-3000:]}"
    )

    selected = [line for line in output.splitlines() if line.startswith("tests/conformance/")]
    assert selected, (
        f"`make {target}` collected nothing from tests/conformance/, so the bed venue "
        f"has no lane: either the selector reaches other trees only, or the tree left "
        f"`testpaths` and this path-less invocation never saw it (G11e). Where it "
        f"reaches nothing at all pytest exits 5 and the recipe aborts, which the "
        f"return-code assertion above catches first:\n{output[-2000:]}"
    )

    drew = [line for line in output.splitlines() if "conformance: venue=" in line]
    assert len(drew) == 1, (
        f"the conformance tree logged {len(drew)} draw lines under `make {target}`, "
        f"expected exactly 1 — with none, the tree was never collected at all and the "
        f"assertion above passed on something else (see G11e):\n{output[-2000:]}"
    )
    assert "venue=bed" in drew[0], (
        f"`make {target}` resolved its cells in the wrong venue: {drew[0].strip()!r}. "
        f"The knob it exports ({lane_env}) did not reach "
        f"`tests/conformance/_venue.py`, so this lane is `make conformance` under a "
        f"second name — a green run certifying a loopback sshd"
    )

    kinds = sorted({resolved.kind for resolved in bed_space()})
    assert kinds, "bed_space() reports no kinds (guard misparse?)"
    missing = [kind for kind in kinds if not any(f"[{kind}[" in line for line in selected)]
    assert not missing, (
        f"`make {target}` collected no cell of kind {missing} out of {kinds}, so the "
        f"lane does not cross every host family the bed resolver knows about — which "
        f"is the whole claim this venue makes over `tests/integration/host/`. A "
        f"narrowed cell budget is the usual cause; the lane is exhaustive by default "
        f"for exactly this reason."
    )
