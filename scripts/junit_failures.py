#!/usr/bin/env python3
"""Summarize failures and errors from pytest JUnit XML reports.

Useful for triaging CI flakes: point it at one or more ``reports/junit/**/*.xml``
files and it prints each failing/erroring test case with its message, and
optionally the full failure text. Each make test target writes into its own
subdirectory (e.g. ``reports/junit/nox-unit/``, ``reports/junit/coverage/``).

Usage::

    scripts/junit_failures.py reports/junit/**/*.xml
    scripts/junit_failures.py --full reports/junit/nox/tests_all-3.13.xml
"""

from __future__ import annotations

import argparse
import sys
import xml.etree.ElementTree as ET
from pathlib import Path


def iter_problems(xml_path: Path):
    """Yield ``(kind, classname::name, message, text)`` for each failure/error."""
    tree = ET.parse(xml_path)  # noqa: S314 — parses our own trusted JUnit output, not untrusted input
    for tc in tree.iter("testcase"):
        for kind in ("failure", "error"):
            el = tc.find(kind)
            if el is not None:
                name = f"{tc.get('classname')}::{tc.get('name')}"
                yield kind, name, el.get("message") or "", el.text or ""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("reports", nargs="+", type=Path, help="JUnit XML file(s)")
    parser.add_argument(
        "--full", action="store_true", help="Print the full failure text, not just the message"
    )
    args = parser.parse_args(argv)

    total = 0
    for xml_path in args.reports:
        problems = list(iter_problems(xml_path))
        print(f"=== {xml_path} ({len(problems)} problem(s)) ===")
        for kind, name, message, text in problems:
            total += 1
            print(f"  [{kind.upper()}] {name}")
            body = text if args.full else message
            for line in body.strip().splitlines():
                print(f"    {line}")
        print()

    return 1 if total else 0


if __name__ == "__main__":
    sys.exit(main())
