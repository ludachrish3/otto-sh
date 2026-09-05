"""Measure otto's import footprint per CLI surface — deterministic, host-independent.

The metric is *module count / module identity* and *file I/O*, never wall-clock.
Each surface is measured in a fresh subprocess with a sanitized env (all OTTO_*
vars stripped) so the footprint reflects otto-core only, regardless of the dev's
labs / SUT dirs. The one thing put BACK is a throwaway ``OTTO_HOME``, on every
surface: the runner's real ``~/.otto`` is machine state, and ``open_home``
gates what a startup reads there (see :func:`surface_env`).

WALL-CLOCK CANNOT GATE, WHICH IS WHY THE I/O GOLDENS EXIST. Syscall counts
reproduced a real NFS deployment's cold `otto --version` to the one
significant figure that field observation carries (2,427 syscalls x 1.2 ms
RTT ~ 2.9 s against an observed ~3 s), where a dev-box wall-clock number
predicted nothing about that machine at all — and they repeat identically run
to run where a timing number never does. `--hyperfine` stays as a MANUAL
diagnostic (`make hyperfine` installs the tool); it is no longer wired into
`make profile`, i.e. no longer part of the release gate. See
docs/guide/startup-performance.md.

I/O goldens are keyed per Python minor (``<key>.io.<major.minor>.txt``) and
are checked against the RUNNING interpreter's file; a missing file is a named
failure, never a skip. `--update` regenerates only the running interpreter's.

The ``real_entry`` surfaces are the deliberate exception: they run the console
entry path against a GENERATED repo, because startup I/O against an empty
workspace is zero and therefore unmeasurable. They stay host-independent the
same way — the harness creates what it measures.

Usage:
    python scripts/import_budget.py            # print a per-surface count table
    python scripts/import_budget.py --update    # regenerate golden snapshots
    python scripts/import_budget.py --check      # enforce the budget; exit non-zero on a breach
    python scripts/import_budget.py --hyperfine  # also show wall-clock stats (manual)
"""

import argparse
import atexit
import functools
import json
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SNAPSHOT_DIR = REPO_ROOT / "tests" / "unit" / "import_budget" / "snapshots"


@dataclass(frozen=True)
class Surface:
    """One measured CLI surface: its argv, denied heavy stacks, and non-stdlib module cap."""

    key: str
    argv: list[str]
    deny: tuple[str, ...]
    cap: int | None = None
    bootstrap: bool = False
    """Run the composition root before resolving the dispatch target.

    Off for the surfaces that measure LAZY DISPATCH — how much a command
    resolution costs on its own, which is what the completion fast path pays.
    On for the surface that measures a REAL INVOCATION, where
    ``otto.cli.main.entry`` calls ``bootstrap()`` before argv is parsed.
    """

    sut_files: int | None = None
    """Generate a repo with this many NESTED test files and measure against it.

    None keeps the historical behaviour: no repos, otto-core only.
    """

    sut_dirs_count: int = 1
    """Subdirectory count, scaled independently of ``sut_files``: a stat-only
    walk is visible only per directory, via ``os.scandir``."""

    real_entry: bool = False
    """Run the console-script entry path instead of resolving a dispatch
    target. Required to observe bootstrap and cache behaviour; carries its own
    snapshots because it admits help-rendering imports the other surfaces
    deliberately exclude."""

    warm: bool = False
    """Measure the SECOND run against one repo and one ``OTTO_HOME``.

    Every measurement is otherwise COLD by construction: ``surface_env``
    mints a fresh ``OTTO_HOME`` per call so repeated measurements of one
    surface stay independent. That is right for the fallback path and wrong
    for the cached one — a cold ``--help`` MUST walk the corpus (miss → full
    bootstrap → collect → write), because cache-or-load never degrades help.
    A warm surface therefore runs the child TWICE against the same generated
    repo and the same home: a discarded seed run, then the measured one.

    The seed writes ONLY into ``OTTO_HOME`` (the cache file). Nothing is
    written into the fixture tree — ``PYTHONDONTWRITEBYTECODE`` still keeps
    ``__pycache__`` out of it — so the pair stays deterministic and repeated
    warm measurements read identical counts.
    """

    env_extra: tuple[tuple[str, str], ...] = ()
    """Extra environment variables for the child, as ``(name, value)`` pairs.

    The completion fast path is a MODE, not an argv: a shell asks for
    completions by running the bare console script with ``_OTTO_COMPLETE`` /
    ``COMP_WORDS`` / ``COMP_CWORD`` set (click's protocol), so a surface that
    measures what a TAB costs cannot be expressed with ``argv`` alone.

    Pairs rather than a dict so the field stays declarative and the table
    stays diffable; the sanitizer runs FIRST, then these are applied, so a
    surface may deliberately set an ``OTTO_*`` var the sanitizer would
    otherwise have stripped.
    """


def surface_by_key(key: str) -> Surface:
    """Return the surface named *key*. Never index SURFACES positionally."""
    for surface in SURFACES:
        if surface.key == key:
            return surface
    raise KeyError(f"no surface named {key!r}")


# Heavy third-party stacks that must stay off the surfaces that don't own them.
_ALL_HEAVY = ("fastapi", "uvicorn", "starlette", "pytest")

