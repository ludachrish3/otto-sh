"""Measure otto's file operations per CLI surface — deterministic, host-independent.

The metric is *file operations*: every path syscall (the stat family, opens,
directory listings, link reads, access checks) across the command's WHOLE
process tree, counted from outside by strace. Never wall-clock. Each surface is
measured in a fresh subprocess with a sanitized env (all OTTO_* vars stripped)
so the count reflects otto-core only, regardless of the dev's labs / SUT dirs.
The one thing put BACK is a throwaway ``OTTO_HOME``, on every surface: the
runner's real ``~/.otto`` is machine state (see :func:`surface_env`).

Wall-clock cannot gate, because it fails for reasons outside the change; file
operations repeat run to run. They are also what a network filesystem charges
for: syscall counts reproduced a real NFS deployment's cold `otto --version` to
the one significant figure that field observation carries (2,427 syscalls x
1.2 ms RTT ~ 2.9 s against an observed ~3 s), where a dev-box wall-clock number
predicted nothing about that machine at all. See
docs/architecture/startup-performance.md.

EVERY GATED COUNTER IS A CEILING: a measured baseline plus headroom
(:func:`ceiling`). Two counters per surface, ``file_ops`` (the whole tree) and
``workspace`` (the subset under the generated repo and ``OTTO_HOME``). Growing
past a ceiling fails, with the growth broken down by package and by child
process (:func:`breakdown_diff`); shrinking never fails, and a counter far below
its ceiling earns an advisory NOTE (:func:`advisories`). A tracked surface is
measured and printed, never enforced.

THE RUN, HOST AND TEST SURFACES ALSO CARRY A TARGET: a ceiling on their
``file_ops`` as a multiple of ``otto --version``'s (``version_repo``) measured
in the same run (:attr:`Surface.target_ratio`, :func:`check_ratio`). A ceiling
pins today's cost; the ratio says how far above an interpreter start plus the
shim a command may sit, and it survives a dependency update that makes every
import dearer, because both sides move together.

The baselines live in one file per Python minor
(``tests/unit/import_budget/ceilings/<major.minor>.json``), because each
interpreter's stdlib and import machinery differ, and are checked against the
RUNNING interpreter's file; a missing baseline is a named failure, never a
skip. `--update` regenerates only the running interpreter's file.

The ``real_entry`` surfaces run the console entry path against a GENERATED
repo, because startup I/O against an empty workspace is zero and therefore
unmeasurable. They stay host-independent the same way — the harness creates
what it measures.

Usage:
    python scripts/import_budget.py            # print the per-surface table
    python scripts/import_budget.py --check    # enforce the ceilings; exit non-zero on a breach
    python scripts/import_budget.py --update   # rewrite this interpreter's ceilings file
    python scripts/import_budget.py --report   # the table (an alias of no flag)
    python scripts/import_budget.py --report-json PATH   # the same, with full breakdowns, as JSON
"""

import argparse
import atexit
import errno
import functools
import json
import os
import pty
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CEILINGS_DIR = REPO_ROOT / "tests" / "unit" / "import_budget" / "ceilings"

HEADROOM = 0.10
"""How far above its baseline a gated counter may grow before the gate fails.

Ten percent is below what an unneeded heavy dependency costs (pulling
``asyncssh`` and ``cryptography`` into a command is ~600 file operations on a
~2,000 surface) and above what a dependency's patch release or a small otto
change costs. A surface may widen its own (:attr:`Surface.headroom`), with a
comment saying why."""

MIN_SLACK = 5
"""The absolute floor under :data:`HEADROOM`, for the small counters.

Ten percent of a ``workspace`` count of 3 is nothing, and the import system's
directory-listing wobble (a ``FileFinder`` re-listing a directory whose mtime a
sibling process moved: #360, #361, #428) is worth one or two operations on any
surface. Without a floor that wobble alone would trip a low counter."""

RATIO_FLOOR = "version_repo"
"""The surface every :attr:`Surface.target_ratio` divides by: ``otto --version``.

The shim answers it without importing the CLI, so it is an interpreter start
plus ``otto._shim`` and ``otto.version`` against the same generated repo: the
least any command can cost."""

STALE_RATIO = 0.8
"""Below this fraction of its ceiling, a counter earns an advisory NOTE, never a failure.

A ceiling is only as tight as its baseline: one recorded before a real
reduction keeps the old, higher number, and every later regression up to it
passes unseen. Tightening it is a regeneration, which is a human's call, not
the gate's."""


def ceiling(baseline: int, headroom: float) -> int:
    """Return the most a counter with *baseline* may measure: *headroom* or MIN_SLACK more."""
    return max(int(baseline * (1 + headroom)), baseline + MIN_SLACK)


@dataclass(frozen=True)
class Surface:
    """One measured CLI surface: its argv, how it is run, and whether its ceilings gate."""

    key: str
    argv: list[str]
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
    walk is visible only per directory."""

    real_entry: bool = False
    """Run the console-script entry path instead of resolving a dispatch
    target. Required to observe bootstrap and cache behaviour, and it renders
    help the way a user sees it, which the dispatch-only surfaces skip on
    purpose."""

    warm: bool = False
    """Measure the SECOND run against one repo and one ``OTTO_HOME``.

    Every measurement is otherwise COLD by construction: ``surface_env``
    mints a fresh ``OTTO_HOME`` per call so repeated measurements of one
    surface stay independent. That is right for the fallback path and wrong
    for the cached one — a cold ``--help`` MUST take the full load (miss →
    full bootstrap → collect → write), because cache-or-load never degrades
    help.
    A warm surface therefore runs the child TWICE against the same generated
    repo and the same home: a discarded seed run, then the measured one.

    The seed writes ONLY into ``OTTO_HOME`` (the cache file). Nothing is
    written into the fixture tree — ``PYTHONDONTWRITEBYTECODE`` still keeps
    ``__pycache__`` out of it — so the pair stays deterministic and repeated
    warm measurements read an identical ``workspace`` count. Not an identical
    ``file_ops`` total: that counter also carries the state of a bytecode
    cache this harness neither owns nor can quiesce, so it moves between two
    measurements of one surface whenever another process on the machine
    imports the same module. That is what the headroom absorbs.
    """

    seed_argv: list[str] | None = None
    """Argv the seed run uses instead of ``argv``, when the measured argv itself
    never reads or writes the cache.

    ``None`` means ``argv`` seeds itself: root help and completion are cache
    readers, so their own first run already takes the miss → full bootstrap →
    collect → write path and leaves a valid cache for the second, measured run
    to find. An ordinary dispatch is not a reader (``entry()``'s
    ``reads_cache`` is only ever true for those two), so seeding a dispatch
    with its own argv leaves ``OTTO_HOME`` empty both runs, and a measured run
    that ignores the cache would do so for the wrong reason: there is nothing
    to open, not that dispatch declines to open it. Seeding with root
    ``otto --help`` instead puts a real cache beside the measured run.
    ``otto test`` is the exception among the verbs: it reads and writes the
    collected-tests tables (never a section), so ``test_repo`` names its own
    argv here, and its seed leaves exactly the table a repeated run finds.
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

    tracked: bool = False
    """Measured and printed, never enforced: the tier for a verb nobody has optimized yet.

    A tracked surface becomes gated when someone optimizes it and wants the
    win pinned. Its key starts with ``tracked_``, and it has no baseline in
    the ceilings files."""

    expect_exit: int = 0
    """The exit code a real-entry run must end with.

    Zero for every surface but the ones whose command is a failure by
    construction: the closed-port SSH surface measures a connection that is
    refused at once, because that is the SSH path with no real host."""

    pty_input: str | None = None
    """When set, the child runs under a pseudo-terminal and this text is written to it.

    For a verb that bridges the user's terminal (``host login``): with a pipe
    on stdin it would measure a different code path, or refuse to start."""

    expect_loaded: str | None = None
    """A module a real-entry run must have imported, if any.

    ``expect_exit`` alone cannot tell a refused connection from a command that
    never reached the network: an unknown host id also exits 1, with the same
    one-line error and no exception. A surface that fails by construction
    names a module only the path it measures imports, so a run that failed
    earlier cannot pass as the surface with a smaller count."""

    ssh_lab: bool = False
    """Generate the repo with a second lab source holding one SSH host on a closed port.

    See ``generate_repo``'s ``ssh_lab_port``. The harness picks the port and
    proves it refuses a connection before every measured run, so the surface
    measures one refused attempt rather than whatever a stray listener says.
    """

    headroom: float = HEADROOM
    """How far above its baseline this surface's counters may grow (see :func:`ceiling`).

    Widen it only with a comment on the surface saying why; the default is
    what every surface should be able to hold."""

    target_ratio: float | None = None
    """The most this surface's ``file_ops`` may be, as a multiple of :data:`RATIO_FLOOR`'s.

    ``None`` sets no target. Each value is derived from the LARGEST ratio
    measured across the supported CPython minors (3.10 to 3.14) when it was
    set, rounded UP to one decimal, plus 10%, kept to two decimals: a largest
    measured 4.003 becomes 4.1, then 4.51. One target serves every interpreter
    because both sides of the ratio are measured in the same run
    (:func:`check_ratio`); the largest ratio is the one the target must admit.

    The SSH surfaces' ratios include the child processes asyncssh's import
    starts to probe for liboqs (``ldconfig``, ``gcc``, ``ld``), measured on
    the aarch64 dev VM. Another architecture's toolchain may cost a different
    amount; if CI trips on those surfaces alone, re-derive their targets from
    CI's own measurement."""


def surface_by_key(key: str) -> Surface:
    """Return the surface named *key*. Never index SURFACES positionally."""
    for surface in SURFACES:
        if surface.key == key:
            return surface
    raise KeyError(f"no surface named {key!r}")


