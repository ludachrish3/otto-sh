"""The old project words, and the lines allowed to keep one.

Spec ``docs/superpowers/specs/2026-10-17-649-project-vocabulary-design.md``
(Guards). One home for what every check of the vocabulary shares: the
whole-word pattern, the cited spec's name, the git-sense allowlist and the
rejection inventory. The vocabulary guard
(``tests/unit/test_vocabulary_guard.py``), the per-file prose check below and
the rename script's residual inventory all read them, so the lines they allow
cannot drift apart.

Run as a module to list the old words left in some files, allowed lines aside::

    uv run --no-sync python -m tests._fixtures.vocabulary src/otto/coverage/reporter.py
"""

import re
import sys
from pathlib import Path

OLD_WORD = re.compile(r"\b(?:repo|repos|Repo|sut_dir|sut_dirs)\b")
"""Whole-word, so ``repository``, ``repo1`` and ``NotAGitRepoError`` never match."""

CITED_SPEC = "2026-10-06-repo-and-scope-inputs-design"
"""A spec's file name that docstrings and docs cite; a citation is history, so it is
removed from a line before the line is searched."""

GIT_SENSE: dict[str, list[str]] = {
    # path -> exact fragments of lines that use "repo" in git's sense. A line
    # containing one is allowed. Each fragment must still occur, so a stale
    # entry fails too. The guard's word scope is src/ and docs/, so it applies
    # only those entries (yet checks that every entry still occurs). The
    # entries outside that scope are a SAMPLE, not an inventory: the git-sense
    # lines the rename's residual pass kept and named, while scripts/,
    # noxfile.py and the rest carry many more git-sense "repo"s nobody lists.
    # otto's own checkout: the worktree rule names its root.
    "AGENTS.md": ["treat the main checkout at the repo root as READ-ONLY"],
    # otto's own checkout: upstream drift cannot change its bytes.
    "Makefile": ["upstream drift cannot change a byte this repo"],
    # otto's own checkout: the {repo} manifest token and the <repo> placeholder
    # name it, and the scrub orders its root against the examples root.
    "scripts/refresh_docs_captures.py": [
        "# root before the repo root (an examples root under ROOT is a",
        "# substring of it and must win), and the repo root before the",
        '(re.escape(str(ROOT)), "<repo>"),',
        '"{repo}": str(ROOT),  # captures run from {tmp}; repo scripts need an absolute path',
        '"{repo}": ".",',
    ],
    # otto's own checkout: the child's cwd, the script's sys.path and a
    # failure message's relative path.
    "scripts/import_budget.py": [
        "the caller's cwd handed the child the repo root, which every other test",
        "# repo root on the path. Done lazily, here, so merely importing this module",
        "repo-relative when it is inside the repo.",
    ],
}

REJECTION_INVENTORY: dict[str, list[str]] = {
    # path -> the exact lines (compared stripped, whole line) that must spell an
    # old word in order to refuse it, or to tell a reader it was removed. Each
    # must occur exactly once in its file.
    # Coverage metadata an older otto wrote is refused by its old keys (spec §5.5).
    "src/otto/coverage/reporter.py": ['_OLD_META_KEYS = ["repo_name", "sut_dir"]'],
    # docs: a removed settings token, named so a reader can find what replaced it
    "docs/configuration/settings.md": [
        "#### Removed: `${sut_dir}`",
        "Earlier versions expanded `${sut_dir}` to the project root.  It is gone, and",
        "directory literally named `${sut_dir}`.",
        'Drop the prefix: `"${sut_dir}/tests"` becomes `"tests"`, and',
        '`"${sut_dir}/../shared"` becomes `"../shared"`.',
    ],
}


def rejection_exempt(rel: str, line: str) -> bool:
    """True when *line* of *rel* is, whole and stripped, one of its rejection lines."""
    return line.strip() in REJECTION_INVENTORY.get(rel, [])


def allowed(rel: str, line: str) -> bool:
    """True when *line* of *rel* may keep an old word: a rejection line or a git-sense line."""
    return rejection_exempt(rel, line) or any(f in line for f in GIT_SENSE.get(rel, []))


def old_words(root: Path, rel: str) -> list[str]:
    """``path:line: text`` for each line of *rel* that spells an old word, allowed lines aside."""
    text = (root / rel).read_text(encoding="utf-8")
    return [
        f"{rel}:{n}: {line.strip()}"
        for n, line in enumerate(text.splitlines(), 1)
        if OLD_WORD.search(line.replace(CITED_SPEC, "")) and not allowed(rel, line)
    ]


def main(argv: list[str]) -> int:
    """Print every old word left in the files named in *argv*; exit 1 if any."""
    found = [hit for rel in argv for hit in old_words(Path.cwd(), rel)]
    if found:
        sys.stdout.write("\n".join(found) + "\n")
    return 1 if found else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
