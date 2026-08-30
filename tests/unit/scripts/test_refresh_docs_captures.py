"""The capture runner: a manifest in, deterministic artifacts out, drift reported (spec §5).

No otto command is run here — the manifest points at ``python -c`` so the
tests exercise substitution, redaction, expected-exit handling and the
--check diff without a bed or a project.
"""

from pathlib import Path

import pytest

from scripts import refresh_docs_captures as rdc
from tests._fixtures.sutrepo import make_sut_repo


def _manifest(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "captures.toml"
    p.write_text(body)
    return p


def _ctx(tmp_path: Path) -> rdc.RunContext:
    return rdc.RunContext(
        examples_root=tmp_path / "examples",
        captures_dir=tmp_path / "captures",
        tmp=tmp_path / "scratch",
    )


def test_manifest_parses_defaults(tmp_path):
    m = _manifest(tmp_path, '[[capture]]\nid = "a"\nargv = ["{python}", "-c", "print(1)"]\n')
    (cap,) = rdc.load_manifest(m)
    fields = (cap.id, cap.labless, cap.project, cap.expect_exit, cap.timeout, cap.settings_append)
    assert fields == (
        "a",
        False,
        "",
        0,
        120,
        "",
    )


def test_redaction_applies_defaults_then_manifest_rules(tmp_path):
    ctx = _ctx(tmp_path)
    text = f"wrote {ctx.tmp}/x at 2026-08-29T10:00:00Z took 1.5s"
    out = rdc.redact(text, [*ctx.default_rules(), (r"took [\d.]+s", "took <elapsed>")])
    assert out == "wrote /tmp/otto-gs/x at <timestamp> took <elapsed>"


def test_refresh_writes_the_command_line_then_output(tmp_path):
    ctx = _ctx(tmp_path)
    m = _manifest(
        tmp_path, '[[capture]]\nid = "hello"\nargv = ["{python}", "-c", "print(\'hi\')"]\n'
    )
    rdc.refresh(rdc.load_manifest(m), ctx)
    assert (ctx.captures_dir / "hello.txt").read_text() == "$ python -c print('hi')\nhi\n"


def test_unexpected_exit_code_is_an_error_not_a_capture(tmp_path):
    ctx = _ctx(tmp_path)
    m = _manifest(
        tmp_path, '[[capture]]\nid = "boom"\nargv = ["{python}", "-c", "raise SystemExit(3)"]\n'
    )
    with pytest.raises(rdc.CaptureError, match=r"boom.*exited 3.*expected 0"):
        rdc.refresh(rdc.load_manifest(m), ctx)
    assert not (ctx.captures_dir / "boom.txt").exists()


def test_expected_nonzero_exit_is_captured(tmp_path):
    ctx = _ctx(tmp_path)
    m = _manifest(
        tmp_path,
        '[[capture]]\nid = "refused"\nexpect_exit = 1\n'
        'argv = ["{python}", "-c", "import sys; print(\'no\'); sys.exit(1)"]\n',
    )
    rdc.refresh(rdc.load_manifest(m), ctx)
    assert (ctx.captures_dir / "refused.txt").read_text().endswith("no\n")


def test_check_reports_drift_and_missing(tmp_path, capsys):
    ctx = _ctx(tmp_path)
    m = _manifest(tmp_path, '[[capture]]\nid = "d"\nargv = ["{python}", "-c", "print(2)"]\n')
    caps = rdc.load_manifest(m)
    assert rdc.check(caps, ctx) == 1  # missing artifact
    rdc.refresh(caps, ctx)
    assert rdc.check(caps, ctx) == 0
    (ctx.captures_dir / "d.txt").write_text("$ python -c print(2)\n3\n")
    assert rdc.check(caps, ctx) == 1
    assert "-3\n+2" in capsys.readouterr().out.replace("\r", "")


def test_placeholders_render_as_a_reader_would_type_them():
    line = rdc.render_command(
        ["{python}", "{repo}/scripts/x.py", "{tmp}/out.json", "--path", "{project}/lab"]
    )
    assert line == "$ python ./scripts/x.py /tmp/otto-gs/out.json --path ./lab"


def test_labless_filter_and_only(tmp_path):
    m = _manifest(
        tmp_path,
        '[[capture]]\nid = "a"\nlabless = true\nargv = ["{python}", "-c", "1"]\n'
        '[[capture]]\nid = "b"\nargv = ["{python}", "-c", "1"]\n',
    )
    caps = rdc.load_manifest(m)
    assert [c.id for c in rdc.select(caps, labless=True, only=[])] == ["a"]
    assert [c.id for c in rdc.select(caps, labless=False, only=["b"])] == ["b"]
    with pytest.raises(rdc.CaptureError, match="unknown capture id: zz"):
        rdc.select(caps, labless=False, only=["zz"])


# --- Task 4 fix round 1: findings T4-1..T4-4 -------------------------------


def test_timeout_raises_capture_error(tmp_path):
    ctx = _ctx(tmp_path)
    m = _manifest(
        tmp_path,
        '[[capture]]\nid = "slow"\ntimeout = 1\n'
        'argv = ["{python}", "-c", "import time; time.sleep(5)"]\n',
    )
    with pytest.raises(rdc.CaptureError, match="timed out"):
        rdc.refresh(rdc.load_manifest(m), ctx)


# --- Task 4 fix round 2: finding T4-5 --------------------------------------


def test_timeout_message_carries_readable_partial_output(tmp_path):
    ctx = _ctx(tmp_path)
    m = _manifest(
        tmp_path,
        '[[capture]]\nid = "slow"\ntimeout = 1\n'
        'argv = ["{python}", "-c", "import sys, time; print(\'partial line\'); '
        'sys.stdout.flush(); time.sleep(5)"]\n',
    )
    with pytest.raises(rdc.CaptureError, match=r"output so far:\npartial line") as excinfo:
        rdc.refresh(rdc.load_manifest(m), ctx)
    assert "b'" not in str(excinfo.value)


def test_missing_binary_raises_capture_error(tmp_path):
    ctx = _ctx(tmp_path)
    m = _manifest(tmp_path, '[[capture]]\nid = "nope"\nargv = ["{tmp}/does-not-exist"]\n')
    with pytest.raises(rdc.CaptureError, match="command not found"):
        rdc.refresh(rdc.load_manifest(m), ctx)


def test_manifest_row_missing_argv_raises(tmp_path):
    m = _manifest(tmp_path, '[[capture]]\nid = "x"\n')
    with pytest.raises(rdc.CaptureError, match="lacks 'argv'"):
        rdc.load_manifest(m)


def test_malformed_redact_raises(tmp_path):
    m = _manifest(
        tmp_path,
        '[[capture]]\nid = "x"\nargv = ["{python}", "-c", "1"]\nredact = [["only-one"]]\n',
    )
    with pytest.raises(rdc.CaptureError, match="pairs"):
        rdc.load_manifest(m)


def test_unparsable_manifest_raises(tmp_path):
    m = _manifest(tmp_path, "[[capture]\nid = \n")
    with pytest.raises(rdc.CaptureError, match="manifest"):
        rdc.load_manifest(m)


def test_check_on_empty_selection_refuses(tmp_path):
    ctx = _ctx(tmp_path)
    with pytest.raises(rdc.CaptureError, match="no captures selected"):
        rdc.check([], ctx)


def test_refresh_on_empty_selection_refuses(tmp_path):
    ctx = _ctx(tmp_path)
    with pytest.raises(rdc.CaptureError, match="no captures selected"):
        rdc.refresh([], ctx)


def test_main_list_on_empty_manifest_returns_0(tmp_path):
    m = _manifest(tmp_path, "")
    assert rdc.main(["--list", "--manifest", str(m)]) == 0


def test_repo_root_is_redacted_after_examples_root(tmp_path):
    ctx = _ctx(tmp_path)
    text = f"ran {rdc.ROOT}/scripts/x.py in {ctx.examples_root}/gs"
    assert rdc.redact(text, ctx.default_rules()) == "ran <repo>/scripts/x.py in <examples>/gs"

    # examples_root nested under ROOT: the examples rule must still win.
    nested_ctx = rdc.RunContext(
        examples_root=rdc.ROOT / "docs" / "examples", captures_dir=tmp_path, tmp=tmp_path / "s"
    )
    nested_text = f"in {nested_ctx.examples_root}/gs"
    assert rdc.redact(nested_text, nested_ctx.default_rules()) == "in <examples>/gs"


def test_main_list_prints_ids(tmp_path, capsys):
    m = _manifest(tmp_path, '[[capture]]\nid = "a"\nargv = ["{python}", "-c", "1"]\n')
    assert rdc.main(["--list", "--manifest", str(m)]) == 0
    assert "a" in capsys.readouterr().out


# ``main()`` (without --list) runs against the real, fixed SCRATCH_DIR — two
# such invocations must not race under xdist, so they share one group and
# land on the same worker (`--dist loadgroup` is pinned in pyproject.toml).
@pytest.mark.xdist_group(name="scratch_dir")
def test_main_check_then_refresh_then_check(tmp_path):
    m = _manifest(tmp_path, '[[capture]]\nid = "cli"\nargv = ["{python}", "-c", "print(1)"]\n')
    captures_dir = tmp_path / "out"
    common = ["--manifest", str(m), "--captures-dir", str(captures_dir)]
    assert rdc.main(["--check", *common]) == 1
    assert rdc.main(common) == 0
    assert rdc.main(["--check", *common]) == 0


def test_main_unknown_only_id_raises(tmp_path):
    m = _manifest(tmp_path, '[[capture]]\nid = "a"\nargv = ["{python}", "-c", "1"]\n')
    with pytest.raises(rdc.CaptureError, match="unknown capture id: zz"):
        rdc.main(["--only", "zz", "--manifest", str(m)])


def test_redact_rows_parse_to_tuples(tmp_path):
    m = _manifest(
        tmp_path,
        '[[capture]]\nid = "a"\nargv = ["{python}", "-c", "1"]\nredact = [["a", "b"]]\n',
    )
    (cap,) = rdc.load_manifest(m)
    assert cap.redact == [("a", "b")]


def test_duplicate_ids_raise(tmp_path):
    m = _manifest(
        tmp_path,
        '[[capture]]\nid = "a"\nargv = ["{python}", "-c", "1"]\n'
        '[[capture]]\nid = "a"\nargv = ["{python}", "-c", "2"]\n',
    )
    with pytest.raises(rdc.CaptureError, match="duplicate capture ids"):
        rdc.load_manifest(m)


def test_timeout_parses(tmp_path):
    m = _manifest(tmp_path, '[[capture]]\nid = "a"\ntimeout = 7\nargv = ["{python}", "-c", "1"]\n')
    (cap,) = rdc.load_manifest(m)
    assert cap.timeout == 7


def test_env_sets_otto_sut_dirs_only_when_project_given(tmp_path):
    ctx = _ctx(tmp_path)
    (ctx.examples_root / "p").mkdir(parents=True)
    (ctx.examples_root / "p" / "marker.txt").write_text("hi")
    with_project = rdc.Capture(id="a", argv=[], project="p")
    without_project = rdc.Capture(id="b", argv=[])
    assert ctx.env(with_project)["OTTO_SUT_DIRS"] == str(ctx.tmp / "p")
    assert "OTTO_SUT_DIRS" not in ctx.env(without_project)


# --- Task 9 fix round 1: finding T9-5 ---------------------------------------


def test_env_points_otto_sut_dirs_at_a_scratch_copy_of_the_project(tmp_path):
    ctx = _ctx(tmp_path)
    (ctx.examples_root / "p").mkdir(parents=True)
    (ctx.examples_root / "p" / "marker.txt").write_text("hi")
    cap = rdc.Capture(id="a", argv=[], project="p")
    assert ctx.env(cap)["OTTO_SUT_DIRS"] == str(ctx.tmp / "p")
    assert (ctx.tmp / "p" / "marker.txt").read_text() == "hi"


def test_project_dir_raises_when_the_source_is_missing(tmp_path):
    ctx = _ctx(tmp_path)
    cap = rdc.Capture(id="a", argv=[], project="does-not-exist")
    with pytest.raises(rdc.CaptureError, match="not found under"):
        ctx.project_dir(cap)


def test_project_placeholder_in_argv_resolves_to_the_scratch_copy(tmp_path):
    ctx = _ctx(tmp_path)
    (ctx.examples_root / "p").mkdir(parents=True)
    (ctx.examples_root / "p" / "marker.txt").write_text("hi")
    m = _manifest(
        tmp_path,
        '[[capture]]\nid = "a"\nproject = "p"\n'
        'argv = ["{python}", "-c", "import sys; print(sys.argv[1])", "{project}"]\n',
    )
    rdc.refresh(rdc.load_manifest(m), ctx)
    # The resolved path is the scratch-dir copy, not examples_root/p -- the
    # default redaction rules normalize ctx.tmp itself to SCRATCH_DIR.
    assert (ctx.captures_dir / "a.txt").read_text().endswith(f"{rdc.SCRATCH_DIR / 'p'}\n")


# --- Task 5: mkdir precreation ---------------------------------------------


def test_mkdir_precreates_directories(tmp_path):
    ctx = _ctx(tmp_path)
    m = _manifest(
        tmp_path,
        '[[capture]]\nid = "m"\nmkdir = ["{tmp}/made"]\n'
        'argv = ["{python}", "-c", '
        '"import os,sys; print(os.path.isdir(sys.argv[1]))", "{tmp}/made"]\n',
    )
    rdc.refresh(rdc.load_manifest(m), ctx)
    assert (ctx.captures_dir / "m.txt").read_text().endswith("True\n")


# --- Task 5 fix round 1: findings T5-1, T5-2 --------------------------------


@pytest.mark.xdist_group(name="scratch_dir")
def test_main_runs_captures_in_the_fixed_scratch_dir(tmp_path):
    m = _manifest(
        tmp_path,
        '[[capture]]\nid = "cwd"\nargv = ["{python}", "-c", "import os; print(os.getcwd())"]\n',
    )
    captures_dir = tmp_path / "out"
    assert rdc.main(["--manifest", str(m), "--captures-dir", str(captures_dir)]) == 0
    body = (captures_dir / "cwd.txt").read_text()
    assert body.splitlines()[-1] == str(rdc.SCRATCH_DIR)


def test_mkdir_must_be_a_list_of_strings(tmp_path):
    m = _manifest(
        tmp_path, '[[capture]]\nid = "x"\nmkdir = "acme"\nargv = ["{python}", "-c", "1"]\n'
    )
    with pytest.raises(rdc.CaptureError, match="strings"):
        rdc.load_manifest(m)


def test_mkdir_outside_scratch_dir_refuses(tmp_path):
    ctx = _ctx(tmp_path)
    m = _manifest(
        tmp_path,
        '[[capture]]\nid = "x"\nmkdir = ["../escape"]\nargv = ["{python}", "-c", "1"]\n',
    )
    with pytest.raises(rdc.CaptureError, match="outside the scratch dir"):
        rdc.refresh(rdc.load_manifest(m), ctx)
    assert not Path("../escape").resolve().exists()


# --- Task 13a: settings_append -----------------------------------------------


_READ_SETTINGS = (
    "import os, pathlib; "
    "print(pathlib.Path(os.environ['OTTO_SUT_DIRS'], '.otto', 'settings.toml').read_text())"
)


def _project_with_settings(ctx: rdc.RunContext) -> tuple[Path, str]:
    """An example project for the runner to copy, plus its committed settings text.

    Scaffolded by the one builder (review §7.5) rather than a hand-rolled
    ``.otto/settings.toml`` write, so a new settings field does not have to be
    re-typed here. The committed bytes come back with it: the restore
    assertions compare against what the builder wrote, not a literal.
    """
    src = make_sut_repo(ctx.examples_root / "p", name="p")
    (src / "extra.toml").write_text('[reservations]\nbackend = "json"\n')
    return src, (src / ".otto" / "settings.toml").read_text()


def test_settings_append_is_seen_by_the_command_and_restored_after(tmp_path):
    ctx = _ctx(tmp_path)
    _, original = _project_with_settings(ctx)
    m = _manifest(
        tmp_path,
        '[[capture]]\nid = "a"\nproject = "p"\nsettings_append = "extra.toml"\n'
        f'argv = ["{{python}}", "-c", "{_READ_SETTINGS}"]\n',
    )
    rdc.refresh(rdc.load_manifest(m), ctx)
    body = (ctx.captures_dir / "a.txt").read_text()
    assert 'name = "p"' in body
    assert 'backend = "json"' in body
    # The scratch copy is shared by every capture of the project in a run:
    # the next one must see the committed settings, not this one's addition.
    assert (ctx.tmp / "p" / ".otto" / "settings.toml").read_text() == original
    # The source project is never touched.
    assert (ctx.examples_root / "p" / ".otto" / "settings.toml").read_text() == original


def test_settings_append_is_restored_when_the_command_fails(tmp_path):
    ctx = _ctx(tmp_path)
    _, original = _project_with_settings(ctx)
    m = _manifest(
        tmp_path,
        '[[capture]]\nid = "a"\nproject = "p"\nsettings_append = "extra.toml"\n'
        'argv = ["{python}", "-c", "raise SystemExit(3)"]\n',
    )
    with pytest.raises(rdc.CaptureError, match="exited 3"):
        rdc.refresh(rdc.load_manifest(m), ctx)
    assert (ctx.tmp / "p" / ".otto" / "settings.toml").read_text() == original


def test_settings_append_requires_a_project(tmp_path):
    m = _manifest(
        tmp_path,
        '[[capture]]\nid = "a"\nsettings_append = "extra.toml"\nargv = ["{python}", "-c", "1"]\n',
    )
    with pytest.raises(rdc.CaptureError, match="settings_append needs project"):
        rdc.load_manifest(m)


def test_settings_append_must_be_a_string(tmp_path):
    m = _manifest(
        tmp_path,
        '[[capture]]\nid = "a"\nproject = "p"\nsettings_append = ["extra.toml"]\n'
        'argv = ["{python}", "-c", "1"]\n',
    )
    with pytest.raises(rdc.CaptureError, match="settings_append is a file name"):
        rdc.load_manifest(m)


@pytest.mark.parametrize("fragment", ["missing.toml", "../escape.toml"])
def test_settings_append_must_name_a_file_inside_the_project(tmp_path, fragment):
    ctx = _ctx(tmp_path)
    _project_with_settings(ctx)
    (tmp_path / "scratch" / "escape.toml").parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / "scratch" / "escape.toml").write_text("x = 1\n")
    m = _manifest(
        tmp_path,
        f'[[capture]]\nid = "a"\nproject = "p"\nsettings_append = "{fragment}"\n'
        'argv = ["{python}", "-c", "1"]\n',
    )
    with pytest.raises(rdc.CaptureError, match="not a file inside project"):
        rdc.refresh(rdc.load_manifest(m), ctx)


