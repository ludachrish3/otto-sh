"""``scripts/release_events.py``: one event model for the checker, the bump and the changelog.

A release is its marked commits plus its corrections. ``check_breaking_marks``
asks whether a commit is marked, ``release_bump`` names the commits that force
the bump, and git-cliff (through ``cliff.toml``) computes the census and writes
the notes. git-cliff parses commit messages itself, so the model's parse is
pinned against the real binary: the differential below runs ``git-cliff
--context`` over a corpus of message shapes and compares, commit by commit.

The correction tests build throwaway repositories under ``tmp_path`` with
``tests._fixtures.gitrepo.TmpGitRepo`` -- never the dev repo.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from scripts import release_events
from scripts.check_breaking_marks import main as check_main
from scripts.release_bump import main as bump_main
from scripts.release_events import (
    Commit,
    Event,
    footers,
    is_breaking_commit,
    kept_by_cliff,
)
from tests._fixtures.gitrepo import TmpGitRepo
from tests._fixtures.paths import PROJECT_ROOT

pytestmark = pytest.mark.interpreter_agnostic

CLIFF_CONFIG = PROJECT_ROOT / "cliff.toml"
DATE = "2026-01-01T00:00:00Z"
ORIGINAL = "feat(api): drop Beta quietly"


def _cliff() -> str:
    cliff = shutil.which("git-cliff")
    assert cliff, (
        "git-cliff is a declared dev dependency (pyproject [dependency-groups] dev) "
        "and this test drives the real binary -- run `uv sync` rather than skipping"
    )
    return cliff


def _run_cliff(repo: TmpGitRepo, *args: str) -> str:
    proc = subprocess.run(
        [_cliff(), "--config", str(CLIFF_CONFIG), "--offline", *args],
        cwd=repo.root,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, f"git-cliff exited {proc.returncode}\n{proc.stderr}"
    return proc.stdout


def _correction(target: str, original: str = ORIGINAL, header: str = "") -> str:
    """A correction message: marked, footer block last, one ``Corrects:`` footer."""
    header = header or "fix(api)!: record that dropping Beta broke callers"
    return (
        f"{header}\n\nBREAKING CHANGE: Beta is gone; import Alpha.\nCorrects: {target} {original}"
    )


# ---------------------------------------------------------------------------
# The mark: what git-cliff's census counts.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("subject", "body"),
    [
        ("feat!: drop the old flag", ""),
        ("fix(cli)!: rename a flag", ""),
        ("feat(cov): add a thing", "BREAKING CHANGE: store.json v5 -> v6."),
        ("feat(cov): add a thing", "BREAKING-CHANGE: store.json v5 -> v6."),
        ("feat(cov): add a thing", "Some prose.\n\nRefs: 12\nBREAKING CHANGE: later line"),
        ("chore(release)!: a marked release chore", ""),
    ],
)
def test_marked(subject: str, body: str) -> None:
    assert is_breaking_commit(subject, body)


@pytest.mark.parametrize(
    ("subject", "body", "why"),
    [
        ("fix(cli): mend a thing", "", "no mark at all"),
        ("feat(x): a", "The BREAKING CHANGE: here sits in prose.", "prose, not a footer"),
        ("feat(x): a", "Prose line.\nBREAKING CHANGE: second line", "paragraph starts in prose"),
        ("feat()!: empty scope", "", "an empty scope is not conventional"),
        ("test(api)!: a test-only break", "", "cliff.toml skips test"),
        ("ci!: a workflow break", "", "cliff.toml skips ci"),
        ("build!: a build break", "", "cliff.toml maps build(deps only"),
        ("Not conventional", "BREAKING CHANGE: x", "an unconventional header"),
    ],
)
def test_not_marked(subject: str, body: str, why: str) -> None:
    assert not is_breaking_commit(subject, body), why


def test_the_footer_block_starts_at_the_first_footer_paragraph() -> None:
    body = "Prose.\n\nRefs: 12\nmore prose\n\nCorrects: abc subject one\nBREAKING CHANGE: x"
    assert [(f.token, f.value) for f in footers(body)] == [
        ("Refs", "12"),
        ("Corrects", "abc subject one"),
        ("BREAKING CHANGE", "x"),
    ]


def test_a_parser_row_the_model_does_not_mirror_is_refused(tmp_path: Path) -> None:
    config = tmp_path / "cliff.toml"
    config.write_text(
        '[git]\nconventional_commits = true\ncommit_parsers = [{ body = "x", skip = true }]\n'
    )
    with pytest.raises(ValueError, match="does not mirror"):
        kept_by_cliff("feat: a", breaking=False, config=config)


def test_split_commits_is_refused(tmp_path: Path) -> None:
    config = tmp_path / "cliff.toml"
    config.write_text("[git]\nconventional_commits = true\nsplit_commits = true\n")
    with pytest.raises(ValueError, match="unsplit"):
        kept_by_cliff("feat: a", breaking=False, config=config)


# ---------------------------------------------------------------------------
# The differential: the model against the real git-cliff, shape by shape.
# ---------------------------------------------------------------------------

# Each shape is one commit message; "{A}" becomes a real ancestor's sha.
SHAPES = [
    "feat!:nospace",
    "feat-x!: hyphen type",
    "feat_x!: underscore type",
    "fe at!: space in the type",
    "feat(a)(b)!: two scopes",
    "feat(a!): bang inside the scope",
    "feat(a)!:  two spaces",
    "feat(a)!:\ttab after the colon",
    "feat(a-b/c.d)!: punctuation in the scope",
    "feat()!: empty scope",
    "feat!: ",
    "FEAT!: upper-case type",
    "feat(link): add a thing",
    "fix(cli)!: mend a thing in a way that breaks callers",
    "docs!: a breaking change with no scope",
    "test(api)!: a skipped type, marked",
    "ci!: a skipped type, marked",
    "style(x)!: a skipped type, marked",
    "chore(release)!: the release skip is anchored on `chore(release):`",
    "chore(release): bump version 1 -> 2",
    "build(makefile)!: an unmapped build scope",
    "build(deps)!: a mapped build scope",
    "chore(repo)!: maintenance",
    "revert(cli)!: revert",
    "Not a conventional commit at all",
    "Not conventional\n\nBREAKING CHANGE: x",
    "feat(a): prose mention\n\nThis says BREAKING CHANGE: inline.\n\nRefs: 12",
    "feat(a): mid paragraph\n\nProse line.\nBREAKING CHANGE: second line",
    "feat(a): indented\n\n  BREAKING CHANGE: indented",
    "feat(a): lower-case token\n\nBreaking Change: x",
    "feat(a): space before the colon\n\nBREAKING CHANGE : x",
    "feat(a): no space after the colon\n\nBREAKING CHANGE:x",
    "feat(a): hash separator\n\nBREAKING CHANGE #12",
    "feat(a): hyphen spelling\n\nBREAKING-CHANGE: x",
    "feat(a): footer then prose\n\nBREAKING CHANGE: x\n\nprose after",
    "feat(a): refs then a later line\n\nRefs: 12\n\nprose\nBREAKING CHANGE: inside",
    "feat(a): a url starts the block\n\nhttps://example.com\n\nBREAKING CHANGE: real",
    "feat(a): prose after a token paragraph\n\nNote: hello\n\nBREAKING CHANGE is prose: x",
    "fix(api)!: correction\n\nBREAKING CHANGE: x\nCorrects: {A} " + ORIGINAL,
    "fix(api)!: corrects first\n\nCorrects: {A} " + ORIGINAL + "\nBREAKING CHANGE: x",
    "fix(api)!: absorbed prose\n\nCorrects: {A} " + ORIGINAL + "\n\nTrailing prose.",
    "fix(api)!: corrects outside the block\n\nProse.\nCorrects: {A} " + ORIGINAL,
    "fix(api)!: hash separator\n\nCorrects #{A} " + ORIGINAL,
    "fix(api)!: lower-case token\n\ncorrects: {A} " + ORIGINAL,
    "docs!: two corrections\n\nCorrects: {A} " + ORIGINAL + "\nCorrects: {A} again",
    # A type or a footer token is any run of characters other than whitespace,
    # parentheses, `!` and `:`. git-cliff 2.14.2: breaking, breaking, kept
    # unmarked, kept unmarked, breaking.
    "feat.x!: a dotted type",
    "feat(a): a dotted token\n\nSee.also: x\nBREAKING CHANGE: y",
    "feat(a): a parenthesised token\n\n(x): x\nBREAKING CHANGE: y",
    "feat(a): a token with a bang\n\na!b: x\nBREAKING CHANGE: y",
    "feat(a): a starred token and a hash\n\n**Note** #1\nBREAKING CHANGE: y",
    # A footer with no value on its line takes the next non-blank line as its
    # value. git-cliff 2.14.2: kept unmarked, kept unmarked, kept unmarked,
    # breaking; the last reads the Corrects value from the line below.
    "feat(a): an empty value absorbs the next line\n\nRefs:\nBREAKING CHANGE: x",
    "feat(a): prose ending in a colon\n\nprose:\nBREAKING CHANGE: y",
    "feat(a): an empty value skips a blank line\n\nRefs:\n\nBREAKING CHANGE: x",
    "fix(api)!: an empty corrects value\n\nBREAKING CHANGE: x\nCorrects:\n{A} " + ORIGINAL,
    "feat(a): the breaking value on the next line\n\nBREAKING CHANGE:\nthe details",
    # An unconventional message: git-cliff keeps it through `^feat`/`^fix`, with
    # no mark and no footers. A header continued on its second line, or a
    # footer block ending in a footer with no value at all.
    "feat(a)!: header\ncontinued on line two",
    "feat(a): header\ncontinued\n\nBREAKING CHANGE: x",
    "feat(a)!: an empty footer last\n\nRefs:",
    "feat(a): an empty breaking footer\n\nBREAKING CHANGE:",
    "feat(a): an empty hash footer\n\nBREAKING CHANGE #",
    "fix(api)!: a continued header\nsecond line\n\nBREAKING CHANGE: x\nCorrects: {A} " + ORIGINAL,
    "fix(api)!: an empty footer after corrects\n\nBREAKING CHANGE: x\nCorrects: {A} "
    + ORIGINAL
    + "\nNote:",
]

# Committed with `--cleanup=verbatim`, because git's default cleanup would
# blank the second line. git-cliff 2.14.2: unconventional, kept unmarked -- a
# second line holding only whitespace is not a blank one.
VERBATIM_SHAPES = [
    "feat(a)!: a space on the second line\n \nbody",
    "feat(a)!: a tab on the second line\n\t\nbody",
    # Inside the body, a line of only whitespace (Unicode White_Space, so a
    # no-break space too) ends a paragraph, and so does a run of empty lines.
    # git-cliff 2.14.2: breaking, breaking, breaking, breaking.
    "feat(a): a space line ends the prose\n\nprose\n \nBREAKING CHANGE: y",
    "feat(a): a tab line ends the prose\n\nprose\n\t\nBREAKING CHANGE: y",
    "feat(a): a no-break space line ends the prose\n\nprose\n\u00a0\nBREAKING CHANGE: y",
    "feat(a): two empty lines end the prose\n\nprose\n\n\nBREAKING CHANGE: y",
    # A file separator is not White_Space, though Python's str.strip() drops it.
    # git-cliff 2.14.2: kept unmarked.
    "feat(a): a file separator line is text\n\nprose\n\x1c\nBREAKING CHANGE: y",
]


@pytest.fixture(scope="module")
def differential(tmp_path_factory) -> "list[tuple[str, dict | None, Commit]]":
    """Each shape's commit as git-cliff's context records it (None: dropped), and as read."""
    repo = TmpGitRepo(tmp_path_factory.mktemp("shapes"), dates=DATE)
    repo.write("seed", "x")
    repo.commit("feat: the released past")
    repo.git("tag", "v0.1.0")
    repo.write("a", "x")
    target = repo.commit(ORIGINAL)
    shas = []
    for i, shape in enumerate(SHAPES):
        repo.write(f"f{i}", "x")
        shas.append(repo.commit(shape.replace("{A}", target)))
    for i, shape in enumerate(VERBATIM_SHAPES):
        repo.write(f"v{i}", "x")
        repo.git("add", "-A")
        repo.git("commit", "-q", "--cleanup=verbatim", "-m", shape)
        shas.append(repo.git("rev-parse", "HEAD").strip())
    context = json.loads(_run_cliff(repo, "--context", "--unreleased"))
    seen = {c["id"]: c for release in context for c in release["commits"]}
    return [
        (shape, seen.get(sha), release_events.read_commit(repo.root, sha))
        for shape, sha in zip(SHAPES + VERBATIM_SHAPES, shas, strict=True)
    ]


def test_the_model_marks_exactly_what_git_cliff_counts(differential) -> None:
    wrong = [
        shape
        for shape, cliff, commit in differential
        if is_breaking_commit(commit.subject, commit.body, separated=commit.separated)
        != bool(cliff and cliff.get("breaking"))
    ]
    assert wrong == []


def test_the_model_reads_exactly_the_corrects_footers_git_cliff_reads(differential) -> None:
    wrong = [
        shape
        for shape, cliff, commit in differential
        if cliff is not None
        and [f.value for f in release_events.commit_footers(commit) if f.token == "Corrects"]
        != [
            f["value"].split("\n")[0].strip()
            for f in cliff.get("footers") or []
            if f["token"] == "Corrects"
        ]
    ]
    assert wrong == []


def test_the_corpus_reaches_every_outcome(differential) -> None:
    """A differential is only as strong as its shapes: each outcome must occur."""
    kept_marked = [s for s, c, _ in differential if c and c.get("breaking")]
    kept_unmarked = [s for s, c, _ in differential if c and not c.get("breaking")]
    dropped_marked = [s for s, c, _ in differential if c is None and "!:" in s]
    with_corrects = [
        s
        for s, c, _ in differential
        if c and any(f["token"] == "Corrects" for f in c.get("footers") or [])
    ]
    assert len(kept_marked) >= 15
    assert len(kept_unmarked) >= 8
    assert len(dropped_marked) >= 4
    assert len(with_corrects) >= 5


# ---------------------------------------------------------------------------
# Corrections.
# ---------------------------------------------------------------------------


def _shipped(tmp_path: Path) -> "tuple[TmpGitRepo, str]":
    """A repo whose v0.1.1 shipped ORIGINAL unmarked; returns it and ORIGINAL's sha."""
    repo = TmpGitRepo(tmp_path, dates=DATE)
    # release_bump runs git-cliff in the repository, which reads ./cliff.toml.
    repo.write("cliff.toml", CLIFF_CONFIG.read_text())
    repo.commit("feat: the released past")
    repo.git("tag", "v0.1.0")
    repo.write("beta", "gone")
    target = repo.commit(ORIGINAL)
    repo.git("tag", "v0.1.1")
    return repo, target


