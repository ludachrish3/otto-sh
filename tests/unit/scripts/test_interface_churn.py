"""``scripts/interface_churn.py``: the per-window interface measurement."""

from pathlib import Path

import pytest

from scripts import api_compat, api_records, interface_churn
from scripts.api_records import Record
from tests._fixtures.gitrepo import TmpGitRepo

pytestmark = pytest.mark.interpreter_agnostic

GOLDEN = Path("golden.txt")
V1_HEADER = "# a v1 golden: every data line is a name\n"


def _function(key: str, params: str) -> "list[Record]":
    return [Record("name", key, ["function"]), Record("call", key, ["sync", params])]


def _model(key: str, inputs: "list[str]") -> "list[Record]":
    records = [
        Record("name", key, ["model"]),
        Record("mro", key, ["-"]),
        Record("abstract", key, ["-"]),
    ]
    for name in inputs:
        encoded = f'S:"{name}"'
        records.append(Record("input", key, [encoded, "optional", "N", encoded]))
        records.append(Record("member", f"{key}.{name}", ["field"]))
    return records


def _dump(*groups: "list[Record]") -> str:
    return api_records.render_dump([rec for group in groups for rec in group])


_FORMAT = [Record(api_records.FORMAT, "check-report", ["-", 'S:"otto-check/1"'])]
_OLD = _dump(
    _function("otto:get_host", "PK:host_id:- KO:term:N"),
    _function("otto.lab:get_host", "PK:host_id:- KO:term:N"),
    _function("otto:gone", "-"),
    _model("otto.models:Settings", ["env", "lab"]),
    _FORMAT,
)
_NEW = _dump(
    _function("otto:get_host", "PK:host_id:-"),
    _function("otto.lab:get_host", "PK:host_id:-"),
    _function("otto:fresh", "-"),
    _model("otto.models:Settings", ["lab"]),
)


def test_sites_are_what_a_finding_can_name():
    old = api_records.parse_dump(_OLD)
    settings = "otto.models:Settings"
    assert api_compat.sites(old) == {
        "otto:get_host": "otto:get_host",
        "otto.lab:get_host": "otto.lab:get_host",
        "otto:gone": "otto:gone",
        settings: settings,
        "otto.models:Settings.env": settings,
        "otto.models:Settings.lab": settings,
        'otto.models:Settings input S:"env"': settings,
        'otto.models:Settings input S:"lab"': settings,
        "format check-report": "format check-report",
    }
    findings = api_compat.compare_dumps(old, api_records.parse_dump(_NEW))
    named = {api_compat.site_of(f) for f in findings}
    assert named == {
        "otto:get_host",
        "otto.lab:get_host",
        "otto:gone",
        "otto.models:Settings.env",
        'otto.models:Settings input S:"env"',
        "format check-report",
    }
    assert named <= api_compat.sites(old).keys()


@pytest.mark.parametrize(
    ("site", "namespace"),
    [
        ("otto:get_host", "otto"),
        ("otto.models:Settings.env", "otto.models"),
        ('otto.models:Settings input S:"env"', "otto.models"),
        ("format check-report", api_compat.FORMATS_NAMESPACE),
    ],
)
def test_namespace_of_a_site(site, namespace):
    assert api_compat.namespace_of(site) == namespace


def test_two_v2_goldens_are_judged_by_the_breaking_marks_judge():
    recorded = interface_churn.compare_goldens(_OLD, _NEW)
    assert recorded is not None
    assert recorded.surface == 9
    assert len(recorded.broken) == 6
    assert recorded.added == 1, "otto:fresh"
    assert recorded.churn == pytest.approx(100 * 6 / 9)
    assert recorded.findings is not None
    assert len(recorded.findings) == 6
    # The same keyword removed under two namespaces is two findings and one distinct one.
    assert len(api_compat.group_findings(recorded.findings)) == 5


def test_a_new_required_input_breaks_its_binding_and_churn_stays_a_share():
    """The finding names an input only the newer golden has: the binding that gained it is
    the broken site, so `broken` never leaves the older surface."""
    old = _dump(_model("otto.models:S", ["a"]))
    grown = _model("otto.models:S", ["a"])
    for name in ("b", "c"):
        grown.append(Record("input", "otto.models:S", [f'S:"{name}"', "required", "-", "-"]))
    recorded = interface_churn.compare_goldens(old, _dump(grown))
    assert recorded is not None
    assert recorded.findings is not None
    assert len(recorded.findings) == 2
    assert recorded.broken == ["otto.models:S"]
    assert recorded.surface == 3
    assert recorded.added == 2
    assert recorded.churn == pytest.approx(100 / 3)


