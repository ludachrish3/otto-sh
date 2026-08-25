"""The support matrix's two axes, DERIVED FROM THE TREE rather than tabulated.

``schemas/support_matrix.json`` is a ``{surface} x {profile}`` grid (spec
2026-08-22 §5). Both axes are answers this repo already holds, so this module
reads them rather than restating them:

``profiles``
    ``axes_for(element).userland`` over every element the bed labs declare.
    Nine today. A hand-written list of nine strings would be a second copy of
    :func:`tests._fixtures.profiles._userland`'s rule, and the copy that goes
    stale is the one nothing runs -- a typo'd profile name is a matrix column
    no observation can ever match, which reads as "never measured" rather than
    as a bug.

``surfaces``
    The conformance CONTRACTS -- the test functions under
    ``tests/conformance/`` that take the ``resolved_cell`` fixture AND are not
    positive controls. Six today.
    ``test_bed_opener_witness.py``'s two tests name their own cell instead and
    so are not contracts; that is the same distinction
    ``tests/conformance/conftest.py``'s ``_cell_under_test`` makes when it
    answers ``None`` for a callspec without a ``resolved_cell`` param. The
    POSITIVE CONTROLS are the other exclusion and the subtler one: they take
    ``resolved_cell`` too, because a control has to run on the cell it vouches
    for, so only the ``@pytest.mark.positive_control`` marker separates them
    (``tests/conformance/_controls.py``). Six of those today as well, one per
    surface.

**THE SURFACE TABLE IS KEYED BY NODEID AND IS CHECKED BOTH WAYS.**
:data:`SURFACES` maps each contract's nodeid to the matrix row's id and title.
That mapping cannot be derived -- a slug and a human title are labels, not
facts -- so it is written down. What is *not* left to trust is its agreement
with the tree: :func:`discover_contracts` walks the tree and
``tests/unit/test_support_matrix.py`` asserts the two sets are EQUAL. A
contract added, renamed or deleted therefore fails loudly instead of silently
losing or gaining a row. The failure mode this avoids is the one
``tests/conformance/_vocabulary.py`` records for its own table: *a missing
entry looks like a passing cell*.

**DISCOVERY READS SIGNATURES, and that is a real limit.** MEASURED
(2026-08-24, ``pytest tests/conformance --collect-only``): fourteen test
functions collect, twelve of which name ``resolved_cell`` as a parameter --
six contracts and six positive controls -- and NONE of the three contract
modules contains the string ``usefixtures``. A contract that requested the
cell through ``@pytest.mark.usefixtures`` instead would be invisible here, so
``tests/unit/test_support_matrix.py`` refuses that spelling outright rather
than leaving the blind spot open.

REGENERATING THE ARTIFACT'S AXES, when a contract or a lab element lands::

    PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -c "
    from tests._fixtures.support_matrix import rewrite_matrix_axes
    print(rewrite_matrix_axes())"

That rewrite adds and removes CELLS; it never touches a verdict an existing
cell carries (spec §5: only collation writes ``measured-*``, and a run that
did not draw a cell says nothing about it).
"""

import ast
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from tests._fixtures.paths import PROJECT_ROOT
from tests._fixtures.profiles import axes_for, axis_space
from tests.conformance._bed import BED_LABS, bed_space
from tests.conformance._controls import (
    control_surface_of,
    positive_control_for,
    walk_test_functions,
)
from tests.conformance._sample import cell_label

SCHEMA_PATH = PROJECT_ROOT / "schemas" / "support-matrix.schema.json"
"""The contract ``schemas/support_matrix.json`` validates against."""

MATRIX_PATH = PROJECT_ROOT / "schemas" / "support_matrix.json"
"""The committed artifact.

Under ``schemas/`` because spec §4 and §5 both name that path. That directory
is otherwise the git-ignored output of ``make schema``, so ``.gitignore``
re-includes these two files by name -- and it had to switch from ``/schemas/``
to ``/schemas/*`` to do it, because git never descends into an excluded
DIRECTORY and a re-include under one is inert (measured, in a throwaway repo,
both spellings).
"""

