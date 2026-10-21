"""End-to-end: the hierarchical coverage example on the bed (test1).

tests/project_hier is a CMake project laid out the way real ones are: a shared
library (``libcore.so``, its own object directory) that ``app`` reaches only
through ``DT_NEEDED``; a second product, ``tool``, whose only source shares
app's basename (``main.cpp``); a gtest unit-test tree under the project root
that compiles the library's source again and is the project's unit tier; and a
stale second build tree (``build-old/``, compiled with ``-DSTALE=ON`` so its
code and stamps differ). Each pass is the user's own flow::

    tests/project_hier/build.sh                # also runs the unit tests once
    otto -l unix run install-tools && otto -l unix run install
    otto -l unix cov clean
    otto -l unix host test1 exec '<app 3 times, tool 2 times>'
    otto -l unix cov get
    otto -l unix cov report <get output dir> --dir <report>

The first pass runs with ``build-old/`` present, the second with it removed;
each ``.gcda`` must pair with its own notes by path both times, so the two
reports' system tiers are line-for-line identical. The unit tier counts only
product sources: the library's ``core.cpp``, never the gtest test file.

The products run with ``GCOV_PREFIX_STRIP`` set to the project root's depth, so
every object's ``.gcda`` is written at its path relative to the project root —
the one build rule the coverage docs state.

**Prerequisites:** test1 up; ``cmake``, ``g++``, ``libgtest-dev`` and ``lcov``
on the dev VM;
test1 must share the dev VM's architecture and glibc, because
``app``, ``tool`` and ``libcore.so`` are built on the dev VM and run on test1
as they are. A host that is down fails the run; nothing here skips. Teardown
runs ``otto run cleanup --no-reset-impairments`` whatever happened, so the bed
ends clean: the flag is there because the unix lab's test1 carries a link to
the busybox lab's ``bb1350-qemu`` guest, which ``--lab unix`` does not load, so
the impairment repair would refuse that link and fail cleanup — a stale link
outside this test, which places no impairments; the flag stays until #695 is
fixed. Pinned to the one coverage
xdist group: the product directories on test1 are shared state.
"""

import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from otto.coverage.capture.model import Capture
from otto.coverage.store.model import CoverageStore, FileRecord
from tests._fixtures.paths import CHECKOUT_ROOT
from tests.e2e._otto_subprocess import output_dirs, run_otto
from tests.e2e.cov._project5_build import _hits, _line_of

PROJECT_HIER = CHECKOUT_ROOT / "tests" / "project_hier"
STALE_TREE = PROJECT_HIER / "build-old"
_LAB = "unix"
_HOST = "test1"
PRODUCTS = {"app", "tool"}

# The three measured sources, relative to the project root, and the marker
# comment on the first statement of the function each product exercises.
CORE = "src/core/core.cpp"
APP = "src/app/main.cpp"
TOOL = "src/tool/main.cpp"
SOURCES = {CORE, APP, TOOL}
MARKERS = {CORE: "// hier: core-entry", APP: "// hier: app-main", TOOL: "// hier: tool-main"}
APP_RUNS = 3
TOOL_RUNS = 2
# tests/unit/test_core.cpp's two TESTs each call core_scale once.
UNIT_CALLS = 2

# Each product's .gcda, at its path under the product's directory: the
# object's own path relative to the project root, CMake's per-target object
# directory included.
GCDA = {
    "app": {
        "build/src/app/CMakeFiles/app.dir/main.cpp.gcda",
        "build/src/core/CMakeFiles/core.dir/core.cpp.gcda",
    },
    "tool": {"build/src/tool/CMakeFiles/tool.dir/main.cpp.gcda"},
}

# GCOV_PREFIX_STRIP drops the project root's components, leaving each object's
# path relative to it.
_STRIP = len(PROJECT_HIER.parts) - 1


def _run(
    argv: list[str], *, xdir: Path, timeout: int = 600, debug: bool = False
) -> subprocess.CompletedProcess[str]:
    """Run ``otto --lab unix ARGV`` against project_hier and fail loudly on a non-zero exit.

    *debug* runs it at ``--log-level DEBUG``, on a wide console so no log line
    is folded mid-path.
    """
    result = run_otto(
        argv,
        xdir=xdir,
        roots=PROJECT_HIER,
        lab=_LAB,
        timeout=timeout,
        extra_argv_prefix=["--log-level", "DEBUG"] if debug else None,
        extra_env={"COLUMNS": "400"} if debug else None,
    )
    if result.returncode != 0:
        raise AssertionError(
            f"otto --lab {_LAB} {' '.join(argv)} exited {result.returncode}\n"
            f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
        )
    return result


def _exercise() -> str:
    """The shell command that runs app and tool on the host, failing on the first error."""
    app = (
        f"GCOV_PREFIX=/tmp/otto-hier-cov-app GCOV_PREFIX_STRIP={_STRIP} "
        "LD_LIBRARY_PATH=/tmp /tmp/app"
    )
    tool = f"GCOV_PREFIX=/tmp/otto-hier-cov-tool GCOV_PREFIX_STRIP={_STRIP} /tmp/tool"
    return (
        f"for i in $(seq {APP_RUNS}); do {app} || exit 1; done; "
        f"for i in $(seq {TOOL_RUNS}); do {tool} || exit 1; done"
    )


