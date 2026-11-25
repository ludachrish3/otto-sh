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
``workspace`` (the subset under the generated project and ``OTTO_HOME``). Growing
past a ceiling fails, with the growth broken down by package and by child
process (:func:`breakdown_diff`); shrinking never fails, and a counter far below
its ceiling earns an advisory NOTE (:func:`advisories`). A tracked surface is
measured and printed, never enforced.

THE RUN, HOST AND TEST SURFACES ALSO CARRY A TARGET: a ceiling on their
``file_ops`` as a multiple of ``otto --version``'s (``version_project``) measured
in the same run (:attr:`Surface.target_ratio`, :func:`check_ratio`). A ceiling
pins today's cost; the ratio says how far above an interpreter start plus the
shim a command may sit, and it survives a dependency update that makes every
import dearer, because both sides move together.

The baselines live in one file per Python minor
(``tests/unit/import_budget/ceilings/<major.minor>.json``), because each
interpreter's stdlib and import machinery differ, and are checked against the
MEASURED interpreter's file; a missing baseline is a named failure, never a
skip. The measured interpreter is the one every measured child runs
(:func:`measured_python`), which need not be the one running this script. By
default it is a runtime-only venv :func:`build_env` syncs for the running
interpreter's minor, so the dev-only packages of the venv running the
harness (pytest plugins above all) never count against a surface (#1150).
`--update` regenerates every minor that has a file, each measured in a venv
:func:`build_env` syncs for it, and writes nothing unless every minor measured
clean.

The ``real_entry`` surfaces run the console entry path against a GENERATED
project, because startup I/O against an empty workspace is zero and therefore
unmeasurable. They stay host-independent the same way — the harness creates
what it measures.

A COUNT IS ONLY KEPT FROM THE PATH IT NAMES. A ``real_entry`` run passes one
validity verdict (:func:`check_exit`) before its count gates or is recorded:
its exit and escaped exception, the module roots it must and must not have
imported (a TAB the shim answers denies :data:`SHIM_DENY`), and, for a TAB,
the candidate lines on its stdout. A run that failed earlier, fell through to
another path or answered the wrong site would otherwise pass with a smaller
count, and ``--update`` would record it.

THE TAB SURFACES MEASURE ONE WARM TAB PER COST PROFILE: each way the shim
validates its cache, and each kind of hand-over to the full CLI. The site map
(:data:`SITE_PROFILES`, :data:`HANDOVER_SITES`) sends every completion site to
one of them, and tests/unit/import_budget/test_completion_coverage.py fails on
a site it does not. Two TABs stay unmeasured on purpose: a remote-path listing
miss, which contacts the host, and a reservation-cache miss, whose cost is the
backend's. See docs/architecture/subsystems/completion.md.

Usage:
    python scripts/import_budget.py            # print the per-surface table
    python scripts/import_budget.py --check    # enforce the ceilings; exit non-zero on a breach
    python scripts/import_budget.py --update   # rewrite every minor's ceilings file
    python scripts/import_budget.py --update --only KEY[,KEY...]   # only these surfaces' entries
    python scripts/import_budget.py --python PATH   # measure PATH's interpreter as it is
    python scripts/import_budget.py --report   # the table (an alias of no flag)
    python scripts/import_budget.py --report-json PATH   # the same, with full breakdowns, as JSON
"""

import argparse
import atexit
import contextlib
import errno
import fcntl
import functools
import importlib.util
import json
import os
import pty
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import asdict, dataclass, field
from pathlib import Path
from types import ModuleType

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

RATIO_FLOOR = "version_project"
"""The surface every :attr:`Surface.target_ratio` divides by: ``otto --version``.

The shim answers it without importing the CLI, so it is an interpreter start
plus ``otto._shim`` and ``otto.version`` against the same generated project: the
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

    project_files: int | None = None
    """Generate a project with this many NESTED test files and measure against it.

    None keeps the historical behaviour: no projects, otto-core only.
    """

    project_dirs_count: int = 1
    """Subdirectory count, scaled independently of ``project_files``: a stat-only
    walk is visible only per directory."""

    real_entry: bool = False
    """Run the console-script entry path instead of resolving a dispatch
    target. Required to observe bootstrap and cache behaviour, and it renders
    help the way a user sees it, which the dispatch-only surfaces skip on
    purpose."""

    warm: bool = False
    """Measure the SECOND run against one project and one ``OTTO_HOME``.

    Every measurement is otherwise COLD by construction: ``surface_env``
    mints a fresh ``OTTO_HOME`` per call so repeated measurements of one
    surface stay independent. That is right for the fallback path and wrong
    for the cached one — a cold ``--help`` MUST take the full load (miss →
    full bootstrap → collect → write), because cache-or-load never degrades
    help.
    A warm surface therefore runs the child TWICE against the same generated
    project and the same home: a discarded seed run, then the measured one.

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
    collected-tests tables (never a section), so ``test_project`` names its own
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

    expect_loaded: tuple[str, ...] = ()
    """Module roots a real-entry run must have imported (:func:`check_exit`).

    Each entry is a root: it is met by the module itself or any dotted child
    (:func:`loaded_root`). ``expect_exit`` alone cannot tell a refused
    connection from a command that never reached the network: an unknown host
    id also exits 1, with the same one-line error and no exception. A surface
    that fails by construction names a module only the path it measures
    imports, so a run that failed earlier cannot pass as the surface with a
    smaller count. A handover TAB names the module its completer lives in, so
    a TAB the shim answered by itself cannot pass as the handover."""

    expect_not_loaded: tuple[str, ...] = ()
    """Module roots a real-entry run must NOT have imported, matched as
    :attr:`expect_loaded` is. A shim surface sets :data:`SHIM_DENY`, so a TAB
    that fell through to the full CLI cannot pass as one the shim answered."""

    expect_answer: tuple[str, ...] = ()
    """Candidate lines the TAB's stdout must hold, each as a WHOLE line.

    Bash completion prints one candidate per line, so ``run`` is met by a
    ``run`` line and never by ``runner``. A TAB that answered nothing, or
    answered the wrong site, is a failure path with a smaller count."""

    expect_answer_templated: tuple[str, ...] = ()
    """Candidate lines, as :attr:`expect_answer`'s, that come from the templated lab.

    Required only when the measured interpreter loads a jsonnet binding
    (``otto.templating.binding.load_binding``, asked by :func:`warm_paths`):
    without one the lab never renders, so these lines are dropped from the
    verdict and the result's ``proofs_skipped`` names it
    (:data:`SKIPPED_TEMPLATED_ANSWER`). The counts are measured and gated
    all the same; the stat pass under test is the same either way."""

    expect_answer_exact: frozenset[str] | None = None
    """The exact set of candidate lines the TAB's stdout must hold, when set.

    For a site whose wrong answer would still contain the right lines (a
    past-separator site that also offered the values already typed)."""

    seed_env_remove: tuple[str, ...] = ()
    """Variable names removed from the SEED run's environment only; the measured run keeps them.

    For a surface whose measured env would make the seed do something else: a
    seed of ``otto --help`` with the completion variables still set would
    complete instead (``otto._shim``), and try a live listing."""

    post_seed: str | None = None
    """A key into :data:`POST_SEED_WRITERS`: the writer run after the seed, before the measured run.

    It fills a cache the seed cannot (a remote listing, the observed
    containers) through otto's own writer, in the surface's env
    (:func:`_run_post_seed`). A writer that fails or wrote nothing is the
    result's ``ending``, so the surface is never measured on an empty cache."""

    cache_unchanged: bool = False
    """``completion_cache.json`` under the surface's home must keep its mtime and size
    across the measured run, and so must its remote sidecar
    (``remote_completion_cache.json``), present or absent.

    A warm TAB that found the cache stale and rebuilt it would otherwise be
    measured, and baselined, as the warm path; a remote-path TAB that missed
    its cached listing and listed live writes the sidecar. A changed cache is
    the result's ``ending``."""

    stat_leg: bool = False
    """The measured run must take the shim's ``names`` stat leg, not the marker window's skip.

    The harness backdates the ``names`` marker after the seed (and the
    writer), and the run must touch it: only the stat pass does. A seed that
    left none (a cold seed TAB is answered by the full CLI, which writes no
    marker) leaves the run to take the stat leg on the missing marker, and
    the run must create it. A ``tests`` marker the seed left is touched
    instead, so a TAB that reads the test tables finds no refresh due and
    starts no collect child. A run that did not touch the ``names`` marker is
    the result's ``ending``."""

    ssh_lab: bool = False
    """Generate the project with a second lab source holding one SSH host on a closed port.

    See ``generate_project``'s ``ssh_lab_port``. The harness picks the port and
    proves it refuses a connection before every measured run, so the surface
    measures one refused attempt rather than whatever a stray listener says.
    """

    reservations: bool = False
    """Generate the project with ``[reservations]`` declared, on the ``json`` backend.

    See ``generate_project``'s ``reservations``. Per surface, so no other
    surface's project, and no other surface's ceiling, changes with it."""

    templated_lab: bool = False
    """Generate the project with a ``jsonnet`` lab source as well.

    See ``generate_project``'s ``templated_lab``: an entry, a library under its
    import root and the json inventory it renders hosts from."""

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

    The SSH surfaces' ratios include the ``ldconfig`` runs asyncssh's import
    makes to look for liboqs and nettle. ``otto.host._asyncssh`` holds that
    lookup to the linker cache, so no compiler or linker runs and the count
    does not depend on the machine's toolchain; a ``gcc`` or ``ld`` in those
    surfaces' child processes means an import of asyncssh got in ahead of
    that module."""


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
    expect_loaded: tuple[str, ...] = (),
    target_ratio: float | None = None,
    seed_argv: list[str] | None = None,
) -> Surface:
    """Build a surface measuring one real command, the way a user runs it day to day.

    Every verb surface shares one shape, so the numbers compare across verbs:
    the real console entry against a realistic generated project, WARM (seeded
    with root help, which writes the cache an ordinary command then finds
    beside it, as ``dispatch_project_warm`` explains), with ``OTTO_LAB`` naming
    the JSON lab the project declares so no command needs a real host.
    *seed_argv* replaces root help for a verb whose own warm state is
    something else (``test_project``).
    """
    return Surface(
        key,
        argv,
        project_files=50,
        project_dirs_count=5,
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


def _remote_hit_surface(
    key: str, *, post_seed: str, reservations: bool = False, deny: tuple[str, ...] = ()
) -> Surface:
    """Build a remote-path hit surface: ``otto host budget-ssh get <TAB>``, listing cached.

    Both share one shape and differ only in the project's ``[reservations]``,
    the writer that fills the sidecar, and what else the run must not import.
    ``COMP_CWORD=4`` is the empty word after ``get``: its first argument, the
    remote source path.
    """
    return Surface(
        key,
        ["otto"],
        project_files=50,
        project_dirs_count=5,
        real_entry=True,
        warm=True,
        ssh_lab=True,
        reservations=reservations,
        seed_argv=["otto", "--help"],
        seed_env_remove=("_OTTO_COMPLETE", "COMP_WORDS", "COMP_CWORD"),
        env_extra=(
            ("OTTO_LAB", "unix"),
            ("_OTTO_COMPLETE", "complete_bash"),
            TAB_HOST_TIMEOUT,
            ("COMP_WORDS", f"otto host {BUDGET_SSH_HOST} get "),
            ("COMP_CWORD", "4"),
        ),
        post_seed=post_seed,
        expect_loaded=("otto.cli.remote_completion",),
        expect_not_loaded=("asyncssh.connection", *deny),
        expect_answer=(REMOTE_HIT_ANSWER,),
        cache_unchanged=True,
    )


_GATED_VERBS = {"run", "test", "host"}
"""The verbs with a gated surface of their own above; every other registered verb is tracked."""

VERBS_ENV = "IMPORT_BUDGET_VERBS"
"""Names the first-party commands, comma-separated, when the measured otto cannot.

``scripts/import_budget_delta.py`` runs this harness under each side's own
otto, and an older release has no :func:`~otto.cli.builtin_commands.builtin_command_names`.
So the delta, which runs under the tip's otto, reads the list once and sets
this; both sides then measure the same tracked verbs."""


def first_party_commands() -> list[str]:
    """Return the first-party command names: :data:`VERBS_ENV` when set, else the registry's.

    The registry is imported here, not at module scope, so a harness handed the
    list never imports a helper the measured otto may not have. A variable that
    is set but names nothing is refused rather than read as "ask the registry".
    """
    named = os.environ.get(VERBS_ENV)
    if named is None:
        from otto.cli.builtin_commands import builtin_command_names

        return builtin_command_names()
    verbs = [verb.strip() for verb in named.split(",") if verb.strip()]
    if not verbs:
        raise ValueError(f"{VERBS_ENV} is set but names no command")
    return verbs


_TRACKED_VERBS = [verb for verb in first_party_commands() if verb not in _GATED_VERBS]
"""The top-level verbs with no gated surface of their own; each is measured at ``--help``.

Read off the CLI's own registry (by origin, so a command a test planted in
the same process is not one), so a new command is tracked the day it
is registered; ``scripts/import_budget_delta.py``'s ``KNOWN_SURFACES``
pins the resulting set literally. Under the delta the list arrives in
:data:`VERBS_ENV` instead, read from the tip's registry."""


