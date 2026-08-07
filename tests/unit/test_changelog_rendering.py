"""`cliff.toml` renders what a commit subject actually claims.

Conventional Commits marks a breaking change two ways — a `!` after the
type/scope, or a `BREAKING CHANGE:` footer — and both were inert here: the
config mapped `^fix` to "Fixed" and nothing else, so `fix(cli)!:` rendered as
an ordinary bullet indistinguishable from a routine fix, and the scope was
dropped too, leaving a reader unable to tell which subsystem a line was about.

The grouping half is pinned as tightly as the badge, because the two live in
the same file: deleting seven of the twelve `commit_parsers` rows silently
drops 193 of CHANGELOG.md's 649 bullets into the terminal `.*` skip, and a
test naming only three sections would not notice.

Driven through the REAL renderer against a synthetic repo built in `tmp_path`,
not against otto's own history: the point is to pin the template, and a test
reading this repo's log would change its own subject matter every commit.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

from tests._fixtures.paths import PROJECT_ROOT

CLIFF = PROJECT_ROOT / "cliff.toml"

# One commit per shape the config has to tell apart.
COMMITS = [
    "feat(link): add a thing",
    "fix(cli): mend a thing",
    "fix(cli)!: mend a thing in a way that breaks callers",
    "feat(cov)!: a breaking feature\n\nBREAKING CHANGE: store.json v5 -> v6.",
    "docs!: a breaking change with no scope at all",
    "perf(host): make a thing faster",
    "refactor(cov): move a thing",
    "revert(cli): undo a thing",
    "chore(deps): bump a dependency",
    "chore(deps-dev): bump a dev dependency",
    "build(deps): bump a build dependency",
    "chore(repo): tidy a thing",
    "chore(release): bump version 1 -> 2",
    "test(link): a test-only change",
    "ci(gha): a workflow change",
    "style(cli): reflow a thing",
    "build(makefile): a build change with a non-deps scope",
    "Not a conventional commit at all",
]

# type -> the section heading it must land in, per cliff.toml's commit_parsers.
GROUPED = {
    "add a thing": "Added",
    "mend a thing": "Fixed",
    "make a thing faster": "Changed",
    "move a thing": "Changed",
    "undo a thing": "Changed",
    "a breaking change with no scope at all": "Documentation",
    "bump a dependency": "Dependencies",
    "bump a dev dependency": "Dependencies",
    "bump a build dependency": "Dependencies",
    "tidy a thing": "Maintenance",
}

# Everything the config drops. `build(makefile)` is here on purpose: only
# `build(deps` is mapped, so every other `build(...)` falls through to the
# terminal `.*` skip — which is easy to write by accident and impossible to
# notice, since the commit simply never appears.
DROPPED = [
    "bump version 1 -> 2",
    "a test-only change",
    "a workflow change",
    "reflow a thing",
    "a build change with a non-deps scope",
    "Not a conventional commit at all",
]


def _git(repo: Path, *args: str) -> str:
    env = {
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
        "GIT_AUTHOR_DATE": "2026-01-01T00:00:00Z",
        "GIT_COMMITTER_DATE": "2026-01-01T00:00:00Z",
        "PATH": "/usr/bin:/bin",
        # HOME and GIT_CONFIG_SYSTEM both neutered: a developer's global
        # `commit.gpgsign` or an `/etc/gitconfig` `core.hooksPath` would
        # otherwise fail these commits with an opaque CalledProcessError.
        "HOME": str(repo),
        "GIT_CONFIG_SYSTEM": "/dev/null",
        "GIT_CONFIG_GLOBAL": "/dev/null",
    }
    return subprocess.run(
        ["git", *args], cwd=repo, env=env, capture_output=True, text=True, check=True
    ).stdout


@pytest.fixture(scope="module")
def rendered(tmp_path_factory) -> str:
    """CHANGELOG text git-cliff produces for COMMITS under the repo's own config.

    Module-scoped to build the repo once per xdist worker — with `-n auto` the
    tests are distributed, so this really does run several times; the scope is
    a cost reduction, not an isolation claim. The fixture yields an immutable
    string, so no test can affect another.
    """
    assert CLIFF.is_file(), f"cliff.toml not found at {CLIFF} — git-cliff would "
    "silently fall back to its DEFAULT config and every assertion below would "
    "fail with a confusing diff instead of naming the real problem"
    cliff = shutil.which("git-cliff")
    assert cliff, (
        "git-cliff is a declared dev dependency (pyproject [dependency-groups] dev) "
        "and this test drives the real renderer — run `uv sync` rather than skipping"
    )
    repo = tmp_path_factory.mktemp("cliffrepo")
    _git(repo, "init", "-q", "-b", "main")
    # A prior tag is required, not scenery: the footer template reads
    # `releases[0].previous.version` to build the Unreleased compare link, and
    # git-cliff fails the whole render on a repo that has never been tagged.
    (repo / "seed").write_text("x")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "feat: the released past")
    _git(repo, "tag", "v0.1.0")
    for i, subject in enumerate(COMMITS):
        (repo / f"f{i}").write_text("x")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", subject)
    # --offline is not optional. git-cliff fetches api.github.com whenever the
    # config declares a `[remote.*]` section, and PANICS (exit 101, and it
    # writes the reason to a stderr this call used to discard) on any failure
    # — including the routine 403 an unauthenticated caller gets from a
    # rate-limited shared CI egress IP. That is exactly how this test broke
    # CI on three Pythons the day it landed. `cliff.toml` no longer declares
    # one, so this is belt-and-braces: it keeps a re-added `[remote.github]`
    # from silently turning a unit test into a network test.
    proc = subprocess.run(
        [cliff, "--config", str(CLIFF), "--offline", "--unreleased"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    # Not check=True: CalledProcessError's message omits stderr, and stderr is
    # where git-cliff puts the panic. Diagnosing the CI failure above took a
    # long detour for exactly that reason.
    assert proc.returncode == 0, (
        f"git-cliff exited {proc.returncode}\n--- stderr ---\n{proc.stderr}"
    )
    return proc.stdout


def test_a_bang_marks_the_entry_breaking(rendered: str) -> None:
    """The whole point: `fix(cli)!:` must not read as a routine fix."""
    assert "- **BREAKING** **cli**: mend a thing in a way that breaks callers" in rendered


def test_a_breaking_change_footer_also_marks_the_entry(rendered: str) -> None:
    """The other Conventional Commits spelling. otto's history uses both."""
    assert "- **BREAKING** **cov**: a breaking feature\n" in rendered