def test_a_correction_after_a_tag_bumps_the_next_release(tmp_path, monkeypatch, capsys) -> None:
    repo, target = _shipped(tmp_path)
    repo.write("fix", "x")
    correction = repo.commit(_correction(target))

    events = release_events.release_events(repo.root, "v0.1.1..HEAD")
    assert events.refusals == []
    assert [(e.commit.sha, [c.target for c in e.corrections]) for e in events.events] == [
        (correction, [target])
    ]
    assert _run_cliff(repo, "--bumped-version").strip() == "v0.2.0"
    notes = _run_cliff(repo, "--unreleased")
    assert (
        f"  - corrects {ORIGINAL} ({target[:8]}), which broke callers without a mark for it\n"
        in notes
    )

    monkeypatch.setenv("PATH", f"{Path(_cliff()).parent}:/usr/bin:/bin")
    monkeypatch.setenv("BUMP", "patch")
    monkeypatch.delenv("NEW_VERSION", raising=False)
    monkeypatch.setattr(
        "scripts.release_bump.bump_my_version_new_version", lambda bump, cwd=None: "0.1.2"
    )
    assert bump_main(["--cwd", str(repo.root)]) == 1
    err = capsys.readouterr().err
    assert f"corrects {target[:12]} {ORIGINAL}" in err
    assert "BUMP=minor is the floor" in err