CONFORMANCE_ROOT = PROJECT_ROOT / "tests" / "conformance"

FORMAT = 1
"""The artifact's document version, spelled the way ``monitor-export`` spells
its own: required, no default, so an unversioned file fails loud rather than
validating as an empty modern one."""

#: The fixture a test takes to declare itself a CONTRACT over a resolved cell.
CELL_FIXTURE = "resolved_cell"


@dataclass(frozen=True)
class Surface:
    """One conformance contract, as a matrix row."""

    id: str
    title: str
    contract: str
    """The contract's pytest nodeid, without its ``[cell]`` parametrization."""


#: nodeid -> (row id, human title). Order is the rendered row order, and it is
#: a real declaration rather than a sort: the three exec contracts, then the
#: two transfer ones, then timeout -- the order the contract modules and the
#: spec's §4 list both use.
SURFACES: "tuple[Surface, ...]" = (
    Surface(
        id="exec-exit-code",
        title="exec: reports the documented exit code",
        contract=(
            "tests/conformance/test_exec_contract.py::test_exec_reports_the_documented_exit_code"
        ),
    ),
    Surface(
        id="exec-framing",
        title="exec: frames output without prompt noise",
        contract=(
            "tests/conformance/test_exec_contract.py::test_exec_frames_output_without_prompt_noise"
        ),
    ),
    Surface(
        id="exec-failure-in-sequence",
        title="exec: a failing command is not reported as success",
        contract=(
            "tests/conformance/test_exec_contract.py"
            "::test_a_failing_command_is_not_reported_as_success"
        ),
    ),
    Surface(
        id="transfer-roundtrip",
        title="transfer: put/get roundtrip preserves content",
        contract=(
            "tests/conformance/test_transfer_contract.py::test_put_get_roundtrip_preserves_content"
        ),
    ),
    Surface(
        id="transfer-mode",
        title="transfer: put lands the documented mode on the host",
        contract=(
            "tests/conformance/test_transfer_contract.py"
            "::test_put_lands_the_documented_mode_on_the_host"
        ),
    ),
    Surface(
        id="timeout",
        title="timeout: a command over budget fails the documented way",
        contract=(
            "tests/conformance/test_timeout_contract.py"
            "::test_a_command_exceeding_its_budget_fails_the_documented_way"
        ),
    ),
)


def _takes_the_cell(node: "ast.FunctionDef | ast.AsyncFunctionDef") -> bool:
    """Whether *node* names :data:`CELL_FIXTURE` among its parameters."""
    args = node.args
    named = [*args.posonlyargs, *args.args, *args.kwonlyargs]
    return any(arg.arg == CELL_FIXTURE for arg in named)


def _contracts_in(path: Path) -> "list[str]":
    """Every contract nodeid *path* declares, in source order.

    Walks classes too (through :func:`~tests.conformance._controls.walk_test_functions`),
    so a future ``class TestX`` module is not silently dropped; a dropped
    module would remove matrix rows rather than fail.

    **A POSITIVE CONTROL IS NOT A CONTRACT**, and the exclusion below is what
    keeps it out of the matrix's rows. Both take :data:`CELL_FIXTURE` -- a
    control has to run on the very cell it vouches for -- so the parameter
    alone cannot separate them and every control would otherwise arrive here
    as a seventh, eighth, ... surface. The marker is the declaration; see
    ``tests/conformance/_controls.py``.
    """
    return [
        nodeid
        for nodeid, node in walk_test_functions(path)
        if _takes_the_cell(node) and control_surface_of(node) is None
    ]