# Caps are on the NON-STDLIB module count (otto + third-party), never the full
# sys.modules total. The stdlib import graph drifts across Python versions
# (e.g. 3.14 pulls in compression.zstd, annotationlib, asyncio.graph, ...):
# noise unrelated to otto's own footprint. One cap (baseline + ~15 headroom)
# then holds on every gated interpreter. This is the same "stable across
# dependency/version upgrades" rule the design already applies to the
# otto-only golden snapshot.
#
# THE NON-STDLIB COUNT IS NOT IDENTICAL ACROSS INTERPRETERS, so a cap is
# baselined on the HIGHEST-MEASURING gated one — today 3.10. A third party may
# import a compat module only on the older interpreter: `markdown_it._compat`
# is on `help_repo` under 3.10 and gone under 3.11+, which puts 3.10 one module
# above every other version on that surface (464 vs 463 as of typer 0.27.2).
# A cap baselined on 3.11+ is therefore already breached on 3.10 the day it
# lands, and the 3.10 lane is the only one that says so — #303.
#
# The ~15 headroom is for a FULL-PATH surface: one whose deny list still lets
# some third-party stack (typer, click, rich, ...) through, so a version bump
# in one of those can move its count without otto importing anything new —
# exactly what opened #303, where typer 0.27.2 reparented its vendored click
# exceptions onto a new `typer.exceptions` module and every typer-bearing
# surface gained one module that otto neither imports nor can defer. A
# surface whose deny list forbids every third party instead (`version_repo`,
# `completion_repo_warm`) has a module set that is structurally fixed by that
# deny list — nothing outside otto's own graph can ever appear — so it gets
# an exact cap with no headroom, the same way the golden snapshot itself is
# exact.
SURFACES: list[Surface] = [
    Surface("import_otto", ["python"], _ALL_HEAVY, cap=19),  # lazy __init__ (Part D)
    Surface("help", ["otto", "--help"], _ALL_HEAVY, cap=188),
    Surface("run", ["otto", "run", "--help"], _ALL_HEAVY, cap=154),
    Surface("host", ["otto", "host", "--help"], _ALL_HEAVY, cap=255),
    Surface("reservation", ["otto", "reservation", "--help"], _ALL_HEAVY, cap=251),
    Surface("docker", ["otto", "docker", "--help"], _ALL_HEAVY, cap=258),
    Surface("schema", ["otto", "schema", "--help"], _ALL_HEAVY, cap=150),
    # monitor owns the dashboard, so fastapi/uvicorn/starlette are allowed here.
    Surface("monitor", ["otto", "monitor", "--help"], ("pytest",), cap=265),
    # test runs the suite, so pytest is allowed here.
    Surface("test", ["otto", "test", "--help"], ("fastapi", "uvicorn", "starlette"), cap=251),
    Surface(
        "cov", ["otto", "cov", "--help"], ("fastapi", "uvicorn", "starlette", "pytest"), cap=265
    ),
    # THE COMPOSITION ROOT IS ON THE PATH OF EVERY REAL INVOCATION, and until
    # this surface existed nothing measured it: every surface above resolves a
    # dispatch target through the root group WITHOUT calling `bootstrap()`, so
    # when bootstrap grew an import (it now imports `otto.project.instructions`
    # to register the first-party `otto run` verbs, which pulls otto.project
    # and its dependents) the guard measured none of it and stayed green.
    #
    # Deliberately a SECOND surface over the same argv as `run` rather than a
    # change to that one: the pair is the measurement. `run` keeps reporting
    # what lazy dispatch costs by itself (what the completion fast path pays,
    # which never bootstraps), and the DIFFERENCE between the two snapshots is
    # the composition root's own footprint. `run` is the argv because the verbs
    # bootstrap registers are `otto run`'s.
    # cap 267 -> 278 per #303: 263 measured on 3.10 + 15, restoring the
    # headroom a full-path surface is supposed to carry.
    Surface("run_bootstrapped", ["otto", "run", "--help"], _ALL_HEAVY, cap=278, bootstrap=True),
    # EVERY SURFACE ABOVE MEASURES AN EMPTY WORKSPACE. The sanitized env strips
    # OTTO_*, so `sut_dirs` is empty, discovery finds zero repos, and the walk
    # this spec exists to remove costs nothing on any of them — `scandir` reads
    # 0 across the whole table, including `run_bootstrapped`. A guard that
    # cannot observe the defect cannot witness the fix either.
    #
    # The surfaces below carry a GENERATED repo (deterministic by
    # construction, so they stay as host-independent as the rest of the table)
    # and — `bootstrap_repo`, which drives the composition root directly, aside
    # — run the REAL entry path, so they observe bootstrap and cache
    # behaviour no other surface can see. They carry their own snapshots:
    # entry() renders help, admitting rich-markdown and pygments that the
    # dispatch-only surfaces exclude on purpose.
    # Capped as of Task 4: the console script now answers `--version` from
    # `otto._shim` without importing the CLI, so the measured non-stdlib set is
    # exactly {otto, otto._shim, otto.version} — 3, capped at 4.
    #
    # THE HEADROOM IS ONE, DELIBERATELY. The usual reason for slack — stdlib
    # and dependency drift across 3.10-3.14 — cannot apply: this path imports
    # no third party at all, so the measured set is structurally fixed, and
    # any legitimate growth regenerates the snapshot anyway (which is a
    # reviewed diff, not a silent raise). Slack here buys nothing and costs
    # detection: measured directly, a stray module-scope `import rich` adds
    # only TWO non-stdlib modules (rich's submodules are lazy) and
    # `platformdirs` five, so a cap of 4 would wave neither through only
    # because it is this tight.
    #
    # The denylist is the second, independent edge, and it is what actually
    # caught the rich case: click/typer/rich ARE the framework whose
    # ~2400-syscall import this fast path exists to skip, so their presence is
    # the regression at ANY module count — including one.
    Surface(
        "version_repo",
        ["otto", "--version"],
        ("pytest", "rich", "typer", "click"),
        cap=4,
        sut_files=50,
        sut_dirs_count=5,
        real_entry=True,
    ),
    # THE HELP PAIR. `help_repo` is COLD — a fresh OTTO_HOME per measurement,
    # so it is the fallback path: cache miss → full bootstrap → collect →
    # write, which is what cache-or-load promises and must keep costing what
    # a complete answer costs. It is therefore also the surface that PROVES
    # THE HARNESS still finds a real repo (`scandir >= 2 * dirs`), which is
    # why it stays in the table unchanged rather than being replaced.
    #
    # `help_repo_warm` is the same surface measured on its SECOND run against
    # one home, i.e. the cached path Task 7 built: root help resolves the
    # command list from the `names` section and never walks the corpus. The
    # scaling gate keys on this one — a cold help legitimately scales,
    # because a full load legitimately reads everything.
    Surface(
        "help_repo",
        ["otto", "--help"],
        ("pytest",),
        # 411 -> 463: a cold cache write now calls build_shim_payload,
        # which serialises the WHOLE CLI tree (every subcommand group, not
        # just the root) to build the `shim` section's tree — resolving each
        # one for the first time pulls in its own module plus its backends
        # (otto.coverage.*, otto.docker.*, otto.tunnel.*, otto.link.*, ...).
        # help_repo_warm is unaffected: a warm run never reaches the slow
        # path that calls it.
        #
        # 463 -> 479: 463 was the RAW 3.10 measurement, i.e. a full-path
        # surface carrying zero headroom, so the first third-party module to
        # appear anywhere on it was a CI failure (#303). Re-baselined to the
        # policy above: 464 measured on 3.10 + 15.
        cap=479,
        sut_files=50,
        sut_dirs_count=5,
        real_entry=True,
    ),
    Surface(
        "help_repo_warm",
        ["otto", "--help"],
        ("pytest",),
        # 390 -> 404: raw-measurement baseline, same #303 re-baseline as its
        # cold twin — 389 measured on 3.10 + 15.
        cap=404,
        sut_files=50,
        sut_dirs_count=5,
        real_entry=True,
        warm=True,
    ),
    # `run_bootstrapped`'s REPO-BEARING SIBLING, and the pair is again the
    # measurement. That surface runs the composition root against an EMPTY
    # workspace — zero repos discovered, `scandir` 0 — which is what isolates
    # bootstrap's own import graph from anything a workspace drags in, and it
    # must keep doing exactly that. This one runs the same root against a
    # generated repo, so the work bootstrap does PER REPO (reading the repo's
    # settings, putting its lib dirs on the path, importing its init tree) is
    # charged to a surface for the first time. Same argv for the same reason
    # the original chose it: the verbs bootstrap registers are `otto run`'s.
    Surface(
        "bootstrap_repo",
        ["otto", "run", "--help"],
        _ALL_HEAVY,
        # 274 -> 288: raw-measurement baseline, re-baselined per #303 —
        # 273 measured on 3.10 + 15.
        cap=288,
        bootstrap=True,
        sut_files=50,
        sut_dirs_count=5,
    ),
    # THE STEADY-STATE TAB COST. Completion is the surface a user hits most
    # often and notices most sharply, and it is a MODE rather than an argv:
    # the shell runs the bare console script with click's `_OTTO_COMPLETE`
    # protocol in the environment (see `Surface.env_extra`), which is why no
    # `argv`-only surface could reach it.
    #
    # WARM, like `help_repo_warm` and for the same reason: a cold cache means
    # a miss, and a miss is a full bootstrap by design — completion never
    # degrades to a wrong answer. The seed run now also leaves the `shim`
    # section (written by `entry()` on every cache write, spec §3.1), and the
    # measured run is the shim's stat pass plus one JSON read: `otto._shim`
    # answers the TAB from the cache without ever importing `otto.cli`,
    # `otto.config`, typer, click, or rich. The steady state is the
    # second and every later TAB, served from the shim's stat-and-marker
    # validator, and that is what a budget should bound.
    Surface(
        "completion_repo_warm",
        ["otto"],
        (*_ALL_HEAVY, "typer", "click", "rich", "pydantic", "pydantic_settings"),
        cap=3,
        sut_files=50,
        sut_dirs_count=5,
        real_entry=True,
        warm=True,
        env_extra=(
            ("_OTTO_COMPLETE", "complete_bash"),
            ("COMP_WORDS", "otto "),
            ("COMP_CWORD", "1"),
        ),
    ),
    # THE FALLBACK'S TAB COST. `otto tunnel remove <TAB>` (COMP_CWORD=3) is a
    # `live` site (spec §1 decision 3): the resolver hands over rather than
    # answering, and the shim falls through to the unchanged full CLI path.
    # This is a DIFFERENT site than `completion_repo_warm`'s — that one
    # completes top-level command NAMES (COMP_CWORD=1), which never resolves
    # a specific command's module; this one resolves three levels deep into
    # `tunnel remove`'s own argument completer, which imports `otto.cli.tunnel`
    # and, transitively, all of `otto.tunnel`, `otto.project`,
    # `otto.instructions`, `otto.host.daemon`, and `otto.config.fleet`/`lab`/
    # `dependencies` — modules the top-level site never touches. Its module
    # set is therefore what THIS SAME TAB cost before the shim existed, plus
    # `otto._shim_complete` (the one new import the resolver adds on the way
    # to deciding to hand over). The goldens show `open_home 4, scandir 2`
    # against the warm surface's `2, 0`, and that is not a regression against
    # it: the warm surface is a different, shallower site with a different,
    # much smaller baseline, so "as cheap as the warm path" was never the bar
    # here. This site has no pre-shim golden to diff against, so the
    # shim's own contribution here is not a measured delta; it is a fact of
    # `otto._shim.main`'s construction — a cache read, the stat pass, and a
    # marker touch always happen before it can decide to hand over, on every
    # path, this one included.
    Surface(
        "completion_repo_handover",
        ["otto"],
        _ALL_HEAVY,
        # cap=315: measured 300 non-stdlib modules + ~15 headroom, like every
        # other full-CLI-path surface (see the SURFACES-level comment above) —
        # this surface's deny list does not fix its module set the way
        # `completion_repo_warm`'s does, so a typer/click dependency bump that
        # every peer absorbs in headroom would otherwise make this the one
        # surface that goes red on it.
        cap=315,
        sut_files=50,
        sut_dirs_count=5,
        real_entry=True,
        warm=True,
        env_extra=(
            ("_OTTO_COMPLETE", "complete_bash"),
            ("COMP_WORDS", "otto tunnel remove "),
            ("COMP_CWORD", "3"),
        ),
    ),
]

