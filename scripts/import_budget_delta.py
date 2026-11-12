"""The import-budget delta: the previous release's file operations against HEAD's.

`make profile` gates every surface against a ceiling, a recorded baseline plus
10 % headroom. Growth that stays under the ceiling passes it silently, release
after release. This script is the release's answer: it measures the base (the
last release tag, `git describe --tags --abbrev=0`) and the tip (HEAD), one
throwaway venv per Python minor synced from each tree, with ONE instrument —
the tip's `scripts/import_budget.py`, which measures whatever interpreter runs
it (the base's venv holds the base's otto) — and STOPS the release on any
surface that costs more file operations at the tip than at the base, on any
minor, unless the committed approvals file
(`tests/unit/import_budget/approved_growth.json`) holds an entry for it: the
surface, maybe one minor, the buckets and the amount each may grow by, and a
`why`. Growth is approved when every moved bucket is named by the entry and
within its amount (an unnamed bucket may move by `BUCKET_NOISE`). The file
names the release tag it was written against; against any other base its
entries are spent (NOTE lines), never evidence. A stop prints the entry the
measurement warrants, to paste once its `why` is written; an entry nothing
grew for, over the whole span, is a NOTE (unused), never a stop.

File operations only, never wall clock. Serial, one interpreter at a time, the
primary first. Every minor is judged, whatever an earlier one found: the run
stops once, at the end, with every minor's verdict printed and, when anything
grew beyond its approval, ONE complete approvals file to commit (the entries
that were used, plus the ones the stops warrant), so a release is not
re-run once per minor. When `<out>/other/<minor>.json` exists (the other
environment's records for that minor, placed by hand), a grown surface's stop
line is followed by that environment's numbers.

Growth or a bucket shuffle (buckets moved, total unchanged) is never a verdict
on one measurement: the pair is measured again, and a verdict stands only when
two consecutive pairs agree record for record, both sides (each bucket within
`BUCKET_NOISE`, totals exact) and on which surfaces shuffled — pairs 1 and 2
confirm pair 1's, else a third pair is measured once and pairs 2 and 3 confirm
pair 2's. Three pairs that never agree are inconclusive: a stop that names the
surfaces whose records differed, not a regression. The tables printed are the
deciding pair's; an inconclusive minor prints every pair's, labelled.

A record directory (a SIDE) holds, per minor M: `M.json` (the rows the
harness wrote: per surface its file operations, workspace count, exit, the
harness's own `ending` verdict, buckets and per-path counts) and `M.status.json`
(every step's exit status and elapsed seconds, the sha and measured tree, the
interpreter's full version, the instrument that measured). A row without
`by_path` (a cache from before it was recorded) is refused by name, so the
first run after it landed measured every cached side afresh. The paths only
attribute: a growth stop's tables name the paths that moved, and no verdict
reads them. A base surface whose `ending` is not empty did not end as the
surface expects at the tag (its reason is
printed) and is listed, not judged; at the tip it is invalid evidence. Under
`--out` (reports/budget/, gitignored, per checkout):

    <base-tag>/        the base's records, reused by every later run whose
                       provenance matches (measured tree, interpreter, instrument)
    <measured tree>/   the tip's, by its measured tree (the committed tree minus
                       the approvals file), reused on the same terms; other
                       tips' directories are removed at the start of a run
      again-1/, again-2/  that tip's confirming pairs (<base-tag>/ and <measured
                       tree>/ inside), reused on the same terms and removed
                       with their tip: an approvals-only retry measures nothing
    scratch/           the interpreter probes' output; not records
    run.log            every command, its exit status and elapsed seconds

Each side and minor is measured in `$TMPDIR/otto-budget-delta/<side>-<minor>/`
(its worktrees and venv), removed before and after every measurement; the path
is stable so the harness's persistent bytecode cache
(`~/.cache/otto/import-budget-pycache`, which mirrors absolute paths) keeps one
mirror per side and minor instead of a new one per run. It is one per box,
shared by every checkout of the repository: never run two at once.

Exit statuses, the worst over the minors. `measure` and `judge`: 0 accepted
(nothing grew, or every growth is approved by an in-force entry; declines,
unreproduced shuffles, surfaces the base cannot run and spent or unused
entries are reported); 1 a policy STOP (a line per reason; the stops print
the complete approvals file to write); 2 invalid evidence or an operational
failure (a step, git or the filesystem failed, a record is not evidence, the
approvals file is malformed, more than three
pairs); `measure` still judges the other minors after one failed. `validate`:
0 evidence, 1 not. `tables`: 0, or 2 on a side that is not evidence.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType

REPO_ROOT = Path(__file__).resolve().parent.parent

KNOWN_SURFACES = [
    "import_otto", "help", "run", "host", "reservation", "docker", "schema", "monitor", "test",
    "cov", "run_bootstrapped", "version_project", "help_project", "help_project_warm",
    "bootstrap_project", "dispatch_project_warm", "completion_project_warm",
    "completion_project_handover", "test_project", "host_local_exec", "host_local_put",
    "host_local_get", "host_ssh_exec", "host_ssh_login", "dispatch_local_warm", "tracked_init",
    "tracked_env", "tracked_cache", "tracked_docker", "tracked_link", "tracked_tunnel",
    "tracked_monitor", "tracked_cov", "tracked_reservation", "tracked_inventory", "tracked_schema",
    "tracked_host_probe", "tracked_host_power",
]  # fmt: skip
"""The surfaces scripts/import_budget.py measures, literally: every record must hold each once.

A literal list, never a record's own contents, so a surface that vanished from
a report is refused rather than silently uncompared. Each row carries every
field `Record` names, the harness's `ending` verdict among them.
tests/unit/scripts/test_import_budget_delta.py pins it to import_budget.SURFACES.
"""

REPO_SURFACES = [
    "version_project", "help_project", "help_project_warm", "bootstrap_project",
    "dispatch_project_warm", "completion_project_warm", "completion_project_handover",
    "test_project", "host_local_exec", "host_local_put", "host_local_get", "host_ssh_exec",
    "host_ssh_login", "dispatch_local_warm", "tracked_init", "tracked_env", "tracked_cache",
    "tracked_docker", "tracked_link", "tracked_tunnel", "tracked_monitor", "tracked_cov",
    "tracked_reservation", "tracked_inventory", "tracked_schema", "tracked_host_probe",
    "tracked_host_power",
]  # fmt: skip
"""The surfaces that run inside the harness's generated repo (a `project_files` surface).

The generated repo is one fixture: when it cannot run at the base, none of
these surfaces' base measurements is evidence, whatever its exit.
Pinned to import_budget.SURFACES by the tests.
"""

FIXTURE_CANARIES = ["dispatch_project_warm"]
"""The repo surfaces that dispatch the fixture's own `noop` instruction.

When one of them did not end as expected at the base, the generated repo does
not run there, and every surface of REPO_SURFACES is listed as not measurable
on that minor with the canary's reason. Pinned to import_budget.SURFACES.
"""

STEPS = ["sync", "version", "report"]
"""The steps `measure` records per side and minor, in the order they run."""

APPROVALS_PATH = Path("tests/unit/import_budget/approved_growth.json")
"""The committed approvals file, relative to the repository root."""

SUPPORTED_MINORS = ["3.10", "3.11", "3.12", "3.13", "3.14"]
"""Every minor otto supports: the Makefile's nox list, pinned by a test.

An approval may name any of them; the pure verbs measure them when none are given.
"""

MAX_PAIRS = 3
"""Measurements of one minor's pair before the minor is inconclusive."""

CONFIRMING_PAIR = 2
"""The pair that confirms the first pair's verdict when the two agree."""

BUCKET_NOISE = 2
"""The most file operations a bucket may differ by between two maps that agree.

Two measurements of one tree are not identical: an operation can land in one
bucket on one run and another on the next (0.17.0's 3.10 `host_local_exec`
flips one between `fd` and `other` at an unchanged total). Pairs whose buckets
agree within it reproduce, an approval within it of the measurement holds, and
a move within it is no shuffle. Totals stay exact: one operation more than the
base is growth, so two pairs agree only on the same totals, and an approval
covers exactly the growth measured."""

