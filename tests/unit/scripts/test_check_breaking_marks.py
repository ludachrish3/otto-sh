"""``scripts/check_breaking_marks.py``: a public-API golden deletion must be marked.

``tests/unit/api_snapshot/public_api.txt`` (``scripts/api_snapshot.py``) is the
golden anchor: a line removed from it is, by construction, a public-API break
(a name gone from ``otto.__all__``, a deep import the docs stopped teaching, or
a ``Host`` protocol parameter dropped/renamed). ``make release``'s version
comes from git-cliff's conventional-commit census, which only sees what a
commit SUBJECT/BODY says — this script is what makes an unmarked deletion a
CI failure instead of a silent patch release.

Every test builds its own throwaway git repo under ``tmp_path`` via
``tests._fixtures.gitrepo.TmpGitRepo`` (the same hermetic harness
``test_release_bump.py``'s real-git-cliff tests use) — never the dev repo
itself.
"""

import pytest

from scripts.check_breaking_marks import (
    RangeError,
    commits_in_range,
    is_library_line,
    is_widened_protocol_line,
    main,
    path_resolves,
    removed_golden_lines,
    validate_range,
)
from tests._fixtures.gitrepo import TmpGitRepo

GOLDEN_HEADER = "# a golden header line, never a data line\n"


def _seed(repo: TmpGitRepo, golden_lines: "list[str]") -> str:
    """Commit a golden file with a header plus *golden_lines* as the seed commit."""
    repo.write("golden.txt", GOLDEN_HEADER + "\n".join(golden_lines) + "\n")
    return repo.commit("chore: seed the golden")


def test_removal_marked_with_bang_passes(tmp_path):
    repo = TmpGitRepo(tmp_path)
    _seed(repo, ["otto:Alpha", "otto:Beta"])
    repo.write("golden.txt", GOLDEN_HEADER + "otto:Alpha\n")
    tip = repo.commit("feat(api)!: drop Beta")

    exit_code = main([f"{tip}~1..{tip}", "--repo", str(repo.root), "--golden", "golden.txt"])

    assert exit_code == 0


def test_removal_marked_with_footer_only_passes(tmp_path):
    repo = TmpGitRepo(tmp_path)
    _seed(repo, ["otto:Alpha", "otto:Beta"])
    repo.write("golden.txt", GOLDEN_HEADER + "otto:Alpha\n")
    tip = repo.commit("feat(api): drop Beta\n\nBREAKING CHANGE: Beta no longer exists.")

    exit_code = main([f"{tip}~1..{tip}", "--repo", str(repo.root), "--golden", "golden.txt"])

    assert exit_code == 0


def test_removal_unmarked_fails_naming_commit_and_line(tmp_path, capsys):
    repo = TmpGitRepo(tmp_path)
    _seed(repo, ["otto:Alpha", "otto:Beta"])
    repo.write("golden.txt", GOLDEN_HEADER + "otto:Alpha\n")
    tip = repo.commit("feat(api): drop Beta")

    exit_code = main([f"{tip}~1..{tip}", "--repo", str(repo.root), "--golden", "golden.txt"])

    assert exit_code == 1
    out = capsys.readouterr().out
    assert tip in out
    assert "drop Beta" in out
    assert "otto:Beta" in out
    assert "RULE:" in out


def test_pure_addition_passes(tmp_path):
    repo = TmpGitRepo(tmp_path)
    _seed(repo, ["otto:Alpha"])
    repo.write("golden.txt", GOLDEN_HEADER + "otto:Alpha\notto:Beta\n")
    tip = repo.commit("feat(api): add Beta")

    exit_code = main([f"{tip}~1..{tip}", "--repo", str(repo.root), "--golden", "golden.txt"])

    assert exit_code == 0