# non_stdlib_modules is the gated metric: total sys.modules minus the stdlib
# (classified via the *child's own* sys.stdlib_module_names, so each Python
# version self-classifies). Excluding the stdlib makes the count version-robust:
# the stdlib graph grows release to release, otto's footprint does not.

# Prepended to every child, BEFORE `import otto`, so every file-I/O operation
# otto's own import graph performs is observed. Audit events, not an os.stat
# wrapper: verified on 3.10.20, pathlib binds its stat accessor at import time,
# so patching os.stat afterwards counts 0 against 500 real stats, and there is
# no os.stat audit event. Path.read_text does fire "open". rglob/glob are
# deliberately not counted: otto's own walk is os.walk, and glob event
# behaviour differs across versions.
#
# ``open_fixture`` IS THE GATED HALF OF ``open``, and the split is forced by
# measurement, not taste. The whole-process ``open`` total is dominated by the
# import machinery and moves with the ENVIRONMENT rather than with otto:
#
#   * bytecode-cache state. A module whose ``.pyc`` is missing costs TWO
#     counted opens (the failed ``open`` of the cache file — the audit event
#     fires before the attempt — plus the source read), and a third when the
#     interpreter writes the cache back. Measured on one machine, one
#     interpreter (3.10.20): the `run` surface reads 552 opens the first time
#     it runs in a fresh venv and 249 every time after, and `help_repo_warm`
#     636 with a warm cache against 796 with a cold one — the repo-bearing
#     surfaces set ``PYTHONDONTWRITEBYTECODE`` (see :func:`surface_env`), so
#     for them the cold number never converges on its own.
#   * the installed distribution set. Rendering help imports pygments, whose
#     plugin lookup opens one ``entry_points.txt`` PER INSTALLED DIST: the dev
#     venv and a ``nox`` ``dev``-group venv of the SAME interpreter measured
#     645 against 636 on ``help_repo_warm`` with byte-identical module sets
#     (605 modules both), the 9 being exactly the 9 lint/arch dists the dev
#     venv carries.
#
# Neither drift has anything to do with the change under test, which makes an
# exact whole-process ``open`` golden a check that fails for reasons outside
# the change — monitoring, not gating. Opens UNDER THE FIXTURE ROOT carry
# neither: site-packages and the stdlib live outside it, and the fixture's OWN
# bytecode caches are kept out by ``PYTHONDONTWRITEBYTECODE``, which
# :func:`surface_env` pins on every repo-bearing child. THAT PIN IS LOAD-BEARING
# FOR THIS COUNTER, not just for leaving the tree clean. Drop it and the
# fixture's `.pyc` probe misses and writebacks land INSIDE the gated number:
# measured on `bootstrap_repo` with the pin removed, `open_fixture` reads 10 on
# the first child in a process and 4 on every later one — order-dependent
# within one run, because `_generated_repo_for` caches the tree per process. It
# is the exact flake class ``open`` was rejected for, so
# `test_repo_bearing_surface_isolates_home_and_repo` asserts the pin rather
# than leaving it to a comment.
#
# ATTRIBUTION IS A PREFIX MATCH, AND IT CARRIES BOTH SPELLINGS OF THE ROOT.
# The harness hands the child a path built from `tempfile.mkdtemp`, while otto
# may resolve what it opens — so on a box whose TMPDIR is a symlink the two
# would not share a prefix and the counter would silently read low. The child
# therefore matches against the raw root AND its `realpath`. Bytes paths are
# decoded; an `int` (an `open` on an existing fd) is skipped. RELATIVE paths
# are out of scope and cannot occur here: every path under measurement is
# built from the ABSOLUTE `OTTO_SUT_DIRS` / `OTTO_HOME` the harness injects,
# and a miss would undercount, which the non-zero liveness pins in
# `tests/unit/import_budget/` would catch.
#
# What remains under the root is exactly the workspace I/O this budget exists
# to bound (a warm `otto --help` opens two files there: the repo's
# `.otto/settings.toml` and `completion_cache.json`). Both envs above measured
# it identical. The whole-process total stays in the payload as CONTEXT for a
# failure and as the input to the corpus-scaling deltas the unit suite
# asserts — those compare two measurements from ONE environment, so the drift
# cancels and ``open`` stays gated where it can be.
#
# ``open_home`` IS THE SAME MECHANISM POINTED AT ``OTTO_HOME`` — same dual
# spelling, same bytes decode, same str-only guard — and it is a SECOND VIEW
# rather than a partition. On a repo-bearing surface the temp home lives
# inside the fixture root (`surface_env` puts it there so one atexit sweep
# collects both), so the cache file a warm start reads is counted by BOTH
# counters. The overlap is the point: `open_fixture` bounds the workspace I/O
# as a whole, and inside that total a repo read and a home read are
# indistinguishable, while the home half is the one that hurts when `$HOME` is
# on a network filesystem and the repo half is not.
#
# IT GATES THE OPEN, NOT THE WHOLE FOOTPRINT. There is no stat audit event —
# the same fact that makes `scandir` the only observable for the corpus walk —
# so the warm read's existence stat and the user `settings.toml` probe ride
# invisibly. The warm home footprint is 1 open + 2 stats; this counter is an
# exact bound on the open, which is the term that dominates on a network home,
# and NOT a claim that the open is all of it.
#
# IT ALSO NEEDS ``OTTO_HOME`` ON EVERY SURFACE, which is why
# :func:`surface_env` mints one for the non-repo surfaces too. Without the var
# (the sanitizer strips `OTTO_*`) two things are true at once: the prefix
# tuple is empty, so the counter is structurally 0 for that surface — a gate
# that cannot observe anything — and the child resolves `~/.otto`, so whatever
# home I/O it does perform lands on the DEVELOPER'S REAL HOME and is charged
# to `open` and to the gated `scandir`/`listdir`. A counter whose value
# depends on what one box happens to keep under `~/.otto` is the drift class
# `open` was rejected for.
_CHILD_IO_PREAMBLE = """
import os as _os
import sys as _sys
_fixture_root = _os.environ.get("IMPORT_BUDGET_FIXTURE_ROOT")
_fixture_prefixes = ()
if _fixture_root:
    _fixture_prefixes = (_fixture_root + _os.sep,)
    _fixture_real = _os.path.realpath(_fixture_root) + _os.sep
    if _fixture_real not in _fixture_prefixes:
        _fixture_prefixes += (_fixture_real,)
_home_root = _os.environ.get("OTTO_HOME")
_home_prefixes = ()
if _home_root:
    _home_prefixes = (_home_root + _os.sep,)
    _home_real = _os.path.realpath(_home_root) + _os.sep
    if _home_real not in _home_prefixes:
        _home_prefixes += (_home_real,)
_io_counts = {"open": 0, "scandir": 0, "listdir": 0, "open_fixture": 0, "open_home": 0}
def _io_hook(event, args):
    if event == "open":
        _io_counts["open"] += 1
        if _fixture_prefixes or _home_prefixes:
            _path = args[0]
            if isinstance(_path, bytes):
                _path = _os.fsdecode(_path)
            if isinstance(_path, str):
                if _fixture_prefixes and _path.startswith(_fixture_prefixes):
                    _io_counts["open_fixture"] += 1
                if _home_prefixes and _path.startswith(_home_prefixes):
                    _io_counts["open_home"] += 1
    elif event == "os.scandir":
        _io_counts["scandir"] += 1
    elif event == "os.listdir":
        _io_counts["listdir"] += 1
_sys.addaudithook(_io_hook)
def _fixture_path_entries():
    if not _fixture_root:
        return 0
    return sum(1 for p in _sys.path
               if p == _fixture_root or p.startswith(_fixture_root + _os.sep))
"""