INSTRUMENT_PATHS = ["scripts/import_budget.py", "tests/_fixtures"]
"""What a measurement depends on besides the measured tree: the harness and fixtures, at the tip."""

Runner = Callable[["list[str]", Path, "dict[str, str]", "Path | None"], int]
"""(argv, cwd, env, stdout file or None) -> exit status: `measure`'s one door to the world."""


@dataclass(frozen=True)
class Record:
    """One surface's measurement on one side and minor."""

    surface: str
    file_ops: int
    workspace: int
    exit: "int | None"
    by_bucket: "dict[str, int]"
    ending: "list[str]"
    """The harness's reasons the run did not end as the surface expects; empty when it did."""
    by_path: "dict[str, int]"
    """Calls per `<bucket>:<path below its root>` (`fd`: no path); attribution only."""


@dataclass(frozen=True)
class Provenance:
    """What a cached record must have been measured on, and with, to be reused."""

    tree: str
    """The measured tree (:func:`measured_tree`), compared with the status's `measured_tree`."""
    python: str
    """The interpreter's full version (`platform.python_version()`), not just the minor."""
    instrument: str
    """The harness blob and fixtures tree at the tip, `<blob>:<tree>`."""


@dataclass(frozen=True)
class Approval:
    """One committed approval: a surface (maybe one minor) may grow by up to these buckets."""

    surface: str
    minor: "str | None"
    buckets: "dict[str, int]"
    why: str

    def label(self) -> str:
        """Return `surface` or `surface@minor`: how every message names the entry."""
        return self.surface if self.minor is None else f"{self.surface}@{self.minor}"

    def entry(self) -> "dict[str, object]":
        """Return the JSON object this approval is written as in the approvals file."""
        return {
            "surface": self.surface,
            "minor": self.minor,
            "buckets": self.buckets,
            "why": self.why,
        }


@dataclass(frozen=True)
class Approvals:
    """The approvals file as read against one base tag."""

    base: str
    """The release tag the file was written against."""
    in_force: "list[Approval]"
    """The entries that count: all of them when `base` is the base tag, else none."""
    spent: "list[str]"
    """One note per entry of a file written against another base: spent, not evidence."""


def _load(path: Path) -> object:
    return json.loads(path.read_text())


def _count_problem(name: str, value: object) -> "str | None":
    if not isinstance(value, int) or isinstance(value, bool):
        return f"{name} is not an int"
    if value < 0:
        return f"{name} is negative"
    return None


def _end_problem(row: "dict[str, object]") -> "str | None":
    """Why *row*'s exit or ending is misshapen; None when both are well-formed."""
    if "exit" not in row:
        return "exit is missing"
    if row["exit"] is not None and (
        not isinstance(row["exit"], int) or isinstance(row["exit"], bool)
    ):
        return "exit is not an int or null"
    if "ending" not in row:
        return "ending is missing"
    ending = row["ending"]
    if not isinstance(ending, list) or not all(isinstance(r, str) for r in ending):
        return "ending is not a list of str"
    return None


def _paths_problem(row: "dict[str, object]") -> "str | None":
    """Why *row*'s `by_path` is missing or misshapen; None when it is a `{str: count}`."""
    paths = row.get("by_path")
    if paths is None:
        return f"{row['surface']}: no by_path"
    if not isinstance(paths, dict):
        return "by_path is not a dict"
    for k, v in paths.items():
        if not isinstance(k, str) or not isinstance(v, int) or isinstance(v, bool) or v < 0:
            return "by_path is not {str: non-negative int}"
    return None


def _row_problem(row: object) -> "str | None":
    """Why *row* is not a Record's worth of fields; None when it is."""
    if not isinstance(row, dict):
        return "not an object"
    if not isinstance(row.get("surface"), str):
        return "surface is not a str"
    for name in ("file_ops", "workspace"):
        bad = _count_problem(name, row.get(name))
        if bad is not None:
            return bad
    bad = _end_problem(row)
    if bad is not None:
        return bad
    buckets = row.get("by_bucket")
    if not isinstance(buckets, dict):
        return "by_bucket is not a dict"
    for k, v in buckets.items():
        if not isinstance(k, str) or not isinstance(v, int) or isinstance(v, bool):
            return "by_bucket is not {str: int}"
        if v < 0:
            return "by_bucket holds a negative count"
    return _paths_problem(row)


def rows(side: Path, minor: str) -> "dict[str, Record]":
    """Read the side's records for *minor*, by surface. Validate first; this trusts the files."""
    table = _load(side / f"{minor}.json")
    if not isinstance(table, list):
        raise TypeError(f"{side}: {minor} report is not a list of rows")
    return {
        row["surface"]: Record(
            row["surface"],
            row["file_ops"],
            row["workspace"],
            row["exit"],
            dict(row["by_bucket"]),
            list(row["ending"]),
            dict(row["by_path"]),
        )
        for row in table
    }


def _status_problems(
    side: Path, minor: str, status: object, expect: "Provenance | None"
) -> "list[str]":
    """Return the status record's problems; a failed step is the only problem it reports."""
    if not isinstance(status, dict):
        return [f"{side}: {minor} status is not an object"]
    failed = [
        f"{side}: {minor} step {s} exited {status.get(s)}, not 0"
        for s in STEPS
        if status.get(s) != 0
    ]
    if failed:
        return failed
    problems = []
    python = str(status.get("python"))
    if not python.startswith(f"{minor}."):
        problems.append(f"{side}: {minor} ran under {python}")
    if expect is not None:
        fields = (
            ("measured_tree", expect.tree),
            ("python", expect.python),
            ("instrument", expect.instrument),
        )
        problems += [
            f"{side}: {minor} {n} {status.get(n)}, not {w}" for n, w in fields if status.get(n) != w
        ]
    return problems


def _table_problems(side: Path, minor: str) -> "list[str]":
    """Return the report's problems: readable, well-formed rows, one per known surface, sums."""
    try:
        table = _load(side / f"{minor}.json")
    except (OSError, ValueError) as e:
        return [f"{side}: {minor} unreadable: {e}"]
    if not isinstance(table, list):
        return [f"{side}: {minor} report is not a list of rows"]
    for row in table:
        bad = _row_problem(row)
        if bad is not None:
            name = row.get("surface", "?") if isinstance(row, dict) else "?"
            return [f"{side}: {minor} row {name} is misshapen ({bad})"]
    problems = []
    got = [row["surface"] for row in table]
    if len(got) != len(set(got)) or sorted(got) != sorted(KNOWN_SURFACES):
        problems.append(
            f"{side}: {minor} reports {len(got)} rows, not one per known surface: "
            f"{sorted(set(got) ^ set(KNOWN_SURFACES))}"
        )
    for row in table:
        total = sum(row["by_bucket"].values())
        if total != row["file_ops"]:
            problems.append(
                f"{side}: {minor} {row['surface']} buckets sum to {total}, "
                f"not its {row['file_ops']} file ops"
            )
    return problems


def validate_side(side: Path, minor: str, expect: "Provenance | None") -> "list[str]":
    """Every reason *side* is not evidence for *minor*; empty when it is.

    *expect*, when given, is the provenance a cached record must match to be
    reused (the base cache); None validates the records alone (a fresh side).
    A wrong exit on a surface is NOT a problem here: the judge lists such a
    surface as not measurable on that side (the tip's rule is the judge's).
    """
    status_path = side / f"{minor}.status.json"
    if not status_path.is_file():
        return [f"{side}: {minor} not measured (no status)"]
    try:
        status = _load(status_path)
    except (OSError, ValueError) as e:
        return [f"{side}: {minor} unreadable: {e}"]
    problems = _status_problems(side, minor, status, expect)
    if any(p.endswith(", not 0") for p in problems):
        return problems
    return problems + _table_problems(side, minor)


