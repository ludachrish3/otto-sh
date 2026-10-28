"""The old project words, and the lines allowed to keep one.

Spec ``docs/superpowers/specs/2026-10-17-649-project-vocabulary-design.md``
(Guards). One home for what every check of the vocabulary shares: the word
pattern (``OLD_WORD``: ``repo`` as a whole word or as one component of a name),
the git-sense tokens removed from a line before it is searched (``GIT_TOKEN``),
the cited spec's name, and three tables of lines allowed to keep an old word:
``GIT_SENSE`` (git's sense of the word), ``KEPT_WORDS`` (any other outside
sense) and ``REJECTION_INVENTORY`` (lines that spell an old word in order to
refuse it). The vocabulary guard (``tests/unit/test_vocabulary_guard.py``), the
per-file prose check below and the rename script's residual inventory all read
them, so the lines they allow cannot drift apart.

Run as a module to list the old words left in some files, allowed lines aside::

    uv run --no-sync python -m tests._fixtures.vocabulary src/otto/coverage/reporter.py
"""

import re
import sys
from pathlib import Path

OLD_WORD = re.compile(
    r"(?<![A-Za-z0-9])(?:repo|repos|Repo|Repos)(?![a-z0-9])"
    r"|(?<=[a-z0-9])(?:Repo|Repos)(?![a-z0-9])"
    r"|(?<![A-Za-z0-9])sut_dirs?(?![a-z])"
)
"""``repo`` as a whole word or one component of a name: snake (``fake_repo``),
kebab (``repo-a``), dotted, or CamelCase (``MockRepo``, ``RepoActions``); and
``sut_dir`` / ``sut_dirs`` as a component, numbered too (``sut_dir1``). Never
``repository``, ``report``, ``repo1``, ``project1`` or upper case."""

GIT_TOKEN = re.compile(r"[A-Za-z0-9_]*(?:[Gg]it_?[Rr]epos?|GIT_?REPOS?)[A-Za-z0-9_]*")
"""A name in git's sense (``git_repo``, ``TmpGitRepo``, ``NotAGitRepoError``),
removed from a line before the line is searched."""

CITED_SPEC = "2026-10-06-repo-and-scope-inputs-design"
"""A spec's file name that docstrings and docs cite; a citation is history, so it is
removed from a line before the line is searched."""


def searchable(line: str) -> str:
    """*line* as ``OLD_WORD`` searches it: the cited spec and every git-sense token removed."""
    return GIT_TOKEN.sub("", line.replace(CITED_SPEC, ""))


GIT_SENSE: dict[str, list[str]] = {
    # path -> exact fragments of lines that use "repo" in git's sense. A line
    # containing one is allowed. Each fragment must still occur, so a stale
    # entry fails too. The guard's word scope is src/, docs/ and tests/, so it
    # applies only those entries (yet checks that every entry still occurs).
    # The entries outside that scope are a SAMPLE, not an inventory: the
    # git-sense lines the rename's residual pass kept and named, while
    # scripts/, noxfile.py and the rest carry many more git-sense "repo"s
    # nobody lists.
    # otto's own checkout: the worktree rule names its root.
    "AGENTS.md": ["treat the main checkout at the repo root as READ-ONLY"],
    # otto's own checkout: upstream drift cannot change its bytes.
    "Makefile": ["upstream drift cannot change a byte this repo"],
    # otto's own checkout: the <repo> placeholder names it, and the scrub orders
    # its root against the examples root.
    "scripts/refresh_docs_captures.py": [
        "# root before the repo root (an examples root under ROOT is a",
        "# substring of it and must win), and the repo root before the",
        '(re.escape(str(ROOT)), "<repo>"),',
    ],
    # otto's own checkout: the child's cwd, the script's sys.path and a
    # failure message's relative path.
    "scripts/import_budget.py": [
        "the caller's cwd handed the child the repo root, which every other test",
        "# repo root on the path. Done lazily, here, so merely importing this module",
        "repo-relative when it is inside the repo.",
    ],
    # stand-ins for scripts/api_regen.py's generate_worktree(repo, ...) and
    # scripts/api_agreement.py's namespace_reports(namespaces, repo): a git
    # checkout, spelled as the real signatures spell it (scripts/ is out of scope).
    "tests/unit/api_snapshot/test_public_api_snapshot.py": [
        'lambda repo, manifest, assume_dir=False, seed="0"',
        "def broken(namespaces, repo):",
        "def spy(repo, manifest, assume_dir=False, seed=api_regen.GATE_HASH_SEED):",
    ],
    # scripts/docs_build_stamp.py's build_stamp(repo=...) keyword: the git
    # checkout the stamp reads its tags from.
    "tests/unit/test_docs_build_stamp.py": ["repo=_make_git_repo(", "repo=tmp_path"],
}