# Child script for `import otto` surface: bare import, no CLI invocation.
_CHILD_IMPORT_BODY = """
import sys, json
import otto
mods = sorted(sys.modules)
otto_mods = [m for m in mods if m == "otto" or m.startswith("otto.")]
non_std = [m for m in mods if m.split(".")[0] not in sys.stdlib_module_names]
print(json.dumps({"count": len(mods), "modules": mods, "otto_modules": otto_mods,
                  "non_stdlib_modules": non_std, "io": _io_counts,
                  "sys_path_len": len(sys.path),
                  "fixture_path_entries": _fixture_path_entries()}))
"""
_CHILD_IMPORT = _CHILD_IO_PREAMBLE + _CHILD_IMPORT_BODY

# Child script for CLI surfaces: resolve the dispatch target through the
# registry-backed root group (otto.cli.main._OttoGroup.get_command) the same
# way a real `--help`/completion invocation would, without running Click's
# help-rendering pipeline (which would additionally pull in rich's markdown
# renderer, pygments, etc. — a measurement artifact unrelated to otto's own
# lazy-import footprint). Every surface's argv is `[..., "<name>", "--help"]`
# except the bare `help` surface (`["otto", "--help"]`, no dispatch target).
#
# `Surface.bootstrap` additionally runs the composition root, in the position
# `otto.cli.main.entry` runs it: BEFORE argv is parsed, so every import
# bootstrap performs is charged to the surface. It needs no lab and reads no
# repo — the sanitized env strips OTTO_*, `sut_dirs` then defaults to empty,
# and discovery finds zero repos — so the surface stays as host-independent
# and deterministic as the rest of the table while covering otto's own
# startup graph.
_CHILD_CLI_BODY = """
import sys, json
import typer
sys.argv = {argv!r}
import otto
if {bootstrap!r}:
    from otto import bootstrap as _bs
    _bs.bootstrap()
cmd = typer.main.get_command(otto.app)
ctx = cmd.make_context("otto", sys.argv[1:], resilient_parsing=True)
target = sys.argv[1] if len(sys.argv) > 1 and not sys.argv[1].startswith("-") else None
if target is not None:
    _ = cmd.get_command(ctx, target)
mods = sorted(sys.modules)
otto_mods = [m for m in mods if m == "otto" or m.startswith("otto.")]
non_std = [m for m in mods if m.split(".")[0] not in sys.stdlib_module_names]
print(json.dumps({{"count": len(mods), "modules": mods, "otto_modules": otto_mods,
                   "non_stdlib_modules": non_std, "io": _io_counts,
                   "sys_path_len": len(sys.path),
                   "fixture_path_entries": _fixture_path_entries()}}))
"""
_CHILD_CLI = _CHILD_IO_PREAMBLE.replace("{", "{{").replace("}", "}}") + _CHILD_CLI_BODY

