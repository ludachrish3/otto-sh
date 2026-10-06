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

import sys

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

pytestmark = pytest.mark.interpreter_agnostic

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


def test_commits_in_range_includes_merges(tmp_path):
    repo = TmpGitRepo(tmp_path)
    base = _seed(repo, ["otto:Alpha"])
    repo.git("checkout", "-b", "side")
    repo.write("unrelated.txt", "x")
    side_tip = repo.commit("chore: side")
    repo.git("checkout", "main")
    repo.write("more.txt", "y")
    repo.commit("chore: main")
    repo.git("merge", "--no-ff", "-m", "merge: side", side_tip)
    merge_tip = repo.git("rev-parse", "HEAD").strip()
    assert merge_tip in commits_in_range(repo.root, f"{base}..{merge_tip}")


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


from scripts import api_regen

OTTO_ONLY = '[namespaces."otto"]\ntier = 1\nstability = "provisional"\n'
WITH_HOST = OTTO_ONLY + '\n[namespaces."otto.host"]\ntier = 1\nstability = "provisional"\n'


def _commit_dump(
    repo, files, msg, *, manifest=OTTO_ONLY, edit=None, manifest_path="api/public.toml"
):
    """Commit *files* under src/, the manifest, and the dump generated from them.

    *edit*, when given, rewrites the dump text before it is committed: the way a
    test plants a stale or re-sorted golden.
    """
    repo.write(manifest_path, manifest)
    for rel, text in files.items():
        repo.write(f"src/{rel}", text)
    generated = api_regen.generate_worktree(repo.root, repo.root / manifest_path)
    assert generated.text is not None, generated.refusals
    repo.write("golden.txt", generated.text if edit is None else edit(generated.text))
    return repo.commit(msg)


def _init(names, body):
    return {"otto/__init__.py": f"__all__ = {names!r}\n{body}"}


def _run(repo, rev_range, manifest_path=None):
    args = ["--repo", str(repo.root), "--golden", "golden.txt", "--env", "current"]
    if manifest_path is not None:
        args += ["--manifest", manifest_path]
    return main([rev_range, *args])


def test_v2_binding_removal_unmarked_fails(tmp_path, capsys):
    repo = TmpGitRepo(tmp_path)
    _commit_dump(repo, _init(["f", "g"], "def f(): pass\ndef g(): pass\n"), "feat: f and g")
    tip = _commit_dump(repo, _init(["f"], "def f(): pass\n"), "refactor(api): drop g")
    assert _run(repo, f"{tip}~1..{tip}") == 1
    assert "g: removed [otto]" in capsys.readouterr().out


def test_v2_binding_removal_marked_passes(tmp_path):
    repo = TmpGitRepo(tmp_path)
    _commit_dump(repo, _init(["f", "g"], "def f(): pass\ndef g(): pass\n"), "feat: f and g")
    tip = _commit_dump(repo, _init(["f"], "def f(): pass\n"), "refactor(api)!: drop g")
    assert _run(repo, f"{tip}~1..{tip}") == 0


def test_v2_shape_break_unmarked_fails(tmp_path, capsys):
    repo = TmpGitRepo(tmp_path)
    _commit_dump(repo, _init(["f"], "def f(a): pass\n"), "feat: f")
    tip = _commit_dump(repo, _init(["f"], "def f(a, b): pass\n"), "feat: f takes b")
    assert _run(repo, f"{tip}~1..{tip}") == 1
    assert "f: new required parameter b [otto]" in capsys.readouterr().out


def test_v2_safe_widening_passes_unmarked(tmp_path):
    repo = TmpGitRepo(tmp_path)
    _commit_dump(repo, _init(["f"], "def f(a): pass\n"), "feat: f")
    tip = _commit_dump(repo, _init(["f"], "def f(a, *, b=0): pass\n"), "feat: f takes b")
    assert _run(repo, f"{tip}~1..{tip}") == 0


def test_stale_dump_is_refused_even_when_marked(tmp_path, capsys):
    repo = TmpGitRepo(tmp_path)
    first = _commit_dump(repo, _init(["f"], "def f(a): pass\n"), "feat: f")
    old = repo.git("show", f"{first}:golden.txt")
    tip = _commit_dump(
        repo, _init(["f"], "def f(): pass\n"), "feat(api)!: f loses a", edit=lambda text: old
    )
    assert _run(repo, f"{tip}~1..{tip}") == 1
    assert "refused: stale dump" in capsys.readouterr().out


def test_resorted_dump_is_refused_by_freshness_but_has_no_compat_finding(tmp_path, capsys):
    repo = TmpGitRepo(tmp_path)
    files = _init(["f", "g"], "def f(): pass\ndef g(): pass\n")
    _commit_dump(repo, files, "feat: f and g")

    def resort(text):
        header, schema, body = text.split("\n", 2)
        return f"{header}\n{schema}\n" + "".join(reversed(body.splitlines(keepends=True)))

    tip = _commit_dump(repo, files, "chore: touch", edit=resort)
    assert _run(repo, f"{tip}~1..{tip}") == 1
    out = capsys.readouterr().out
    assert "refused: stale dump" in out
    assert "removed" not in out
    assert "new required" not in out


