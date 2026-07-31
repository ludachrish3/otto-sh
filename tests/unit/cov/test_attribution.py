"""parse_commit_diff: split one commit's `-U0` diff into per-file diffs, keyed by new path."""

import subprocess
from pathlib import Path

from otto.coverage import attribution
from otto.coverage.attribution import (
    UNCOMMITTED,
    attribute_lines,
    attribute_tickets,
    parse_commit_diff,
    parse_rename_records,
)
from otto.coverage.capture import gitio
from otto.coverage.tickets import build_ticket_spec

_MODIFY = """diff --git a/src/a.c b/src/a.c
--- a/src/a.c
+++ b/src/a.c
@@ -3 +3 @@
-old
+new
"""

_CREATE = """diff --git a/src/new.c b/src/new.c
new file mode 100644
--- /dev/null
+++ b/src/new.c
@@ -0,0 +1,3 @@
+a
+b
+c
"""

_RENAME = """diff --git a/src/old.c b/src/new.c
similarity index 98%
rename from src/old.c
rename to src/new.c
--- a/src/old.c
+++ b/src/new.c
@@ -7 +7 @@
-x
+y
"""

_DELETE = """diff --git a/src/gone.c b/src/gone.c
deleted file mode 100644
--- a/src/gone.c
+++ /dev/null
@@ -1,2 +0,0 @@
-a
-b
"""

_PURE_RENAME = """diff --git a/src/old.c b/src/new.c
similarity index 100%
rename from src/old.c
rename to src/new.c
"""


def test_modification_keyed_by_new_path_with_hunks():
    """A plain modification is keyed by its (unchanged) new path with its hunks intact."""
    parsed = parse_commit_diff(_MODIFY)
    assert set(parsed) == {"src/a.c"}
    fd = parsed["src/a.c"]
    assert fd.old_path == "src/a.c"
    assert [h.new_start for h in fd.hunks] == [3]


def test_file_creation_is_kept_with_old_path_none():
    """The regression parse_multifile_u0 would cause: a created file dropped."""
    parsed = parse_commit_diff(_CREATE)
    assert set(parsed) == {"src/new.c"}
    assert parsed["src/new.c"].old_path is None


def test_rename_records_both_sides():
    """A rename is keyed by its new path but still records the old path."""
    parsed = parse_commit_diff(_RENAME)
    assert set(parsed) == {"src/new.c"}
    assert parsed["src/new.c"].old_path == "src/old.c"


def test_deletion_is_dropped():
    """A file deleted by this commit has no new-side lines to attribute."""
    assert parse_commit_diff(_DELETE) == {}


def test_pure_rename_has_no_hunks_but_keeps_old_path():
    """100% similarity: no `---`/`+++`/`@@` block at all, just rename from/to.

    Backward replay depends on this: with hunks=[], LineRemapper.new_to_old
    is the identity, so every line survives the rename unchanged and keeps
    its identity into the parent's coordinates (Task 3 deferred this case
    untested; the reviewer flagged it as load-bearing for Task 4).
    """
    parsed = parse_commit_diff(_PURE_RENAME)
    assert set(parsed) == {"src/new.c"}
    fd = parsed["src/new.c"]
    assert fd.old_path == "src/old.c"
    assert fd.hunks == []


def test_multiple_files_in_one_commit():
    """A commit touching several files yields one entry per new path."""
    parsed = parse_commit_diff(_MODIFY + _CREATE)
    assert set(parsed) == {"src/a.c", "src/new.c"}


def test_empty_diff_is_empty():
    """An empty diff (a commit gitio reports no changes for) parses to no entries."""
    assert parse_commit_diff("") == {}


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "r"
    repo.mkdir()
    for args in (
        ["init", "-q"],
        ["config", "user.email", "t@example.com"],
        ["config", "user.name", "t"],
    ):
        subprocess.run(["git", *args], cwd=repo, check=True)
    return repo


def _commit(repo: Path, message: str) -> str:
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", message], cwd=repo, check=True)
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()


def _head(repo: Path) -> str:
    """Return the sha of the current HEAD commit."""
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()


