#!/usr/bin/env python3
"""Refresh or check the committed command captures the docs pages include.

Spec 2026-08-27 §5. One TOML manifest, ``docs/examples/captures.toml``, lists
every capture for every example project, and every artifact lands in one
directory, ``docs/examples/captures/``. Each capture names: the argv to run,
whether it needs the bed, which example project to run it against (copied into
the scratch dir on first use, so every path a capture prints is anchored to the
scratch dir rather than baking in this checkout's own path — see
``RunContext.project_dir``), the exit code it is expected to return, the
seconds it may run before it is killed, directories to make under the scratch
dir before it runs, a settings fragment to append to the project copy for that
capture alone, redaction rules, and whether its output is recorded — ``id``,
``argv``, ``labless``, ``project``, ``expect_exit``, ``timeout``, ``mkdir``,
``settings_append``, ``redact``, ``record``, ``env``, ``always``,
``arms_teardown``, ``unordered``.
The runner executes each one in a clean environment, redacts
the output, and either writes
``captures/<id>.txt`` (refresh) or diffs against it (``--check``, exit 1 on
drift). ``--labless`` selects only captures that need no bed; the docs gate
runs ``--check --labless``, and ``make docs-captures-check`` runs ``--check``
against the bed.

A capture with ``record = false`` is a step a later capture needs (a build, a
test run that fills a coverage store): it runs and its exit code is checked, but
no artifact is written or compared. Captures run in manifest order and share one
scratch copy of the examples, so a set whose later captures depend on earlier
steps must be selected whole. A top-level ``[ordered]`` table declares those
sets (``sets = ["<project>", ...]``: each project's captures are one set), and
an ``--only`` list that names some of a set's captures but not all is refused
before anything runs. Each listed project must be one some capture runs.

``[[capture.store]]`` asserts one hit count in a coverage store after the run;
its ``file`` takes the same ``{project}``/``{tmp}``/``{xdir}`` placeholders as
its ``path``. With ``marker``, the line must also hold that text in the
committed example (``verify_markers``), so an edit that moves the line fails
before any capture runs. ``[[capture.store_sources]]`` asserts the exact set of
source files in a tier, whatever their hit count (``_check_sources``).

``env = { K = "v" }`` sets variables for one capture alone. They are applied
after every ``OTTO_*`` variable of the caller is stripped, so a set can pin one
(``OTTO_COMPOSE_SUFFIX``) for its own commands. A capture may not change what
the runner pins for every capture (``RunContext.pinned_env``): width, colour,
``OTTO_HOME``, ``OTTO_XDIR`` and the project's ``OTTOPATH``.

``always = true`` marks a set's teardown. Captures run in order; once one
fails, only the remaining ``always`` captures run, unrecorded, and the first
failure is the error raised: a teardown that fails beside it is printed, never
raised in its place. On a run with no failure an ``always`` capture is
ordinary, recorded and exit-checked. An ``always`` capture may not be followed
by an ordinary capture of its own project: teardown is the tail of a set. Only
a capture's own failure triggers it; an interrupt propagates at once.

``arms_teardown = true`` marks the one capture of a set that proves the set
owns what its teardown removes (a preflight). A set that has one runs its
``always`` captures only once that capture has succeeded in this run: if it
failed, or never ran because an earlier capture (of any set) failed first, each
``always`` capture is skipped and said to be, never run. The teardown of such a
set removes what this run made, and a refused preflight leaves the host
exactly as it found it. A set without one tears down as above.

The lease ends with the runner, so the command a capture is running must end
first. Ctrl-C, ``make`` and ``timeout`` signal the whole process group, which
ends both. A SIGTERM sent to the runner's PID alone would leave the child
driving the bed after the lease is gone, so the runner turns SIGTERM into
``Terminated`` for the run (``_sigterm_interrupts``): ``subprocess.run`` then
kills and reaps the running command before the lease is released.

``unordered = true`` is for a command whose log lines interleave work otto
does on several hosts at once, so their order varies from run to run.
Refreshing writes the artifact exactly as printed. ``--check`` compares the
redacted lines as a multiset (``_same_lines``): it ignores their order, but a
missing, extra or changed line is still drift.

A top-level ``[leases]`` table names the projects whose captures need a bed
lease (``docker_host = ["<project>", ...]``). A run selecting any capture of
such a project holds the lease for its whole length, teardown included: the
docker host lease the docker e2e tests and the orphan reaper take, from
``tests/_fixtures/_host_pool``, so a capture set and a test session never
share the docker host. ``--list`` takes no lease.

A top-level ``[snippets]`` table names a piece of text several captures share
(``<name> = "<text>"``); an argv entry spells it ``{snippet:<name>}``, and the
manifest is read with each one put in its place (``_expand_snippets``). It is
how one decision several shell texts make (which docker cache records are
private) is written once. A name no snippet defines is refused at load.

A top-level ``[overlays]`` table maps a project to a directory under the
examples root (``<project> = "<dir>"``) that is copied over that project's
scratch copy. It is how a downloadable example ships placeholder hosts while
its captures reach the bed: the overlay carries the bed's lab data, and the
committed example never changes. Each key must be a plain project name a
capture runs, and each directory must lie under the examples root; the whole
table is checked when the manifest loads, before anything is copied.

Deliberately a script, not a pytest module: nothing collected under
tests/integration/ may run without reaping the lab, and this must be safe to
invoke while unrelated work is in flight.

Captures run in a fixed scratch directory (``SCRATCH_DIR``), not a fresh
``tempfile.mkdtemp()``: a Rich-rendered table wraps and centres its title
against the *pre-redaction* scratch path, so the artifact's layout depends on
that path's length. A directory that moves with ``$TMPDIR`` freezes a
layout that only holds on the machine (and environment) that took it. Because
the scratch directory is fixed and reused, two refreshes must not run
concurrently on one machine -- across worktrees and sessions too, since the
path is machine-wide. ``SCRATCH_LOCK`` enforces that: a second run says it is
waiting, then waits, instead of appending to (or deleting) the first run's
copy, which corrupted both runs whenever two ``make docs`` overlapped.
"""

import argparse
import contextlib
import dataclasses
import difflib
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
from collections import Counter
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager
from pathlib import Path
from typing import TYPE_CHECKING, Any

import tomli

from otto.config.env import without_otto_env
from otto.host.privilege import SUDO_PROMPT_SHAPE

