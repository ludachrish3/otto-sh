#!/usr/bin/env python3
"""The bed lease: one run per bed cell, taken by every bed lane, readable by otto.

The lab bed is single-tenant. Every Makefile target that touches it acquires
this lease first (``$(call bed_acquire,<target>)``) and releases it last; a
second run on the same cell fails fast naming the holder, or waits when
``BED_LEASE_WAIT=<seconds>`` is set. The spec is
docs/superpowers/specs/2026-11-01-bed-lease-design.md.

Verbs (``uv run python scripts/bed_lease.py --lab <lab.json> <verb>``)::

    acquire --pid PID [--purpose TEXT] [--unless-all-under DIR -- PATHS...]
    release --pid PID [--force]
    status

The cell is the lab file's directory name (``tech1``); the file's ``labs``
table names the cell's labs, loaded through otto so the resource set is the
one ``otto reservation check`` computes (lab, element and host levels).

Two files per cell under the root (``BED_LEASE_ROOT``, default
``/tmp/otto-bed-lease``): ``<cell>.json``, the structured record, present
only while held; and ``<cell>.reservations.json``, otto's JSON reservation
backend format, re-rendered from the record by every verb (one entry while
held, none when free), so ``otto reservation check`` on the cell reports who
holds the bed. otto only reads it: the lease script is the scheduler.

A run is its holder pid plus every process group the run is known to occupy:
the holder's own, and the group of every re-entrant acquirer (a nested
``make`` started in a session of its own) when it differs. The lease is live
while the holder is alive with its recorded start time or any recorded group
has a member; a SIGKILLed ``make`` whose xdist workers still run keeps the
bed until they are gone, and the next acquirer then takes it over with a
notice. Expiry (``BED_LEASE_TTL``, default four hours) reports an overrun;
it never licenses an overlap: ``make bed-release`` is the eviction.
"""

import argparse
import contextlib
import getpass
import json
import os
import socket
import sys
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
from scripts import process_group  # noqa: E402
from scripts.gate_stamp_common import _git, store_lock, utc_iso, write_atomically  # noqa: E402
from scripts.process_group import (  # noqa: E402
    ancestors,
    group_members,
    process_start,
    process_state,
)

if TYPE_CHECKING:
    from otto.labs.loader import Lab

DEFAULT_ROOT = Path("/tmp/otto-bed-lease")  # noqa: S108 — every user of the cell meets here
ROOT_ENV = "BED_LEASE_ROOT"
WAIT_ENV = "BED_LEASE_WAIT"
TTL_ENV = "BED_LEASE_TTL"
DRAIN_ENV = "BED_LEASE_DRAIN"
LEASE_BUDGET_KNOBS: frozenset[str] = frozenset({WAIT_ENV, TTL_ENV, DRAIN_ENV})
"""The caller's lease budgets: a runner that holds the lease for its lanes spends them
itself, so the lanes it runs do not inherit them (``ROOT_ENV`` is not one: it names the cell)."""
DEFAULT_TTL_S = 4 * 3600
DEFAULT_DRAIN_S = 30
SIBLING_WINDOW_S = 2.0
"""How long after the holder's start a process its parent started beside it counts as a sibling."""
POLL_S = 5.0
DIR_MODE = 0o777
FILE_MODE = 0o666
EXIT_HELD = 1
EXIT_USAGE = 2
CUSTOM_HOSTS = REPO_ROOT / "tests" / "custom_hosts"
"""The shared frame package the cell's labs need (``zephyr-inline``); projects ``init`` it."""


class UsageError(Exception):
    """A refusal before any lease step: bad arguments, a pid that does not exist, no lab."""


class MalformedRecordError(UsageError):
    """The cell's record cannot be read: ``release --force`` is the way out."""


@dataclass
class Group:
    """One process group of the run: its id, the pid that registered it, whose it is."""

    pgid: int
    pid: int
    owner: str