def test_settings_append_reports_a_project_whose_settings_cannot_be_read(tmp_path):
    ctx = _ctx(tmp_path)
    src = ctx.examples_root / "p"
    src.mkdir(parents=True)
    (src / "extra.toml").write_text('[reservations]\nbackend = "json"\n')
    m = _manifest(
        tmp_path,
        '[[capture]]\nid = "a"\nproject = "p"\nsettings_append = "extra.toml"\n'
        'argv = ["{python}", "-c", "1"]\n',
    )
    with pytest.raises(rdc.CaptureError, match="cannot read"):
        rdc.refresh(rdc.load_manifest(m), ctx)


def test_settings_append_without_a_project_refuses_before_copying_anything(tmp_path):
    # The manifest guard cannot see a Capture built in code. Without the guard
    # in _appended_settings, ``examples_root / ""`` is the examples root itself
    # and the whole tree would be copied into the scratch dir.
    ctx = _ctx(tmp_path)
    _project_with_settings(ctx)
    cap = rdc.Capture(
        id="a", argv=["{python}", "-c", "1"], project="", settings_append="extra.toml"
    )
    with pytest.raises(rdc.CaptureError, match="needs project"):
        rdc.run_capture(cap, ctx)
    assert not (ctx.tmp / "p").exists()
