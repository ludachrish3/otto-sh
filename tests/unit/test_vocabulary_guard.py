"""The old project words cannot come back.

Spec ``docs/superpowers/specs/2026-10-17-649-project-vocabulary-design.md``
(Guards). ``OTTO_SUT_DIRS`` is refused in every tracked file except the
historical records and the negative inventories that must spell it
(``EXEMPT_PREFIXES``). The old words (``repo`` and its forms, ``sut_dir`` /
``sut_dirs``) are refused in ``src/``, ``docs/`` and ``tests/``, as a whole word
or as one component of a name; the exact match is ``OLD_WORD``'s docstring in
``tests/_fixtures/vocabulary.py``. The word scan also skips the paths in
``WORD_ONLY_EXEMPT``: ``tests/unit/scripts/`` (it tests ``scripts/``, which is
out of scope and says "repo" in git's sense, as its tests do), the dated
import-budget measurement records and the guard's own pattern module. Those
paths are not exempt from ``OTTO_SUT_DIRS``, which is refused there too. Every
other exemption is by an exact line fragment (``GIT_SENSE``, ``KEPT_WORDS``) or
by an exact whole line (``REJECTION_INVENTORY``); the three tables live in
``tests/_fixtures/vocabulary.py``, shared with the prose check and the rename
script's residual inventory, and each entry says why.
"""

import re
import subprocess
from pathlib import Path

import pytest

from tests._fixtures import vocabulary
from tests._fixtures.gitrepo import git_env
from tests._fixtures.paths import CHECKOUT_ROOT
from tests._fixtures.vocabulary import (
    GIT_SENSE,
    KEPT_WORDS,
    OLD_WORD,
    REJECTION_INVENTORY,
    allowed,
    searchable,
)

OLD_VARIABLE = re.compile(r"\bOTTO_SUT_DIRS\b")
WORD_SCOPE = ("src/", "docs/", "tests/")

EXEMPT_PREFIXES = [
    "docs/superpowers/",  # historical specs and plans keep the words of their day
    "CHANGELOG.md",  # generated from commit subjects; existing entries are history
    "tests/unit/test_moved_name_spellings.py",  # negative inventory: spells gone modules
    "tests/unit/test_vocabulary_renames.py",  # negative inventory: spells the old names
    "tests/unit/config/test_ottopath_env.py",  # pins that OTTO_SUT_DIRS does nothing
    "tests/unit/test_vocabulary_guard.py",  # this guard
]
"""Paths exempt from both scans: each must spell ``OTTO_SUT_DIRS`` or keeps history."""

WORD_ONLY_EXEMPT = [
    "tests/unit/scripts/",  # tests of scripts/, which is out of scope: git's "repo", as there
    "tests/unit/import_budget/measurements/",  # dated measurement records
    "tests/_fixtures/vocabulary.py",  # the guard's own pattern and tables
]
"""Paths exempt from the word scan only; ``OTTO_SUT_DIRS`` is still refused in them."""


def _exempt(rel: str, prefixes: list[str]) -> bool:
    return any(rel == p or rel.startswith(p) for p in prefixes)


def offences(root: Path, files: list[str]) -> list[str]:
    """``path:line: word`` for every old word in *files* (paths relative to *root*)."""
    found = []
    for rel in files:
        if _exempt(rel, EXEMPT_PREFIXES):
            continue
        try:
            text = (root / rel).read_text(encoding="utf-8")
        except (UnicodeDecodeError, IsADirectoryError, FileNotFoundError):
            continue
        in_scope = rel.startswith(WORD_SCOPE) and not _exempt(rel, WORD_ONLY_EXEMPT)
        for lineno, line in enumerate(text.splitlines(), 1):
            found.extend(f"{rel}:{lineno}: {m.group(0)}" for m in OLD_VARIABLE.finditer(line))
            if not in_scope or allowed(rel, line):
                continue
            words = OLD_WORD.finditer(searchable(line))
            found.extend(f"{rel}:{lineno}: {m.group(0)}" for m in words)
    return found


