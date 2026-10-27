"""The duplicate-code ratchet: pylint's clone pairs against a committed baseline.

Run from the repo root (``make lint-arch`` and nox's ``lint`` session do)::

    uv run --group lint python scripts/check_duplicate_code.py           # gate
    uv run --group lint python scripts/check_duplicate_code.py --write   # rewrite the baseline

pylint's ``similarities`` checker (``R0801``, "Similar lines in N files")
reports every pair of modules under ``src/otto`` sharing at least
``MIN_SIMILAR_LINES`` consecutive lines, comments, docstrings and imports
left out. ``scripts/duplicate_code_baseline.txt`` lists the pairs the tree
had when the gate landed, one unordered pair per line, and the contract is
tach.toml's: no pair may appear that the baseline does not list, and a
listed pair that no longer reports comes out of the baseline in the same
change, so the file only shrinks. Pairs are keyed on module names, not line
ranges, because every edit above a clone moves its lines.

What this catches is the textual kind of duplication: a block copied
between modules. The other kind — the same decision computed at two entry
points in different words — is invisible here and lives in the review
(AGENTS.md, "one decision, one place").
"""

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BASELINE = REPO_ROOT / "scripts" / "duplicate_code_baseline.txt"
SCAN_ROOTS = ["src/otto"]
MIN_SIMILAR_LINES = 6

_SIMILAR_RE = re.compile(r"^R0801: Similar lines in (\d+) files$")
_MEMBER_RE = re.compile(r"^==(?P<module>[\w.]+):\[(?P<start>\d+):(?P<end>\d+)\]$")

_BASELINE_HEADER = """\
# The duplicate-code ratchet baseline (scripts/check_duplicate_code.py; Makefile:
# lint-arch). One unordered pair of modules per line, sorted: the clone pairs
# pylint's similarities checker reported when the gate landed. The contract is
# tach.toml's: no pair may appear that this file does not list, and a listed
# pair that stops reporting comes out of this file in the same change, so it
# only shrinks. `--write` regenerates it from the tree; review the diff, since
# an added line is a new clone, not a baseline update.
"""


@dataclass(frozen=True)
class Clone:
    """One ``Similar lines`` report: the modules it spans and where in each."""

    modules: tuple[str, ...]
    """The module names, sorted, so the same pair keys the same whatever pylint's order."""
    spans: tuple[str, ...]
    """``module:[start:end]`` for each member, for the message only."""

    @property
    def key(self) -> str:
        """The baseline line for this clone."""
        return " ".join(self.modules)


def pylint_argv(roots: list[str]) -> list[str]:
    """Build the pylint invocation: the similarities checker alone, one process."""
    return [
        sys.executable,
        "-m",
        "pylint",
        "--disable=all",
        "--enable=duplicate-code",
        f"--min-similarity-lines={MIN_SIMILAR_LINES}",
        "--ignore-comments=y",
        "--ignore-docstrings=y",
        "--ignore-imports=y",
        # The checker compares every file against every other; pylint's
        # process pool splits the files up, so each worker sees only its share.
        "--jobs=1",
        "--score=n",
        "--persistent=n",
        "--msg-template={msg_id}: {msg}",
        *roots,
    ]


def parse_report(text: str) -> list[Clone]:
    """Turn pylint's text output into the clones it reports.

    A report is a ``R0801: Similar lines in N files`` line followed by one
    ``==module:[start:end]`` line per member; whatever follows (the shared
    lines themselves) is skipped up to the next report.
    """
    clones: list[Clone] = []
    members: list[tuple[str, str]] = []
    expected = 0

    def flush() -> None:
        if members:
            ordered = sorted(members)
            clones.append(
                Clone(
                    modules=tuple(m for m, _ in ordered),
                    spans=tuple(s for _, s in ordered),
                )
            )
        members.clear()

    for raw in text.splitlines():
        line = raw.strip()
        similar = _SIMILAR_RE.match(line)
        if similar:
            flush()
            expected = int(similar.group(1))
            continue
        member = _MEMBER_RE.match(line)
        if member and expected and len(members) < expected:
            module = member.group("module")
            members.append((module, f"{module}:[{member.group('start')}:{member.group('end')}]"))
    flush()
    return clones


def read_baseline(path: Path) -> set[str]:
    """Read the pairs the baseline lists, comment and blank lines dropped."""
    if not path.exists():
        return set()
    return {
        line.strip()
        for line in path.read_text().splitlines()
        if line.strip() and not line.startswith("#")
    }


def render_baseline(clones: list[Clone]) -> str:
    """Render the baseline file for *clones*: the header, then every key once, sorted."""
    keys = sorted({c.key for c in clones})
    return _BASELINE_HEADER + "".join(f"{k}\n" for k in keys)


@dataclass(frozen=True)
class Verdict:
    """What the report says against the baseline."""

    new: list[Clone]
    """Reported pairs the baseline does not list."""
    gone: list[str]
    """Listed pairs the report no longer has."""

    @property
    def holds(self) -> bool:
        """Whether the tree matches the baseline exactly."""
        return not self.new and not self.gone


def compare(clones: list[Clone], baseline: set[str]) -> Verdict:
    """Split the report into pairs the baseline does not list and listed pairs that went."""
    found = {c.key for c in clones}
    return Verdict(
        new=[c for c in clones if c.key not in baseline],
        gone=sorted(baseline - found),
    )


def run_pylint(roots: list[str]) -> str:
    """Run pylint over *roots* and return its text; exit status is not a verdict.

    pylint exits non-zero whenever it reports anything, so the parsed
    report, not the status, says whether the gate holds. A crash (status
    bit 1) or a usage error (bit 32) still fails loudly.
    """
    proc = subprocess.run(  # noqa: S603 — argv is this module's own, no caller input
        pylint_argv(roots), cwd=REPO_ROOT, capture_output=True, text=True, check=False
    )
    if proc.returncode & 1 or proc.returncode & 32:
        sys.stderr.write(proc.stdout)
        sys.stderr.write(proc.stderr)
        msg = f"pylint failed (exit {proc.returncode})"
        raise SystemExit(msg)
    return proc.stdout


def main(argv: list[str] | None = None) -> int:
    """Gate the tree against the baseline, or rewrite the baseline with ``--write``."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--write", action="store_true", help="rewrite the baseline from the tree instead of gating"
    )
    args = parser.parse_args(argv)

    clones = parse_report(run_pylint(SCAN_ROOTS))
    pairs = len({c.key for c in clones})
    rel = BASELINE.relative_to(REPO_ROOT)
    if args.write:
        BASELINE.write_text(render_baseline(clones))
        print(f"wrote {pairs} pairs to {rel}")
        return 0

    verdict = compare(clones, read_baseline(BASELINE))
    if verdict.holds:
        print(f"duplicate code: {pairs} known pairs, nothing new")
        return 0
    for clone in verdict.new:
        print(f"new clone pair, not in {rel}: " + " ".join(clone.spans))
    for key in verdict.gone:
        print(f"listed pair no longer reports; remove it from {rel}: {key}")
    print(
        "A new pair is code copied between modules: fold it into one function the\n"
        "copies call. A pair that stopped reporting is a ratchet step: drop its line."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