def test_a_marked_target_may_still_be_corrected(tmp_path) -> None:
    repo = TmpGitRepo(tmp_path, dates=DATE)
    repo.write("seed", "x")
    repo.commit("feat: the released past")
    repo.write("beta", "gone")
    target = repo.commit("feat(api)!: drop Beta, marked")
    repo.git("tag", "v0.2.0")
    repo.write("fix", "x")
    repo.commit(_correction(target, "feat(api)!: drop Beta, marked"))

    events = release_events.release_events(repo.root, "v0.2.0..HEAD")
    assert events.refusals == []
    assert [c.target for c in events.events[0].corrections] == [target]


def _refusals(repo: TmpGitRepo, rev_range: str) -> "dict[str, list[str]]":
    """Map each refused commit's subject to its reasons."""
    out: dict[str, list[str]] = {}
    for refusal in release_events.release_events(repo.root, rev_range).refusals:
        out.setdefault(refusal.commit.subject, []).append(refusal.reason)
    return out


@pytest.fixture
def refused(tmp_path) -> "dict[str, list[str]]":
    """One history holding every refusal shape, each under its own subject."""
    repo, target = _shipped(tmp_path)
    shapes = [
        _correction(target[:12], header="fix(api)!: a short sha"),
        _correction("f" * 40, header="fix(api)!: an unknown target"),
        _correction(target, "feat(api): drop Beta", header="fix(api)!: a misquoted subject"),
        f"fix(api): an unmarked correction\n\nCorrects: {target} {ORIGINAL}",
        f"test(api)!: a mark the changelog drops\n\nCorrects: {target} {ORIGINAL}",
        f"fix(api)!: a stray line\n\nProse.\nCorrects: {target} {ORIGINAL}",
        (
            f"fix(api)!: the same target twice\n\nCorrects: {target} {ORIGINAL}\n"
            f"Corrects: {target} {ORIGINAL}"
        ),
        _correction(target, header="fix(api)!: the first valid correction"),
        _correction(target, header="fix(api)!: a duplicate"),
    ]
    for i, shape in enumerate(shapes):
        repo.write(f"r{i}", "x")
        repo.commit(shape)
    return _refusals(repo, "v0.1.1..HEAD")