def stale_exemptions(root: Path) -> list[str]:
    """Each git-sense or kept fragment no longer found, and each rejection line not found
    exactly once."""
    stale = [
        f"{rel}: {fragment!r}"
        for table in (GIT_SENSE, KEPT_WORDS)
        for rel, fragments in table.items()
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
        cwd=CHECKOUT_ROOT,
        env=git_env(home),
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return [p for p in out.split("\0") if p]


def test_no_old_word_in_the_tree(tmp_path):
    tracked = _tracked(tmp_path)
    # Not vacuous: the scan really walks the tree, src/, docs/ and tests/ included.
    assert "src/otto/utils.py" in tracked
    assert "docs/overview.md" in tracked
    assert "tests/_fixtures/paths.py" in tracked
    found = offences(CHECKOUT_ROOT, tracked)
    assert found == [], "\n".join(found)


def test_every_exemption_still_matches_exactly():
    assert stale_exemptions(CHECKOUT_ROOT) == []


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
            [f"{REPORTER}:1: repo", f"{REPORTER}:1: sut_dir", f"{REPORTER}:1: sut_dir"],
        ),
        (f"# {REJECTION_LINE}\n", [f"{REPORTER}:1: repo", f"{REPORTER}:1: sut_dir"]),
        (
            f"{REJECTION_LINE}  # the repo\n",
            [f"{REPORTER}:1: repo", f"{REPORTER}:1: sut_dir", f"{REPORTER}:1: repo"],
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
    """Write every file the three tables name, each exemption present exactly once.

    Generated from the tables themselves, so an entry added later (a docs
    page's rejection lines, a git-sense fragment) is seeded too.
    """
    bodies: dict[str, list[str]] = {}
    for table in (REJECTION_INVENTORY, GIT_SENSE, KEPT_WORDS):
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


@pytest.mark.parametrize(
    ("text", "words"),
    [
        ("fake_repo(name)", ["repo"]),
        ("def repo_marker():", ["repo"]),
        ('name = "repo-a"', ["repo"]),
        ("class MockRepo(Project):", ["Repo"]),
        ("#     class RepoActions(ProjectActions):", ["Repo"]),
        ("_default_sut_dirs_env()", ["sut_dirs"]),
        ("sut_dir1 = 1", ["sut_dir"]),
        ("the repos", ["repos"]),
        ("a repository, a report, REPORT, project1, repo1", []),
        ("NotAGitRepoError, TmpGitRepo, git_repo, gitrepo", []),
        ("see 2026-10-06-repo-and-scope-inputs-design.md", []),
        ("Report, Repository, MockReporter", []),
        ("git_repo and the repo", ["repo"]),
    ],
    ids=[
        "snake",
        "fixture-name",
        "kebab",
        "camel-suffix",
        "camel-prefix",
        "sut-dirs",
        "sut-dir-numbered",
        "plural",
        "lookalikes",
        "git-tokens",
        "cited-spec",
        "report-lookalikes",
        "git-token-stripped-first",
    ],
)
def test_the_word_pattern_fires_on_a_component_and_not_on_a_lookalike(tmp_path, text, words):
    """Red before the fix: the whole-word pattern missed six of the first seven (all but
    ``repo-a``)."""
    _plant(tmp_path, "src/otto/x.py", f"{text}\n")
    assert offences(tmp_path, ["src/otto/x.py"]) == [f"src/otto/x.py:1: {w}" for w in words]


def test_the_test_tree_is_in_scope_and_its_scripts_tests_are_not(tmp_path):
    """Red before the fix: the word scope was src/ and docs/ only."""
    _plant(tmp_path, "tests/unit/cli/test_x.py", "def test_x(repo):\n")
    _plant(tmp_path, "tests/unit/scripts/test_y.py", "def test_y(repo):\n")
    files = ["tests/unit/cli/test_x.py", "tests/unit/scripts/test_y.py"]
    assert offences(tmp_path, files) == ["tests/unit/cli/test_x.py:1: repo"]


@pytest.mark.parametrize("rel", WORD_ONLY_EXEMPT)
def test_the_old_variable_is_refused_under_a_word_only_exemption(tmp_path, rel):
    """Red before the fix: a word-scan exemption skipped the whole file, the
    ``OTTO_SUT_DIRS`` check included."""
    planted = rel if rel.endswith(".py") else f"{rel}x.py"
    _plant(tmp_path, planted, "the repo\nexport OTTO_SUT_DIRS=/x\n")
    assert offences(tmp_path, [planted]) == [f"{planted}:2: OTTO_SUT_DIRS"]