SHIM_DENY: tuple[str, ...] = ("typer", "rich", "pydantic", "otto.cli", "otto.config")
"""The module roots a TAB the shim answers never imports: its surfaces' ``expect_not_loaded``.

The one list; ``otto._shim_complete``'s docstring points here. Typer vendors
click, so denying ``typer`` covers click, and ``click`` itself would prove
nothing. ``tests/unit/test_shim.py`` pins the shim's whole warm module set in
process; this list is what the measured surfaces enforce from outside."""

TAB_HOST_TIMEOUT: tuple[str, str] = ("OTTO_COMPLETION_HOST_TIMEOUT", "300")
"""Every TAB surface's ``env_extra`` entry lifting the host-enumeration deadline to 300 s.

The variable is ``otto.config.completion_cache.HOST_SUMMARY_DEADLINE_ENV_VAR``
(a test pins the name). A TAB surface's seed is a cold TAB or root help, so
the full CLI enumerates the hosts under strace against the 2 s default; a
missed deadline caches ``hosts: ["local"]``, and the measured TAB answers from
it. That fails the verdict, or moves the count, for a reason in the
preparation, not a regression. Reading the variable is no file operation.
``tests/unit/config/test_completion_templated.py`` lifts it the same way for
its recorded TABs."""

OBSERVED_PARENT = "test1"
"""The docker parent ``completion_docker_observed_warm`` completes containers on: a tech1 host."""

OBSERVED_CONTAINER = "otto-budget-observed-1"
"""The container name the ``docker_containers`` writer records, and that surface's TAB answers."""


def _generated_project() -> ModuleType:
    """Return ``tests._fixtures.generated_project``, where the generated project is defined.

    ``tests._fixtures`` is not importable from the script's own ``sys.path[0]``
    (``scripts/``), so a standalone ``python scripts/import_budget.py`` puts the
    repo root on the path for the import and takes it off again: merely
    importing this module never leaves ``sys.path`` changed. The fixture and
    what it imports are stdlib-only at module scope.
    """
    if str(REPO_ROOT) in sys.path:
        import tests._fixtures.generated_project as module
    else:
        sys.path.insert(0, str(REPO_ROOT))
        try:
            import tests._fixtures.generated_project as module
        finally:
            sys.path.remove(str(REPO_ROOT))
    return module


BUDGET_SSH_HOST: str = _generated_project().BUDGET_SSH_HOST
"""``ssh_lab``'s closed-port host, as the generated project names it: the SSH verbs' and
the remote-path TABs' host.

It is in ``unix`` and has ``term="ssh"``; an SSH verb is refused at its connect,
and a TAB that missed the cached listing would dial the same closed loopback port."""

REMOTE_HIT_ENTRIES: dict[str, bool] = {"budget-dir-a": True, "budget-file-b": False}
"""The entries (name -> is_dir) the remote-path writers cache for the home directory."""

REMOTE_HIT_ANSWER = "~/budget-dir-a/"
"""One line the remote-path TAB prints for :data:`REMOTE_HIT_ENTRIES`: the directory, slashed."""

_REMOTE_LISTING_BODY = f"""
import json
from datetime import datetime, timezone
from otto.cli.remote_completion import split_incomplete
from otto.config.remote_completion_cache import ListingEntry, cached_listing, store_listing

now = datetime.now(tz=timezone.utc)
directory = split_incomplete("").directory
entries = [ListingEntry(name=n, is_dir=d) for n, d in {REMOTE_HIT_ENTRIES!r}.items()]
store_listing({BUDGET_SSH_HOST!r}, directory, entries, now)
wrote = len(cached_listing({BUDGET_SSH_HOST!r}, directory, now) or [])
"""
"""Store the remote-path TAB's listing, keyed as the completer reads it, and read it back."""

_REMOTE_RESERVATIONS_BODY = f"""
import os
from otto.cli.remote_completion import _ChainParams, _required_for
from otto.config.remote_completion_cache import cached_reservation_ok, store_reservations
from otto.reservations.identity import resolve_username
from otto.reservations.protocol import Reservation

# The chain the measured TAB collects: no --holder, no --hop, no -I/-E, and the
# one lab OTTO_LAB names in the surface's env.
labs = [os.environ["OTTO_LAB"]]
chain = _ChainParams(host_id={BUDGET_SSH_HOST!r}, hop="", term=None, labs=labs, holder=None)
required = _required_for(chain)
if not required:
    raise RuntimeError("the lab requires no resource, so the gate would never read the cache")
username = resolve_username(chain.holder).username
rows = [Reservation(user=username, resource=r) for r in sorted(required)]
store_reservations(username, rows, now)
if cached_reservation_ok(username, required, now) is not True:
    wrote = 0
"""
"""Then cache a covering answer for the gate, from the gate's own username and requirement."""

_WROTE = """
print(json.dumps({"wrote": wrote}))
"""