@pytest.mark.parametrize(
    ("subject", "reason"),
    [
        ("fix(api)!: a short sha", "is not `<full 40-hex sha> <original subject>`"),
        ("fix(api)!: an unknown target", "is not a commit in this repository"),
        ("fix(api)!: a misquoted subject", "but that commit's subject is"),
        ("fix(api): an unmarked correction", "must carry a mark git-cliff counts"),
        ("test(api)!: a mark the changelog drops", "must carry a mark git-cliff counts"),
        ("fix(api)!: a stray line", "outside the footer block"),
        ("fix(api)!: the same target twice", "appears twice in this commit"),
        ("fix(api)!: the first valid correction", "is also corrected (breaking) by"),
        ("fix(api)!: a duplicate", "is also corrected (breaking) by"),
    ],
)
def test_every_refusal_case(refused, subject: str, reason: str) -> None:
    assert any(reason in r for r in refused.get(subject, [])), refused.get(subject)


def test_every_shape_in_the_refusal_history_is_refused(refused) -> None:
    """Seven corrections fail on their own; the two valid ones of one target fail together."""
    assert len(refused) == 9


def test_an_invalid_correction_claims_nothing(tmp_path) -> None:
    """A correction refused for its own fault never makes a valid one a duplicate."""
    repo, target = _shipped(tmp_path)
    repo.write("bad", "x")
    bad = repo.commit(_correction(target, "feat(api): drop Beta", header="fix(api)!: misquoted"))
    repo.write("good", "x")
    good = repo.commit(_correction(target, header="fix(api)!: quoted exactly"))

    events = release_events.release_events(repo.root, "v0.1.1..HEAD")

    assert [r.commit.sha for r in events.refusals] == [bad]
    assert [(e.commit.sha, [c.target for c in e.corrections]) for e in events.events] == [
        (bad, []),
        (good, [target]),
    ]


