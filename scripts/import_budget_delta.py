"""The import-budget delta: the previous release's file operations against HEAD's.

`make profile` gates every surface against a ceiling, a recorded baseline plus
10 % headroom. Growth that stays under the ceiling passes it silently, release
after release. This script is the release's answer: it measures the base (the
last release tag, `git describe --tags --abbrev=0`) and the tip (HEAD), one
throwaway venv per Python minor synced from each tree, with ONE instrument —
the tip's `scripts/import_budget.py`, which measures whatever interpreter runs
it (the base's venv holds the base's otto) — and STOPS the release on any
surface that costs more file operations at the tip than at the base, on any
minor, unless the owner named the surface AND the buckets it grew by:
`make release BUDGET_APPROVE="help@3.14=stdlib+4,otto:*+1 run=otto:*+1"`.
A surface named without its buckets is approved but not attributed, and the
stop prints the entry the measurement warrants; an entry whose buckets differ
from the measurement is a tip the owner has not seen; an entry nothing grew
for, over the whole span, is stale.

File operations only, never wall clock. Serial, one interpreter at a time, the
primary first, so a release that must stop stops at the first failing minor,
once its growth or shuffle is confirmed.

Growth or a bucket shuffle (buckets moved, total unchanged) is never a verdict
on one measurement: the pair is measured again, and a verdict stands only when
two consecutive pairs agree record for record, both sides — pairs 1 and 2
confirm pair 1's, else a third pair is measured once and pairs 2 and 3 confirm
pair 2's. Three pairs that never agree are inconclusive: a stop that names the
surfaces whose records differed, not a regression. The tables printed are the
deciding pair's; an inconclusive minor prints every pair's, labelled.

A record directory (a SIDE) holds, per minor M: `M.json` (the rows the
harness wrote: per surface its file operations, workspace count, exit, the
harness's own `ending` verdict and buckets) and `M.status.json` (every step's
exit status and elapsed seconds, the sha and tree measured, the interpreter's
full version, the instrument that measured). A base surface whose `ending` is
not empty did not end as the surface expects at the tag (its reason is
printed) and is listed, not judged; at the tip it is invalid evidence. Under
`--out` (reports/budget/, gitignored, per checkout):

    <base-tag>/        the base's records, reused by every later run whose
                       provenance matches (sha, tree, interpreter, instrument)
    head/              the tip's, measured afresh on every run
    again-1/, again-2/ the confirming pairs (<base-tag>/ and head/ inside)
    scratch/           the interpreter probes' output; not records
    run.log            every command, its exit status and elapsed seconds

Each side and minor is measured in `$TMPDIR/otto-budget-delta/<side>-<minor>/`
(its worktrees and venv), removed before and after every measurement; the path
is stable so the harness's persistent bytecode cache
(`~/.cache/otto/import-budget-pycache`, which mirrors absolute paths) keeps one
mirror per side and minor instead of a new one per run. It is one per box,
shared by every checkout of the repository: never run two at once.

Exit statuses. `measure` and `judge`: 0 accepted (nothing grew, or every
growth is approved and attributed; declines, unreproduced shuffles and
surfaces the base cannot run are reported); 1 a policy STOP (a line per
reason; a growth stop carries the entry to paste whenever the moved buckets
can be written as a word); 2 invalid evidence or an operational failure (a
step, git or the filesystem failed, a record is not evidence, an approval
word is malformed, base and tip are the same tree, more than three pairs) —
possibly after earlier minors were judged and printed. `validate`: 0
evidence, 1 not. `tables`: 0, or 2 on a side that is not evidence.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

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

MINORS_DEFAULT = ["3.10", "3.11", "3.12", "3.13", "3.14"]
"""The minors the pure verbs take when none are given (the Makefile passes the nox list)."""

MAX_PAIRS = 3
"""Measurements of one minor's pair before the minor is inconclusive."""