def test_empty_range_is_ok(tmp_path):
    repo = TmpGitRepo(tmp_path)
    tip = _seed(repo, ["otto:Alpha"])

    exit_code = main([f"{tip}..{tip}", "--repo", str(repo.root), "--golden", "golden.txt"])

    assert exit_code == 0


def test_merge_commit_is_skipped(tmp_path):
    repo = TmpGitRepo(tmp_path)
    base = _seed(repo, ["otto:Alpha", "otto:Beta"])

    repo.git("checkout", "-b", "side")
    repo.write("unrelated.txt", "x")
    side_tip = repo.commit("chore: unrelated side work")

    repo.git("checkout", "main")
    repo.write("golden.txt", GOLDEN_HEADER + "otto:Alpha\n")
    repo.commit("feat(api)!: drop Beta on main")

    repo.git("merge", "--no-ff", "-m", "merge: bring in side", side_tip)
    merge_tip = repo.git("rev-parse", "HEAD").strip()

    exit_code = main([f"{base}..{merge_tip}", "--repo", str(repo.root), "--golden", "golden.txt"])

    assert exit_code == 0


def test_header_line_removal_is_ignored(tmp_path):
    """A ``#`` header edit is never a public-API deletion, marked or not."""
    repo = TmpGitRepo(tmp_path)
    _seed(repo, ["otto:Alpha"])
    repo.write("golden.txt", "# a rewritten header\notto:Alpha\n")
    tip = repo.commit("docs(api): reword the golden header")

    exit_code = main([f"{tip}~1..{tip}", "--repo", str(repo.root), "--golden", "golden.txt"])

    assert exit_code == 0


def test_commits_in_range_excludes_merges(tmp_path):
    repo = TmpGitRepo(tmp_path)
    base = _seed(repo, ["otto:Alpha"])

    repo.git("checkout", "-b", "side")
    repo.write("unrelated.txt", "x")
    side_tip = repo.commit("chore: unrelated side work")

    repo.git("checkout", "main")
    repo.write("more.txt", "y")
    main_tip = repo.commit("chore: unrelated main work")

    repo.git("merge", "--no-ff", "-m", "merge: bring in side", side_tip)
    merge_tip = repo.git("rev-parse", "HEAD").strip()

    shas = commits_in_range(repo.root, f"{base}..{merge_tip}")

    assert side_tip in shas
    assert main_tip in shas
    assert merge_tip not in shas


def test_removed_golden_lines_ignores_header_and_file_marker_lines(tmp_path):
    repo = TmpGitRepo(tmp_path)
    _seed(repo, ["otto:Alpha", "otto:Beta"])
    repo.write("golden.txt", "# a totally rewritten header\notto:Alpha\n")
    tip = repo.commit("docs(api): reword header and drop Beta")

    removed = removed_golden_lines(repo.root, tip, repo.root / "golden.txt")

    assert removed == ["otto:Beta"]


def test_root_commit_in_range_is_all_additions_and_passes(tmp_path):
    """A commit with no parent must never crash the scan — it can only add lines.

    Built via an orphan branch merged into main: the orphan's own first
    commit has no parent, so it lands in ``base..merge_tip`` as a genuine
    root commit (the merge commit itself is excluded by ``--no-merges``).
    Diffed against the empty tree, it can only be reported as additions —
    exercised both at the ``removed_golden_lines`` unit level (a root sha's
    own diff) and end-to-end through ``main``.
    """
    repo = TmpGitRepo(tmp_path)
    base = _seed(repo, ["otto:Alpha"])

    repo.git("checkout", "--orphan", "rootbranch")
    repo.git("rm", "-rf", "--cached", ".")
    root_sha = _seed(repo, ["otto:FromOrphanRoot"])
    assert repo.git("rev-list", "--max-parents=0", root_sha).strip() == root_sha

    repo.git("checkout", "main")
    repo.git(
        "merge",
        "--no-ff",
        "--allow-unrelated-histories",
        "-X",
        "ours",
        "-m",
        "merge: bring in root",
        root_sha,
    )
    merge_tip = repo.git("rev-parse", "HEAD").strip()

    removed = removed_golden_lines(repo.root, root_sha, repo.root / "golden.txt")
    assert removed == []

    exit_code = main([f"{base}..{merge_tip}", "--repo", str(repo.root), "--golden", "golden.txt"])
    assert exit_code == 0