def test_intermediate_stale_dump_is_flagged_on_its_own_commit(tmp_path, capsys):
    repo = TmpGitRepo(tmp_path)
    base = _commit_dump(repo, _init(["f"], "def f(a): pass\n"), "feat: f")
    old = repo.git("show", f"{base}:golden.txt")
    stale = _commit_dump(
        repo, _init(["f"], "def f(): pass\n"), "feat(api)!: drop a", edit=lambda text: old
    )
    _commit_dump(repo, _init(["f"], "def f(): pass\n"), "chore: fix the golden")
    assert _run(repo, f"{base}..HEAD") == 1
    out = capsys.readouterr().out
    assert f"commit {stale}" in out
    assert "stale dump" in out


def test_namespace_import_failure_is_refused_even_when_marked(tmp_path, capsys):
    repo = TmpGitRepo(tmp_path)
    first = _commit_dump(repo, _init(["f"], "def f(): pass\n"), "feat: f")
    old = repo.git("show", f"{first}:golden.txt")
    repo.write("src/otto/__init__.py", "1/0\n")
    repo.write("golden.txt", old)
    tip = repo.commit("feat(api)!: break the import")
    assert _run(repo, f"{tip}~1..{tip}") == 1
    assert "cannot regenerate the dump" in capsys.readouterr().out


def test_unknown_producer_schema_is_refused(tmp_path, capsys):
    repo = TmpGitRepo(tmp_path)
    _commit_dump(repo, _init(["f"], "def f(): pass\n"), "feat: f")
    tip = _commit_dump(
        repo,
        _init(["f"], "def f(): pass\n"),
        "chore!: schema",
        edit=lambda text: text.replace("# producer-schema 1", "# producer-schema 2"),
    )
    assert _run(repo, f"{tip}~1..{tip}") == 1
    assert "is not this checker's schema" in capsys.readouterr().out


def test_producer_schema_decrease_through_a_merged_parent_is_refused(tmp_path, capsys):
    repo = TmpGitRepo(tmp_path)
    base = _commit_dump(repo, _init(["f"], "def f(): pass\n"), "feat: f")
    repo.git("checkout", "-q", "-b", "side")
    repo.write(
        "golden.txt",
        repo.git("show", f"{base}:golden.txt").replace(
            "# producer-schema 1", "# producer-schema 2"
        ),
    )
    repo.commit("chore: a newer producer")
    repo.git("checkout", "-q", "main")
    repo.git("merge", "-q", "--no-ff", "-s", "ours", "-m", "chore!: merge side", "side")
    merge = repo.git("rev-parse", "HEAD").strip()
    assert _run(repo, f"{merge}~1..{merge}") == 1
    assert "producer-schema decreased" in capsys.readouterr().out


def test_merge_regenerates_only_its_own_dump(tmp_path, capsys):
    repo = TmpGitRepo(tmp_path)
    base = _commit_dump(repo, _init(["f"], "def f(): pass\n"), "feat: f")
    repo.git("checkout", "-q", "-b", "side")
    old = repo.git("show", f"{base}:golden.txt")
    stale = _commit_dump(
        repo, _init(["f", "g"], "def f(): pass\ndef g(): pass\n"), "feat: g", edit=lambda text: old
    )
    repo.git("checkout", "-q", "main")
    repo.git("merge", "-q", "--no-ff", "-m", "merge side", "side")
    _commit_dump(repo, _init(["f", "g"], "def f(): pass\ndef g(): pass\n"), "chore: regenerate")
    merge = repo.git("rev-parse", "HEAD~1").strip()
    assert _run(repo, f"{base}..HEAD") == 1
    out = capsys.readouterr().out
    # The side commit is flagged on its own sha. The merge inherits the side's
    # stale golden with the side's code, so its OWN regeneration is stale too:
    # two refusals, one per commit, and the regenerating tip passes.
    assert f"commit {stale}" in out
    assert f"commit {merge}" in out
    assert out.count("refused: stale dump") == 2


def test_harmless_v2_merge_passes(tmp_path):
    repo = TmpGitRepo(tmp_path)
    base = _commit_dump(repo, _init(["f"], "def f(): pass\n"), "feat: f")
    repo.git("checkout", "-q", "-b", "side")
    repo.write("README", "x\n")
    repo.commit("docs: readme")
    repo.git("checkout", "-q", "main")
    repo.git("merge", "-q", "--no-ff", "-m", "merge side", "side")
    assert _run(repo, f"{base}..HEAD") == 0