@dataclass
class Record:
    """The structured lease record (``<cell>.json``)."""

    owner: str
    user: str
    pid: int
    pid_started: int
    groups: list[Group]
    checkout: str
    since: str
    expires: str
    purpose: str
    resources: list[str]

    @classmethod
    def from_json(cls, data: dict) -> "Record":
        """Rebuild a record from its JSON object; a missing or mistyped key raises.

        Unknown keys are ignored: a newer branch's record must stay readable to an
        older one sharing the root, so the lease stays one lease across worktrees.
        """
        known = {f.name for f in fields(cls)}
        groups = [Group(**g) for g in data["groups"]]
        return cls(**{**{k: v for k, v in data.items() if k in known}, "groups": groups})


@dataclass
class Cell:
    """A bed cell: its name, its lab file and the loaded labs."""

    name: str
    lab_file: Path
    labs: list[str]
    resources: list[str]
    """Every resource the cell's labs require; loaded for acquire, which records it."""
    root: Path

    @property
    def record_path(self) -> Path:
        """The structured record, present only while held."""
        return self.root / f"{self.name}.json"

    @property
    def rendered_path(self) -> Path:
        """The otto-format rendering, always present once the cell was ever leased."""
        return self.root / f"{self.name}.reservations.json"


@dataclass
class Liveness:
    """What is still alive of a record's run."""

    holder: bool
    groups: list[Group] = field(default_factory=list)

    @property
    def live(self) -> bool:
        """Whether anything of the run is still running."""
        return self.holder or bool(self.groups)

    def describe(self) -> str:
        """One phrase for the status and held lines."""
        if self.holder:
            return "holder running"
        if not self.groups:
            return "dead"
        note = ""
        if any(g.owner == "foreign" for g in self.groups):
            note = " (one is the launcher's: foreign)"
        return f"holder pid gone, {len(self.groups)} group(s) still running{note}"


# --- the cell ----------------------------------------------------------------


def load_cell(lab_file: Path, root: Path, *, resources: bool = True) -> Cell:
    """Return the cell: its name is the lab file's directory, its labs the file's table.

    With *resources* the labs are loaded through otto and the cell carries the
    resource set a new record needs; release and status, which only name the cell
    and read the record's own resources, skip that load (a lab that fails to load
    must not turn a green lane's release into a red exit).
    """
    if root.is_symlink():
        raise UsageError(f"lease root {root} is a symlink; refusing to follow it")
    if not lab_file.is_file():
        raise UsageError(f"no lab file at {lab_file}")
    try:
        names = list(json.loads(lab_file.read_text()).get("labs", {}))
    except (OSError, ValueError) as exc:
        raise UsageError(f"cannot read {lab_file}: {exc}") from exc
    if not names:
        raise UsageError(f"{lab_file} declares no lab in its `labs` table")
    found = cell_resources(lab_file, names) if resources else []
    return Cell(lab_file.parent.name, lab_file, names, found, root)


def load_labs(lab_file: Path, names: list[str]) -> "Lab":
    """Load the cell's labs as one otto Lab, registering the shared frame package first."""
    if str(CUSTOM_HOSTS) not in sys.path:
        sys.path.insert(0, str(CUSTOM_HOSTS))
    import custom_hosts  # noqa: F401 — registers the frames the cell's hosts name

    import otto

    return otto.load_lab("+".join(names), search_paths=[lab_file.parent])


def cell_resources(lab_file: Path, names: list[str]) -> list[str]:
    """Every resource the cell's labs require, resolved by otto (lab, element, host)."""
    from otto.reservations.check import required_resources

    try:
        lab = load_labs(lab_file, names)
    except Exception as exc:  # any loader refusal is "no lab", named
        raise UsageError(
            f"cannot load labs {'+'.join(names)} from {lab_file.parent}: {exc}"
        ) from exc
    return sorted(required_resources(lab))


def username() -> str:
    """Return the holder's login, as otto resolves the invoking identity on this machine."""
    from otto.reservations.identity import resolve_username

    return resolve_username(None).username


# --- the record --------------------------------------------------------------


def read_record(cell: Cell) -> "Record | None":
    """Read the cell's record, or None when the cell is free; a malformed one is a usage error."""
    if not cell.record_path.exists():
        return None
    try:
        return Record.from_json(json.loads(cell.record_path.read_text()))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise MalformedRecordError(f"malformed lease record {cell.record_path}: {exc}") from exc