CONFIRMING_PAIR = 2
"""The pair that confirms the first pair's verdict when the two agree."""

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


@dataclass(frozen=True)
class Provenance:
    """What a cached record must have been measured on, and with, to be reused."""

    sha: str
    tree: str
    python: str
    """The interpreter's full version (`platform.python_version()`), not just the minor."""
    instrument: str
    """The harness blob and fixtures tree at the tip, `<blob>:<tree>`."""


@dataclass(frozen=True)
class Approval:
    """One BUDGET_APPROVE word: a surface, maybe a minor, maybe the buckets it moved by."""

    surface: str
    minor: "str | None"
    buckets: "dict[str, int] | None"

    def word(self) -> str:
        """Return the word that parses back to this approval (buckets sorted by name)."""
        head = self.surface if self.minor is None else f"{self.surface}@{self.minor}"
        if self.buckets is None:
            return head
        return head + "=" + ",".join(f"{k}{v:+d}" for k, v in sorted(self.buckets.items()))


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
    return None


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
            ("sha", expect.sha),
            ("tree", expect.tree),
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


_BUCKET = re.compile(r"^(?P<name>.+?)(?P<delta>[+-]\d+)$")
UNWRITABLE = (",", "=")
"""Characters a bucket name cannot carry inside an approval word (the word's own separators)."""


def _parse_buckets(word: str, spec: str) -> "dict[str, int]":
    if not spec:
        raise ValueError(f"--approve {word}: names no bucket")
    out: "dict[str, int]" = {}
    for item in spec.split(","):
        m = _BUCKET.match(item)
        if m is None:
            raise ValueError(f"--approve {word}: bucket '{item}' has no +N/-N")
        delta = int(m.group("delta"))
        if delta == 0:
            raise ValueError(f"--approve {word}: bucket '{item}' moves nothing")
        if m.group("name") in out:
            raise ValueError(f"--approve {word}: bucket '{m.group('name')}' named twice")
        out[m.group("name")] = delta
    return out


def parse_approvals(words: "list[str]", minors: "list[str] | None" = None) -> "list[Approval]":
    """Read every BUDGET_APPROVE word; raise ValueError naming the first malformed one."""
    minors = MINORS_DEFAULT if minors is None else minors
    out: "list[Approval]" = []
    for word in words:
        head, _, spec = word.partition("=")
        surface, _, minor = head.partition("@")
        if surface not in KNOWN_SURFACES:
            raise ValueError(f"--approve {word}: not a known surface")
        if minor and minor not in minors:
            raise ValueError(f"--approve {word}: not a measured minor")
        if any(a.surface == surface and a.minor == (minor or None) for a in out):
            raise ValueError(f"--approve {word}: a second entry for {head}")
        buckets = _parse_buckets(word, spec) if "=" in word else None
        out.append(Approval(surface, minor or None, buckets))
    return out


@dataclass
class Verdict:
    """What the judge found for one minor."""

    minor: str
    stop: "list[str]" = field(default_factory=list)
    named: "list[str]" = field(default_factory=list)
    declined: "list[str]" = field(default_factory=list)
    shuffles: "list[str]" = field(default_factory=list)
    unmeasurable: "dict[str, str]" = field(default_factory=dict)
    """Each base surface not judged, by name, with why (sorted by surface)."""
    grown: "list[tuple[str, str]]" = field(default_factory=list)
    """Each (surface, minor) that grew in the deciding pair, approved or not; staleness reads it."""
    used: "list[Approval]" = field(default_factory=list)
    reason: "str | None" = None
    """Why a second pair is needed: "growth", "a shuffle", or None for a quiet pair."""
    more: bool = False
    """True when one more pair would decide (`measure` measures it; `judge` says so)."""
    confirmed: "int | None" = None
    """The pair that confirmed the verdict (2 or 3); None when one measurement was enough."""
    evidence: "int | None" = 0
    """Index of the pair the verdict (and its tables) describes; None when inconclusive."""