def _make_ticket_repo(tmp_path: Path, message: str) -> Path:
    """Create a repo with a single commit touching a.c line 1, with the given message."""
    repo = _repo(tmp_path)
    (repo / "a.c").write_text("one\n")
    _commit(repo, message)
    return repo


def _make_two_commit_ticket_repo(tmp_path: Path) -> Path:
    """Create a repo with two commits: c1 touches line 1, c2 touches line 2."""
    repo = _repo(tmp_path)
    (repo / "a.c").write_text("one\ntwo\n")
    _commit(repo, "fix #1")
    (repo / "a.c").write_text("one\nCHANGED\n")
    _commit(repo, "fix #2")
    return repo


def test_each_line_attributed_to_the_commit_that_last_touched_it(tmp_path):
    repo = _repo(tmp_path)
    (repo / "a.c").write_text("one\ntwo\n")
    first = _commit(repo, "c1")
    (repo / "a.c").write_text("one\nCHANGED\n")
    second = _commit(repo, "c2")

    got = attribute_lines(repo, {"a.c": 2})

    assert got["a.c"] == {1: first, 2: second}


def test_created_file_attributes_all_lines_to_its_creating_commit(tmp_path):
    repo = _repo(tmp_path)
    (repo / "seed.c").write_text("s\n")
    _commit(repo, "seed")
    (repo / "new.c").write_text("a\nb\nc\n")
    created = _commit(repo, "add new.c")

    got = attribute_lines(repo, {"new.c": 3})

    assert got["new.c"] == {1: created, 2: created, 3: created}


def test_whitespace_only_edit_does_not_reattribute(tmp_path):
    repo = _repo(tmp_path)
    (repo / "a.c").write_text("int x;\n")
    first = _commit(repo, "c1")
    (repo / "a.c").write_text("    int x;\n")
    _commit(repo, "reindent")

    got = attribute_lines(repo, {"a.c": 1})

    assert got["a.c"] == {1: first}


def test_rename_is_followed_across_m_flag(tmp_path):
    repo = _repo(tmp_path)
    (repo / "old.c").write_text("".join(f"line{i}\n" for i in range(20)))
    first = _commit(repo, "c1")
    subprocess.run(["git", "mv", "old.c", "new.c"], cwd=repo, check=True)
    _commit(repo, "move it")

    got = attribute_lines(repo, {"new.c": 20})

    assert got["new.c"][1] == first


def test_rename_with_edit_before_and_after_attributes_each_side_correctly(tmp_path):
    """Guards the general rename case, not just a lone pure-rename commit.

    A file-scoped ``git log -- new.c`` walk would select the rename commit
    (it does touch ``new.c``) but render it as a fresh creation instead of a
    rename once ``old.c`` is not itself in the pathspec — and every commit
    under the old name becomes invisible to the walk entirely, since none of
    them touch ``new.c``. That would misattribute the pre-rename lines to
    whoever did the move, crediting them with code they did not write.
    """
    repo = _repo(tmp_path)
    body = [f"line{i}\n" for i in range(25)]
    (repo / "old.c").write_text("".join(body))
    seed = _commit(repo, "seed")
    subprocess.run(["git", "mv", "old.c", "new.c"], cwd=repo, check=True)
    _commit(repo, "rename")
    body[5] = "post-rename edit\n"
    (repo / "new.c").write_text("".join(body))
    after = _commit(repo, "edit after rename")

    got = attribute_lines(repo, {"new.c": len(body)})

    assert got["new.c"][6] == after  # 1-indexed: body[5] is line 6
    assert got["new.c"][1] == seed
    assert got["new.c"][25] == seed


