"""The kgcov compatibility matrix's two axes, derived from the tree where they can be.

``schemas/kgcov_matrix.json`` is a ``{surface} x {profile}`` grid, a sibling
of ``schemas/support_matrix.json`` with the same three rules and none of its
code (spec 2026-09-18, kgcov compatibility matrix, §3): only a run writes a
verdict, every verdict carries its provenance, a lost verdict stops the
release.

``surfaces``
    The kgcov contracts: every test under ``tests/e2e/cov/test_kgcov_*.py``
    that takes the ``built_with``, ``coverage_run`` or ``kernel_build``
    fixture. The id and title of a row are labels and are written down in
    :data:`SURFACES`; :func:`discover_contracts` walks the tree and
    ``tests/unit/test_kgcov_matrix.py`` asserts the two sets are equal both
    ways, so a contract added, renamed or deleted fails loudly instead of
    silently losing or gaining a row.

``profiles``
    The compilers and kernels the lane measures: the Makefile's default
    KGCOV_TOOLCHAINS (read, not copied — bed_profile_ids), one column each,
    in the bed venue; and its default KGCOV_KERNELS (build_profile_ids), one
    column each, in the build venue — x86_64-cross first, the 6.8 source
    tree built on the host, then each provisioned kernel built in an image
    of its own era. A column id is the NAME the lane selects by, so the
    ``clang`` column keeps its id when the VM's clang changes; the measured
    full version is provenance on the cell.

A row and a column meet only in the same venue, so the grid holds bed rows
by bed columns and build rows by the build columns, and nothing else.

REGENERATING THE AXES, when a contract or the Makefile default changes::

    PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -c "
    from tests._fixtures.kgcov_matrix import rewrite_matrix_axes
    print(rewrite_matrix_axes())"

That rewrite adds and removes CELLS; it never touches a verdict an existing
cell carries.
"""

import ast
import json
import re
from dataclasses import dataclass
from pathlib import Path

from jsonschema import Draft202012Validator

from tests._fixtures.paths import PROJECT_ROOT

SCHEMA_PATH = PROJECT_ROOT / "schemas" / "kgcov-matrix.schema.json"
MATRIX_PATH = PROJECT_ROOT / "schemas" / "kgcov_matrix.json"
PAGE_PATH = PROJECT_ROOT / "docs" / "cli" / "cov" / "instrumenting" / "kgcov-matrix.md"
MAKEFILE_PATH = PROJECT_ROOT / "Makefile"
KGCOV_TESTS = sorted((PROJECT_ROOT / "tests" / "e2e" / "cov").glob("test_kgcov_*.py"))

FORMAT = 1
BED = "bed"
BUILD = "build"
CROSS_PROFILE = "x86_64-cross"
MEASURED_OK = "measured-ok"
MEASURED_BROKEN = "measured-broken"
UNTESTED_STATUS = "untested"
STATUSES = (MEASURED_OK, MEASURED_BROKEN, UNTESTED_STATUS)

#: The fixtures a test takes to be a contract, and the venue each one measures in.
CONTRACT_FIXTURES = {"built_with": BED, "coverage_run": BED, "kernel_build": BUILD}

_BED_MODULE = "tests/e2e/cov/test_kgcov_toolchains_e2e.py"
_BUILD_MODULE = "tests/e2e/cov/test_kgcov_kernel_builds.py"


@dataclass(frozen=True)
class Surface:
    """One kgcov contract, as a matrix row."""

    id: str
    title: str
    venue: str
    contract: str
    """The test's nodeid without its ``[compiler]`` parametrization."""
    control: bool = False
    """Whether this row is the column's positive control (one bed row is)."""