def _read_entry(path: Path, i: int, raw: object) -> Approval:
    """Return entry *i* (*raw*) of the file at *path* as an Approval; ValueError if it is none."""
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: entry {i} is not an object")  # noqa: TRY004
    surface, minor, why, buckets = (raw.get(k) for k in ("surface", "minor", "why", "buckets"))
    if surface not in KNOWN_SURFACES:
        raise ValueError(f"{path}: entry {i}: {surface!r} is not a known surface")
    where = f"{path}: entry {i} ({Approval(surface, minor, {}, '').label()})"
    if minor is not None and minor not in SUPPORTED_MINORS:
        raise ValueError(f"{where}: not a supported minor")
    if not isinstance(why, str) or not why.strip():
        raise ValueError(f"{where}: why is missing or empty")
    if why.strip() == WARRANTED_WHY:
        raise ValueError(f"{where}: why is still the placeholder; replace it with the reason")
    if not isinstance(buckets, dict) or not buckets:
        raise ValueError(f"{where}: buckets is not a non-empty object")
    for name, amount in buckets.items():
        if not isinstance(amount, int) or isinstance(amount, bool) or amount <= 0:
            raise ValueError(f"{where}: bucket {name!r} is not a positive int")
    return Approval(surface, minor, dict(buckets), why)


def load_approvals(path: Path, base_tag: str) -> Approvals:
    """Read the approvals file at *path* against *base_tag*; ValueError names the first problem.

    Every entry is checked whatever the file's base, so a malformed entry
    never hides behind a spent file. An entry holds until the next release
    tag: a file written against another base puts all its entries in `spent`
    and none in force. An all-minors entry and a per-minor entry for one
    surface may coexist: the per-minor one decides on its minor
    (:func:`_applicable`), the all-minors one everywhere else. An entry may
    name any supported minor; one this run does not measure is simply never
    applied (CI measures one minor per leg).
    """
    try:
        data = _load(path)
    except (OSError, ValueError) as e:
        raise ValueError(f"{path}: unparsable: {e}") from e
    if not isinstance(data, dict) or not isinstance(data.get("base"), str) or not data["base"]:
        raise ValueError(f"{path}: no base")
    raw_entries = data.get("entries")
    if not isinstance(raw_entries, list):
        raise ValueError(f"{path}: entries is not a list")  # noqa: TRY004
    entries: "list[Approval]" = []
    for i, raw in enumerate(raw_entries):
        got = _read_entry(path, i, raw)
        if any(a.surface == got.surface and a.minor == got.minor for a in entries):
            raise ValueError(f"{path}: entry {i} ({got.label()}): a second entry for {got.label()}")
        entries.append(got)
    if data["base"] == base_tag:
        return Approvals(data["base"], entries, [])
    spent = [
        f"{a.label()}: approved for {data['base']}, the base is {base_tag} (spent)" for a in entries
    ]
    return Approvals(data["base"], [], spent)


@dataclass
class Verdict:
    """What the judge found for one minor."""

    minor: str
    stop: "list[str]" = field(default_factory=list)
    named: "list[str]" = field(default_factory=list)
    declined: "list[str]" = field(default_factory=list)
    shuffles: "list[str]" = field(default_factory=list)
    shuffled: "list[str]" = field(default_factory=list)
    """Each surface whose buckets moved beyond the noise at an unchanged total."""
    unmeasurable: "dict[str, str]" = field(default_factory=dict)
    """Each base surface not judged, by name, with why (sorted by surface)."""
    grown: "list[tuple[str, str]]" = field(default_factory=list)
    """Each (surface, minor) that grew in the deciding pair, approved or not; staleness reads it."""
    used: "list[Approval]" = field(default_factory=list)
    attribution: "dict[str, list[str]]" = field(default_factory=dict)
    """Each grown surface -> the paths that moved (:func:`path_attribution`), for the tables."""
    growth_stops: "dict[int, str]" = field(default_factory=dict)
    """Index in `stop` of each growth stop line -> its surface (for the other environment)."""
    warranted: "list[dict]" = field(default_factory=list)
    """The approvals-file entry each stopped growth warrants, to paste once its why is written."""
    reason: "str | None" = None
    """Why a second pair is needed: "growth", "a shuffle", or None for a quiet pair."""
    more: bool = False
    """True when one more pair would decide (`measure` measures it; `judge` says so)."""
    confirmed: "int | None" = None
    """The pair that confirmed the verdict (2 or 3); None when one measurement was enough."""
    evidence: "int | None" = 0
    """Index of the pair the verdict (and its tables) describes; None when inconclusive."""


def _harness() -> "ModuleType":
    """Return the tip's harness module, `scripts/import_budget.py`, imported under this otto."""
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    from scripts import import_budget

    return import_budget


def expected_exits() -> "dict[str, int | None]":
    """Surface -> the exit a real-entry surface must end with (None for a harness child)."""
    return {s.key: (s.expect_exit if s.real_entry else None) for s in _harness().SURFACES}


def _moved(before: "dict[str, int]", after: "dict[str, int]") -> "dict[str, int]":
    keys = sorted(set(before) | set(after))
    return {
        k: after.get(k, 0) - before.get(k, 0) for k in keys if after.get(k, 0) != before.get(k, 0)
    }


def within_noise(a: "dict[str, int]", b: "dict[str, int]") -> bool:
    """Return whether *a* and *b* agree: the same total, no bucket over BUCKET_NOISE apart."""
    moved = _moved(a, b).values()
    return sum(moved) == 0 and all(abs(d) <= BUCKET_NOISE for d in moved)


def _applicable(approvals: "list[Approval]", surface: str, minor: str) -> "Approval | None":
    """Return the entry for *surface* on *minor*: the minor-specific one, else the generic one."""
    exact = [a for a in approvals if a.surface == surface and a.minor == minor]
    generic = [a for a in approvals if a.surface == surface and a.minor is None]
    return (exact or generic or [None])[0]


def _ended_wrong(record: Record, expected: "int | None") -> "str | None":
    """Why *record*'s run did not end as its surface expects; None when it did.

    The harness's own verdict first (it names the exit, an escaped exception or
    the module the surface must have loaded); the exit alone as a fallback.
    """
    if record.ending:
        return "; ".join(record.ending)
    if expected is not None and record.exit != expected:
        return f"exited {record.exit}, not {expected}"
    return None


def _unmeasurable(
    records: "dict[str, Record]", expected: "dict[str, int | None]", base_name: str
) -> "dict[str, str]":
    """Return the base's surfaces that are not evidence, by name, with why.

    A surface whose run did not end as expected is listed with the harness's
    reason. When a fixture canary is among them, the generated repo does not
    run at the base, so every other repo surface is listed with its reason.
    """
    out = {k: why for k, r in records.items() if (why := _ended_wrong(r, expected[k])) is not None}
    broken = [c for c in FIXTURE_CANARIES if c in out]
    if broken:
        canary = broken[0]
        because = f"the generated repo does not run at {base_name}: {canary}: {out[canary]}"
        out.update({k: because for k in REPO_SURFACES if k in records and k not in broken})
    return dict(sorted(out.items()))


WARRANTED_WHY = "<why: say what this growth buys>"
"""The placeholder `why` of a warranted entry; the owner replaces it before committing."""


def _within_amounts(entry: Approval, moved: "dict[str, int]") -> bool:
    """Whether *moved* fits *entry*: a named bucket up to its amount, others by the noise."""
    return all(n <= entry.buckets.get(b, BUCKET_NOISE) for b, n in moved.items())