def test_double_rename_is_followed_across_the_bounded_walk(tmp_path):
    """A file renamed twice must pull both ancestor names into the discovery
    pass's expanded pathspec, not just the most recent one.

    If ``_expand_historical_paths`` stopped after one hop (adding ``mid.c``
    but not ``old1.c``), the patch pass's pathspec would be
    ``{new.c, mid.c}`` — missing ``old1.c`` — and the "first rename" commit
    (``old1.c`` -> ``mid.c``) would render as a fresh creation of ``mid.c``
    instead of a rename (the same pathspec/rename-pairing bug this whole
    module exists to route around), crediting the first-rename commit with
    lines the seed commit actually wrote.
    """
    repo = _repo(tmp_path)
    body = [f"line{i}\n" for i in range(15)]
    (repo / "old1.c").write_text("".join(body))
    seed = _commit(repo, "seed")
    subprocess.run(["git", "mv", "old1.c", "mid.c"], cwd=repo, check=True)
    _commit(repo, "first rename")
    subprocess.run(["git", "mv", "mid.c", "new.c"], cwd=repo, check=True)
    _commit(repo, "second rename")

    got = attribute_lines(repo, {"new.c": len(body)})

    assert got["new.c"][1] == seed
    assert got["new.c"][len(body)] == seed


def test_expand_historical_paths_follows_double_rename(tmp_path):
    """White-box check on the discovery pass itself: transitively pulls in
    both ``old1.c`` and ``mid.c`` for a file now known as ``new.c``.
    """
    repo = _repo(tmp_path)
    (repo / "old1.c").write_text("x\n")
    _commit(repo, "seed")
    subprocess.run(["git", "mv", "old1.c", "mid.c"], cwd=repo, check=True)
    _commit(repo, "first rename")
    subprocess.run(["git", "mv", "mid.c", "new.c"], cwd=repo, check=True)
    _commit(repo, "second rename")

    result = attribution._expand_historical_paths(repo, {"new.c"}, first_parent=True)

    assert result == {"new.c", "mid.c", "old1.c"}


def test_expand_historical_paths_does_not_grow_for_a_never_renamed_file(tmp_path):
    """The common case: a covered file with no rename history expands to itself only."""
    repo = _repo(tmp_path)
    (repo / "a.c").write_text("x\n")
    _commit(repo, "c1")
    (repo / "a.c").write_text("y\n")
    _commit(repo, "c2")

    result = attribution._expand_historical_paths(repo, {"a.c"}, first_parent=True)

    assert result == {"a.c"}


def test_parse_rename_records_extracts_only_renames():
    """Add/modify/delete lines have no old side and carry no path identity."""
    block = "M\tunrelated.c\nR100\told.c\tnew.c\nA\tcreated.c\n"

    assert parse_rename_records(block) == {"new.c": "old.c"}


def test_parse_rename_records_empty_block_is_empty():
    assert parse_rename_records("") == {}


def test_uncommitted_edit_gets_the_sentinel_not_the_previous_committer(tmp_path):
    repo = _repo(tmp_path)
    (repo / "a.c").write_text("one\ntwo\n")
    first = _commit(repo, "c1")
    (repo / "a.c").write_text("one\nDIRTY\n")

    got = attribute_lines(repo, {"a.c": 2})

    assert got["a.c"] == {1: first, 2: UNCOMMITTED}