def test_merge_only_removal_needs_a_mark_on_the_merge(tmp_path, capsys):
    repo = TmpGitRepo(tmp_path)
    _commit_dump(repo, _init(["f", "g"], "def f(): pass\ndef g(): pass\n"), "feat: f g")
    repo.git("checkout", "-q", "-b", "side")
    repo.write("README", "x\n")
    repo.commit("docs: readme")
    repo.git("checkout", "-q", "main")
    repo.git("merge", "-q", "--no-ff", "--no-commit", "side")
    _commit_dump(repo, _init(["f"], "def f(): pass\n"), "merge side")
    merge = repo.git("rev-parse", "HEAD").strip()
    assert _run(repo, f"{merge}~1..{merge}") == 1
    assert "g: removed [otto]" in capsys.readouterr().out


def test_a_break_restored_by_a_later_commit_is_still_flagged_on_its_own_commit(tmp_path, capsys):
    repo = TmpGitRepo(tmp_path)
    base = _commit_dump(repo, _init(["f", "g"], "def f(): pass\ndef g(): pass\n"), "feat: f g")
    broke = _commit_dump(repo, _init(["f"], "def f(): pass\n"), "refactor: drop g")
    _commit_dump(repo, _init(["f", "g"], "def f(): pass\ndef g(): pass\n"), "fix: g is back")
    assert _run(repo, f"{base}..HEAD") == 1
    out = capsys.readouterr().out
    assert f"commit {broke}" in out
    assert "g: removed [otto]" in out


def test_a_v1_first_v2_second_merge_that_keeps_v2_takes_the_conversion_path(tmp_path):
    repo = TmpGitRepo(tmp_path)
    repo.write("golden.txt", "otto:f\n")
    base = repo.commit("chore: v1")
    repo.git("checkout", "-q", "-b", "side")
    _commit_dump(repo, _init(["f"], "def f(): pass\n"), "build(api): v2")
    repo.git("checkout", "-q", "main")
    repo.write("README", "x\n")
    repo.commit("docs: readme")
    repo.git("merge", "-q", "--no-ff", "-m", "merge side", "side")
    assert _run(repo, f"{base}..HEAD") == 0


def test_merge_that_leaves_v2_is_refused_even_when_marked(tmp_path, capsys):
    repo = TmpGitRepo(tmp_path)
    repo.write("golden.txt", "otto:f\n")
    repo.commit("chore: v1")
    repo.git("checkout", "-q", "-b", "side")
    _commit_dump(repo, _init(["f"], "def f(): pass\n"), "build(api)!: v2")
    repo.git("checkout", "-q", "main")
    repo.git("merge", "-q", "--no-ff", "-s", "ours", "-m", "chore!: merge side", "side")
    merge = repo.git("rev-parse", "HEAD").strip()
    assert _run(repo, f"{merge}~1..{merge}") == 1
    assert "merge leaves api-snapshot v2" in capsys.readouterr().out


def test_rollback_from_v2_to_v1_is_refused_even_when_marked(tmp_path, capsys):
    repo = TmpGitRepo(tmp_path)
    _commit_dump(repo, _init(["f"], "def f(): pass\n"), "feat: f")
    repo.write("golden.txt", "otto:f\n")
    tip = repo.commit("revert(api)!: back to v1")
    assert _run(repo, f"{tip}~1..{tip}") == 1
    assert "rollback from api-snapshot v2 to v1" in capsys.readouterr().out


def test_v2_golden_deleted_is_refused_even_when_marked(tmp_path, capsys):
    repo = TmpGitRepo(tmp_path)
    _commit_dump(repo, _init(["f"], "def f(): pass\n"), "feat: f")
    repo.git("rm", "-q", "golden.txt")
    tip = repo.commit("chore(api)!: drop the golden")
    assert _run(repo, f"{tip}~1..{tip}") == 1
    assert "v2 golden deleted" in capsys.readouterr().out


def test_two_commit_rollback_is_refused_at_the_deletion(tmp_path, capsys):
    repo = TmpGitRepo(tmp_path)
    base = _commit_dump(repo, _init(["f"], "def f(): pass\n"), "feat: f")
    repo.git("rm", "-q", "golden.txt")
    deletion = repo.commit("chore(api)!: drop the golden")
    repo.write("golden.txt", "otto:f\n")
    repo.commit("chore(api)!: a v1 golden")
    assert _run(repo, f"{base}..HEAD") == 1
    assert f"commit {deletion}" in capsys.readouterr().out


def test_v2_commit_with_malformed_manifest_is_refused_even_when_marked(tmp_path, capsys):
    repo = TmpGitRepo(tmp_path)
    _commit_dump(repo, _init(["f"], "def f(): pass\n"), "feat: f")
    repo.write("api/public.toml", "not = [valid")
    tip = repo.commit("chore(api)!: break the manifest")
    assert _run(repo, f"{tip}~1..{tip}") == 1
    assert "missing or malformed" in capsys.readouterr().out


def test_manifest_only_downgrade_needs_a_mark(tmp_path, capsys):
    repo = TmpGitRepo(tmp_path)
    stable = OTTO_ONLY.replace("provisional", "stable")
    _commit_dump(repo, _init(["f"], "def f(): pass\n"), "feat: f", manifest=stable)
    tip = _commit_dump(repo, _init(["f"], "def f(): pass\n"), "chore: provisional again")
    assert _run(repo, f"{tip}~1..{tip}") == 1
    assert "stable" in capsys.readouterr().out