def test_the_footer_text_itself_is_not_paraphrased_into_the_bullet(rendered: str) -> None:
    """Deliberate, and the deliberate part is what needs pinning.

    git-cliff's `commit.breaking_description` is NOT the footer: it stops at
    the first continuation line shaped like `Token: value` and ignores every
    footer after the first. Rendering it truncated otto's own
    `hosts.json -> lab.json` note mid-sentence (its next line starts
    `{"hosts":`) and dropped two of ad0edab3's three migration notes. A
    half-quoted migration instruction under a **BREAKING** badge is worse
    than a badge that sends the reader to the commit, so the bullet carries
    the subject and nothing else.
    """
    assert "store.json v5 -> v6." not in rendered
    # ...and the badge that replaces it is still there, so this is not just
    # asserting that a feature is absent.
    assert "- **BREAKING** **cov**: a breaking feature\n" in rendered


def test_a_breaking_marker_survives_without_a_scope(rendered: str) -> None:
    """`docs!:` has no scope; the badge must not depend on one being present."""
    assert "- **BREAKING** a breaking change with no scope at all" in rendered


def test_a_non_breaking_entry_carries_its_scope_and_no_badge(rendered: str) -> None:
    """The negative control. Without it every assertion above would still pass
    against a template that stamped **BREAKING** on every line."""
    assert "- **cli**: mend a thing\n" in rendered
    assert "- **link**: add a thing\n" in rendered
    assert rendered.count("**BREAKING**") == 3


def _section_of(rendered: str, subject: str) -> str:
    """The `### <group>` heading the bullet for *subject* sits under."""
    section = ""
    for line in rendered.splitlines():
        if line.startswith("### "):
            section = line[4:].strip()
        elif line.startswith("- ") and line.endswith(subject):
            return section
    raise AssertionError(f"no bullet ends with {subject!r} in:\n{rendered}")


@pytest.mark.parametrize(("subject", "section"), sorted(GROUPED.items()))
def test_every_mapped_type_lands_in_its_section(rendered: str, subject: str, section: str) -> None:
    """Every row of `commit_parsers` that maps, not just the three obvious ones."""
    assert _section_of(rendered, subject) == section


@pytest.mark.parametrize("subject", DROPPED)
def test_every_dropped_type_stays_out(rendered: str, subject: str) -> None:
    """Including the terminal `.*` skip, which is what makes an unmapped type
    (`build(makefile)`, `chore(release)`) vanish rather than land somewhere."""
    assert subject not in rendered


def test_the_config_declares_no_remote() -> None:
    """A `[remote.*]` section makes every render hit api.github.com.

    git-cliff fetches `/commits` and `/pulls` whenever one is configured, and
    turns any non-200 — including the 403 a rate-limited unauthenticated
    caller gets — into a Rust panic rather than an error. That reaches
    `make changelog`, `make release` and the release-notes workflow, none of
    which pass a token, so all three would fail on a cold cache. The template
    writes its GitHub URLs as literal strings and reads no remote context, so
    the section earned nothing; this keeps it from coming back by accident.
    """
    declared = [
        line for line in CLIFF.read_text().splitlines() if line.lstrip().startswith("[remote.")
    ]
    # Line-start, not a substring search: the config explains this decision in
    # a comment that names `[remote.github]`, and a substring check would be
    # satisfied by its own rationale.
    assert not declared, declared