def _growth_line(
    v: Verdict, line: str, key: str, moved: "dict[str, int]", approvals: "list[Approval]"
) -> None:
    """Classify one surface's growth into a stop or a named approval."""
    entry = _applicable(approvals, key, v.minor)
    if entry is not None and _within_amounts(entry, moved):
        v.named.append(f"{line}: approved up to {entry.buckets} ({entry.why})")
        v.used.append(entry)
        return
    v.growth_stops[len(v.stop)] = key
    if entry is None:
        v.stop.append(f"{line}: grew and is not approved")
    else:
        v.stop.append(f"{line}: grew beyond {entry.buckets} ({entry.why})")
    # Built from the applicable entry: its buckets stand (another environment's
    # allowance, or the generic entry this per-minor one shadows on v.minor),
    # and only the buckets this measurement exceeds are raised.
    warranted = dict(entry.buckets) if entry is not None else {}
    for b, n in moved.items():
        if n > warranted.get(b, 0):
            warranted[b] = n
    v.warranted.append(Approval(key, v.minor, warranted, WARRANTED_WHY).entry())


def path_attribution(before: Record, after: Record) -> "list[str]":
    """Return the paths that moved between two records of one surface, one line each.

    `+ <path> <n>` is a path the base never touched, `- <path> <n>` one the
    tip no longer does, `  <path> <before> -> <after>` one both touch a
    different number of times. New and vanished paths come first, then the
    changed ones; each group by size of the move, largest first, then path.
    Empty when no path moved. Never an input to a verdict: the judge reads
    totals and buckets only.
    """
    moves = []
    for path in set(before.by_path) | set(after.by_path):
        b, a = before.by_path.get(path, 0), after.by_path.get(path, 0)
        if a == b:
            continue
        if b == 0:
            moves.append((0, a, path, f"+ {path} {a}"))
        elif a == 0:
            moves.append((0, b, path, f"- {path} {b}"))
        else:
            moves.append((1, abs(a - b), path, f"  {path} {b} -> {a}"))
    return [line for _group, _size, _path, line in sorted(moves, key=lambda m: (m[0], -m[1], m[2]))]


def judge_minor(base: Path, tip: Path, minor: str, approvals: "list[Approval]") -> Verdict:
    """Compare one measured pair on *minor*. Both sides validated by the caller."""
    before, after = rows(base, minor), rows(tip, minor)
    v = Verdict(minor, unmeasurable=_unmeasurable(before, expected_exits(), base.name))
    for key in sorted(k for k in before if k not in v.unmeasurable):
        b, a = before[key], after[key]
        delta = a.file_ops - b.file_ops
        moved = _moved(b.by_bucket, a.by_bucket)
        line = f"{minor} {key} {b.file_ops} -> {a.file_ops} {moved}"
        if delta > 0:
            v.grown.append((key, minor))
            v.attribution[key] = path_attribution(b, a)
            _growth_line(v, line, key, moved, approvals)
        elif delta < 0:
            v.declined.append(f"{minor} {key} {b.file_ops} -> {a.file_ops}")
        elif not within_noise(b.by_bucket, a.by_bucket):
            v.shuffles.append(f"{minor} {key} {b.file_ops} moved {moved}")
            v.shuffled.append(key)
    if v.grown:
        v.reason = "growth"
    elif v.shuffles:
        v.reason = "a shuffle"
    return v


def reproduced(first: Path, second: Path, minor: str) -> "list[str]":
    """Return the surfaces whose record differs between two measurements of one tree.

    Every field the judge reads is compared: the exit, the file operations,
    the buckets (each within BUCKET_NOISE) and the workspace count. A
    surface neither run could measure (a non-empty ending) agrees whatever
    else it holds, since it is listed and never judged and its ending text can
    quote a per-run temp path; one measurable in only one of the runs differs.
    The result is empty when nothing differs.
    """
    a, b = rows(first, minor), rows(second, minor)
    out = []
    for key in sorted(set(a) | set(b)):
        if key not in a or key not in b:
            out.append(f"{key}: measured on one run only")
        elif bool(a[key].ending) != bool(b[key].ending):
            out.append(f"{key}: measurable in one measurement, not the other")
        elif a[key].ending:
            continue
        elif a[key].exit != b[key].exit:
            out.append(f"{key}: ended differently ({a[key].exit} vs {b[key].exit})")
        elif a[key].file_ops != b[key].file_ops:
            out.append(f"{key}: {a[key].file_ops} vs {b[key].file_ops} file ops")
        elif not within_noise(a[key].by_bucket, b[key].by_bucket):
            out.append(f"{key}: {a[key].by_bucket} vs {b[key].by_bucket}")
        elif a[key].workspace != b[key].workspace:
            out.append(f"{key}: workspace {a[key].workspace} vs {b[key].workspace}")
    return out


def _disagreement(pairs: "list[tuple[Path, Path]]", i: int, minor: str) -> "list[str]":
    """How pair i and pair i+1 differ, as '<side> <surface>: …' lines, then '<surface>: …' ones.

    Records that agree within the noise can still straddle it, a shuffle in one
    pair and none in the other: that is a disagreement of the pairs, not of a
    side, and is named by surface alone.
    """
    (b1, t1), (b2, t2) = pairs[i], pairs[i + 1]
    one, two = (set(judge_minor(*pair, minor, []).shuffled) for pair in (pairs[i], pairs[i + 1]))
    return (
        [f"base {d}" for d in reproduced(b1, b2, minor)]
        + [f"tip {d}" for d in reproduced(t1, t2, minor)]
        + [f"{key}: a shuffle in one pair, not the other" for key in sorted(one ^ two)]
    )


def _confirmed(verdict: Verdict, by: int, evidence: int) -> Verdict:
    verdict.confirmed, verdict.evidence, verdict.more = by, evidence, False
    verdict.stop += [f"{s}: reproduced" for s in verdict.shuffles]
    return verdict


def _pending(minor: str, text: str, reason: "str | None") -> Verdict:
    return Verdict(minor, stop=[text], reason=reason, more=True, evidence=None)


def decide(pairs: "list[tuple[Path, Path]]", minor: str, approvals: "list[Approval]") -> Verdict:
    """Return the verdict for *minor* over one to three measured (base, tip) pairs.

    A quiet single pair decides. Otherwise two consecutive agreeing pairs
    decide, and the verdict is the earlier one's; a pending verdict (`more`)
    says which measurement would decide; three pairs that never agree are
    inconclusive. The first pair's reading is never kept once a later pair
    failed to reproduce it.
    """
    if len(pairs) > MAX_PAIRS:
        raise ValueError(f"at most {MAX_PAIRS} pairs: {len(pairs)} given")
    first = judge_minor(*pairs[0], minor, approvals)
    if len(pairs) == 1:
        if first.reason is None:
            return first
        return _pending(
            minor,
            f"{minor} needs a second measurement to confirm {first.reason} (judge --pair twice)",
            first.reason,
        )
    diff12 = _disagreement(pairs, 0, minor)
    if not diff12:
        return _confirmed(first, CONFIRMING_PAIR, 0)
    joined12 = "\n  ".join(diff12)
    if len(pairs) == CONFIRMING_PAIR:
        text = (
            f"{minor} did not reproduce (pair 1 vs 2):\n  {joined12}\n"
            "  needs a third measurement (judge --pair three times)"
        )
        return _pending(minor, text, first.reason)
    diff23 = _disagreement(pairs, 1, minor)
    if not diff23:
        return _confirmed(judge_minor(*pairs[1], minor, approvals), MAX_PAIRS, 1)
    joined23 = "\n  ".join(diff23)
    text = (
        f"{minor} inconclusive after three measurements:\n  pair 1 vs 2: {joined12}\n"
        f"  pair 2 vs 3: {joined23}\n  measure again when the box is quiet"
    )
    return Verdict(minor, stop=[text], evidence=None)