def discover_contracts() -> "list[str]":
    """Every conformance contract the tree declares now, in file then source order.

    Sorted by FILE (so the set is stable against a directory listing's order)
    but never within a file, where source order is the module's own statement.
    """
    return [
        nodeid
        for path in sorted(CONFORMANCE_ROOT.glob("test_*.py"))
        for nodeid in _contracts_in(path)
    ]


def _natural_key(name: str) -> "tuple[object, ...]":
    """Sort key that orders ``busybox-1.9`` before ``busybox-1.16``.

    Digit runs compare as integers, everything else as text. Deliberately
    grammar-free: it does NOT parse ``gnu`` / ``busybox-<v>`` / ``zephyr-<v>``,
    so it cannot drift from :func:`tests._fixtures.profiles._userland`, which
    is the one place that grammar is decided.
    """
    return tuple(
        int(part) if part.isdigit() else part for part in re.split(r"(\d+)", name) if part != ""
    )


@dataclass(frozen=True)
class Profile:
    """One userland, as a matrix column, with the elements it stands for."""

    id: str
    elements: "list[str]"


def discover_profiles() -> "list[Profile]":
    """Every ``userland`` the bed labs' elements resolve to, naturally sorted.

    SORTED, unlike the ``(term, transfer)`` menus
    :func:`tests._fixtures.profiles.axis_space` deliberately leaves in the
    order the host reported them. A menu's order is otto's answer; this is a
    SET aggregated over sixteen elements across three labs, and a set has no
    order of its own -- so the choice is between the lab file's incidental
    order and a stable one, and only the stable one keeps the committed
    artifact's diffs about verdicts. The elements inside each profile keep the
    order the labs listed them in.
    """
    elements: "list[str]" = []
    for lab in BED_LABS:
        for cell in axis_space(lab):
            if cell.element not in elements:
                elements.append(cell.element)
    by_userland: "dict[str, list[str]]" = {}
    for element in elements:
        by_userland.setdefault(axes_for(element).userland, []).append(element)
    return [
        Profile(id=userland, elements=by_userland[userland])
        for userland in sorted(by_userland, key=_natural_key)
    ]


UNTESTED: "dict[str, str]" = {"status": "untested"}
"""The default cell. Spec §5: *untested, not unsupported.*"""


def build_matrix(existing: "dict | None" = None) -> dict:
    """The artifact for the tree as it stands, keeping *existing* verdicts.

    Cells the tree still declares keep whatever verdict they carry; cells it
    no longer declares are dropped and new ones start :data:`UNTESTED`. This
    function NEVER writes a ``measured-*`` verdict of its own -- spec §5
    reserves that for collation.
    """
    old = (existing or {}).get("cells", {})
    surfaces = SURFACES
    profiles = discover_profiles()
    return {
        "$schema": "./support-matrix.schema.json",
        "format": FORMAT,
        "surfaces": [{"id": s.id, "title": s.title, "contract": s.contract} for s in surfaces],
        "profiles": [{"id": p.id, "elements": list(p.elements)} for p in profiles],
        "cells": {
            s.id: {p.id: dict(old.get(s.id, {}).get(p.id, UNTESTED)) for p in profiles}
            for s in surfaces
        },
    }


def rewrite_matrix_axes() -> str:
    """Re-derive :data:`MATRIX_PATH`'s axes in place; report what moved."""
    existing = json.loads(MATRIX_PATH.read_text()) if MATRIX_PATH.exists() else None
    rebuilt = build_matrix(existing)
    MATRIX_PATH.write_text(json.dumps(rebuilt, indent=2) + "\n")
    was = {(s, p) for s, row in (existing or {}).get("cells", {}).items() for p in row}
    now = {(s, p) for s, row in rebuilt["cells"].items() for p in row}
    return f"{MATRIX_PATH}: {len(now)} cells (+{len(now - was)} added, -{len(was - now)} removed)"