POST_SEED_WRITERS: dict[str, str] = {
    # The listing `otto host budget-ssh get <TAB>` reads: `split_incomplete("")`
    # is the directory key the completer looks up (`~`).
    "remote_listing": _REMOTE_LISTING_BODY + _WROTE,
    # The same listing, then a cached reservation answer covering everything
    # the gate requires, so the gate answers from the sidecar and builds no
    # backend.
    "remote_listing_reserved": _REMOTE_LISTING_BODY + _REMOTE_RESERVATIONS_BODY + _WROTE,
    # The control's writer: the listing alone, on a project that declares
    # `[reservations]`, so the gate misses and builds the backend.
    "remote_listing_reserved_without_reservations": _REMOTE_LISTING_BODY + _WROTE,
    # The projects as discovery found them, as `otto docker`'s TAB readers
    # reach the cache (src/otto/cli/docker.py, `_discovered_projects`), then
    # read back through the reader those TABs share.
    "docker_containers": f"""
import json
from otto.config.completion_cache import read_docker_observed, record_docker_containers
from otto.config.project import discover

projects = discover().projects
record_docker_containers(projects, {OBSERVED_PARENT!r}, names=[{OBSERVED_CONTAINER!r}], ids=[])
found = read_docker_observed(projects, {OBSERVED_PARENT!r}).container_names
print(json.dumps({{"wrote": len(found)}}))
""",
}
"""Post-seed writer name -> the body of the child script that runs it (:attr:`Surface.post_seed`).

Each body calls otto's own writer, never hand-written JSON, then READS BACK
what it wrote through otto's own reader and prints one JSON line,
``{"wrote": <int>}``: the count the reader found, never what was attempted.
otto's writers return ``None`` and can no-op silently (an ephemeral
fingerprint, no projects), and a writer that wrote nothing would leave the
surface measuring a miss. The body runs in the measured venv with the
surface's env (:func:`_run_post_seed`), so it resolves the same home and
projects the measured run does."""

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
    # OTTO_* and OTTOPATH, so `ottopath` is empty, discovery finds zero projects, and a walk of
    # the workspace costs nothing on any of them. A guard that cannot observe a
    # defect cannot witness its fix either.
    #
    # The surfaces below carry a GENERATED project (deterministic by
    # construction, so they stay as host-independent as the rest of the table)
    # and — `bootstrap_project`, which drives the composition root directly, aside
    # — run the REAL entry path, so they observe bootstrap and cache
    # behaviour no other surface can see.
    #
    # `--version` is answered by `otto._shim` without importing the CLI, so
    # this surface is the floor every other command is compared against: an
    # interpreter start plus the shim and `otto.version`.
    Surface(
        "version_project",
        ["otto", "--version"],
        project_files=50,
        project_dirs_count=5,
        real_entry=True,
    ),
    # THE HELP PAIR. `help_project` is COLD — a fresh OTTO_HOME per measurement,
    # so it is the fallback path: cache miss → full bootstrap → collect →
    # write, which is what cache-or-load promises and must keep costing what
    # a complete answer costs. It is therefore also the surface that PROVES
    # THE HARNESS still finds a real project (its `workspace` count clears a
    # project-less run of the same command by every `names` key path), which is
    # why it stays in the table unchanged.
    #
    # `help_project_warm` is the same surface measured on its SECOND run against
    # one home, i.e. the cached path: root help resolves the command list
    # from the `names` section and never walks the corpus. The scaling test
    # keys on this one.
    Surface(
        "help_project", ["otto", "--help"], project_files=50, project_dirs_count=5, real_entry=True
    ),
    Surface(
        "help_project_warm",
        ["otto", "--help"],
        project_files=50,
        project_dirs_count=5,
        real_entry=True,
        warm=True,
    ),
    # `run_bootstrapped`'s REPO-BEARING SIBLING, and the pair is again the
    # measurement. That surface runs the composition root against an EMPTY
    # workspace — zero projects discovered — which is what isolates bootstrap's
    # own import graph from anything a workspace drags in, and it must keep
    # doing exactly that. This one runs the same root against a generated
    # project, so the work bootstrap does PER PROJECT (reading the project's settings,
    # putting its lib dirs on the path, importing its init tree) is charged to
    # a surface. Same argv for the same reason the original chose it: the
    # verbs bootstrap registers are `otto run`'s.
    Surface(
        "bootstrap_project",
        ["otto", "run", "--help"],
        bootstrap=True,
        project_files=50,
        project_dirs_count=5,
    ),
    # THE COMMON CASE: a real command, dispatched through the real entry with
    # a warm cache. Every other real-entry surface above is `--version`, root
    # help or a TAB, so until this existed nothing measured what `otto run Y`
    # pays before its own work starts — which is where a completion-cache
    # check and every project's test-file import were hiding. `-R` skips the
    # reservation check and OTTO_LAB names a JSON lab the generated project
    # declares, so the run contacts no host.
    #
    # `seed_argv=root --help`, NOT this surface's own argv: see
    # `Surface.seed_argv`. Root help is a real cache reader/writer, so it
    # leaves a real cache behind, and the measured run shows dispatch
    # ignoring a cache that is really there.
    Surface(
        "dispatch_project_warm",
        ["otto", "-R", "run", "noop"],
        project_files=50,
        project_dirs_count=5,
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
    # WARM, like `help_project_warm` and for the same reason: a cold cache means
    # a miss, and a miss is a full bootstrap by design — completion never
    # degrades to a wrong answer. The seed run leaves the `shim` section, and
    # the measured run is the shim's stat pass plus one JSON read: `otto._shim`
    # answers the TAB from the cache without ever importing `otto.cli`,
    # `otto.config`, typer, click, or rich. `expect_not_loaded` enforces that
    # (`SHIM_DENY`), and `expect_answer` that the answer is the command names.
    Surface(
        "completion_project_warm",
        ["otto"],
        project_files=50,
        project_dirs_count=5,
        real_entry=True,
        warm=True,
        env_extra=(
            ("_OTTO_COMPLETE", "complete_bash"),
            TAB_HOST_TIMEOUT,
            ("COMP_WORDS", "otto "),
            ("COMP_CWORD", "1"),
        ),
        expect_not_loaded=SHIM_DENY,
        expect_answer=("run",),
        cache_unchanged=True,
        stat_leg=True,
    ),
    # THE FALLBACK'S TAB COST. `otto tunnel remove <TAB>` (COMP_CWORD=3) is a
    # `live` site: the resolver hands over rather than answering, and the shim
    # falls through to the unchanged full CLI path. This is a DIFFERENT site
    # than `completion_project_warm`'s — that one completes top-level command
    # NAMES (COMP_CWORD=1), which never resolves a specific command's module;
    # this one resolves three levels deep into `tunnel remove`'s own argument
    # completer, which imports `otto.cli.tunnel` and, transitively, all of
    # `otto.tunnel`, `otto.project`, `otto.instruction_catalog`, `otto.host.daemon`,
    # and `otto.fleet`, `otto.labs.loader`, `otto.config.dependencies` — modules the top-level
    # site never touches. So "as cheap as the warm path" was never the bar
    # here; the shim's own part is a cache read, the stat pass, and a marker
    # touch, which always happen before it can decide to hand over.
    Surface(
        "completion_project_handover",
        ["otto"],
        project_files=50,
        project_dirs_count=5,
        real_entry=True,
        warm=True,
        env_extra=(
            ("_OTTO_COMPLETE", "complete_bash"),
            TAB_HOST_TIMEOUT,
            ("COMP_WORDS", "otto tunnel remove "),
            ("COMP_CWORD", "3"),
        ),
        # The handover reached the completer: a TAB the shim answered alone
        # would never import it.
        expect_loaded=("otto.cli.tunnel",),
        cache_unchanged=True,
    ),
    # A HOST TAB IN A TEMPLATED PROJECT: `otto -l unix+templated host <TAB>`,
    # with a jsonnet lab source beside the json one. The shim answers the host
    # ids from the `names` payload after the stat pass over its key paths,
    # which here include the jsonnet entry, its import root and what is under
    # it, and the inventory file: it evaluates nothing and reads no template,
    # library or inventory. `-l` names both labs so the answer holds a json
    # host (`test1`) and a rendered one (`tmpl-0`); the selection filters the
    # payload in memory and leaves the stat pass as it is. It stands for every
    # `payload`, `static` and `none` site (the sub-variant test covers the
    # rest).
    Surface(
        "completion_host_warm",
        ["otto"],
        project_files=50,
        project_dirs_count=5,
        real_entry=True,
        warm=True,
        templated_lab=True,
        env_extra=(
            ("_OTTO_COMPLETE", "complete_bash"),
            TAB_HOST_TIMEOUT,
            ("COMP_WORDS", "otto -l unix+templated host "),
            ("COMP_CWORD", "4"),
        ),
        expect_not_loaded=SHIM_DENY,
        expect_answer=("test1",),
        expect_answer_templated=("tmpl-0",),
        cache_unchanged=True,
        stat_leg=True,
    ),
    # A TEST-NAME TAB: `otto test T<TAB>`, answered by the shim from the
    # collected-tests tables (`table_view`) after the `names` stat pass. The
    # seed is the same TAB on a cold home: it hands over, and the full path
    # waits once for the collect child's whole-tree table, which leaves the
    # `tests` marker. `stat_leg` keeps that marker fresh, so the measured run
    # finds no check due and starts no collect child: `subprocess`, which only
    # starting one imports (`spawn_refresh`), is denied to prove it.
    Surface(
        "completion_test_warm",
        ["otto"],
        project_files=50,
        project_dirs_count=5,
        real_entry=True,
        warm=True,
        env_extra=(
            ("_OTTO_COMPLETE", "complete_bash"),
            TAB_HOST_TIMEOUT,
            ("COMP_WORDS", "otto test T"),
            ("COMP_CWORD", "2"),
        ),
        expect_not_loaded=(*SHIM_DENY, "subprocess"),
        expect_answer=("TestTop0",),
        cache_unchanged=True,
        stat_leg=True,
    ),
    # AN OBSERVED-STATE TAB: `otto docker logs --parent test1 <TAB>`, answered
    # by the shim from `__docker_observed__` after the `names` stat pass. No
    # seed can fill that namespace (only a docker verb that asked a daemon
    # does), so the `docker_containers` writer records one container through
    # otto's own writer after the seed.
    Surface(
        "completion_docker_observed_warm",
        ["otto"],
        project_files=50,
        project_dirs_count=5,
        real_entry=True,
        warm=True,
        env_extra=(
            ("_OTTO_COMPLETE", "complete_bash"),
            TAB_HOST_TIMEOUT,
            ("COMP_WORDS", f"otto docker logs --parent {OBSERVED_PARENT} "),
            ("COMP_CWORD", "5"),
        ),
        post_seed="docker_containers",
        expect_not_loaded=SHIM_DENY,
        expect_answer=(OBSERVED_CONTAINER,),
        cache_unchanged=True,
        stat_leg=True,
    ),
    # PAST THE SEPARATOR: `otto tunnel add --hosts test1,<TAB>`. The `hosts`
    # payload is `live_past_sep`, so the shim hands over once a host is typed,
    # and the completer runs host and address discovery and narrows to the
    # hosts on test1's /24 (src/otto/cli/tunnel.py, `_hosts_completer`). No
    # lab is selected, so the candidates are the whole fleet, and the exact
    # set is tech1's other hosts on 10.10.200.0/24. It leaves out every host
    # on another segment (`local`, the zephyr and busybox hosts), which the
    # completer's fallback to every host would offer, so a broken narrowing
    # cannot pass as this one.
    Surface(
        "completion_hosts_past_sep_handover",
        ["otto"],
        project_files=50,
        project_dirs_count=5,
        real_entry=True,
        warm=True,
        env_extra=(
            ("_OTTO_COMPLETE", "complete_bash"),
            TAB_HOST_TIMEOUT,
            ("COMP_WORDS", "otto tunnel add --hosts test1,"),
            ("COMP_CWORD", "4"),
        ),
        expect_loaded=("otto.cli.tunnel",),
        expect_answer_exact=frozenset({"test1,test2", "test1,test3", "test1,test4"}),
        cache_unchanged=True,
    ),
    # THE REMOTE-PATH HIT: `otto host budget-ssh get <TAB>` with the listing
    # cached. The site is `live`, so the shim hands over, and the full CLI
    # bootstraps the projects, runs the reservation gate (no `[reservations]`
    # here, so it returns at once), builds the lab and checks the host's term
    # before it reads the cached listing (src/otto/cli/remote_completion.py,
    # `_complete`). The seed is root help with the completion variables
    # removed: with them it would complete, and try a live listing. The
    # `remote_listing` writer caches the listing after it, and the sidecar
    # must not change across the run, so a TAB that listed live is a miss.
    # `asyncssh.connection` is denied: only a connect imports it.
    _remote_hit_surface("completion_remote_path_hit", post_seed="remote_listing"),
    # THE SAME HIT WITH RESERVATIONS: `[reservations]` declared on the json
    # backend, and the gate's answer cached by the writer, so the gate loads
    # the lab a second time (`_required_for`), resolves the username and reads
    # the sidecar. It builds no backend: `otto.reservations.registry`, which
    # only building one loads, is denied. The control
    # (`test_reserved_hit_without_reservations_fails_its_verdict`) caches no
    # answer and fails this verdict.
    _remote_hit_surface(
        "completion_remote_path_hit_reserved",
        post_seed="remote_listing_reserved",
        reservations=True,
        deny=("otto.reservations.registry",),
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
    # run: the seed is the same `otto test TestTop0`, which writes the project's
    # collected-tests table, so the measured run finds it current. Its
    # session collects the whole tree all the same, as every run does: the
    # table is a hint and never narrows a run (#592). Seeded with root help
    # instead, it would measure the first run in a fresh home, which also
    # seeds the table. 11.66: the whole-tree session's ratio on 3.10 (11.10x,
    # the highest minor; 9.98x on 3.14) plus 5%, as 9.46 was for the session
    # the table used to narrow to one file. The cost of a verdict pytest
    # would agree with (#592).
    _verb_surface(
        "test_project",
        ["otto", "test", "TestTop0"],
        target_ratio=11.66,
        seed_argv=["otto", "test", "TestTop0"],
    ),
    # The built-in `local` host stands in for the ~30 host subverbs that run a
    # shell command: every one is a `@cli_exposed` method on the same class.
    _verb_surface("host_local_exec", ["otto", "host", "local", "exec", "true"], target_ratio=4.54),
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
        ["otto", "host", BUDGET_SSH_HOST, "exec", "true"],
        ssh_lab=True,
        expect_exit=1,
        expect_loaded=("asyncssh.connection",),
        # Re-derived once asyncssh's import stopped running gcc and ld to
        # look for liboqs (#659): the largest ratio was 5.531 (3.11), so 5.6
        # plus 10%.
        target_ratio=6.16,
    ),
    # THE LOGIN PATH, on the same closed-port host, under a pty as a user's
    # terminal would be. Not the built-in `local` host: `LocalHost` implements
    # no interactive session, so its `login` refuses before connecting.
    # `budget-ssh` takes `UnixHost.run_login`'s SSH branch and is refused at the
    # connect that precedes the terminal bridge (`interact.run_ssh_login`
    # needs the connection), so the bridge module is counted as far as the
    # host module imports it, and the bridge itself never runs. It ends as
    # `host_ssh_exec` does.
    _verb_surface(
        "host_ssh_login",
        ["otto", "host", BUDGET_SSH_HOST, "login"],
        pty_input="exit\n",
        ssh_lab=True,
        expect_exit=1,
        expect_loaded=("asyncssh.connection",),
        target_ratio=6.16,
    ),
    # `dispatch_project_warm`'s own sibling, dispatching `local_true` instead of
    # `noop`: `local_true` opens a persistent `LocalHost` session and runs one
    # command through it (tests/_fixtures/generated_project.py), so this surface
    # is the one that can see what a session's own connection-lost choke
    # points cost. `dispatch_project_warm` never opens a host session at all, so
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

# WHICH SURFACE MEASURES EACH TAB COMPLETION SITE: the one site map.
# tests/unit/import_budget/test_completion_coverage.py enumerates every
# value-taking parameter of the serialised completion tree, through every host
# class's view (tests/_fixtures/completion_sites.py), and fails on a site
# neither map sends to a gated surface above. A site the shim answers maps by
# its source kind; a site it hands over (a `live` source, a list that goes live
# past its separator, `nargs > 1`) maps by its identity, one entry per host
# class that has it.

SITE_PROFILES: dict[str, str] = {
    "tests": "completion_test_warm",
    "markers": "completion_test_warm",
    "observed": "completion_docker_observed_warm",
    "payload": "completion_host_warm",
    "static": "completion_host_warm",
    "none": "completion_host_warm",
}
"""A shim-answered site's source kind to the surface its validation profile is
measured by. ``static``, ``none`` and ``markers`` cost what their representative
costs (tests/unit/shim/test_subvariant_cost.py). ``echo`` (a File parameter) has
no otto parameter today and so no entry: its first site fails the coverage test
until someone maps it."""

HANDOVER_SITES: dict[tuple[tuple[str, ...], str | None, str], str] = {
    (("tunnel", "remove"), None, "tunnel_id"): "completion_project_handover",
    (("tunnel", "add"), None, "hosts"): "completion_hosts_past_sep_handover",
    (("tunnel", "check"), None, "hosts"): "completion_hosts_past_sep_handover",
    # The remote paths: the union menu (an id with no known class) and `unix`,
    # the only builtin class whose transfer completes the far side.
    (("host", "get"), None, "src_files"): "completion_remote_path_hit",
    (("host", "get"), "unix", "src_files"): "completion_remote_path_hit",
    (("host", "put"), None, "dest_dir"): "completion_remote_path_hit",
    (("host", "put"), "unix", "dest_dir"): "completion_remote_path_hit",
}
"""Every site the shim hands over, keyed ``(command path, host class, parameter)``
(``None`` outside a class view), to the surface that measures that hand-over.
A scoped verb's path omits the host id: ``("host", "get")``."""

VARIANT_SURFACES: dict[str, str] = {
    # The same remote-path sites as the hit, under cached reservations.
    "completion_remote_path_hit_reserved": "completion_remote_path_hit",
    # The command and option menus, not a value site: the same warm shim pass
    # as the `names` profile.
    "completion_project_warm": "completion_host_warm",
}
"""A measured completion surface no site maps to, to the mapped surface it varies."""

# The payload still carries the module inventory (`modules`, `otto_modules`,
# `non_stdlib_modules`): nothing gates on it, but a failure is easier to read
# with it, and tests that ask "is this module loaded on this path" read it.
# `non_stdlib_modules` is total sys.modules minus the stdlib, classified via
# the *child's own* `_non_stdlib`, so each Python version self-classifies.

UNLISTED_STDLIB = frozenset({"_missing_stdlib_info"})
"""Stdlib modules ``sys.stdlib_module_names`` leaves out.

CPython 3.15 generates this one at build time, ``traceback`` imports it, and
the generated list omits it, so classifying by that list would call it foreign.
"""

NON_STDLIB_HELPER = f"""
import sys as _sys
def _non_stdlib(mods):
    stdlib = _sys.stdlib_module_names.union({sorted(UNLISTED_STDLIB)!r})
    return [m for m in mods if m.split(".")[0] not in stdlib]
"""
"""Child source defining ``_non_stdlib``, the one classification of a module as not stdlib."""

# Prepended to every child. It installs nothing: every file operation is
# counted from OUTSIDE the child, by strace, so the child must do no path work
# of its own beyond what otto does. That is why the fixture root is used as the
# raw string the harness passed in, never resolved: a `realpath` here would
# charge one `lstat` per path component to every surface.
_CHILD_PREAMBLE = (
    NON_STDLIB_HELPER
    + """
import os as _os
_fixture_root = _os.environ.get("IMPORT_BUDGET_FIXTURE_ROOT")
def _fixture_path_entries():
    if not _fixture_root:
        return 0
    return sum(1 for p in _sys.path
               if p == _fixture_root or p.startswith(_fixture_root + _os.sep))
"""
)


# Child script for `import otto` surface: bare import, no CLI invocation.
_CHILD_IMPORT_BODY = """
import sys, json
import otto
mods = sorted(sys.modules)
otto_mods = [m for m in mods if m == "otto" or m.startswith("otto.")]
non_std = _non_stdlib(mods)
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
# project — the sanitized env strips OTTO_* and OTTOPATH, `ottopath` then
# defaults to empty, and discovery finds zero projects — so the surface stays
# as host-independent and deterministic as the rest of the table while
# covering otto's own startup graph.
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
non_std = _non_stdlib(mods)
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
# produced it. `Project.add_libs_to_pythonpath` prepends each discovered project's
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
non_std = _non_stdlib(mods)
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
_CHILD_BASELINE = (
    NON_STDLIB_HELPER
    + """
import sys, json
print(json.dumps(_non_stdlib(sorted(sys.modules))))
"""
)


def _sanitized_env() -> dict[str, str]:
    """Return the measured child's env: :func:`venv_clean` less every OTTO_* var and OTTOPATH.

    Starting from :func:`venv_clean` means the measured children and the
    delta's sync drop the caller's venv and Python paths the same way. The
    OTTO_* strip makes the measurement lab/host independent. The measured
    interpreter's venv is then the active one (:func:`activated_env`), so a
    surface that starts a tool finds the measured venv's, not the harness's.
    An interpreter outside any venv gets no ``VIRTUAL_ENV`` at all.
    """
    # Spelled inline, not through otto's own env-var predicate: the delta runs
    # this harness under an older release's otto, which may not have it.
    env = {k: v for k, v in venv_clean().items() if not (k.startswith("OTTO_") or k == "OTTOPATH")}
    env.pop(VERBS_ENV, None)  # the harness's own input, never the measured otto's
    # The measured command must set this itself, as the `otto` command does.
    env.pop("PYDANTIC_DISABLE_PLUGINS", None)
    venv = venv_of(measured_python())
    if venv is None:
        return env
    return activated_env(env, venv)


# The measured venv: how one is made, found and entered. The delta
# (scripts/import_budget_delta.py) runs its own sync step with these same
# functions, so the sync command is decided here alone.


def _load_uv_env() -> "ModuleType":
    """Load scripts/uv_env.py from beside this file, by path.

    By path, not as ``scripts.uv_env``: the delta loads this harness by path
    from a tip worktree, where no ``scripts`` package is importable, and the
    copy beside it is the tip's own.
    """
    path = Path(__file__).resolve().parent / "uv_env.py"
    spec = importlib.util.spec_from_file_location("_import_budget_uv_env", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"import_budget: cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_UV_ENV = _load_uv_env()
UV_TARGET_VARS: "frozenset[str]" = _UV_ENV.UV_TARGET_VARS
"""What a uv command must not inherit (scripts/uv_env.py, its one home)."""
SYNC_TIMEOUT: int = _UV_ENV.SYNC_TIMEOUT
"""How long a ``uv sync`` may take (scripts/uv_env.py, its one home)."""
venv_clean: "Callable[[], dict[str, str]]" = _UV_ENV.venv_clean


def sync_argv(minor: str) -> list[str]:
    """Return the command that syncs a measured venv for Python *minor* from a tree's uv.lock.

    Run in the tree whose otto is measured, with :func:`sync_env`.
    ``--frozen`` installs exactly what the lock says, never a re-resolution.
    ``--no-default-groups`` makes the venv runtime-only, as a user's install
    is: a dev-only package (a pytest plugin above all, which pytest loads at
    startup) would otherwise count against every surface that starts pytest
    (#1150). The project itself stays installed, editable as uv installs it,
    so otto's own files still bucket as otto's.
    """
    return ["uv", "sync", "--frozen", "--no-default-groups", "--python", minor]


def sync_env(venv: Path) -> dict[str, str]:
    """Return the env :func:`sync_argv` runs in: venv-clean, aimed at *venv*."""
    env = venv_clean()
    env["UV_PROJECT_ENVIRONMENT"] = str(venv)
    return env


def activated_env(env: dict[str, str], venv: Path) -> dict[str, str]:
    """Return *env* with *venv* active: ``VIRTUAL_ENV`` set and its ``bin`` first on ``PATH``.

    Idempotent: when ``PATH`` already leads with that ``bin`` (``uv run``'s
    own venv measured as itself) it is not added twice, so the measured env
    is the one the caller already had.
    """
    bin_dir = str(venv / "bin")
    path = env.get("PATH", "")
    if path.split(os.pathsep, 1)[0] != bin_dir:
        path = f"{bin_dir}{os.pathsep}{path}"
    return dict(env, VIRTUAL_ENV=str(venv), PATH=path)


def venv_of(python: str) -> "Path | None":
    """Return the venv the interpreter at *python* belongs to, or ``None`` when it has none.

    A venv interpreter is :func:`venv_python`, a link that must not be
    resolved, and ``<venv>/pyvenv.cfg`` marks the venv, as the interpreter
    itself decides at startup.
    """
    venv = Path(python).parent.parent
    return venv if (venv / "pyvenv.cfg").is_file() else None


def venv_python(venv: Path) -> Path:
    """Return the interpreter of the venv at *venv*: ``<venv>/bin/python``."""
    return venv / "bin" / "python"


def find_interpreter(minor: str) -> "str | None":
    """Return the path of the interpreter uv would sync a *minor* venv from; ``None`` if none.

    Asked as the sync asks: venv-clean, and ``--system``, because ``uv python
    find`` would otherwise answer a ``.venv`` in the cwd or any parent (the
    checkout's own), which a venv the sync creates is never built from.
    """
    try:
        found = subprocess.run(  # noqa: S603 (fixed argv, no shell)
            ["uv", "python", "find", "--system", minor],  # noqa: S607 (`uv` via PATH)
            env=venv_clean(),
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None
    path = found.stdout.strip()
    return path if found.returncode == 0 and path else None


def default_venv(minor: str, tree: Path = REPO_ROOT) -> Path:
    """Return the measured venv :func:`build_env` makes for *minor* in *tree* by default.

    One per checkout and minor, gitignored, so parallel worktrees never share one.
    """
    return tree / ".venv-budget" / minor


def _left_behind(left: "list[Path]") -> str:
    """Return the clause naming staged files a failed write could not remove; empty when none."""
    if not left:
        return ""
    return "; staged files left behind: " + ", ".join(_display_path(p) for p in left)


class CeilingsStageError(OSError):
    """Staging the ceilings files failed, and some staged files could not be removed."""

    def __init__(self, cause: Exception, left: "list[Path]") -> None:
        self.left = left
        super().__init__(f"{cause}{_left_behind(left)}")


class CeilingsReplaceError(OSError):
    """Replacing a staged ceilings file failed, perhaps after earlier ones were replaced."""

    def __init__(
        self, replaced: "list[Path]", failed: Path, cause: OSError, left: "list[Path]"
    ) -> None:
        self.replaced, self.failed, self.left = replaced, failed, left
        names = ", ".join(_display_path(p) for p in replaced) or "none"
        super().__init__(
            f"replacing {_display_path(failed)} failed ({cause}); replaced: {names}"
            f"{_left_behind(left)}"
        )


class EnvBuildError(RuntimeError):
    """:func:`build_env` could not sync a measured venv."""


class NestedVenvLockError(RuntimeError):
    """This process asked again for a venv lock it holds, or built under one: a deadlock."""


@dataclass(frozen=True)
class HeldLock:
    """A venv lock this process holds (:func:`_venv_lock`): its file, and whether shared."""

    path: Path
    shared: bool


_HELD_LOCKS: "list[HeldLock]" = []
"""The venv locks this process holds now, innermost last (:func:`_venv_lock`)."""


_BUILT: "dict[str, Path]" = {}
"""Venv path -> its interpreter, for every venv :func:`build_env` synced in this process."""

_BUILD_FAILED: "dict[str, EnvBuildError]" = {}
"""Venv path -> the error its sync failed with in this process, re-raised on every later ask."""


def _venv_lock_path(venv: Path) -> Path:
    """Return the lock file that serializes syncing *venv* against measuring it: ``<venv>.lock``."""
    return venv.with_name(venv.name + ".lock")


def _venv_gate_path(venv: Path) -> Path:
    """Return the turnstile in front of *venv*'s lock (:func:`_venv_lock`): ``<venv>.lock.gate``."""
    lock = _venv_lock_path(venv)
    return lock.with_name(lock.name + ".gate")


# Not scripts/gate_stamp_common.store_lock: the delta loads this harness by path
# from a tip worktree, so it stays stdlib-only and reads no file outside
# INSTRUMENT_PATHS (itself and scripts/uv_env.py).
@contextlib.contextmanager
def _venv_lock(venv: Path, *, shared: bool) -> "Iterator[None]":
    """Hold *venv*'s lock, shared (a measurement) or exclusive (a sync), for the block.

    ``flock`` locks belong to the open file, so two processes, or two opens in
    one, exclude each other; the lock is gone when its holder dies, so a
    killed run never leaves the venv locked. Never nested: an exclusive
    second flock of this process on another open of the same lock waits
    for the first, which never lets go, and even a nested shared one would
    queue behind a sync waiting at the turnstile below. :data:`_HELD_LOCKS`
    records what is held, so asking again for a lock this process holds, or
    :func:`build_env` under any held lock, refuses by name instead of hanging.

    A turnstile (:func:`_venv_gate_path`) keeps a sync from starving: Linux
    ``flock`` grants a new shared lock while an exclusive one waits, so
    overlapping measurements (xdist workers) could hold the venv shared
    without a gap for as long as they keep coming. A sync holds the gate
    exclusively while it waits for the lock; a measurement passes through
    the gate (shared, let go at once) before it asks for the lock, so it
    queues behind a waiting sync, which gets the lock as soon as the
    measurements already inside finish.
    """
    lock = _venv_lock_path(venv)
    if any(held.path == lock for held in _HELD_LOCKS):
        raise NestedVenvLockError(
            f"import_budget: {lock} is already held by this process; "
            f"taking it again would wait on this process forever"
        )
    lock.parent.mkdir(parents=True, exist_ok=True)
    with lock.open("a") as handle:
        with _venv_gate_path(venv).open("a") as gate:
            fcntl.flock(gate.fileno(), fcntl.LOCK_SH if shared else fcntl.LOCK_EX)
            if shared:
                fcntl.flock(gate.fileno(), fcntl.LOCK_UN)
            fcntl.flock(handle.fileno(), fcntl.LOCK_SH if shared else fcntl.LOCK_EX)
        # Leaving the gate's block closes it, so a sync lets the gate go only
        # once it holds the lock.
        held = HeldLock(lock, shared)
        _HELD_LOCKS.append(held)
        try:
            yield
        finally:
            _HELD_LOCKS.remove(held)
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _measurement_lock() -> "contextlib.AbstractContextManager[None]":
    """Hold the measured venv's lock shared, when :func:`build_env` made that venv.

    Every child that runs the measured interpreter is launched under it
    (:func:`_run_child`, and the warm-up's compile), so no sync changes the
    venv's packages under a probe or a measurement. Reentrant within this
    process: when this process already holds that lock shared (a
    :func:`measure` around its children), it takes no second ``flock``. The
    interpreter is resolved (and, the first time, built) before any lock is
    taken, so the first build never runs under one.

    A venv with no lock file was not made by the builder (``uv run``'s own, or
    one the delta synced in a step of its own), so nothing can re-sync it
    concurrently and there is nothing to hold. That holds only while two
    things stay true: nothing deletes the lock file of a venv the builder
    made (it lives beside the venv and is never cleaned up on its own), and
    no builder is pointed at a venv made outside it, whose measurements
    would then run unlocked against the builder's sync.
    """
    venv = venv_of(measured_python())
    if venv is None or not _venv_lock_path(venv).exists():
        return contextlib.nullcontext()
    if HeldLock(_venv_lock_path(venv), shared=True) in _HELD_LOCKS:
        return contextlib.nullcontext()
    return _venv_lock(venv, shared=True)


def build_env(minor: str, *, tree: Path = REPO_ROOT, venv: "Path | None" = None) -> Path:
    """Sync the measured venv for Python *minor* from *tree*'s uv.lock; return its interpreter.

    *venv* defaults to :func:`default_venv`. The sync runs every time a
    process first asks, never skipped because the venv exists: ``uv sync
    --frozen`` on a current venv is a quick no-op, and on a stale one (a
    dependency bump since it was made) it is the update. After that the
    process reuses it (:data:`_BUILT`). The sync holds the venv's lock
    exclusively, so it waits for every live measurement of that venv
    (:func:`_measurement_lock`) and a concurrent builder waits for it; no
    half-synced venv is ever measured. Called while this process holds a
    venv lock, it raises :class:`NestedVenvLockError` rather than wait on
    itself forever. A sync that fails, or runs past :data:`SYNC_TIMEOUT`, is
    an :class:`EnvBuildError`, remembered for the process
    (:data:`_BUILD_FAILED`): a later ask re-raises it rather than sync again.
    """
    venv = default_venv(minor, tree) if venv is None else venv
    known = _BUILT.get(str(venv))
    if known is not None:
        return known
    failed = _BUILD_FAILED.get(str(venv))
    if failed is not None:
        raise failed
    if _HELD_LOCKS:
        raise NestedVenvLockError(
            f"import_budget: build_env({minor!r}) called while holding {_HELD_LOCKS[-1].path}; "
            f"syncing under it would wait on this process forever"
        )
    try:
        _sync(minor, tree, venv)
    except EnvBuildError as e:
        _BUILD_FAILED[str(venv)] = e
        raise
    python = venv_python(venv)
    _BUILT[str(venv)] = python
    return python


def _sync(minor: str, tree: Path, venv: Path) -> None:
    """Run :func:`sync_argv` for *venv* from *tree* under its exclusive lock; raise on failure."""
    argv = sync_argv(minor)
    with _venv_lock(venv, shared=False):
        try:
            done = subprocess.run(  # noqa: S603
                argv, cwd=tree, env=sync_env(venv), check=False, timeout=SYNC_TIMEOUT
            )
        except subprocess.TimeoutExpired as e:
            raise EnvBuildError(
                f"import_budget: `{' '.join(argv)}` timed out after {SYNC_TIMEOUT:g}s"
            ) from e
        except OSError as e:
            raise EnvBuildError(f"import_budget: `{' '.join(argv)}` could not run: {e}") from e
    if done.returncode != 0:
        raise EnvBuildError(
            f"import_budget: `{' '.join(argv)}` in {tree} for {venv} exited {done.returncode}"
        )


_STRACE_CALL = re.compile(r"^(\d+)\s+([a-z_0-9]+)\((.*)$")
_BYTECODE = re.compile(r"(?:__pycache__/)?([^/]+)\.cpython-3\d+(?:\.opt-\d+)?\.pyc$")
"""A ``.pyc`` name (in or out of ``__pycache__``): group 1 is its source module's stem."""


def _source_path(below: str) -> str:
    """Return *below* with a bytecode file named as its source, ``<dir>/<name>.py``.

    by_path only: the spec attributes a ``.pyc`` to the source it caches;
    buckets and totals read the path as opened.
    """
    return _BYTECODE.sub(r"\1.py", below)


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
    (:func:`measured_facts`), not from whichever one runs the harness.
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
        Measured on ``version_project``, 72 of its ~700 operations are stats of
        the stdlib directory alone. Such a stat names no module, so under
        ``otto`` and ``site`` it is charged to the pseudo-name ``*``.
        """
        return self._locate(path)[0]

    def relative(self, path: str) -> "tuple[str, str]":
        """Return the group :meth:`bucket` charges *path* to, and *path* below that group's root.

        The second item is ``""`` for a root directory itself, and the whole
        *path* for one under no root (``other``). It is what a per-path
        breakdown keys on, so two reads of ``os.py`` are one entry however
        the interpreter is installed.
        """
        name, root = self._locate(path)
        if root is None:
            return name, path
        return name, path[len(root) :]

    def _locate(self, path: str) -> "tuple[str, str | None]":
        """Return :meth:`bucket`'s group for *path* and the root it matched (``None``: no root).

        The one place the root order is decided, so :meth:`bucket` and
        :meth:`relative` cannot disagree.
        """
        root = _root_of(path, self.workspace)
        if root is not None:
            return "workspace", root
        if _root_of(path, [self.otto_src]) is not None:
            name = "otto:" + _first_component(path, self.otto_src).removesuffix(".py")
            return name, self.otto_src
        root = _root_of(path, self.site_packages)
        if root is not None:
            return "site:" + _import_name(_first_component(path, root)), root
        root = _root_of(path, self.stdlib)
        if root is not None:
            return "stdlib", root
        return "other", None


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
    by_path: dict[str, int] = field(default_factory=dict)
    """Calls per path, keyed ``<bucket>:<path below the bucket's root>`` (the
    whole path under ``other``; ``""`` for the root itself), or ``fd`` for a
    call that names no path. Bytecode-mirror paths are charged to the source
    they mirror. Report-only: no ceiling stores it."""


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
      system's per-import probes of a project's lib dirs.
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
        bucket, below = buckets.relative(path) if path else ("fd", "")
        ops.by_bucket[bucket] = ops.by_bucket.get(bucket, 0) + 1
        key = f"{bucket}:{_source_path(below)}" if path else "fd"
        ops.by_path[key] = ops.by_path.get(key, 0) + 1
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
    ``OTTO_HOME`` — and, when it has one, its generated project — reach the child.

    The child never inherits the caller's cwd; it starts in :func:`_quiet_cwd`.
    It runs under the measured venv's shared lock (:func:`_measurement_lock`).

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

    THE CHILD'S STDOUT RIDES A RESULT-FILE PAYLOAD, as ``stdout``
    (:func:`_with_stdout`): it is where a TAB's candidates are printed, which
    :func:`check_exit` reads, so the answer is judged from the measured run
    itself, with no second invocation.

    With *pty_input*, the child's stdin, stdout and stderr are a
    pseudo-terminal instead (:func:`_run_under_pty`), and *pty_input* is
    typed into it. Only the real-entry child is run this way, so its result
    still comes back through the file, with an empty ``stdout``: the terminal
    is not a channel this function keeps.
    """
    result_fd, result_name = tempfile.mkstemp(prefix="otto-budget-result-", suffix=".json")
    os.close(result_fd)
    result_path = Path(result_name)
    try:
        child_env = dict(_sanitized_env() if env is None else env)
        child_env[RESULT_PATH_ENV_VAR] = str(result_path)
        argv = [measured_python(), "-c", code]
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
        with _measurement_lock():
            if pty_input is not None:
                _run_under_pty(argv, child_env, pty_input)
                return _with_stdout(result_path.read_text(), "")
            out = subprocess.run(  # noqa: S603 (fixed interpreter + measured argv, no shell)
                argv,
                capture_output=True,
                text=True,
                check=True,
                env=child_env,
                cwd=_quiet_cwd(),
            )
        if result_path.stat().st_size > 0:
            return _with_stdout(result_path.read_text(), out.stdout)
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


def _with_stdout(payload: str, stdout: str) -> str:
    """Return the JSON object *payload* with *stdout* added under ``"stdout"``."""
    return json.dumps({**json.loads(payload), "stdout": stdout})


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
    """Parent directory for the temp ``OTTO_HOME``s of non-project surfaces.

    A project-bearing surface's home sits beside its generated project, inside the
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
    child an extra listing (``help_project_warm`` on 3.14, issue #428).

    Nothing writes here — the child writes only into its ``OTTO_HOME`` — so
    this directory's mtime never moves while a child is importing from it.
    Made ONCE per process and swept with the fixture roots.
    """
    root = Path(tempfile.mkdtemp(prefix="otto-budget-cwd-"))
    _FIXTURE_ROOTS.append(root)
    return root


@functools.cache
def _generated_repo_for(
    key: str,
    files: int,
    dirs: int,
    ssh_lab_port: int | None = None,
    templated_lab: bool = False,
    reservations: bool = False,
) -> Path:
    """Build (once per surface) a generated project and return its path.

    Keyed on the surface's NAME and shape rather than on the ``Surface`` itself:
    ``Surface.argv`` is a list, so the frozen dataclass is unhashable and cannot
    be an ``lru_cache`` key. Caching means repeated measurement of one surface —
    the script's table, then several tests — reuses one tree instead of writing
    a fresh corpus per call.
    """
    generate_project = _generated_project().generate_project
    root = Path(tempfile.mkdtemp(prefix=f"otto-budget-{key}-"))
    _FIXTURE_ROOTS.append(root)
    # realistic=True (spec §3.5 of `2026-09-25-dispatch-startup-cost-design.md`):
    # top-level suite files import pytest, the init module registers an
    # instruction and imports a monitor parser, and a JSON
    # lab source is declared — so every project-bearing surface's count shows
    # what a real project actually costs, rather than an empty init module nobody
    # ships.
    return generate_project(
        root,
        files=files,
        dirs=dirs,
        realistic=True,
        ssh_lab_port=ssh_lab_port,
        templated_lab=templated_lab,
        reservations=reservations,
    )


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
    """Return *surface*'s generated project, built once per process."""
    if surface.project_files is None:
        raise ValueError(f"surface {surface.key!r} carries no generated project")
    port = _budget_ssh_port() if surface.ssh_lab else None
    return _generated_repo_for(
        surface.key,
        surface.project_files,
        surface.project_dirs_count,
        port,
        surface.templated_lab,
        surface.reservations,
    )


def fixture_root(surface: Surface) -> Path:
    """Return the fixture root ``{root}`` names in *surface*'s argv: its project's parent."""
    return _surface_repo(surface).parent


ROOT_PLACEHOLDER = "{root}"
"""Stands for the surface's fixture root in an argv; see :data:`SURFACES`."""


def _uses_fixture_root(surface: Surface) -> bool:
    """Whether *surface*'s argv names paths under its fixture root."""
    return any(ROOT_PLACEHOLDER in arg for arg in surface.argv)


def _reset_transfer_fixture(root: Path) -> None:
    """Write the file a transfer surface moves, and empty the directories it moves it into.

    Reset before every measured run, because the project (and so the root) is
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
own ``sys.path`` entries came from the project under measurement.

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
    """Env for *surface*: sanitized, a FRESH OTTO_HOME, plus any generated project.

    ``OTTO_HOME`` is not optional, AND IT IS PINNED ON EVERY SURFACE, not only
    the project-bearing ones. ``workspace_home()`` resolves under ``otto_home()``
    = ``$OTTO_HOME`` else ``~/.otto``, and the sanitizer strips ``OTTO_*`` — so
    without injection a child reads (and, on the surfaces that run the real
    entry path, WRITES) the developer's real ``~/.otto``, whose contents are
    machine state. The ``workspace`` counter also attributes by prefix
    against this root, so an unpinned surface would charge its home reads to
    whatever that box keeps under ``~/.otto``. A non-project surface's home
    therefore comes from :func:`_home_parent_for_non_repo_surfaces` rather
    than from a fixture root it does not have.

    IT IS ALSO FRESH PER CALL, and bytecode writing is off, because REPEATED
    MEASUREMENTS OF ONE SURFACE MUST BE INDEPENDENT. Two caches otherwise warm
    between calls and make the first measurement in a process differ from every
    later one — deterministic but ORDER-DEPENDENT, and under ``-n auto`` with
    pytest-randomly, which call draws the cold number varies per run. That bias
    lands directly inside the corpus delta the gates assert.

    - The child writes ``completion_cache.json`` into its ``OTTO_HOME``, so the
      home is a fresh directory per call. For a project-bearing surface it stays
      inside the fixture root, so the atexit sweep still collects it.
    - The dominant one: importing the project's init module and its top-level test
      files writes ``__pycache__`` INTO THE FIXTURE TREE, which is cached and
      therefore shared across calls. Measured directly with the audit-hook
      open count this harness used before strace — 607, then 598, 598, 598;
      deleting the two ``__pycache__`` dirs returned it to exactly 607.
      ``PYTHONDONTWRITEBYTECODE`` keeps the harness from mutating
      the tree it is measuring, so every call is the cold number.

    Only these need to be fresh. The project tree stays cached: generating a
    200-file corpus per call is pure cost, and with no bytecode written it
    never warms.

    EVERY SURFACE READS BYTECODE FROM THE HARNESS'S OWN CACHE AND WRITES
    NONE (:func:`bytecode_prefix`), whatever the runner exports. A module
    whose ``.pyc`` is missing costs one more file operation than a cached one
    (the failed probe, then the source read), and a child that may write
    adds the write too, so the count otherwise follows whatever bytecode the
    machine happens to hold: measured on ``bootstrap_project`` under CPython
    3.10, deleting the venv's and ``src/otto``'s in-tree ``.pyc`` files, as a
    fresh CI checkout has them (``uv`` compiles none at install), moved
    ``file_ops`` from 2738 to 3030, over a 10% ceiling on unchanged product
    code. The cache is compiled before the first measurement
    (:func:`_warm_bytecode`) and no child writes to it, so every measurement
    sees the same warm cache: the steady state of any installation after its
    first run. The fixture project's own modules are never compiled, so they
    are cold on every run alike, and their probes are charged back to the
    workspace (:func:`parse_file_ops`).
    """
    env = _sanitized_env()
    if surface.project_files is not None:
        project = _surface_repo(surface)
        env["OTTOPATH"] = str(project)
        env[FIXTURE_ROOT_ENV_VAR] = str(project.parent)
        home_parent = project.parent
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


def baseline_modules() -> frozenset[str]:
    """Non-stdlib modules the bare measured interpreter already carries — never otto's doing.

    Site startup executes every ``.pth`` in site-packages, and legacy
    ``-nspkg.pth`` files (setuptools-era namespace packages) inject their
    package into ``sys.modules`` before user code runs. In this venv
    ``sphinxcontrib-jsmath`` does exactly that, so ``sphinxcontrib`` is present
    in ``python -c pass`` — nothing in otto imports it. ``_virtualenv`` and
    ``__main__`` arrive the same way. Charging these to otto would make the
    module inventory depend on which unrelated dev/docs dependencies happen
    to be installed, so they are measured and subtracted rather than counted.
    Asked once per measured interpreter (:data:`_BASELINES`).
    """
    python = measured_python()
    if python not in _BASELINES:
        _BASELINES[python] = frozenset(json.loads(_run_child(_CHILD_BASELINE)))
    return _BASELINES[python]


_BASELINES: "dict[str, frozenset[str]]" = {}
"""Interpreter path -> its :func:`baseline_modules`: keyed on the path, so a
switch of interpreter is never answered from another one's entry."""


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


_CHILD_FACTS = """
import json, os, site, sys, sysconfig
import otto
_paths = sysconfig.get_paths()
print(json.dumps({"site_packages": site.getsitepackages(),
                  "stdlib": [_paths["stdlib"], _paths["platstdlib"]],
                  "otto_src": os.path.dirname(os.path.realpath(otto.__file__)),
                  "import_dirs": [p for p in sys.path if p and os.path.isdir(p)],
                  "minor": "%d.%d" % sys.version_info[:2],
                  "cache_tag": sys.implementation.cache_tag}))
"""


_MEASURED: "str | None" = None
"""The interpreter :func:`use_python` chose; ``None``: the default (:func:`measured_python`)."""


def measured_python() -> str:
    """Return the interpreter every measured child runs, and every fact about one is asked of.

    The one :func:`use_python` chose, else the runtime-only venv
    :func:`build_env` syncs for the running interpreter's minor, built on the
    first call and kept for the process by :func:`build_env`'s own memo
    (:data:`_BUILT`). Every lock-taking caller asks for this before it locks
    (:func:`_measurement_lock` resolves it to find the venv), so the first
    build never runs under a held lock.
    Nothing measured may read the harness's ``sys.version_info`` or
    ``sys.implementation``: a harness on 3.10 measuring a 3.14 venv must key,
    bucket and warm as 3.14 (:func:`measured_facts`). Choosing the default
    venv's minor is the one place the harness's own version decides.
    """
    return _MEASURED or str(build_env(f"{sys.version_info[0]}.{sys.version_info[1]}"))


def use_python(path: str) -> None:
    """Measure with the interpreter at *path* from now on, forgetting every other one's facts.

    The per-interpreter caches are keyed on the path, so none could answer
    for the wrong interpreter; they are cleared anyway, so a process that
    measures several interpreters in turn holds only the current one's.
    """
    global _MEASURED  # noqa: PLW0603 — the seam's chooser
    _MEASURED = path
    _FACTS.clear()
    _BASELINES.clear()
    _WARMED.clear()


def measured_facts() -> dict:
    """Return the measured interpreter's minor, bytecode tag and paths.

    ``minor`` (``"3.14"``) keys its ceilings file, ``cache_tag`` names its
    ``.pyc`` files, and the paths bucket its file operations.

    Asked of the measured interpreter (:func:`measured_python`, in the
    sanitized env, exactly what every measured child runs), because the
    harness itself may run under another. Asked ONCE per interpreter
    (:data:`_FACTS`), in a child of its own that nobody
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
    python = measured_python()
    if python not in _FACTS:
        _FACTS[python] = json.loads(_run_child(_CHILD_FACTS))
    return _FACTS[python]


_FACTS: "dict[str, dict]" = {}
"""Interpreter path -> its :func:`measured_facts`, keyed like :data:`_BASELINES`."""


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


_WARMED: "set[str]" = set()
"""The interpreters :func:`_warm_bytecode` has compiled the cache for in this process."""


def _warm_bytecode() -> None:
    """Compile every module the measured interpreter can import into :func:`bytecode_prefix`.

    Once per process and measured interpreter (:data:`_WARMED`), before its
    first measurement, in a child nobody traces. The measured interpreter
    compiles it, because a ``.pyc`` is named for the compiler's own bytecode
    tag and the measured children look only for theirs. ``compileall`` skips
    a module whose cached ``.pyc`` is current, so a warm cache costs only its
    checks. Its exit status is not checked: a distribution may ship a file
    the measured interpreter cannot compile (test
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
    python = measured_python()
    if python in _WARMED:
        return
    prefix = bytecode_prefix()
    env = _sanitized_env()
    env.pop("PYTHONDONTWRITEBYTECODE", None)
    env["PYTHONPYCACHEPREFIX"] = prefix
    import_dirs = measured_facts()["import_dirs"]
    with _measurement_lock():
        done = subprocess.run(  # noqa: S603 (fixed interpreter + fixed module, no shell)
            [
                python,
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
    _WARMED.add(python)


def cached_bytecode(prefix: str, source: Path) -> Path:
    """Where the import system looks for *source*'s ``.pyc`` under the bytecode cache *prefix*.

    ``PYTHONPYCACHEPREFIX`` mirrors the source tree: the source's directory,
    rooted at the prefix, holding ``<name>.<cache tag>.pyc`` with no
    ``__pycache__`` level. The tag is the measured interpreter's.
    """
    return Path(prefix, *source.parent.parts[1:]) / (
        f"{source.stem}.{measured_facts()['cache_tag']}.pyc"
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
    by (:func:`measured_facts`).

    The measured venv's lock is held shared throughout
    (:func:`_measurement_lock`), so no :func:`build_env` re-syncs it mid-run.
    """
    if real_entry:
        code = _CHILD_ENTRY.format(argv=argv)
    elif argv[:1] == ["python"]:
        code = _CHILD_IMPORT
    else:
        code = _CHILD_CLI.format(argv=argv, bootstrap=bootstrap)
    with (
        _measurement_lock(),
        tempfile.TemporaryDirectory(prefix="otto-budget-strace-") as tmp,
    ):
        trace = Path(tmp) / "trace"
        result = json.loads(_run_child(code, env, trace_to=trace, pty_input=pty_input))
        text = trace.read_text(errors="replace")
        paths = measured_facts()
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


class PostSeedError(RuntimeError):
    """A surface's post-seed writer failed, or read back nothing (:func:`_run_post_seed`)."""


class WarmProofError(RuntimeError):
    """The seed left no cache for a warm proof to keep."""


def _run_post_seed(surface: Surface, env: dict[str, str]) -> None:
    """Run *surface*'s post-seed writer as a child of the measured interpreter, in *env*.

    Raises :class:`PostSeedError` when it exits non-zero, prints no JSON line or a
    malformed one, or reports ``wrote == 0`` (see :data:`POST_SEED_WRITERS` for the
    contract).
    """
    name = surface.post_seed
    if name not in POST_SEED_WRITERS:
        raise KeyError(f"no post-seed writer named {name!r} (surface `{surface.key}`)")
    try:
        report = json.loads(_run_child(POST_SEED_WRITERS[name], env))
    except subprocess.CalledProcessError as e:
        tail = (e.stderr or "").strip().splitlines()[-1:] or [f"exit {e.returncode}"]
        raise PostSeedError(f"the post-seed writer {name!r} failed: {tail[0]}") from e
    except RuntimeError as e:
        raise PostSeedError(f"the post-seed writer {name!r} reported nothing: {e}") from e
    except ValueError as e:  # a JSON-looking line that does not parse
        raise PostSeedError(f"the post-seed writer {name!r} printed a malformed report: {e}") from e
    wrote = report.get("wrote") if isinstance(report, dict) else None
    if not isinstance(wrote, int) or isinstance(wrote, bool) or wrote <= 0:
        raise PostSeedError(
            f"the post-seed writer {name!r} read back {wrote!r} entries, so the measured "
            f"run would find nothing it wrote"
        )


@dataclass(frozen=True)
class _CacheStamp:
    """``completion_cache.json``'s mtime and size: what :attr:`Surface.cache_unchanged` keeps."""

    mtime_ns: int
    size: int


def _cache_stamp(path: str) -> "_CacheStamp | None":
    try:
        st = Path(path).stat()
    except FileNotFoundError:
        return None
    return _CacheStamp(st.st_mtime_ns, st.st_size)


def _mtime_ns(path: str) -> "int | None":
    try:
        return Path(path).stat().st_mtime_ns
    except FileNotFoundError:
        return None


@dataclass(frozen=True)
class StatLegStamp:
    """The ``names`` marker a warm TAB must touch, and its mtime before (:func:`stage_stat_leg`)."""

    marker: str
    before_ns: int | None
    """``None`` when there was no marker: a cold seed writes none, and the TAB creates it."""


def stage_stat_leg(cache_dir: str) -> StatLegStamp:
    """Stage the warm TAB's stat leg in *cache_dir*, the cache's directory, and stamp it.

    The ``names`` marker is backdated past its window, so the TAB runs the stat
    pass instead of trusting it; the ``tests`` marker is freshened, so a TAB
    that reads the test tables finds no check due and starts no collect child.
    Each only if it exists, never created: a seed that wrote no ``tests``
    marker has a TAB that never reads the tables. The paths come from the
    shim's own ``marker_path``, so the caller's otto must have it.
    :func:`stat_leg_ending` checks the stamp after the TAB. The one staging:
    :func:`_prepare_proofs` and ``tests/_fixtures/recorded_tab.py`` share it.
    """
    from otto import _shim_complete as shim

    marker = shim.marker_path(cache_dir, "names")
    # A cold seed answers from the full CLI and writes no marker; the
    # measured run then finds none, takes the stat leg, and creates it.
    if Path(marker).exists():
        old = time.time() - 10 * shim.WINDOW_SECONDS
        os.utime(marker, (old, old))
    stamp = StatLegStamp(marker, _mtime_ns(marker))
    # Fresh, so a TAB that reads the test tables finds no refresh due; never
    # created, since a surface whose seed wrote none never reads them.
    tests = shim.marker_path(cache_dir, "tests")
    if Path(tests).exists():
        os.utime(tests, None)
    return stamp


def stat_leg_ending(stamp: StatLegStamp) -> "str | None":
    """Why the TAB after :func:`stage_stat_leg` skipped the stat pass; ``None`` when it took it.

    It took it when it created the ``names`` marker or touched it.
    """
    if _mtime_ns(stamp.marker) in (None, stamp.before_ns):
        return (
            f"the measured run did not touch the names marker ({stamp.marker}), "
            f"so it skipped the stat pass the surface measures"
        )
    return None


@dataclass(frozen=True)
class _WarmProofs:
    """What :func:`_prepare_proofs` recorded before the measured run, for :meth:`ending`."""

    cache: str
    cache_before: _CacheStamp | None
    stat_leg: StatLegStamp | None
    """The staged stat leg (:func:`stage_stat_leg`), ``None`` when the surface checks none."""
    skipped: list[str] = field(default_factory=list)
    """Each proof the measured tree's otto cannot support, with why: the result's
    ``proofs_skipped``. The counts are still measured and compared."""

    sidecar: str | None = None
    """The remote sidecar beside :attr:`cache`, kept with it (:attr:`Surface.cache_unchanged`)."""
    sidecar_before: _CacheStamp | None = None
    """Its stamp before the run, ``None`` when there was none: it must stay absent too."""

    def ending(self) -> "str | None":
        """Why the measured run is not the surface's warm leg; ``None`` when it is."""
        if self.cache_before is not None and _cache_stamp(self.cache) != self.cache_before:
            return (
                f"completion_cache.json changed across the measured run ({self.cache}), "
                f"so the TAB rebuilt a cache it found stale instead of serving it"
            )
        if self.sidecar is not None and _cache_stamp(self.sidecar) != self.sidecar_before:
            return (
                f"remote_completion_cache.json changed across the measured run "
                f"({self.sidecar}), so the TAB listed or queried live instead of "
                f"answering from it"
            )
        if self.stat_leg is not None:
            return stat_leg_ending(self.stat_leg)
        return None


_CHILD_WARM_PATHS = """
import json
from otto.config.completion_cache import _cache_path
from otto.config.remote_completion_cache import _path

cache, sidecar = _cache_path(), _path()
try:  # an otto from before the module (a base tag) renders no templated lab either
    from otto.templating.binding import load_binding

    load_binding()
    jsonnet = True
except Exception:
    jsonnet = False
print(json.dumps({"cache": cache and str(cache), "sidecar": sidecar and str(sidecar),
                  "jsonnet": jsonnet}))
"""
"""Print the cache and the remote sidecar as otto itself resolves them, and whether it
loads a jsonnet binding (:func:`warm_paths`)."""


@dataclass(frozen=True)
class WarmPaths:
    """Where the measured otto keeps the completion cache and its remote sidecar."""

    cache: str
    sidecar: str
    jsonnet: bool = True
    """Whether the measured interpreter loads a jsonnet binding, so a templated lab renders."""


def warm_paths(env: dict[str, str]) -> WarmPaths:
    """Ask the measured otto, in *env*, where its completion cache and remote sidecar live.

    One child of the measured interpreter, outside the measured run, printing
    ``completion_cache._cache_path()`` and ``remote_completion_cache._path()``:
    the harness never restates either location, and the answer is the
    measured tree's, not the harness interpreter's. Raises
    :class:`WarmProofError` when otto says the cache is off, or the child fails.
    """
    try:
        report = json.loads(_run_child(_CHILD_WARM_PATHS, env))
    except (subprocess.CalledProcessError, RuntimeError, ValueError) as e:
        raise WarmProofError(f"the measured otto did not name its cache paths: {e}") from e
    if not report.get("cache") or not report.get("sidecar"):
        raise WarmProofError("the completion cache is off in the surface's env: nothing to keep")
    return WarmPaths(report["cache"], report["sidecar"], jsonnet=report.get("jsonnet") is True)


SKIPPED_STAT_LEG = "stat_leg: marker_path unavailable at base"
"""The ``proofs_skipped`` note of a stat-leg proof whose measured otto has no ``marker_path``."""

SKIPPED_TEMPLATED_ANSWER = "templated answer: no jsonnet binding on this interpreter"
"""The ``proofs_skipped`` note of :attr:`Surface.expect_answer_templated` lines dropped from
the verdict, because the measured interpreter loads no jsonnet binding."""


def _prepare_proofs(surface: Surface, env: dict[str, str]) -> "_WarmProofs | None":
    """Backdate and snapshot what *surface*'s warm proofs check, after the seed and the writer.

    The cache and sidecar paths are the measured otto's own (:func:`warm_paths`,
    one child in the surface's env). The markers' come from the shim's
    ``marker_path``, imported from the harness interpreter's otto, which is
    the measured tree's under the delta and ``make import-snapshot``. An otto
    with no ``marker_path`` (a release from before it, under the delta) skips
    the stat-leg proof, noted in ``skipped``, and is still measured; the
    harness never restates the join itself. Raises :class:`WarmProofError`
    when the seed left no cache to keep.
    """
    if not (surface.cache_unchanged or surface.stat_leg or surface.expect_answer_templated):
        return None
    from otto import _shim_complete as shim

    paths = warm_paths(env)
    cache = paths.cache
    cache_before = None
    sidecar = sidecar_before = None
    if surface.cache_unchanged:
        cache_before = _cache_stamp(cache)
        if cache_before is None:
            raise WarmProofError(f"the seed left no completion_cache.json at {cache}")
        sidecar = paths.sidecar
        sidecar_before = _cache_stamp(sidecar)
    stat_leg = None
    skipped: list[str] = []
    if surface.expect_answer_templated and not paths.jsonnet:
        skipped.append(SKIPPED_TEMPLATED_ANSWER)
    if surface.stat_leg and not hasattr(shim, "marker_path"):
        skipped.append(SKIPPED_STAT_LEG)
    elif surface.stat_leg:
        stat_leg = stage_stat_leg(str(Path(cache).parent))
    return _WarmProofs(cache, cache_before, stat_leg, skipped, sidecar, sidecar_before)


def _ended_before_measuring(why: str) -> dict:
    """Return the :func:`measure_surface` result of a surface that ended before its measured run.

    Zero counts and no exit, so a report row still renders (the release
    delta lists it by *why*); :func:`check_exit` reports *why* alone.
    """
    return {
        "ending": why,
        "measured": False,
        "exit_code": None,
        "exception": None,
        "modules": [],
        "non_stdlib_modules": [],
        "stdout": "",
        "file_ops": asdict(FileOps()),
    }


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

    The one sequence: seed (in its env less :attr:`Surface.seed_env_remove`),
    the fixture reset and the closed-port proof, the post-seed writer
    (:attr:`Surface.post_seed`), the warm proofs' backdate and snapshot
    (:func:`_prepare_proofs`), the measured run, then the proofs' checks. A
    writer or proof that fails becomes the result's ``ending``, which
    :func:`check_exit` reports.
    """
    _warm_bytecode()
    env = surface_env(surface)
    argv = surface.argv
    if _uses_fixture_root(surface):
        root = env[FIXTURE_ROOT_ENV_VAR]
        argv = [arg.replace(ROOT_PLACEHOLDER, root) for arg in argv]
    if surface.warm:
        # The seed. Same child, same project, same home — so it performs exactly
        # the work the measured run would have had to, and leaves exactly the
        # cache the measured run reads. Its counts are discarded. The argv is
        # the surface's own UNLESS ``seed_argv`` overrides it — see
        # ``Surface.seed_argv`` for when the measured argv cannot seed itself.
        seed_env = {k: v for k, v in env.items() if k not in surface.seed_env_remove}
        measure(
            surface.seed_argv or argv,
            bootstrap=surface.bootstrap,
            real_entry=surface.real_entry,
            env=seed_env,
        )
    # Immediately before the measured run, after the seed: the seed never
    # transfers or connects, and these must hold for the run that counts.
    if _uses_fixture_root(surface):
        _reset_transfer_fixture(Path(env[FIXTURE_ROOT_ENV_VAR]))
    if surface.ssh_lab:
        _prove_port_closed(_budget_ssh_port())
    seeded_at = None
    try:
        if surface.post_seed is not None:
            _run_post_seed(surface, env)
            seeded_at = time.monotonic()
        proofs = _prepare_proofs(surface, env)
    except (PostSeedError, WarmProofError) as e:
        return _ended_before_measuring(str(e))
    result = measure(
        argv,
        bootstrap=surface.bootstrap,
        real_entry=surface.real_entry,
        env=env,
        pty_input=surface.pty_input,
    )
    if seeded_at is not None:
        # What the cached state aged by before the run finished reading it:
        # :func:`check_exit` names an expired listing (:func:`_listing_expiry`).
        result["seeded_age_seconds"] = time.monotonic() - seeded_at
    ending = proofs.ending() if proofs is not None else None
    if ending is not None:
        result["ending"] = ending
    if proofs is not None and proofs.skipped:
        result["proofs_skipped"] = proofs.skipped
    return result


class UnmeasuredSurfaceError(RuntimeError):
    """A count was asked of a result that names an ``ending`` (:func:`measure_file_ops`)."""


def measure_file_ops(surface: Surface) -> FileOps:
    """Measure *surface* and return only its file operations.

    Raises :class:`UnmeasuredSurfaceError` when the result names an ``ending``:
    its counts are not the surface's (zeros, when it ended before the measured
    run), and a caller that reads counts alone would compare them as if they were.
    """
    result = measure_surface(surface)
    ending = result.get("ending")
    if ending is not None:
        raise UnmeasuredSurfaceError(f"`{surface.key}`: {ending}")
    return FileOps(**result["file_ops"])


def interpreter_tag() -> str:
    """Return the measured interpreter's ``major.minor``, which keys a ceilings file.

    File-operation counts are only comparable WITHIN one Python minor: the
    interpreter's own stdlib and import machinery are part of every count, and
    they change release to release.
    """
    return measured_facts()["minor"]


def ceilings_path(minor: str) -> Path:
    """Path to the ceilings file for Python *minor* (``major.minor``)."""
    return CEILINGS_DIR / f"{minor}.json"


_MINOR_STEM = re.compile(r"^\d+\.\d+$")
"""A ceilings file's stem: ``major.minor``."""


def ceilings_minors() -> list[str]:
    """Return every minor that has a ceilings file, oldest first: what ``--update`` regenerates.

    Any other ``.json`` in the directory is refused by name: ``--update``
    would otherwise either skip it silently or measure a "minor" uv cannot name.
    """
    paths = sorted(CEILINGS_DIR.glob("*.json"))
    stray = [path for path in paths if not _MINOR_STEM.match(path.stem)]
    if stray:
        names = ", ".join(_display_path(path) for path in stray)
        raise ValueError(
            f"import_budget: {names} in the ceilings directory is not named <major>.<minor>.json"
        )
    minors = [path.stem for path in paths]
    return sorted(minors, key=lambda m: tuple(int(part) for part in m.split(".")))


SNAPSHOT_HOW = (
    "`make import-snapshot`, which regenerates every minor that has a file in "
    "tests/unit/import_budget/ceilings/ (a new minor first needs a file there: "
    "any content, the regeneration replaces it)"
)
"""How to regenerate the ceilings, for every message that asks for it."""


def read_ceilings() -> dict[str, dict]:
    """Return the measured interpreter's baselines, keyed by surface.

    Each value holds ``file_ops``, ``workspace``, ``by_bucket`` and
    ``by_process``. A missing file reads as no baselines at all, which
    :func:`check_surface` turns into a NAMED failure per gated surface rather
    than a skip. A file that is present but wrong fails by name here: one
    recorded under another interpreter (a copied or renamed file), or an
    entry missing a gated counter (a hand edit), would otherwise gate
    against the wrong numbers or end in a bare ``KeyError``.
    """
    tag = interpreter_tag()
    path = ceilings_path(tag)
    if not path.exists():
        return {}
    data = json.loads(path.read_text())
    if data.get("interpreter") != tag:
        raise ValueError(
            f"import_budget: {_display_path(path)} records interpreter "
            f"{data.get('interpreter')!r}, not {tag!r}; regenerate it with {SNAPSHOT_HOW}"
        )
    surfaces = data["surfaces"]
    for key, baseline in surfaces.items():
        missing = [name for name in CEILING_COUNTERS if name not in baseline]
        if missing:
            raise ValueError(
                f"import_budget: the baseline for `{key}` in {_display_path(path)} has no "
                f"{', '.join(missing)}; regenerate the file with {SNAPSHOT_HOW}"
            )
    return surfaces


def write_ceilings(minor: str, results: dict[str, FileOps]) -> None:
    """Write *results* as Python *minor*'s baselines (:func:`write_ceilings_files` for one)."""
    write_ceilings_files({minor: results})


# Not scripts/gate_stamp_common.write_atomically: the delta loads this harness by
# path from a tip worktree, so it stays stdlib-only and reads no file outside
# INSTRUMENT_PATHS (itself and scripts/uv_env.py).
def write_ceilings_files(
    results: "dict[str, dict[str, FileOps]]",
    kept: "dict[str, dict[str, dict[str, object]]] | None" = None,
) -> None:
    """Write each minor's ceilings file from *results* (minor -> its measurements).

    *kept* (minor -> surface -> its stored entry) is what a keys-scoped
    update (``--only``) carries over from each file as it was: those entries
    are written back as they were read, under the measured ones.

    Every file's content is staged first, each in a file of its own in
    :data:`CEILINGS_DIR` (``.<minor>.json.staged``, never read as a minor's
    file); only once all are staged is each moved over its file with
    ``Path.replace`` (``rename(2)``), so each file is replaced atomically: a
    reader sees the old file or the new one, never part of one. A failure
    while staging (an unknown surface, a full disk) raises before any file
    is replaced. A failure while replacing raises
    :class:`CeilingsReplaceError`, which names the files already replaced
    and the one that failed. Either way the staged files left are removed,
    and a removal that fails never hides the first error: the files it
    could not remove are named in it (:class:`CeilingsStageError` for a
    staging failure).
    """
    CEILINGS_DIR.mkdir(parents=True, exist_ok=True)
    staged: "list[tuple[Path, Path]]" = []
    try:
        for minor, measured in results.items():
            text = _ceilings_text(minor, measured, (kept or {}).get(minor))
            tmp = CEILINGS_DIR / f".{minor}.json.staged"
            staged.append((tmp, ceilings_path(minor)))
            tmp.write_text(text)
    except (OSError, KeyError) as e:
        left = _remove_staged([tmp for tmp, _target in staged])
        if left:
            raise CeilingsStageError(e, left) from e
        raise
    replaced: "list[Path]" = []
    for index, (tmp, target) in enumerate(staged):
        try:
            tmp.replace(target)
        except OSError as e:
            left = _remove_staged([tmp for tmp, _target in staged[index:]])
            raise CeilingsReplaceError(replaced, target, e, left) from e
        replaced.append(target)


def _remove_staged(staged: "list[Path]") -> "list[Path]":
    """Remove every staged file in *staged*; return the ones that could not be removed."""
    return [tmp for tmp in staged if not _unlinked(tmp)]


def _unlinked(path: Path) -> bool:
    """Remove *path* if it exists; return False, never raise, when the removal fails."""
    try:
        path.unlink(missing_ok=True)
    except OSError:
        return False
    return True


def _ceilings_text(
    minor: str, results: dict[str, FileOps], kept: "dict[str, dict[str, object]] | None" = None
) -> str:
    """Return the ceilings file for Python *minor* from *results*, for the gated surfaces only.

    The file stores BASELINES, not ceilings: :func:`ceiling` is applied at
    check time, so a headroom change never needs a regeneration. The
    breakdowns are stored beside the totals so a breach can say which
    package or child process grew (:func:`breakdown_diff`). A tracked surface
    is never written, because nothing reads its baseline. The whole file is
    replaced: a regeneration records one interpreter's measurement, not a
    merge with an older one, unless *kept* names the entries a keys-scoped
    update carries over (:func:`write_ceilings_files`). *minor* names the
    interpreter that measured *results*; the caller knows it, this function
    does not ask.
    """
    surfaces = dict(kept or {})
    surfaces |= {
        key: {
            "file_ops": ops.total,
            "workspace": ops.workspace,
            "by_bucket": ops.by_bucket,
            "by_process": ops.by_process,
        }
        for key, ops in results.items()
        if not surface_by_key(key).tracked
    }
    payload = {"interpreter": minor, "surfaces": surfaces}
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


CEILING_COUNTERS: dict[str, str] = {"file_ops": "total", "workspace": "workspace"}
"""The gated counters: each baseline key, and the :class:`FileOps` field it bounds."""

BREAKDOWN_LINES = 12
"""The most lines :func:`breakdown_diff` prints; ``--report-json`` has every group."""


DENIED_NAMED = 8
"""How many denied modules a :func:`check_exit` violation names before it counts the rest."""


def loaded_root(module: str, roots: tuple[str, ...]) -> bool:
    """Return whether *module* is one of *roots* or a dotted child of one (``typer.core``)."""
    return any(module == root or module.startswith(root + ".") for root in roots)


def check_exit(surface: Surface, result: dict) -> list[str]:
    """Return why a real-entry run is not the surface it measured (empty = pass).

    The one validity verdict: :func:`check_surface`, ``--update``
    (:func:`_measure_table`) and every report row (:func:`report_row`, which
    the delta reports read) all take it from here.

    The ending: the exit code must be ``expect_exit``, and no exception may
    have escaped the entry: every surface, the failing ones included, measures
    a command that ends as a user's does, and an uncaught exception is a
    rendered traceback, a cost of its own. The message always quotes the
    recorded exception, so a crash names its cause without a re-run.

    The path: every root in ``expect_loaded`` must have been imported, and
    none in ``expect_not_loaded`` (:func:`loaded_root`).

    The answer: every ``expect_answer`` line, and with ``expect_answer_exact``
    exactly that set of lines, in the child's ``stdout``, where a TAB prints
    one candidate per line.

    The warm sequence's own verdict, first: :func:`measure_surface`'s
    ``ending`` (a post-seed writer that failed, a cache the run changed, a
    marker it left untouched). One that ended before the measured run is the
    only violation, since nothing else was measured.
    """
    violations: list[str] = []
    ending = result.get("ending")
    if ending is not None:
        violations.append(f"`{surface.key}`: {ending}; it is not the surface")
        if result.get("measured") is False:
            return violations
    if not surface.real_entry:
        return violations
    exit_code = result.get("exit_code", 0)
    exception = result.get("exception")
    modules = result.get("modules", [])
    missing = [r for r in surface.expect_loaded if not any(loaded_root(m, (r,)) for m in modules)]
    if exit_code != surface.expect_exit or exception is not None or missing:
        expected = f"exit {surface.expect_exit} with no exception" + (
            f" having imported {', '.join(surface.expect_loaded)}" if surface.expect_loaded else ""
        )
        violations.append(
            f"`{surface.key}`: the measured command exited {exit_code} with "
            f"{exception or 'no exception'}, not {expected}, so it measured a failure "
            f"path, not the surface"
        )
    denied = [m for m in modules if loaded_root(m, surface.expect_not_loaded)]
    if denied:
        named = ", ".join(denied[:DENIED_NAMED]) + (
            f" and {len(denied) - DENIED_NAMED} more" if len(denied) > DENIED_NAMED else ""
        )
        violations.append(
            f"`{surface.key}`: the measured command imported {named}, under a "
            f"denied root ({', '.join(surface.expect_not_loaded)}), so it took a "
            f"different path than the surface"
        )
    # A blank line is no candidate: a TAB with nothing to offer prints one.
    lines = {line for line in result.get("stdout", "").splitlines() if line}
    expected = list(surface.expect_answer)
    if SKIPPED_TEMPLATED_ANSWER not in result.get("proofs_skipped", []):
        expected += surface.expect_answer_templated
    absent = [line for line in expected if line not in lines]
    if absent:
        violations.append(
            f"`{surface.key}`: the TAB's answer lacks the candidate line(s) "
            f"{', '.join(map(repr, absent))}; it answered {sorted(lines)!r}"
        )
    exact = surface.expect_answer_exact
    if exact is not None and lines != exact:
        violations.append(
            f"`{surface.key}`: the TAB answered {sorted(lines)!r}, not exactly {sorted(exact)!r}"
        )
    expiry = _listing_expiry(surface, result) if violations else None
    if expiry is not None:
        violations.append(f"`{surface.key}`: {expiry}")
    return violations


_REMOTE_LISTING_WRITERS = frozenset(
    name for name, body in POST_SEED_WRITERS.items() if body.startswith(_REMOTE_LISTING_BODY)
)
"""The post-seed writers that cache a remote listing: every one built on its body."""


def _listing_expiry(surface: Surface, result: dict) -> "str | None":
    """Why a failed remote-path verdict may be the clock's: the seeded listing aged past its TTL.

    A declared limitation, not a clock seam: the listing is stamped when the
    writer runs, and the TAB trusts it for ``LISTING_TTL_SECONDS`` (read from
    otto). Measured margins are over 25 times, and an expired listing fails
    closed (the TAB lists live and the proofs catch it); this only names the
    cause, so an overloaded machine is not read as a regression.
    """
    if surface.post_seed not in _REMOTE_LISTING_WRITERS:
        return None
    age = result.get("seeded_age_seconds")
    from otto.config.remote_completion_cache import LISTING_TTL_SECONDS

    if age is None or age < LISTING_TTL_SECONDS:
        return None
    return (
        f"the seeded listing expired before the TAB read it ({age:.1f} s elapsed): "
        f"the machine was overloaded, not a regression"
    )


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

      1. its validity (:func:`check_exit`) — a ``real_entry`` surface must exit
         ``surface.expect_exit`` with no exception escaping, have imported
         every ``surface.expect_loaded`` root and none of
         ``surface.expect_not_loaded``, and print the answer it expects, or
         it measured a failure path rather than the surface: a misspelled
         host id exits 1 too, and its smaller count would read as a saving;
      2. its baseline — no entry for this surface in the MEASURED
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
            f"{_display_path(ceilings_path(tag))}. Every interpreter that runs this "
            f"check needs its own file (the counts are not comparable across minors). "
            f"Regenerate it with {SNAPSHOT_HOW}.\n"
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
        tag = interpreter_tag()
        violations.append(
            f"`{surface.key}`: over its ceiling on CPython {tag} "
            f"({_display_path(ceilings_path(tag))}): {'; '.join(breaches)}. If the growth "
            f"is intended, regenerate with {SNAPSHOT_HOW}, and say why in the "
            f"commit.{growth}"
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
            tag = interpreter_tag()
            notes.append(
                f"`{surface.key}`: {name} {value} is below {STALE_RATIO:g}x its ceiling "
                f"{limit} (baseline {base}) on CPython {tag}. A stale-high "
                f"baseline widens the ceiling silently; consider regenerating "
                f"{_display_path(ceilings_path(tag))} with {SNAPSHOT_HOW}."
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
    """One ``--report`` row: *surface*'s file operations out of a measurement *result*.

    ``ending`` is :func:`check_exit`'s verdict on the run (empty when it ended
    as the surface expects), so a reader of the JSON can tell a run that
    measured the surface from one that failed earlier with the same exit.
    ``proofs_skipped`` names each warm proof the measured otto could not
    support (:func:`_prepare_proofs`): measured and compared all the same.
    """
    ops = result["file_ops"]
    return {
        "surface": surface.key,
        "tier": "tracked" if surface.tracked else "gated",
        "file_ops": ops["total"],
        "workspace": ops["workspace"],
        "exit": result.get("exit_code"),
        "ending": check_exit(surface, result),
        "proofs_skipped": list(result.get("proofs_skipped", [])),
        "by_bucket": ops["by_bucket"],
        "by_process": ops["by_process"],
        "by_path": ops["by_path"],
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


def _measure_table(
    surfaces: list[Surface], *, check: bool, update: bool
) -> "tuple[list[dict], dict[str, FileOps], bool]":
    """Measure *surfaces* with the measured interpreter, printing the table as it goes.

    Returns the report rows, each surface's file operations, and whether any
    surface failed: under *check* its ceilings and ratio, under *update* its
    ending, since a baseline recorded from a failure path would pin the cost
    of a crash.
    """
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
        if check:
            floor = measured.get(RATIO_FLOOR)
            violations = check_surface(s, r, floor_file_ops=floor.total if floor else None)
        elif update:
            violations = check_exit(s, r)
        for v in violations:
            print(f"  FAIL {v}", flush=True)
        for skipped in rows[-1]["proofs_skipped"]:
            print(f"  NOTE `{s.key}`: proof skipped ({skipped})", flush=True)
        if check:
            # Advisory only: printed, never counted toward `failed`.
            for note in advisories(s, r):
                print(f"  NOTE {note}", flush=True)
        failed = failed or bool(violations)
    return rows, measured, failed


def _nothing_written(why: str) -> int:
    print(f"\nimport budget: FAILED — {why}; nothing written.", flush=True)
    return 1


def _scoped(surfaces: list[Surface], only: "list[str]") -> list[Surface]:
    """Return what a keys-scoped update measures: *only*'s surfaces, and the floor one needs.

    :data:`RATIO_FLOOR` comes first when a named surface has a
    ``target_ratio``, so every ratio has its floor from the same run; it is
    measured, never written, unless it is named. Raises ``KeyError`` for a
    key no surface has, ``ValueError`` for a tracked one, which has no
    baseline to record.
    """
    tracked = [key for key in only if surface_by_key(key).tracked]
    if tracked:
        raise ValueError(
            f"--only: {', '.join(tracked)} is tracked, and a tracked surface has no baseline"
        )
    named = [s for s in surfaces if s.key in only]
    wanted = set(only)
    if any(s.target_ratio is not None for s in named):
        wanted.add(RATIO_FLOOR)
    return sorted((s for s in surfaces if s.key in wanted), key=lambda s: s.key != RATIO_FLOOR)


_KEEPS = "a keys-scoped update keeps every other entry, but"
"""How a refusal to merge into a ceilings file starts (:func:`_stored_entries`)."""


def _stored_entries(minor: str) -> "dict[str, dict[str, object]]":
    """Every entry in *minor*'s ceilings file as it is: what a keys-scoped update keeps.

    Raises ``ValueError`` for a file that is not one minor's ceilings (any
    content a new minor's file may hold), since nothing in it can be kept, and
    for one that is not in the form :func:`_ceilings_text` writes: rewriting it
    would move the kept entries' bytes, where on a canonical file they stay
    byte-identical.
    """
    path = ceilings_path(minor)
    text = path.read_text()
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise ValueError(f"{_KEEPS} {_display_path(path)} is not JSON ({e})") from e
    if not isinstance(data, dict) or data.get("interpreter") != minor:
        raise ValueError(f"{_KEEPS} {_display_path(path)} is not CPython {minor}'s ceilings")
    entries = dict(data.get("surfaces", {}))
    if text != _ceilings_text(minor, {}, entries):
        raise ValueError(
            f"{_KEEPS} {_display_path(path)} is not in canonical form (as --update writes "
            f"it), so its kept entries would not stay byte-identical; regenerate it with "
            f"{SNAPSHOT_HOW}"
        )
    return entries


def _update_every_minor(surfaces: list[Surface], *, only: "list[str] | None" = None) -> int:
    """Regenerate every minor's ceilings file, each measured in the venv :func:`build_env` makes.

    Every minor's interpreter is looked up before anything is built, and
    nothing is written unless every minor measured clean. Then every file is
    staged and each replaced atomically (:func:`write_ceilings_files`); a
    failed replacement names what was already replaced.

    With *only* (``--only``), just those surfaces are measured (:func:`_scoped`)
    and their entries written into each file, every other entry kept as it was
    stored; a file that is not its minor's ceilings refuses the update whole,
    a full regeneration first making it one.
    """
    try:
        minors = ceilings_minors()
    except ValueError as e:
        return _nothing_written(str(e))
    if not minors:
        return _nothing_written(f"no ceilings file in {_display_path(CEILINGS_DIR)} to regenerate")
    missing = [minor for minor in minors if find_interpreter(minor) is None]
    for minor in missing:
        print(
            f"no interpreter for {minor} (uv python find --system {minor}); "
            f"install it with `uv python install {minor}`",
            flush=True,
        )
    if missing:
        return _nothing_written("every minor with a ceilings file must be measured")
    kept: "dict[str, dict[str, dict[str, object]]] | None" = None
    if only is not None:
        try:
            surfaces = _scoped(surfaces, only)
            kept = {minor: _stored_entries(minor) for minor in minors}
        except (KeyError, OSError, ValueError) as e:
            return _nothing_written(e.args[0] if isinstance(e, KeyError) else str(e))
    results: dict[str, dict[str, FileOps]] = {}
    for minor in minors:
        try:
            python = build_env(minor)
        except EnvBuildError as e:
            return _nothing_written(str(e))
        use_python(str(python))
        if interpreter_tag() != minor:
            return _nothing_written(f"{python} is CPython {interpreter_tag()}, not {minor}")
        print(f"\nCPython {minor} ({python})", flush=True)
        _rows, measured, failed = _measure_table(surfaces, check=False, update=True)
        if failed:
            return _nothing_written(f"CPython {minor} failed (see FAIL lines above)")
        results[minor] = (
            measured if only is None else {k: v for k, v in measured.items() if k in only}
        )
    try:
        write_ceilings_files(results, kept)
    except CeilingsReplaceError as e:
        print(f"\nimport budget: FAILED — {e}", flush=True)
        return 1
    except (OSError, KeyError) as e:
        return _nothing_written(f"staging the ceilings files failed: {e}")
    for minor in results:
        print(f"wrote {_display_path(ceilings_path(minor))}", flush=True)
    return 0


def _built_or_named_failure(build: "Callable[[], object]") -> "str | None":
    """Run *build*; return what it built as text, or ``None`` once its failure is on stderr.

    The one place a command line's "the venv could not be built" exit is
    decided: the message the :class:`EnvBuildError` carries, then exit 1.
    """
    try:
        return str(build())
    except EnvBuildError as e:
        print(e, file=sys.stderr, flush=True)
        return None


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
        help="rewrite every minor's ceilings file (each one in tests/unit/import_budget/"
        "ceilings/), each measured in a venv synced for it; writes nothing unless every "
        "minor's interpreter is found and measures clean",
    )
    ap.add_argument(
        "--only",
        metavar="KEY[,KEY...]",
        help="with --update: measure only these surfaces (and the ratio floor one of them "
        "divides by) and write their entries into each ceilings file, keeping every other "
        "entry as it is",
    )
    ap.add_argument(
        "--python",
        metavar="PATH",
        help="measure the interpreter at PATH as it is, with no venv built "
        "(default: a runtime-only venv synced for this interpreter's minor)",
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
    if args.update and args.python:
        ap.error(
            "--update regenerates every interpreter's file, each in the venv it syncs; "
            "it measures no single --python"
        )
    only = None if args.only is None else [k.strip() for k in args.only.split(",")]
    if only is not None and not args.update:
        ap.error("--only scopes --update; it cannot be used without it")
    if only is not None and not all(only):
        ap.error("--only names surfaces by key, comma-separated, with no empty key")
    if args.python:
        # Absolute, since the measured children run elsewhere (`_quiet_cwd`);
        # never resolved, since a venv's interpreter is a link `venv_of` reads.
        use_python(str(Path(args.python).absolute()))

    # A tracked surface has no baseline to record, so --update skips it.
    surfaces = [s for s in SURFACES if not (args.update and s.tracked)]
    # The ratio floor first (a stable sort keeps the rest in table order), so
    # every target ratio is checked against a floor from this same run.
    surfaces.sort(key=lambda s: s.key != RATIO_FLOOR)
    if args.update:
        return _update_every_minor(surfaces, only=only)
    # Resolved before the table: a failed build is a named exit, and the
    # sync's own output lands above the header, not inside the table.
    if _built_or_named_failure(measured_python) is None:
        return 1
    rows, _measured, failed = _measure_table(surfaces, check=args.check, update=False)
    if args.report_json:
        args.report_json.write_text(json.dumps(rows, indent=2, sort_keys=True) + "\n")
    if failed:
        print("\nimport budget: FAILED — see FAIL lines above.", flush=True)
        return 1
    if args.check:
        print("\nimport budget: OK", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