# Child script for the REAL console-script entry path — the only child that
# observes bootstrap and cache behaviour, because it is the only one that runs
# what `[project.scripts]` names.
#
# IT MUST ENTER THROUGH `otto._shim:main`, NOT `otto.cli.main:entry`. The shim
# IS the console script (and `python -m otto`); calling `entry` directly walks
# straight past it, so the surface would keep measuring the graph the shim
# exists to skip and the fast path would be structurally invisible — the
# measurement would report ~500 opens for `--version` however well the shim
# worked. Measure what a user actually runs. `main()` raises SystemExit on
# --version and --help alike; both are caught so the counts can still be
# reported.
#
# `sys_path_len` and `fixture_path_entries` ride EVERY child's payload (not
# just this one) so `measure()` returns one uniform shape whichever child
# produced it. The second is the gate: `Repo.add_libs_to_pythonpath` prepends
# each discovered repo's lib dirs, and every later import probe pays for every
# entry — a regression class the module count cannot see. It counts only the
# entries under the FIXTURE ROOT, because the total length is a budget on the
# whole interpreter and moves with editable-vs-wheel installs, layout changes,
# and any dev dependency shipping a `.pth`. `sys_path_len` stays as context for
# a failure, never as a threshold.
_CHILD_ENTRY_BODY = """
import sys, json
sys.argv = {argv!r}
from otto._shim import main as _console_entry
try:
    _console_entry()
except SystemExit:
    pass
mods = sorted(sys.modules)
otto_mods = [m for m in mods if m == "otto" or m.startswith("otto.")]
non_std = [m for m in mods if m.split(".")[0] not in sys.stdlib_module_names]
print(json.dumps({{"count": len(mods), "modules": mods, "otto_modules": otto_mods,
                   "non_stdlib_modules": non_std, "io": _io_counts,
                   "sys_path_len": len(sys.path),
                   "fixture_path_entries": _fixture_path_entries()}}))
"""
_CHILD_ENTRY = _CHILD_IO_PREAMBLE.replace("{", "{{").replace("}", "}}") + _CHILD_ENTRY_BODY


# Child script measuring the empty baseline: what a bare interpreter already has
# in sys.modules before a single line of otto runs.
_CHILD_BASELINE = """
import sys, json
print(json.dumps([m for m in sorted(sys.modules)
                  if m.split(".")[0] not in sys.stdlib_module_names]))
"""


def _sanitized_env() -> dict[str, str]:
    """Env with all OTTO_* vars stripped, so measurement is lab/host independent."""
    return {k: v for k, v in os.environ.items() if not k.startswith("OTTO_")}


def _run_child(code: str, env: dict[str, str] | None = None) -> str:
    """Run *code* in a fresh interpreter and return its last stdout line.

    *env* defaults to the sanitized env — no ``OTTO_*`` at all, which is the
    right baseline for a direct ``measure`` call. A SURFACE always passes
    :func:`surface_env` instead (via :func:`measure_surface`), so its private
    ``OTTO_HOME`` — and, when it has one, its generated repo — reach the child.
    """
    out = subprocess.run(  # noqa: S603 (fixed interpreter + measured argv, no shell)
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=True,
        env=_sanitized_env() if env is None else env,
    )
    return out.stdout.strip().splitlines()[-1]


# Every temp tree the harness mints — generated fixture roots, and the parent
# of the throwaway ``OTTO_HOME``s handed to surfaces that carry no fixture —
# so the atexit sweep below removes them all. They live under the system temp
# dir and must not accumulate.
_FIXTURE_ROOTS: list[Path] = []


@atexit.register
def _remove_fixture_roots() -> None:
    """Delete every generated temp tree on interpreter exit."""
    for root in _FIXTURE_ROOTS:
        shutil.rmtree(root, ignore_errors=True)


@functools.cache
def _home_parent_for_non_repo_surfaces() -> Path:
    """Parent directory for the temp ``OTTO_HOME``s of non-repo surfaces.

    A repo-bearing surface's home sits beside its generated repo, inside the
    fixture root, so one sweep collects both. A surface with no fixture has no
    such parent, and it still needs a pinned home (see
    :data:`_CHILD_IO_PREAMBLE`) — so one throwaway parent is made on first use
    and registered for the same sweep. Made ONCE per process, not per call:
    only the leaf home has to be fresh per measurement, and the leaf is a name
    rather than a directory the harness creates.
    """
    root = Path(tempfile.mkdtemp(prefix="otto-budget-homes-"))
    _FIXTURE_ROOTS.append(root)
    return root