def test_unresolvable_range_reports_clean_error_no_traceback(tmp_path, capsys):
    """An unknown ref must produce a message on stderr and rc 2, never a traceback."""
    repo = TmpGitRepo(tmp_path)
    _seed(repo, ["otto:Alpha"])

    exit_code = main(["origin/main..HEAD", "--repo", str(repo.root), "--golden", "golden.txt"])

    assert exit_code == 2
    err = capsys.readouterr().err
    assert "cannot resolve range" in err
    assert "origin/main..HEAD" in err


def test_range_without_dotdot_is_refused(tmp_path, capsys):
    repo = TmpGitRepo(tmp_path)
    tip = _seed(repo, ["otto:Alpha"])

    exit_code = main([tip, "--repo", str(repo.root), "--golden", "golden.txt"])

    assert exit_code == 2
    err = capsys.readouterr().err
    assert "not a commit range" in err


def test_validate_range_raises_on_a_bare_ref():
    with pytest.raises(RangeError, match="not a commit range"):
        validate_range("HEAD")


def test_validate_range_accepts_a_two_dot_range():
    validate_range("origin/main..HEAD")  # must not raise


def test_red_inverting_the_marked_check_lets_an_unmarked_removal_through(tmp_path, monkeypatch):
    """RED-proof: with the marked-commit check inverted, the unmarked-removal test must fail.

    Confirms ``test_removal_unmarked_fails_naming_commit_and_line`` is actually
    exercising the classifier rather than passing by construction — flip
    ``is_breaking_commit`` to its negation and the refusal must disappear.
    """
    import scripts.check_breaking_marks as mod

    monkeypatch.setattr(mod, "is_breaking_commit", lambda subject, body: True)

    repo = TmpGitRepo(tmp_path)
    _seed(repo, ["otto:Alpha", "otto:Beta"])
    repo.write("golden.txt", GOLDEN_HEADER + "otto:Alpha\n")
    tip = repo.commit("feat(api): drop Beta")

    exit_code = mod.main([f"{tip}~1..{tip}", "--repo", str(repo.root), "--golden", "golden.txt"])

    assert exit_code == 0, "inverted classifier should have let the unmarked removal through"


def test_docs_only_removal_that_still_imports_passes_unmarked(tmp_path, capsys):
    """A path only the docs taught, still importable, is not a break."""
    repo = TmpGitRepo(tmp_path)
    _seed(repo, ["json:dumps", "otto:Alpha"])
    repo.write("golden.txt", GOLDEN_HEADER + "otto:Alpha\n")
    tip = repo.commit("docs: stop teaching json:dumps")

    exit_code = main([f"{tip}~1..{tip}", "--repo", str(repo.root), "--golden", "golden.txt"])

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "docs-only, still importable" in out
    assert "json:dumps" in out


def test_docs_only_removal_that_no_longer_imports_needs_a_mark(tmp_path, capsys):
    repo = TmpGitRepo(tmp_path)
    _seed(repo, ["json:no_such_name", "otto:Alpha"])
    repo.write("golden.txt", GOLDEN_HEADER + "otto:Alpha\n")
    tip = repo.commit("docs: stop teaching a dead path")

    exit_code = main([f"{tip}~1..{tip}", "--repo", str(repo.root), "--golden", "golden.txt"])

    assert exit_code == 1
    out = capsys.readouterr().out
    assert "json:no_such_name" in out

    repo.write("golden.txt", GOLDEN_HEADER + "json:no_such_name\notto:Alpha\n")
    repo.commit("docs: put it back")
    repo.write("golden.txt", GOLDEN_HEADER + "otto:Alpha\n")
    marked = repo.commit("docs!: stop teaching a dead path")

    assert main([f"{marked}~1..{marked}", "--repo", str(repo.root), "--golden", "golden.txt"]) == 0


