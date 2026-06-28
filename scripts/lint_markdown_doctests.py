#!/usr/bin/env python3
"""Fail if a doctest prompt (``>>>``) appears where Sphinx will not execute it.

Sphinx's doctest builder only runs ```{doctest}``` fenced blocks. A ``>>>`` line
in any other fence (```python```, ```pycon```, …) renders as code but is never
executed, so such "examples" can silently drift from the real API. This linter
makes that pattern a hard error.

Rules:
  * A fence opened with >=4 backticks is a *display* fence (used to show fence
    syntax) and its contents are not linted.
  * Inside a 3-backtick fence whose info string is not ``{doctest}``, any line
    matching ``^\\s*>>>`` is an offense.
  * A ``>>>`` line outside any fence is an offense.
  * ``<!-- doctest-lint: ignore -->`` on the line immediately preceding a fence
    exempts that fence (for intentional, non-runnable pedagogy).

Usage: python scripts/lint_markdown_doctests.py docs/
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

PROMPT = re.compile(r"^\s*>>>(\s|$)")
FENCE = re.compile(r"^\s*(?P<ticks>`{3,})(?P<info>.*)$")
IGNORE = "<!-- doctest-lint: ignore -->"
SKIP_PARTS = ("_build", "superpowers")
_STANDARD_FENCE_TICKS = 3  # CommonMark: a standard fenced code block uses exactly 3 backticks


def lint_file(path: Path) -> list[tuple[int, str]]:
    offenses: list[tuple[int, str]] = []
    open_ticks = 0  # length of the open fence, 0 if none
    info = ""  # info string of the open fence
    ignored = False  # the open fence is exempt
    pending_ignore = False  # the previous line was the ignore comment
    for n, line in enumerate(path.read_text().splitlines(), 1):
        m = FENCE.match(line)
        if open_ticks == 0:
            if m:
                open_ticks = len(m.group("ticks"))
                info = m.group("info").strip()
                ignored = pending_ignore
                pending_ignore = False
                continue
            pending_ignore = line.strip() == IGNORE
            if PROMPT.match(line):
                offenses.append((n, "doctest prompt outside any fence"))
        else:
            if m and len(m.group("ticks")) >= open_ticks and not m.group("info").strip():
                open_ticks = 0
                info = ""
                ignored = False
                continue
            if (
                open_ticks == _STANDARD_FENCE_TICKS
                and info != "{doctest}"
                and not ignored
                and PROMPT.match(line)
            ):
                offenses.append(
                    (n, f"doctest prompt in ```{info or '(plain)'} fence (not {{doctest}})")
                )
    return offenses


def main(argv: list[str]) -> int:
    roots = [Path(a) for a in argv[1:]] or [Path("docs")]
    failures = 0
    for root in roots:
        for md in sorted(root.rglob("*.md")):
            if any(part in SKIP_PARTS for part in md.parts):
                continue
            for n, why in lint_file(md):
                print(f"{md}:{n}: {why}")
                failures += 1
    if failures:
        print(
            f"\n{failures} doctest-lint offense(s). Move runnable examples into "
            f"```{{doctest}}``` fences, or mark intentional non-runnable ones "
            f"with '{IGNORE}' on the line before the fence."
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