def test_manifest_without_v2_golden_changes_nothing(tmp_path):
    repo = TmpGitRepo(tmp_path)
    repo.write("golden.txt", "otto:f\n")
    base = repo.commit("chore: v1")
    repo.write("api/public.toml", "not = [valid")
    repo.commit("chore: an early manifest")
    assert _run(repo, f"{base}..HEAD") == 0


HOST = (
    "from typing import Protocol\n__all__ = ['Host']\n"
    "class Host(Protocol):\n    def put(self, {params}): ...\n"
)


def _convert(
    tmp_path, v1_lines, put_params, msg="build(api): switch to the dump", manifest=WITH_HOST
):
    repo = TmpGitRepo(tmp_path)
    repo.write("golden.txt", "\n".join(v1_lines) + "\n")
    repo.commit("chore: v1")
    files = {
        **_init(["f"], "def f(): pass\n"),
        "otto/host/__init__.py": HOST.format(params=put_params),
    }
    tip = _commit_dump(repo, files, msg, manifest=manifest)
    return repo, tip


V1 = ["otto:f", "otto.host.host:Host.put(src_files, dest_dir)"]


def test_conversion_carrying_every_line_needs_no_mark(tmp_path):
    repo, tip = _convert(tmp_path, V1, "src_files, dest_dir")
    assert _run(repo, f"{tip}~1..{tip}") == 0


def test_conversion_host_widening_with_an_optional_parameter_needs_no_mark(tmp_path):
    repo, tip = _convert(tmp_path, V1, "src_files, dest_dir, mode=None")
    assert _run(repo, f"{tip}~1..{tip}") == 0


def test_conversion_host_keyword_only_reorder_needs_no_mark(tmp_path):
    v1 = ["otto:f", "otto.host.host:Host.put(src_files, user, show_progress)"]
    repo, tip = _convert(tmp_path, v1, "src_files, *, show_progress=False, user=None")
    assert _run(repo, f"{tip}~1..{tip}") == 0


def test_conversion_kind_narrowing_v1_never_recorded_needs_no_mark(tmp_path):
    v1 = ["otto:f", "otto.host.host:Host.put(src_files)"]
    repo, tip = _convert(tmp_path, v1, "*, src_files")
    assert _run(repo, f"{tip}~1..{tip}") == 0


def test_conversion_host_lost_keyword_needs_a_mark(tmp_path, capsys):
    repo, tip = _convert(tmp_path, V1, "src_files")
    assert _run(repo, f"{tip}~1..{tip}") == 1
    assert "keyword dest_dir is no longer accepted" in capsys.readouterr().out


def test_conversion_host_positional_reorder_needs_a_mark(tmp_path, capsys):
    repo, tip = _convert(tmp_path, V1, "dest_dir, src_files")
    assert _run(repo, f"{tip}~1..{tip}") == 1
    assert "positional order changed" in capsys.readouterr().out


def test_conversion_host_mid_insertion_of_a_positional_needs_a_mark(tmp_path, capsys):
    repo, tip = _convert(tmp_path, V1, "src_files, mode=None, dest_dir=None")
    assert _run(repo, f"{tip}~1..{tip}") == 1
    assert "positional order changed" in capsys.readouterr().out


def test_conversion_host_front_positional_only_insertion_needs_a_mark(tmp_path, capsys):
    repo, tip = _convert(tmp_path, V1, "x=None, /, src_files=None, dest_dir=None")
    assert _run(repo, f"{tip}~1..{tip}") == 1
    assert "positional order changed" in capsys.readouterr().out


def test_conversion_host_new_required_parameter_needs_a_mark(tmp_path, capsys):
    repo, tip = _convert(tmp_path, V1, "src_files, dest_dir, mode")
    assert _run(repo, f"{tip}~1..{tip}") == 1
    assert "new required parameter mode" in capsys.readouterr().out


def test_conversion_host_new_required_positional_only_parameter_needs_a_mark(tmp_path, capsys):
    # v1 recorded no positional-or-keyword name here, so no slot order constrains it.
    v1 = ["otto:f", "otto.host.host:Host.put(timeout)"]
    repo, tip = _convert(tmp_path, v1, "newpo, /, *, timeout=5")
    assert _run(repo, f"{tip}~1..{tip}") == 1
    assert "new required parameter newpo" in capsys.readouterr().out


def test_conversion_host_optional_positional_only_parameter_needs_no_mark(tmp_path):
    v1 = ["otto:f", "otto.host.host:Host.put(timeout)"]
    repo, tip = _convert(tmp_path, v1, "newpo=None, /, *, timeout=5")
    assert _run(repo, f"{tip}~1..{tip}") == 0


def test_conversion_host_method_missing_needs_a_mark(tmp_path, capsys):
    v1 = [*V1, "otto.host.host:Host.get(src_files)"]
    repo, tip = _convert(tmp_path, v1, "src_files, dest_dir")
    assert _run(repo, f"{tip}~1..{tip}") == 1
    assert "otto.host:Host.get" in capsys.readouterr().out