def test_bare_module_line_follows_the_same_resolvability_rule(tmp_path, capsys):
    repo = TmpGitRepo(tmp_path)
    _seed(repo, ["json:", "no_such_module_at_all:", "otto:Alpha"])
    repo.write("golden.txt", GOLDEN_HEADER + "otto:Alpha\n")
    tip = repo.commit("docs: stop teaching both bare imports")

    exit_code = main([f"{tip}~1..{tip}", "--repo", str(repo.root), "--golden", "golden.txt"])

    assert exit_code == 1
    out = capsys.readouterr().out
    assert "docs-only, still importable" in out
    assert "json:" in out
    assert "no_such_module_at_all:" in out


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("otto:Status", True),
        ("otto:", False),
        ("otto.host.host:Host.run(a)", True),
        ("otto.host.host:Host", False),
        ("otto.utils:Status", False),
        ("a line with no colon", True),
    ],
    ids=[
        "all-exports-name",
        "bare-otto-module",
        "host-protocol-signature",
        "host-class-without-method",
        "deep-import-path",
        "unrecognised-shape",
    ],
)
def test_only_the_two_library_shapes_and_an_unrecognised_line_are_library_surface(line, expected):
    assert is_library_line(line) is expected


def test_library_line_removal_still_fails_unmarked_even_though_it_imports(tmp_path, capsys):
    """A library line is a break even when the path it names still resolves."""
    repo = TmpGitRepo(tmp_path)
    _seed(repo, ["otto:Alpha", "otto:Status"])
    repo.write("golden.txt", GOLDEN_HEADER + "otto:Alpha\n")
    tip = repo.commit("refactor(api): drop Status")

    assert path_resolves("otto:Status", repo.root), "the removed line must still import"

    exit_code = main([f"{tip}~1..{tip}", "--repo", str(repo.root), "--golden", "golden.txt"])

    assert exit_code == 1
    assert "otto:Status" in capsys.readouterr().out


def test_resolution_imports_from_the_scanned_tree_not_the_interpreters_own(tmp_path, capsys):
    """A module that exists only under ``--repo``'s ``src/`` counts as resolvable.

    ``python -c`` in a src-layout project imports through the venv's ``.pth``,
    which names one absolute source directory — so running the resolver with
    its cwd inside the scanned tree is not enough. Only that tree's ``src/``
    on the child's path makes this module visible.
    """
    repo = TmpGitRepo(tmp_path)
    repo.write("src/zz_docs_only_probe.py", "thing = object()\n")
    _seed(repo, ["otto:Alpha", "zz_docs_only_probe:thing"])
    repo.write("golden.txt", GOLDEN_HEADER + "otto:Alpha\n")
    tip = repo.commit("docs: stop teaching the probe import")

    exit_code = main([f"{tip}~1..{tip}", "--repo", str(repo.root), "--golden", "golden.txt"])

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "zz_docs_only_probe:thing" in out
    assert "docs-only, still importable" in out


def test_range_ending_at_an_annotated_tag_that_is_head_is_accepted(tmp_path):
    """``rev-parse`` on an annotated tag yields the TAG object, not the commit."""
    repo = TmpGitRepo(tmp_path)
    base = _seed(repo, ["otto:Alpha"])
    repo.write("golden.txt", GOLDEN_HEADER + "otto:Alpha\notto:Beta\n")
    repo.commit("feat(api): add Beta")
    repo.git("tag", "-a", "v9.9.9", "-m", "v9.9.9")
    assert repo.git("rev-parse", "v9.9.9").strip() != repo.git("rev-parse", "HEAD").strip()

    exit_code = main([f"{base}..v9.9.9", "--repo", str(repo.root), "--golden", "golden.txt"])

    assert exit_code == 0