@functools.cache
def _generated_repo_for(key: str, files: int, dirs: int) -> Path:
    """Build (once per surface) a generated sut-dir repo and return its path.

    Keyed on the surface's NAME and shape rather than on the ``Surface`` itself:
    ``Surface.argv`` is a list, so the frozen dataclass is unhashable and cannot
    be an ``lru_cache`` key. Caching means repeated measurement of one surface —
    the script's table, then several tests — reuses one tree instead of writing
    a fresh corpus per call.
    """
    # `tests._fixtures` is not importable from the script's own sys.path[0]
    # (`scripts/`), so standalone `python scripts/import_budget.py` needs the
    # repo root on the path. Done lazily, here, so merely importing this module
    # never mutates sys.path.
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    from tests._fixtures.generated_repo import generate_repo

    root = Path(tempfile.mkdtemp(prefix=f"otto-budget-{key}-"))
    _FIXTURE_ROOTS.append(root)
    return generate_repo(root, files=files, dirs=dirs)


FIXTURE_ROOT_ENV_VAR = "IMPORT_BUDGET_FIXTURE_ROOT"
"""Tells the child which tree is the fixture, so it can report how many of its
own ``sys.path`` entries came from the repo under measurement.

Deliberately NOT ``OTTO_``-prefixed: otto parses that namespace, and a name it
does not know has no business reaching its settings model. The sanitizer only
strips ``OTTO_*``, so this one survives into the child either way.
"""


def surface_env(surface: Surface) -> dict[str, str]:
    """Env for *surface*: sanitized, a FRESH OTTO_HOME, plus any generated repo.

    ``OTTO_HOME`` is not optional, AND IT IS PINNED ON EVERY SURFACE, not only
    the repo-bearing ones. ``workspace_home()`` resolves under ``otto_home()``
    = ``$OTTO_HOME`` else ``~/.otto``, and the sanitizer strips ``OTTO_*`` — so
    without injection a child reads (and, on the surfaces that run the real
    entry path, WRITES) the developer's real ``~/.otto``, whose contents are
    machine state. Since ``open_home`` gates opens under this root, an
    unpinned surface would also be structurally blind: no var, no prefix, a
    counter that reads 0 whatever the child does. A non-repo surface's home
    therefore comes from :func:`_home_parent_for_non_repo_surfaces` rather
    than from a fixture root it does not have.

    IT IS ALSO FRESH PER CALL, and bytecode writing is off, because REPEATED
    MEASUREMENTS OF ONE SURFACE MUST BE INDEPENDENT. Two caches otherwise warm
    between calls and make the first measurement in a process differ from every
    later one — deterministic but ORDER-DEPENDENT, and under ``-n auto`` with
    pytest-randomly, which call draws the cold number varies per run. That bias
    lands directly inside the corpus delta the gates assert.

    - The child writes ``completion_cache.json`` into its ``OTTO_HOME``, so the
      home is a fresh directory per call. For a repo-bearing surface it stays
      inside the fixture root, so the atexit sweep still collects it — and so
      that a home-side open is ALSO an ``open_fixture``, which is what makes
      the two counters comparable on one surface.
    - The dominant one: importing the repo's init module and its top-level test
      files writes ``__pycache__`` INTO THE FIXTURE TREE, which is cached and
      therefore shared across calls. Measured directly — ``open`` reads 607,
      then 598, 598, 598; deleting the two ``__pycache__`` dirs returns it to
      exactly 607. ``PYTHONDONTWRITEBYTECODE`` keeps the harness from mutating
      the tree it is measuring, so every call is the cold number.

    Only these need to be fresh. The repo tree stays cached: generating a
    200-file corpus per call is pure cost, and with no bytecode written it
    never warms.
    """
    env = _sanitized_env()
    if surface.sut_files is not None:
        repo = _generated_repo_for(surface.key, surface.sut_files, surface.sut_dirs_count)
        env["OTTO_SUT_DIRS"] = str(repo)
        env[FIXTURE_ROOT_ENV_VAR] = str(repo.parent)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        home_parent = repo.parent
    else:
        home_parent = _home_parent_for_non_repo_surfaces()
    env["OTTO_HOME"] = str(home_parent / f"home-{uuid.uuid4().hex}")
    # LAST, so a surface can set what the sanitizer strips (see Surface.env_extra).
    env.update(surface.env_extra)
    return env


@functools.lru_cache(maxsize=1)
def baseline_modules() -> frozenset[str]:
    """Non-stdlib modules a bare interpreter already carries — never otto's doing.

    Site startup executes every ``.pth`` in site-packages, and legacy
    ``-nspkg.pth`` files (setuptools-era namespace packages) inject their
    package into ``sys.modules`` before user code runs. In this venv
    ``sphinxcontrib-jsmath`` does exactly that, so ``sphinxcontrib`` is present
    in ``python -c pass`` — nothing in otto imports it. ``_virtualenv`` and
    ``__main__`` arrive the same way. Charging these to otto's budget would
    make the cap depend on which unrelated dev/docs dependencies happen to be
    installed, so they are measured and subtracted rather than counted.
    """
    return frozenset(json.loads(_run_child(_CHILD_BASELINE)))


def _is_measurement_artifact(module: str) -> bool:
    """Report whether *module*'s presence reflects the build/platform, not otto's imports.

    mypyc-compiled wheels load a single hashed shared module named
    ``<hash>__mypyc`` backing the whole compiled group. Those wheels are built
    per-platform, so an x86_64 CI runner loads it where an aarch64 machine
    falls back to pure Python and does not — a one-module difference that has
    nothing to do with how much otto imports. Counting it makes the cap
    architecture-dependent, which is exactly what this guard promises not to be.
    """
    return module.endswith("__mypyc")


def measure(
    argv: list[str],
    *,
    bootstrap: bool = False,
    real_entry: bool = False,
    env: dict[str, str] | None = None,
) -> dict:
    """Import otto in a fresh subprocess for *argv*; return its module inventory.

    *bootstrap* additionally runs the composition root, as a real invocation does.
    *real_entry* runs ``otto.cli.main.entry`` itself instead, which is the only
    way to observe bootstrap AND the completion/name caches on one path.
    *env* defaults to the sanitized env; a surface passes its own
    (:func:`surface_env`), which is where ``OTTO_HOME`` comes from.
    """
    if real_entry:
        code = _CHILD_ENTRY.format(argv=argv)
    elif argv[:1] == ["python"]:
        code = _CHILD_IMPORT
    else:
        code = _CHILD_CLI.format(argv=argv, bootstrap=bootstrap)
    result = json.loads(_run_child(code, env))
    baseline = baseline_modules()
    result["non_stdlib_modules"] = [
        m
        for m in result["non_stdlib_modules"]
        if m not in baseline and not _is_measurement_artifact(m)
    ]
    return result