def expected_exits() -> "dict[str, int | None]":
    """Surface -> the exit a real-entry surface must end with (None for a harness child)."""
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    from scripts import import_budget

    return {s.key: (s.expect_exit if s.real_entry else None) for s in import_budget.SURFACES}


def _moved(before: "dict[str, int]", after: "dict[str, int]") -> "dict[str, int]":
    keys = sorted(set(before) | set(after))
    return {
        k: after.get(k, 0) - before.get(k, 0) for k in keys if after.get(k, 0) != before.get(k, 0)
    }


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


def _growth_line(
    v: Verdict, line: str, key: str, moved: "dict[str, int]", approvals: "list[Approval]"
) -> None:
    """Classify one surface's growth into a stop or a named approval."""
    unwritable = [k for k in moved if any(c in k for c in UNWRITABLE)]
    if unwritable:
        v.stop.append(
            f"{line}: bucket '{unwritable[0]}' cannot be written as an approval word "
            "(it holds ',' or '='); rename the path"
        )
        return
    warranted = Approval(key, v.minor, moved).word()
    entry = _applicable(approvals, key, v.minor)
    if entry is None:
        v.stop.append(
            f"{line}: grew and is not approved; "
            f'to approve and attribute: BUDGET_APPROVE="{warranted}"'
        )
    elif entry.buckets is None:
        v.stop.append(
            f'{line}: approved but not attributed; to attribute: BUDGET_APPROVE="{warranted}"'
        )
    elif entry.buckets != moved:
        v.stop.append(
            f"{line}: approved as {entry.word()} but moved {moved} "
            "(the tip is not the tree that was approved); "
            f'to attribute: BUDGET_APPROVE="{warranted}"'
        )
    else:
        v.named.append(f"{line}: approved as {entry.word()}")
        v.used.append(entry)


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
            _growth_line(v, line, key, moved, approvals)
        elif delta < 0:
            v.declined.append(f"{minor} {key} {b.file_ops} -> {a.file_ops}")
        elif moved:
            v.shuffles.append(f"{minor} {key} {b.file_ops} moved {moved}")
    if v.grown:
        v.reason = "growth"
    elif v.shuffles:
        v.reason = "a shuffle"
    return v


def reproduced(first: Path, second: Path, minor: str) -> "list[str]":
    """Return the surfaces whose record differs between two measurements of one tree.

    Every field the judge reads is compared: the exit, the file operations,
    the buckets and the workspace count. A surface neither run could measure
    (a non-empty ending) agrees whatever else it holds, since it is listed and
    never judged and its ending text can quote a per-run temp path; one
    measurable in only one of the runs differs. The result is empty when
    nothing differs.
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
        elif a[key].by_bucket != b[key].by_bucket:
            out.append(f"{key}: {a[key].by_bucket} vs {b[key].by_bucket}")
        elif a[key].workspace != b[key].workspace:
            out.append(f"{key}: workspace {a[key].workspace} vs {b[key].workspace}")
    return out


def _disagreement(pairs: "list[tuple[Path, Path]]", i: int, minor: str) -> "list[str]":
    """How pair i and pair i+1 differ, both sides, as '<side> <surface>: …' lines."""
    (b1, t1), (b2, t2) = pairs[i], pairs[i + 1]
    return [f"base {d}" for d in reproduced(b1, b2, minor)] + [
        f"tip {d}" for d in reproduced(t1, t2, minor)
    ]


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
) -> "list[str]":
    """Return the entries whose surface grew on no minor in their scope, over the whole span.

    *unresolved* names the minors with no verdict yet (pending or inconclusive):
    an entry whose scope touches one is not judged, because growth there is
    not yet known either way.
    """
    pending = set(unresolved or [])
    out = []
    for a in approvals:
        if (a.minor is None and pending) or a.minor in pending:
            continue
        hits = [m for s, m in grown if s == a.surface and (a.minor is None or m == a.minor)]
        if hits:
            continue
        where = "on any measured minor" if a.minor is None else f"on {a.minor}"
        out.append(
            f"{a.word()}: approved but {a.surface} did not grow {where} (stale BUDGET_APPROVE)"
        )
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
        return
    for i, (base, tip) in enumerate(pairs, start=1):
        print("\n".join(per_surface_table(base, tip, verdict.minor, f" (pair {i})")) + "\n")


def print_verdict(v: Verdict, base_name: str) -> None:
    """Print the verdict's sections, in the order a reader wants them; STOP last."""
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
        print("STOP:\n  " + "\n  ".join(v.stop))


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