def test_a_correction_brought_in_by_a_merge_counts(tmp_path, capsys) -> None:
    repo, target = _shipped(tmp_path)
    repo.git("checkout", "-q", "-b", "side")
    repo.write("fix", "x")
    correction = repo.commit(_correction(target))
    repo.git("checkout", "-q", "main")
    repo.write("other", "x")
    repo.commit("feat(cli): unrelated work on main")
    repo.git("merge", "--no-ff", "-q", "-m", "chore: merge side", "side")

    assert check_main([f"{target}..HEAD", "--repo", str(repo.root), "--golden", "golden.txt"]) == 0
    assert "check-breaking-marks: OK" in capsys.readouterr().out
    events = release_events.release_events(repo.root, "v0.1.1..HEAD")
    assert [(e.commit.sha, [c.target for c in e.corrections]) for e in events.events] == [
        (correction, [target])
    ]
    assert _run_cliff(repo, "--bumped-version").strip() == "v0.2.0"


def test_a_cherry_picked_correction_whose_target_is_not_an_ancestor_is_refused(
    tmp_path, capsys
) -> None:
    repo, target = _shipped(tmp_path)
    repo.write("fix", "x")
    correction = repo.commit(_correction(target))
    repo.git("checkout", "-q", "-b", "maintenance", "v0.1.0")
    repo.git("cherry-pick", correction)

    exit_code = check_main(["v0.1.0..HEAD", "--repo", str(repo.root), "--golden", "golden.txt"])

    assert exit_code == 1
    out = capsys.readouterr().out
    assert f"Corrects: {target[:12]} is not an ancestor of this commit" in out
    assert "RULE: a correction is a marked commit" in out