def test_range_not_ending_at_head_is_refused(tmp_path, capsys):
    repo = TmpGitRepo(tmp_path)
    base = _seed(repo, ["otto:Alpha"])
    repo.write("golden.txt", GOLDEN_HEADER + "otto:Alpha\notto:Beta\n")
    repo.commit("feat(api): add Beta")

    exit_code = main([f"{base}~0..{base}", "--repo", str(repo.root), "--golden", "golden.txt"])

    assert exit_code == 2
    err = capsys.readouterr().err
    assert "must end at HEAD" in err


def test_a_trailing_host_parameter_addition_widens_and_passes_unmarked(tmp_path, capsys):
    """Spec 2026-09-10 recursive-transfer, fix round 1: a widening is not a break."""
    repo = TmpGitRepo(tmp_path)
    _seed(repo, ["otto.host.host:Host.put(a, b)"])
    repo.write("golden.txt", GOLDEN_HEADER + "otto.host.host:Host.put(a, b, c)\n")
    tip = repo.commit("feat(host): put gains c")

    exit_code = main([f"{tip}~1..{tip}", "--repo", str(repo.root), "--golden", "golden.txt"])

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "widened" in out
    assert "otto.host.host:Host.put(a, b)" in out
    assert "no mark needed" in out


def _fake_host_module(repo: TmpGitRepo, params: str) -> None:
    """Give *repo* an importable ``otto.host.host:Host`` whose ``put`` takes *params*.

    The checker puts ``<repo>/src`` FIRST on its resolver subprocess's path
    (:func:`scripts.check_breaking_marks._resolver_env`), so a package
    planted here is the one the live-signature check reads — never the
    installed otto.
    """
    repo.write("src/otto/__init__.py", "")
    repo.write("src/otto/host/__init__.py", "")
    repo.write("src/otto/host/host.py", f"class Host:\n    def put(self, {params}):\n        ...\n")


def test_an_appended_host_parameter_with_a_default_widens_and_passes_unmarked(tmp_path, capsys):
    """The live signature agrees the appended parameter is optional."""
    repo = TmpGitRepo(tmp_path)
    _fake_host_module(repo, "a, b, c=None")
    _seed(repo, ["otto.host.host:Host.put(a, b)"])
    repo.write("golden.txt", GOLDEN_HEADER + "otto.host.host:Host.put(a, b, c)\n")
    tip = repo.commit("feat(host): put gains an optional c")

    exit_code = main([f"{tip}~1..{tip}", "--repo", str(repo.root), "--golden", "golden.txt"])

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "widened" in out
    assert "no mark needed" in out


def test_an_appended_host_parameter_without_a_default_is_a_break(tmp_path, capsys):
    """A REQUIRED appended parameter breaks every caller that omits it — the
    golden carries names only, so the live signature is what tells them apart."""
    repo = TmpGitRepo(tmp_path)
    _fake_host_module(repo, "a, b, c")
    _seed(repo, ["otto.host.host:Host.put(a, b)"])
    repo.write("golden.txt", GOLDEN_HEADER + "otto.host.host:Host.put(a, b, c)\n")
    tip = repo.commit("feat(host): put gains a required c")

    exit_code = main([f"{tip}~1..{tip}", "--repo", str(repo.root), "--golden", "golden.txt"])

    assert exit_code == 1
    out = capsys.readouterr().out
    assert "otto.host.host:Host.put(a, b)" in out
    assert "REQUIRED parameter c" in out
    assert "RULE:" in out