if TYPE_CHECKING:
    from otto.coverage.store.model import CoverageStore, FileRecord

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from scripts.check_docs_examples import not_copied  # noqa: E402 -- path set up above
from scripts.docs_display_env import DISPLAY_ENV  # noqa: E402 -- path set up above

EXAMPLES = ROOT / "docs" / "examples"
DEFAULT_MANIFEST = EXAMPLES / "captures.toml"
DEFAULT_CAPTURES = EXAMPLES / "captures"
SCRATCH_DIR = Path("/tmp/otto-gs")  # noqa: S108 -- a fixed, documented scratch path; the artifact layout depends on its length
# Beside the scratch directory, never inside it: every run deletes that.
SCRATCH_LOCK = Path("/tmp/otto-gs.lock")  # noqa: S108 -- pairs with SCRATCH_DIR, machine-wide on purpose

_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?")
# The log path column ends a log line with ``file.py:<line>``; the number moves
# with every edit to that file, so a capture would drift on almost any src change.
# Rich pads the message so that column ends at the right edge, so the padding in
# front of it moves too when the number gains or loses a digit: the digits' width
# goes into the padding, which leaves the redacted line the same either way.
_LOG_SOURCE_LINE = re.compile(r"(?m)( *)(\S+\.py):(\d+)(?=[ \t]*$)")
_LINE_PLACEHOLDER = "<n>"


def _redact_log_source(match: re.Match[str]) -> str:
    pad, path, digits = match.groups()
    width = len(pad) + len(digits) - len(_LINE_PLACEHOLDER)
    return " " * max(width, 1 if pad else 0) + f"{path}:{_LINE_PLACEHOLDER}"


Replacement = str | Callable[[re.Match[str]], str]


class CaptureError(RuntimeError):
    """A capture could not be taken as the manifest describes."""


@contextlib.contextmanager
def scratch_lock(path: Path = SCRATCH_LOCK) -> Iterator[None]:
    """Hold the machine-wide lock on the scratch directory for the block.

    A run that finds the lock taken says so on stderr, then blocks until the
    holder finishes. The lock is an ``flock`` on an open file, so the kernel
    drops it when its holder exits, however it exits, and a crashed run can
    never leave it stuck.
    """
    import fcntl  # POSIX-only, like the fixed /tmp path this guards

    with path.open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(
                f"docs captures: another capture run holds {path}; waiting for it to finish",
                file=sys.stderr,
            )
            fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


KINDS = ("otto", "gtest")
"""What a capture runs: an ordinary command, or a GoogleTest binary."""

_GTEST_RULES = [(r"\(\d+ ms\)", "(<n> ms)"), (r"\(\d+ ms total\)", "(<n> ms total)")]
"""A gtest binary's per-test and total timings change on every run."""


@dataclasses.dataclass(frozen=True)
class StoreAssertion:
    """One hit count a capture's coverage store must record.

    ``hits`` is the count on ``file``:``line`` in ``tier``: the tier total, or,
    with ``product``, the sum over that product's runs in the tier. ``file``
    takes the same placeholders as ``path``. ``marker``, when set, is text that
    line must hold in the committed example, so the assertion cannot drift
    off the line it means.
    """

    path: str
    file: str
    line: int
    tier: str
    hits: int
    product: "str | None" = None
    marker: "str | None" = None


@dataclasses.dataclass(frozen=True)
class StoreSources:
    """The exact set of files a capture's coverage store records a hit for.

    A file counts when any of its lines has a nonzero count in ``tier``: the
    tier total, or, with ``product``, the sum over that product's runs.
    ``files`` take the same placeholders as ``path``.
    """

    path: str
    tier: str
    files: list[str]
    product: "str | None" = None


@dataclasses.dataclass(frozen=True)
class Capture:
    """One manifest entry: the argv to run and how to judge and redact it."""

    id: str
    argv: list[str]
    labless: bool = False
    project: str = ""
    expect_exit: int = 0
    timeout: int = 120
    redact: list[tuple[str, str]] = dataclasses.field(default_factory=list)
    mkdir: list[str] = dataclasses.field(default_factory=list)
    settings_append: str = ""
    kind: str = "otto"
    store: list[StoreAssertion] = dataclasses.field(default_factory=list)
    record: bool = True
    """A step a later capture needs, run but not recorded: no artifact is written or compared."""
    store_sources: list[StoreSources] = dataclasses.field(default_factory=list)
    env: dict[str, str] = dataclasses.field(default_factory=dict)
    """Variables set for this capture alone, after otto's own are stripped (``RunContext.env``)."""
    always: bool = False
    """Part of its set's teardown: still run, unrecorded, after an earlier capture fails."""
    arms_teardown: bool = False
    """Its success in this run is what lets its set's ``always`` captures run at all."""
    armed_by: "str | None" = None
    """For an ``always`` capture, the id of its set's ``arms_teardown`` capture, if it has one."""
    unordered: bool = False
    """``--check`` ignores the order of its lines, never their count or text (``_same_lines``)."""


