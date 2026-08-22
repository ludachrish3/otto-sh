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
import contextlib
import re
import shlex
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
    scan in G8d/G8e is recorded debt; it is not inherited.
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
    are live right now — ``.github/workflows/nightly.yml:183`` and ``:217``.
    Neither reaches this tier today, which is why the tree is compliant, but
    they are the shape this guard cannot judge, not a hypothetical one. It
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
        markerless += [(label, tokens) for tokens in invocations if "-m" not in tokens]
    # Without this the guard is green whenever the scanners break, which is the
    # one failure mode a scanner-based rule really has. Two `-m`-less lanes
    # exist today (both `pytest ... src/otto` doctest runs), so an empty result
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


def _makefile_targets_selecting(marker: str) -> "list[str]":
    """Names of the Makefile targets whose recipe positively selects *marker*.

    Derived from the recipe text rather than hardcoded, so renaming the lane
    moves the guard with it instead of quietly leaving it pinned to a target
    that no longer exists.
    """
    makefile = (PROJECT_ROOT / "Makefile").read_text()
    blocks = re.finditer(r"^([a-zA-Z0-9_.-]+):[^\n]*\n((?:\t[^\n]*\n)+)", makefile, re.MULTILINE)
    return [b.group(1) for b in blocks if re.search(rf'-m\s+"{re.escape(marker)}"', b.group(2))]


def _ci_jobs_invoking(target: str) -> "list[str]":
    """CI job names with a `run` step invoking `make <target>`.

    Word-boundary on both sides, with `(?![-\\w])` closing the right-hand
    side: `make busybox-cache` primes the cache and runs no tests, so a plain
    substring match would report the tier covered by the one target that most
    looks like the lane and least is it.
    """
    ci = yaml.safe_load((PROJECT_ROOT / ".github" / "workflows" / "ci.yml").read_text())
    pattern = rf"\bmake\b[^\n&|;]*\b{re.escape(target)}\b(?![-\w])"
    return [
        name
        for name, job in ci["jobs"].items()
        if any(re.search(pattern, step["run"]) for step in job.get("steps", []) if "run" in step)
    ]


def test_ci_runs_the_busybox_lane():
    """G8c: a lane nothing invokes is the same evaporated coverage as no lane.

    G8b pins that an opt-in Makefile lane EXISTS; it cannot see whether
    anything ever runs it, and for a while nothing did. The tier needs no qemu
    on an x86_64 runner — `ubuntu-latest` executes the artifacts natively — so
    there was never a technical reason for the gap, only the order the work
    landed in. Asserted against the workflow rather than against a job name of
    this test's choosing: the lane is derived from the Makefile and the
    invocation is looked for across every job, so renaming either end moves
    the guard instead of breaking it.

    Also pins the job into `report-failure`'s `needs`. That list is how a
    broken `main` opens an issue instead of waiting for someone to watch the
    Actions tab; a job absent from it fails silently in exactly the case the
    reporting exists for.
    """
    lanes = _makefile_targets_selecting("busybox")
    assert lanes, "no Makefile target positively selects `-m busybox` (G8b covers this)"

    running_jobs = sorted({job for lane in lanes for job in _ci_jobs_invoking(lane)})
    assert running_jobs, (
        f"no CI job invokes any of {lanes} — every default lane excludes "
        f"`-m busybox` (G8), so the whole tier runs nowhere in CI"
    )

    ci = yaml.safe_load((PROJECT_ROOT / ".github" / "workflows" / "ci.yml").read_text())
    reported = ci["jobs"]["report-failure"]["needs"]
    missing = [name for name in running_jobs if name not in reported]
    assert not missing, (
        f"{missing} run the BusyBox lane but are absent from report-failure's "
        f"`needs`, so a failure on main opens no tracking issue"
    )

    # A job can be present, wired into `needs`, and still not matter:
    # `continue-on-error: true` makes it report success whatever it finds. That
    # is the cheapest possible way to neutralise the tier while every other
    # assertion here stays green, so the guard has to name it.
    advisory = [n for n in running_jobs if ci["jobs"][n].get("continue-on-error") is True]
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
