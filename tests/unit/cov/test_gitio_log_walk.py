import subprocess
from pathlib import Path

from otto.coverage.capture import gitio


def _init(tmp_path: Path) -> Path:
    repo = tmp_path / "r"
    repo.mkdir()
    for args in (
        ["init", "-q"],
        ["config", "user.email", "t@example.com"],
        ["config", "user.name", "t"],
    ):
        subprocess.run(["git", *args], cwd=repo, check=True)
    return repo


def _commit(repo: Path, message: str) -> None:
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", message], cwd=repo, check=True)


def test_walk_is_newest_first_and_carries_subject_and_body(tmp_path):
    repo = _init(tmp_path)
    (repo / "a.c").write_text("one\n")
    _commit(repo, "first PROJ-1")
    (repo / "a.c").write_text("one\ntwo\n")
    _commit(repo, "second PROJ-2\n\nbody line")

    walk = gitio.log_walk_u0(repo, ["a.c"])

    assert [c.subject for c in walk] == ["second PROJ-2", "first PROJ-1"]
    assert "body line" in walk[0].body
    assert walk[0].sha != walk[1].sha


def test_diff_text_is_u0_and_parseable(tmp_path):
    repo = _init(tmp_path)
    (repo / "a.c").write_text("one\n")
    _commit(repo, "c1")
    (repo / "a.c").write_text("one\ntwo\n")
    _commit(repo, "c2")

    walk = gitio.log_walk_u0(repo, ["a.c"])

    assert "@@ -1,0 +2 @@" in walk[0].diff_text or "@@ -1 +1,2 @@" in walk[0].diff_text
    assert "diff --git" in walk[0].diff_text


def test_source_containing_delimiters_does_not_split_records(tmp_path):
    """Source files with \\x1e/\\x1f preserve diff_text (these appear in diffs, not metadata)."""
    repo = _init(tmp_path)
    (repo / "a.c").write_text("char sep = '\x1e';\nchar unit = '\x1f';\n")
    _commit(repo, "c1")
    (repo / "a.c").write_text("char sep = '\x1e';\nchar unit = '\x1f';\nchar newline = '\\n';\n")
    _commit(repo, "c2")

    walk = gitio.log_walk_u0(repo, ["a.c"])

    assert len(walk) == 2
    assert walk[0].subject == "c2"
    assert walk[1].subject == "c1"
    assert "@@" in walk[0].diff_text
    assert "+char newline" in walk[0].diff_text


