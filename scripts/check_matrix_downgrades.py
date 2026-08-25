#!/usr/bin/env python3
"""Classify what a re-collated support matrix would change, and refuse a downgrade.

The release re-measures the bed and commits the result (``make release-matrix``).
Most of what a re-measure produces needs nobody's attention: a cell that went
from broken to ok, a newly added host that works, a cell whose evidence moved
under an unchanged verdict. What DOES need a person is a claim that otto is
broken -- that is either a real regression, in which case the release must stop,
or a known gap, in which case it should be recorded deliberately rather than
swept into a release nobody read.

So the gate is DIRECTIONAL rather than a diff check:

* BLOCKING when the new status is ``measured-broken`` and the old one is not,
  or when a ``measured-ok`` verdict is lost for any reason.
* ALLOWED otherwise -- improvements, new non-broken cells, and
  ``untested -> not-observable``, which makes no claim about the product.
* REMOVED cells are reported and never block: a surface or profile leaving the
  tree is a tree change, not a measurement.

Pure by design -- two files in, a report out, and no version control of any
kind. The plumbing that decides WHICH two files (the committed matrix at HEAD
against the working tree) lives in the Makefile. That keeps this script
testable over synthetic matrices, and it keeps
``scripts/collate_support_matrix.py`` untouched: the collate step is guarded to
hold no version-control vocabulary at all, and this change does not spend that.

Usage, from the repo root::

    python scripts/check_matrix_downgrades.py --baseline OLD.json [--candidate NEW.json]

Exit codes: 0 nothing blocking, 1 at least one blocking transition, 2 the input
could not be read. Two and one are distinct on purpose -- a parse failure must
never be mistaken for a clean gate, nor for a downgrade someone would then go
looking for in a diff that does not contain one.
"""

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path

MATRIX_PATH = Path("schemas/support_matrix.json")

MEASURED_OK = "measured-ok"
MEASURED_BROKEN = "measured-broken"

BLOCKING = "blocking"
ALLOWED = "allowed"
REMOVED = "removed"


@dataclass(frozen=True)
class Transition:
    """One cell's status change, and which side of the gate it falls on."""

    surface: str
    profile: str
    old: "str | None"
    new: "str | None"
    kind: str

    @property
    def line(self) -> str:
        """The one-line rendering a reviewer reads in the release log."""
        old = self.old or "(absent)"
        new = self.new or "(absent)"
        return f"  {self.surface} x {self.profile}: {old} -> {new}"


def classify(old_status: "str | None", new_status: "str | None") -> str:
    """Which side of the gate a single cell's transition falls on.

    Two rules, and they overlap deliberately: ``measured-ok -> measured-broken``
    is caught by both, because it is both a new brokenness claim and a lost
    verdict, and neither rule should be the only thing standing in front of it.
    """
    if new_status is None:
        return REMOVED
    if new_status == MEASURED_BROKEN and old_status != MEASURED_BROKEN:
        return BLOCKING
    if old_status == MEASURED_OK and new_status != MEASURED_OK:
        return BLOCKING
    return ALLOWED


def _statuses(matrix: dict) -> "dict[tuple[str, str], str]":
    return {
        (surface, profile): cell.get("status")
        for surface, row in matrix.get("cells", {}).items()
        for profile, cell in row.items()
    }


def transitions(baseline: dict, candidate: dict) -> "list[Transition]":
    """Every cell whose status differs, plus every cell the candidate dropped.

    Sorted, so a release log and a hand-run report the same order and a reader
    comparing two runs is comparing like with like.
    """
    before = _statuses(baseline)
    after = _statuses(candidate)
    out: "list[Transition]" = []
    for key in sorted(before.keys() | after.keys()):
        old, new = before.get(key), after.get(key)
        if old == new:
            continue
        out.append(Transition(key[0], key[1], old, new, classify(old, new)))
    return out


_HEADINGS = [
    (BLOCKING, "NEEDS A PERSON — a broken claim, or a lost measured-ok verdict"),
    (ALLOWED, "auto-accepted — improvements and new non-broken cells"),
    (REMOVED, "removed from the grid (a tree change, never a measurement)"),
]


def main(argv: "list[str]") -> int:
    """Report every status change, and refuse the ones a person must see."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--candidate", type=Path, default=MATRIX_PATH)
    args = parser.parse_args(argv)

    try:
        baseline = json.loads(args.baseline.read_text(encoding="utf-8"))
        candidate = json.loads(args.candidate.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        print(f"matrix gate: cannot read the matrices to compare: {e!r}", file=sys.stderr)
        return 2

    changed = transitions(baseline, candidate)
    if not changed:
        print("matrix gate: no cell changed status")
        return 0

    for kind, heading in _HEADINGS:
        rows = [t for t in changed if t.kind == kind]
        if rows:
            print(f"matrix gate: {len(rows)} {heading}")
            print("\n".join(t.line for t in rows))

    blocking = [t for t in changed if t.kind == BLOCKING]
    if blocking:
        print(
            f"\nmatrix gate: REFUSING to auto-commit {len(blocking)} cell(s). This is either a "
            f"regression the release must not ship, or a gap worth recording on purpose. "
            f"Review the diff, commit schemas/support_matrix.json yourself, and re-run.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main(sys.argv[1:]))