def test_conversion_dropping_a_still_importable_deep_line_needs_a_mark(tmp_path, capsys):
    repo, tip = _convert(tmp_path, [*V1, "json:dumps"], "src_files, dest_dir")
    assert _run(repo, f"{tip}~1..{tip}") == 1
    assert "json:dumps -- not carried into the v2 golden" in capsys.readouterr().out


def test_conversion_bare_line_needs_its_namespace_in_the_manifest(tmp_path, capsys):
    repo, tip = _convert(tmp_path, [*V1, "otto.docker:"], "src_files, dest_dir")
    assert _run(repo, f"{tip}~1..{tip}") == 1
    assert "no namespace entry for otto.docker" in capsys.readouterr().out


def test_conversion_dropping_a_root_line_needs_a_mark(tmp_path, capsys):
    repo, tip = _convert(tmp_path, [*V1, "otto:Gone"], "src_files, dest_dir")
    assert _run(repo, f"{tip}~1..{tip}") == 1
    assert "otto:Gone -- not carried" in capsys.readouterr().out


def test_conversion_marked_passes(tmp_path):
    repo, tip = _convert(
        tmp_path, [*V1, "otto:Gone"], "src_files, dest_dir", msg="build(api)!: switch to the dump"
    )
    assert _run(repo, f"{tip}~1..{tip}") == 0


@pytest.mark.parametrize(
    "mark", ["feat(api)!: drop g", "feat: drop g\n\nBREAKING CHANGE: g is gone"]
)
def test_both_mark_forms_excuse_a_v2_finding_but_never_a_refusal(tmp_path, mark):
    repo = TmpGitRepo(tmp_path)
    first = _commit_dump(repo, _init(["f", "g"], "def f(): pass\ndef g(): pass\n"), "feat: f g")
    tip = _commit_dump(repo, _init(["f"], "def f(): pass\n"), mark)
    assert _run(repo, f"{tip}~1..{tip}") == 0
    old = repo.git("show", f"{first}:golden.txt")
    stale = _commit_dump(repo, _init(["f"], "def f(): pass\n"), mark, edit=lambda text: old)
    assert _run(repo, f"{stale}~1..{stale}") == 1


CUSTOM = "meta/surface.toml"


def test_a_malformed_manifest_at_a_custom_path_is_refused_naming_it(tmp_path, capsys):
    repo = TmpGitRepo(tmp_path)
    _commit_dump(repo, _init(["f"], "def f(): pass\n"), "feat: f", manifest_path=CUSTOM)
    repo.write(CUSTOM, "not = [valid")
    tip = repo.commit("chore(api)!: break the manifest")
    assert _run(repo, f"{tip}~1..{tip}", CUSTOM) == 1
    out = capsys.readouterr().out
    assert f"{CUSTOM} missing or malformed" in out
    assert "api/public.toml" not in out


def test_a_missing_namespace_entry_names_the_custom_manifest_path(tmp_path, capsys):
    repo = TmpGitRepo(tmp_path)
    repo.write("golden.txt", "otto:f\notto.docker:\n")
    repo.commit("chore: v1")
    tip = _commit_dump(
        repo, _init(["f"], "def f(): pass\n"), "build(api): v2", manifest_path=CUSTOM
    )
    assert _run(repo, f"{tip}~1..{tip}", CUSTOM) == 1
    out = capsys.readouterr().out
    assert f"no namespace entry for otto.docker in {CUSTOM}" in out
    assert "api/public.toml" not in out


def test_regeneration_reads_the_custom_manifest_path(tmp_path):
    repo = TmpGitRepo(tmp_path)
    _commit_dump(
        repo, _init(["f", "g"], "def f(): pass\ndef g(): pass\n"), "feat: f g", manifest_path=CUSTOM
    )
    tip = _commit_dump(
        repo,
        _init(["f", "g"], "def f(): pass\ndef g(): pass\n\nh = 1\n"),
        "chore: touch",
        manifest_path=CUSTOM,
    )
    assert not (repo.root / "api" / "public.toml").exists()
    assert _run(repo, f"{tip}~1..{tip}", CUSTOM) == 0


def test_a_merge_whose_only_change_downgrades_a_namespace_needs_a_mark_on_the_merge(
    tmp_path, capsys
):
    repo = TmpGitRepo(tmp_path)
    stable = OTTO_ONLY.replace("provisional", "stable")
    base = _commit_dump(repo, _init(["f"], "def f(): pass\n"), "feat: f", manifest=stable)
    repo.git("checkout", "-q", "-b", "side")
    side = _commit_dump(repo, _init(["f"], "def f(): pass\n"), "docs(api)!: provisional")
    repo.git("checkout", "-q", "main")
    repo.write("README", "x\n")
    repo.commit("docs: main moves on")
    repo.git("merge", "-q", "--no-ff", "-m", "merge side", side)
    merge = repo.git("rev-parse", "HEAD").strip()
    assert _run(repo, f"{base}..HEAD") == 1
    out = capsys.readouterr().out
    assert f"commit {merge} merge side" in out
    assert f"commit {side}" not in out
    assert "stable" in out