def write_record(cell: Cell, record: Record) -> None:
    """Write the structured record, world-writable so another user can take a dead lease over."""
    write_atomically(cell.record_path, json.dumps(asdict(record), indent=2) + "\n", mode=FILE_MODE)


def render(cell: Cell, record: "Record | None") -> None:
    """Write the otto-format file from the record: one open-ended entry, or none."""
    entries = [] if record is None else [{"user": record.user, "resources": record.resources}]
    text = json.dumps({"version": 1, "reservations": entries}, indent=2) + "\n"
    write_atomically(cell.rendered_path, text, mode=FILE_MODE)


def remove_record(cell: Cell) -> None:
    """Free the cell: the empty rendering first, then the record's removal."""
    render(cell, None)
    cell.record_path.unlink(missing_ok=True)


# --- processes ---------------------------------------------------------------


def group_owner(pid: int) -> str:
    """Whose group *pid* is in: ``holder`` (its own), ``ancestor`` (an ancestor's) or ``foreign``.

    The note the record keeps per group; the lease counts every recorded group whatever
    the note says (a launcher's group may be wider than the run, never narrower).
    """
    pgid = os.getpgid(pid)
    if pgid == pid:
        return "holder"
    if pgid in ancestors(pid):
        return "ancestor"
    return "foreign"


def group_of(pid: int) -> Group:
    """Return *pid*'s group as a record entry."""
    return Group(pgid=os.getpgid(pid), pid=pid, owner=group_owner(pid))


def running_members(pgid: int) -> list[int]:
    """Return the members of group *pgid* still running: a zombie has run its course.

    The one reading of "alive" the lease uses, for the holder and for its groups
    alike: an exited process nobody reaped yet is still a group member to the
    kernel, and counting it would hold the bed on a launcher that never waits.
    """
    return [p for p in group_members(pgid) if process_state(p) != "Z"]


def holder_alive(record: Record) -> bool:
    """Whether the holder is that very process, still running."""
    return process_group.alive(record.pid, record.pid_started)


def liveness(record: Record) -> Liveness:
    """Return what is still alive of the record's run: the holder, the live groups."""
    holder = holder_alive(record)
    return Liveness(holder, [g for g in record.groups if running_members(g.pgid)])


def is_run_member(record: Record, pid: int) -> bool:
    """Whether *pid* is the holder or a descendant of it (the holder still being that process)."""
    return holder_alive(record) and (record.pid == pid or record.pid in ancestors(pid))


def survivors(record: Record) -> list[int]:
    """Return the run's processes still running: the recorded groups' members that are its own.

    Not this process or its ancestors (the lane asking is part of the run), and
    not a pipeline sibling: a process the holder's own parent started beside it
    in the same group, within :data:`SIBLING_WINDOW_S` of the holder's start
    (``make lane 2>&1 | tee log``: ``tee`` is in make's group, is not the run's,
    and outlives the lane by design). The window is what tells a sibling from
    a worker a lane left behind that a subreaper parent adopted: the worker
    came seconds to hours later, and finding it is what this function is for.
    """
    me = os.getpid()
    mine = {me, *ancestors(me)}
    launcher = ancestors(record.pid)[:1]
    sibling_until = record.pid_started + SIBLING_WINDOW_S * os.sysconf("SC_CLK_TCK")
    found: set[int] = set()
    for group in record.groups:
        for pid in running_members(group.pgid):
            if pid in mine:
                continue
            chain = ancestors(pid)
            started = process_start(pid)
            if (
                chain[:1] == launcher
                and launcher != [1]
                and record.pid not in chain
                and started is not None
                and started <= sibling_until
            ):
                continue
            found.add(pid)
    return sorted(found)


def drain(record: Record, budget_s: float) -> list[int]:
    """Wait up to *budget_s* for the run's survivors to exit; return the ones left."""
    from otto.utils import WaitTimeoutError, wait_for

    try:
        wait_for(
            lambda: not survivors(record),
            budget_s,
            interval=0.2,
            on_timeout="survivors remain",
        )
    except WaitTimeoutError:
        return survivors(record)
    return []