def _sign_with_ssh(repo: Path, tmp_path: Path) -> None:
    """Arm *repo* for SSH commit signing with a fresh, passphrase-less key.

    SSH signing (``gpg.format=ssh``) needs no keyring, agent, or
    ``allowedSignersFile`` setup to *create* a signature — only to verify
    one — so this is fast and fully self-contained: no GPG key generation,
    no gpg-agent socket, nothing that could be flaky in a sandboxed CI
    runner. ``ssh-keygen`` ships with git's own dependency (openssh) on
    every platform this suite targets.
    """
    key = tmp_path / "id_ed25519"
    subprocess.run(["ssh-keygen", "-t", "ed25519", "-N", "", "-f", str(key), "-q"], check=True)
    subprocess.run(["git", "config", "gpg.format", "ssh"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.signingkey", f"{key}.pub"], cwd=repo, check=True)


def test_attribution_survives_hostile_repo_local_git_config(tmp_path):
    """Final-review blocker 1: repo-local git config must not corrupt attribution.

    Three real reproductions found in review, all against config a SUT
    repo (or the invoking user's ``~/.gitconfig``) can set with otto having
    no say in it:

    - ``diff.mnemonicprefix = true`` (a common dotfiles setting) makes
      ``git diff HEAD`` key the worktree overlay's diff under ``w/a.c``
      instead of ``a.c``, so ``_apply_worktree_overlay`` silently skips
      the file and the uncommitted line below would inherit ``second``'s
      sha instead of getting :data:`UNCOMMITTED`.
    - ``log.showSignature = true`` on a repo with signed commits
      interleaves gpg/ssh signature-check text (``No signature`` here,
      since verification has no ``allowedSignersFile`` to check against)
      into the metadata stream ``gitio.log_walk_u0`` parses, corrupting
      every signed commit's sha field — with both commits signed here,
      the whole walk silently degrades to "no history found" and every
      line reads :data:`UNCOMMITTED`.
    - ``diff.external`` set to any program makes the worktree-overlay diff
      subprocess hard fail with ``gitio.GitUnavailableError`` instead of
      running git's own diff engine.

    All three are set repo-locally below (not via a hermetic test-only
    env) precisely because that is what a real SUT checkout can do to
    otto with zero cooperation from otto's own test harness — pinning
    them via CLI flags on every git invocation this module makes
    (``otto.coverage.capture.gitio._pin``) is what survives it.
    """
    repo = _repo(tmp_path)
    _sign_with_ssh(repo, tmp_path)

    (repo / "a.c").write_text("one\ntwo\nthree\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-S", "-m", "seed"], cwd=repo, check=True)
    first = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()

    (repo / "a.c").write_text("one\nCHANGED\nthree\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-S", "-m", "second"], cwd=repo, check=True)
    second = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()

    (repo / "a.c").write_text("one\nCHANGED\nDIRTY\n")  # uncommitted edit to line 3

    subprocess.run(["git", "config", "diff.mnemonicprefix", "true"], cwd=repo, check=True)
    subprocess.run(["git", "config", "log.showSignature", "true"], cwd=repo, check=True)
    subprocess.run(["git", "config", "diff.external", "/bin/false"], cwd=repo, check=True)

    got = attribute_lines(repo, {"a.c": 3})

    assert got["a.c"] == {1: first, 2: second, 3: UNCOMMITTED}


def test_tickets_map_lines_and_collect_commits(tmp_path):
    repo = _repo(tmp_path)
    (repo / "a.c").write_text("one\ntwo\n")
    _commit(repo, "seed PROJ-1")
    (repo / "a.c").write_text("one\nCHANGED\n")
    sha = _commit(repo, "fix PROJ-2 and PROJ-3")

    spec = build_ticket_spec(r"[A-Z]{2,10}-[0-9]+", None)
    lines, commits, shas = attribute_tickets(repo, {"a.c": 2}, spec)

    assert lines["a.c"][1] == ["PROJ-1"]
    assert lines["a.c"][2] == ["PROJ-2", "PROJ-3"]
    assert commits["PROJ-2"] == [sha]
    assert shas["a.c"][2] == sha


def test_commit_with_no_ticket_yields_empty_id_list(tmp_path):
    repo = _repo(tmp_path)
    (repo / "a.c").write_text("one\n")
    _commit(repo, "chore: bump")

    spec = build_ticket_spec(r"[A-Z]{2,10}-[0-9]+", None)
    lines, _, _shas = attribute_tickets(repo, {"a.c": 1}, spec)

    assert lines["a.c"][1] == []


def test_untracked_file_is_entirely_uncommitted(tmp_path):
    """git knows nothing about it, so no commit can own its lines."""
    repo = _repo(tmp_path)
    (repo / "a.c").write_text("one\n")
    _commit(repo, "c1")
    (repo / "new.c").write_text("x\ny\n")  # never added

    got = attribute_lines(repo, {"a.c": 1, "new.c": 2})

    assert got["new.c"] == {1: UNCOMMITTED, 2: UNCOMMITTED}