def _judge_verb(pairs: "list[tuple[Path, Path]]", minors: "list[str]", words: "list[str]") -> int:
    """`judge`: approvals first (no record read on a bad word), then tables and verdicts."""
    try:
        approvals = parse_approvals(words, minors)
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
    stopped = False
    grown: "list[tuple[str, str]]" = []
    unresolved: "list[str]" = []
    totals = []
    for minor in minors:
        v = decide(pairs, minor, approvals)
        print_tables(pairs, v)
        print_verdict(v, pairs[0][0].name)
        grown += v.grown
        stopped = stopped or bool(v.stop)
        if v.evidence is None:
            unresolved.append(minor)
        else:
            totals.append(totals_line(*pairs[v.evidence], minor))
    print("\n".join(TOTALS_HEADER + totals))
    stale = stale_approvals(approvals, grown, unresolved)
    if stale:
        print("STOP:\n  " + "\n  ".join(stale))
    return 1 if stopped or stale else 0


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
    tip: str
    minors: "list[str]"
    approve: "list[str]"
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


def _git(repo: Path, *args: str) -> str:
    """Run git in *repo*; a failure of any kind is a MeasureError naming the command."""
    try:
        result = subprocess.run(  # noqa: S603 -- fixed argv, no shell
            ["git", "-C", str(repo), *args],  # noqa: S607 -- `git` via PATH
            check=False, capture_output=True, text=True,
        )  # fmt: skip
    except (OSError, UnicodeDecodeError) as e:
        raise MeasureError(f"git {' '.join(args)} could not run: {e}") from e
    if result.returncode != 0:
        raise MeasureError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def base_ref(repo: Path) -> str:
    """Return the previous release: the nearest tag reachable from HEAD."""
    return _git(repo, "describe", "--tags", "--abbrev=0")


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
        [py, "scripts/import_budget.py", "--report-json", str(report)], harness, inner, None
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
        "tree": _git(job.repo, "rev-parse", f"{job.sha}^{{tree}}"),
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


def _pair(run: _Run, minor: str, where: Path) -> "tuple[Path, Path]":
    """Measure (base, tip) on *minor* into *where*, reusing a base cache that is evidence."""
    s = run.settings
    base_dir, tip_dir = where / s.base, where / "head"
    python = interpreter_version(minor, run.runner, s.out / "scratch")
    if python is None:
        print(f"interpreter for {minor} not found by uv; the base cache cannot be reused")
    tree = _git(s.repo, "rev-parse", f"{run.base_sha}^{{tree}}")
    expect = None if python is None else Provenance(run.base_sha, tree, python, run.instrument)
    if expect is not None and not validate_side(base_dir, minor, expect):
        print(f"reusing {s.base} on {minor} from {base_dir}")
    else:
        print(f"measuring {s.base} on {minor}")
        measure_side(run.job("base", minor, base_dir), run.runner)
    print(f"measuring {s.tip} on {minor}")
    measure_side(run.job("tip", minor, tip_dir), run.runner)
    return base_dir, tip_dir


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
        pairs.append(_pair(run, minor, s.out / f"again-{len(pairs)}"))