def test_a_removed_binding_breaks_every_site_it_owned():
    keep = _function("otto:keep", "-")
    old = _dump(keep, _model("otto.models:Gone", ["a", "b"]))
    recorded = interface_churn.compare_goldens(old, _dump(keep))
    assert recorded is not None
    assert recorded.findings == ["otto.models:Gone: removed"]
    assert recorded.broken == [
        "otto.models:Gone",
        'otto.models:Gone input S:"a"',
        'otto.models:Gone input S:"b"',
        "otto.models:Gone.a",
        "otto.models:Gone.b",
    ]
    assert (recorded.surface, recorded.churn) == (6, pytest.approx(100 * 5 / 6))


def test_a_removed_binding_whose_spelling_survives_as_a_member_still_takes_its_sites():
    """A nested class replaced by a plain attribute: `Outer.Inner` is no longer a binding,
    though the newer golden still spells `Outer.Inner` as a member of `Outer`."""

    def klass(key: str, members: "list[tuple[str, list[str]]]") -> "list[Record]":
        records = [Record("name", key, ["class"]), Record("mro", key, ["-"])]
        records += [Record("call", key, ["sync", "-"]), Record("abstract", key, ["-"])]
        return records + [Record("member", f"{key}.{name}", fields) for name, fields in members]

    run = ["method", "sync", "-"]
    old = _dump(
        klass("otto:Outer", [("Inner", ["attribute"])]),
        [Record("name", "otto:Outer.Inner", ["class"]), Record("mro", "otto:Outer.Inner", ["-"])],
        [Record("call", "otto:Outer.Inner", ["sync", "-"])],
        [Record("abstract", "otto:Outer.Inner", ["-"])],
        [Record("member", "otto:Outer.Inner.run", run)],
    )
    new = _dump(klass("otto:Outer", [("Inner", ["attribute"])]))
    recorded = interface_churn.compare_goldens(old, new)
    assert recorded is not None
    assert recorded.broken == ["otto:Outer.Inner", "otto:Outer.Inner.run"]


def test_an_additive_window_breaks_nothing():
    grown = _dump(_function("otto:f", "PK:x:- KO:y:N"), _function("otto:g", "-"))
    recorded = interface_churn.compare_goldens(_dump(_function("otto:f", "PK:x:-")), grown)
    assert recorded is not None
    assert (recorded.broken, recorded.findings, recorded.added) == ([], [], 1)
    assert recorded.churn == 0.0


def test_two_v1_goldens_compare_by_line():
    old = V1_HEADER + "otto:Alpha\notto:Beta\notto.host.host:Host.run(cmds)\n"
    new = V1_HEADER + "otto:Alpha\notto:Gamma\notto.host.host:Host.run(cmds, timeout)\n"
    recorded = interface_churn.compare_goldens(old, new)
    assert recorded is not None
    assert recorded.surface == 3
    assert recorded.broken == ["otto.host.host:Host.run(cmds)", "otto:Beta"]
    assert recorded.added == 2
    assert recorded.findings is None


@pytest.mark.parametrize(
    ("old", "new"),
    [
        (None, _NEW),
        (_OLD, None),
        (V1_HEADER + "otto:Alpha\n", _NEW),
        (_OLD, _NEW.replace("\tfunction\n", "\tno-such-kind\n", 1)),
    ],
    ids=["no-old-golden", "no-new-golden", "the-cutover", "unparsable"],
)
def test_a_window_without_two_goldens_of_one_schema_is_not_comparable(old, new):
    assert interface_churn.compare_goldens(old, new) is None