def test_a_deletion_after_the_conversion_in_one_range_is_judged_by_v2_rules(tmp_path, capsys):
    repo = TmpGitRepo(tmp_path)
    repo.write("golden.txt", "otto:f\notto:g\n")
    base = repo.commit("chore: v1")
    both = _init(["f", "g"], "def f(): pass\ndef g(): pass\n")
    conversion = _commit_dump(repo, both, "build(api): switch to the dump")
    deletion = _commit_dump(repo, _init(["f"], "def f(): pass\n"), "refactor(api): drop g")
    assert _run(repo, f"{base}..HEAD") == 1
    out = capsys.readouterr().out
    assert f"commit {deletion}" in out
    assert "g: removed [otto]" in out
    assert f"commit {conversion}" not in out


FORMATS = OTTO_ONLY + '\n[formats.store]\nreads = "otto.versions:READS"\n'


def _versions(reads):
    return {**_init(["f"], "def f(): pass\n"), "otto/versions.py": f"READS = {reads!r}\n"}


@pytest.mark.parametrize(("msg", "code"), [("feat: drop v7", 1), ("feat!: drop v7", 0)])
def test_a_dropped_read_version_needs_a_mark(tmp_path, capsys, msg, code):
    repo = TmpGitRepo(tmp_path)
    _commit_dump(repo, _versions([7, 8]), "feat: store", manifest=FORMATS)
    tip = _commit_dump(repo, _versions([8]), msg, manifest=FORMATS)
    assert _run(repo, f"{tip}~1..{tip}") == code
    if code:
        assert "format store: reads no longer has I:7" in capsys.readouterr().out


def test_an_added_read_version_needs_no_mark(tmp_path):
    repo = TmpGitRepo(tmp_path)
    _commit_dump(repo, _versions([8]), "feat: store", manifest=FORMATS)
    tip = _commit_dump(repo, _versions([8, 9]), "feat: read v9", manifest=FORMATS)
    assert _run(repo, f"{tip}~1..{tip}") == 0


def test_a_format_refusal_is_not_excused_by_a_mark(tmp_path, capsys):
    repo = TmpGitRepo(tmp_path)
    first = _commit_dump(repo, _versions([8]), "feat: store", manifest=FORMATS)
    old = repo.git("show", f"{first}:golden.txt")
    repo.write("src/otto/versions.py", "READS = [8, 8]\n")
    repo.write("golden.txt", old)
    tip = repo.commit("feat!: a duplicate")
    assert _run(repo, f"{tip}~1..{tip}") == 1
    assert "duplicate version" in capsys.readouterr().out


FORMATS_RW = (
    OTTO_ONLY
    + '\n[formats.store]\nreads = "otto.versions:READS"\nwrites = "otto.versions:WRITES"\n'
)


def _rw_versions(reads, writes):
    return {
        **_init(["f"], "def f(): pass\n"),
        "otto/versions.py": f"READS = {reads!r}\nWRITES = {writes!r}\n",
    }


@pytest.mark.parametrize(("msg", "code"), [("feat: write v8 only", 1), ("feat!: write v8 only", 0)])
def test_a_dropped_write_version_needs_a_mark(tmp_path, capsys, msg, code):
    repo = TmpGitRepo(tmp_path)
    _commit_dump(repo, _rw_versions([7, 8], [7, 8]), "feat: store", manifest=FORMATS_RW)
    tip = _commit_dump(repo, _rw_versions([7, 8], [8]), msg, manifest=FORMATS_RW)
    assert _run(repo, f"{tip}~1..{tip}") == code
    out = capsys.readouterr().out
    if code:
        assert "format store: writes no longer has I:7" in out
        assert "reads no longer has" not in out


@pytest.mark.parametrize(("msg", "code"), [("feat: drop store", 1), ("feat!: drop store", 0)])
def test_a_removed_format_needs_a_mark(tmp_path, capsys, msg, code):
    repo = TmpGitRepo(tmp_path)
    _commit_dump(repo, _versions([8]), "feat: store", manifest=FORMATS)
    tip = _commit_dump(repo, _versions([8]), msg, manifest=OTTO_ONLY)
    assert _run(repo, f"{tip}~1..{tip}") == code
    if code:
        assert "format store: removed" in capsys.readouterr().out


def test_a_new_format_needs_no_mark(tmp_path):
    repo = TmpGitRepo(tmp_path)
    _commit_dump(repo, _versions([8]), "feat: versions", manifest=OTTO_ONLY)
    tip = _commit_dump(repo, _versions([8]), "feat: declare store", manifest=FORMATS)
    assert "format\tstore" in repo.git("show", f"{tip}:golden.txt")
    assert _run(repo, f"{tip}~1..{tip}") == 0


CR = chr(13)