def drained_run(cell: Cell, record: Record, budget_s: float) -> "tuple[Record | None, list[int]]":
    """Drain *record*'s run outside the lock, then re-read it: the current record and what is left.

    The drain is the long step and runs unlocked; the record is re-read under the
    lock afterwards, and a group a nested acquire registered during the drain is
    counted too, so a run that grew while it was being waited for is not called
    empty. The current record is ``None`` when the cell went free meanwhile, and
    another run's when it changed hands (:func:`same_run` tells).
    """
    left = drain(record, budget_s)
    with _lock(cell):
        current = read_record(cell)
        if same_run(record, current) and current is not None and current.groups != record.groups:
            left = sorted(set(left) | set(survivors(current)))
        return current, left


# --- paths (make quick's classifier) -------------------------------------------


def paths_need_bed(checkout: Path, under: str, paths: list[str]) -> bool:
    """Whether any of *paths* lies outside *under* (both resolved against *checkout*).

    ``make quick``'s one classifier, shared with the reap: normalised, so
    ``tests/unit/../integration``, ``./tests/unit/x``, ``.`` and ``tests``
    classify as what they name. No path at all is the whole tree (pytest's
    reading of an empty selection), so it needs the bed.
    """
    if not paths:
        return True
    base = Path(os.path.normpath(checkout / under))
    for raw in paths:
        path = Path(os.path.normpath(checkout / raw))
        if path != base and base not in path.parents:
            return True
    return False


# --- environment ----------------------------------------------------------------


def seconds_from_env(env: str, default: int) -> int:
    """Return the non-negative seconds *env* holds, *default* when unset or empty."""
    raw = os.environ.get(env)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        value = -1
    if value < 0:
        raise UsageError(f"{env} must be a non-negative integer of seconds, got {raw!r}")
    return value


def _checkout() -> str:
    try:
        return _git(Path.cwd(), "rev-parse", "--show-toplevel").strip()
    except Exception:  # noqa: BLE001 — outside a repository the cwd is the checkout
        return str(Path.cwd())


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse(when: str) -> datetime:
    return datetime.fromisoformat(when)


def _overrun(record: Record) -> "timedelta | None":
    late = _now() - _parse(record.expires)
    return late if late > timedelta(0) else None


def _held_line(cell: Cell, record: Record, alive: Liveness) -> str:
    line = (
        f"bed-lease: {cell.name} is held by {record.owner} ({record.purpose!r}, pid {record.pid}, "
        f"since {record.since}, expires {record.expires}): {alive.describe()}"
    )
    late = _overrun(record)
    if late is not None:
        line += f"; overran its budget by {late}"
    return line


# --- verbs ----------------------------------------------------------------------


def _new_record(pid: int, purpose: str, cell: Cell, ttl_s: int) -> Record:
    now = _now()
    return Record(
        owner=f"{getpass.getuser()}@{socket.gethostname()}",
        user=username(),
        pid=pid,
        pid_started=process_start(pid) or 0,
        groups=[group_of(pid)],
        checkout=_checkout(),
        since=utc_iso(now),
        expires=utc_iso(now + timedelta(seconds=ttl_s)),
        purpose=purpose,
        resources=cell.resources,
    )


def _lock(cell: Cell):  # noqa: ANN202 — a context manager over store_lock, with the lease's modes
    return store_lock(cell.root, dir_mode=DIR_MODE, file_mode=FILE_MODE)


def _try_acquire(cell: Cell, pid: int, purpose: str, ttl_s: int, said: list[str]) -> "str | None":
    """One attempt under the lock: the line to print on success, None while held.

    A held cell's line is printed once, and again only when it changes (a
    waiting acquire polls every :data:`POLL_S` seconds; a two-hour wait must
    not print it hundreds of times).
    """
    with _lock(cell):
        record = read_record(cell)
        if record is None:
            fresh = _new_record(pid, purpose, cell, ttl_s)
            write_record(cell, fresh)
            render(cell, fresh)
            return (
                f"bed-lease: {cell.name} taken by {fresh.owner} (pid {pid}) for {purpose!r} "
                f"until {fresh.expires}"
            )
        if is_run_member(record, pid):
            mine = group_of(pid)
            if mine.pgid not in {g.pgid for g in record.groups}:
                record.groups.append(mine)
                write_record(cell, record)
            render(cell, record)
            line = (
                f"bed-lease: {cell.name} held by this run (pid {record.pid}, since {record.since})"
            )
            late = _overrun(record)
            return line if late is None else f"{line}; overran its budget by {late}"
        alive = liveness(record)
        if alive.live:
            render(cell, record)
            line = _held_line(cell, record, alive)
            if line not in said:
                said.append(line)
                print(line)
            return None
        fresh = _new_record(pid, purpose, cell, ttl_s)
        write_record(cell, fresh)
        render(cell, fresh)
        state = "dead" if _overrun(record) is None else "dead, expired"
        return (
            f"bed-lease: {cell.name} taken over from {record.owner} ({record.purpose!r}, "
            f"since {record.since}): {state}; now {fresh.owner} (pid {pid}) for {purpose!r}"
        )