@dataclasses.dataclass
class RunContext:
    """Where a run reads and writes: the example projects, the artifacts, and a scratch dir.

    ``overlays`` maps a project to a directory under ``examples_root`` copied
    over that project's scratch copy (the manifest's ``[overlays]``).
    """

    examples_root: Path
    captures_dir: Path
    tmp: Path
    _project_dirs: dict[str, Path] = dataclasses.field(init=False, default_factory=dict, repr=False)
    overlays: dict[str, str] = dataclasses.field(default_factory=dict)

    def project_dir(self, cap: Capture) -> Path:
        """Return the scratch-dir copy of ``cap.project``, copying it there on first use.

        A capture must never point ``OTTOPATH`` (or any printed path) at
        ``examples_root`` directly: that path is under this checkout, and its
        length varies by worktree, which shifts exactly where Rich wraps a
        long log line -- baking a checkout-specific wrap point into the
        committed artifact. Copying the project into the scratch dir first
        means every printed path is anchored to ``SCRATCH_DIR`` instead, which
        is fixed and identical on every checkout. Cached by project name on
        ``self`` so two captures sharing a project copy it only once per run.

        Copies every project under ``examples_root`` together, not just
        ``cap.project`` alone: a project may declare ``libs``/``init`` (or any
        other relative path) reaching into a sibling with ``../<project>/...``
        (spec 2026-09-06 creds-store §6.4 -- the inventory twin shares the
        original example's init module this way), and that relative path must
        still resolve once both projects sit under the scratch dir, exactly as
        it does in the checkout. Bytecode and each example's own top-level
        ``build/`` stay behind (the rule ``check_docs_examples`` copies by): a
        stale in-place build's CMake cache names the checkout's paths and
        would break a fresh configure in the copy. Each overlay is then copied
        over its project's copy, so the bed's lab data reaches the captures
        while the committed example keeps its placeholder hosts.
        """
        if cap.project not in self._project_dirs:
            src = self.examples_root / cap.project
            if not src.is_dir():
                raise CaptureError(
                    f"capture {cap.id!r}: project {cap.project!r} "
                    f"not found under {self.examples_root}"
                )
            # Once per RUN, not once per newly-seen project: the copy always
            # brings every project across (the whole-tree copy above), so a
            # second distinct project needs only the validation above -- a
            # second copytree would just re-copy what the first already
            # placed under self.tmp.
            if not self._project_dirs:
                ignore = not_copied(self.examples_root)
                shutil.copytree(self.examples_root, self.tmp, ignore=ignore, dirs_exist_ok=True)
                self._apply_overlays(ignore)
            self._project_dirs[cap.project] = self.tmp / cap.project
        return self._project_dirs[cap.project]

    def _apply_overlays(self, ignore: Callable[[str, list[str]], set[str]]) -> None:
        """Copy each overlay directory over its project's scratch copy."""
        for project, overlay in self.overlays.items():
            source, dest = _overlay_paths(project, overlay, self.examples_root, self.tmp)
            shutil.copytree(source, dest, ignore=ignore, dirs_exist_ok=True)

    def default_rules(self) -> list[tuple[str, Replacement]]:
        """Redaction rules every capture gets before its own manifest rules run."""
        # Order matters: the scratch dir is redacted before the examples
        # root (so a path never half-matches a date later), the examples
        # root before the repo root (an examples root under ROOT is a
        # substring of it and must win), and the repo root before the
        # generic timestamp rule.
        return [
            (re.escape(str(self.tmp)), str(SCRATCH_DIR)),
            (re.escape(str(self.examples_root)), "<examples>"),
            (re.escape(str(ROOT)), "<repo>"),
            (_TIMESTAMP.pattern, "<timestamp>"),
            (_LOG_SOURCE_LINE.pattern, _redact_log_source),
            # Each elevated command's sudo prompt is fresh, so a logged
            # command line would differ on every refresh.
            (SUDO_PROMPT_SHAPE.pattern, "otto-sudo-<nonce>:"),
        ]

    def substitutions(self, cap: Capture) -> dict[str, str]:
        """Build the ``{placeholder}`` -> real-path mapping for a capture's actual argv."""
        otto = Path(sys.executable).parent / "otto"
        project = str(self.project_dir(cap)) if cap.project else ""
        return {
            "{otto}": str(otto),
            "{python}": sys.executable,
            "{tmp}": str(self.tmp),
            "{project}": project,
            "{checkout}": str(ROOT),  # captures run from {tmp}; scripts need an absolute path
            "{xdir}": str(self.tmp / "xdir"),  # the capture's OTTO_XDIR
        }

    def pinned_env(self, cap: Capture) -> dict[str, str]:
        """Return the variables every capture runs with; a capture's ``env`` may not change them."""
        pinned = {
            **DISPLAY_ENV,
            "OTTO_HOME": str(self.tmp / "home"),
            "OTTO_XDIR": str(self.tmp / "xdir"),
        }
        if cap.project:
            pinned["OTTOPATH"] = str(self.project_dir(cap))
        return pinned

    def env(self, cap: Capture) -> dict[str, str]:
        """Build the clean environment a capture's subprocess runs in."""
        # Same shape as scripts/capture_docs_termynal.py: strip every OTTO_*
        # variable, pin width and colour (scripts/docs_display_env.py, shared
        # with it), then
        # point at the example project.
        # The machine's own settings (a slow machine's OTTO_INIT_TIMEOUT) are
        # kept; see otto.config.env.machine_env_vars. The capture's own
        # ``env`` goes last, so it survives the strip, but it may not change a pin.
        pinned = self.pinned_env(cap)
        clash = sorted(set(cap.env) & set(pinned))
        if clash:
            raise CaptureError(
                f"capture {cap.id!r}: env sets {clash}, which the runner sets for every capture"
            )
        (self.tmp / "home").mkdir(parents=True, exist_ok=True)
        (self.tmp / "xdir").mkdir(parents=True, exist_ok=True)
        return {**without_otto_env(os.environ), **pinned, **cap.env}


def _store_assertions(
    path: Path, cap_id: str, raw: object, *, project: str
) -> list[StoreAssertion]:
    """Parse a capture's ``[[capture.store]]`` entries, naming the capture on any error."""
    where = f"manifest {path}: capture {cap_id!r}: store"
    if not isinstance(raw, list):
        raise CaptureError(f"{where} entries are tables ([[capture.store]])")
    out = []
    for entry in raw:
        if not isinstance(entry, dict):
            raise CaptureError(f"{where} entries are tables ([[capture.store]])")
        for key, kind in (
            ("path", str),
            ("file", str),
            ("line", int),
            ("tier", str),
            ("hits", int),
        ):
            if key not in entry:
                raise CaptureError(f"{where} entry lacks {key!r}")
            if not isinstance(entry[key], kind) or isinstance(entry[key], bool):
                raise CaptureError(f"{where} {key!r} must be a {kind.__name__}")
        product = _optional_str(entry, "product", where)
        marker = _optional_str(entry, "marker", where)
        if marker is not None and not project:
            raise CaptureError(f"{where} 'marker' needs the capture's project")
        if marker is not None and not entry["file"].startswith(_PROJECT_PREFIX):
            raise CaptureError(f"{where} 'marker' needs a file under {_PROJECT_PREFIX!r}")
        out.append(
            StoreAssertion(
                path=entry["path"],
                file=entry["file"],
                line=entry["line"],
                tier=entry["tier"],
                hits=entry["hits"],
                product=product,
                marker=marker,
            )
        )
    return out


_PROJECT_PREFIX = "{project}/"
"""How a marked store assertion names its file: relative to the capture's project."""


def _optional_str(entry: dict[str, object], key: str, where: str) -> "str | None":
    value = entry.get(key)
    if value is not None and not isinstance(value, str):
        raise CaptureError(f"{where} {key!r} must be a str")
    return value