@dataclass(frozen=True)
class CellCoverage:
    """How much of a profile one cell's verdict actually rests on.

    THE RENDERER'S PRIMITIVE, and it exists because the page is a LOOKUP
    REFERENCE -- "can otto do X against a device like mine?" -- not internal
    bookkeeping. A reader who takes ``zephyr-3.7 x transfer = measured-ok`` as
    covering all four 3.7 elements and then deploys to a no-filesystem board
    has been misled by the artifact meant to inform them. So the sentence a
    page puts next to a verdict is DERIVED from these four lists (the phrasing
    lives in the renderer; the artifact stores no prose, because a hand-written
    sentence beside a machine-written verdict is the fabrication path spec §5's
    guards exist to close).

    ``unaccounted`` is the list nobody should ignore: elements of the profile
    that the cell neither observed nor declared un-observable. Today it is
    always the whole profile, because every cell is ``untested``. It becomes
    meaningful the moment collation lands, and it is deliberately NOT folded
    into ``not_observable`` -- "we did not look here" and "this environment
    cannot express the observable" are different claims, and collapsing them is
    the error §5 names in its other direction.
    """

    elements: "list[str]"
    observed_on: "list[str]"
    not_observable: "list[str]"
    unaccounted: "list[str]"


def cell_coverage(matrix: dict, surface_id: str, profile_id: str) -> CellCoverage:
    """Break *matrix*'s ``(surface_id, profile_id)`` cell down by element."""
    elements = next(p["elements"] for p in matrix["profiles"] if p["id"] == profile_id)
    cell = matrix["cells"][surface_id][profile_id]
    observed = list(cell.get("observed_on", []))
    unobservable = [entry["element"] for entry in cell.get("not_observable", [])]
    named = set(observed) | set(unobservable)
    return CellCoverage(
        elements=list(elements),
        observed_on=observed,
        not_observable=unobservable,
        unaccounted=[e for e in elements if e not in named],
    )


def element_accounting_errors(matrix: dict) -> "list[str]":
    """Every way a cell's per-element evidence contradicts its own profile.

    NOT expressible in the JSON Schema: the check is a cross-reference between
    a cell and the ``profiles`` entry it sits under, and a schema cannot follow
    that pointer. What the schema CAN do -- refuse a state that omits the
    breakdown entirely -- it does; this is the other half.

    Does NOT flag :attr:`CellCoverage.unaccounted`. A run measures the cells it
    DREW, so a ``measured-ok`` cell may legitimately rest on a subset while the
    rest of the profile is simply not yet looked at; failing on that would
    force collation to file "not drawn" under ``not_observable`` and destroy
    the distinction the matrix exists to keep. Naming an element the profile
    does not hold, or filing one under both verdicts, is a different thing
    entirely: it makes a rendered sentence FALSE.
    """
    errors: "list[str]" = []
    for surface_id, row in matrix["cells"].items():
        for profile_id in row:
            coverage = cell_coverage(matrix, surface_id, profile_id)
            held = set(coverage.elements)
            where = f"cells.{surface_id}.{profile_id}"
            for element in coverage.observed_on:
                if element not in held:
                    errors.append(f"{where}: observed_on names {element!r}, not in this profile")
            for element in coverage.not_observable:
                if element not in held:
                    errors.append(f"{where}: not_observable names {element!r}, not in this profile")
            both = sorted(set(coverage.observed_on) & set(coverage.not_observable))
            if both:
                errors.append(f"{where}: {both} are filed as BOTH observed and not-observable")
            duplicates = sorted(
                {e for e in coverage.not_observable if coverage.not_observable.count(e) > 1}
            )
            if duplicates:
                errors.append(f"{where}: not_observable lists {duplicates} more than once")
    return errors


