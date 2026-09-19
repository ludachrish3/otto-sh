"""Fold a `make kgcov` run's observation records into ``schemas/kgcov_matrix.json``.

**THE ONLY WRITER OF A ``measured-*`` VERDICT** for the kgcov compatibility
matrix (design 2026-09-18 §4.5). ``tests/_fixtures/kgcov_matrix.py``'s
``rewrite_matrix_axes`` adds and removes CELLS and copies verdicts across; a
verdict enters the artifact here or nowhere.

Usage, from the repo root::

    uv run python -m scripts.collate_kgcov_matrix           # report only, writes nothing
    uv run python -m scripts.collate_kgcov_matrix --write   # fold the records in
    uv run python -m scripts.collate_kgcov_matrix --records DIR --matrix FILE

``-m`` and not a path, for the reason ``scripts/collate_support_matrix.py``
gives: the ``tests`` package this reads the axes and the record format from
is on ``sys.path`` because python put the repo root there.

THE FIVE RULES, each structural:

1. A record naming a nodeid no surface holds, or a profile no column holds,
   is discarded and counted in the report with its reason.
2. A cell the run drew becomes ``measured-ok`` on ``passed`` and
   ``measured-broken`` on ``failed`` or ``error``; a ``skipped`` record is
   discarded with its reason and moves nothing.
3. A bed cell becomes ``measured-ok`` only if the column's control record
   of the same ``run_id`` is ``passed``; otherwise the cell is written
   ``measured-broken`` with the reason in the report and in its
   ``failure_summary`` — a contract whose instrument did not demonstrate it
   could fail is not evidence. The build column has no control and its
   cells say so (``control: null``).
4. A cell the run did not draw is copied across unchanged; the collator
   never downgrades what it did not measure.
5. Nothing here runs a version-control command; the release stage's
   snapshot, gate and commit live in the Makefile.

A record whose provenance is null (its build fixture never produced a
toolchain, so the compiler could not even be named) is discarded with its
reason rather than written: the lane has already failed loudly on that
compiler, and a verdict without a version would be a verdict about nothing.
"""

import argparse
import copy
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

from tests._fixtures.kgcov_matrix import (
    BED,
    CONTROL_SURFACE,
    MATRIX_PATH,
    MEASURED_BROKEN,
    MEASURED_OK,
    surface_for,
    validation_errors,
)
from tests.e2e.cov._kgcov_observation import (
    DEFAULT_OBSERVATIONS_DIR,
    FORMAT,
    OBSERVATION,
    read_records,
)

PASSED = "passed"
EVIDENTIAL = frozenset({"passed", "failed", "error"})


@dataclass
class Discarded:
    """One reason records were dropped, with the records themselves."""

    reason: str
    records: "list[dict]" = field(default_factory=list)

    def line(self) -> str:
        """One report line: how many, why, and a sample so it can be chased."""
        sample = sorted({r.get("item") or "?" for r in self.records})[:3]
        return f"  {len(self.records):4d}  {self.reason}\n        e.g. {', '.join(sample)}"


@dataclass
class Collation:
    """Everything one collation decided, so the report and the write share a source."""

    matrix: dict
    changed: "dict[tuple[str, str], tuple[dict, dict]]" = field(default_factory=dict)
    unchanged_with_records: "list[tuple[str, str]]" = field(default_factory=list)
    refused_reasons: "list[str]" = field(default_factory=list)
    discarded: "list[Discarded]" = field(default_factory=list)
    kept: int = 0

    @property
    def ok(self) -> bool:
        """Whether every bed cell this run drew got a control-backed verdict."""
        return not self.refused_reasons


def _bucket(records: "list[dict]", matrix: dict) -> "tuple[list[dict], list[Discarded]]":
    columns = {p["id"]: p["venue"] for p in matrix["profiles"]}
    reasons = (
        f"not format {FORMAT} -- an unversioned or future record, unreadable here",
        "not an observation record",
        "names no surface -- the nodeid is not a contract the table holds",
        "names no column -- the profile is not one the Makefile default measures",
        (
            "names a column of another venue -- the profile exists, but not in the "
            "venue this record claims"
        ),
        (
            "row and column do not meet -- this contract is not measured in that venue "
            "(the grid holds bed rows by bed columns, build rows by the build column)"
        ),
        (
            "not evidence about the contract -- a skip, an xfail or an unexpected pass "
            "is a statement about the run, not the compiler"
        ),
        (
            "no provenance -- the build fixture never produced a toolchain, so the "
            "compiler is unnamed"
        ),
    )
    buckets = {reason: Discarded(reason) for reason in reasons}
    kept: "list[dict]" = []
    for r in records:
        surface = surface_for(r.get("nodeid", ""))
        if r.get("format") != FORMAT:
            buckets[reasons[0]].records.append(r)
        elif r.get("kind") != OBSERVATION:
            buckets[reasons[1]].records.append(r)
        elif surface is None:
            buckets[reasons[2]].records.append(r)
        elif r.get("profile") not in columns:
            buckets[reasons[3]].records.append(r)
        elif columns[r["profile"]] != r.get("venue"):
            buckets[reasons[4]].records.append(r)
        elif surface.venue != columns[r["profile"]]:
            buckets[reasons[5]].records.append(r)
        elif r.get("outcome") not in EVIDENTIAL:
            buckets[reasons[6]].records.append(r)
        elif not r.get("compiler_version") or not r.get("kernel_release"):
            buckets[reasons[7]].records.append(r)
        else:
            kept.append(r)
    return kept, [b for b in buckets.values() if b.records]


