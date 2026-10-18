"""The old project words cannot come back.

Spec ``docs/superpowers/specs/2026-10-17-649-project-vocabulary-design.md``
(Guards). ``OTTO_SUT_DIRS`` is refused in every tracked file. ``sut_dir`` /
``sut_dirs`` and a whole-word ``repo`` / ``repos`` / ``Repo`` are refused in
``src/`` and ``docs/``; the test tree joins when its fixtures are renamed. The
match is whole-word, so ``repository``, ``repo1`` and ``NotAGitRepoError`` never
fire it. Every exemption is by path (below), by an exact line fragment
(``GIT_SENSE``) or by an exact whole line (``REJECTION_INVENTORY``); the two
tables live in ``tests/_fixtures/vocabulary.py``, shared with the prose check
and the rename script's residual inventory, and each entry says why.
"""

import re
import subprocess
from pathlib import Path

import pytest

from tests._fixtures import vocabulary
from tests._fixtures.gitrepo import git_env
from tests._fixtures.paths import PROJECT_ROOT
from tests._fixtures.vocabulary import (
    CITED_SPEC,
    GIT_SENSE,
    OLD_WORD,
    REJECTION_INVENTORY,
    allowed,
)

OLD_VARIABLE = re.compile(r"\bOTTO_SUT_DIRS\b")
WORD_SCOPE = ("src/", "docs/")

EXEMPT_PREFIXES = [
    "docs/superpowers/",  # historical specs and plans keep the words of their day
    "CHANGELOG.md",  # generated from commit subjects; existing entries are history
    "tests/unit/test_moved_name_spellings.py",  # negative inventory: spells gone modules
    "tests/unit/test_vocabulary_renames.py",  # negative inventory: spells the old names
    "tests/unit/config/test_ottopath_env.py",  # pins that OTTO_SUT_DIRS does nothing
    "tests/unit/test_vocabulary_guard.py",  # this guard
]


def _exempt(rel: str) -> bool:
    return any(rel == p or rel.startswith(p) for p in EXEMPT_PREFIXES)


def offences(root: Path, files: list[str]) -> list[str]:
    """``path:line: word`` for every old word in *files* (paths relative to *root*)."""
    found = []
    for rel in files:
        if _exempt(rel):
            continue
        try:
            text = (root / rel).read_text(encoding="utf-8")
        except (UnicodeDecodeError, IsADirectoryError, FileNotFoundError):
            continue
        in_scope = rel.startswith(WORD_SCOPE)
        for lineno, line in enumerate(text.splitlines(), 1):
            found.extend(f"{rel}:{lineno}: {m.group(0)}" for m in OLD_VARIABLE.finditer(line))
            if not in_scope or allowed(rel, line):
                continue
            words = OLD_WORD.finditer(line.replace(CITED_SPEC, ""))
            found.extend(f"{rel}:{lineno}: {m.group(0)}" for m in words)
    return found


def stale_exemptions(root: Path) -> list[str]:
    """Each git-sense fragment no longer found, and each rejection line not found exactly once."""
    stale = [
        f"{rel}: {fragment!r}"
        for rel, fragments in GIT_SENSE.items()
        for fragment in fragments
        if fragment not in (root / rel).read_text(encoding="utf-8")
    ]
    for rel, lines in REJECTION_INVENTORY.items():
        present = [line.strip() for line in (root / rel).read_text(encoding="utf-8").splitlines()]
        stale += [
            f"{rel}: {line!r} x{present.count(line)}" for line in lines if present.count(line) != 1
        ]
    return stale


def _tracked(home: Path) -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=PROJECT_ROOT,
        env=git_env(home),
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return [p for p in out.split("\0") if p]


def test_no_old_word_in_the_tree(tmp_path):
    tracked = _tracked(tmp_path)
    # Not vacuous: the scan really walks the tree, src/ and docs/ included.
    assert "src/otto/utils.py" in tracked
    assert "docs/overview.md" in tracked
    found = offences(PROJECT_ROOT, tracked)
    assert found == [], "\n".join(found)