def stale_approvals(
    approvals: "list[Approval]",
    grown: "list[tuple[str, str]]",
    unresolved: "list[str] | None" = None,
    measured: "list[str] | None" = None,
) -> "list[str]":
    """Return a NOTE line per entry whose surface grew on no minor in its scope, over the span.

    *unresolved* names the minors with no verdict yet (pending or inconclusive):
    an entry whose scope touches one is not judged, because growth there is
    not yet known either way. *measured*, when given, names the minors this
    run measured: a per-minor entry for another one is not judged either.
    """
    pending = set(unresolved or [])
    out = []
    for a in approvals:
        if (a.minor is None and pending) or a.minor in pending:
            continue
        if a.minor is not None and measured is not None and a.minor not in measured:
            continue
        hits = [m for s, m in grown if s == a.surface and (a.minor is None or m == a.minor)]
        if hits:
            continue
        where = "on any measured minor" if a.minor is None else f"on {a.minor}"
        out.append(f"{a.label()}: approved but did not grow {where} (unused)")
    return out


def per_surface_table(base: Path, tip: Path, minor: str, label: str = "") -> "list[str]":
    """One minor's per-surface table over the surfaces the base could run."""
    before, after = rows(base, minor), rows(tip, minor)
    skip = set(_unmeasurable(before, expected_exits(), base.name))
    lines = [f"CPython {minor}{label}", "| surface | base | tip | delta |", "|---|---|---|---|"]
    for key in sorted(k for k in before if k not in skip):
        b, a = before[key].file_ops, after[key].file_ops
        lines.append(f"| {key} | {b} | {a} | {a - b:+d} |")
    return lines


TOTALS_HEADER = [
    "| CPython | surfaces | base total | tip total | surfaces changed |",
    "|---|---|---|---|---|",
]


def print_totals(rows_: "list[str]") -> None:
    """Print the combined totals table over *rows_* (one per minor with a verdict)."""
    if rows_:
        print("\n".join([*TOTALS_HEADER, *rows_]))


def totals_line(base: Path, tip: Path, minor: str) -> str:
    """One minor's totals row (over the surfaces the base could run)."""
    before, after = rows(base, minor), rows(tip, minor)
    skip = set(_unmeasurable(before, expected_exits(), base.name))
    shared = sorted(k for k in before if k not in skip)
    changed = sum(1 for k in shared if before[k].file_ops != after[k].file_ops)
    return (
        f"| {minor} | {len(shared)} | {sum(before[k].file_ops for k in shared)} | "
        f"{sum(after[k].file_ops for k in shared)} | {changed} |"
    )


def print_tables(pairs: "list[tuple[Path, Path]]", verdict: Verdict) -> None:
    """Print the deciding pair's table, or every pair's labelled when inconclusive."""
    if verdict.evidence is not None:
        print("\n".join(per_surface_table(*pairs[verdict.evidence], verdict.minor)) + "\n")
        for surface, lines in verdict.attribution.items():
            if lines:
                print(f"{surface} paths:\n  " + "\n  ".join(lines) + "\n")
            else:
                print(f"{surface}: the same paths, more operations\n")
        return
    for i, (base, tip) in enumerate(pairs, start=1):
        print("\n".join(per_surface_table(base, tip, verdict.minor, f" (pair {i})")) + "\n")


def other_environment(other: Path, minor: str) -> "dict[str, Record]":
    """Return the other environment's records for *minor* under *other* ({} when it has none).

    The files are placed by hand (CI's `budget-<minor>` artifact), so they are
    checked like any record before they are shown.
    """
    if not (other / f"{minor}.json").is_file():
        return {}
    problems = _table_problems(other, minor)
    if problems:
        raise MeasureError("INVALID:\n  " + "\n  ".join(problems))
    return rows(other, minor)


def print_verdict(v: Verdict, base_name: str, other: "Path | None" = None) -> None:
    """Print the verdict's sections, in the order a reader wants them; STOP last.

    *other*, when given, is the directory of the other environment's records:
    each growth stop is followed by that environment's numbers for its surface.
    """
    if v.unmeasurable:
        why = (f"{k}: {reason}" for k, reason in v.unmeasurable.items())
        print(f"not measurable at {base_name}:\n  " + "\n  ".join(why))
    if v.declined:
        print("DECLINED:\n  " + "\n  ".join(v.declined))
    if v.named:
        print("APPROVED GROWTH:\n  " + "\n  ".join(v.named))
    if v.shuffles:
        print("SHUFFLED (unchanged total):\n  " + "\n  ".join(v.shuffles))
    if v.confirmed is not None and v.evidence is not None:
        print(f"confirmed by pair {v.confirmed} (tables: pair {v.evidence + 1})")
    if v.stop:
        theirs = other_environment(other, v.minor) if other is not None else {}
        lines = []
        for i, line in enumerate(v.stop):
            record = theirs.get(v.growth_stops.get(i, ""))
            extra = (
                f"\n    other environment: {record.file_ops} {record.by_bucket}" if record else ""
            )
            lines.append(line + extra)
        print("STOP:\n  " + "\n  ".join(lines))


def print_notes(lines: "list[str]") -> None:
    """Print the advisory lines (spent and unused approvals), which never fail a run."""
    if lines:
        print("NOTE:\n  " + "\n  ".join(lines))


def approval_file(base_tag: str, verdicts: "list[Verdict]", in_force: "list[Approval]") -> dict:
    """Return the complete approvals file the stops ask for.

    Writing it replaces the file, so it carries every in-force entry (one the
    run did not use may still hold for another environment or a minor that
    failed to measure; unused entries are NOTEd so the owner prunes them on
    purpose), each replaced by the warranted entry for its surface and minor
    when a stop names it (a second entry for one is refused by
    :func:`load_approvals`), then the new warranted entries. Spent entries
    are not carried: they hold for no base.
    """
    warranted = [e for v in verdicts for e in v.warranted]
    named = {(e["surface"], e["minor"]) for e in warranted}
    kept = [a.entry() for a in in_force if (a.surface, a.minor) not in named]
    return {"base": base_tag, "entries": kept + warranted}


def print_warranted(base_tag: str, verdicts: "list[Verdict]", in_force: "list[Approval]") -> None:
    """Print the complete approvals file when any stop warrants an entry, else nothing."""
    if any(v.warranted for v in verdicts):
        print(f"to approve, write {APPROVALS_PATH}:")
        print(json.dumps(approval_file(base_tag, verdicts, in_force), indent=2))


def conclude(
    base_tag: str, approvals: Approvals, results: "list[tuple[str, Verdict | None]]"
) -> int:
    """Print what follows the minors (notes, the file to write) and return the run's status.

    *results* holds each minor with its verdict, or None when measuring it
    failed. The status is the worst over the minors (2 failed, 1 stopped,
    0 accepted): the one place `measure` and `judge` decide it.
    """
    verdicts = [v for _minor, v in results if v is not None]
    unresolved = [m for m, v in results if v is None or v.evidence is None]
    grown = [g for v in verdicts for g in v.grown]
    measured = [m for m, _v in results]
    stale = stale_approvals(approvals.in_force, grown, unresolved, measured)
    print_notes(approvals.spent + stale)
    print_warranted(base_tag, verdicts, approvals.in_force)
    return max((2 if v is None else int(bool(v.stop)) for _minor, v in results), default=0)


def _tip_problems(side: Path, minor: str) -> "list[str]":
    """Return the tip's extra problems: a surface that did not end as it expects.

    The harness's ending verdict names why; a wrong exit alone is named as such.
    """
    expected = expected_exits()
    problems = []
    for k, r in sorted(rows(side, minor).items()):
        if r.ending:
            problems.append(f"{side}: {minor} {k} did not end as expected: {'; '.join(r.ending)}")
        elif expected[k] is not None and r.exit != expected[k]:
            problems.append(f"{side}: {minor} {k} exited {r.exit}, not {expected[k]}")
    return problems


def validate_pair(base: Path, tip: Path, minor: str) -> "list[str]":
    """Return both sides' problems on *minor*; the tip's exit rule runs once the tip is readable."""
    problems = validate_side(base, minor, None)
    tip_problems = validate_side(tip, minor, None)
    if not tip_problems:
        tip_problems = _tip_problems(tip, minor)
    return problems + tip_problems