def _verb_surface(
    key: str,
    argv: list[str],
    *,
    tracked: bool = False,
    expect_exit: int = 0,
    pty_input: str | None = None,
    ssh_lab: bool = False,
    expect_loaded: str | None = None,
    target_ratio: float | None = None,
    seed_argv: list[str] | None = None,
) -> Surface:
    """Build a surface measuring one real command, the way a user runs it day to day.

    Every verb surface shares one shape, so the numbers compare across verbs:
    the real console entry against a realistic generated repo, WARM (seeded
    with root help, which writes the cache an ordinary command then finds
    beside it, as ``dispatch_repo_warm`` explains), with ``OTTO_LAB`` naming
    the JSON lab the repo declares so no command needs a real host.
    *seed_argv* replaces root help for a verb whose own warm state is
    something else (``test_repo``).
    """
    return Surface(
        key,
        argv,
        sut_files=50,
        sut_dirs_count=5,
        real_entry=True,
        warm=True,
        seed_argv=seed_argv or ["otto", "--help"],
        env_extra=(("OTTO_LAB", "unix"),),
        tracked=tracked,
        expect_exit=expect_exit,
        pty_input=pty_input,
        ssh_lab=ssh_lab,
        expect_loaded=expect_loaded,
        target_ratio=target_ratio,
    )


_TRACKED_VERBS = [
    "init",
    "env",
    "cache",
    "docker",
    "link",
    "tunnel",
    "monitor",
    "cov",
    "reservation",
    "inventory",
    "schema",
]
"""The top-level verbs with no gated surface of their own; each is measured at ``--help``."""