SURFACES: "tuple[Surface, ...]" = (
    Surface(
        "build-names-compiler",
        "build: both modules name the requested compiler",
        BED,
        f"{_BED_MODULE}::TestBuild::test_both_modules_name_the_requested_compiler",
    ),
    Surface(
        "build-init-array-bracket",
        "build: the .init_array is bracketed by the sentinels",
        BED,
        f"{_BED_MODULE}::TestBuild::test_the_init_array_is_bracketed_by_the_sentinels",
    ),
    Surface(
        "refuses-other-compiler",
        "control: a demo from another compiler is refused at load",
        BED,
        f"{_BED_MODULE}::TestBuild::test_a_demo_from_another_compiler_is_refused_at_load",
        control=True,
    ),
    Surface(
        "fetch-only-the-demo",
        "coverage: only the demo is fetched from the two hosts",
        BED,
        f"{_BED_MODULE}::TestCoverage::test_only_the_demo_is_fetched_from_the_two_hosts",
    ),
    Surface(
        "three-gcda-per-host",
        "coverage: three .gcda per host",
        BED,
        f"{_BED_MODULE}::TestCoverage::test_three_gcda_per_host",
    ),
    Surface(
        "library-loaded-on-demand",
        "coverage: the run log says the library was loaded for the demo",
        BED,
        f"{_BED_MODULE}::TestCoverage::test_the_run_log_says_the_library_was_loaded_for_the_demo",
    ),
    Surface(
        "store-three-demo-files",
        "coverage: the store has the three demo files",
        BED,
        f"{_BED_MODULE}::TestCoverage::test_store_has_the_three_demo_files",
    ),
    Surface(
        "policy-hits",
        "coverage: policy paths have the expected hits",
        BED,
        f"{_BED_MODULE}::TestCoverage::test_policy_paths_have_the_expected_hits",
    ),
    Surface(
        "parse-hits",
        "coverage: parse hits",
        BED,
        f"{_BED_MODULE}::TestCoverage::test_parse_hits",
    ),
    Surface(
        "exit-routine-once-per-host",
        "coverage: exit-routine lines are hit once per host",
        BED,
        f"{_BED_MODULE}::TestCoverage::test_exit_routine_lines_are_hit_once_per_host",
    ),
    Surface(
        "policy-branches",
        "coverage: branches are recorded for the policy switch",
        BED,
        f"{_BED_MODULE}::TestCoverage::test_branches_are_recorded_for_the_policy_switch",
    ),
    Surface(
        "mid-dump-no-double-count",
        "coverage: the mid-suite dump does not double count",
        BED,
        f"{_BED_MODULE}::TestCoverage::test_the_mid_suite_dump_does_not_double_count",
    ),
    Surface(
        "build-release",
        "build: both modules carry the tree's release",
        BUILD,
        f"{_BUILD_MODULE}::test_both_modules_carry_the_trees_release",
    ),
    Surface(
        "build-target-isa",
        "build: both modules are objects for the column's ISA",
        BUILD,
        f"{_BUILD_MODULE}::test_both_modules_are_objects_for_the_columns_isa",
    ),
    Surface(
        "build-compiler",
        "build: the column's compiler built them",
        BUILD,
        f"{_BUILD_MODULE}::test_the_columns_compiler_built_them",
    ),
    Surface(
        "build-instrumented-bracketed",
        "build: the demo is instrumented and bracketed",
        BUILD,
        f"{_BUILD_MODULE}::test_the_demo_is_instrumented_and_bracketed",
    ),
    Surface(
        "build-fixture-untouched",
        "build: the in-place fixture build was not touched",
        BUILD,
        f"{_BUILD_MODULE}::test_the_in_place_fixture_build_was_not_touched",
    ),
    Surface(
        "build-linked-against-library",
        "build: the demo linked against the library",
        BUILD,
        f"{_BUILD_MODULE}::test_the_demo_linked_against_the_library",
    ),
    Surface(
        "build-library-warning-free",
        "build: the library compiled without a warning of its own",
        BUILD,
        f"{_BUILD_MODULE}::test_the_library_compiled_without_a_warning_of_its_own",
    ),
    Surface(
        "build-clang-backend-compiles",
        "build: the clang backend's unit compiles with the column's gcc",
        BUILD,
        f"{_BUILD_MODULE}::test_the_clang_backends_unit_compiles_with_the_columns_gcc",
    ),
    Surface(
        "build-ctors-convention-bracketed",
        "build: a .ctors-convention demo is bracketed too",
        BUILD,
        f"{_BUILD_MODULE}::test_a_ctors_convention_demo_is_bracketed_too",
    ),
)