def _store_sources(path: Path, cap_id: str, raw: object) -> list[StoreSources]:
    """Parse a capture's ``[[capture.store_sources]]`` entries, naming the capture on any error."""
    where = f"manifest {path}: capture {cap_id!r}: store_sources"
    if not isinstance(raw, list):
        raise CaptureError(f"{where} entries are tables ([[capture.store_sources]])")
    out = []
    for entry in raw:
        if not isinstance(entry, dict):
            raise CaptureError(f"{where} entries are tables ([[capture.store_sources]])")
        for key, kind in (("path", str), ("tier", str), ("files", list)):
            if key not in entry:
                raise CaptureError(f"{where} entry lacks {key!r}")
            if not isinstance(entry[key], kind):
                raise CaptureError(f"{where} {key!r} must be a {kind.__name__}")
        if not all(isinstance(f, str) for f in entry["files"]):
            raise CaptureError(f"{where} 'files' entries are strings")
        out.append(
            StoreSources(
                path=entry["path"],
                tier=entry["tier"],
                files=list(entry["files"]),
                product=_optional_str(entry, "product", where),
            )
        )
    return out


def _read_toml(path: Path) -> dict[str, Any]:
    """Read and parse the manifest file: the one place it is read."""
    try:
        return tomli.loads(path.read_text())
    except (OSError, tomli.TOMLDecodeError) as exc:
        raise CaptureError(f"manifest {path}: {exc}") from exc


def _overlays(doc: dict[str, Any], path: Path) -> dict[str, str]:
    """Parse the top-level ``[overlays]`` table: project -> dir under the examples."""
    raw = doc.get("overlays", {})
    if not isinstance(raw, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in raw.items()
    ):
        raise CaptureError(
            f"manifest {path}: [overlays] maps a project to a directory under the examples"
        )
    return dict(raw)


def _ordered(doc: dict[str, Any], path: Path, caps: list[Capture]) -> list[str]:
    """Parse the ``[ordered]`` sets: projects whose captures must be selected whole.

    Each name must be a project some capture runs, so a typo fails at load
    rather than silently leaving a set unguarded.
    """
    raw = doc.get("ordered", {})
    shape = f"manifest {path}: [ordered] sets is a list of project names"
    if not isinstance(raw, dict) or set(raw) - {"sets"}:
        raise CaptureError(shape)
    sets = raw.get("sets", [])
    if not isinstance(sets, list) or not all(isinstance(s, str) for s in sets):
        raise CaptureError(shape)
    projects = {cap.project for cap in caps if cap.project}
    for name in sets:
        if name not in projects:
            raise CaptureError(f"manifest {path}: [ordered] set: no capture runs project {name!r}")
    return list(sets)


@contextlib.contextmanager
def _docker_host_lease() -> Iterator[None]:
    """Take the docker host lease: the lock the docker e2e holds on the docker host.

    Imported here, not at module load: only a bed run that needs a lease pays
    for it. The lock lives under ``bed_lock_root()``, as every session's does.
    """
    from tests._fixtures import _host_pool

    print(
        f"docs captures: taking the docker host lease on {_host_pool.DOCKER_HOST}; "
        "a session holding it is waited for",
        file=sys.stderr,
    )
    with _host_pool.docker_host_lease():
        yield


_LEASE_TAKERS: dict[str, Callable[[], AbstractContextManager[None]]] = {
    "docker_host": _docker_host_lease,
}
"""Each bed lease a ``[leases]`` table may name, and how ``held_leases`` takes it."""

LEASES = tuple(_LEASE_TAKERS)
"""The bed leases a ``[leases]`` table may name; ``held_leases`` takes each."""


def _leases(doc: dict[str, Any], path: Path, caps: list[Capture]) -> dict[str, list[str]]:
    """Parse the ``[leases]`` table: a lease -> the projects whose captures need it.

    Each project must be one some capture runs, so a typo fails at load
    rather than silently running a set without its lease.
    """
    raw = doc.get("leases", {})
    shape = f"manifest {path}: [leases] maps a lease ({', '.join(LEASES)}) to a list of projects"
    if not isinstance(raw, dict) or set(raw) - set(LEASES):
        raise CaptureError(shape)
    projects = {cap.project for cap in caps if cap.project}
    out: dict[str, list[str]] = {}
    for lease, names in raw.items():
        if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
            raise CaptureError(shape)
        for name in names:
            if name not in projects:
                raise CaptureError(
                    f"manifest {path}: [leases] {lease}: no capture runs project {name!r}"
                )
        out[lease] = list(names)
    return out


def leased(caps: list[Capture], leases: dict[str, list[str]]) -> list[str]:
    """Return the leases a run of ``caps`` must hold: those naming a selected capture's project."""
    projects = {cap.project for cap in caps if cap.project}
    return [lease for lease, names in leases.items() if projects & set(names)]


@contextlib.contextmanager
def held_leases(names: list[str]) -> Iterator[None]:
    """Hold each named bed lease (``_LEASE_TAKERS``) for the block, in the order given."""
    with contextlib.ExitStack() as stack:
        for name in names:
            stack.enter_context(_LEASE_TAKERS[name]())
        yield


@dataclasses.dataclass(frozen=True)
class Manifest:
    """The whole manifest, parsed and checked once."""

    captures: list[Capture]
    overlays: dict[str, str]
    ordered: list[str]
    """Projects whose captures form one ordered set, selected whole or not at all."""
    leases: dict[str, list[str]] = dataclasses.field(default_factory=dict)
    """Each bed lease, mapped to the projects whose captures run under it."""
    snippets: dict[str, str] = dataclasses.field(default_factory=dict)
    """Each ``[snippets]`` text by name, already expanded in ``captures``."""


def read_manifest(path: Path) -> Manifest:
    """Parse the TOML manifest at ``path``: captures, overlays, ordered sets, leases, snippets."""
    doc = _read_toml(path)
    snippets = _snippets(doc, path)
    caps = _captures(doc, path, snippets)
    return Manifest(
        captures=caps,
        overlays=_overlays(doc, path),
        ordered=_ordered(doc, path, caps),
        leases=_leases(doc, path, caps),
        snippets=snippets,
    )