def measure_surface(surface: Surface) -> dict:
    """Measure *surface* with its own options — the one way to measure a surface.

    Every caller goes through this rather than ``measure(surface.argv)``: a
    surface carries options (``bootstrap``, ``warm``) that a bare argv does
    not, and a caller that dropped one would measure a DIFFERENT surface than
    the one whose snapshot it then compares against. The script and
    ``tests/unit/import_budget/`` share this for the same reason they share
    :func:`check_surface`.

    ``surface_env`` is resolved ONCE here, so a warm surface's seed run and
    its measured run share the same ``OTTO_HOME`` — the seed's cache write is
    the state the measurement exists to observe. Two calls still get two
    homes, which is what keeps repeated measurements independent.
    """
    env = surface_env(surface)
    if surface.warm:
        # The seed. Same child, same repo, same home — so it performs exactly
        # the work the measured run would have had to, and leaves exactly the
        # cache the measured run reads. Its counts are discarded.
        measure(
            surface.argv,
            bootstrap=surface.bootstrap,
            real_entry=surface.real_entry,
            env=env,
        )
    return measure(
        surface.argv,
        bootstrap=surface.bootstrap,
        real_entry=surface.real_entry,
        env=env,
    )


def snapshot_path(key: str) -> Path:
    """Path to the golden snapshot file for surface *key*."""
    return SNAPSHOT_DIR / f"{key}.txt"


def write_snapshot(key: str, otto_modules: list[str]) -> None:
    """Write *otto_modules* as the golden snapshot for surface *key*."""
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    snapshot_path(key).write_text("\n".join(otto_modules) + "\n")


def read_snapshot(key: str) -> list[str]:
    """Return the otto-owned module list recorded in surface *key*'s golden snapshot."""
    return [ln for ln in snapshot_path(key).read_text().splitlines() if ln]


GATED_IO_COUNTERS = ("listdir", "open_fixture", "open_home", "scandir")
"""The I/O counters an I/O golden records and enforces EXACTLY.

Not ``open``. The whole-process open total drifts with the environment rather
than with otto — bytecode-cache state and the installed distribution set, both
measured, both explained at :data:`_CHILD_IO_PREAMBLE` — so an exact golden on
it would be a check that fails for reasons outside the change. These four do
not drift: the two ``os.*`` counters observe otto's own directory work,
``open_fixture`` observes its file reads inside the workspace under
measurement, and ``open_home`` the half of those that land in the user's home
— the term that dominates when ``$HOME`` is on a network filesystem, and the
one a fixture total cannot be read back apart into.
"""


def interpreter_tag() -> str:
    """Return the running interpreter's ``major.minor``, which keys an I/O golden.

    I/O counts are only comparable WITHIN one Python minor version: the
    interpreter's own import machinery is part of what the audit hook observes,
    and it changes release to release. Module counts do not need this — the
    non-stdlib set is identical across 3.10-3.14 by design — which is why the
    module snapshots stay version-free and only these are keyed.
    """
    return f"{sys.version_info.major}.{sys.version_info.minor}"


def io_snapshot_path(key: str) -> Path:
    """Path to the I/O golden for surface *key* on the RUNNING interpreter."""
    return SNAPSHOT_DIR / f"{key}.io.{interpreter_tag()}.txt"


def write_io_snapshot(key: str, io: dict) -> None:
    """Write the gated I/O counters for surface *key* on the running interpreter."""
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    header = (
        f"# I/O golden: surface `{key}`, CPython {interpreter_tag()}. "
        f"Regenerate with `make import-snapshot` UNDER THIS INTERPRETER.\n"
    )
    body = "".join(f"{name} {io[name]}\n" for name in GATED_IO_COUNTERS)
    io_snapshot_path(key).write_text(header + body)


def read_io_snapshot(key: str) -> dict[str, int]:
    """Return the recorded I/O counters for surface *key* on the running interpreter.

    Raises ``FileNotFoundError`` when this interpreter has no golden — which
    :func:`check_surface` turns into a NAMED failure rather than a skip.
    """
    recorded: dict[str, int] = {}
    for line in io_snapshot_path(key).read_text().splitlines():
        if not line or line.startswith("#"):
            continue
        name, _, value = line.partition(" ")
        recorded[name] = int(value)
    return recorded


def check_surface(surface: Surface, result: dict) -> list[str]:
    """Return human-readable import-budget violations for a surface (empty = pass).

    Runs the four checks the unit test enforces, so the script (`--check`) and
    `tests/unit/import_budget/` share one source of truth:
      1. denylist     — heavy third-party stacks must be absent
      2. count cap     — non-stdlib module count must not exceed surface.cap
      3. golden snapshot — the otto-owned module set must match exactly
      4. I/O golden     — the gated I/O counters must match exactly, against
         the golden for the RUNNING interpreter (see :func:`interpreter_tag`)
    """
    violations: list[str] = []

    leaked = [d for d in surface.deny if d in result["modules"]]
    if leaked:
        violations.append(f"`{surface.key}`: heavy modules leaked onto the path: {leaked}")

    if surface.cap is None:
        # A `real_entry` surface is capless ON PURPOSE until the fix it exists
        # to witness lands: its module set until then includes everything the
        # defect drags in, so a cap written earlier would enshrine the defect
        # as the budget. Task 4 landed `version_repo`'s (cap=4) and Task 7 the
        # help pair's, so NOTHING uses this branch any more — the countdown
        # reached zero and a unit test pins the used set as empty. The branch
        # itself stays: it is the mechanism a future repo-bearing surface
        # needs while its own fix is being written, and it is keyed on
        # `real_entry` rather than on a key list so it cannot widen silently.
        if not surface.real_entry:
            violations.append(f"`{surface.key}` has no cap set")
    else:
        non_stdlib = result["non_stdlib_modules"]
        if len(non_stdlib) > surface.cap:
            violations.append(
                f"`{surface.key}`: {len(non_stdlib)} non-stdlib modules > cap {surface.cap}. "
                f"If intentional, re-run `make import-snapshot` and raise the cap.\n"
                f"  non-stdlib modules: {non_stdlib}"
            )

    expected = read_snapshot(surface.key)
    if result["otto_modules"] != expected:
        violations.append(
            f"`{surface.key}`: otto module set changed. "
            f"If intentional, re-run `make import-snapshot` and review the diff.\n"
            f"  added:   {sorted(set(result['otto_modules']) - set(expected))}\n"
            f"  removed: {sorted(set(expected) - set(result['otto_modules']))}"
        )

    violations.extend(_check_io(surface, result))
    return violations