@dataclass
class Pass:
    """One pass of the flow: its run tree, its report's store and the get's console text."""

    cov_dir: Path
    store: CoverageStore
    store_json: dict[str, Any]
    get_output: str
    report_output: str


def _one_pass(root: Path) -> Pass:
    xdir = root / "xdir"
    xdir.mkdir(parents=True)
    report_dir = root / "report"
    _run(["run", "install-tools"], xdir=xdir)
    _run(["run", "install"], xdir=xdir)
    _run(["cov", "clean"], xdir=xdir)
    _run(["host", _HOST, "exec", _exercise()], xdir=xdir)
    got = _run(["cov", "get"], xdir=xdir)
    gets = [d for d in output_dirs(xdir, "cov") if d.name.endswith("_get")]
    assert len(gets) == 1, f"expected one cov get output dir, found {gets}"
    get_dir = gets[0]
    reported = _run(
        ["cov", "report", str(get_dir), "--dir", str(report_dir)],
        xdir=xdir,
        timeout=300,
        debug=True,
    )
    store_path = report_dir / "store.json"
    return Pass(
        cov_dir=get_dir / "cov",
        store=CoverageStore.load(store_path),
        store_json=json.loads(store_path.read_text()),
        get_output=" ".join((got.stdout + got.stderr).split()),
        report_output=" ".join((reported.stdout + reported.stderr).split()),
    )


@pytest.fixture(scope="module")
def built():
    """Build build/ and the stale build-old/ (incremental; the stale tree is rebuilt if removed)."""
    result = subprocess.run(
        [str(PROJECT_HIER / "build.sh")], check=False, capture_output=True, text=True
    )
    if result.returncode != 0:
        raise AssertionError(
            f"build.sh exited {result.returncode}\n"
            f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
        )
    assert (STALE_TREE / "src" / "core" / "libcore.so").is_file()


@pytest.fixture(scope="module")
def passes(built, tmp_path_factory):
    """Run the flow with the stale tree, then without it; clean the bed either way.

    Returns ``{"stale": Pass, "clean": Pass}``. The second pass removes
    ``build-old/`` first; ``built`` makes it again on the next session.
    """
    root = tmp_path_factory.mktemp("hier_e2e")
    try:
        with_stale = _one_pass(root / "stale")
        shutil.rmtree(STALE_TREE)
        without_stale = _one_pass(root / "clean")
        yield {"stale": with_stale, "clean": without_stale}
    finally:
        teardown = root / "cleanup"
        teardown.mkdir()
        # --no-reset-impairments: the unix lab's test1 carries a link to the
        # busybox lab's bb1350-qemu guest, which `--lab unix` does not load, so
        # the impairment repair refuses that link and fails cleanup. This
        # fixture places no impairments, so it has none to repair. Until #695
        # is fixed.
        _run(["run", "cleanup", "--no-reset-impairments"], xdir=teardown)


def _record(store: CoverageStore, rel: str) -> FileRecord:
    for fr in store.files():
        if fr.path == PROJECT_HIER / rel:
            return fr
    raise AssertionError(f"no FileRecord for {rel}; have {[str(f.path) for f in store.files()]}")


def _line_hits(store: CoverageStore, rel: str) -> dict[int, int]:
    rec = _record(store, rel)
    return {n: lr.hits.for_tier("system") for n, lr in sorted(rec.lines.items())}