def test_a_crlf_copy_of_a_fresh_dump_is_stale_even_when_marked(tmp_path, capsys):
    repo = TmpGitRepo(tmp_path)
    _commit_dump(repo, _init(["f"], "def f(): pass\n"), "feat: f")
    fresh = repo.git("show", "HEAD:golden.txt")
    (repo.root / "golden.txt").write_bytes(fresh.replace("\n", CR + "\n").encode())
    tip = repo.commit("chore(api)!: CRLF")
    assert _run(repo, f"{tip}~1..{tip}") == 1
    assert "refused: stale dump" in capsys.readouterr().out


def test_the_manifest_is_read_as_committed_bytes(tmp_path, capsys):
    # A lone CR is no TOML newline; a newline-translating read would repair it.
    repo = TmpGitRepo(tmp_path)
    _commit_dump(repo, _init(["f"], "def f(): pass\n"), "feat: f")
    (repo.root / "api" / "public.toml").write_bytes(OTTO_ONLY.replace("\n", CR).encode())
    tip = repo.commit("chore: CR manifest")
    assert _run(repo, f"{tip}~1..{tip}") == 1
    assert "refused: api/public.toml missing or malformed" in capsys.readouterr().out


def test_a_non_utf8_manifest_is_a_refusal_not_a_traceback(tmp_path, capsys):
    repo = TmpGitRepo(tmp_path)
    _commit_dump(repo, _init(["f"], "def f(): pass\n"), "feat: f")
    (repo.root / "api" / "public.toml").write_bytes(OTTO_ONLY.encode() + b"# \xff\n")
    tip = repo.commit("chore: latin-1 manifest")
    assert _run(repo, f"{tip}~1..{tip}") == 1
    assert "api/public.toml is not UTF-8" in capsys.readouterr().out


def _python_with(tmp_path, name, dep_default):
    """An interpreter whose environment provides ``dep.DEFAULT = dep_default``."""
    site = tmp_path / f"site-{name}"
    site.mkdir()
    (site / "dep.py").write_text(f"DEFAULT = {dep_default}\n")
    python = tmp_path / f"python-{name}"
    python.write_text(f'#!/bin/sh\nPYTHONPATH={site} exec {sys.executable} "$@"\n')
    python.chmod(0o755)
    return python


def test_each_commit_is_regenerated_under_its_own_lock(tmp_path, monkeypatch):
    pythons = {
        "lock a\n": _python_with(tmp_path, "a", 1),
        "lock b\n": _python_with(tmp_path, "b", 2),
    }
    seen = []

    class LockKeyedEnv:
        def python(self, tree):
            lock = (tree / "uv.lock").read_text()
            seen.append(lock)
            return pythons[lock]

    repo = TmpGitRepo(tmp_path / "repo")
    repo.write("README", "x\n")
    base = repo.commit("chore: seed")
    files = _init(["f"], "import dep\ndef f(a=dep.DEFAULT): pass\n")
    for lock, msg in [("lock a\n", "feat: f"), ("lock b\n", "build(deps)!: dep 2")]:
        repo.write("uv.lock", lock)
        repo.write("api/public.toml", OTTO_ONLY)
        for rel, text in files.items():
            repo.write(f"src/{rel}", text)
        generated = api_regen._generated(
            api_regen.run_child(pythons[lock], repo.root / "src", ["otto"])
        )
        assert generated.text is not None, generated.refusals
        repo.write("golden.txt", generated.text)
        repo.commit(msg)
    assert "PK:a:I:1" in repo.git("show", "HEAD~1:golden.txt")
    assert "PK:a:I:2" in repo.git("show", "HEAD:golden.txt")
    monkeypatch.setattr(api_regen, "CurrentEnv", LockKeyedEnv)
    assert _run(repo, f"{base}..HEAD") == 0
    assert seen == ["lock a\n", "lock b\n"]


def test_an_ordinary_producer_schema_decrease_is_refused_even_when_marked(tmp_path, capsys):
    repo = TmpGitRepo(tmp_path)
    files = _init(["f"], "def f(): pass\n")
    _commit_dump(
        repo,
        files,
        "feat: f",
        edit=lambda text: text.replace("# producer-schema 1", "# producer-schema 2"),
    )
    tip = _commit_dump(repo, files, "chore(api)!: schema 1 again")
    assert _run(repo, f"{tip}~1..{tip}") == 1
    assert "producer-schema decreased from 2" in capsys.readouterr().out


def test_each_commit_in_a_merged_range_is_regenerated_once_as_itself(tmp_path, monkeypatch):
    repo = TmpGitRepo(tmp_path)
    base = _commit_dump(repo, _init(["f"], "def f(): pass\n"), "feat: f")
    repo.git("checkout", "-q", "-b", "side")
    side_g = _commit_dump(repo, _init(["f", "g"], "def f(): pass\ndef g(): pass\n"), "feat: g")
    side_h = _commit_dump(
        repo, _init(["f", "g", "h"], "def f(): pass\ndef g(): pass\ndef h(): pass\n"), "feat: h"
    )
    repo.git("checkout", "-q", "main")
    repo.write("README", "x\n")
    main_doc = repo.commit("docs: readme")
    repo.git("merge", "-q", "--no-ff", "-m", "merge side", "side")
    merge = repo.git("rev-parse", "HEAD").strip()
    calls = []
    real = api_regen.generate_at_commit

    def spy(repo_root, sha, *args, **kwargs):
        calls.append(sha)
        return real(repo_root, sha, *args, **kwargs)

    monkeypatch.setattr(api_regen, "generate_at_commit", spy)
    assert _run(repo, f"{base}..HEAD") == 0
    assert sorted(calls) == sorted([side_g, side_h, main_doc, merge])