def test_attribute_tickets_exposes_owning_sha_so_callers_can_tell_uncommitted_from_no_ticket(
    tmp_path,
):
    """Task 14: both a no-ticket commit and an uncommitted edit yield an
    empty id list from ``attribute_tickets`` alone (indistinguishable by
    ``lines`` alone) — the third return value is exactly the raw
    ``attribute_lines`` sha map, computed internally either way, so a
    caller (``CoverageReporter._annotate_tickets``) can assign the right
    sentinel without a second git log walk."""
    repo = _repo(tmp_path)
    (repo / "a.c").write_text("one\ntwo\n")
    sha = _commit(repo, "chore: bump")
    (repo / "a.c").write_text("one\nDIRTY\n")

    spec = build_ticket_spec(r"[A-Z]{2,10}-[0-9]+", None)
    lines, _commits, shas = attribute_tickets(repo, {"a.c": 2}, spec)

    assert lines["a.c"] == {1: [], 2: []}
    assert shas["a.c"] == {1: sha, 2: UNCOMMITTED}


def test_reattribution_replaces_the_parsed_ticket_set(tmp_path):
    repo = _make_ticket_repo(tmp_path, "fix #1")
    sha = _head(repo)
    spec = build_ticket_spec("#(?P<n>[0-9]+)", None)
    lines, commits, _ = attribute_tickets(repo, {"a.c": 1}, spec, reattributions={sha: ["#9"]})
    assert lines["a.c"][1] == ["#9"]
    assert "#1" not in commits
    assert commits["#9"] == [sha]


def test_reattribution_to_empty_list_means_no_ticket(tmp_path):
    repo = _make_ticket_repo(tmp_path, "fix #1")
    sha = _head(repo)
    spec = build_ticket_spec("#(?P<n>[0-9]+)", None)
    lines, commits, _ = attribute_tickets(repo, {"a.c": 1}, spec, reattributions={sha: []})
    assert lines["a.c"][1] == []
    assert commits == {}


def test_reattribution_leaves_other_commits_alone(tmp_path):
    repo = _make_two_commit_ticket_repo(tmp_path)
    c2 = _head(repo)
    spec = build_ticket_spec("#(?P<n>[0-9]+)", None)
    lines, _, _ = attribute_tickets(repo, {"a.c": 2}, spec, reattributions={c2: ["#7"]})
    assert lines["a.c"][1] == ["#1"]
    assert lines["a.c"][2] == ["#7"]


def test_commit_message_matching_a_reserved_sentinel_id_is_dropped_not_merged(tmp_path, caplog):
    """A user's ticket pattern could in principle match the literal text
    '(no ticket)' or '(uncommitted)' inside a real commit message. Letting
    that match through as a normal ticket id would silently merge a real
    commit into the synthetic sentinel bucket reporter.py builds
    separately (Task 14 brief's collision trap) — it must instead be
    dropped (loudly logged), leaving the line exactly where an ordinary
    no-ticket-match commit would: an empty id list."""
    from otto.coverage.attribution import NO_TICKET

    repo = _repo(tmp_path)
    (repo / "a.c").write_text("one\n")
    _commit(repo, "fix (no ticket) needed here")

    spec = build_ticket_spec(r"\(no ticket\)", None)
    with caplog.at_level("WARNING"):
        lines, commits, _shas = attribute_tickets(repo, {"a.c": 1}, spec)

    assert lines["a.c"][1] == []
    assert NO_TICKET not in commits
    assert commits == {}
    assert any("sentinel" in rec.message.lower() for rec in caplog.records)


def _spawn_count(repo: Path, monkeypatch, n_files: int) -> int:
    calls: list[list[str]] = []
    real = gitio._run_raw

    def counting(args, cwd, ok_codes=(0,)):
        calls.append(args)
        return real(args, cwd, ok_codes)

    monkeypatch.setattr(gitio, "_run_raw", counting)
    spec = build_ticket_spec(r"[A-Z]{2,10}-[0-9]+", None)
    attribute_tickets(repo, {f"f{n}.c": 1 for n in range(n_files)}, spec)
    return len(calls)