CONTROL_SURFACE = next(s for s in SURFACES if s.control)

RENAMED_ROWS = {
    "cross-release": "build-release",
    "cross-x86_64-objects": "build-target-isa",
    "cross-compiler-built": "build-compiler",
    "cross-instrumented-bracketed": "build-instrumented-bracketed",
    "cross-fixture-untouched": "build-fixture-untouched",
    "cross-linked-against-library": "build-linked-against-library",
}
"""Row ids the build venue used while it had one column, mapped to their ids now.

Applied by :func:`build_matrix` to whatever artifact it is given, so the
``x86_64-cross`` verdicts and their provenance travelled to the new ids
with nothing lost, even transiently; on the rewritten artifact it is a
no-op, which is what keeps ``build_matrix(committed) == committed``.
"""


def surface_for(contract: str) -> "Surface | None":
    """The row *contract* is, or None when the table does not name it."""
    return next((s for s in SURFACES if s.contract == contract), None)


@dataclass(frozen=True)
class Profile:
    """One compiler, as a matrix column."""

    id: str
    title: str
    venue: str


def _makefile_default(variable: str) -> "list[str]":
    text = MAKEFILE_PATH.read_text(encoding="utf-8")
    m = re.search(rf"^{variable} \?= (.+)$", text, re.MULTILINE)
    if not m:
        raise RuntimeError(f"{MAKEFILE_PATH} declares no `{variable} ?= …` line")
    return [name.strip() for name in m.group(1).split(",") if name.strip()]


def bed_profile_ids() -> "list[str]":
    """The Makefile's default ``KGCOV_TOOLCHAINS``, read off the file."""
    return _makefile_default("KGCOV_TOOLCHAINS")


def build_profile_ids() -> "list[str]":
    """The Makefile's default ``KGCOV_KERNELS``, read off the file; ``x86_64-cross`` leads."""
    return _makefile_default("KGCOV_KERNELS")


def _build_title(kernel_id: str) -> str:
    return "x86_64 cross build" if kernel_id == CROSS_PROFILE else f"kernel {kernel_id}"


def profiles() -> "list[Profile]":
    """Every matrix column: the Makefile's bed toolchains, then its build kernels."""
    return [
        *(Profile(n, n, BED) for n in bed_profile_ids()),
        *(Profile(k, _build_title(k), BUILD) for k in build_profile_ids()),
    ]


def _fixtures_of(node: "ast.FunctionDef | ast.AsyncFunctionDef") -> "set[str]":
    args = node.args
    return {a.arg for a in [*args.posonlyargs, *args.args, *args.kwonlyargs]}