def test_a_merge_commit_can_be_corrected(tmp_path) -> None:
    """A merge can carry an unmarked break of its own; its own first line is what is quoted."""
    repo, _ = _shipped(tmp_path)
    repo.git("checkout", "-q", "-b", "side")
    repo.write("side", "x")
    repo.commit("feat(cli): side work")
    repo.git("checkout", "-q", "main")
    repo.git("merge", "--no-ff", "-q", "-m", "chore: merge side, dropping Gamma", "side")
    merge = repo.git("rev-parse", "HEAD").strip()
    repo.git("tag", "v0.1.2")
    repo.write("fix", "x")
    repo.commit(_correction(merge, "chore: merge side, dropping Gamma"))

    events = release_events.release_events(repo.root, "v0.1.2..HEAD")

    assert events.refusals == []
    assert [c.target for c in events.events[0].corrections] == [merge]


def _twice(target: str, *others: str) -> str:
    """The refusal of one claimant of a target that *others* correct too."""
    by = ", ".join(other[:12] for other in others)
    return (
        f"Corrects: {target[:12]} is also corrected (breaking) by {by}; a target is "
        "corrected once per kind, so no correction of it stands"
    )


def test_two_branches_correcting_one_target_are_both_refused(tmp_path) -> None:
    """Neither correction is an ancestor of the other, so neither is "first": both fail."""
    repo, target = _shipped(tmp_path)
    shas = {}
    for branch in ["left", "right"]:
        repo.git("checkout", "-q", "-b", branch, "v0.1.1")
        repo.write(branch, "x")
        shas[branch] = repo.commit(_correction(target, header=f"fix(api)!: correct on {branch}"))
    repo.git("checkout", "-q", "main")
    repo.git("merge", "--no-ff", "-q", "-m", "chore: merge left", "left")
    repo.git("merge", "--no-ff", "-q", "-m", "chore: merge right", "right")

    events = release_events.release_events(repo.root, "v0.1.1..HEAD")

    assert sorted((r.commit.sha, r.reason) for r in events.refusals) == sorted(
        [
            (shas["left"], _twice(target, shas["right"])),
            (shas["right"], _twice(target, shas["left"])),
        ]
    )
    assert sum(len(e.corrections) for e in events.events) == 0