def acquire(cell: Cell, pid: int, purpose: str) -> int:
    """Take the lease for *pid*'s run, polling for ``BED_LEASE_WAIT`` seconds while held."""
    from otto.utils import WaitTimeoutError, wait_for

    ttl_s = seconds_from_env(TTL_ENV, DEFAULT_TTL_S)
    wait_s = seconds_from_env(WAIT_ENV, 0)
    outcome: list[str] = []
    said: list[str] = []

    def attempt() -> bool:
        line = _try_acquire(cell, pid, purpose, ttl_s, said)
        if line is not None:
            outcome.append(line)
        return bool(outcome)

    if wait_s == 0:
        attempt()
    else:
        with contextlib.suppress(WaitTimeoutError):
            wait_for(attempt, wait_s, interval=POLL_S, on_timeout="the bed stayed held")
    if not outcome:
        return EXIT_HELD
    print(outcome[0])
    return 0


def same_run(a: "Record | None", b: "Record | None") -> bool:
    """Whether two records (either possibly None) name the same run: pid and start time."""
    return a is not None and b is not None and (a.pid, a.pid_started) == (b.pid, b.pid_started)


def release(cell: Cell, pid: int, *, force: bool) -> int:
    """Release the lease *pid*'s run holds (after a drain), or anyone's with *force*.

    The drain (up to ``BED_LEASE_DRAIN`` seconds) runs outside the lock: the lock
    is one per root and only for short steps, and a status or a fast-failing
    acquire must not block behind a lane's survivors. The record is re-read
    under the lock afterwards and removed only if it is still this run's.
    """
    with _lock(cell):
        try:
            record = read_record(cell)
        except MalformedRecordError as exc:
            if not force:
                raise
            remove_record(cell)
            print(f"bed-lease: {cell.name} released by force; evicted an unreadable record ({exc})")
            return 0
        if record is None:
            render(cell, None)
            print(f"bed-lease: {cell.name} free")
            return 0
        if force:
            remove_record(cell)
            print(
                f"bed-lease: {cell.name} released by force; evicted {record.owner} "
                f"({record.purpose!r}, pid {record.pid})"
            )
            return 0
        if not (record.pid == pid and holder_alive(record)):
            render(cell, record)
            print(f"bed-lease: {cell.name} not held by pid {pid} (held by {record.owner}); kept")
            return 0
    current, left = drained_run(cell, record, seconds_from_env(DRAIN_ENV, DEFAULT_DRAIN_S))
    with _lock(cell):
        if not same_run(record, current) or read_record(cell) != current:
            render(cell, current)
            print(f"bed-lease: {cell.name} is no longer pid {pid}'s during its drain; kept as is")
            return 0
        if left:
            render(cell, current)
            print(
                f"bed-lease: {cell.name} kept: {len(left)} process(es) of this run still running "
                f"(pids {' '.join(map(str, left))}); the next acquirer takes it over when they "
                "are gone, `make bed-release` evicts it"
            )
            return 0
        remove_record(cell)
        print(f"bed-lease: {cell.name} released by pid {pid}")
        return 0