SURFACES: list[Surface] = [
    Surface("import_otto", ["python"]),  # bare `import otto`: the lazy package init
    # The `--help` surfaces resolve their dispatch target through the root
    # group WITHOUT running Click's help rendering (see `_CHILD_CLI_BODY`).
    Surface("help", ["otto", "--help"]),
    Surface("run", ["otto", "run", "--help"]),
    Surface("host", ["otto", "host", "--help"]),
    Surface("reservation", ["otto", "reservation", "--help"]),
    Surface("docker", ["otto", "docker", "--help"]),
    Surface("schema", ["otto", "schema", "--help"]),
    Surface("monitor", ["otto", "monitor", "--help"]),
    Surface("test", ["otto", "test", "--help"]),
    Surface("cov", ["otto", "cov", "--help"]),
    # THE COMPOSITION ROOT IS ON THE PATH OF EVERY REAL INVOCATION, and until
    # this surface existed nothing measured it: every surface above resolves a
    # dispatch target through the root group WITHOUT calling `bootstrap()`, so
    # when bootstrap grew an import (it imports `otto.project.actions` to
    # register the first-party `otto run` verbs, which pulls otto.project and
    # its dependents) the guard measured none of it and stayed green.
    #
    # Deliberately a SECOND surface over the same argv as `run` rather than a
    # change to that one: the pair is the measurement. `run` keeps reporting
    # what lazy dispatch costs by itself (what the completion fast path pays,
    # which never bootstraps), and the DIFFERENCE between the two is the
    # composition root's own footprint. `run` is the argv because the verbs
    # bootstrap registers are `otto run`'s.
    Surface("run_bootstrapped", ["otto", "run", "--help"], bootstrap=True),
    # EVERY SURFACE ABOVE MEASURES AN EMPTY WORKSPACE. The sanitized env strips
    # OTTO_*, so `sut_dirs` is empty, discovery finds zero repos, and a walk of
    # the workspace costs nothing on any of them. A guard that cannot observe a
    # defect cannot witness its fix either.
    #
    # The surfaces below carry a GENERATED repo (deterministic by
    # construction, so they stay as host-independent as the rest of the table)
    # and — `bootstrap_repo`, which drives the composition root directly, aside
    # — run the REAL entry path, so they observe bootstrap and cache
    # behaviour no other surface can see.
    #
    # `--version` is answered by `otto._shim` without importing the CLI, so
    # this surface is the floor every other command is compared against: an
    # interpreter start plus the shim and `otto.version`.
    Surface("version_repo", ["otto", "--version"], sut_files=50, sut_dirs_count=5, real_entry=True),
    # THE HELP PAIR. `help_repo` is COLD — a fresh OTTO_HOME per measurement,
    # so it is the fallback path: cache miss → full bootstrap → collect →
    # write, which is what cache-or-load promises and must keep costing what
    # a complete answer costs. It is therefore also the surface that PROVES
    # THE HARNESS still finds a real repo (its `workspace` count clears a
    # repo-less run of the same command by every `names` key path), which is
    # why it stays in the table unchanged.
    #
    # `help_repo_warm` is the same surface measured on its SECOND run against
    # one home, i.e. the cached path: root help resolves the command list
    # from the `names` section and never walks the corpus. The scaling test
    # keys on this one.
    Surface("help_repo", ["otto", "--help"], sut_files=50, sut_dirs_count=5, real_entry=True),
    Surface(
        "help_repo_warm",
        ["otto", "--help"],
        sut_files=50,
        sut_dirs_count=5,
        real_entry=True,
        warm=True,
    ),
    # `run_bootstrapped`'s REPO-BEARING SIBLING, and the pair is again the
    # measurement. That surface runs the composition root against an EMPTY
    # workspace — zero repos discovered — which is what isolates bootstrap's
    # own import graph from anything a workspace drags in, and it must keep
    # doing exactly that. This one runs the same root against a generated
    # repo, so the work bootstrap does PER REPO (reading the repo's settings,
    # putting its lib dirs on the path, importing its init tree) is charged to
    # a surface. Same argv for the same reason the original chose it: the
    # verbs bootstrap registers are `otto run`'s.
    Surface(
        "bootstrap_repo",
        ["otto", "run", "--help"],
        bootstrap=True,
        sut_files=50,
        sut_dirs_count=5,
    ),
    # THE COMMON CASE: a real command, dispatched through the real entry with
    # a warm cache. Every other real-entry surface above is `--version`, root
    # help or a TAB, so until this existed nothing measured what `otto run Y`
    # pays before its own work starts — which is where a completion-cache
    # check and every repo's test-file import were hiding. `-R` skips the
    # reservation check and OTTO_LAB names a JSON lab the generated repo
    # declares, so the run contacts no host.
    #
    # `seed_argv=root --help`, NOT this surface's own argv: see
    # `Surface.seed_argv`. Root help is a real cache reader/writer, so it
    # leaves a real cache behind, and the measured run shows dispatch
    # ignoring a cache that is really there.
    Surface(
        "dispatch_repo_warm",
        ["otto", "-R", "run", "noop"],
        sut_files=50,
        sut_dirs_count=5,
        real_entry=True,
        warm=True,
        seed_argv=["otto", "--help"],
        env_extra=(("OTTO_LAB", "unix"),),
        target_ratio=4.4,
    ),
    # THE STEADY-STATE TAB COST. Completion is the surface a user hits most
    # often and notices most sharply, and it is a MODE rather than an argv:
    # the shell runs the bare console script with click's `_OTTO_COMPLETE`
    # protocol in the environment (see `Surface.env_extra`), which is why no
    # `argv`-only surface could reach it.
    #
    # WARM, like `help_repo_warm` and for the same reason: a cold cache means
    # a miss, and a miss is a full bootstrap by design — completion never
    # degrades to a wrong answer. The seed run leaves the `shim` section, and
    # the measured run is the shim's stat pass plus one JSON read: `otto._shim`
    # answers the TAB from the cache without ever importing `otto.cli`,
    # `otto.config`, typer, click, or rich.
    Surface(
        "completion_repo_warm",
        ["otto"],
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
    # `live` site: the resolver hands over rather than answering, and the shim
    # falls through to the unchanged full CLI path. This is a DIFFERENT site
    # than `completion_repo_warm`'s — that one completes top-level command
    # NAMES (COMP_CWORD=1), which never resolves a specific command's module;
    # this one resolves three levels deep into `tunnel remove`'s own argument
    # completer, which imports `otto.cli.tunnel` and, transitively, all of
    # `otto.tunnel`, `otto.project`, `otto.instructions`, `otto.host.daemon`,
    # and `otto.config.fleet`/`lab`/`dependencies` — modules the top-level
    # site never touches. So "as cheap as the warm path" was never the bar
    # here; the shim's own part is a cache read, the stat pass, and a marker
    # touch, which always happen before it can decide to hand over.
    Surface(
        "completion_repo_handover",
        ["otto"],
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
    # THE COMMANDS A USER RUNS. The surfaces above measure `--version`, help,
    # completion and one dispatch; nothing measured what `otto host X exec`
    # or `otto test` costs before its own work starts, which is where each
    # verb paid for every other verb's imports.
    #
    # `{root}` in an argv is the surface's fixture root, filled in at
    # measurement time. A surface that names it gets the transfer fixture
    # there: a one-line `payload.txt` and EMPTY `put-dest/` and `get-dest/`,
    # reset before every run so each measurement transfers into a directory
    # that has never held the file.
    #
    # The one verb that must import pytest. Its warm state is a repeated
    # run: the seed is the same `otto test TestTop0`, which writes the repo's
    # collected-tests table, so the measured run finds it current. Its
    # session collects the whole tree all the same, as every run does: the
    # table is a hint and never narrows a run (#592). Seeded with root help
    # instead, it would measure the first run in a fresh home, which also
    # seeds the table. 11.66: the whole-tree session's ratio on 3.10 (11.10x,
    # the highest minor; 9.98x on 3.14) plus 5%, as 9.46 was for the session
    # the table used to narrow to one file. The cost of a verdict pytest
    # would agree with (#592).
    _verb_surface(
        "test_repo",
        ["otto", "test", "TestTop0"],
        target_ratio=11.66,
        seed_argv=["otto", "test", "TestTop0"],
    ),
    # The built-in `local` host stands in for the ~30 host subverbs that run a
    # shell command: every one is a `@cli_exposed` method on the same class.
    _verb_surface("host_local_exec", ["otto", "host", "local", "exec", "true"], target_ratio=4.4),
    _verb_surface(
        "host_local_put",
        ["otto", "host", "local", "put", "{root}/payload.txt", "{root}/put-dest"],
        target_ratio=4.51,
    ),
    _verb_surface(
        "host_local_get",
        ["otto", "host", "local", "get", "{root}/payload.txt", "{root}/get-dest"],
        target_ratio=4.51,
    ),
    # THE SSH PATH WITH NO REAL HOST: `budget-ssh` is `127.0.0.1` on a port
    # the harness proves closed, so asyncssh's connect is refused at once.
    # otto makes one attempt (no retry to disable), and the verb prints one
    # line naming the host and exits 1. `expect_loaded` pins WHICH failure: an
    # unknown host id exits 1 as well, but never reaches asyncssh.
    _verb_surface(
        "host_ssh_exec",
        ["otto", "host", "budget-ssh", "exec", "true"],
        ssh_lab=True,
        expect_exit=1,
        expect_loaded="asyncssh.connection",
        # Re-derived once the refusal stopped rendering a traceback: the
        # largest ratio was 6.978 (3.11), so 7.0 plus 10%.
        target_ratio=7.7,
    ),
    # THE LOGIN PATH, on the same closed-port host, under a pty as a user's
    # terminal would be. Not the built-in `local` host: `LocalHost` implements
    # no interactive session, so its `login` refuses before connecting.
    # `budget-ssh` takes `UnixHost._login`'s SSH branch and is refused at the
    # connect that precedes the terminal bridge (`interact.run_ssh_login`
    # needs the connection), so the bridge module is counted as far as the
    # host module imports it, and the bridge itself never runs. It ends as
    # `host_ssh_exec` does.
    _verb_surface(
        "host_ssh_login",
        ["otto", "host", "budget-ssh", "login"],
        pty_input="exit\n",
        ssh_lab=True,
        expect_exit=1,
        expect_loaded="asyncssh.connection",
        target_ratio=7.7,
    ),
    # `dispatch_repo_warm`'s own sibling, dispatching `local_true` instead of
    # `noop`: `local_true` opens a persistent `LocalHost` session and runs one
    # command through it (tests/_fixtures/generated_repo.py), so this surface
    # is the one that can see what a session's own connection-lost choke
    # points cost. `dispatch_repo_warm` never opens a host session at all, so
    # it never could.
    # Typer renders an underscored Python name with hyphens on the command
    # line ("local_true" -> "local-true"); the argv must say what a user
    # would actually type.
    _verb_surface("dispatch_local_warm", ["otto", "-R", "run", "local-true"], target_ratio=4.51),
    # TRACKED: measured and printed, never enforced.
    *(
        _verb_surface(f"tracked_{verb}", ["otto", verb, "--help"], tracked=True)
        for verb in _TRACKED_VERBS
    ),
    # `--help`, not the real verb: a real probe or power cycle of the local
    # host measures the machine it runs on, not otto.
    _verb_surface("tracked_host_probe", ["otto", "host", "local", "probe", "--help"], tracked=True),
    _verb_surface("tracked_host_power", ["otto", "host", "local", "power", "--help"], tracked=True),
]

# The payload still carries the module inventory (`modules`, `otto_modules`,
# `non_stdlib_modules`): nothing gates on it, but a failure is easier to read
# with it, and tests that ask "is this module loaded on this path" read it.
# `non_stdlib_modules` is total sys.modules minus the stdlib, classified via
# the *child's own* sys.stdlib_module_names, so each Python version
# self-classifies.

# Prepended to every child. It installs nothing: every file operation is
# counted from OUTSIDE the child, by strace, so the child must do no path work
# of its own beyond what otto does. That is why the fixture root is used as the
# raw string the harness passed in, never resolved: a `realpath` here would
# charge one `lstat` per path component to every surface.
_CHILD_PREAMBLE = """
import os as _os
import sys as _sys
_fixture_root = _os.environ.get("IMPORT_BUDGET_FIXTURE_ROOT")
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
                  "non_stdlib_modules": non_std,
                  "sys_path_len": len(sys.path),
                  "fixture_path_entries": _fixture_path_entries()}))
"""
_CHILD_IMPORT = _CHILD_PREAMBLE + _CHILD_IMPORT_BODY

# Child script for CLI surfaces: resolve the dispatch target through the
# registry-backed root group (otto.cli.main._OttoGroup.get_command) the same
# way a real `--help`/completion invocation would, without running Click's
# help-rendering pipeline (which would additionally pull in rich's markdown
# renderer, pygments, etc. — a measurement artifact unrelated to otto's own
# lazy-import footprint). Every surface's argv is `[..., "<name>", "--help"]`
# except the bare `help` surface (`["otto", "--help"]`, no dispatch target),
# which instead lists the root's commands: the walk a real `otto --help` makes
# to fill its command table (the command registry, the completion cache's
# names), still without rendering it.
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
if {bootstrap!r}:
    # Mirror otto._shim.main: the real command sets this before any model
    # builds, so pydantic's plugin scan is not a cost a user pays.
    import os
    os.environ.setdefault("PYDANTIC_DISABLE_PLUGINS", "__all__")
import otto
if {bootstrap!r}:
    from otto import bootstrap as _bs
    _bs.bootstrap()
cmd = typer.main.get_command(otto.app)
ctx = cmd.make_context("otto", sys.argv[1:], resilient_parsing=True)
target = sys.argv[1] if len(sys.argv) > 1 and not sys.argv[1].startswith("-") else None
if target is not None:
    _ = cmd.get_command(ctx, target)
else:
    _ = cmd.list_commands(ctx)
mods = sorted(sys.modules)
otto_mods = [m for m in mods if m == "otto" or m.startswith("otto.")]
non_std = [m for m in mods if m.split(".")[0] not in sys.stdlib_module_names]
print(json.dumps({{"count": len(mods), "modules": mods, "otto_modules": otto_mods,
                   "non_stdlib_modules": non_std,
                   "sys_path_len": len(sys.path),
                   "fixture_path_entries": _fixture_path_entries()}}))
"""
_CHILD_CLI = _CHILD_PREAMBLE.replace("{", "{{").replace("}", "}}") + _CHILD_CLI_BODY

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
# produced it. `Repo.add_libs_to_pythonpath` prepends each discovered repo's
# lib dirs, and every later import probe pays for every entry, so the tests
# read `fixture_path_entries`: the entries under the FIXTURE ROOT only, because
# the total length moves with editable-vs-wheel installs, layout changes, and
# any dev dependency shipping a `.pth`. `sys_path_len` is context for a
# failure, never a threshold.
#
# THE PAYLOAD IS WRITTEN TO A FILE, NEVER PRINTED. A real dispatch is the
# first child that runs otto's own business logic, and that logic logs: `otto
# -R run <x>` warns that the reservation check was skipped, through the same
# `CONSOLE` (stdout) `_print_output_dir`'s atexit hook also writes to. `print`
# performs its content and its trailing newline as TWO separate underlying
# writes, so a warning rendered between them lands mid-line, with no newline
# to split on — measured: `'{{"count": 877, ... "exit_code": 0}}WARN
# Reservation check skipped ...'` as ONE unparsable `_run_child` line, only on
# the surface that actually dispatches. Writing to a private file otto never
# touches removes the shared channel instead of racing to stay ahead of it —
# unconditionally, since this is the only child kind that can log mid-payload.
#
# AN EXCEPTION THAT ESCAPES THE ENTRY ENDS THE RUN AS THE INTERPRETER WOULD:
# `sys.excepthook` renders it (otto installs rich's, which reads source lines,
# and that reading is part of what the user's failed command costs), and the
# exit status is 1. Without the arm the child would die before writing its
# payload, and a surface that regressed into a traceback would report nothing
# at all rather than the exception that escaped.
#
# THE ESCAPING EXCEPTION RIDES THE PAYLOAD (`exception`, "<module>.<qualname>:
# <message>", or null), because exit status 1 alone names no cause: an unknown
# host id exits 1 too, through `typer.Exit`, a `SystemExit` that is no
# exception here. `check_exit` fails any run one escapes, and a failure
# message quotes it, since the traceback itself went to a stderr nobody keeps.
_CHILD_ENTRY_BODY = """
import sys, json
sys.argv = {argv!r}
_result_path = _os.environ["IMPORT_BUDGET_RESULT_PATH"]
from otto._shim import main as _console_entry
_exit_code = 0
_exception = None
try:
    _console_entry()
except SystemExit as _e:
    _exit_code = _e.code if isinstance(_e.code, int) else (0 if _e.code is None else 1)
except BaseException as _e:
    sys.excepthook(*sys.exc_info())
    _exit_code = 1
    _exception = f"{{type(_e).__module__}}.{{type(_e).__qualname__}}: {{_e}}"
mods = sorted(sys.modules)
otto_mods = [m for m in mods if m == "otto" or m.startswith("otto.")]
non_std = [m for m in mods if m.split(".")[0] not in sys.stdlib_module_names]
_payload = json.dumps({{"count": len(mods), "modules": mods, "otto_modules": otto_mods,
                   "non_stdlib_modules": non_std,
                   "sys_path_len": len(sys.path),
                   "fixture_path_entries": _fixture_path_entries(),
                   "exit_code": _exit_code, "exception": _exception}})
with open(_result_path, "w") as _f:
    _f.write(_payload)
"""
_CHILD_ENTRY = _CHILD_PREAMBLE.replace("{", "{{").replace("}", "}}") + _CHILD_ENTRY_BODY


# Child script measuring the empty baseline: what a bare interpreter already has
# in sys.modules before a single line of otto runs.
_CHILD_BASELINE = """
import sys, json
print(json.dumps([m for m in sorted(sys.modules)
                  if m.split(".")[0] not in sys.stdlib_module_names]))
"""


def _sanitized_env() -> dict[str, str]:
    """Env with all OTTO_* vars stripped, so measurement is lab/host independent."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("OTTO_")}
    # The measured command must set this itself, as the `otto` command does.
    env.pop("PYDANTIC_DISABLE_PLUGINS", None)
    return env


_STRACE_CALL = re.compile(r"^(\d+)\s+([a-z_0-9]+)\((.*)$")
_FIRST_QUOTED = re.compile(r'"((?:[^"\\]|\\.)*)"')


def strace_executable() -> str:
    """Return the strace binary, or FAIL naming how to install it — never skip.

    strace is the only view this guard has of file operations, which are what
    a network filesystem charges for (CPython's audit hooks see no stat at
    all). A guard that quietly measured less on a machine without strace would
    report green for exactly the regressions it exists to catch.
    """
    exe = shutil.which("strace")
    if exe is None:
        raise RuntimeError(
            "the import budget needs strace to count file operations; install it "
            "(Debian/Ubuntu: `sudo apt-get install strace`)"
        )
    return exe


FILE_OP_CALLS = frozenset(
    {
        # The stat family and the access checks: the lookups an import makes.
        "stat",
        "lstat",
        "newfstatat",
        "fstatat64",
        "stat64",
        "lstat64",
        "oldstat",
        "oldlstat",
        "statx",
        "statfs",
        "statfs64",
        "access",
        "faccessat",
        "faccessat2",
        # Opens, listings and link reads.
        "open",
        "openat",
        "openat2",
        "creat",
        "getdents64",
        "readlink",
        "readlinkat",
        "getcwd",
        "chdir",
        "chroot",
        # Process images: a child process costs its own exec lookups.
        "execve",
        "execveat",
        "uselib",
        # Writes to the namespace.
        "mkdir",
        "mkdirat",
        "mknod",
        "mknodat",
        "rmdir",
        "unlink",
        "unlinkat",
        "rename",
        "renameat",
        "renameat2",
        "link",
        "linkat",
        "symlink",
        "symlinkat",
        "truncate",
        "truncate64",
        # Metadata writes.
        "chmod",
        "fchmodat",
        "fchmodat2",
        "chown",
        "chown32",
        "lchown",
        "lchown32",
        "fchownat",
        "utime",
        "utimes",
        "utimensat",
        "futimesat",
        "setxattr",
        "lsetxattr",
        "getxattr",
        "lgetxattr",
        "listxattr",
        "llistxattr",
        "removexattr",
        "lremovexattr",
        "setxattrat",
        "getxattrat",
        "listxattrat",
        "removexattrat",
        # Watches, handles and mounts: never on a measured path today, but the
        # filter emits them, so the set names them.
        "inotify_add_watch",
        "fanotify_mark",
        "name_to_handle_at",
        "mount",
        "umount",
        "umount2",
        "pivot_root",
        "open_tree",
        "move_mount",
        "fspick",
        "mount_setattr",
        "swapon",
        "swapoff",
        "acct",
        "quotactl",
    }
)
"""Every syscall name the harness's strace filter, ``trace=%file,getdents64``, can emit.

``%file`` is strace's class of syscalls that take a file name; ``getdents64``
is added because a directory listing is a network round trip too, and it
takes an fd, not a name. :func:`parse_file_ops` counts EVERY call line the
trace holds rather than filtering on this set, so a syscall a newer strace
adds to the class is counted before anyone lists it here: a ceiling must
never undercount. The set is the names a reader of a breakdown can expect.
"""

EXCLUDED_ROOTS: list[str] = ["/proc/", "/sys/", "/dev/"]
"""The kernel's virtual filesystems, the only paths a file-op count leaves out.

They are never on a network filesystem, whatever the user's mounts: the
kernel answers them locally. Every other path counts, because the harness
cannot know which of a user's mounts are remote, and for the NFS user this
metric exists for, the venv and the interpreter's stdlib are both remote."""


@dataclass
class PathBuckets:
    """Where the paths of one measured interpreter live, for grouping a file-op count.

    Every root is a directory prefix WITH a trailing ``/``, so a prefix match
    cannot confuse ``site-packages`` with ``site-packages-old``; a list root
    may carry both a raw and a resolved spelling, as ``_workspace_prefixes``
    does. The paths come from the measured interpreter
    (:func:`_interpreter_paths`), not from whichever one runs the harness.
    """

    site_packages: list[str]
    stdlib: list[str]
    otto_src: str
    workspace: list[str]

    def bucket(self, path: str) -> str:
        """Return the group *path* is charged to, checking the roots in a fixed order.

        The order is load-bearing, because the roots nest. The workspace is
        first because it is what otto itself reads. ``otto_src`` comes before
        ``site_packages`` because a wheel install puts otto inside
        site-packages. ``site_packages`` comes before ``stdlib`` because a
        system interpreter keeps its site-packages under its stdlib.

        A root directory ITSELF belongs to its root: the import system stats
        each ``sys.path`` directory once per import to check its mtime, and
        that is the stdlib's, otto's or site-packages' cost, not "other".
        Measured on ``version_repo``, 72 of its ~700 operations are stats of
        the stdlib directory alone. Such a stat names no module, so under
        ``otto`` and ``site`` it is charged to the pseudo-name ``*``.
        """
        if _root_of(path, self.workspace) is not None:
            return "workspace"
        if _root_of(path, [self.otto_src]) is not None:
            return "otto:" + _first_component(path, self.otto_src).removesuffix(".py")
        root = _root_of(path, self.site_packages)
        if root is not None:
            return "site:" + _import_name(_first_component(path, root))
        if _root_of(path, self.stdlib) is not None:
            return "stdlib"
        return "other"


def _root_of(path: str, roots: list[str]) -> str | None:
    """Return the first of *roots* (each ending in ``/``) that *path* is, or lies under."""
    for root in roots:
        if path.startswith(root) or path == root.rstrip("/"):
            return root
    return None


def _first_component(path: str, root: str) -> str:
    """Return *path*'s first component below *root* (ending in ``/``), or ``*`` for the root."""
    return path[len(root) :].split("/", 1)[0] or "*"


def _import_name(entry: str) -> str:
    """Reduce a site-packages entry to the import name it belongs to.

    A breakdown groups by what a reader would ``import``: a package's
    ``*.dist-info`` metadata directory, its compiled extension and its
    ``*.libs`` bundle are all the same dependency's cost.
    ``pydantic_core-2.33.dist-info`` and ``_cffi_backend.cpython-310-...so``
    reduce to ``pydantic_core`` and ``_cffi_backend``. A ``__pycache__``
    directory stays as it is.
    """
    if entry.endswith((".dist-info", ".egg-info")):
        return entry.split("-", 1)[0]
    return entry.split(".", 1)[0] or entry


@dataclass
class FileOps:
    """One command's file operations: the total, the workspace subset, and two breakdowns."""

    total: int = 0
    workspace: int = 0
    by_bucket: dict[str, int] = field(default_factory=dict)
    """Calls per :meth:`PathBuckets.bucket` group, plus ``fd`` for the calls that
    name no path (a listing, or a stat relative to an open directory)."""
    by_process: dict[str, int] = field(default_factory=dict)
    """Calls per program, keyed by the basename a child ``execve``d (``python``
    for the measured interpreter), summed over every tid running it."""


def parse_file_ops(
    text: str, *, workspace_prefixes: list[str], buckets: PathBuckets, bytecode_prefix: str = ""
) -> FileOps:
    """Count every file operation in strace ``-f -o`` output, over the WHOLE process tree.

    Child processes count, deliberately: a program otto starts (``gcc``
    behind ``ctypes.util.find_library``, ``git``, a shell) makes file
    operations the user pays for on the same filesystem, so a count of the
    interpreter's own calls alone would call a command cheap while it spawns
    a compiler.

    - A call is excluded only when its first quoted path starts with one of
      :data:`EXCLUDED_ROOTS`.
    - A call with no quoted path (``getdents64``, an ``fd``-relative stat with
      ``""`` and ``AT_EMPTY_PATH``) counts under the bucket ``fd``.
    - ``<... resumed>`` lines never match :data:`_STRACE_CALL`, so a call strace
      split across two lines counts once, from its ``<unfinished ...>`` half.
    - ``workspace`` is the subset under *workspace_prefixes*, the roots
      themselves included (see :meth:`PathBuckets.bucket`). Nothing is
      subtracted: this is a ceiling, and a ceiling may carry the import
      system's per-import probes of a repo's lib dirs.
    - The first tid in the trace is the measured interpreter, which never
      execs. Any other tid is named by the program it ``execve``s; its
      ``execve`` counts under that name.
    - A path under *bytecode_prefix* (``PYTHONPYCACHEPREFIX``, a mirror of
      the source tree) is charged as the source path it mirrors: a cached
      ``rich`` module's ``.pyc`` read is ``site:rich``'s cost, and a fixture
      module's ``.pyc`` probe is the workspace's, exactly as they are when
      the cache sits beside the source.
    """
    ops = FileOps()
    excluded = tuple(EXCLUDED_ROOTS)
    program: dict[str, str] = {}
    main_tid: str | None = None
    for line in text.splitlines():
        m = _STRACE_CALL.match(line)
        if m is None:
            continue
        tid, call, rest = m.groups()
        main_tid = main_tid or tid
        quoted = _FIRST_QUOTED.search(rest)
        path = quoted.group(1) if quoted else ""
        if bytecode_prefix and path.startswith(bytecode_prefix + "/"):
            path = path[len(bytecode_prefix) :]
        if call == "execve" and tid != main_tid and path:
            program[tid] = Path(path).name
        if path.startswith(excluded):
            continue
        ops.total += 1
        bucket = buckets.bucket(path) if path else "fd"
        ops.by_bucket[bucket] = ops.by_bucket.get(bucket, 0) + 1
        if _root_of(path, workspace_prefixes) is not None:
            ops.workspace += 1
        name = program.get(tid, "python")
        ops.by_process[name] = ops.by_process.get(name, 0) + 1
    return ops


def _workspace_prefixes(env: dict[str, str] | None) -> list[str]:
    """Return the fixture root and ``OTTO_HOME``, raw and resolved, each with a trailing sep."""
    if env is None:
        return []
    out: list[str] = []
    for var in (FIXTURE_ROOT_ENV_VAR, "OTTO_HOME"):
        root = env.get(var)
        if not root:
            continue
        for spelling in (root, os.path.realpath(root)):
            prefix = spelling.rstrip(os.sep) + os.sep
            if prefix not in out:
                out.append(prefix)
    return out


def _run_child(
    code: str,
    env: dict[str, str] | None = None,
    *,
    trace_to: Path | None = None,
    pty_input: str | None = None,
) -> str:
    """Run *code* in a fresh interpreter and return its JSON payload line.

    *env* defaults to the sanitized env — no ``OTTO_*`` at all, which is the
    right baseline for a direct ``measure`` call. A SURFACE always passes
    :func:`surface_env` instead (via :func:`measure_surface`), so its private
    ``OTTO_HOME`` — and, when it has one, its generated repo — reach the child.

    The child never inherits the caller's cwd; it starts in :func:`_quiet_cwd`.

    With *trace_to*, the child runs under ``strace -f`` writing to that file.
    ``--seccomp-bpf`` means only the traced syscall classes stop the child, so
    the module inventory measured alongside is unaffected.

    THE RESULT TRAVELS THROUGH A PRIVATE FILE, NOT STDOUT, for the real-entry
    child (``_CHILD_ENTRY_BODY`` — see the comment there) — unconditionally,
    every call. A real dispatch runs otto's own logging, which otto's own
    ``CONSOLE`` — the same stdout ``_print_output_dir``'s ``atexit`` hook
    writes to — can render on a different thread than this child's main one,
    mid-write of this function's own JSON print. A file this child alone
    writes has no such shared channel to race on.

    Every OTHER child kind (bare import — ``_CHILD_IMPORT``; dispatch-target
    resolution — ``_CHILD_CLI``; the baseline — ``_CHILD_BASELINE``) has never
    been observed to log anything, so they still print their JSON as the last
    stdout line and this function falls back to reading it — the
    ``result_path`` empty check below is what decides which one happened, per
    call, rather than per child kind, so a future body that starts writing the
    file needs no change here.

    With *pty_input*, the child's stdin, stdout and stderr are a
    pseudo-terminal instead (:func:`_run_under_pty`), and *pty_input* is
    typed into it. Only the real-entry child is run this way, so its result
    still comes back through the file.
    """
    result_fd, result_name = tempfile.mkstemp(prefix="otto-budget-result-", suffix=".json")
    os.close(result_fd)
    result_path = Path(result_name)
    try:
        child_env = dict(_sanitized_env() if env is None else env)
        child_env[RESULT_PATH_ENV_VAR] = str(result_path)
        argv = [sys.executable, "-c", code]
        if trace_to is not None:
            argv = [
                strace_executable(),
                "-f",
                "-qq",
                "--seccomp-bpf",
                "-e",
                "trace=%file,getdents64",
                "-o",
                str(trace_to),
                *argv,
            ]
        if pty_input is not None:
            _run_under_pty(argv, child_env, pty_input)
            return result_path.read_text()
        out = subprocess.run(  # noqa: S603 (fixed interpreter + measured argv, no shell)
            argv,
            capture_output=True,
            text=True,
            check=True,
            env=child_env,
            cwd=_quiet_cwd(),
        )
        if result_path.stat().st_size > 0:
            return result_path.read_text()
        # THE LAST *JSON* LINE, NOT THE LAST LINE — for the three child kinds
        # that still reach here (`_CHILD_IMPORT`, `_CHILD_CLI`,
        # `_CHILD_BASELINE`; the real-entry child never does, since it always
        # writes the result file above). None of the three is known to print
        # anything but its own JSON dict, but a blind `splitlines()[-1]` broke
        # once already — the real-entry surface that opens an output
        # directory used to print its JSON too, with an `atexit` summary
        # landing after it — so the scan stays defensive for whichever of
        # these three logs something first.
        lines = out.stdout.strip().splitlines()
        for line in reversed(lines):
            if line.startswith(("{", "[")):
                return line
        raise RuntimeError(
            f"import_budget: child produced no JSON line on stdout; stderr:\n{out.stderr}"
        )
    finally:
        result_path.unlink(missing_ok=True)


def _run_under_pty(argv: list[str], env: dict[str, str], text: str) -> None:
    """Run *argv* on a fresh pseudo-terminal, type *text* into it, and wait for it to end.

    The terminal is the child's stdin, stdout AND stderr, as a user's would
    be, so a verb that checks ``isatty`` or puts the terminal in raw mode
    takes the path it takes for a user. The output is drained in this loop
    until the terminal closes, never on a thread: a child blocks once the
    terminal's buffer fills, so the drain has to keep pace with it, and it
    returns EOF (``EIO`` on Linux) once every process holding the terminal
    has exited. Raises ``CalledProcessError`` with the output on a non-zero
    exit, as ``subprocess.run(check=True)`` does on the pipe path.
    """
    master, slave = pty.openpty()
    try:
        proc = subprocess.Popen(  # noqa: S603 (fixed interpreter + measured argv, no shell)
            argv, stdin=slave, stdout=slave, stderr=slave, env=env, cwd=_quiet_cwd()
        )
    finally:
        os.close(slave)
    output = bytearray()
    try:
        os.write(master, text.encode())
        while True:
            try:
                chunk = os.read(master, 65536)
            except OSError:
                break
            if not chunk:
                break
            output += chunk
    finally:
        os.close(master)
    if proc.wait() != 0:
        raise subprocess.CalledProcessError(
            proc.returncode, argv, output=output.decode(errors="replace")
        )


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
    such parent, and it still needs a pinned home (see :func:`surface_env`)
    — so one throwaway parent is made on first use
    and registered for the same sweep. Made ONCE per process, not per call:
    only the leaf home has to be fresh per measurement, and the leaf is a name
    rather than a directory the harness creates.
    """
    root = Path(tempfile.mkdtemp(prefix="otto-budget-homes-"))
    _FIXTURE_ROOTS.append(root)
    return root


@functools.cache
def _quiet_cwd() -> Path:
    """Return the empty directory every measured child starts in.

    ``python -c`` puts the cwd on ``sys.path``, so the child imports from it,
    and every import probes it. CPython's ``FileFinder`` caches a directory's
    listing and re-lists it whenever the directory's mtime moved, so a
    shared cwd charges the child for whoever else writes there. Inheriting
    the caller's cwd handed the child the repo root, which every other test
    process in the run writes into; one such write mid-measurement cost the
    child an extra listing (``help_repo_warm`` on 3.14, issue #428).

    Nothing writes here — the child writes only into its ``OTTO_HOME`` — so
    this directory's mtime never moves while a child is importing from it.
    Made ONCE per process and swept with the fixture roots.
    """
    root = Path(tempfile.mkdtemp(prefix="otto-budget-cwd-"))
    _FIXTURE_ROOTS.append(root)
    return root


@functools.cache
def _generated_repo_for(key: str, files: int, dirs: int, ssh_lab_port: int | None = None) -> Path:
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
    # realistic=True (spec §3.5 of `2026-09-25-dispatch-startup-cost-design.md`):
    # top-level suite files import pytest, the init module registers an
    # instruction and imports a monitor parser, and a JSON
    # lab source is declared — so every repo-bearing surface's count shows
    # what a real repo actually costs, rather than an empty init module nobody
    # ships.
    return generate_repo(root, files=files, dirs=dirs, realistic=True, ssh_lab_port=ssh_lab_port)


@functools.cache
def _budget_ssh_port() -> int:
    """Return a loopback port with no listener, picked once per process.

    Bound and released: nothing listens on it afterwards, so a connection is
    refused at once. That is not a guarantee it stays free, which is why
    :func:`_prove_port_closed` checks it again before every measured run.
    """
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _prove_port_closed(port: int) -> None:
    """FAIL, naming *port*, unless a connection to it on ``127.0.0.1`` is refused.

    The SSH surface measures one refused connection. A listener that appeared
    on the port since it was picked would turn that into a handshake with
    whatever answers, which measures something else entirely.
    """
    with socket.socket() as sock:
        err = sock.connect_ex(("127.0.0.1", port))
    if err != errno.ECONNREFUSED:
        raise RuntimeError(
            f"import_budget: the closed-port SSH surface needs 127.0.0.1:{port} to refuse "
            f"connections, but connecting returned {errno.errorcode.get(err, err)}"
        )


def _surface_repo(surface: Surface) -> Path:
    """Return *surface*'s generated repo, built once per process."""
    if surface.sut_files is None:
        raise ValueError(f"surface {surface.key!r} carries no generated repo")
    port = _budget_ssh_port() if surface.ssh_lab else None
    return _generated_repo_for(surface.key, surface.sut_files, surface.sut_dirs_count, port)


def fixture_root(surface: Surface) -> Path:
    """Return the fixture root ``{root}`` names in *surface*'s argv: its repo's parent."""
    return _surface_repo(surface).parent


ROOT_PLACEHOLDER = "{root}"
"""Stands for the surface's fixture root in an argv; see :data:`SURFACES`."""


def _uses_fixture_root(surface: Surface) -> bool:
    """Whether *surface*'s argv names paths under its fixture root."""
    return any(ROOT_PLACEHOLDER in arg for arg in surface.argv)


def _reset_transfer_fixture(root: Path) -> None:
    """Write the file a transfer surface moves, and empty the directories it moves it into.

    Reset before every measured run, because the repo (and so the root) is
    cached per process: a destination that already held the file from an
    earlier measurement would let a later one measure an overwrite, or pass a
    "the file arrived" check the run itself never earned.
    """
    (root / "payload.txt").write_text("import budget transfer payload\n")
    for dest in ("put-dest", "get-dest"):
        shutil.rmtree(root / dest, ignore_errors=True)
        (root / dest).mkdir()


FIXTURE_ROOT_ENV_VAR = "IMPORT_BUDGET_FIXTURE_ROOT"
"""Tells the child which tree is the fixture, so it can report how many of its
own ``sys.path`` entries came from the repo under measurement.

Deliberately NOT ``OTTO_``-prefixed: otto parses that namespace, and a name it
does not know has no business reaching its settings model. The sanitizer only
strips ``OTTO_*``, so this one survives into the child either way.
"""

RESULT_PATH_ENV_VAR = "IMPORT_BUDGET_RESULT_PATH"
"""Tells the child where to write its JSON payload, instead of printing it.

See :func:`_run_child` and the comment on ``_CHILD_ENTRY_BODY``: a real
dispatch runs otto's own logging, which can render — on a different thread —
between this function's own ``print``'s content and its trailing newline,
corrupting the one stdout line every child kind used to be parsed from. Not
``OTTO_``-prefixed for the same reason :data:`FIXTURE_ROOT_ENV_VAR` is not.
"""


def surface_env(surface: Surface) -> dict[str, str]:
    """Env for *surface*: sanitized, a FRESH OTTO_HOME, plus any generated repo.

    ``OTTO_HOME`` is not optional, AND IT IS PINNED ON EVERY SURFACE, not only
    the repo-bearing ones. ``workspace_home()`` resolves under ``otto_home()``
    = ``$OTTO_HOME`` else ``~/.otto``, and the sanitizer strips ``OTTO_*`` — so
    without injection a child reads (and, on the surfaces that run the real
    entry path, WRITES) the developer's real ``~/.otto``, whose contents are
    machine state. The ``workspace`` counter also attributes by prefix
    against this root, so an unpinned surface would charge its home reads to
    whatever that box keeps under ``~/.otto``. A non-repo surface's home
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
      inside the fixture root, so the atexit sweep still collects it.
    - The dominant one: importing the repo's init module and its top-level test
      files writes ``__pycache__`` INTO THE FIXTURE TREE, which is cached and
      therefore shared across calls. Measured directly with the audit-hook
      open count this harness used before strace — 607, then 598, 598, 598;
      deleting the two ``__pycache__`` dirs returned it to exactly 607.
      ``PYTHONDONTWRITEBYTECODE`` keeps the harness from mutating
      the tree it is measuring, so every call is the cold number.

    Only these need to be fresh. The repo tree stays cached: generating a
    200-file corpus per call is pure cost, and with no bytecode written it
    never warms.

    EVERY SURFACE READS BYTECODE FROM THE HARNESS'S OWN CACHE AND WRITES
    NONE (:func:`bytecode_prefix`), whatever the runner exports. A module
    whose ``.pyc`` is missing costs one more file operation than a cached one
    (the failed probe, then the source read), and a child that may write
    adds the write too, so the count otherwise follows whatever bytecode the
    machine happens to hold: measured on ``bootstrap_repo`` under CPython
    3.10, deleting the venv's and ``src/otto``'s in-tree ``.pyc`` files, as a
    fresh CI checkout has them (``uv`` compiles none at install), moved
    ``file_ops`` from 2738 to 3030, over a 10% ceiling on unchanged product
    code. The cache is compiled before the first measurement
    (:func:`_warm_bytecode`) and no child writes to it, so every measurement
    sees the same warm cache: the steady state of any installation after its
    first run. The fixture repo's own modules are never compiled, so they
    are cold on every run alike, and their probes are charged back to the
    workspace (:func:`parse_file_ops`).
    """
    env = _sanitized_env()
    if surface.sut_files is not None:
        repo = _surface_repo(surface)
        env["OTTO_SUT_DIRS"] = str(repo)
        env[FIXTURE_ROOT_ENV_VAR] = str(repo.parent)
        home_parent = repo.parent
    else:
        home_parent = _home_parent_for_non_repo_surfaces()
    env["OTTO_HOME"] = str(home_parent / f"home-{uuid.uuid4().hex}")
    # A real dispatch writes an output directory (spec §3.2 of
    # `2026-09-25-dispatch-startup-cost-design.md`), and a fresh one
    # per call keeps that write deterministic and non-accumulating — the same
    # reason OTTO_HOME above is fresh per call rather than shared.
    env["OTTO_XDIR"] = str(home_parent / f"xdir-{uuid.uuid4().hex}")
    env["PYTHONPYCACHEPREFIX"] = bytecode_prefix()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
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
    ``__main__`` arrive the same way. Charging these to otto would make the
    module inventory depend on which unrelated dev/docs dependencies happen
    to be installed, so they are measured and subtracted rather than counted.
    """
    return frozenset(json.loads(_run_child(_CHILD_BASELINE)))


def _is_measurement_artifact(module: str) -> bool:
    """Report whether *module*'s presence reflects the build/platform, not otto's imports.

    mypyc-compiled wheels load a single hashed shared module named
    ``<hash>__mypyc`` backing the whole compiled group. Those wheels are built
    per-platform, so an x86_64 CI runner loads it where an aarch64 machine
    falls back to pure Python and does not — a one-module difference that has
    nothing to do with how much otto imports. Counting it would make the
    module inventory architecture-dependent.
    """
    return module.endswith("__mypyc")


_CHILD_PATHS = """
import json, os, site, sys, sysconfig
import otto
_paths = sysconfig.get_paths()
print(json.dumps({"site_packages": site.getsitepackages(),
                  "stdlib": [_paths["stdlib"], _paths["platstdlib"]],
                  "otto_src": os.path.dirname(os.path.realpath(otto.__file__)),
                  "import_dirs": [p for p in sys.path if p and os.path.isdir(p)]}))
"""


@functools.cache
def _interpreter_paths() -> dict:
    """Return where the measured interpreter keeps site-packages, the stdlib and otto.

    Asked of the CHILD interpreter (``sys.executable`` in the sanitized env,
    exactly what every measured child runs), because the harness itself may
    be imported by another. Asked ONCE, in a child of its own that nobody
    traces, rather than by each measured child as it writes its payload:
    ``import sysconfig`` alone costs 60-80 file operations (measured on 3.10,
    3.12, 3.13 and 3.14), and ``realpath`` one ``lstat`` per path component.
    A measured child that looked these up would charge that to every
    surface, inflating exactly the counter it is bucketing.

    ``import_dirs`` is the child's ``sys.path``, the directories it imports
    from, spelled exactly as the child spells them: :func:`_warm_bytecode`
    compiles them, and a cached ``.pyc`` is found under the source path as
    the importer spells it.
    """
    return json.loads(_run_child(_CHILD_PATHS))


def bytecode_prefix() -> str:
    """Return the bytecode cache every measured child reads (``PYTHONPYCACHEPREFIX``).

    The harness's own directory, outside the checkout and outside the venv:
    nothing else writes there, so its state is whatever :func:`_warm_bytecode`
    made it. It persists across runs (the compile it holds is the slow part)
    and is shared by every interpreter, whose ``.pyc`` names carry their own
    ``cpython-XY`` tag. It lives beside ``tests/conftest.py``'s session cache
    and follows the same ``XDG_CACHE_HOME`` rule.
    """
    try:
        base = Path(os.environ.get("XDG_CACHE_HOME") or "~/.cache").expanduser()
    except RuntimeError:
        # No $HOME and no passwd entry for this uid: a scratch container.
        base = Path(tempfile.gettempdir())
    return str(base / "otto" / "import-budget-pycache")


@functools.cache
def _warm_bytecode() -> None:
    """Compile every module the measured interpreter can import into :func:`bytecode_prefix`.

    Once per process, before the first measurement, in a child nobody
    traces. ``compileall`` skips a module whose cached ``.pyc`` is current,
    so a warm cache costs only its checks. Its exit status is not checked:
    a distribution may ship a file this interpreter cannot compile (test
    data, another Python's syntax), which no import can load either, and
    the rest of the cache is compiled regardless. What IS checked is that
    otto's own package landed in the cache: a prefix nobody can write to
    would otherwise leave every module cold, and an ``--update`` would
    record those inflated counts as baselines without a word.

    The invalidation mode is pinned to ``timestamp``, the mode the import
    system writes and checks by default: an ambient ``SOURCE_DATE_EPOCH``
    would otherwise switch ``compileall`` to checked-hash ``.pyc`` files,
    which cost a full source read on every import.
    """
    prefix = bytecode_prefix()
    env = _sanitized_env()
    env.pop("PYTHONDONTWRITEBYTECODE", None)
    env["PYTHONPYCACHEPREFIX"] = prefix
    import_dirs = _interpreter_paths()["import_dirs"]
    done = subprocess.run(  # noqa: S603 (fixed interpreter + fixed module, no shell)
        [
            sys.executable,
            "-m",
            "compileall",
            "-q",
            "--invalidation-mode",
            "timestamp",
            *import_dirs,
        ],
        capture_output=True,
        text=True,
        check=False,
        env=env,
        cwd=_quiet_cwd(),
    )
    otto_init = next(
        (
            Path(d, "otto", "__init__.py")
            for d in import_dirs
            if Path(d, "otto", "__init__.py").is_file()
        ),
        None,
    )
    cached = None if otto_init is None else cached_bytecode(prefix, otto_init)
    if cached is None or not cached.is_file():
        raise RuntimeError(
            f"import_budget: compiling the bytecode cache left no {cached or 'otto/__init__'}; "
            f"every measurement would read cold modules. Check that {prefix} is writable "
            f"(it follows XDG_CACHE_HOME).\n{done.stderr[-2000:]}"
        )


def cached_bytecode(prefix: str, source: Path) -> Path:
    """Where the import system looks for *source*'s ``.pyc`` under the bytecode cache *prefix*.

    ``PYTHONPYCACHEPREFIX`` mirrors the source tree: the source's directory,
    rooted at the prefix, holding ``<name>.<cache tag>.pyc`` with no
    ``__pycache__`` level.
    """
    return Path(prefix, *source.parent.parts[1:]) / (
        f"{source.stem}.{sys.implementation.cache_tag}.pyc"
    )


def _dir_prefixes(dirs: list[str]) -> list[str]:
    """Each of *dirs*, raw and resolved, with a trailing separator, without duplicates."""
    out: list[str] = []
    for d in dirs:
        for spelling in (d, os.path.realpath(d)):
            prefix = spelling.rstrip(os.sep) + os.sep
            if prefix not in out:
                out.append(prefix)
    return out


def measure(
    argv: list[str],
    *,
    bootstrap: bool = False,
    real_entry: bool = False,
    env: dict[str, str] | None = None,
    pty_input: str | None = None,
) -> dict:
    """Import otto in a fresh subprocess for *argv*; return its module inventory.

    *bootstrap* additionally runs the composition root, as a real invocation does.
    *real_entry* runs ``otto.cli.main.entry`` itself instead, which is the only
    way to observe bootstrap AND the completion/name caches on one path.
    *env* defaults to the sanitized env; a surface passes its own
    (:func:`surface_env`), which is where ``OTTO_HOME`` comes from.
    *pty_input* runs the child on a pseudo-terminal (see :func:`_run_child`).

    ``result["file_ops"]`` is :func:`parse_file_ops` over the same trace, as a
    dict, and the result also carries the interpreter paths it was bucketed
    by (:func:`_interpreter_paths`).
    """
    if real_entry:
        code = _CHILD_ENTRY.format(argv=argv)
    elif argv[:1] == ["python"]:
        code = _CHILD_IMPORT
    else:
        code = _CHILD_CLI.format(argv=argv, bootstrap=bootstrap)
    with tempfile.TemporaryDirectory(prefix="otto-budget-strace-") as tmp:
        trace = Path(tmp) / "trace"
        result = json.loads(_run_child(code, env, trace_to=trace, pty_input=pty_input))
        text = trace.read_text(errors="replace")
        paths = _interpreter_paths()
        result.update({key: paths[key] for key in ("site_packages", "stdlib", "otto_src")})
        result["file_ops"] = asdict(
            parse_file_ops(
                text,
                workspace_prefixes=_workspace_prefixes(env),
                bytecode_prefix=(env or {}).get("PYTHONPYCACHEPREFIX", ""),
                buckets=PathBuckets(
                    site_packages=_dir_prefixes(paths["site_packages"]),
                    stdlib=_dir_prefixes(paths["stdlib"]),
                    otto_src=os.path.realpath(paths["otto_src"]).rstrip(os.sep) + os.sep,
                    workspace=_workspace_prefixes(env),
                ),
            )
        )
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
    the one whose baseline it then compares against. The script and
    ``tests/unit/import_budget/`` share this for the same reason they share
    :func:`check_surface`.

    ``surface_env`` is resolved ONCE here, so a warm surface's seed run and
    its measured run share the same ``OTTO_HOME`` — the seed's cache write is
    the state the measurement exists to observe. Two calls still get two
    homes, which is what keeps repeated measurements independent.
    """
    _warm_bytecode()
    env = surface_env(surface)
    argv = surface.argv
    if _uses_fixture_root(surface):
        root = env[FIXTURE_ROOT_ENV_VAR]
        argv = [arg.replace(ROOT_PLACEHOLDER, root) for arg in argv]
    if surface.warm:
        # The seed. Same child, same repo, same home — so it performs exactly
        # the work the measured run would have had to, and leaves exactly the
        # cache the measured run reads. Its counts are discarded. The argv is
        # the surface's own UNLESS ``seed_argv`` overrides it — see
        # ``Surface.seed_argv`` for when the measured argv cannot seed itself.
        measure(
            surface.seed_argv or argv,
            bootstrap=surface.bootstrap,
            real_entry=surface.real_entry,
            env=env,
        )
    # Immediately before the measured run, after the seed: the seed never
    # transfers or connects, and these must hold for the run that counts.
    if _uses_fixture_root(surface):
        _reset_transfer_fixture(Path(env[FIXTURE_ROOT_ENV_VAR]))
    if surface.ssh_lab:
        _prove_port_closed(_budget_ssh_port())
    return measure(
        argv,
        bootstrap=surface.bootstrap,
        real_entry=surface.real_entry,
        env=env,
        pty_input=surface.pty_input,
    )


def measure_file_ops(surface: Surface) -> FileOps:
    """Measure *surface* and return only its file operations."""
    return FileOps(**measure_surface(surface)["file_ops"])


def interpreter_tag() -> str:
    """Return the running interpreter's ``major.minor``, which keys a ceilings file.

    File-operation counts are only comparable WITHIN one Python minor: the
    interpreter's own stdlib and import machinery are part of every count, and
    they change release to release.
    """
    return f"{sys.version_info.major}.{sys.version_info.minor}"


def ceilings_path(tag: str | None = None) -> Path:
    """Path to the ceilings file for interpreter *tag* (``major.minor``; default: this one)."""
    return CEILINGS_DIR / f"{tag or interpreter_tag()}.json"


def read_ceilings() -> dict[str, dict]:
    """Return the running interpreter's baselines, keyed by surface.

    Each value holds ``file_ops``, ``workspace``, ``by_bucket`` and
    ``by_process``. A missing file reads as no baselines at all, which
    :func:`check_surface` turns into a NAMED failure per gated surface rather
    than a skip. A file that is present but wrong fails by name here: one
    recorded under another interpreter (a copied or renamed file), or an
    entry missing a gated counter (a hand edit), would otherwise gate
    against the wrong numbers or end in a bare ``KeyError``.
    """
    path = ceilings_path()
    if not path.exists():
        return {}
    data = json.loads(path.read_text())
    tag = interpreter_tag()
    if data.get("interpreter") != tag:
        raise ValueError(
            f"import_budget: {_display_path(path)} records interpreter "
            f"{data.get('interpreter')!r}, not {tag!r}; regenerate it with "
            f"`make import-snapshot` under CPython {tag}"
        )
    surfaces = data["surfaces"]
    for key, baseline in surfaces.items():
        missing = [name for name in CEILING_COUNTERS if name not in baseline]
        if missing:
            raise ValueError(
                f"import_budget: the baseline for `{key}` in {_display_path(path)} has no "
                f"{', '.join(missing)}; regenerate the file with `make import-snapshot` "
                f"under CPython {tag}"
            )
    return surfaces


def write_ceilings(results: dict[str, FileOps]) -> None:
    """Write *results* as the running interpreter's baselines, for the gated surfaces only.

    The file stores BASELINES, not ceilings: :func:`ceiling` is applied at
    check time, so a headroom change never needs a regeneration. The
    breakdowns are stored beside the totals so a breach can say which
    package or child process grew (:func:`breakdown_diff`). A tracked surface
    is never written, because nothing reads its baseline. The whole file is
    replaced: a regeneration records one interpreter's measurement, not a
    merge with an older one.
    """
    surfaces = {
        key: {
            "file_ops": ops.total,
            "workspace": ops.workspace,
            "by_bucket": ops.by_bucket,
            "by_process": ops.by_process,
        }
        for key, ops in results.items()
        if not surface_by_key(key).tracked
    }
    CEILINGS_DIR.mkdir(parents=True, exist_ok=True)
    payload = {"interpreter": interpreter_tag(), "surfaces": surfaces}
    ceilings_path().write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


CEILING_COUNTERS: dict[str, str] = {"file_ops": "total", "workspace": "workspace"}
"""The gated counters: each baseline key, and the :class:`FileOps` field it bounds."""

BREAKDOWN_LINES = 12
"""The most lines :func:`breakdown_diff` prints; ``--report-json`` has every group."""


def check_exit(surface: Surface, result: dict) -> list[str]:
    """Return a violation unless a real-entry run ended the way *surface* expects (empty = pass).

    The exit code must be ``expect_exit``, and no exception may have escaped
    the entry: every surface, the failing ones included, measures a command
    that ends as a user's does, and an uncaught exception is a rendered
    traceback, a cost of its own. A surface with ``expect_loaded`` must also
    have imported that module. The message always quotes the recorded
    exception, so a crash names its cause without a re-run.
    """
    if not surface.real_entry:
        return []
    exit_code = result.get("exit_code", 0)
    exception = result.get("exception")
    loaded = surface.expect_loaded is None or surface.expect_loaded in result.get("modules", [])
    if exit_code == surface.expect_exit and exception is None and loaded:
        return []
    expected = f"exit {surface.expect_exit} with no exception" + (
        f" having imported {surface.expect_loaded}" if surface.expect_loaded else ""
    )
    message = (
        f"`{surface.key}`: the measured command exited {exit_code} with "
        f"{exception or 'no exception'}, not {expected}, so it measured a failure "
        f"path, not the surface"
    )
    return [message]


RATIO_BREAKDOWN_LINES = 8
"""How many of a surface's largest buckets and child processes a ratio failure lists."""


def check_ratio(surface: Surface, measured: FileOps, floor_file_ops: int | None) -> list[str]:
    """Return a violation if *surface* costs more than its target multiple of ``otto --version``.

    *floor_file_ops* is :data:`RATIO_FLOOR`'s ``file_ops`` from the same run.
    A surface with no :attr:`Surface.target_ratio` passes whatever it is
    given; one with a target and no floor is the caller's mistake, raised
    rather than passed, because a target never checked is no target.

    A failure lists the surface's largest buckets and every child process
    beside the numbers, because a ratio breach need not come with a ceiling
    breach, whose breakdown would otherwise be the only one printed.
    """
    target = surface.target_ratio
    if target is None:
        return []
    if floor_file_ops is None:
        raise ValueError(
            f"`{surface.key}` has a target ratio of {target:g}; checking it needs "
            f"{RATIO_FLOOR}'s file_ops from the same run"
        )
    if measured.total <= target * floor_file_ops:
        return []
    ratio = measured.total / floor_file_ops
    largest = sorted(measured.by_bucket.items(), key=lambda kv: (-kv[1], kv[0]))
    children = sorted(
        (name, count) for name, count in measured.by_process.items() if name != "python"
    )
    lines = [f"{count} {name}" for name, count in largest[:RATIO_BREAKDOWN_LINES]]
    lines += [f"{count} process {name}" for name, count in children]
    breakdown = "".join(f"\n  {line}" for line in lines)
    message = (
        f"`{surface.key}`: file_ops {measured.total} is {ratio:.2f}x {RATIO_FLOOR}'s "
        f"{floor_file_ops} (`otto --version`), over its target of {target:g}x "
        f"(at most {int(target * floor_file_ops)}) on CPython {interpreter_tag()}. "
        f"The verb is paying for something it does not need: an import or a child "
        f"process. Its largest buckets and every child process follow; "
        f"`--report-json` has the full `by_bucket` and `by_process`. Raise "
        f"`target_ratio` only with a commit saying why the verb needs the cost."
        f"{breakdown}"
    )
    return [message]


def check_surface(
    surface: Surface, result: dict, *, floor_file_ops: int | None = None
) -> list[str]:
    """Return human-readable import-budget violations for a surface (empty = pass).

    The script (``--check``) and ``tests/unit/import_budget/`` share this, so
    they share one source of truth. A TRACKED surface returns ``[]`` whatever
    it measured. A gated one is checked in this order:

      1. its ending (:func:`check_exit`) — a ``real_entry`` surface must exit
         ``surface.expect_exit`` with no exception escaping AND have imported
         ``surface.expect_loaded``, or it measured a failure path rather than
         the surface: a misspelled host id exits 1 too, and its smaller count
         would read as a saving;
      2. its baseline — no entry for this surface in the RUNNING
         interpreter's ceilings file (see :func:`interpreter_tag`) is a named
         failure, never a skip, because "no file for this Python" is the shape
         a newly added interpreter or surface takes;
      3. its ceilings — ``file_ops`` and ``workspace`` must each stay at or
         under :func:`ceiling` of their baseline with ``surface.headroom``. A
         breach is followed by :func:`breakdown_diff`'s lines, so it names
         what grew;
      4. its target ratio (:func:`check_ratio`), against *floor_file_ops*:
         :data:`RATIO_FLOOR`'s ``file_ops`` from the same run, which a
         surface with a ``target_ratio`` requires.
    """
    if surface.tracked:
        return []
    violations = check_exit(surface, result)
    measured = FileOps(**result["file_ops"])
    ratio_violations = check_ratio(surface, measured, floor_file_ops)
    baseline = read_ceilings().get(surface.key)
    if baseline is None:
        tag = interpreter_tag()
        violations.append(
            f"`{surface.key}`: no baseline for CPython {tag} in "
            f"{_display_path(ceilings_path())}. Every interpreter that runs this "
            f"check needs its own file (the counts are not comparable across minors). "
            f"Regenerate it by running `make import-snapshot` UNDER CPython {tag} — "
            f"for another minor, `uv run nox -s tests_hostless-{tag} --install-only`, then "
            f"`.nox/tests_hostless-{tag.replace('.', '-')}/bin/python "
            f"scripts/import_budget.py --update`.\n"
            f"  measured now: file_ops {measured.total}, workspace {measured.workspace}"
        )
        return violations + ratio_violations
    breaches = []
    for name, attr in CEILING_COUNTERS.items():
        value, base = getattr(measured, attr), baseline[name]
        limit = ceiling(base, surface.headroom)
        if value > limit:
            breaches.append(f"{name} {value} > ceiling {limit} (baseline {base})")
    if breaches:
        growth = "".join(f"\n  {line}" for line in breakdown_diff(baseline, measured))
        violations.append(
            f"`{surface.key}`: over its ceiling on CPython {interpreter_tag()} "
            f"({_display_path(ceilings_path())}): {'; '.join(breaches)}. If the growth "
            f"is intended, regenerate with `make import-snapshot` under this "
            f"interpreter and say why in the commit.{growth}"
        )
    return violations + ratio_violations


def breakdown_diff(baseline: dict, measured: FileOps) -> list[str]:
    """Return one line per bucket or child process that grew, largest growth first.

    ``+598 site:asyncssh (0 → 598)`` is a package's file operations;
    ``+12 process gcc (0 → 12)`` a child program's. Groups that shrank or held
    are left out: a breach is explained by what grew. Capped at
    :data:`BREAKDOWN_LINES`, the last line then saying how many were left out.
    """
    grown: list[tuple[int, str, int, int]] = []
    for label, before, now in [
        ("", baseline.get("by_bucket", {}), measured.by_bucket),
        ("process ", baseline.get("by_process", {}), measured.by_process),
    ]:
        for name, count in now.items():
            was = before.get(name, 0)
            if count > was:
                grown.append((count - was, f"{label}{name}", was, count))
    grown.sort(key=lambda row: (-row[0], row[1]))
    lines = [f"+{delta} {name} ({was} → {count})" for delta, name, was, count in grown]
    if len(lines) > BREAKDOWN_LINES:
        left_out = len(lines) - (BREAKDOWN_LINES - 1)
        lines = [*lines[: BREAKDOWN_LINES - 1], f"... and {left_out} more that grew"]
    return lines


def advisories(surface: Surface, result: dict) -> list[str]:
    """Return non-failing notes about *surface*'s ceilings (empty = nothing to say).

    Kept apart from :func:`check_surface` on purpose: a note never fails a
    run, so it must never reach the violations list. A note says a counter
    measured below :data:`STALE_RATIO` of its ceiling, i.e. the
    baseline is stale-high and the ceiling admits more than it should.

    A counter within :data:`MIN_SLACK` of its baseline never earns one, even
    under the ratio: on a small counter the absolute floor alone puts the
    measurement under 80% of its ceiling (a ``workspace`` of 1 has a ceiling
    of 6), and that is the floor doing its job, not a stale baseline. A
    missing baseline says nothing here; :func:`check_surface` already fails
    it by name.
    """
    if surface.tracked:
        return []
    baseline = read_ceilings().get(surface.key)
    if baseline is None:
        return []
    measured = FileOps(**result["file_ops"])
    notes = []
    for name, attr in CEILING_COUNTERS.items():
        value, base = getattr(measured, attr), baseline[name]
        limit = ceiling(base, surface.headroom)
        if value < STALE_RATIO * limit and value < base - MIN_SLACK:
            notes.append(
                f"`{surface.key}`: {name} {value} is below {STALE_RATIO:g}x its ceiling "
                f"{limit} (baseline {base}) on CPython {interpreter_tag()}. A stale-high "
                f"baseline widens the ceiling silently; consider regenerating "
                f"{_display_path(ceilings_path())} with `make import-snapshot` under "
                f"this interpreter."
            )
    return notes


def _display_path(path: Path) -> str:
    """Render *path* for a failure message: repo-relative when it is inside the repo.

    A message formatter must never be the thing that raises. ``relative_to``
    throws on any path outside ``REPO_ROOT`` — which a redirected
    ``CEILINGS_DIR`` (a test driving the ceilings against ``tmp_path``) or a
    symlinked checkout produces — turning a diagnosable budget failure into a
    ``ValueError`` traceback from inside the diagnosis.
    """
    try:
        return str(path.relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


def report_row(surface: Surface, result: dict) -> dict:
    """One ``--report`` row: *surface*'s file operations out of a measurement *result*."""
    ops = result["file_ops"]
    return {
        "surface": surface.key,
        "tier": "tracked" if surface.tracked else "gated",
        "file_ops": ops["total"],
        "workspace": ops["workspace"],
        "exit": result.get("exit_code"),
        "by_bucket": ops["by_bucket"],
        "by_process": ops["by_process"],
    }


REPORT_TOP_BUCKETS = 5
"""How many of a row's largest buckets the ``--report`` table prints; the JSON has them all."""


REPORT_HEADER = (
    f"{'surface':26} {'tier':7} {'file_ops':>8} {'workspace':>9} {'exit':>4}"
    f"  top buckets | child processes"
)


def format_report_row(row: dict) -> str:
    """Render one ``--report`` row as a line of the table under :data:`REPORT_HEADER`.

    The top buckets say where a surface's file operations go; the child
    processes (every program but ``python``, the measured interpreter) say
    what it started. Buckets are cut to fit a line; ``--report-json`` keeps
    the full breakdowns.
    """
    ranked = sorted(row["by_bucket"].items(), key=lambda kv: (-kv[1], kv[0]))
    top = " ".join(f"{name}={n}" for name, n in ranked[:REPORT_TOP_BUCKETS])
    children = " ".join(
        f"{name}={n}" for name, n in sorted(row["by_process"].items()) if name != "python"
    )
    exit_code = "-" if row["exit"] is None else str(row["exit"])
    return (
        f"{row['surface']:26} {row['tier']:7} {row['file_ops']:8d} {row['workspace']:9d} "
        f"{exit_code:>4}  {top} | {children or '-'}"
    )


def main(argv: list[str] | None = None) -> int:
    """Print the per-surface table; with ``--check`` enforce it, with ``--update`` record it."""
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument(
        "--check",
        action="store_true",
        help="enforce the file-operation ceilings; exit non-zero on a breach",
    )
    mode.add_argument(
        "--update",
        action="store_true",
        help="rewrite THIS interpreter's ceilings file from a fresh measurement of the gated "
        "surfaces (other minors need their own run)",
    )
    ap.add_argument(
        "--report",
        action="store_true",
        help="print the table (what no flag does); measures only, never checks or updates",
    )
    ap.add_argument(
        "--report-json",
        metavar="PATH",
        type=Path,
        help="also write the table, with full bucket and process breakdowns, as JSON; "
        "measures only, never checks or updates",
    )
    args = ap.parse_args(argv)
    # The report flags promise a measurement and nothing else. Accepting one
    # beside --check would print a table and exit 0 without having checked it.
    if (args.report or args.report_json) and (args.check or args.update):
        ap.error(
            "--report and --report-json only measure; they cannot be combined with "
            "--check or --update"
        )

    # A tracked surface has no baseline to record, so --update skips it.
    surfaces = [s for s in SURFACES if not (args.update and s.tracked)]
    # The ratio floor first (a stable sort keeps the rest in table order), so
    # every target ratio is checked against a floor from this same run.
    surfaces.sort(key=lambda s: s.key != RATIO_FLOOR)
    # flush=True: interleaves correctly when stdout is piped/redirected
    # (e.g. `make profile > log`), and a row appears as soon as it is measured:
    # the whole table takes minutes.
    print(REPORT_HEADER, flush=True)
    rows = []
    measured: dict[str, FileOps] = {}
    failed = False
    for s in surfaces:
        r = measure_surface(s)
        rows.append(report_row(s, r))
        print(format_report_row(rows[-1]), flush=True)
        measured[s.key] = FileOps(**r["file_ops"])
        violations: list[str] = []
        if args.check:
            floor = measured.get(RATIO_FLOOR)
            violations = check_surface(s, r, floor_file_ops=floor.total if floor else None)
        elif args.update:
            # The ending is checked here too: a baseline recorded from a
            # failure path would pin the cost of a crash.
            violations = check_exit(s, r)
        for v in violations:
            print(f"  FAIL {v}", flush=True)
        if args.check:
            # Advisory only: printed, never counted toward `failed`.
            for note in advisories(s, r):
                print(f"  NOTE {note}", flush=True)
        failed = failed or bool(violations)
    if args.report_json:
        args.report_json.write_text(json.dumps(rows, indent=2, sort_keys=True) + "\n")
    if failed:
        what = "nothing written" if args.update else "see FAIL lines above"
        print(f"\nimport budget: FAILED — {what}.", flush=True)
        return 1
    if args.update:
        write_ceilings(measured)
        print(f"\nwrote {_display_path(ceilings_path())}", flush=True)
    if args.check:
        print("\nimport budget: OK", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