def measure(s: Settings, runner: Runner) -> int:
    """Measure base vs tip on every minor in order, each judged as soon as its pair is measured.

    Every MeasureError and every OSError (a directory that cannot be made, a
    record that cannot be read or written) is status 2 with its text, after
    whatever earlier minors already printed; nothing is a traceback.
    """
    try:
        approvals = parse_approvals(s.approve, s.minors)
    except ValueError as e:
        print(f"INVALID:\n  {e}")
        return 2
    try:
        base_sha, tip_sha = _git(s.repo, "rev-parse", s.base), _git(s.repo, "rev-parse", s.tip)
        if _git(s.repo, "rev-parse", f"{base_sha}^{{tree}}") == _git(
            s.repo, "rev-parse", f"{tip_sha}^{{tree}}"
        ):
            print(f"budget-delta: {s.base} and {s.tip} are the same tree; nothing to measure")
            return 2
        run = _Run(s, base_sha, tip_sha, instrument_id(s.repo, tip_sha), runner)
        for stale in [s.out / "head", *s.out.glob("again-*")]:
            shutil.rmtree(stale, ignore_errors=True)
        s.out.mkdir(parents=True, exist_ok=True)
        print(
            f"budget-delta: {s.base} ({base_sha[:12]}) -> {s.tip} ({tip_sha[:12]}); "
            f"minors {' '.join(s.minors)}"
        )
        grown: "list[tuple[str, str]]" = []
        for minor in s.minors:
            verdict, pairs = _measure_minor(run, minor, approvals)
            print_tables(pairs, verdict)
            print_verdict(verdict, s.base)
            if verdict.evidence is not None:
                print("\n".join([*TOTALS_HEADER, totals_line(*pairs[verdict.evidence], minor)]))
            grown += verdict.grown
            if verdict.stop:
                return 1
    except (MeasureError, OSError) as e:
        print(str(e))
        return 2
    stale = stale_approvals(approvals, grown)
    if stale:
        print("STOP:\n  " + "\n  ".join(stale))
        return 1
    return 0


def _add_minors(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--minors",
        nargs="+",
        default=MINORS_DEFAULT,
        metavar="M",
        help="the Python minors, in order",
    )


def _add_approve(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--approve", nargs="+", default=[], metavar="WORD",
        help="SURFACE[@MINOR][=BUCKET+N,…]: a surface the owner lets grow, by exactly "
        "those buckets",
    )  # fmt: skip


def main(argv: "list[str] | None" = None) -> int:
    """Run one of the four verbs; the module docstring gives the exit statuses."""
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = ap.add_subparsers(dest="verb", required=True)
    j = sub.add_parser("judge", help="judge one to three measured pairs; 0 pass, 1 stop, 2 invalid")
    j.add_argument(
        "--pair", nargs=2, action="append", required=True, metavar=("BASE", "TIP"), type=Path
    )
    _add_minors(j)
    _add_approve(j)
    v = sub.add_parser(
        "validate", help="is a record directory evidence for every minor; 0 yes, 1 no"
    )
    v.add_argument("side", type=Path)
    _add_minors(v)
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
    m.add_argument(
        "--base", default=None, help="the base ref (default: git describe --tags --abbrev=0)"
    )
    m.add_argument("--tip", default="HEAD", help="the tip ref")
    _add_minors(m)
    _add_approve(m)
    args = ap.parse_args(argv)
    if args.verb == "measure":
        repo = args.repo.resolve()
        out = args.out if args.out.is_absolute() else repo / args.out
        try:
            base = args.base or base_ref(repo)
        except MeasureError as e:
            print(str(e))
            return 2
        return measure(Settings(repo, out, base, args.tip, args.minors, args.approve), _run)
    if args.verb == "judge":
        return _judge_verb([(b, t) for b, t in args.pair], args.minors, args.approve)
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
    print("\n".join([*TOTALS_HEADER, *(totals_line(args.base, args.tip, m) for m in args.minors)]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