@pytest.mark.parametrize("meet", ["main merged into side", "side merged into main"])
def test_check_time_and_release_time_refuse_the_same_claimants(tmp_path, capsys, meet) -> None:
    """A verdict depends on the claimants in the tip's history, not on parent order.

    C1 lands on a side branch and C2 on main, both correcting one target. C2's
    push is clean: its history holds one claimant. Then the branches meet,
    either the way a pull does (main merged into side, main fast-forwarded,
    so main's line is the second parent) or by merging side into main. The
    meeting's push must be refused, naming C1, and the release must refuse
    C1 too, plus C2, which its range also holds.
    """
    repo, target = _shipped(tmp_path)
    repo.git("checkout", "-q", "-b", "side")
    repo.write("c1", "x")
    c1 = repo.commit(_correction(target, header="fix(api)!: C1 on side"))
    repo.git("checkout", "-q", "main")
    repo.write("c2", "x")
    c2 = repo.commit(_correction(target, header="fix(api)!: C2 on main"))
    assert release_events.release_events(repo.root, f"{target}..{c2}").refusals == []
    if meet == "main merged into side":
        repo.git("checkout", "-q", "side")
        repo.git("merge", "--no-ff", "-q", "-m", "chore: merge main into side", "main")
        repo.git("checkout", "-q", "main")
        repo.git("merge", "--ff-only", "-q", "side")
    else:
        repo.git("merge", "--no-ff", "-q", "-m", "chore: merge side into main", "side")

    pushed = release_events.release_events(repo.root, f"{c2}..HEAD")
    released = release_events.release_events(repo.root, "v0.1.1..HEAD")

    assert [(r.commit.sha, r.reason) for r in pushed.refusals] == [(c1, _twice(target, c2))]
    assert sorted((r.commit.sha, r.reason) for r in released.refusals) == sorted(
        [(c1, _twice(target, c2)), (c2, _twice(target, c1))]
    )
    assert check_main([f"{c2}..HEAD", "--repo", str(repo.root), "--golden", "golden.txt"]) == 1
    assert _twice(target, c2) in capsys.readouterr().out


def test_every_claimant_of_a_target_corrected_twice_is_refused_in_any_range(tmp_path) -> None:
    """A commit's verdict is the same whichever range holds it.

    A corrects X, B corrects X and Y, C corrects Y. X and Y each have two
    claimants, so A, B and C are all refused, and C is refused the same way
    in a range that starts after B.
    """
    repo, x = _shipped(tmp_path)
    gamma = "feat(api): drop Gamma quietly"
    repo.write("gamma", "gone")
    y = repo.commit(gamma)
    repo.git("tag", "v0.1.2")
    repo.write("a", "x")
    a = repo.commit(_correction(x, header="fix(api)!: A corrects X"))
    repo.write("b", "x")
    b = repo.commit(
        "fix(api)!: B corrects X and Y\n\nBREAKING CHANGE: Beta and Gamma are gone.\n"
        f"Corrects: {x} {ORIGINAL}\nCorrects: {y} {gamma}"
    )
    repo.write("c", "x")
    c = repo.commit(_correction(y, gamma, header="fix(api)!: C corrects Y"))

    whole = release_events.release_events(repo.root, "v0.1.2..HEAD")
    after_b = release_events.release_events(repo.root, f"{b}..HEAD")

    assert [(r.commit.sha, r.reason) for r in whole.refusals] == [
        (a, _twice(x, b)),
        (b, _twice(x, a)),
        (b, _twice(y, c)),
        (c, _twice(y, b)),
    ]
    assert [(r.commit.sha, r.reason) for r in after_b.refusals] == [(c, _twice(y, b))]
    for events in [whole, after_b]:
        assert [cl for e in events.events for cl in e.corrections] == []


def test_describe_names_each_corrected_commit_under_the_event() -> None:
    commit = Commit("a" * 40, "fix(api)!: record it", "")
    correction = release_events.Correction(commit.sha, "b" * 40, ORIGINAL)
    assert Event(commit, [correction]).describe() == (
        f"{'a' * 12} fix(api)!: record it\n    corrects {'b' * 12} {ORIGINAL}"
    )