def test_a_reordered_host_parameter_list_is_still_a_break(tmp_path, capsys):
    repo = TmpGitRepo(tmp_path)
    _seed(repo, ["otto.host.host:Host.put(a, b)"])
    repo.write("golden.txt", GOLDEN_HEADER + "otto.host.host:Host.put(a, c, b)\n")
    tip = repo.commit("feat(host): put reorders")

    exit_code = main([f"{tip}~1..{tip}", "--repo", str(repo.root), "--golden", "golden.txt"])

    assert exit_code == 1
    out = capsys.readouterr().out
    assert "otto.host.host:Host.put(a, b)" in out
    assert "widened" not in out


def test_a_renamed_host_parameter_is_still_a_break(tmp_path, capsys):
    repo = TmpGitRepo(tmp_path)
    _seed(repo, ["otto.host.host:Host.put(a, b)"])
    repo.write("golden.txt", GOLDEN_HEADER + "otto.host.host:Host.put(a, c)\n")
    tip = repo.commit("feat(host): put renames b to c")

    exit_code = main([f"{tip}~1..{tip}", "--repo", str(repo.root), "--golden", "golden.txt"])

    assert exit_code == 1
    out = capsys.readouterr().out
    assert "otto.host.host:Host.put(a, b)" in out
    assert "widened" not in out


def test_a_shortened_host_parameter_list_is_still_a_break(tmp_path, capsys):
    repo = TmpGitRepo(tmp_path)
    _seed(repo, ["otto.host.host:Host.put(a, b)"])
    repo.write("golden.txt", GOLDEN_HEADER + "otto.host.host:Host.put(a)\n")
    tip = repo.commit("feat(host): put drops b")

    exit_code = main([f"{tip}~1..{tip}", "--repo", str(repo.root), "--golden", "golden.txt"])

    assert exit_code == 1
    out = capsys.readouterr().out
    assert "otto.host.host:Host.put(a, b)" in out
    assert "widened" not in out


def test_a_pure_host_removal_with_no_added_counterpart_is_still_a_break(tmp_path, capsys):
    repo = TmpGitRepo(tmp_path)
    _seed(repo, ["otto.host.host:Host.put(a, b)", "otto:Alpha"])
    repo.write("golden.txt", GOLDEN_HEADER + "otto:Alpha\n")
    tip = repo.commit("feat(host): drop put entirely")

    exit_code = main([f"{tip}~1..{tip}", "--repo", str(repo.root), "--golden", "golden.txt"])

    assert exit_code == 1
    out = capsys.readouterr().out
    assert "otto.host.host:Host.put(a, b)" in out
    assert "widened" not in out


@pytest.mark.parametrize(
    ("removed", "added", "expected"),
    [
        ("otto.host.host:Host.put(a, b)", ["otto.host.host:Host.put(a, b, c)"], True),
        ("otto.host.host:Host.put(a, b)", ["otto.host.host:Host.put(a, c, b)"], False),
        ("otto.host.host:Host.put(a, b)", ["otto.host.host:Host.put(a, c)"], False),
        ("otto.host.host:Host.put(a, b)", ["otto.host.host:Host.put(a)"], False),
        ("otto.host.host:Host.put(a, b)", [], False),
        ("otto.host.host:Host.put(a, b)", ["otto.host.host:Host.get(a, b, c)"], False),
        ("otto:Alpha", ["otto:Alpha", "otto:Beta"], False),
        ("otto.host.host:Host.put(a, b)", ["otto.host.host:Host.put_many(a, b, c)"], False),
        ("otto.host.host:Host.put(a, b)", ["otto.host.host:Host.put(a, bb)"], False),
    ],
    ids=[
        "trailing-addition-widens",
        "reorder-does-not-widen",
        "rename-does-not-widen",
        "shortened-does-not-widen",
        "no-added-counterpart",
        "different-method-does-not-widen",
        "non-protocol-line-never-widens",
        "a-longer-method-name-does-not-widen",
        "a-lengthened-parameter-name-does-not-widen",
    ],
)
def test_is_widened_protocol_line(removed, added, expected):
    assert is_widened_protocol_line(removed, added) is expected