def test_git_subprocess_count_is_constant_in_file_count(tmp_path, monkeypatch):
    """O(1) subprocesses, not O(files) — the guard `test_git_spawn_budget.py`
    already enforces for the validity pass. A per-file worktree diff or a
    second log walk would reintroduce exactly the cost profile this design
    rejected `git blame` for.

    The fixed budget is 4, not 3 (Task 4b, the bounded name-status
    discovery + expanded pathspec walk): the discovery pass
    (``gitio.name_status_walk_u0``) is one more raw call, ``log_walk_u0``
    (Task 2) is deliberately *two* raw calls (NUL-delimited metadata, then
    a sha-joined diff stream — its own control-character-safety fix), and
    one more is the worktree overlay's single diff. What matters is that
    this total never grows with file count, so the real assertion compares
    the count at two different file counts rather than trusting one
    arbitrary number — and ``large > 0`` so a refactor that bypassed
    ``_run_raw`` entirely (and so recorded zero calls) could not make this
    pass vacuously.
    """
    repo = _repo(tmp_path)
    for n in range(50):
        (repo / f"f{n}.c").write_text(f"line {n}\n")
    _commit(repo, "seed PROJ-1")

    small = _spawn_count(repo, monkeypatch, 5)
    large = _spawn_count(repo, monkeypatch, 50)

    assert small == large, f"spawn count grew with file count: {small} (5 files) vs {large} (50)"
    assert large > 0, "recorded zero git spawns — _run_raw interception is not wired up"
    assert large <= 4, f"expected <=4 git spawns for 50 files, got {large}"


def _unrestricted_lines(
    repo: Path, line_counts: dict[str, int], *, first_parent: bool = True
) -> dict[str, dict[int, str]]:
    """Reference implementation: the pre-Task-4b engine, fed straight into
    ``attribute_lines`` via its ``walk=`` override so the replay logic under
    test is identical and only the walk's pathspec differs.
    """
    walk = gitio.log_walk_u0(repo, ["."], first_parent=first_parent)
    return attribute_lines(repo, line_counts, first_parent=first_parent, walk=walk)


def test_bounded_walk_matches_unrestricted_walk_exactly_on_synthetic_history(tmp_path):
    """The acceptance bar: bounded output must equal unrestricted output, not
    merely look plausible. Builds one repo mixing every case the brief
    calls out — a never-renamed file, a renamed-then-edited file, a double
    rename, and a plain creation — and compares the *entire* result dict
    between the bounded engine (``attribute_lines``'s real ``_fetch_walk``)
    and the unrestricted reference above.
    """
    repo = _repo(tmp_path)

    # a.c: created, edited, never renamed.
    (repo / "a.c").write_text("a1\na2\n")
    # b_old.c: will be renamed once, then edited post-rename.
    (repo / "b_old.c").write_text("".join(f"b{i}\n" for i in range(10)))
    _commit(repo, "seed a.c and b_old.c")

    (repo / "a.c").write_text("a1\nCHANGED\n")
    subprocess.run(["git", "mv", "b_old.c", "b_mid.c"], cwd=repo, check=True)
    _commit(repo, "edit a.c, first rename of b")

    subprocess.run(["git", "mv", "b_mid.c", "b_new.c"], cwd=repo, check=True)
    # c_old.c: created and renamed within the same window (double rename setup for c too).
    (repo / "c_old.c").write_text("c1\nc2\nc3\n")
    _commit(repo, "second rename of b, create c_old.c")

    b_body = [f"b{i}\n" for i in range(10)]
    b_body[3] = "post-rename b edit\n"
    (repo / "b_new.c").write_text("".join(b_body))
    subprocess.run(["git", "mv", "c_old.c", "c_new.c"], cwd=repo, check=True)
    _commit(repo, "edit b_new.c, rename c")

    # d.c: plain creation, no rename, added last.
    (repo / "d.c").write_text("d1\n")
    _commit(repo, "create d.c")

    line_counts = {
        "a.c": 2,
        "b_new.c": len(b_body),
        "c_new.c": 3,
        "d.c": 1,
    }

    bounded = attribute_lines(repo, line_counts)
    unrestricted = _unrestricted_lines(repo, line_counts)

    assert bounded == unrestricted