def test_a_marked_environment_refusal_still_fails(tmp_path, capsys):
    repo = TmpGitRepo(tmp_path)
    _commit_dump(repo, _init(["f"], "def f(): pass\n"), "feat: f")
    repo.write("src/otto/__init__.py", "import os\nos._exit(3)\n")
    tip = repo.commit("feat(api)!: the import crashes")
    assert _run(repo, f"{tip}~1..{tip}") == 1
    out = capsys.readouterr().out
    assert "refused: cannot regenerate the dump" in out
    assert "environment: the dump child crashed" in out


def test_a_marked_provenance_refusal_still_fails(tmp_path, capsys):
    repo = TmpGitRepo(tmp_path)
    _commit_dump(repo, _init(["f"], "def f(): pass\n"), "feat: f")
    ghost = "import sys, types\nsys.modules['otto.ghost'] = types.ModuleType('otto.ghost')\n"
    repo.write("src/otto/__init__.py", ghost + "__all__ = ['f']\ndef f(): pass\n")
    tip = repo.commit("feat(api)!: a ghost module")
    assert _run(repo, f"{tip}~1..{tip}") == 1
    out = capsys.readouterr().out
    assert "refused: cannot regenerate the dump" in out
    assert "provenance: otto.ghost: no __file__ or __path__" in out


def test_a_marked_unparseable_dump_still_fails(tmp_path, capsys):
    repo = TmpGitRepo(tmp_path)
    files = _init(["f"], "def f(): pass\n")
    _commit_dump(repo, files, "feat: f")
    tip = _commit_dump(repo, files, "feat(api)!: f", edit=lambda text: text + "junk\n")
    assert _run(repo, f"{tip}~1..{tip}") == 1
    assert "refused: the dump does not parse" in capsys.readouterr().out


def test_a_marked_commit_over_an_unparseable_parent_dump_still_fails(tmp_path, capsys):
    repo = TmpGitRepo(tmp_path)
    files = _init(["f"], "def f(): pass\n")
    _commit_dump(repo, files, "feat: f", edit=lambda text: text + "junk\n")
    tip = _commit_dump(repo, files, "fix(api)!: a fresh dump")
    assert _run(repo, f"{tip}~1..{tip}") == 1
    assert "refused: the parent's dump does not parse" in capsys.readouterr().out


from scripts import api_records

DUMP_SPEC = api_regen.REPO_ROOT / "docs" / "superpowers" / "specs" / "2026-10-05-api-dump-design.md"


def _schema_table_rows(spec_text):
    """Return the Schema cells of the table in the dump spec's §5.4."""
    section = spec_text.split("\n### 5.4 ", 1)[1].split("\n### ", 1)[0]
    rows = [line for line in section.splitlines() if line.startswith("|")]
    assert rows[0].strip("|").split("|")[0].strip() == "Schema", rows
    return [row.strip("|").split("|")[0].strip() for row in rows[2:]]


def test_the_checker_s_producer_schema_has_a_row_in_the_spec_table():
    # A schema bump "must add a row for the new number to the table" (dump spec §5.4).
    rows = _schema_table_rows(DUMP_SPEC.read_text(encoding="utf-8"))
    assert str(api_records.PRODUCER_SCHEMA) in rows, rows


# An older producer, as a historical commit might carry it: it reports nothing.
OLD_PRODUCER = (
    "import json, sys\n"
    'print(json.dumps({"records": [], "refusals": [], "provenance": [], "private": {}}))\n'
)


def test_historical_commits_are_regenerated_by_the_checker_s_own_producer(tmp_path):
    repo = TmpGitRepo(tmp_path)
    repo.write("README", "x\n")
    base = repo.commit("chore: seed")
    repo.write("scripts/api_dump_child.py", OLD_PRODUCER)
    historical = _commit_dump(repo, _init(["f"], "def f(): pass\n"), "feat: f")
    repo.write("scripts/api_dump_child.py", OLD_PRODUCER + "# maintained\n")
    repo.commit("refactor: producer maintenance")
    committed = repo.git("show", f"{historical}:golden.txt")
    assert api_records.producer_schema_of(committed) == api_records.PRODUCER_SCHEMA
    assert "name\totto:f\tfunction\n" in committed
    assert api_regen.CHILD == api_regen.REPO_ROOT / "scripts" / "api_dump_child.py"
    assert _run(repo, f"{base}..HEAD") == 0