def cell_outcome_errors(matrix: dict) -> "list[str]":
    """Every way a cell's per-CELL breakdown contradicts the rest of the cell.

    ``observed_cells`` is what stops a scalar status reading as uniform over a
    space that is not: the matrix's axis is the PROFILE, but the bed measures
    an (element, term, transfer) cell, and MEASURED 2026-08-24 ``bb1161``
    answers two different things -- the roundtrip passes over ``shell`` and
    fails over ``nc``. The schema already ties the entries' outcomes to the
    status in both directions (every entry passed, or at least one did not).
    These are the three checks it cannot make, each a cross-reference:

    1. **An entry names an element this cell did not observe.** The rendered
       sentence would then attribute a transport result to a device whose
       verdict does not rest on it.
    2. **An observed element has NO entry.** This is the one that matters most
       and the one a schema can never see: a breakdown that silently omits a
       device is exactly the uniform reading this field exists to prevent,
       wearing the field's own clothes. ``observed_on`` and ``observed_cells``
       must name the same element set.
    3. **A ``cell_label`` names a cell the bed venue does not draw**, or names
       axes that disagree with its own spelling. Resolved against
       :func:`~tests.conformance._bed.bed_space` the same way
       :func:`acceptable_controls` resolves a control nodeid, because a
       plausible-looking label is precisely what this artifact must not accept
       on trust -- the item's own fixture once cited a positive control that
       could never resolve, and it satisfied every shape check in the file.

    NONE OF THESE CAN FIRE FROM TODAY'S COLLATOR, and that is said out loud
    rather than left to be discovered: it builds the entries by walking
    ``observed_on`` and reads every field off the record, so 1 and 2 are
    structurally impossible there and 3 would need a record whose label the bed
    did not produce. They are guards on the ARTIFACT -- against a hand-edit,
    and against a future collator that starts constructing what it should be
    reading. That is the same standing this file's other accounting checks
    have.
    """
    errors: "list[str]" = []
    labels = _labels_by_element()
    for surface_id, row in matrix["cells"].items():
        for profile_id, cell in row.items():
            where = f"cells.{surface_id}.{profile_id}"
            entries = cell.get("observed_cells", [])
            observed = set(cell.get("observed_on", []))
            for entry in entries:
                element, label = entry["element"], entry["cell_label"]
                axes = f"[{element}:{entry['term']}:{entry['transfer']}]"
                if element not in observed:
                    errors.append(
                        f"{where}: observed_cells names {label!r} on {element!r}, "
                        f"which is not in observed_on"
                    )
                if label not in labels.get(element, []):
                    errors.append(
                        f"{where}: observed_cells names {label!r}, which is not a cell the "
                        f"bed venue draws for {element!r}"
                    )
                elif not label.endswith(axes):
                    errors.append(
                        f"{where}: observed_cells entry {label!r} disagrees with the axes "
                        f"beside it {axes}"
                    )
            duplicates = sorted(
                {
                    entry["cell_label"]
                    for entry in entries
                    if [e["cell_label"] for e in entries].count(entry["cell_label"]) > 1
                }
            )
            if duplicates:
                errors.append(f"{where}: observed_cells lists {duplicates} more than once")
            missing = sorted(observed - {entry["element"] for entry in entries})
            if missing:
                errors.append(
                    f"{where}: observed_on names {missing}, which observed_cells does not "
                    f"break down -- the per-transport evidence would hide those devices"
                )
    return errors


# --------------------------------------------------------------------------
# The positive-control guard: §5's central requirement, made checkable
# --------------------------------------------------------------------------


@lru_cache(maxsize=1)
def _labels_by_element() -> "dict[str, list[str]]":
    """element -> the ``cell_label`` of every BED cell that element appears in.

    Memoised because :func:`~tests.conformance._bed.bed_space` builds hosts
    through otto's factory to resolve each cell (measured 0.06s cold, 0.011s
    warm for the 49 bed cells; nothing is opened and nothing is fetched).

    BED cells and no others, per the ruling of 2026-08-24: the profile axis is
    built from the bed labs' elements, so the hermetic venue populates NO
    matrix cell and its labels can never be a cell's evidence.
    """
    labels: "dict[str, list[str]]" = {}
    for resolved in bed_space():
        labels.setdefault(resolved.cell.element, []).append(cell_label(resolved))
    return labels