@pytest.mark.integration
@pytest.mark.xdist_group("coverage_e2e")
class TestHierarchicalCoverage:
    @pytest.mark.parametrize("which", ["stale", "clean"])
    def test_every_product_captured(self, passes, which):
        cov_dir = passes[which].cov_dir
        # Dot entries are otto's own: the metadata sidecar stays, while the
        # capture's ``.otto-stage`` scratch tree is gone once collection is done.
        assert {d.name for d in cov_dir.iterdir() if not d.name.startswith(".")} == {_HOST}
        assert not (cov_dir / ".otto-stage").exists()
        assert {d.name for d in (cov_dir / _HOST).iterdir()} == PRODUCTS
        for product in PRODUCTS:
            capture = cov_dir / _HOST / product / "capture.json"
            assert Capture.load(capture).product == product
        assert not list(cov_dir.rglob("capture.failed"))

    @pytest.mark.parametrize("which", ["stale", "clean"])
    def test_gcda_keep_their_object_paths(self, passes, which):
        """Fetched counters mirror the object tree; app's include the library's."""
        cov_dir = passes[which].cov_dir
        for product, expected in GCDA.items():
            product_dir = cov_dir / _HOST / product
            found = {p.relative_to(product_dir).as_posix() for p in product_dir.rglob("*.gcda")}
            assert found == expected, product

    def test_exact_hit_counts(self, passes):
        """The library counts app's three runs; each main counts its own product's runs."""
        store = passes["stale"].store
        expected = {CORE: APP_RUNS, APP: APP_RUNS, TOOL: TOOL_RUNS}
        for rel, hits in expected.items():
            line = _line_of(PROJECT_HIER / rel, MARKERS[rel])
            assert _hits(_record(store, rel), line) == hits, f"{rel}:{line}"

    def test_stale_tree_changes_nothing(self, passes):
        for rel in sorted(SOURCES):
            stale = _line_hits(passes["stale"].store, rel)
            clean = _line_hits(passes["clean"].store, rel)
            assert stale, f"{rel}: no line records"
            assert stale == clean, rel

    def test_library_reached_through_dt_needed(self, passes):
        """Without the stale tree, libcore.so is the one match for app's DT_NEEDED soname.

        So the library's object is on app's object list and pairs with its own
        notes directly. The stale half pins a known limitation: with
        ``build-old/`` present the root holds two ``libcore.so``, so the soname
        has two matches and DT_NEEDED is not followed; the library's .gcda is
        then not on app's object list and pairs by path search instead. The
        identical reports above show that search reached the same notes.
        """
        unlisted = "not in the artifact's object list"
        assert unlisted in passes["stale"].get_output
        assert unlisted not in passes["clean"].get_output

    @pytest.mark.parametrize("which", ["stale", "clean"])
    def test_report_holds_only_product_sources(self, passes, which):
        reported = {
            fr.path.relative_to(PROJECT_HIER).as_posix() for fr in passes[which].store.files()
        }
        assert not {p for p in reported if p.startswith(("tests/", "build-old/"))}
        assert reported == SOURCES

    @pytest.mark.parametrize("which", ["stale", "clean"])
    def test_every_run_carries_host_and_product(self, passes, which):
        """Every e2e run names its host and product; the clean pass adds the unit run.

        The unit tier's plain ``harvest_dirs`` sweep is one run with no host
        and no product behind it. The stale pass has none (see
        :meth:`test_stale_unit_view_keeps_nothing`).
        """
        runs = passes[which].store_json["runs"]
        e2e = sorted((r["host"], r["product"]) for r in runs if r["tier"] == "system")
        assert e2e == [(_HOST, "app"), (_HOST, "tool")]
        unit = [(r["host"], r["product"]) for r in runs if r["tier"] == "unit"]
        assert unit == ([("", "")] if which == "clean" else [])
        assert len(runs) == len(e2e) + len(unit)

    def test_unit_tier_holds_only_product_sources(self, passes):
        """The unit build compiles core.cpp and the gtest file; only core.cpp counts.

        core.cpp is app's source through its DT_NEEDED library, so it is a
        product source; tests/unit/test_core.cpp is no product's. core.h is
        not reported because the notes never name it: it holds a declaration
        and no code.
        """
        store = passes["clean"].store
        unit_files = {
            fr.path.relative_to(PROJECT_HIER).as_posix()
            for fr in store.files()
            if any(lr.hits.for_tier("unit") for lr in fr.lines.values())
        }
        assert unit_files == {CORE}
        line = _line_of(PROJECT_HIER / CORE, MARKERS[CORE])
        assert _record(store, CORE).lines[line].hits.for_tier("unit") == UNIT_CALLS

    def test_unit_filter_counts_what_it_dropped(self, passes):
        """The filter keeps core.cpp for app and drops the test file and every header.

        The dropped count is not pinned: besides tests/unit/test_core.cpp it
        holds the system and gtest headers the compiler records for the test
        file, which a gcc or libgtest-dev upgrade changes. Each of those is
        an absolute path outside the project.
        """
        out = passes["clean"].report_output
        counts = re.search(
            r"unit unit: kept (\d+) product source files, dropped (\d+) not in any product", out
        )
        assert counts is not None, out
        assert int(counts[1]) == 1
        assert int(counts[2]) >= 2
        keeps = re.findall(r"unit unit: keep (\S+) \(product (\S+)\)", out)
        assert keeps == [(CORE, "app")]
        drops = re.findall(r"unit unit: drop (\S+) \(not in any product\)", out)
        assert "tests/unit/test_core.cpp" in drops
        others = [d for d in drops if d != "tests/unit/test_core.cpp"]
        assert others, drops
        for dropped in others:
            path = Path(dropped)
            assert path.is_absolute(), dropped
            assert not path.is_relative_to(PROJECT_HIER), dropped
        assert len(drops) == int(counts[2])

    def test_stale_unit_view_keeps_nothing(self, passes):
        """With build-old/ present the unit tier keeps nothing: a known limitation (#694).

        Two ``libcore.so`` under the project root make app's DT_NEEDED
        ambiguous, so core.cpp is not on app's object list and no product
        owns it (as :meth:`test_library_reached_through_dt_needed` pins for
        collection). The unit view then skips its capture and adds no run.
        """
        out = passes["stale"].report_output
        assert "unit unit: kept 0 product source files" in out
        assert "unit unit: no product source files under" in out
        assert "nothing to capture" in out
        assert not [r for r in passes["stale"].store_json["runs"] if r["tier"] == "unit"]