def _overlay_paths(project: str, overlay: str, examples_root: Path, scratch: Path) -> list[Path]:
    """Return an overlay's source and destination, refusing either outside its root.

    The key names a project's scratch copy, so it must be a plain project
    name: an absolute key would make ``scratch / project`` the checked-in
    example itself, and a refresh would then overwrite its placeholder lab
    with the bed's. The source must stay under ``examples_root``.
    """
    where = f"overlay for {project!r}"
    if project in {"", ".", ".."} or Path(project).name != project:
        raise CaptureError(f"{where}: the key must be a plain project name")
    root = examples_root.resolve()
    source = (examples_root / overlay).resolve()
    if source == root or not source.is_relative_to(root):
        raise CaptureError(f"{where}: {overlay!r} resolves outside {examples_root}")
    if not source.is_dir():
        raise CaptureError(f"{where}: {overlay!r} is not a directory under {examples_root}")
    if not (examples_root / project).is_dir():
        raise CaptureError(
            f"overlay {overlay!r}: no example project {project!r} under {examples_root}"
        )
    dest = (scratch / project).resolve()
    if not dest.is_relative_to(scratch.resolve()):
        raise CaptureError(f"{where}: its copy resolves outside the scratch dir {scratch}")
    return [source, dest]


def check_overlays(
    overlays: dict[str, str], caps: list[Capture], *, examples_root: Path, scratch: Path
) -> None:
    """Refuse an overlay before anything is copied: each must name a manifest project.

    Runs on the whole manifest, so the labless docs lane catches a bad entry
    a bed capture would only reach later.
    """
    projects = {cap.project for cap in caps if cap.project}
    for project, overlay in overlays.items():
        _overlay_paths(project, overlay, examples_root, scratch)
        if project not in projects:
            raise CaptureError(f"overlay {overlay!r}: no capture runs project {project!r}")


def load_manifest(path: Path) -> list[Capture]:
    """Parse the TOML manifest at ``path`` into a list of Captures, checking the whole file."""
    return read_manifest(path).captures


_SNIPPET = re.compile(r"\{snippet:([A-Za-z_][A-Za-z0-9_]*)\}")


def _snippets(doc: dict[str, Any], path: Path) -> dict[str, str]:
    """Parse the top-level ``[snippets]`` table: a name -> the text it stands for."""
    raw = doc.get("snippets", {})
    if not isinstance(raw, dict) or not all(
        isinstance(k, str) and _ENV_NAME.fullmatch(k) and isinstance(v, str) for k, v in raw.items()
    ):
        raise CaptureError(
            f"manifest {path}: [snippets] maps a name, spelled like a shell variable, to a text"
        )
    return dict(raw)