def _controls(kept: "list[dict]") -> "dict[tuple[str, str], dict]":
    """``(profile, run_id)`` -> the control's record, for every bed column a run drew."""
    return {
        (r["profile"], r["run_id"]): r
        for r in kept
        if r["nodeid"] == CONTROL_SURFACE.contract and r["venue"] == BED
    }


def _cell(record: dict, control: "dict | None") -> "tuple[dict, str | None]":
    """Build the cell *record* earns, and the rule-3 reason when the control did not back it."""
    cell = {
        "status": MEASURED_OK if record["outcome"] == PASSED else MEASURED_BROKEN,
        "nodeid": record["nodeid"],
        "venue": record["venue"],
        "as_of": record["as_of"],
        "outcome": record["outcome"],
        "compiler_version": record["compiler_version"],
        "kernel_release": record["kernel_release"],
        "control": CONTROL_SURFACE.contract if record["venue"] == BED else None,
    }
    why = None
    if record["venue"] == BED and (control is None or control["outcome"] != PASSED):
        how = (
            "no control record in this run"
            if control is None
            else f"the control {control['outcome']}"
        )
        why = f"{CONTROL_SURFACE.contract}: {how}"
        cell["status"] = MEASURED_BROKEN
    if cell["status"] == MEASURED_BROKEN:
        own = record.get("failure_summary") or record["outcome"]
        cell["failure_summary"] = (
            f"{own}; {why}" if why and record["outcome"] != PASSED else (why or own)
        )
    return cell, why


def collate(matrix: dict, records: "list[dict]") -> Collation:
    """Fold *records* into *matrix*. Pure: no clock, no filesystem, no environment."""
    kept, discarded = _bucket(records, matrix)
    result = Collation(matrix=copy.deepcopy(matrix), discarded=discarded, kept=len(kept))
    controls = _controls(kept)
    for record in sorted(kept, key=lambda r: (r["nodeid"], r["profile"])):
        surface = surface_for(record["nodeid"])  # kept by _bucket, so never None here
        where = (surface.id, record["profile"])
        control = controls.get((record["profile"], record["run_id"]))
        cell, why = _cell(record, control)
        if why is not None:
            result.refused_reasons.append(f"{where[0]} x {where[1]}: {why}")
        before = matrix["cells"][where[0]][where[1]]
        if cell == before:
            result.unchanged_with_records.append(where)
            continue
        result.matrix["cells"][where[0]][where[1]] = cell
        result.changed[where] = (before, cell)
    return result


def report(result: Collation, *, records_dir: Path, writing: bool) -> "list[str]":
    """Render *result* as the lines `main` prints, attributing every discard and change."""
    lines = [f"collate: {result.kept} usable record(s) from {records_dir}"]
    if result.discarded:
        total = sum(len(b.records) for b in result.discarded)
        lines.append(f"DISCARDED {total} record(s), every one attributed:")
        lines.extend(b.line() for b in result.discarded)
    if result.changed:
        lines.append(f"{len(result.changed)} cell(s) CHANGED:")
        for (surface, profile), (before, after) in sorted(result.changed.items()):
            moved = sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))
            what = (
                f"{before['status']} -> {after['status']}"
                if before["status"] != after["status"]
                else f"{after['status']}, changed: {', '.join(moved)}"
            )
            lines.append(f"  {surface} x {profile}: {what}")
    if result.unchanged_with_records:
        lines.append(
            f"{len(result.unchanged_with_records)} cell(s) re-measured to the same verdict"
        )
    untouched = sum(
        1
        for s, row in result.matrix["cells"].items()
        for p in row
        if (s, p) not in result.changed and (s, p) not in result.unchanged_with_records
    )
    lines.append(f"{untouched} cell(s) had NO record and were left untouched (never downgraded)")
    if result.refused_reasons:
        lines.append(
            f"CONTROL DID NOT BACK {len(result.refused_reasons)} cell(s) "
            "-- written measured-broken:"
        )
        lines.extend(f"  {why}" for why in result.refused_reasons)
    if writing:
        lines.append("artifact written")
    elif not result.changed:
        lines.append("no cell changed, so nothing was written")
    else:
        lines.append(f"{len(result.changed)} cell(s) WOULD change -- pass --write to fold them in")
    return lines


def main(argv: "list[str]") -> int:
    """CLI entry point: collate, report, and (with ``--write``) fold the result in."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--records", type=Path, default=DEFAULT_OBSERVATIONS_DIR)
    parser.add_argument("--matrix", type=Path, default=MATRIX_PATH)
    parser.add_argument(
        "--write", action="store_true", help="fold the records in; otherwise report only"
    )
    args = parser.parse_args(argv)
    matrix = json.loads(args.matrix.read_text(encoding="utf-8"))
    result = collate(matrix, read_records(args.records))
    problems = validation_errors(result.matrix)
    if problems:
        print("\n".join([*report(result, records_dir=args.records, writing=False), *problems]))
        return 2
    writing = args.write and bool(result.changed)
    if writing:
        args.matrix.write_text(json.dumps(result.matrix, indent=2) + "\n", encoding="utf-8")
    print("\n".join(report(result, records_dir=args.records, writing=writing)))
    return 0 if result.ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