def _display_path(path: Path) -> str:
    """Render *path* for a failure message: repo-relative when it is inside the repo.

    A message formatter must never be the thing that raises. ``relative_to``
    throws on any path outside ``REPO_ROOT`` — which a redirected
    ``SNAPSHOT_DIR`` (a test driving the golden machinery against ``tmp_path``)
    or a symlinked checkout produces — turning a diagnosable budget failure
    into a ``ValueError`` traceback from inside the diagnosis.
    """
    try:
        return str(path.relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


def _check_io(surface: Surface, result: dict) -> list[str]:
    """Return I/O-golden violations for *surface* (empty = pass).

    A MISSING GOLDEN IS A FAILURE, NAMED — never a silent skip. The goldens
    are per interpreter minor, so "no file for this Python" is the shape a
    newly added interpreter takes, and a skip there would gate four of five
    CI legs while reporting green on all five. The message carries the three
    things needed to fix it: which file, which interpreter, and the command
    that writes it.
    """
    measured = {name: result["io"][name] for name in GATED_IO_COUNTERS}
    try:
        recorded = read_io_snapshot(surface.key)
    except FileNotFoundError:
        return [
            (
                f"`{surface.key}`: no I/O golden for CPython {interpreter_tag()} — "
                f"expected {_display_path(io_snapshot_path(surface.key))}. "
                f"Every interpreter that runs this check needs its own file (the counts "
                f"are not comparable across minors). Regenerate it by running "
                f"`make import-snapshot` UNDER CPython {interpreter_tag()} — e.g. "
                f"`uv run nox -s tests_hostless-{interpreter_tag()} --install-only` then "
                f"that session's python.\n"
                f"  measured now: {measured}"
            )
        ]
    if measured != recorded:
        drift = {
            name: (recorded.get(name), measured[name])
            for name in GATED_IO_COUNTERS
            if name in recorded and recorded[name] != measured[name]
        }
        # THE SHAPE OF THE FILE IS PART OF THE COMPARISON, NOT JUST ITS VALUES.
        # The equality above is over the whole parsed golden, so a file
        # carrying a key that is no longer gated (a renamed counter, a
        # hand-edit) is a mismatch with an EMPTY value diff — a red with no
        # diagnosis, which is the worst kind. Name the shape drift explicitly.
        missing = sorted(set(GATED_IO_COUNTERS) - set(recorded))
        unknown = sorted(set(recorded) - set(GATED_IO_COUNTERS))
        detail = f"  golden -> measured: {drift}\n" if drift else ""
        if missing:
            detail += f"  counters GATED but absent from the golden (regenerate it): {missing}\n"
        if unknown:
            detail += (
                f"  counters in the golden that are NOT gated — a stale or hand-edited "
                f"file; regenerate it: {unknown}\n"
            )
        # "counts changed" is the common case and the useful headline; a golden
        # whose SHAPE is wrong reports as a mismatch instead, because no count
        # changed.
        headline = "I/O counts changed" if drift else "I/O golden mismatch"
        return [
            (
                f"`{surface.key}`: {headline} on CPython {interpreter_tag()} "
                f"(golden {_display_path(io_snapshot_path(surface.key))}). "
                f"If intentional, re-run `make import-snapshot` under this interpreter "
                f"and review the diff.\n"
                f"{detail}"
                f"  gated counters measured: {measured}\n"
                f"  full io (open is context, not gated): {result['io']}"
            )
        ]
    return []


def _run_hyperfine(surface: Surface) -> None:
    hyperfine = shutil.which("hyperfine")
    if hyperfine is None:
        print("  (hyperfine not found — run `make hyperfine` to install it)")
        return
    venv_py = REPO_ROOT / ".venv" / "bin" / "python"
    if surface.argv[:1] == ["python"]:
        cmd = f'{venv_py} -c "import otto"'
    else:
        cmd = f"{REPO_ROOT / '.venv' / 'bin' / 'otto'} {' '.join(surface.argv[1:])}"
    subprocess.run(  # noqa: S603 (dev tool, resolved exe + fixed argv, no shell)
        [hyperfine, "--warmup", "5", "--min-runs", "20", "--shell=none", "--ignore-failure", cmd],
        check=False,
    )


def main() -> int:
    """Print the per-surface count table; optionally --update / --check / --hyperfine."""
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--update", action="store_true", help="regenerate golden snapshots")
    ap.add_argument(
        "--check",
        action="store_true",
        help="enforce the import budget (caps + snapshots + denylist); exit non-zero on violation",
    )
    ap.add_argument("--hyperfine", action="store_true", help="also show wall-clock stats (manual)")
    args = ap.parse_args()

    # flush=True so these lines interleave correctly with hyperfine's (unbuffered)
    # subprocess output when stdout is piped/redirected (e.g. `make profile > log`).
    print(
        f"{'surface':20} {'total':>6} {'non_std':>7} {'otto':>5} "
        f"{'open':>6} {'open_fx':>7} {'scandir':>7} {'listdir':>7}  heavy_present",
        flush=True,
    )
    failed = False
    for s in SURFACES:
        r = measure_surface(s)
        present = [d for d in s.deny if d in r["modules"]]
        non_std, otto = len(r["non_stdlib_modules"]), len(r["otto_modules"])
        io = r["io"]
        print(
            f"{s.key:20} {r['count']:6d} {non_std:7d} {otto:5d} "
            f"{io['open']:6d} {io['open_fixture']:7d} {io['scandir']:7d} {io['listdir']:7d}"
            f"  {present}",
            flush=True,
        )
        if args.update:
            write_snapshot(s.key, r["otto_modules"])
            write_io_snapshot(s.key, io)
            print(
                f"  -> wrote {snapshot_path(s.key).relative_to(REPO_ROOT)} "
                f"({len(r['otto_modules'])} modules) and "
                f"{io_snapshot_path(s.key).relative_to(REPO_ROOT)}",
                flush=True,
            )
        if args.check:
            violations = check_surface(s, r)
            for v in violations:
                print(f"  FAIL {v}", flush=True)
            failed = failed or bool(violations)
        if args.hyperfine:
            _run_hyperfine(s)
    if failed:
        print("\nimport budget: FAILED — see FAIL lines above.", flush=True)
        return 1
    if args.check:
        print("\nimport budget: OK", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