KEPT_WORDS: dict[str, list[str]] = {
    # path -> exact fragments of lines that spell "repo" in an outside sense that
    # is not git's: a third party's file, flag or message this tree cannot rename.
    # Same shape and stale-entry check as GIT_SENSE.
    # yum's own repository file, which the CentOS 7 image edits in place.
    "tests/project2/docker/oldos/Dockerfile": ["/etc/yum.repos.d/CentOS-Base.repo"],
    # the Makefile's gate-tip recipe, asserted byte for byte: it passes
    # scripts/gate_tip.py's own --repo flag (scripts/ is out of scope).
    "tests/unit/test_make_release_lanes.py": ["scripts/gate_tip.py --repo ."],
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
    # coverage metadata from an older otto: the reader must refuse these keys,
    # and its refusal names the stale one
    "tests/unit/cov/test_cov_meta_refusal.py": [
        'OLD = {"repo_name": "fw", "sut_dir": "/src/fw"}',
        "assert \"'repo_name'\" in message",
        "assert \"'repo_name'\" in str(excinfo.value)",
    ],
    # coverage metadata from an older otto: otto cov's reader must refuse these
    # keys and name the stale one
    "tests/unit/cli/test_cov.py": [
        '{"repo_name": "fw", "sut_dir": "/src/fw", "toolchains": {}, "source_roots": {}}',
        "assert \"'repo_name'\" in out",
    ],
    # the removed ${sut_dir} settings token: a tier's harvest_dirs keep it as a
    # literal path segment
    "tests/unit/cov/test_tiers.py": [
        '"unit": {"kind": "unit", "precedence": 1, "harvest_dirs": ["${sut_dir}/build"]},',
        'assert unit.harvest_dirs == [Path("${sut_dir}/build")]',
    ],
    # the removed ${sut_dir} settings token: a project's libs keep it as a literal
    # path segment
    "tests/unit/config/test_settings_path_anchoring.py": [
        '"""``${sut_dir}`` is gone: it survives parsing as a literal path segment."""',
        'root = _write_project(tmp_path / "proj", \'libs = ["${sut_dir}/pylib"]\')',
        'assert project.libs == [root / "${sut_dir}" / "pylib"]',
    ],
    # the retired docker CLI helpers must be gone from otto.cli.docker, and the
    # removed --repo flag is refused
    "tests/unit/docker/test_cli.py": [
        '"_select_repos",',
        '"_resolve_parent_for_repo",',
        (
            'result = DispatchRunner().invoke(docker_app, ["build", "--repo", "a"], '
            'spec_name="docker")'
        ),
        'assert "No such option: --repo" in " ".join(result.output.split())',
    ],
    # the direct-construction refusal must not point at the retired for_repo API
    "tests/unit/project/test_actions.py": ['assert "for_repo" not in message'],
    # the scaffold templates no longer spell the old fixture name
    "tests/unit/init/test_init_scaffold.py": [
        'assert "repo_marker" not in CONFTEST_TEMPLATE + TEST_EXAMPLE_TEMPLATE'
    ],
    # the root help no longer spells the old word
    "tests/unit/cli/test_help_text_user_facing.py": ['assert "repo" not in DESCRIPTION'],
    # the removed get_repos spelling must still raise ImportError
    "tests/unit/test_lab_facade.py": ['for name in ["get_lab", "get_repos"]:'],
}


def rejection_exempt(rel: str, line: str) -> bool:
    """True when *line* of *rel* is, whole and stripped, one of its rejection lines."""
    return line.strip() in REJECTION_INVENTORY.get(rel, [])


def allowed(rel: str, line: str) -> bool:
    """True when *line* of *rel* may keep an old word: a rejection, git-sense or kept line."""
    return rejection_exempt(rel, line) or any(
        f in line for table in (GIT_SENSE, KEPT_WORDS) for f in table.get(rel, [])
    )


def old_words(root: Path, rel: str) -> list[str]:
    """``path:line: text`` for each line of *rel* that spells an old word, allowed lines aside."""
    text = (root / rel).read_text(encoding="utf-8")
    return [
        f"{rel}:{n}: {line.strip()}"
        for n, line in enumerate(text.splitlines(), 1)
        if OLD_WORD.search(searchable(line)) and not allowed(rel, line)
    ]


def main(argv: list[str]) -> int:
    """Print every old word left in the files named in *argv*; exit 1 if any."""
    found = [hit for rel in argv for hit in old_words(Path.cwd(), rel)]
    if found:
        sys.stdout.write("\n".join(found) + "\n")
    return 1 if found else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