def _judge_verb(
    pairs: "list[tuple[Path, Path]]", minors: "list[str]", approvals_path: Path, base_tag: str
) -> int:
    """`judge`: approvals first (no record read on a bad file), then tables and verdicts."""
    try:
        approvals = load_approvals(approvals_path, base_tag)
    except ValueError as e:
        print(f"INVALID:\n  {e}")
        return 2
    if len(pairs) > MAX_PAIRS:
        print(f"INVALID:\n  at most {MAX_PAIRS} pairs: {len(pairs)} given")
        return 2
    problems = [
        p for base, tip in pairs for minor in minors for p in validate_pair(base, tip, minor)
    ]
    if problems:
        print("INVALID:\n  " + "\n  ".join(problems))
        return 2
    results: "list[tuple[str, Verdict | None]]" = []
    totals = []
    for minor in minors:
        v = decide(pairs, minor, approvals.in_force)
        print_tables(pairs, v)
        print_verdict(v, pairs[0][0].name)
        results.append((minor, v))
        if v.evidence is not None:
            totals.append(totals_line(*pairs[v.evidence], minor))
    print_totals(totals)
    return conclude(base_tag, approvals, results)


VERSION_SNIPPET = "import platform; print(platform.python_version())"
"""Run by an interpreter: its full version, the provenance a cached record must match."""


class MeasureError(Exception):
    """A step outside the runner failed (git, a missing executable, the filesystem): status 2."""


@dataclass(frozen=True)
class Settings:
    """What one `measure` run compares and where it keeps its records."""

    repo: Path
    out: Path
    base: str
    """The base's release tag, resolved once by `main`: it names the base's
    records directory and the approvals' base, and is the base revision."""
    tip: str
    minors: "list[str]"
    approvals: Path
    holder_root: "Path | None" = None
    """Where throwaway worktrees and venvs are made (None: the system temp dir)."""


@dataclass(frozen=True)
class SideJob:
    """One tree measured under one minor into one record directory."""

    repo: Path
    name: str
    """`base` or `tip`: names the throwaway worktree, nothing else."""
    sha: str
    """The tree whose otto is measured (its venv is synced from it)."""
    harness_sha: str
    """The tree whose scripts/import_budget.py does the measuring (the tip's)."""
    minor: str
    dest: Path
    log: Path
    scratch: Path
    """Where the interpreter probe's output goes: never beside the records."""
    holder_root: "Path | None" = None
    """The parent of `otto-budget-delta/<name>-<minor>` (None: the system temp dir)."""


def _git(repo: Path, *args: str, env: "dict[str, str] | None" = None) -> str:
    """Run git in *repo* (with *env* added to the environment); a failure is a MeasureError."""
    try:
        result = subprocess.run(  # noqa: S603 -- fixed argv, no shell
            ["git", "-C", str(repo), *args],  # noqa: S607 -- `git` via PATH
            check=False, capture_output=True, text=True,
            env=None if env is None else {**os.environ, **env},
        )  # fmt: skip
    except (OSError, UnicodeDecodeError) as e:
        raise MeasureError(f"git {' '.join(args)} could not run: {e}") from e
    if result.returncode != 0:
        raise MeasureError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def base_ref(repo: Path, rev: str = "HEAD") -> str:
    """Return the previous release: the nearest tag reachable from *rev*."""
    return _git(repo, "describe", "--tags", "--abbrev=0", rev)


def measured_tree(repo: Path, sha: str) -> str:
    """Return the tree id of what *sha* measures: its committed tree minus the approvals file.

    The approvals file does not change a single file operation, so a commit
    that edits only it measures the same tree as its parent and is judged
    from the records already on disk. Built in a scratch index, never the
    repository's own. The one place a tree is identified for caching and for
    the equal-trees comparison.
    """
    with tempfile.TemporaryDirectory() as tmp:
        env = {"GIT_INDEX_FILE": str(Path(tmp) / "index")}
        _git(repo, "read-tree", sha, env=env)
        _git(repo, "update-index", "--force-remove", APPROVALS_PATH.as_posix(), env=env)
        return _git(repo, "write-tree", env=env)


def instrument_id(repo: Path, sha: str) -> str:
    """Return the instrument at *sha*: the harness blob and fixtures tree, `<blob>:<tree>`."""
    return ":".join(_git(repo, "rev-parse", f"{sha}:{path}") for path in INSTRUMENT_PATHS)


def _run(argv: "list[str]", cwd: Path, env: "dict[str, str]", out: "Path | None") -> int:
    """Run *argv* as a subprocess, stdout to *out* if given; a missing executable raises."""
    try:
        if out is None:
            return subprocess.run(argv, cwd=cwd, env=env, check=False).returncode  # noqa: S603
        with out.open("w") as fh:
            return subprocess.run(argv, cwd=cwd, env=env, stdout=fh, check=False).returncode  # noqa: S603
    except OSError as e:
        raise MeasureError(f"{argv[0]}: no such executable or cannot run: {e}") from e


def _logged(runner: Runner, log: Path, elapsed: "dict[str, float]", step: str) -> Runner:
    """Wrap *runner* to log the command and record its elapsed seconds under *step*."""

    def run(argv: "list[str]", cwd: Path, env: "dict[str, str]", out: "Path | None") -> int:
        started = time.monotonic()
        rc = runner(argv, cwd, env, out)
        elapsed[step] = round(time.monotonic() - started, 1)
        with log.open("a") as fh:
            fh.write(f"$ {' '.join(argv)}  (cwd={cwd}) -> {rc} ({elapsed[step]} s)\n")
        return rc

    return run


def _venv_clean() -> "dict[str, str]":
    """Return the caller's environment without the venv or path it runs under (`uv run`'s)."""
    return {
        k: v for k, v in os.environ.items() if k not in ("VIRTUAL_ENV", "PYTHONHOME", "PYTHONPATH")
    }


def _envs(venv: Path) -> "tuple[dict[str, str], dict[str, str]]":
    """Return (the env uv syncs in, the env the venv's interpreter runs in), both venv-clean."""
    outer = _venv_clean()
    outer["UV_PROJECT_ENVIRONMENT"] = str(venv)
    inner = dict(
        outer, VIRTUAL_ENV=str(venv), PATH=f"{venv / 'bin'}{os.pathsep}{outer.get('PATH', '')}"
    )
    return outer, inner


def _read_text(path: Path) -> "str | None":
    """Return the stripped text of *path*, or None when it is not a file."""
    return path.read_text().strip() if path.is_file() else None


def interpreter_version(minor: str, runner: Runner, scratch: Path) -> "str | None":
    """Return the full version of the interpreter uv syncs *minor* from today; None if none.

    Asked as the sync asks: venv-clean, and `--system`, because `uv python
    find` would otherwise answer a `.venv` in the cwd or any parent (the
    checkout's own), which a venv the sync creates is never built from.
    """
    scratch.mkdir(parents=True, exist_ok=True)
    env = _venv_clean()
    found = scratch / f"find-{minor}.txt"
    if runner(["uv", "python", "find", "--system", minor], scratch, env, found) != 0:
        return None
    python = _read_text(found)
    if not python:
        return None
    version = scratch / f"version-{minor}.txt"
    if runner([python, "-c", VERSION_SNIPPET], scratch, env, version) != 0:
        return None
    return _read_text(version)


def _worktree(repo: Path, holder: Path, name: str, sha: str) -> Path:
    tree = holder / f"tree-{name}"
    _git(repo, "worktree", "add", "--detach", "--quiet", str(tree), sha)
    return tree


def _remove_one(repo: Path, tree: Path) -> "str | None":
    """Unregister one worktree; return what git refused, or None."""
    try:
        _git(repo, "worktree", "remove", "--force", str(tree))
    except MeasureError as e:
        return str(e)
    return None


