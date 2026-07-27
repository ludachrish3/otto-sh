"""`git blame` is the oracle for the replay engine, never the engine itself."""

import subprocess
from pathlib import Path

from otto.coverage.attribution import attribute_lines

_GIT_ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@x",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@x",
    "PATH": "/usr/bin:/bin",
}
"""Hermetic git identity/env, mirroring ``tests/unit/cov/test_pipeline.py``.

Every subprocess call in this file passes ``env={**_GIT_ENV, "HOME":
str(tmp_path)}`` so a host-level ``~/.gitconfig`` setting — most notably
``commit.gpgsign = true``, which made these three tests fail while every
sibling suite (built the same way but already isolated) survived — can
never reach the throwaway repos these tests build. Redirecting ``HOME``
means there is no ``~/.gitconfig`` file to read at all, rather than trying
to override every setting a real one might contain.
"""


def _git(repo: Path, tmp_path: Path, *args: str) -> subprocess.CompletedProcess:
    """Run one git command against *repo*, isolated from the host's git config."""
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        env={**_GIT_ENV, "HOME": str(tmp_path)},
    )


def _blame_shas(repo: Path, tmp_path: Path, relpath: str, line_count: int) -> dict[int, str]:
    out = _git(repo, tmp_path, "blame", "-w", "-M", "--porcelain", "--", relpath).stdout
    # Porcelain emits one header line per hunk (`<40-hex sha> <orig> <final>
    # <nlines>`) followed by that many detail lines with no sha of their
    # own — only the first token of the header line is 40 hex chars, so
    # gating on that length (rather than "any line with >=3 tokens") is what
    # keeps a hunk's later lines from being misread as fresh headers.
    shas: dict[int, str] = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 3 and len(parts[0]) == 40 and parts[0].isalnum():
            try:
                shas[int(parts[2])] = parts[0]
            except ValueError:
                continue
    return {n: shas[n] for n in range(1, line_count + 1) if n in shas}


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "oracle"
    repo.mkdir()
    _git(repo, tmp_path, "init", "-q")
    _git(repo, tmp_path, "config", "user.email", "t@example.com")
    _git(repo, tmp_path, "config", "user.name", "t")
    return repo


def _commit(repo: Path, tmp_path: Path, message: str) -> None:
    _git(repo, tmp_path, "add", "-A")
    _git(repo, tmp_path, "commit", "-q", "-m", message)


def test_replay_matches_blame_on_a_linear_history(tmp_path):
    repo = _repo(tmp_path)
    body = [f"line {i}\n" for i in range(30)]
    (repo / "a.c").write_text("".join(body))
    _commit(repo, tmp_path, "seed")

    for step, idx in enumerate((3, 17, 4, 28, 11)):
        body[idx] = f"edited at step {step}\n"
        (repo / "a.c").write_text("".join(body))
        _commit(repo, tmp_path, f"edit {step}")

    body.insert(10, "inserted\n")
    (repo / "a.c").write_text("".join(body))
    _commit(repo, tmp_path, "insert")

    count = len(body)
    assert attribute_lines(repo, {"a.c": count})["a.c"] == _blame_shas(repo, tmp_path, "a.c", count)


def test_replay_matches_blame_across_a_rename(tmp_path):
    repo = _repo(tmp_path)
    body = [f"line {i}\n" for i in range(25)]
    (repo / "old.c").write_text("".join(body))
    _commit(repo, tmp_path, "seed")
    _git(repo, tmp_path, "mv", "old.c", "new.c")
    _commit(repo, tmp_path, "rename")
    body[5] = "post-rename edit\n"
    (repo / "new.c").write_text("".join(body))
    _commit(repo, tmp_path, "edit after rename")

    count = len(body)
    assert attribute_lines(repo, {"new.c": count})["new.c"] == _blame_shas(
        repo, tmp_path, "new.c", count
    )


def test_first_parent_attributes_to_the_merge_not_the_topic_commit(tmp_path):
    """The documented, intended divergence from blame (spec §2).

    ``attribute_lines`` walks ``--first-parent`` by design: a line brought
    onto the mainline by a merge is credited to the *merge* commit (which
    carries the PR/ticket reference), not the topic commit that originally
    wrote it (often just "wip"). ``git blame`` has no such notion and
    always answers with the topic commit, so this test pins the intended
    divergence rather than the oracle-equality the other two tests assert.
    """
    repo = _repo(tmp_path)
    (repo / "a.c").write_text("base\n")
    _commit(repo, tmp_path, "seed")

    # Capture the real default branch name (main/master/otherwise) instead
    # of guessing it — HEAD is unborn until the first commit exists, so this
    # must happen after "seed", before the topic branch is cut.
    trunk = _git(repo, tmp_path, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()

    _git(repo, tmp_path, "checkout", "-q", "-b", "topic")
    (repo / "a.c").write_text("base\ntopic line\n")
    _commit(repo, tmp_path, "wip")
    _git(repo, tmp_path, "checkout", "-q", trunk)
    _git(repo, tmp_path, "merge", "--no-ff", "-q", "-m", "Merge PR PROJ-9", "topic")

    merge_sha = _git(repo, tmp_path, "rev-parse", "HEAD").stdout.strip()

    got = attribute_lines(repo, {"a.c": 2})

    assert got["a.c"][2] == merge_sha
    assert got["a.c"][2] != _blame_shas(repo, tmp_path, "a.c", 2)[2]
