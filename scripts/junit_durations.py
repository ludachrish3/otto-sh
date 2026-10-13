#!/usr/bin/env python3
"""Rank test durations from pytest JUnit XML reports.

The standing answer to "where does the suite's wall-clock go": point it at
one or more ``reports/junit/**/*.xml`` files and it prints the slowest test
cases across them (``--top``, default 25), or the per-module totals
(``--by-file``). Every make test target writes into its own subdirectory of
``reports/junit/``, so after any gate run the data is already on disk — no
extra instrumentation pass needed.

``--outliers`` is the third view, for one test tree (``--under``, default
``tests.unit``): how its time is spread (count, summed time, p50/p90/p95/p99,
and the share held by the tests at or above ``--threshold`` seconds, default
1), then every such test, grouped by file, slowest file first. A file whose
source names ``subprocess`` or in-process pytest says so, read from the
file itself (``--root``, default the current directory): a hint at the
kind of cost, never a measurement of it. It is a REPORT: it exits 0
whatever it finds, and what it cannot read is named and left out, never an
error: a report missing, malformed or in an encoding it cannot read is
skipped, a test whose time is not a number is counted as unknown, a source
it cannot read gets no hint, and any other failure while reporting is
printed instead of raised. ``--top`` caps how many files it lists.
``make unit-outliers`` runs it on the last ``coverage-python`` leg's two
reports (the parallel leg and the ``serial_timing`` leg).

JUnit XML folds setup + call + teardown into one time per testcase. For a
phase split (is it the fixture or the test?), re-run the lane with pytest's
``--durations=N``, which lists the phases as separate rows.

Usage::

    scripts/junit_durations.py reports/junit/**/*.xml
    scripts/junit_durations.py --top 40 reports/junit/nox/tests_all-3.10.xml
    scripts/junit_durations.py --by-file reports/junit/nox/*.xml
    scripts/junit_durations.py --outliers --threshold 0.5 reports/junit/coverage-python/*.xml
"""

import argparse
import logging
import math
import re
import sys
import xml.etree.ElementTree as ET
from collections import defaultdict
from collections.abc import Generator
from pathlib import Path
from typing import NamedTuple


class Case(NamedTuple):
    """One JUnit testcase: its classname, its name, and its setup + call + teardown seconds."""

    classname: str
    name: str
    seconds: float | None
    """``None`` when the report's ``time`` is not a usable number."""


def parse_seconds(raw: str | None) -> float | None:
    """Read a testcase's ``time`` as seconds: ``0.0`` if absent, ``None`` if unusable."""
    if not raw:
        return 0.0
    try:
        seconds = float(raw)
    except ValueError:
        return None
    return seconds if math.isfinite(seconds) and seconds >= 0 else None


def iter_cases(xml_path: Path) -> Generator[Case, None, None]:
    """Yield a :class:`Case` for each testcase in the report."""
    tree = ET.parse(xml_path)  # noqa: S314 — parses our own trusted JUnit output, not untrusted input
    for tc in tree.iter("testcase"):
        yield Case(tc.get("classname") or "", tc.get("name") or "", parse_seconds(tc.get("time")))


class Distribution(NamedTuple):
    """How one tree's test time is spread, and how much of it the slow tests hold."""

    count: int
    total: float
    p50: float
    p90: float
    p95: float
    p99: float
    slow_count: int
    slow_total: float


class Outlier(NamedTuple):
    """One test at or above the threshold."""

    name: str
    seconds: float


class OutlierFile(NamedTuple):
    """One file's tests at or above the threshold, slowest first."""

    module: str
    total: float
    tests: list[Outlier]


def module_of(classname: str) -> str:
    """Return the test module a JUnit ``classname`` names, dropping a trailing ``TestX`` class."""
    parts = classname.split(".")
    while len(parts) > 1 and parts[-1][:1].isupper():
        parts.pop()
    return ".".join(parts)