def _history(tmp_path) -> TmpGitRepo:
    """v0.1.0 (no golden), v0.2.0 (the old golden), then two commits, one of them marked."""
    repo = TmpGitRepo(tmp_path)
    repo.write("src/otto/cli.py", 'A = "--old-flag"\nB = "--keep/--no-keep"\nE = "OTTO_OLD"\n')
    repo.commit("feat: first")
    repo.git("tag", "v0.1.0")
    repo.write(str(GOLDEN), _OLD)
    repo.commit("feat(api): record the surface")
    repo.git("tag", "v0.2.0")
    repo.write(str(GOLDEN), _NEW)
    repo.write(
        "src/otto/cli.py",
        'B = "--keep/--no-keep"\nC = "--new-flag"\n'
        'E = "OTTO_NEW_VAR"\nP = "OTTOPATH"\nX = "OTTOMAN"\n',
    )
    repo.commit("feat(api)!: drop the term override")
    repo.write("notes.txt", "x\n")
    repo.commit("fix: an unmarked fix")
    return repo


def test_default_revs_are_every_release_tag_then_head_when_it_is_ahead(tmp_path):
    repo = _history(tmp_path)
    repo.git("tag", "archive-marker")
    assert interface_churn.default_revs(repo.root) == ["v0.1.0", "v0.2.0", "HEAD"]
    repo.git("tag", "v0.10.0")
    assert interface_churn.default_revs(repo.root) == ["v0.1.0", "v0.2.0", "v0.10.0"]


def test_default_revs_leave_out_a_release_tag_head_does_not_contain(tmp_path):
    repo = _history(tmp_path)
    repo.git("checkout", "-q", "-b", "side", "v0.1.0")
    repo.write("side.txt", "s\n")
    repo.commit("feat: a release cut on a branch that never merged")
    repo.git("tag", "v0.1.1")
    repo.git("checkout", "-q", "main")
    assert interface_churn.default_revs(repo.root) == ["v0.1.0", "v0.2.0", "HEAD"]


def test_default_revs_without_a_release_tag_is_head_alone(tmp_path):
    repo = TmpGitRepo(tmp_path)
    repo.write("a.txt", "a\n")
    repo.commit("feat: first")
    assert interface_churn.default_revs(repo.root) == ["HEAD"]


def test_measure_counts_commits_marks_and_the_recorded_window(tmp_path):
    repo = _history(tmp_path)
    window = interface_churn.measure(repo.root, "v0.2.0", "HEAD", golden=GOLDEN)
    assert (window.commits, window.marked) == (2, 1)
    assert window.recorded is not None
    assert len(window.recorded.broken) == 6
    assert window.unrecorded == {}
    before = interface_churn.measure(repo.root, "v0.1.0", "v0.2.0", golden=GOLDEN)
    assert (before.commits, before.marked, before.recorded) == (1, 0, None)


def test_a_relative_golden_is_relative_to_the_repo_wherever_the_caller_stands(
    tmp_path, monkeypatch
):
    repo = _history(tmp_path / "repo")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    window = interface_churn.measure(repo.root, "v0.2.0", "HEAD", golden=GOLDEN)
    assert window.recorded is not None


def test_measure_approximates_the_unrecorded_surfaces_on_request(tmp_path):
    repo = _history(tmp_path)
    window = interface_churn.measure(repo.root, "v0.2.0", "HEAD", golden=GOLDEN, unrecorded=True)
    flags, env = window.unrecorded["flags"], window.unrecorded["env"]
    assert (flags.removed, flags.added, flags.total) == (["--old-flag"], ["--new-flag"], 3)
    added = ["OTTOPATH", "OTTO_NEW_VAR"]  # OTTOPATH is otto's; OTTOMAN is not a variable
    assert (env.removed, env.added, env.total) == (["OTTO_OLD"], added, 2)