def acceptable_controls(surface_id: str, observed_on: "list[str]") -> "set[str]":
    """The nodeids a cell may cite as its positive control, DERIVED not parsed.

    A cell's control must satisfy two things at once, and both are answered by
    constructing the acceptable set rather than by pulling a nodeid apart:

    1. **It is THIS surface's control.**
       :func:`~tests.conformance._controls.positive_control_for` is the only
       source, so a cell citing the framing control for the timeout row is not
       in the set.
    2. **It ran on an element the verdict actually rests on.** §5's whole
       point is that a control passing on ``gnu`` says nothing about
       ``busybox-1.16.1``; a control run on an element this cell did not
       observe says only slightly more. So the set is built over
       *observed_on*, not over the profile -- a cell whose verdict rests on
       ``zephyr37_fat`` may not cite evidence gathered on ``zephyr37_lfs``.

    PARAMETRIZED, ALWAYS. The unparametrized nodeid names a control that ran
    somewhere, which is exactly the claim the matrix must not accept: the
    evidence is per cell. (This is why the schema's ``NodeId`` pattern had to
    learn to match a nested-bracket parametrization -- see that ``$def``.)

    Built by joining a control's nodeid to ``cell_label``, the SAME function
    ``tests/conformance/conftest.py`` passes as ``ids=`` -- so this cannot
    drift from the ids a run really produces, and
    ``tests/unit/test_support_matrix.py`` additionally resolves every accepted
    nodeid against a real ``--collect-only``.
    """
    control = positive_control_for(surface_id)
    labels = _labels_by_element()
    return {f"{control}[{label}]" for element in observed_on for label in labels.get(element, [])}


def positive_control_errors(matrix: dict, collected: "set[str] | None" = None) -> "list[str]":
    """Every way a cell's ``positive_control`` fails to be evidence for THAT cell.

    Spec §5: "A ``measured-ok`` cell whose positive control is missing is a
    defect in the same class the matrix exists to expose, so the guard rejects
    it rather than rendering it." The schema already refuses a ``measured-ok``
    cell that omits the field, and refuses a value that is not nodeid-SHAPED.
    Neither can do what this does, because both are local checks and this is a
    cross-reference: a nodeid that merely LOOKS well-formed is precisely the
    failure this item exists to prevent.

    *collected* is the set of nodeids a REAL collection produced. Passing it
    is what turns "this string is the right shape and names the right surface"
    into "this test exists and is really collected on that cell"; ``None``
    skips only that last check, for callers that have no collection in hand.

    ★ AND THE SAME QUESTION IS ASKED OF EVERY PER-ROUTE CITATION, one level
    down. ``observed_cells[].positive_control`` backs a claim about ONE drawn
    cell -- *"you can do this over ``shell``"* -- so the acceptable value is a
    single string, not a set: this surface's control parametrized on THAT VERY
    ``cell_label``. That is strictly stronger than the cell-level rule above,
    which accepts a control run on any cell of an observed ELEMENT, and it has
    to be: ``test1`` alone draws eight cells over four transfer backends, and
    otto's ``nc`` gap is precisely a per-cell defect, so "the control passed
    somewhere on this device" is the weakening the split exists to retire.

    The schema already requires the field on a ``passed`` entry and refuses one
    that is not nodeid-shaped. Neither of those is this: both are local checks,
    and a plausible-looking string on the wrong cell is exactly what this
    artifact must not accept on trust -- the item's own Task 1 fixture once
    cited a control in a module that did not exist and satisfied every shape
    check in the file.
    """
    errors: "list[str]" = []
    for surface_id, row in matrix["cells"].items():
        for profile_id, cell in row.items():
            named = cell.get("positive_control")
            where = f"cells.{surface_id}.{profile_id}"
            errors += _route_control_errors(surface_id, cell, where, collected)
            if named is None:
                continue
            allowed = acceptable_controls(surface_id, list(cell.get("observed_on", [])))
            if named not in allowed:
                errors.append(
                    f"{where}: positive_control {named!r} is not this surface's control "
                    f"run on an element this cell observed -- one of {sorted(allowed)}"
                )
            elif collected is not None and named not in collected:
                errors.append(
                    f"{where}: positive_control {named!r} is well formed and names the "
                    f"right control, but NO collected test has that id"
                )
    return errors