def _remove_worktrees(repo: Path, holder: Path) -> "list[str]":
    """Unregister every worktree under *holder* and delete it; return every failure, raise none."""
    failures = [
        f for tree in sorted(holder.glob("tree-*")) if (f := _remove_one(repo, tree)) is not None
    ]
    try:
        shutil.rmtree(holder)
    except OSError as e:
        failures.append(f"holder {holder} not deleted: {e}")
    if failures:
        try:
            _git(repo, "worktree", "prune")
        except MeasureError as e:
            failures.append(str(e))
    return failures


def _fresh_holder(job: SideJob) -> Path:
    """Return *job*'s holder, empty: `otto-budget-delta/<name>-<minor>` under the holder root.

    The path is the same on every run, so the harness's bytecode cache, which
    mirrors absolute paths, is overwritten in place rather than grown. A holder
    an interrupted run left behind is unregistered and deleted first; one that
    cannot be deleted is a MeasureError. Registrations whose directory is
    already gone (a holder swept away outside git) are pruned first, or the
    next ``git worktree add`` at the same path would refuse.
    """
    _git(job.repo, "worktree", "prune")
    root = job.holder_root or Path(tempfile.gettempdir())
    holder = root / "otto-budget-delta" / f"{job.name}-{job.minor}"
    if holder.exists():
        problems = _remove_worktrees(job.repo, holder)
        if holder.exists():
            raise MeasureError(
                f"a previous holder {holder} could not be removed:\n  " + "\n  ".join(problems)
            )
    holder.mkdir(parents=True)
    return holder


def _steps(
    job: SideJob, runner: Runner, tree: Path, harness: Path, venv: Path
) -> "dict[str, object]":
    """Run the three steps, stopping at the first failure; return their statuses and timings."""
    outer, inner = _envs(venv)
    py = str(venv / "bin" / "python")
    elapsed: "dict[str, float]" = {}
    status: "dict[str, object]" = {"elapsed": elapsed}
    status["sync"] = _logged(runner, job.log, elapsed, "sync")(
        ["uv", "sync", "--frozen", "--python", job.minor], tree, outer, None
    )
    if status["sync"] != 0:
        return status
    job.scratch.mkdir(parents=True, exist_ok=True)
    version = job.scratch / f"{job.minor}.python.txt"
    status["version"] = _logged(runner, job.log, elapsed, "version")(
        [py, "-c", VERSION_SNIPPET], tree, inner, version
    )
    status["python"] = _read_text(version)
    if status["version"] != 0:
        return status
    report = job.dest / f"{job.minor}.json"
    status["report"] = _logged(runner, job.log, elapsed, "report")(
        [py, "scripts/import_budget.py", "--report-json", str(report)],
        harness,
        # The harness runs under this side's otto, which may predate the
        # command-list helper; this process runs under the tip's, so it reads
        # the list once and hands it over.
        {**inner, _harness().VERBS_ENV: ",".join(_harness().first_party_commands())},
        None,
    )
    return status


def measure_side(job: SideJob, runner: Runner) -> None:
    """Measure one tree under one minor: a throwaway worktree and venv, the tip's harness.

    The venv is synced FROM the measured tree (so it holds that tree's otto)
    and the harness runs FROM the tip's worktree under that venv's
    interpreter: one instrument, two subjects. Both live in the side and
    minor's stable holder (see :func:`_fresh_holder`). Every step's exit status is
    recorded; the worktrees and the holder are removed whatever happened, the
    status is written last, and a cleanup git or the filesystem refused is
    raised AFTER the status is on disk (the measurement is evidence; the leak
    is reported). When the measurement itself failed, that failure is the
    error raised, with any cleanup failure appended to it, never replaced;
    an interrupt propagates after the same cleanup.
    """
    job.dest.mkdir(parents=True, exist_ok=True)
    status: "dict[str, object]" = {
        "sha": job.sha,
        "measured_tree": measured_tree(job.repo, job.sha),
        "minor": job.minor,
        "instrument": instrument_id(job.repo, job.harness_sha),
    }
    holder = _fresh_holder(job)
    failure: "Exception | None" = None
    problems: "list[str]" = []
    try:
        try:
            tree = _worktree(job.repo, holder, job.name, job.sha)
            harness = (
                tree
                if job.sha == job.harness_sha
                else _worktree(job.repo, holder, "tip", job.harness_sha)
            )
            status.update(_steps(job, runner, tree, harness, holder / f"venv-{job.minor}"))
        except (MeasureError, OSError) as e:
            failure = e
    finally:
        # Runs on every exit, an interrupt included: nothing of ours outlives the call.
        problems = _remove_worktrees(job.repo, holder)
        try:
            (job.dest / f"{job.minor}.status.json").write_text(
                json.dumps(status, indent=2, sort_keys=True) + "\n"
            )
        except OSError as e:
            problems.append(f"status not written: {e}")
    if failure is not None:
        text = str(failure) + (
            "\n  cleanup also failed:\n  " + "\n  ".join(problems) if problems else ""
        )
        raise MeasureError(text) from failure
    if problems:
        raise MeasureError(
            "the measurement ran; recording or cleanup failed:\n  " + "\n  ".join(problems)
        )


@dataclass(frozen=True)
class _Run:
    """One `measure` run's resolved identities, beside its Settings."""

    settings: Settings
    base_sha: str
    tip_sha: str
    base_tree: str
    """`measured_tree` of the base."""
    tip_tree: str
    """`measured_tree` of the tip: its records' directory name."""
    instrument: str
    runner: Runner

    def job(self, name: str, minor: str, dest: Path) -> SideJob:
        """Return the SideJob for *name* (`base` or `tip`) on *minor* into *dest*."""
        sha = self.base_sha if name == "base" else self.tip_sha
        s = self.settings
        return SideJob(
            s.repo, name, sha, self.tip_sha, minor, dest, s.out / "run.log", s.out / "scratch",
            s.holder_root,
        )  # fmt: skip


def _cached_or_measured(run: _Run, name: str, minor: str, dest: Path, python: "str | None") -> None:
    """Make *dest* hold *name*'s records for *minor*: reuse them if evidence, else measure them.

    *name* is `base` or `tip`; the cache key is the side's measured tree, the
    interpreter's full version and the instrument. Without an interpreter
    version (uv found none) nothing can be reused.
    """
    s = run.settings
    label = s.base if name == "base" else s.tip
    tree = run.base_tree if name == "base" else run.tip_tree
    expect = None if python is None else Provenance(tree, python, run.instrument)
    if expect is not None and not validate_side(dest, minor, expect):
        print(f"reusing {label} on {minor} from {dest}")
    else:
        print(f"measuring {label} on {minor}")
        measure_side(run.job(name, minor, dest), run.runner)


def _probe(run: _Run, minor: str) -> "str | None":
    """Return the interpreter's full version for *minor*, saying so when uv finds none."""
    python = interpreter_version(minor, run.runner, run.settings.out / "scratch")
    if python is None:
        print(f"interpreter for {minor} not found by uv; the cache cannot be reused")
    return python


def _pair(run: _Run, minor: str, where: Path) -> "tuple[Path, Path]":
    """Measure (base, tip) on *minor* into *where*, reusing every side that is evidence."""
    s = run.settings
    base_dir, tip_dir = where / s.base, where / run.tip_tree
    python = _probe(run, minor)
    _cached_or_measured(run, "base", minor, base_dir, python)
    _cached_or_measured(run, "tip", minor, tip_dir, python)
    return base_dir, tip_dir


def _same_tree_minor(
    run: _Run, minor: str, approvals: "list[Approval]"
) -> "tuple[Verdict, list[tuple[Path, Path]]]":
    """Judge *minor* when the tip measures the base's tree: the base's records are both sides."""
    base_dir = run.settings.out / run.settings.base
    _cached_or_measured(run, "base", minor, base_dir, _probe(run, minor))
    problems = validate_side(base_dir, minor, None)
    if problems:
        raise MeasureError("INVALID:\n  " + "\n  ".join(problems))
    pairs = [(base_dir, base_dir)]
    return decide(pairs, minor, approvals), pairs