def test_every_exemption_still_matches_exactly():
    assert stale_exemptions(PROJECT_ROOT) == []


REJECTION_LINE = REJECTION_INVENTORY["src/otto/coverage/reporter.py"][0]
REPORTER = "src/otto/coverage/reporter.py"


def _plant(root: Path, rel: str, body: str) -> None:
    (root / rel).parent.mkdir(parents=True, exist_ok=True)
    (root / rel).write_text(body)


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (f"{REJECTION_LINE}\n", []),
        (f"    {REJECTION_LINE}  \n", []),
        (f'{REJECTION_LINE}\nroot = meta["sut_dir"]\n', [f"{REPORTER}:2: sut_dir"]),
        (
            f'{REJECTION_LINE}; root = meta["sut_dir"]\n',
            [f"{REPORTER}:1: sut_dir", f"{REPORTER}:1: sut_dir"],
        ),
        (f"# {REJECTION_LINE}\n", [f"{REPORTER}:1: sut_dir"]),
        (
            f"{REJECTION_LINE}  # the repo\n",
            [f"{REPORTER}:1: sut_dir", f"{REPORTER}:1: repo"],
        ),
    ],
    ids=[
        "exact",
        "indented",
        "next-line",
        "same-line-trailing",
        "commented-out",
        "trailing-comment",
    ],
)
def test_the_rejection_inventory_exempts_its_whole_line_and_nothing_else(tmp_path, body, expected):
    _plant(tmp_path, REPORTER, body)
    assert offences(tmp_path, [REPORTER]) == expected


@pytest.mark.parametrize(("copies", "count"), [(0, 0), (2, 2)], ids=["missing", "duplicated"])
def test_a_rejection_line_must_occur_exactly_once(tmp_path, copies, count):
    _seed_every_exemption(tmp_path)
    assert stale_exemptions(tmp_path) == []
    _plant(tmp_path, REPORTER, f"{REJECTION_LINE}\n" * copies + "x = 1\n")
    assert stale_exemptions(tmp_path) == [f"{REPORTER}: {REJECTION_LINE!r} x{count}"]


def _seed_every_exemption(root: Path) -> None:
    """Write every file the two tables name, each exemption present exactly once.

    Generated from the tables themselves, so an entry added later (a docs
    page's rejection lines, a git-sense fragment) is seeded too.
    """
    bodies: dict[str, list[str]] = {}
    for table in (REJECTION_INVENTORY, GIT_SENSE):
        for rel, entries in table.items():
            bodies.setdefault(rel, []).extend(entries)
    for rel, entries in bodies.items():
        _plant(root, rel, "".join(f"{entry}\n" for entry in entries))


def test_a_git_sense_fragment_exempts_only_its_own_lines_of_its_own_file(tmp_path, monkeypatch):
    monkeypatch.setitem(vocabulary.GIT_SENSE, "src/otto/x.py", ["not a git repo"])
    _plant(tmp_path, "src/otto/x.py", "# git: not a git repo here\n# load the repo\n")
    _plant(tmp_path, "src/otto/y.py", "# git: not a git repo here\n")
    assert offences(tmp_path, ["src/otto/x.py", "src/otto/y.py"]) == [
        "src/otto/x.py:2: repo",
        "src/otto/y.py:1: repo",
    ]


def test_a_planted_old_word_turns_the_guard_red(tmp_path):
    _plant(tmp_path, "src/otto/x.py", '"""Load the repo."""\nsut_dir = 1\n')
    _plant(tmp_path, "README.md", "export OTTO_SUT_DIRS=/x\nthe repo\n")
    _plant(
        tmp_path,
        "docs/ok.md",
        "A repository, tests/repo1, NotAGitRepoError and "
        "2026-10-06-repo-and-scope-inputs-design.md are fine.\n",
    )
    files = ["src/otto/x.py", "README.md", "docs/ok.md"]
    assert offences(tmp_path, files) == [
        "src/otto/x.py:1: repo",
        "src/otto/x.py:2: sut_dir",
        "README.md:1: OTTO_SUT_DIRS",
    ]