def _walk(path: Path):
    """Every ``test*`` function *path* defines, as ``(nodeid, node)``, classes included."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    rel = path.relative_to(PROJECT_ROOT).as_posix()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith(
            "test"
        ):
            yield f"{rel}::{node.name}", node
        elif isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
            for inner in node.body:
                if isinstance(
                    inner, (ast.FunctionDef, ast.AsyncFunctionDef)
                ) and inner.name.startswith("test"):
                    yield f"{rel}::{node.name}::{inner.name}", inner


def discover_contracts() -> "list[str]":
    """Every kgcov contract the tree declares now: file order, then source order."""
    return [
        nodeid
        for path in KGCOV_TESTS
        for nodeid, node in _walk(path)
        if _fixtures_of(node) & CONTRACT_FIXTURES.keys()
    ]


def venue_of_contract(contract: str) -> "str | None":
    """The venue a contract measures in, from the fixture it takes; None when it takes none."""
    for path in KGCOV_TESTS:
        for nodeid, node in _walk(path):
            if nodeid == contract:
                taken = _fixtures_of(node) & CONTRACT_FIXTURES.keys()
                return CONTRACT_FIXTURES[min(taken)] if taken else None
    return None


UNTESTED: "dict[str, str]" = {"status": UNTESTED_STATUS}


def build_matrix(existing: "dict | None" = None) -> dict:
    """The artifact for the tree as it stands, keeping *existing* verdicts.

    Cells the tree still declares keep whatever verdict they carry; cells it
    no longer declares are dropped and new ones start :data:`UNTESTED`. This
    function NEVER writes a ``measured-*`` verdict of its own -- that is
    reserved for the collator (spec 2026-09-18 §3). A row named in
    RENAMED_ROWS is carried under its new id, its cells' nodeid rewritten to
    the new contract.
    """
    contract_of_id = {s.id: s.contract for s in SURFACES}
    old: dict[str, dict] = {}
    for row_id, row in (existing or {}).get("cells", {}).items():
        new_id = RENAMED_ROWS.get(row_id, row_id)
        carried = {}
        for profile_id, raw_cell in row.items():
            cell = dict(raw_cell)
            if row_id != new_id and "nodeid" in cell and new_id in contract_of_id:
                cell["nodeid"] = contract_of_id[new_id]
            carried[profile_id] = cell
        old.setdefault(new_id, {}).update(carried)
    cols = profiles()
    return {
        "$schema": "./kgcov-matrix.schema.json",
        "format": FORMAT,
        "surfaces": [
            {
                "id": s.id,
                "title": s.title,
                "venue": s.venue,
                "contract": s.contract,
                "control": s.control,
            }
            for s in SURFACES
        ],
        "profiles": [{"id": p.id, "title": p.title, "venue": p.venue} for p in cols],
        "cells": {
            s.id: {
                p.id: dict(old.get(s.id, {}).get(p.id, UNTESTED))
                for p in cols
                if p.venue == s.venue
            }
            for s in SURFACES
        },
    }


def rewrite_matrix_axes() -> str:
    """Re-derive :data:`MATRIX_PATH`'s axes in place; report what moved."""
    existing = json.loads(MATRIX_PATH.read_text(encoding="utf-8")) if MATRIX_PATH.exists() else None
    rebuilt = build_matrix(existing)
    MATRIX_PATH.write_text(json.dumps(rebuilt, indent=2) + "\n", encoding="utf-8")
    was = {(s, p) for s, row in (existing or {}).get("cells", {}).items() for p in row}
    now = {(s, p) for s, row in rebuilt["cells"].items() for p in row}
    return f"{MATRIX_PATH}: {len(now)} cells (+{len(now - was)} added, -{len(was - now)} removed)"


def load_matrix() -> dict:
    """The committed artifact at :data:`MATRIX_PATH`, as-is."""
    return json.loads(MATRIX_PATH.read_text(encoding="utf-8"))


def validation_errors(matrix: dict) -> "list[str]":
    """Every schema message *matrix* raises, in path order."""
    validator = Draft202012Validator(json.loads(SCHEMA_PATH.read_text(encoding="utf-8")))
    return [
        f"{'/'.join(str(p) for p in e.absolute_path) or '<root>'}: {e.message}"
        for e in sorted(
            validator.iter_errors(matrix), key=lambda e: list(map(str, e.absolute_path))
        )
    ]


def axes_mismatch(matrix: dict) -> "list[str]":
    """Every way *matrix*'s axes disagree with the tree's (rows) and the Makefile's (columns)."""
    problems: "list[str]" = []
    # The TITLE is compared too: it is the row label the page renders from the
    # artifact, so a title left behind when the table's moved on is a caption
    # that describes a contract the row no longer holds.
    want_rows = [(s.id, s.title, s.contract, s.venue, s.control) for s in SURFACES]
    have_rows = [
        (s["id"], s["title"], s["contract"], s["venue"], s["control"]) for s in matrix["surfaces"]
    ]
    if have_rows != want_rows:
        problems.append(f"surfaces differ from the table: {have_rows} != {want_rows}")
    declared = set(discover_contracts())
    tabled = {s.contract for s in SURFACES}
    for extra in sorted(declared - tabled):
        problems.append(f"the tree declares {extra}, which the surface table does not name")
    for gone in sorted(tabled - declared):
        problems.append(f"the surface table names {gone}, which the tree no longer declares")
    want_cols = [(p.id, p.venue) for p in profiles()]
    have_cols = [(p["id"], p["venue"]) for p in matrix["profiles"]]
    if have_cols != want_cols:
        problems.append(f"profiles differ from the Makefile default: {have_cols} != {want_cols}")
    return problems