def _is_old_tip(path: Path, tip_tree: str, base: str) -> bool:
    """Whether *path* is a tip's record directory (a 40-hex tree id) other than *tip_tree*'s."""
    name = path.name
    return (
        path.is_dir()
        and name not in (tip_tree, base)
        and len(name) == len(tip_tree)
        and all(c in "0123456789abcdef" for c in name)
    )


def _measure_minor(
    run: _Run, minor: str, approvals: "list[Approval]"
) -> "tuple[Verdict, list[tuple[Path, Path]]]":
    """Measure pairs of *minor* until `decide` has a verdict; return it with the pairs."""
    s = run.settings
    pairs = [_pair(run, minor, s.out)]
    while True:
        problems = validate_pair(*pairs[-1], minor)
        if problems:
            raise MeasureError("INVALID:\n  " + "\n  ".join(problems))
        verdict = decide(pairs, minor, approvals)
        if not verdict.more:
            return verdict, pairs
        print(
            f"confirming {minor}: {verdict.reason} appeared; "
            f"measuring the pair again ({len(pairs) + 1} of {MAX_PAIRS})"
        )
        pairs.append(_pair(run, minor, s.out / run.tip_tree / f"again-{len(pairs)}"))


def measure(s: Settings, runner: Runner) -> int:
    """Measure base vs tip on every minor in order, each judged as soon as its pair is measured.

    A MeasureError or OSError (a failed step, a record that cannot be read or
    written) on one minor prints its text as that minor's result and the
    others are still judged; one outside any minor (git, the output directory)
    ends the run. Either is status 2; nothing is a traceback.
    """
    try:
        try:
            approvals = load_approvals(s.approvals, s.base)
        except ValueError as e:
            print(f"INVALID:\n  {e}")
            return 2
        base_sha, tip_sha = _git(s.repo, "rev-parse", s.base), _git(s.repo, "rev-parse", s.tip)
        base_tree, tip_tree = measured_tree(s.repo, base_sha), measured_tree(s.repo, tip_sha)
        run = _Run(
            s, base_sha, tip_sha, base_tree, tip_tree, instrument_id(s.repo, tip_sha), runner
        )
        s.out.mkdir(parents=True, exist_ok=True)
        # A tip's confirming pairs live beneath its records, so they go with it.
        for stale in [d for d in s.out.glob("*") if _is_old_tip(d, tip_tree, s.base)]:
            shutil.rmtree(stale, ignore_errors=True)
        print(
            f"budget-delta: {s.base} ({base_sha[:12]}) -> {s.tip} ({tip_sha[:12]}); "
            f"minors {' '.join(s.minors)}"
        )
        same = base_tree == tip_tree
        if same:
            print(
                f"budget-delta: {s.base} and {s.tip} measure the same tree "
                "(only the approvals file differs); nothing grew"
            )
        judge = _same_tree_minor if same else _measure_minor
        results: "list[tuple[str, Verdict | None]]" = []
        totals: "list[str]" = []
        for minor in s.minors:
            try:
                verdict, pairs = judge(run, minor, approvals.in_force)
                print_tables(pairs, verdict)
                print_verdict(verdict, s.base, s.out / "other")
                if verdict.evidence is not None:
                    totals.append(totals_line(*pairs[verdict.evidence], minor))
            except (MeasureError, OSError) as e:
                print(str(e))
                results.append((minor, None))
                continue
            results.append((minor, verdict))
        print_totals(totals)
    except (MeasureError, OSError) as e:
        print(str(e))
        return 2
    return conclude(s.base, approvals, results)


def _add_minors(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--minors",
        nargs="+",
        default=SUPPORTED_MINORS,
        metavar="M",
        help="the Python minors, in order",
    )


def main(argv: "list[str] | None" = None) -> int:
    """Run one of the verbs; the module docstring gives the exit statuses."""
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = ap.add_subparsers(dest="verb", required=True)
    j = sub.add_parser("judge", help="judge one to three measured pairs; 0 pass, 1 stop, 2 invalid")
    j.add_argument(
        "--pair", nargs=2, action="append", required=True, metavar=("BASE", "TIP"), type=Path
    )
    _add_minors(j)
    j.add_argument(
        "--approvals", required=True, type=Path, metavar="PATH", help="the committed approvals file"
    )
    j.add_argument(
        "--base-tag", required=True, metavar="NAME", help="the release tag the pairs' base is"
    )
    v = sub.add_parser(
        "validate", help="is a record directory evidence for every minor; 0 yes, 1 no"
    )
    v.add_argument("side", type=Path)
    _add_minors(v)
    b = sub.add_parser("base-tag", help="print the nearest release tag reachable from a revision")
    b.add_argument("--repo", type=Path, default=Path.cwd(), help="the repository (default: cwd)")
    b.add_argument("--rev", required=True, metavar="SHA", help="the revision to resolve from")
    t = sub.add_parser("tables", help="print the per-surface tables and totals of one pair")
    t.add_argument("base", type=Path)
    t.add_argument("tip", type=Path)
    _add_minors(t)
    m = sub.add_parser(
        "measure", help="measure base and tip in pristine worktrees and judge each minor"
    )
    m.add_argument(
        "--repo",
        type=Path,
        default=Path.cwd(),
        help="the repository (default: the current directory)",
    )
    m.add_argument(
        "--out",
        type=Path,
        default=Path("reports/budget"),
        help="where records are kept (relative to --repo)",
    )
    which = m.add_mutually_exclusive_group()
    which.add_argument(
        "--base",
        default=None,
        metavar="TAG",
        help="the base release tag (default: git describe --tags --abbrev=0); a value that "
        "does not describe as itself is refused",
    )
    which.add_argument(
        "--base-rev",
        default=None,
        metavar="SHA",
        help="take the base to be the nearest tag reachable from SHA (CI's merge-base)",
    )
    m.add_argument(
        "--tip",
        default="HEAD",
        help="the tip ref; the tracked verbs and expected surfaces are read from the "
        "checked-out tree, so a tip other than HEAD must carry the same surfaces",
    )
    _add_minors(m)
    m.add_argument(
        "--approvals",
        type=Path,
        default=None,
        metavar="PATH",
        help=f"the committed approvals file (default: {APPROVALS_PATH} under --repo)",
    )
    args = ap.parse_args(argv)
    if args.verb == "measure":
        repo = args.repo.resolve()
        out = args.out if args.out.is_absolute() else repo / args.out
        try:
            base = base_ref(repo, args.base or args.base_rev or "HEAD")
        except MeasureError as e:
            print(str(e))
            return 2
        if args.base is not None and base != args.base:
            print(f"--base {args.base} is not a release tag (it describes as {base})")
            return 2
        approvals = args.approvals or APPROVALS_PATH
        approvals = approvals if approvals.is_absolute() else repo / approvals
        return measure(Settings(repo, out, base, args.tip, args.minors, approvals), _run)
    if args.verb == "base-tag":
        try:
            print(base_ref(args.repo.resolve(), args.rev))
        except MeasureError as e:
            print(str(e))
            return 2
        return 0
    if args.verb == "judge":
        return _judge_verb(
            [(b, t) for b, t in args.pair], args.minors, args.approvals, args.base_tag
        )
    if args.verb == "validate":
        problems = [p for m in args.minors for p in validate_side(args.side, m, None)]
        print("\n".join(problems) or "valid")
        return 1 if problems else 0
    problems = [p for m in args.minors for p in validate_pair(args.base, args.tip, m)]
    if problems:
        print("INVALID:\n  " + "\n  ".join(problems))
        return 2
    for minor in args.minors:
        print("\n".join(per_surface_table(args.base, args.tip, minor)) + "\n")
    print_totals([totals_line(args.base, args.tip, m) for m in args.minors])
    return 0


if __name__ == "__main__":
    sys.exit(main())