def status(cell: Cell) -> int:
    """Print the cell's lease, or ``free``; always exit 0."""
    with _lock(cell):
        record = read_record(cell)
        render(cell, record)
    if record is None:
        print(f"{cell.name}: free")
        return 0
    alive = liveness(record)
    print(f"{cell.name}: held by {record.owner} for {record.purpose!r}")
    print(f"  pid {record.pid} ({alive.describe()})")
    for group in record.groups:
        state = "running" if running_members(group.pgid) else "gone"
        print(f"  group {group.pgid} ({group.owner}, registered by pid {group.pid}): {state}")
    print(f"  since {record.since}, expires {record.expires}")
    late = _overrun(record)
    if late is not None:
        print(f"  overran its budget by {late}")
    print(f"  checkout {record.checkout}")
    print(f"  resources {' '.join(record.resources)}")
    return 0


# --- CLI ------------------------------------------------------------------------


def add_cell_arguments(parser: argparse.ArgumentParser) -> None:
    """``--lab`` and ``--root``: how a lease verb and the reap name the cell and its store."""
    parser.add_argument("--lab", required=True, type=Path, help="the cell's lab.json")
    parser.add_argument(
        "--root",
        type=Path,
        default=None,
        help=f"lease root (default ${ROOT_ENV}, then {DEFAULT_ROOT})",
    )


def add_run_arguments(parser: argparse.ArgumentParser, *, skip_help: str) -> None:
    """Add ``--pid`` and ``make quick``'s classifier: the run a verb names, and when to skip."""
    parser.add_argument("--pid", type=int, required=True, help="the run's pid (the lane's make)")
    parser.add_argument("--unless-all-under", metavar="DIR", help=f"{skip_help} (make quick)")
    parser.add_argument("paths", nargs="*")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="the bed lease: one run per bed cell")
    add_cell_arguments(parser)
    verbs = parser.add_subparsers(dest="verb", required=True)
    acq = verbs.add_parser("acquire", help="take the cell's lease for PID's run")
    add_run_arguments(acq, skip_help="take no lease when every PATH after `--` is under DIR")
    acq.add_argument("--purpose", default="")
    rel = verbs.add_parser("release", help="release the lease PID's run holds")
    add_run_arguments(rel, skip_help="do nothing when every PATH after `--` is under DIR")
    rel.add_argument("--force", action="store_true", help="release whoever holds it")
    verbs.add_parser("status", help="print the cell's lease")
    return parser


def classifier_says_skip(args: argparse.Namespace) -> bool:
    """Whether ``--unless-all-under DIR -- PATHS`` says this verb has no bed to touch.

    The lease's and the reap's one reading of ``make quick``'s classifier: stray
    PATHS without the option are a usage error, and with it the verb is skipped
    when :func:`paths_need_bed` says every path is under DIR.
    """
    if args.paths and args.unless_all_under is None:
        raise UsageError("PATHS are only read after --unless-all-under DIR --")
    return args.unless_all_under is not None and not paths_need_bed(
        Path.cwd(), args.unless_all_under, args.paths
    )


def require_pid(pid: int) -> None:
    """Refuse a *pid* no process has (usage: the caller named the wrong run)."""
    if process_start(pid) is None:
        raise UsageError(f"no process with pid {pid}")


def root_from(arg: "Path | None") -> Path:
    """Return the lease root: the ``--root`` argument, else ``BED_LEASE_ROOT``, else the default."""
    return arg or Path(os.environ.get(ROOT_ENV) or DEFAULT_ROOT)


def main(argv: "list[str] | None" = None) -> int:
    """Run one verb; 0 taken or done, 1 held by another run, 2 usage."""
    args = _parser().parse_args(argv)
    root = root_from(args.root)
    try:
        if args.verb in ("acquire", "release") and classifier_says_skip(args):
            print("bed-lease: no bed path in PATHS, no lease")
            return 0
        if args.verb == "acquire":
            seconds_from_env(WAIT_ENV, 0)
            seconds_from_env(TTL_ENV, DEFAULT_TTL_S)
            require_pid(args.pid)
            cell = load_cell(args.lab, root)
            return acquire(cell, args.pid, args.purpose)
        if args.verb == "release":
            require_pid(args.pid)
            cell = load_cell(args.lab, root, resources=False)
            return release(cell, args.pid, force=args.force)
        cell = load_cell(args.lab, root, resources=False)
        return status(cell)
    except UsageError as exc:
        print(f"bed-lease: {exc}", file=sys.stderr)
        return EXIT_USAGE


if __name__ == "__main__":
    sys.exit(main())
