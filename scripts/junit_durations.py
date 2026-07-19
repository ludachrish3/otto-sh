#!/usr/bin/env python3
"""Rank test durations from pytest JUnit XML reports.

The standing answer to "where does the suite's wall-clock go": point it at
one or more ``reports/junit/**/*.xml`` files and it prints the slowest test
cases across them (``--top``, default 25), or the per-module totals
(``--by-file``). Every make test target writes into its own subdirectory of
``reports/junit/``, so after any gate run the data is already on disk — no
extra instrumentation pass needed.

JUnit XML folds setup + call + teardown into one time per testcase. For a
phase split (is it the fixture or the test?), re-run the lane with pytest's
``--durations=N``, which lists the phases as separate rows.

Usage::

    scripts/junit_durations.py reports/junit/**/*.xml
    scripts/junit_durations.py --top 40 reports/junit/nox/tests_all-3.10.xml
    scripts/junit_durations.py --by-file reports/junit/nox/*.xml
"""

from __future__ import annotations

import argparse
import sys
import xml.etree.ElementTree as ET
from collections import defaultdict
from collections.abc import Generator
from pathlib import Path


def iter_cases(xml_path: Path) -> Generator[tuple[str, str, float], None, None]:
    """Yield ``(classname, name, seconds)`` for each testcase in the report."""
    tree = ET.parse(xml_path)  # noqa: S314 — parses our own trusted JUnit output, not untrusted input
    for tc in tree.iter("testcase"):
        yield tc.get("classname") or "", tc.get("name") or "", float(tc.get("time") or 0.0)


def main(argv: list[str] | None = None) -> int:
    """Parse arguments, print the duration ranking, and return exit status."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("reports", nargs="+", type=Path, help="JUnit XML file(s)")
    parser.add_argument("--top", type=int, default=25, help="How many rows to print (default: 25)")
    parser.add_argument(
        "--by-file",
        action="store_true",
        help="Aggregate per classname (module) instead of listing individual tests",
    )
    args = parser.parse_args(argv)

    cases: list[tuple[str, str, float]] = []
    for xml_path in args.reports:
        cases.extend(iter_cases(xml_path))

    if args.by_file:
        per_file: dict[str, float] = defaultdict(float)
        for classname, _, seconds in cases:
            per_file[classname] += seconds
        rows = [(total, classname) for classname, total in per_file.items()]
    else:
        rows = [(seconds, f"{classname}::{name}") for classname, name, seconds in cases]

    rows.sort(reverse=True)
    total = sum(seconds for seconds, _ in rows)
    print(f"=== {len(cases)} case(s) across {len(args.reports)} report(s), {total:.1f}s summed ===")
    for seconds, label in rows[: args.top]:
        print(f"  {seconds:8.1f}s  {label}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