def _expand_snippets(arg: str, snippets: dict[str, str], where: str) -> str:
    """Put each ``{snippet:<name>}`` in *arg* in its text's place, refusing an undefined name."""

    def text(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in snippets:
            raise CaptureError(f"{where}: no [snippets] entry {name!r}")
        return snippets[name]

    return _SNIPPET.sub(text, arg)


def _captures(doc: dict[str, Any], path: Path, snippets: dict[str, str]) -> list[Capture]:
    """Parse the ``[[capture]]`` entries, in manifest order, with their *snippets* expanded."""
    caps = []
    for n, raw in enumerate(doc.get("capture", []), start=1):
        for key in ("id", "argv"):
            if key not in raw:
                raise CaptureError(f"manifest {path}: capture #{n} lacks {key!r}")
        rules = []
        for entry in raw.get("redact", []):
            try:
                pattern, repl = entry
            except (TypeError, ValueError) as exc:
                raise CaptureError(
                    f"manifest {path}: capture {raw['id']!r}: "
                    "redact entries are [pattern, replacement] pairs"
                ) from exc
            rules.append((pattern, repl))
        raw_mkdir = raw.get("mkdir", [])
        if not isinstance(raw_mkdir, list) or not all(isinstance(d, str) for d in raw_mkdir):
            raise CaptureError(f"manifest {path}: capture {raw['id']!r}: mkdir entries are strings")
        raw_append = raw.get("settings_append", "")
        if not isinstance(raw_append, str):
            raise CaptureError(
                f"manifest {path}: capture {raw['id']!r}: "
                "settings_append is a file name (relative to the project)"
            )
        kind = raw.get("kind", "otto")
        if kind not in KINDS:
            raise CaptureError(
                f"manifest {path}: capture {raw['id']!r}: kind {kind!r} is not one of {list(KINDS)}"
            )
        if raw_append and not raw.get("project"):
            raise CaptureError(
                f"manifest {path}: capture {raw['id']!r}: settings_append needs project"
            )
        record = raw.get("record", True)
        if not isinstance(record, bool):
            raise CaptureError(f"manifest {path}: capture {raw['id']!r}: record is true or false")
        always = raw.get("always", False)
        if not isinstance(always, bool):
            raise CaptureError(f"manifest {path}: capture {raw['id']!r}: always is true or false")
        arms = raw.get("arms_teardown", False)
        if not isinstance(arms, bool):
            raise CaptureError(
                f"manifest {path}: capture {raw['id']!r}: arms_teardown is true or false"
            )
        unordered = raw.get("unordered", False)
        if not isinstance(unordered, bool):
            raise CaptureError(
                f"manifest {path}: capture {raw['id']!r}: unordered is true or false"
            )
        caps.append(
            Capture(
                id=raw["id"],
                argv=[
                    _expand_snippets(a, snippets, f"manifest {path}: capture {raw['id']!r}")
                    if isinstance(a, str)
                    else a
                    for a in raw["argv"]
                ],
                labless=bool(raw.get("labless", False)),
                project=raw.get("project", ""),
                expect_exit=int(raw.get("expect_exit", 0)),
                timeout=int(raw.get("timeout", 120)),
                redact=rules,
                mkdir=list(raw_mkdir),
                settings_append=raw_append,
                kind=kind,
                store=_store_assertions(
                    path, raw["id"], raw.get("store", []), project=raw.get("project", "")
                ),
                record=record,
                store_sources=_store_sources(path, raw["id"], raw.get("store_sources", [])),
                env=_capture_env(path, raw["id"], raw.get("env", {})),
                always=always,
                arms_teardown=arms,
                unordered=unordered,
            )
        )
    ids = [c.id for c in caps]
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    if dupes:
        raise CaptureError(f"duplicate capture ids: {dupes}")
    _check_teardown_tails(caps, path)
    return _armed_teardowns(caps, path)


_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _capture_env(path: Path, cap_id: str, raw: object) -> dict[str, str]:
    """Parse a capture's ``env``: its shape and names; ``RunContext.env`` refuses a pinned name.

    A name the environment cannot carry (empty, holding ``=``) is refused
    here, at load, rather than by ``subprocess`` once the set is running.
    """
    if not isinstance(raw, dict) or not all(
        isinstance(k, str) and _ENV_NAME.fullmatch(k) and isinstance(v, str) for k, v in raw.items()
    ):
        raise CaptureError(
            f"manifest {path}: capture {cap_id!r}: env is a table of strings, "
            "each named like a shell variable"
        )
    return dict(raw)


def _check_teardown_tails(caps: list[Capture], path: Path) -> None:
    """Refuse an ``always`` capture followed by an ordinary one of its project.

    Teardown is the tail of a set: after a failure only the ``always``
    captures run, so an ordinary step after one would be skipped exactly when
    its teardown ran. The captures with no project are checked as one group.
    """
    first_always: dict[str, str] = {}
    for cap in caps:
        first = first_always.get(cap.project)
        if first is not None and not cap.always:
            group = f"project {cap.project!r}" if cap.project else "the captures with no project"
            raise CaptureError(
                f"manifest {path}: capture {cap.id!r} follows the always capture "
                f"{first!r} of {group}"
            )
        if cap.always:
            first_always.setdefault(cap.project, cap.id)


def _armed_teardowns(caps: list[Capture], path: Path) -> list[Capture]:
    """Bind each ``always`` capture to its set's ``arms_teardown`` capture, checking the latter.

    An arming capture belongs to a project, is no teardown itself, and is its
    project's only one. It precedes the project's ``always`` captures, since
    no ordinary capture may follow one (``_check_teardown_tails``).
    """
    arming: dict[str, str] = {}
    for cap in caps:
        if not cap.arms_teardown:
            continue
        where = f"manifest {path}: capture {cap.id!r}: arms_teardown"
        if not cap.project:
            raise CaptureError(f"{where} needs project")
        if cap.always:
            raise CaptureError(f"{where} cannot be an always capture: it arms the teardown")
        if cap.project in arming:
            raise CaptureError(
                f"{where}: project {cap.project!r} already has {arming[cap.project]!r}"
            )
        arming[cap.project] = cap.id
    return [
        dataclasses.replace(cap, armed_by=arming[cap.project])
        if cap.always and cap.project in arming
        else cap
        for cap in caps
    ]


def verify_markers(caps: list[Capture], examples_root: Path) -> None:
    """Raise CaptureError unless every marked store assertion's line holds its marker.

    Read from the committed example, not a scratch copy, so it runs before any
    capture and needs no bed: an edit that moves a coverage-asserted line
    fails here, in the docs lane's ``--check --labless``.
    """
    for cap in caps:
        for a in cap.store:
            if a.marker is None:
                continue
            rel = a.file.removeprefix(_PROJECT_PREFIX)
            source = examples_root / cap.project / rel
            try:
                lines = source.read_text().splitlines()
            except OSError as exc:
                raise CaptureError(f"capture {cap.id!r}: cannot read {source}: {exc}") from exc
            if not (1 <= a.line <= len(lines)) or a.marker not in lines[a.line - 1]:
                raise CaptureError(
                    f"capture {cap.id!r}: {rel}:{a.line} does not hold marker {a.marker!r}"
                )


def select(
    caps: list[Capture], *, labless: bool, only: list[str], ordered: list[str] | None = None
) -> list[Capture]:
    """Narrow ``caps`` to the requested ids, then to labless ones if asked.

    An ``only`` list that names some of an ``ordered`` set's captures but not
    all of them is refused: a later member run without its earlier steps
    would fail, or pass against a stale scratch copy.
    """
    known = {c.id for c in caps}
    for wanted in only:
        if wanted not in known:
            raise CaptureError(f"unknown capture id: {wanted}")
    for name in ordered or []:
        members = [c.id for c in caps if c.project == name]
        missing = [i for i in members if i not in only]
        if 0 < len(missing) < len(members):
            raise CaptureError(
                f"ordered set {name!r} is selected whole or not at all; --only lacks {missing}"
            )
    chosen = [c for c in caps if (not only or c.id in only)]
    if labless:
        chosen = [c for c in chosen if c.labless]
    return chosen


def redact(text: str, rules: list[tuple[str, Replacement]]) -> str:
    """Apply each ``(pattern, replacement)`` rule to ``text`` in order."""
    for pattern, repl in rules:
        text = re.sub(pattern, repl, text)
    return text


def render_command(argv: list[str]) -> str:
    """Render the first line of every artifact: the command as a reader would type it."""
    shown = {
        "{otto}": "otto",
        "{python}": "python",
        "{tmp}": str(SCRATCH_DIR),
        "{project}": ".",
        "{checkout}": ".",
        "{xdir}": str(SCRATCH_DIR / "xdir"),
    }
    rendered = []
    for arg in argv:
        line = arg
        for key, value in shown.items():
            line = line.replace(key, value)
        # `Host.run` treats a sequence of commands as separate commands, so
        # an argument carrying a space or another shell-active character
        # (e.g. `echo $APP_ENV`, `1 + 1`) must be quoted the way a reader
        # would need to type it -- otherwise the rendered line reads as more
        # than one command, or hands a copier's own shell a variable to
        # expand. shlex.quote leaves a plain argument bare.
        rendered.append(shlex.quote(line))
    return "$ " + " ".join(rendered)


@contextlib.contextmanager
def _appended_settings(cap: Capture, ctx: RunContext) -> Iterator[None]:
    """Append ``cap.settings_append`` to the project copy's settings for one run.

    Reservation identity is ``--as-user`` or the login name -- nothing an
    environment can pin -- so the committed example must not select a backend
    or every other capture (and every reader) would be refused. A capture that
    needs the selection appends it here. The scratch copy is shared by every
    capture of the same project in a run, so the original bytes are put back
    afterwards, even when the command fails. The containment check below sees
    the copy ``copytree`` made, which dereferences symlinks, so a committed
    symlink out of the project is no weaker than a committed file.
    """
    if not cap.settings_append:
        yield
        return
    if not cap.project:
        raise CaptureError(
            f"capture {cap.id!r}: settings_append {cap.settings_append!r} needs project"
        )
    project = ctx.project_dir(cap)
    fragment = (project / cap.settings_append).resolve()
    if not fragment.is_relative_to(project.resolve()) or not fragment.is_file():
        raise CaptureError(
            f"capture {cap.id!r}: settings_append {cap.settings_append!r} "
            f"is not a file inside project {cap.project!r}"
        )
    settings = project / ".otto" / "settings.toml"
    try:
        original = settings.read_bytes()
    except OSError as exc:
        raise CaptureError(f"capture {cap.id!r}: cannot read {settings}: {exc}") from exc
    settings.write_bytes(original + b"\n" + fragment.read_bytes())
    try:
        yield
    finally:
        settings.write_bytes(original)


def run_capture(cap: Capture, ctx: RunContext) -> str:
    """Run one capture's argv and return its rendered artifact text."""
    subs = ctx.substitutions(cap)
    for entry in cap.mkdir:
        target = Path(_substitute(entry, subs)).resolve()
        if not target.is_relative_to(ctx.tmp.resolve()):
            raise CaptureError(f"capture {cap.id!r}: mkdir {entry!r} is outside the scratch dir")
        target.mkdir(parents=True, exist_ok=True)
    argv = [_substitute(a, subs) for a in cap.argv]
    if cap.kind == "gtest":
        argv.append("--gtest_color=no")
    with _appended_settings(cap, ctx):
        try:
            proc = subprocess.run(  # noqa: S603 — fixed argv from a committed manifest, no shell
                argv,
                env=ctx.env(cap),
                cwd=str(ctx.tmp),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,  # one stream, so ordering survives
                text=True,
                timeout=cap.timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            # ``text=True`` isn't honoured on a timeout: CPython's subprocess.run
            # never applies the text-mode decode step to what it captured before
            # killing the process, so ``exc.output`` is raw bytes here even
            # though every non-timeout path already sees ``str``.
            raw = exc.output
            partial = raw.decode(errors="replace") if isinstance(raw, bytes) else (raw or "")
            raise CaptureError(
                f"capture {cap.id!r} timed out after {cap.timeout}s; output so far:\n{partial}"
            ) from exc
        except FileNotFoundError as exc:
            raise CaptureError(f"capture {cap.id!r}: command not found: {argv[0]}") from exc
    if proc.returncode != cap.expect_exit:
        raise CaptureError(
            f"capture {cap.id!r} exited {proc.returncode}, "
            f"expected {cap.expect_exit}:\n{proc.stdout}"
        )
    for assertion in cap.store:
        _check_store(cap, assertion, subs)
    for sources in cap.store_sources:
        _check_sources(cap, sources, subs)
    gtest_rules = _GTEST_RULES if cap.kind == "gtest" else []
    body = redact(proc.stdout, ctx.default_rules() + gtest_rules + cap.redact)
    # Rich pads a table row to the terminal width; the padding is not output a
    # reader sees, and keeping it made a refresh at another width rewrite lines
    # that had not changed.
    lines = [line.rstrip() for line in body.rstrip("\n").split("\n")]
    return render_command(cap.argv) + "\n" + "\n".join(lines) + "\n"


def _load_store(cap: Capture, raw_path: str, subs: dict[str, str]) -> "CoverageStore":
    from otto.coverage.store.model import CoverageStore  # the project venv's otto

    path = Path(_substitute(raw_path, subs))
    try:
        return CoverageStore.load(path)
    except (OSError, ValueError) as exc:
        raise CaptureError(f"capture {cap.id!r}: cannot read the store {path}: {exc}") from exc


def _check_store(cap: Capture, want: StoreAssertion, subs: dict[str, str]) -> None:
    """Raise CaptureError unless the capture's coverage store records *want*'s count."""
    store = _load_store(cap, want.path, subs)
    file = _substitute(want.file, subs)
    where = f"capture {cap.id!r}: {want.file}:{want.line}"
    record = next((f for f in store.files() if f.path.as_posix() == file), None)
    if record is None:
        held = sorted(f.path.as_posix() for f in store.files())
        raise CaptureError(f"{where}: the store holds no such file; it holds {held}")
    line = record.lines.get(want.line)
    if want.product is None:
        if not any(run.tier == want.tier for run in store.runs):
            raise CaptureError(f"{where}: no run in tier {want.tier!r}")
        recorded = line.hits.for_tier(want.tier) if line else 0
    else:
        runs = [
            run_id
            for run_id, run in enumerate(store.runs)
            if run.tier == want.tier and run.product == want.product
        ]
        if not runs:
            raise CaptureError(f"{where}: no run of product {want.product!r} in tier {want.tier!r}")
        recorded = sum(line.run_hits.get(run_id, 0) for run_id in runs) if line else 0
    if recorded != want.hits:
        product = f", product {want.product!r}" if want.product else ""
        raise CaptureError(
            f"{where}: tier {want.tier!r}{product}: expected {want.hits} hit(s), "
            f"recorded {recorded}"
        )


def _check_sources(cap: Capture, want: StoreSources, subs: dict[str, str]) -> None:
    """Raise CaptureError unless exactly *want*'s files are in its tier (and product).

    A file is in a tier when any of its lines holds a count for it, zero
    included: the loader records an instrumented line the run never executed
    with a zero count, so a foreign source (a test file, a header) that ran
    no code is caught too. A run is credited only with the lines it executed,
    so a source no run in the tier executed cannot be traced to a product:
    every product's set counts it, and must name it.
    """
    store = _load_store(cap, want.path, subs)
    where = f"capture {cap.id!r}: store_sources in tier {want.tier!r}"
    if want.product is not None:
        where += f", product {want.product!r}"
    tier_runs = [run_id for run_id, run in enumerate(store.runs) if run.tier == want.tier]
    runs = [r for r in tier_runs if want.product is None or store.runs[r].product == want.product]
    if not runs:
        raise CaptureError(f"{where}: no such run")

    def credited(record: "FileRecord", run_ids: list[int]) -> bool:
        return any(line.run_hits.get(r, 0) for line in record.lines.values() for r in run_ids)

    in_tier = [
        f for f in store.files() if any(want.tier in line.hits.counts for line in f.lines.values())
    ]
    if want.product is not None:
        in_tier = [f for f in in_tier if credited(f, runs) or not credited(f, tier_runs)]
    held = {f.path.as_posix() for f in in_tier}
    wanted = {_substitute(f, subs) for f in want.files}
    if held != wanted:
        raise CaptureError(
            f"{where}: extra {sorted(held - wanted)}, missing {sorted(wanted - held)}"
        )


def _substitute(arg: str, subs: dict[str, str]) -> str:
    for key, value in subs.items():
        arg = arg.replace(key, value)
    return arg


def _same_lines(committed: str, fresh: str, *, unordered: bool) -> bool:
    """Whether a fresh capture matches its artifact: exactly, or as a multiset of lines.

    The multiset keeps each line's count, so an order-insensitive check
    still sees a line that went missing, appeared, or changed.
    """
    if not unordered:
        return committed == fresh
    return Counter(committed.splitlines()) == Counter(fresh.splitlines())


def _run_set(caps: list[Capture], step: Callable[[Capture, bool], None]) -> None:
    """Run each capture through *step*; after a failure, only the remaining ``always`` ones.

    *step*'s second argument says whether the capture may be recorded: after a
    failure nothing is, because the set did not run as the artifacts describe.
    The first failure is re-raised once the cleanup has run; a cleanup's own
    failure is printed beside it, never in its place. Only a ``CaptureError``
    counts: an interrupt or a bug propagates at once, without teardown.

    An ``always`` capture bound to an arming capture (``armed_by``) runs only
    once that capture has succeeded here, in this call; otherwise it is
    skipped, and said to be, whether or not anything failed.
    """
    first: CaptureError | None = None
    succeeded: set[str] = set()
    for cap in caps:
        if first is not None and not cap.always:
            continue
        if cap.armed_by is not None and cap.armed_by not in succeeded:
            print(
                f"docs captures: skipped {cap.id}: {cap.armed_by} did not succeed in this run, "
                "so nothing this teardown would remove is proven the set's",
                file=sys.stderr,
            )
            continue
        try:
            step(cap, first is None)
        except CaptureError as exc:
            if first is None:
                first = exc
            else:
                print(
                    f"docs captures: cleanup after the failure also failed: {exc}",
                    file=sys.stderr,
                )
        else:
            succeeded.add(cap.id)
    if first is not None:
        raise first


def refresh(caps: list[Capture], ctx: RunContext) -> None:
    """Run every capture in ``caps`` and (over)write its artifact."""
    if not caps:
        raise CaptureError("no captures selected")
    ctx.captures_dir.mkdir(parents=True, exist_ok=True)

    def step(cap: Capture, recorded: bool) -> None:
        text = run_capture(cap, ctx)
        if not (cap.record and recorded):
            print(f"ran {cap.id}", flush=True)
            return
        (ctx.captures_dir / f"{cap.id}.txt").write_text(text)
        print(f"captured {cap.id}", flush=True)

    _run_set(caps, step)


def check(caps: list[Capture], ctx: RunContext) -> int:
    """Diff each capture's fresh output against its committed artifact; 1 if any drifted, else 0.

    Every capture runs, in order; an unrecorded step is run for what later
    captures need and compared to nothing.
    """
    if not caps:
        raise CaptureError("no captures selected")
    drift = 0
    checked = 0

    def step(cap: Capture, recorded: bool) -> None:
        nonlocal drift, checked
        target = ctx.captures_dir / f"{cap.id}.txt"
        fresh = run_capture(cap, ctx)
        if not (cap.record and recorded):
            return
        checked += 1
        if not target.exists():
            print(f"MISSING {target.relative_to(ROOT) if target.is_relative_to(ROOT) else target}")
            drift += 1
            return
        committed = target.read_text()
        if not _same_lines(committed, fresh, unordered=cap.unordered):
            drift += 1
            print(f"DRIFT {cap.id}")
            before = committed.splitlines(keepends=True)
            after = fresh.splitlines(keepends=True)
            if cap.unordered:
                # Sorted, so the diff shows only what the multiset check saw,
                # not every line that merely moved.
                before, after = sorted(before), sorted(after)
            sys.stdout.writelines(
                difflib.unified_diff(
                    before, after, fromfile=f"committed/{cap.id}.txt", tofile=f"fresh/{cap.id}.txt"
                )
            )

    _run_set(caps, step)
    print(f"{checked} capture(s) checked, {drift} drifted", flush=True)
    return 1 if drift else 0


class Terminated(BaseException):
    """SIGTERM reached the runner. Not a ``CaptureError``: it runs no teardown."""


def _interrupt(signum: int, _frame: object) -> None:
    raise Terminated(f"signal {signum}")


@contextlib.contextmanager
def _sigterm_interrupts() -> Iterator[None]:
    """Raise ``Terminated`` on SIGTERM for the block, then put the old handler back.

    ``subprocess.run`` kills its child when an exception interrupts the wait,
    and, for any exception but ``KeyboardInterrupt``, waits for it, so a
    SIGTERM aimed at the runner alone ends the capture's command before the
    lease is released. The default action would let it outlive the lease;
    ``KeyboardInterrupt`` would leave it unreaped, since ``Popen`` assumes the
    child saw the same Ctrl-C.
    """
    previous = signal.signal(signal.SIGTERM, _interrupt)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, previous)


def main(argv: list[str] | None = None) -> int:
    """CLI entry point: parse args, select captures, then list/check/refresh.

    Call it from the main thread: a run installs a SIGTERM handler
    (``_sigterm_interrupts``), which only the main thread may do.
    """
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--check", action="store_true", help="diff against committed artifacts; exit 1 on drift"
    )
    parser.add_argument("--labless", action="store_true", help="only captures that need no bed")
    parser.add_argument(
        "--only", action="append", default=[], metavar="ID", help="capture id (repeatable)"
    )
    parser.add_argument("--list", action="store_true", help="list captures and exit")
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--captures-dir", type=Path, default=DEFAULT_CAPTURES)
    args = parser.parse_args(argv)

    manifest = read_manifest(args.manifest)
    every = manifest.captures
    verify_markers(every, EXAMPLES)
    check_overlays(manifest.overlays, every, examples_root=EXAMPLES, scratch=SCRATCH_DIR)
    caps = select(every, labless=args.labless, only=args.only, ordered=manifest.ordered)
    if args.list:
        for cap in caps:
            print(f"{cap.id:32} {'labless' if cap.labless else 'bed':8} {render_command(cap.argv)}")
        return 0
    with _sigterm_interrupts(), scratch_lock(), held_leases(leased(caps, manifest.leases)):
        shutil.rmtree(SCRATCH_DIR, ignore_errors=True)
        SCRATCH_DIR.mkdir(parents=True)
        try:
            ctx = RunContext(
                examples_root=EXAMPLES,
                captures_dir=args.captures_dir,
                tmp=SCRATCH_DIR,
                overlays=manifest.overlays,
            )
            if args.check:
                return check(caps, ctx)
            refresh(caps, ctx)
            return 0
        finally:
            shutil.rmtree(SCRATCH_DIR, ignore_errors=True)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except CaptureError as exc:
        print(f"docs captures: {exc}", file=sys.stderr)
        sys.exit(2)
    except Terminated:
        sys.exit(128 + signal.SIGTERM)