def _route_control_errors(
    surface_id: str, cell: dict, where: str, collected: "set[str] | None"
) -> "list[str]":
    """Every way an ``observed_cells`` entry's own control fails to back ITS route.

    Two checks, both cross-references the schema cannot follow, and both
    DERIVED rather than parsed: the only acceptable citation for an entry is
    ``positive_control_for(surface)`` joined to that entry's own
    ``cell_label``, and it must resolve against a real collection.
    """
    errors: "list[str]" = []
    control = positive_control_for(surface_id)
    for entry in cell.get("observed_cells", []):
        named = entry.get("positive_control")
        if named is None:
            continue
        at = f"{where}.observed_cells[{entry['cell_label']}]"
        expected = f"{control}[{entry['cell_label']}]"
        if named != expected:
            errors.append(
                f"{at}: positive_control {named!r} does not back THIS route -- the only "
                f"citation that does is {expected!r}"
            )
        elif collected is not None and named not in collected:
            errors.append(
                f"{at}: positive_control {named!r} names the right control on the right "
                f"cell, but NO collected test has that id"
            )
    return errors


#: How a nodeid census is taken: one line per collected item, filtered to the
#: ones that are nodeids. `-q --collect-only` is pytest's own answer to "which
#: tests are there", and the alternative -- an injected plugin writing a JSON
#: census -- buys nothing here because nothing about the RUN is in question,
#: only the ids.
_COLLECT_ARGS = (
    "--collect-only",
    "-q",
    "--no-cov",
    "-p",
    "no:randomly",
    "-n0",
    "-p",
    "no:cacheprovider",
)


def collect_conformance_nodeids(*, bed: bool) -> "set[str]":
    """Every nodeid ``tests/conformance`` really collects, in the named venue.

    A SUBPROCESS, and not ``pytest.main`` in this process, for the reason
    ``tests/unit/test_pytest_addopts.py`` records about in-process configs:
    this repo's ``addopts`` (``--cov``, ``-n auto``, ``--doctest-modules``)
    ride any Config built here, and the venue is selected by an environment
    variable that ``tests/conftest.py`` has already stripped from THIS
    process. A child gets a clean answer to a clean question.

    Collects rather than runs: MEASURED 2026-08-24, the bed venue's whole
    565-item collection takes 0.20s (1.06s wall including interpreter start)
    and opens nothing -- ``bed_space`` builds hosts through otto's factory to
    read their axes, and never connects.
    """
    env = {
        **os.environ,
        "PYTHONDONTWRITEBYTECODE": "1",
        "OTTO_CONFORMANCE_CELLS": "all",
    }
    if bed:
        env["OTTO_CONFORMANCE_BED"] = "1"
    else:
        env.pop("OTTO_CONFORMANCE_BED", None)
    result = subprocess.run(
        [sys.executable, "-m", "pytest", str(CONFORMANCE_ROOT), *_COLLECT_ARGS],
        env=env,
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    nodeids = {
        line.strip()
        for line in result.stdout.splitlines()
        if line.startswith("tests/") and "::" in line
    }
    if not nodeids:
        raise RuntimeError(
            f"collecting tests/conformance (bed={bed}) produced NO nodeids, so any guard "
            f"resolving against it would pass or fail for the wrong reason:\n"
            f"{result.stdout}\n{result.stderr}"
        )
    return nodeids