def test_main_prints_one_row_per_window(tmp_path, capsys):
    repo = _history(tmp_path)
    args = ["--repo", str(repo.root), "--golden", str(GOLDEN)]
    assert interface_churn.main([*args, "--unrecorded"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].split() == [
        *["window", "commits", "marked", "surface", "broken", "churn", "findings", "distinct"],
        *["added", "flags", "(approx.)", "env", "(approx.)"],
    ]
    unmeasured = ["-"] * 6
    assert lines[1].split() == [
        *["v0.1.0..v0.2.0", "1", "0", *unmeasured],
        *["-0", "+0", "=3", "-0", "+0", "=1"],
    ]
    assert lines[2].split() == [
        *["v0.2.0..HEAD", "2", "1", "9", "6", "66.67%", "6", "5", "1"],
        *["-1", "+1", "=3", "-1", "+2", "=2"],
    ]
    assert len(lines) == 3


def test_detail_lists_namespaces_findings_and_spellings_gone(tmp_path, capsys):
    repo = _history(tmp_path)
    argv = ["v0.1.0", "v0.2.0", "HEAD", "--repo", str(repo.root), "--golden", str(GOLDEN)]
    assert interface_churn.main([*argv, "--detail", "--unrecorded"]) == 0
    out = capsys.readouterr().out
    detail = out.split("\n\n", 1)[1]
    assert detail.startswith("v0.2.0..HEAD\n"), "a window with nothing to list has no block"
    assert "       2  otto\n" in detail
    assert "       2  otto.models\n" in detail
    assert "       1  otto.lab\n" in detail
    assert f"       1  {api_compat.FORMATS_NAMESPACE}\n" in detail
    assert "    get_host: keyword term removed [otto, otto.lab]\n" in detail
    assert "  flags gone: --old-flag\n" in detail
    assert "  env gone: OTTO_OLD\n" in detail


def test_an_unknown_revision_exits_2_naming_it(tmp_path, capsys):
    repo = _history(tmp_path)
    assert interface_churn.main(["no-such-rev", "HEAD", "--repo", str(repo.root)]) == 2
    err = capsys.readouterr().err
    assert "no-such-rev" in err
    assert "golden" not in err, "an unknown revision is not a missing golden"


def test_a_single_revision_is_a_usage_error(tmp_path, capsys):
    repo = _history(tmp_path)
    with pytest.raises(SystemExit) as exit_info:
        interface_churn.main(["HEAD", "--repo", str(repo.root)])
    assert exit_info.value.code == 2
    assert "two revisions" in capsys.readouterr().err


def test_a_golden_outside_the_repo_is_a_usage_error(tmp_path, capsys):
    repo = _history(tmp_path / "repo")
    outside = tmp_path / "outside.txt"
    outside.write_text(_OLD)
    argv = ["v0.2.0", "HEAD", "--repo", str(repo.root), "--golden", str(outside)]
    with pytest.raises(SystemExit) as exit_info:
        interface_churn.main(argv)
    assert exit_info.value.code == 2
    assert "inside --repo" in capsys.readouterr().err


@pytest.mark.parametrize("tags", [[], ["v0.1.0"]], ids=["no-release-tag", "head-is-the-only-tag"])
def test_a_history_with_no_window_says_so_and_exits_0(tmp_path, capsys, tags):
    repo = TmpGitRepo(tmp_path)
    repo.write("a.txt", "a\n")
    repo.commit("feat: first")
    for tag in tags:
        repo.git("tag", tag)
    assert interface_churn.main(["--repo", str(repo.root)]) == 0
    why = "HEAD is the only release tag" if tags else "no release tag yet"
    assert f"no window to report: {why}" in capsys.readouterr().out


def test_a_golden_no_given_revision_has_is_said_on_stderr(tmp_path, capsys):
    repo = _history(tmp_path)
    argv = ["v0.2.0", "HEAD", "--repo", str(repo.root), "--golden", "no/such/golden.txt"]
    assert interface_churn.main(argv) == 0
    captured = capsys.readouterr()
    assert "no revision given has the golden no/such/golden.txt" in captured.err
    assert captured.out.splitlines()[1].split()[3:] == ["-"] * 6
    assert interface_churn.main(["--repo", str(repo.root), "--golden", str(GOLDEN)]) == 0
    assert capsys.readouterr().err == "", "one revision with the golden is enough"


def test_detail_of_a_v1_window_lists_the_lines_and_no_findings(tmp_path, capsys):
    repo = TmpGitRepo(tmp_path)
    repo.write(str(GOLDEN), V1_HEADER + "otto:Alpha\notto:Beta\n")
    repo.commit("feat: first")
    repo.git("tag", "v0.1.0")
    repo.write(str(GOLDEN), V1_HEADER + "otto:Alpha\n")
    repo.commit("feat(api)!: drop Beta")
    argv = ["--repo", str(repo.root), "--golden", str(GOLDEN), "--detail"]
    assert interface_churn.main(argv) == 0
    out = capsys.readouterr().out
    assert out.splitlines()[1].split() == [
        "v0.1.0..HEAD",
        "1",
        "1",
        "2",
        "1",
        "50.00%",
        "-",
        "-",
        "0",
    ]
    assert "  lines gone or reworded:\n    otto:Beta\n" in out
    assert "distinct findings" not in out