def test_commit_body_with_delimiters_does_not_fabricate_records(tmp_path):
    """Commit body with control chars must not create phantom records or drop commits.

    CRITICAL REGRESSION: Single-call parser fails when body contains \\n\\x1e A\\x1fB\\x1fC\\x1fD.
    This test uses the two-call NUL-delimited approach to safely handle arbitrary body bytes.
    """
    repo = _init(tmp_path)
    (repo / "a.c").write_text("line1\n")
    _commit(repo, "c1")

    # Create commit with control chars in BODY (via file to handle binary data)
    (repo / "a.c").write_text("line1\nline2\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    # Use -F to pass message via file (allows control chars)
    msg_file = repo / ".git_msg"
    msg_file.write_bytes(b"c2\n\nbody with control\n\x1e A\x1fB\x1fC\x1fD")
    subprocess.run(["git", "commit", "-q", "-F", str(msg_file)], cwd=repo, check=True)
    msg_file.unlink()

    walk = gitio.log_walk_u0(repo, ["a.c"])

    # CRITICAL: Must have exactly 2 commits (no phantom, no drops)
    assert len(walk) == 2, f"Expected 2 commits, got {len(walk)}: {[c.subject for c in walk]}"
    # CRITICAL: Both commits reachable
    subjects = [c.subject for c in walk]
    assert "c1" in subjects
    assert "c2" in subjects

    # CRITICAL: Body with control chars must survive intact
    c2 = next((c for c in walk if c.subject == "c2"), None)
    assert c2 is not None
    assert "\x1e" in c2.body, "Body's \\x1e lost"
    assert "A\x1fB\x1fC\x1fD" in c2.body, "Body's delimiter sequence lost"


def test_whitespace_only_change_produces_no_hunks(tmp_path):
    """-w is what keeps a reindent from re-attributing a line."""
    repo = _init(tmp_path)
    (repo / "a.c").write_text("int x;\n")
    _commit(repo, "c1")
    (repo / "a.c").write_text("    int x;\n")
    _commit(repo, "c2")

    walk = gitio.log_walk_u0(repo, ["a.c"])

    assert "@@" not in walk[0].diff_text


def test_empty_relpaths_returns_empty(tmp_path):
    repo = _init(tmp_path)
    (repo / "a.c").write_text("x\n")
    _commit(repo, "c1")
    assert gitio.log_walk_u0(repo, []) == []


def test_walk_with_first_parent_false(tmp_path):
    """first_parent=False parameter is accepted and produces results."""
    repo = _init(tmp_path)
    (repo / "a.c").write_text("line1\n")
    _commit(repo, "c1")
    (repo / "a.c").write_text("line1\nline2\n")
    _commit(repo, "c2")

    # Both variants should work and produce commit diffs
    walk_fp_true = gitio.log_walk_u0(repo, ["a.c"], first_parent=True)
    walk_fp_false = gitio.log_walk_u0(repo, ["a.c"], first_parent=False)

    # Both should find the commits
    assert len(walk_fp_true) >= 1
    assert len(walk_fp_false) >= 1
    # Both should have diff text (proving git ran)
    assert any(c.diff_text.strip() for c in walk_fp_true)
    assert any(c.diff_text.strip() for c in walk_fp_false)


def test_walk_follows_renames_with_m_flag(tmp_path):
    """The -M flag is enabled in log_walk_u0; verify rename handling works."""
    repo = _init(tmp_path)
    # Create a file and modify it
    (repo / "old.c").write_text("line1\n")
    _commit(repo, "create old.c")
    (repo / "old.c").write_text("line1\nline2\n")
    _commit(repo, "modify old.c")
    # Rename the file
    subprocess.run(["git", "mv", "old.c", "new.c"], cwd=repo, check=True)
    _commit(repo, "rename old.c to new.c")
    # Modify the renamed file
    (repo / "new.c").write_text("line1\nline2\nline3\n")
    _commit(repo, "modify new.c")

    # Query only the new name; with -M we should see commits for this file (renamed or not)
    walk = gitio.log_walk_u0(repo, ["new.c"], first_parent=True)
    subjects = [c.subject for c in walk]
    # Must have at least the modifications of the new file
    assert len(walk) >= 2
    assert "modify new.c" in subjects
    assert "rename old.c to new.c" in subjects


def test_name_status_walk_u0_is_unrestricted_and_finds_renames(tmp_path):
    """A commit that renames old.c -> new.c must show up as an R record even
    though the walk takes no pathspec at all — that's the whole point: the
    discovery pass has no path list yet to restrict itself to.
    """
    repo = _init(tmp_path)
    (repo / "old.c").write_text("line1\n")
    _commit(repo, "create old.c")
    subprocess.run(["git", "mv", "old.c", "new.c"], cwd=repo, check=True)
    _commit(repo, "rename old.c to new.c")

    blocks = gitio.name_status_walk_u0(repo)

    assert len(blocks) == 2
    assert any("R100\told.c\tnew.c" in b for b in blocks)


def test_name_status_walk_u0_newest_first(tmp_path):
    repo = _init(tmp_path)
    (repo / "a.c").write_text("one\n")
    _commit(repo, "first")
    (repo / "a.c").write_text("one\ntwo\n")
    _commit(repo, "second")

    blocks = gitio.name_status_walk_u0(repo)

    assert len(blocks) == 2
    assert "A\ta.c" in blocks[1]
    assert "M\ta.c" in blocks[0]


def test_name_status_walk_u0_first_parent_false_is_accepted(tmp_path):
    repo = _init(tmp_path)
    (repo / "a.c").write_text("x\n")
    _commit(repo, "c1")

    blocks_true = gitio.name_status_walk_u0(repo, first_parent=True)
    blocks_false = gitio.name_status_walk_u0(repo, first_parent=False)

    assert len(blocks_true) == 1
    assert len(blocks_false) == 1


def test_diff_worktree_u0_returns_empty_string_for_empty_relpaths(tmp_path):
    """diff_worktree_u0 with empty relpaths must return empty string (no git spawn)."""
    repo = _init(tmp_path)
    (repo / "a.c").write_text("x\n")
    _commit(repo, "c1")

    result = gitio.diff_worktree_u0(repo, [])

    assert result == ""


def test_diff_worktree_u0_returns_diff_for_modified_files(tmp_path):
    """diff_worktree_u0 returns unified diff of uncommitted changes."""
    repo = _init(tmp_path)
    (repo / "a.c").write_text("line1\n")
    _commit(repo, "c1")

    # Modify the file
    (repo / "a.c").write_text("line1\nline2\n")

    result = gitio.diff_worktree_u0(repo, ["a.c"])

    assert "diff --git" in result
    # -U0 uses format like "@@ -1,0 +2 @@" (unified diff with 0 context lines)
    assert "@@" in result
    assert "+line2" in result


def test_diff_worktree_u0_ignores_whitespace_only_changes(tmp_path):
    """diff_worktree_u0 uses -w to ignore whitespace-only modifications."""
    repo = _init(tmp_path)
    (repo / "a.c").write_text("int x;\n")
    _commit(repo, "c1")

    # Reindent (whitespace-only change)
    (repo / "a.c").write_text("    int x;\n")

    result = gitio.diff_worktree_u0(repo, ["a.c"])

    # -w means no hunks for whitespace-only changes
    assert "@@" not in result or result.strip() == ""


def test_diff_text_shape_is_clean_and_symmetric(tmp_path):
    """Verify diff_text has no leading blank lines and ends with \\n consistently.

    REGRESSION: Current code prepends \\n to wrong record and has inconsistent trailing \\n.
    - diff_text should not start with \\n
    - Every non-empty diff_text should end with \\n (or be empty string)
    """
    repo = _init(tmp_path)

    # Create 3 commits
    (repo / "a.c").write_text("line1\n")
    _commit(repo, "c1")

    (repo / "a.c").write_text("line1\nline2\n")
    _commit(repo, "c2")

    (repo / "a.c").write_text("line1\nline2\nline3\n")
    _commit(repo, "c3")

    walk = gitio.log_walk_u0(repo, ["a.c"])

    assert len(walk) == 3
    assert [c.subject for c in walk] == ["c3", "c2", "c1"]

    # Check shape for each record
    for i, commit in enumerate(walk):
        diff_text = commit.diff_text
        if diff_text and not diff_text.isspace():
            # Non-empty diff must not start with newline
            assert not diff_text.startswith("\n"), (
                f"[{i}] {commit.subject}: diff_text starts with \\n: {diff_text[:30]!r}"
            )
            # Non-empty diff must start with "diff --git"
            assert diff_text.startswith("diff --git"), (
                f"[{i}] {commit.subject}: diff doesn't start with 'diff --git': {diff_text[:30]!r}"
            )
            # Every non-empty diff must end with newline
            assert diff_text.endswith("\n"), (
                f"[{i}] {commit.subject}: diff_text doesn't end with \\n: {diff_text[-20:]!r}"
            )
        elif diff_text and diff_text.isspace():
            # Whitespace-only should not occur (should be empty string)
            import pytest

            pytest.fail(f"[{i}] {commit.subject}: diff_text is whitespace-only: {diff_text!r}")