def percentile(ordered: list[float], q: int) -> float:
    """Return the *q*-th percentile of *ordered* (ascending, non-empty), by nearest rank.

    The rank is ``ceil(n * q / 100)``: the smallest value with at least *q*
    percent of the cases at or below it.
    """
    rank = -(-len(ordered) * q // 100)
    return ordered[max(rank, 1) - 1]


def distribution(seconds: list[float], threshold: float) -> Distribution:
    """Summarize *seconds* (non-empty): count, sum, percentiles, the bucket at *threshold* up."""
    ordered = sorted(seconds)
    slow = [s for s in ordered if s >= threshold]
    return Distribution(
        count=len(ordered),
        total=sum(ordered),
        p50=percentile(ordered, 50),
        p90=percentile(ordered, 90),
        p95=percentile(ordered, 95),
        p99=percentile(ordered, 99),
        slow_count=len(slow),
        slow_total=sum(slow),
    )


def outlier_files(cases: list[Case], threshold: float) -> list[OutlierFile]:
    """Group the tests at or above *threshold* by module: slowest file, then slowest test, first."""
    grouped: dict[str, list[Outlier]] = defaultdict(list)
    for classname, name, seconds in cases:
        if seconds is not None and seconds >= threshold:
            module = module_of(classname)
            label = name if module == classname else f"{classname[len(module) + 1 :]}::{name}"
            grouped[module].append(Outlier(label, seconds))
    files = [
        OutlierFile(module, sum(t.seconds for t in tests), sorted(tests, key=lambda t: -t.seconds))
        for module, tests in grouped.items()
    ]
    return sorted(files, key=lambda f: (-f.total, f.module))


_IN_PROCESS_PYTEST = re.compile(r"\bpytester\b|\bpytest\.main\(|\brunpytest|\binline_run\(")
_SUBPROCESS = re.compile(r"\bsubprocess\b|\bcreate_subprocess_(exec|shell)\b")


def cost_hint(source: str) -> str:
    """Name what the file's source shows that commonly costs time, or return ``""``.

    Read from the text alone, so it says what the file NAMES (a test that
    patches ``subprocess.Popen`` names it too), never what a given test
    spent its time on.
    """
    hints = []
    if _IN_PROCESS_PYTEST.search(source):
        hints.append("in-process pytest")
    if _SUBPROCESS.search(source):
        hints.append("subprocess")
    return f"source names: {', '.join(hints)}" if hints else ""


def _hint_for(module: str, root: Path) -> str:
    """Return the hint for *module*'s source under *root*, or ``""`` when it cannot be read."""
    path = root / (module.replace(".", "/") + ".py")
    try:
        return cost_hint(path.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return ""


def _unreadable_note(unknown: int) -> str:
    return f"  {unknown} case(s) with an unreadable time, not counted"


def outlier_report(
    cases: list[Case], under: str, threshold: float, root: Path, top: int | None
) -> list[str]:
    """Return the ``--outliers`` report for the cases whose classname is under *under*."""
    mine = [c for c in cases if c.classname == under or c.classname.startswith(under + ".")]
    if not mine:
        return [f"=== {under}: no test cases in the report(s) read ==="]
    seconds = [c.seconds for c in mine if c.seconds is not None]
    unknown = len(mine) - len(seconds)
    note = [_unreadable_note(unknown)] if unknown else []
    if not seconds:
        return [f"=== {under}: {len(mine)} case(s), none with a readable time ===", *note]
    dist = distribution(seconds, threshold)
    share = 100 * dist.slow_total / dist.total if dist.total else 0.0
    files = outlier_files(mine, threshold)
    lines = [
        f"=== {under}: {dist.count} case(s), {dist.total:.1f}s summed ===",
        *note,
        (
            f"  p50 {1000 * dist.p50:.0f} ms, p90 {1000 * dist.p90:.0f} ms, "
            f"p95 {1000 * dist.p95:.0f} ms, p99 {1000 * dist.p99:.0f} ms"
        ),
        (
            f"  >= {threshold:g}s: {dist.slow_count} case(s), {dist.slow_total:.1f}s summed "
            f"({share:.0f}% of the tree), in {len(files)} file(s), slowest first:"
        ),
    ]
    for entry in files[:top]:
        hint = _hint_for(entry.module, root)
        suffix = f"  [{hint}]" if hint else ""
        lines.append(f"  {entry.total:8.1f}s  {len(entry.tests):4}  {entry.module}{suffix}")
        lines.extend(f"  {t.seconds:18.1f}s  {t.name}" for t in entry.tests)
    if top is not None and len(files) > top:
        lines.append(f"  ... {len(files) - top} more file(s); drop --top to list them all")
    return lines


def main(argv: list[str] | None = None) -> int:
    """Parse arguments, print the requested view, and return exit status (always 0)."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("reports", nargs="+", type=Path, help="JUnit XML file(s)")
    parser.add_argument(
        "--top",
        type=int,
        default=None,
        help="How many rows to print (default: 25); with --outliers, how many files (default: all)",
    )
    parser.add_argument(
        "--by-file",
        action="store_true",
        help="Aggregate per classname (module) instead of listing individual tests",
    )
    parser.add_argument(
        "--outliers",
        action="store_true",
        help="Report one tree's time distribution and its tests at or above --threshold, by file",
    )
    parser.add_argument(
        "--threshold", type=float, default=1.0, help="--outliers: seconds (default: 1)"
    )
    parser.add_argument(
        "--under",
        default="tests.unit",
        help="--outliers: the dotted classname prefix of the tree (default: tests.unit)",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(),
        help="--outliers: the repo root the test files are read from (default: .)",
    )
    args = parser.parse_args(argv)

    cases: list[Case] = []
    for xml_path in args.reports:
        if not args.outliers:
            cases.extend(iter_cases(xml_path))
            continue
        try:
            cases.extend(iter_cases(xml_path))
        # ValueError covers a codec expat cannot use (UnicodeError is one).
        except (OSError, ET.ParseError, LookupError, ValueError) as exc:
            print(f"unit outliers: skipped {xml_path}, which could not be read: {exc}")

    if args.outliers:
        try:
            report = outlier_report(cases, args.under, args.threshold, args.root, args.top)
        except Exception:
            # A report, never a gate: say what broke (with its traceback), exit 0.
            logging.getLogger("unit outliers").exception("unit outliers: the report failed")
            report = []
        if report:
            print("\n".join(report))
        return 0

    if args.by_file:
        per_file: dict[str, float] = defaultdict(float)
        for classname, _, seconds in cases:
            if seconds is not None:
                per_file[classname] += seconds
        rows = [(total, classname) for classname, total in per_file.items()]
    else:
        rows = [
            (seconds, f"{classname}::{name}")
            for classname, name, seconds in cases
            if seconds is not None
        ]

    rows.sort(reverse=True)
    total = sum(seconds for seconds, _ in rows)
    print(f"=== {len(cases)} case(s) across {len(args.reports)} report(s), {total:.1f}s summed ===")
    unknown = sum(seconds is None for _, _, seconds in cases)
    if unknown:
        print(_unreadable_note(unknown))
    for seconds, label in rows[: 25 if args.top is None else args.top]:
        print(f"  {seconds:8.1f}s  {label}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
