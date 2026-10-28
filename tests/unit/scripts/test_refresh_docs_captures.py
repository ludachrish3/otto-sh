"""The capture runner: a manifest in, deterministic artifacts out, drift reported (spec §5).

No otto command is run here — the manifest points at ``python -c`` so the
tests exercise substitution, redaction, expected-exit handling and the
--check diff without a bed or a project.
"""

import fcntl
import threading
from pathlib import Path

import pytest

from scripts import refresh_docs_captures as rdc
from tests._fixtures.project_scaffold import make_project

pytestmark = pytest.mark.interpreter_agnostic


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
    # `print('hi')` carries shell-active characters (parens, a quote), so the
    # rendered command line quotes it -- see render_command.
    assert (
        ctx.captures_dir / "hello.txt"
    ).read_text() == "$ python -c 'print('\"'\"'hi'\"'\"')'\nhi\n"


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
    # Same quoted command line as the fresh render (`print(2)` has parens, so
    # it is shell-active) -- only the body line drifts here.
    (ctx.captures_dir / "d.txt").write_text("$ python -c 'print(2)'\n3\n")
    assert rdc.check(caps, ctx) == 1
    assert "-3\n+2" in capsys.readouterr().out.replace("\r", "")


def test_placeholders_render_as_a_reader_would_type_them():
    line = rdc.render_command(
        ["{python}", "{checkout}/scripts/x.py", "{tmp}/out.json", "--path", "{project}/lab"]
    )
    assert line == "$ python ./scripts/x.py /tmp/otto-gs/out.json --path ./lab"


def test_shell_active_arguments_are_quoted_so_a_reader_can_copy_them():
    # `Host.run` treats a sequence of commands as separate commands, so an
    # unquoted rendering of `run echo $APP_ENV` tells a copier to run `echo`
    # and then expand `$APP_ENV` in their own shell -- and `run 1 + 1` reads
    # as three commands. Only the argument that actually needs it gets quoted.
    line = rdc.render_command(["{otto}", "--lab", "unix", "host", "test1", "exec", "echo $APP_ENV"])
    assert line == "$ otto --lab unix host test1 exec 'echo $APP_ENV'"

    line = rdc.render_command(["{otto}", "run", "1 + 1"])
    assert line == "$ otto run '1 + 1'"

    # An argv with no shell-active characters at all stays entirely bare.
    line = rdc.render_command(["{otto}", "--lab", "unix", "host", "test1", "probe"])
    assert line == "$ otto --lab unix host test1 probe"


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
    assert ctx.env(with_project)["OTTOPATH"] == str(ctx.tmp / "p")
    assert "OTTOPATH" not in ctx.env(without_project)


# --- Task 9 fix round 1: finding T9-5 ---------------------------------------


def test_env_points_otto_sut_dirs_at_a_scratch_copy_of_the_project(tmp_path):
    ctx = _ctx(tmp_path)
    (ctx.examples_root / "p").mkdir(parents=True)
    (ctx.examples_root / "p" / "marker.txt").write_text("hi")
    cap = rdc.Capture(id="a", argv=[], project="p")
    assert ctx.env(cap)["OTTOPATH"] == str(ctx.tmp / "p")
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
    "print(pathlib.Path(os.environ['OTTOPATH'], '.otto', 'settings.toml').read_text())"
)


def _project_with_settings(ctx: rdc.RunContext) -> tuple[Path, str]:
    """An example project for the runner to copy, plus its committed settings text.

    Scaffolded by the one builder (review §7.5) rather than a hand-rolled
    ``.otto/settings.toml`` write, so a new settings field does not have to be
    re-typed here. The committed bytes come back with it: the restore
    assertions compare against what the builder wrote, not a literal.
    """
    src = make_project(ctx.examples_root / "p", name="p")
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


def test_scratch_lock_holds_the_lock_for_the_block(tmp_path, capsys):
    lock = tmp_path / "otto-gs.lock"
    with rdc.scratch_lock(lock), lock.open("a") as other, pytest.raises(BlockingIOError):
        fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
    assert capsys.readouterr().err == ""
    with lock.open("a") as other:
        fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)  # released on exit


def test_scratch_lock_says_it_is_waiting_then_takes_the_lock(tmp_path, capsys):
    # A second docs build waits for the first instead of deleting its scratch dir.
    lock = tmp_path / "otto-gs.lock"
    releasing = threading.Event()

    def release_holder(holder):
        # Set BEFORE unlocking: the waiter gets past its flock only after the
        # unlock, so inside its block the event is always set. Asserting that
        # the Timer thread has exited instead races that thread's own exit.
        releasing.set()
        fcntl.flock(holder, fcntl.LOCK_UN)

    with lock.open("a") as holder:
        fcntl.flock(holder, fcntl.LOCK_EX)
        release = threading.Timer(0.2, release_holder, (holder,))
        release.start()
        try:
            with rdc.scratch_lock(lock), lock.open("a") as other:
                assert releasing.is_set()  # it waited for the holder to let go
                with pytest.raises(BlockingIOError):  # and now holds the lock itself
                    fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            release.cancel()
            release.join()
    assert f"another capture run holds {lock}; waiting" in capsys.readouterr().err


def test_a_capture_keeps_no_trailing_spaces_and_keeps_everything_else(tmp_path):
    """Red before the fix: Rich pads a table row to the terminal width, and the
    padding reached the artifact, so a refresh at another width rewrote lines
    that had not changed."""
    ctx = _ctx(tmp_path)
    emit = tmp_path / "emit.py"
    emit.write_text('print("a  b   ")\nprint("")\nprint("  c\\t ")\nprint("   ")\n')
    m = _manifest(tmp_path, f'[[capture]]\nid = "pad"\nargv = ["{{python}}", "{emit}"]\n')
    rdc.refresh(rdc.load_manifest(m), ctx)
    lines = (ctx.captures_dir / "pad.txt").read_text().split("\n")
    assert lines[1:] == ["a  b", "", "  c", "", ""]


def _store(path: Path, *, tier: str, product: str, file: str, line: int, hits: int) -> Path:
    """Write a one-line coverage store with otto's own model, so the format cannot drift."""
    from otto.coverage.store.model import CoverageStore

    store = CoverageStore()
    run_id = store.add_run(tier=tier, product=product)
    record = store.get_or_create_file(Path(file)).get_or_create_line(line)
    record.hits.add(tier, hits)
    record.run_hits[run_id] = hits
    store.save(path)
    return path


def _store_manifest(tmp_path: Path, assertion: str) -> Path:
    """A capture that puts a prepared store under {xdir}, then asserts on it."""
    src = _store(
        tmp_path / "prepared.json",
        tier="manual",
        product="app",
        file="src/main.cpp",
        line=12,
        hits=3,
    )
    copy = "import shutil, sys; shutil.copy(sys.argv[1], sys.argv[2])"
    return _manifest(
        tmp_path,
        "[[capture]]\n"
        'id = "cov"\n'
        f'argv = ["{{python}}", "-c", "{copy}", "{src}", "{{xdir}}/store.json"]\n'
        "[[capture.store]]\n"
        'path = "{xdir}/store.json"\n'
        'file = "src/main.cpp"\n'
        "line = 12\n" + assertion,
    )


def test_a_store_assertion_passes_on_the_recorded_count(tmp_path):
    m = _store_manifest(tmp_path, 'tier = "manual"\nproduct = "app"\nhits = 3\n')
    rdc.refresh(rdc.load_manifest(m), _ctx(tmp_path))
    assert (tmp_path / "captures" / "cov.txt").exists()


def test_a_store_assertion_without_a_product_reads_the_tier_total(tmp_path):
    m = _store_manifest(tmp_path, 'tier = "manual"\nhits = 3\n')
    rdc.refresh(rdc.load_manifest(m), _ctx(tmp_path))


def test_a_store_assertion_fails_on_a_different_count(tmp_path):
    m = _store_manifest(tmp_path, 'tier = "manual"\nproduct = "app"\nhits = 4\n')
    with pytest.raises(rdc.CaptureError) as err:
        rdc.refresh(rdc.load_manifest(m), _ctx(tmp_path))
    message = str(err.value)
    assert "src/main.cpp:12" in message
    assert "expected 4" in message
    assert "recorded 3" in message


@pytest.mark.parametrize(
    ("assertion", "said"),
    [
        (
            'tier = "manual"\nproduct = "nope"\nhits = 0\n',
            "no run of product 'nope' in tier 'manual'",
        ),
        ('tier = "unit"\nhits = 0\n', "no run in tier 'unit'"),
    ],
)
def test_an_unrecorded_product_or_tier_is_an_error_not_zero(tmp_path, assertion, said):
    m = _store_manifest(tmp_path, assertion)
    with pytest.raises(rdc.CaptureError, match=said):
        rdc.refresh(rdc.load_manifest(m), _ctx(tmp_path))


def test_a_file_the_store_does_not_hold_is_an_error_naming_what_it_holds(tmp_path):
    m = _store_manifest(tmp_path, 'tier = "manual"\nhits = 3\n').read_text()
    m_path = _manifest(tmp_path, m.replace('file = "src/main.cpp"', 'file = "src/other.cpp"'))
    with pytest.raises(rdc.CaptureError, match=r"src/other\.cpp.*src/main\.cpp"):
        rdc.refresh(rdc.load_manifest(m_path), _ctx(tmp_path))


def test_a_missing_store_file_is_an_error(tmp_path):
    m = _manifest(
        tmp_path,
        '[[capture]]\nid = "cov"\nargv = ["{python}", "-c", "pass"]\n'
        '[[capture.store]]\npath = "{xdir}/store.json"\nfile = "a.c"\nline = 1\n'
        'tier = "manual"\nhits = 1\n',
    )
    with pytest.raises(rdc.CaptureError, match=r"store\.json"):
        rdc.refresh(rdc.load_manifest(m), _ctx(tmp_path))


def test_the_gtest_kind_redacts_timings_and_turns_colour_off(tmp_path):
    script = (
        "import sys; print(' '.join(sys.argv[1:])); "
        "print('[       OK ] Queue.Push (12 ms)'); "
        "print('[==========] 1 test from 1 test suite ran. (34 ms total)')"
    )
    m = _manifest(
        tmp_path,
        f'[[capture]]\nid = "g"\nkind = "gtest"\nargv = ["{{python}}", "-c", "{script}"]\n',
    )
    rdc.refresh(rdc.load_manifest(m), _ctx(tmp_path))
    output = (tmp_path / "captures" / "g.txt").read_text().split("\n", 1)[1]
    assert "--gtest_color=no" in output
    assert "Queue.Push (<n> ms)" in output
    assert "(<n> ms total)" in output
    assert "12 ms" not in output


@pytest.mark.parametrize(
    ("body", "said"),
    [
        ('kind = "pytest"\n', "kind"),
        (
            '[[capture.store]]\npath = "x"\nfile = "a.c"\nline = "one"\ntier = "t"\nhits = 1\n',
            "line",
        ),
        ('[[capture.store]]\npath = "x"\nline = 1\ntier = "t"\nhits = 1\n', "file"),
    ],
)
def test_load_manifest_refuses_a_bad_kind_or_store_entry(tmp_path, body, said):
    m = _manifest(tmp_path, f'[[capture]]\nid = "a"\nargv = ["x"]\n{body}')
    with pytest.raises(rdc.CaptureError, match=said):
        rdc.load_manifest(m)


def test_a_log_line_s_source_line_number_is_redacted(tmp_path):
    """The log's ``file.py:123`` column moves with every edit to that file.

    Left in, a capture drifts on almost any src change and the drift check
    reports noise. Only a ``.py:<digits>`` at the very end of a line is a log
    path column; the same text inside a message is left alone.
    """
    ctx = _ctx(tmp_path)
    text = (
        "INFO     @zephyr37_fat   | kernel uptime          host.py:3103\n"
        "DEBUG    framed write cmd='x'                    session.py:946\n"
        "see host.py:12 for details\n"
    )
    assert rdc.redact(text, ctx.default_rules()) == (
        "INFO     @zephyr37_fat   | kernel uptime           host.py:<n>\n"
        "DEBUG    framed write cmd='x'                    session.py:<n>\n"
        "see host.py:12 for details\n"
    )


def test_a_redacted_source_column_does_not_depend_on_the_line_number_s_width(tmp_path):
    """Rich pads the message so the ``file.py:<line>`` column ends at the right edge.

    A line number that gains or loses a digit moves that padding by one, so a
    redaction that replaced only the digits still drifted whenever an edit
    carried a log call across 999/1000. The two lines below are one log record
    from before and after such an edit; they must redact to the same text.
    """
    ctx = _ctx(tmp_path)
    before = "DEBUG    begin marker matched on                         session.py:1001\n"
    after = "DEBUG    begin marker matched on                          session.py:999\n"
    assert len(before) == len(after)
    redacted = rdc.redact(before, ctx.default_rules())
    assert redacted == rdc.redact(after, ctx.default_rules())
    assert redacted.endswith(" session.py:<n>\n")
    assert len(redacted) == len(before) - len("999") + len("<n>")


def test_an_unrecorded_step_runs_but_writes_no_artifact(tmp_path):
    marker = tmp_path / "scratch" / "ran"
    m = _manifest(
        tmp_path,
        '[[capture]]\nid = "step"\nrecord = false\n'
        f'argv = ["{{python}}", "-c", "open(\'{marker}\', \'w\').close()"]\n',
    )
    caps = rdc.load_manifest(m)
    ctx = _ctx(tmp_path)
    ctx.tmp.mkdir(parents=True)
    rdc.refresh(caps, ctx)
    assert marker.exists()
    assert not (ctx.captures_dir / "step.txt").exists()


def test_check_never_reports_an_unrecorded_step_missing(tmp_path, capsys):
    m = _manifest(
        tmp_path, '[[capture]]\nid = "step"\nrecord = false\nargv = ["{python}", "-c", "pass"]\n'
    )
    ctx = _ctx(tmp_path)
    ctx.tmp.mkdir(parents=True)
    assert rdc.check(rdc.load_manifest(m), ctx) == 0
    assert "MISSING" not in capsys.readouterr().out


def test_an_unrecorded_step_that_fails_still_fails_the_run(tmp_path):
    m = _manifest(
        tmp_path,
        '[[capture]]\nid = "step"\nrecord = false\n'
        'argv = ["{python}", "-c", "raise SystemExit(3)"]\n',
    )
    ctx = _ctx(tmp_path)
    ctx.tmp.mkdir(parents=True)
    with pytest.raises(rdc.CaptureError, match="exited 3"):
        rdc.refresh(rdc.load_manifest(m), ctx)


def test_record_must_be_a_boolean(tmp_path):
    m = _manifest(
        tmp_path, '[[capture]]\nid = "a"\nrecord = "no"\nargv = ["{python}", "-c", "pass"]\n'
    )
    with pytest.raises(rdc.CaptureError, match="record"):
        rdc.load_manifest(m)


def test_a_store_assertion_file_is_matched_after_substitution(tmp_path):
    """The store records absolute paths; the manifest names them through a placeholder."""
    scratch = tmp_path / "scratch"
    src = _store(
        tmp_path / "prepared.json",
        tier="manual",
        product="app",
        file=str(scratch / "src" / "main.cpp"),
        line=12,
        hits=3,
    )
    copy = "import shutil, sys; shutil.copy(sys.argv[1], sys.argv[2])"
    m = _manifest(
        tmp_path,
        '[[capture]]\nid = "cov"\n'
        f'argv = ["{{python}}", "-c", "{copy}", "{src}", "{{xdir}}/store.json"]\n'
        '[[capture.store]]\npath = "{xdir}/store.json"\nfile = "{tmp}/src/main.cpp"\n'
        'line = 12\ntier = "manual"\nproduct = "app"\nhits = 3\n',
    )
    rdc.refresh(rdc.load_manifest(m), _ctx(tmp_path))
    assert (tmp_path / "captures" / "cov.txt").exists()


def test_the_rendered_command_never_shows_the_xdir_placeholder():
    line = rdc.render_command(["{otto}", "cov", "report", "{xdir}/e2e"])
    assert "{xdir}" not in line
    assert line == f"$ otto cov report {rdc.SCRATCH_DIR}/xdir/e2e"


def test_a_marker_that_is_not_on_its_line_is_refused(tmp_path):
    project = tmp_path / "examples" / "p"
    (project / "src").mkdir(parents=True)
    (project / "src" / "x.cpp").write_text("int a;\nint b;  // qs: here\n")
    body = (
        '[[capture]]\nid = "a"\nproject = "p"\nargv = ["{python}", "-c", "pass"]\n'
        '[[capture.store]]\npath = "{xdir}/s.json"\nfile = "{project}/src/x.cpp"\n'
        'tier = "system"\nhits = 1\nmarker = "qs: here"\nline = '
    )
    rdc.verify_markers(rdc.load_manifest(_manifest(tmp_path, body + "2\n")), tmp_path / "examples")
    with pytest.raises(rdc.CaptureError, match=r"x\.cpp:1"):
        rdc.verify_markers(
            rdc.load_manifest(_manifest(tmp_path, body + "1\n")), tmp_path / "examples"
        )


def test_a_source_set_assertion_names_the_extra_file(tmp_path):
    src = _store(
        tmp_path / "prepared.json", tier="unit", product="", file="/p/src/x.cpp", line=1, hits=2
    )
    copy = "import shutil, sys; shutil.copy(sys.argv[1], sys.argv[2])"
    m = _manifest(
        tmp_path,
        '[[capture]]\nid = "cov"\n'
        f'argv = ["{{python}}", "-c", "{copy}", "{src}", "{{xdir}}/store.json"]\n'
        '[[capture.store_sources]]\npath = "{xdir}/store.json"\ntier = "unit"\nfiles = []\n',
    )
    with pytest.raises(rdc.CaptureError, match=r"/p/src/x\.cpp"):
        rdc.refresh(rdc.load_manifest(m), _ctx(tmp_path))


def test_an_overlay_replaces_the_projects_file_in_the_scratch_copy_only(tmp_path):
    examples = tmp_path / "examples"
    (examples / "p" / "lab_data").mkdir(parents=True)
    (examples / "p" / "lab_data" / "lab.json").write_text("placeholder")
    (examples / "bed" / "p" / "lab_data").mkdir(parents=True)
    (examples / "bed" / "p" / "lab_data" / "lab.json").write_text("bed")
    m = _manifest(
        tmp_path,
        '[overlays]\np = "bed/p"\n\n'
        '[[capture]]\nid = "a"\nproject = "p"\nargv = ["{python}", "-c", "pass"]\n',
    )
    ctx = _ctx(tmp_path)
    ctx.overlays = rdc.read_manifest(m).overlays
    ctx.tmp.mkdir(parents=True)
    copied = ctx.project_dir(rdc.load_manifest(m)[0])
    assert (copied / "lab_data" / "lab.json").read_text() == "bed"
    assert (examples / "p" / "lab_data" / "lab.json").read_text() == "placeholder"


def test_an_overlay_naming_a_missing_directory_is_refused(tmp_path):
    (tmp_path / "examples" / "p").mkdir(parents=True)
    m = _manifest(
        tmp_path,
        '[overlays]\np = "bed/nowhere"\n\n'
        '[[capture]]\nid = "a"\nproject = "p"\nargv = ["{python}", "-c", "pass"]\n',
    )
    ctx = _ctx(tmp_path)
    ctx.overlays = rdc.read_manifest(m).overlays
    ctx.tmp.mkdir(parents=True)
    with pytest.raises(rdc.CaptureError, match="bed/nowhere"):
        ctx.project_dir(rdc.load_manifest(m)[0])


def test_the_example_copy_leaves_an_in_place_build_tree_behind(tmp_path):
    (tmp_path / "examples" / "p" / "build").mkdir(parents=True)
    (tmp_path / "examples" / "p" / "build" / "CMakeCache.txt").write_text("x")
    (tmp_path / "examples" / "p" / "src").mkdir()
    ctx = _ctx(tmp_path)
    ctx.tmp.mkdir(parents=True)
    copied = ctx.project_dir(rdc.Capture(id="a", argv=["x"], project="p"))
    assert (copied / "src").is_dir()
    assert not (copied / "build").exists()


def test_the_defaults_are_the_shared_manifest_and_one_artifact_directory():
    assert rdc.DEFAULT_MANIFEST == rdc.EXAMPLES / "captures.toml"
    assert rdc.DEFAULT_CAPTURES == rdc.EXAMPLES / "captures"
    assert rdc.DEFAULT_MANIFEST.is_file()
    ids = {c.id for c in rdc.load_manifest(rdc.DEFAULT_MANIFEST) if c.record}
    on_disk = {p.stem for p in rdc.DEFAULT_CAPTURES.glob("*.txt")}
    assert ids == on_disk


def test_an_overlay_for_a_project_with_no_example_is_refused(tmp_path):
    (tmp_path / "examples" / "p").mkdir(parents=True)
    (tmp_path / "examples" / "bed" / "q").mkdir(parents=True)
    ctx = _ctx(tmp_path)
    ctx.overlays = {"q": "bed/q"}
    ctx.tmp.mkdir(parents=True)
    with pytest.raises(rdc.CaptureError, match="no example project 'q'"):
        ctx.project_dir(rdc.Capture(id="a", argv=["x"], project="p"))


@pytest.mark.parametrize("table", ['overlays = "bed/p"\n', "[overlays]\np = 1\n"])
def test_overlays_must_map_a_project_to_a_directory(tmp_path, table):
    with pytest.raises(rdc.CaptureError, match="overlays"):
        rdc.read_manifest(_manifest(tmp_path, table))


def test_a_manifest_without_overlays_has_none(tmp_path):
    assert rdc.read_manifest(_manifest(tmp_path, "")).overlays == {}


_CAP = '[[capture]]\nid = "a"\nargv = ["{python}", "-c", "pass"]\n'
_STORE = '[[capture.store]]\npath = "s"\nline = 1\ntier = "t"\nhits = 1\n'


@pytest.mark.parametrize(
    ("body", "said"),
    [
        (_CAP + _STORE + 'file = "{project}/x"\nmarker = "m"\n', "needs the capture's project"),
        (
            _CAP.replace('id = "a"', 'id = "a"\nproject = "p"')
            + _STORE
            + 'file = "x"\nmarker = "m"\n',
            "needs a file under",
        ),
        (_CAP + _STORE + 'file = "{project}/x"\nmarker = 3\n', "'marker' must be a str"),
        (_CAP + '[[capture.store_sources]]\npath = "s"\ntier = "t"\n', "lacks 'files'"),
        (_CAP + '[[capture.store_sources]]\npath = "s"\ntier = "t"\nfiles = "x"\n', "'files' must"),
        (_CAP + '[[capture.store_sources]]\npath = "s"\ntier = "t"\nfiles = [1]\n', "are strings"),
        (_CAP + 'store_sources = "x"\n', "are tables"),
    ],
)
def test_load_manifest_refuses_a_bad_marker_or_source_set(tmp_path, body, said):
    with pytest.raises(rdc.CaptureError, match=said):
        rdc.load_manifest(_manifest(tmp_path, body))


def test_a_marker_on_a_line_past_the_end_is_refused(tmp_path):
    (tmp_path / "examples" / "p").mkdir(parents=True)
    (tmp_path / "examples" / "p" / "x.cpp").write_text("one\n")
    cap = rdc.Capture(
        id="a",
        argv=["x"],
        project="p",
        store=[rdc.StoreAssertion("s", "{project}/x.cpp", 5, "t", 1, marker="one")],
    )
    with pytest.raises(rdc.CaptureError, match=r"x\.cpp:5"):
        rdc.verify_markers([cap], tmp_path / "examples")


def test_a_source_set_passes_on_exactly_its_files_for_a_product(tmp_path):
    src = _store(
        tmp_path / "prepared.json", tier="unit", product="app", file="/p/x.cpp", line=1, hits=2
    )
    copy = "import shutil, sys; shutil.copy(sys.argv[1], sys.argv[2])"
    m = _manifest(
        tmp_path,
        '[[capture]]\nid = "cov"\n'
        f'argv = ["{{python}}", "-c", "{copy}", "{src}", "{{xdir}}/store.json"]\n'
        '[[capture.store_sources]]\npath = "{xdir}/store.json"\ntier = "unit"\n'
        'product = "app"\nfiles = ["/p/x.cpp"]\n',
    )
    rdc.refresh(rdc.load_manifest(m), _ctx(tmp_path))
    bad = m.read_text().replace('product = "app"', 'product = "other"')
    with pytest.raises(rdc.CaptureError, match="no such run"):
        rdc.refresh(rdc.load_manifest(_manifest(tmp_path, bad)), _ctx(tmp_path))


# --- Final review: overlays stay inside the examples and the scratch dir ----


def _overlay_examples(tmp_path: Path) -> Path:
    """An examples root with projects ``p`` and ``q``, ``p``'s bed overlay, and a dir outside it.

    No capture runs ``q``: it is an example on disk the manifest never names.
    """
    examples = tmp_path / "examples"
    (examples / "p").mkdir(parents=True)
    (examples / "q").mkdir()
    (examples / "bed" / "p").mkdir(parents=True)
    (tmp_path / "elsewhere").mkdir()
    return examples


@pytest.mark.parametrize(
    ("key", "value", "said"),
    [
        ("/abs/p", "bed/p", "plain project name"),
        ("..", "bed/p", "plain project name"),
        ("p/sub", "bed/p", "plain project name"),
        ("q", "bed/p", "no capture runs project 'q'"),
        ("p", "../elsewhere", "outside"),
    ],
)
def test_main_refuses_an_overlay_that_escapes_before_copying_anything(
    tmp_path, monkeypatch, key, value, said
):
    examples = _overlay_examples(tmp_path)
    if key == "/abs/p":
        key = str(examples / "p")  # an absolute key naming the checked-in example itself
    m = _manifest(
        tmp_path,
        f'[overlays]\n"{key}" = "{value}"\n\n'
        '[[capture]]\nid = "a"\nproject = "p"\nargv = ["{python}", "-c", "pass"]\n',
    )
    copies: list[object] = []
    removals: list[object] = []
    monkeypatch.setattr(rdc, "EXAMPLES", examples)
    monkeypatch.setattr(rdc, "SCRATCH_DIR", tmp_path / "scratch")
    monkeypatch.setattr(rdc.shutil, "copytree", lambda *a, **k: copies.append(a))
    monkeypatch.setattr(rdc.shutil, "rmtree", lambda *a, **k: removals.append(a))
    with pytest.raises(rdc.CaptureError, match=said):
        rdc.main(["--manifest", str(m), "--captures-dir", str(tmp_path / "out")])
    assert copies == []
    assert removals == []


def test_an_overlay_set_on_the_context_cannot_copy_outside_the_scratch_dir(tmp_path):
    examples = _overlay_examples(tmp_path)
    ctx = _ctx(tmp_path)
    ctx.overlays = {str(examples / "p"): "bed/p"}
    ctx.tmp.mkdir(parents=True)
    with pytest.raises(rdc.CaptureError, match="plain project name"):
        ctx.project_dir(rdc.Capture(id="a", argv=["x"], project="p"))


def test_check_overlays_accepts_a_project_s_bed_overlay(tmp_path):
    examples = _overlay_examples(tmp_path)
    caps = [rdc.Capture(id="a", argv=["x"], project="p")]
    rdc.check_overlays({"p": "bed/p"}, caps, examples_root=examples, scratch=tmp_path / "s")


# --- Final review: a source set counts every file in the tier, hit or not ---


def _two_file_store(path: Path, *, product: str, other_hits: int) -> Path:
    """A unit-tier store: ``/p/x.cpp`` hit twice and ``/p/foreign.cpp`` hit *other_hits* times.

    Loaded the way otto loads counters, so a zero-hit line keeps its tier
    entry and credits no run.
    """
    from otto.coverage.merge.lcov_loader import LCOVLoader
    from otto.coverage.store.model import CoverageStore

    info = path.with_suffix(".info")
    info.write_text(
        f"SF:/p/x.cpp\nDA:1,2\nend_of_record\nSF:/p/foreign.cpp\nDA:1,{other_hits}\nend_of_record\n"
    )
    store = CoverageStore()
    run_id = store.add_run(tier="unit", product=product)
    LCOVLoader(store).load(info, "unit", run_id=run_id)
    store.save(path)
    return path


def _sources_manifest(tmp_path: Path, src: Path, product: str) -> Path:
    copy = "import shutil, sys; shutil.copy(sys.argv[1], sys.argv[2])"
    wants_product = f'product = "{product}"\n' if product else ""
    return _manifest(
        tmp_path,
        '[[capture]]\nid = "cov"\n'
        f'argv = ["{{python}}", "-c", "{copy}", "{src}", "{{xdir}}/store.json"]\n'
        '[[capture.store_sources]]\npath = "{xdir}/store.json"\ntier = "unit"\n'
        + wants_product
        + 'files = ["/p/x.cpp"]\n',
    )


@pytest.mark.parametrize("product", ["", "app"], ids=["tier-wide", "per-product"])
def test_a_source_set_refuses_an_extra_file_the_tier_never_executed(tmp_path, product):
    src = _two_file_store(tmp_path / "prepared.json", product=product, other_hits=0)
    m = _sources_manifest(tmp_path, src, product)
    with pytest.raises(rdc.CaptureError, match=r"extra \['/p/foreign\.cpp'\]"):
        rdc.refresh(rdc.load_manifest(m), _ctx(tmp_path))


def test_a_product_s_source_set_leaves_out_a_file_only_another_product_ran(tmp_path):
    from otto.coverage.merge.lcov_loader import LCOVLoader
    from otto.coverage.store.model import CoverageStore

    store = CoverageStore()
    for product, sf in (("app", "/p/x.cpp"), ("other", "/p/foreign.cpp")):
        info = tmp_path / f"{product}.info"
        info.write_text(f"SF:{sf}\nDA:1,2\nend_of_record\n")
        LCOVLoader(store).load(info, "unit", run_id=store.add_run(tier="unit", product=product))
    store.save(tmp_path / "prepared.json")
    m = _sources_manifest(tmp_path, tmp_path / "prepared.json", "app")
    rdc.refresh(rdc.load_manifest(m), _ctx(tmp_path))


# --- The manifest is read once; an ordered set is selected whole ----------


_ORDERED = (
    '[ordered]\nsets = ["p"]\n\n'
    '[[capture]]\nid = "build"\nproject = "p"\nrecord = false\nargv = ["{python}", "-c", "1"]\n'
    '[[capture]]\nid = "use"\nproject = "p"\nargv = ["{python}", "-c", "1"]\n'
    '[[capture]]\nid = "other"\nlabless = true\nargv = ["{python}", "-c", "1"]\n'
)


def test_main_parses_the_manifest_once(tmp_path, monkeypatch):
    m = _manifest(tmp_path, "[overlays]\n\n" + _CAP)
    real = rdc.tomli.loads
    parsed: list[str] = []

    def spy(text: str) -> dict[str, object]:
        parsed.append(text)
        return real(text)

    monkeypatch.setattr(rdc.tomli, "loads", spy)
    assert rdc.main(["--list", "--manifest", str(m)]) == 0
    assert parsed == [m.read_text()]


def test_an_ordered_set_is_declared_in_the_manifest(tmp_path):
    assert rdc.read_manifest(_manifest(tmp_path, _ORDERED)).ordered == ["p"]
    assert rdc.read_manifest(_manifest(tmp_path, _CAP)).ordered == []


def test_only_naming_part_of_an_ordered_set_is_refused_naming_what_is_missing(tmp_path):
    caps = rdc.read_manifest(_manifest(tmp_path, _ORDERED)).captures
    with pytest.raises(rdc.CaptureError, match=r"ordered set 'p'.*\['build'\]"):
        rdc.select(caps, labless=False, only=["use"], ordered=["p"])


def test_main_refuses_a_partial_ordered_set_before_touching_the_scratch_dir(tmp_path, monkeypatch):
    m = _manifest(tmp_path, _ORDERED)
    removals: list[object] = []
    monkeypatch.setattr(rdc, "SCRATCH_DIR", tmp_path / "scratch")
    monkeypatch.setattr(rdc.shutil, "rmtree", lambda *a, **k: removals.append(a))
    with pytest.raises(rdc.CaptureError, match=r"ordered set 'p'.*\['use'\]"):
        rdc.main(["--only", "build", "--manifest", str(m), "--captures-dir", str(tmp_path / "o")])
    assert removals == []


def test_an_ordered_set_selected_whole_or_not_at_all_is_accepted(tmp_path, capsys):
    m = _manifest(tmp_path, _ORDERED)
    common = ["--list", "--manifest", str(m)]
    assert rdc.main([*common, "--only", "use", "--only", "build"]) == 0
    assert rdc.main([*common, "--only", "other"]) == 0
    assert rdc.main(common) == 0
    assert rdc.main([*common, "--labless"]) == 0
    listed = [line.split()[0] for line in capsys.readouterr().out.splitlines()]
    assert listed == ["build", "use", "other", "build", "use", "other", "other"]


@pytest.mark.parametrize(
    ("table", "said"),
    [
        ('[ordered]\nsets = ["q"]\n', "no capture runs project 'q'"),
        ('[ordered]\nsets = "p"\n', r"\[ordered\] sets"),
        ("[ordered]\nsets = [1]\n", r"\[ordered\] sets"),
        ('[ordered]\nset = ["p"]\n', r"\[ordered\] sets"),
        ('ordered = ["p"]\n', r"\[ordered\] sets"),
    ],
)
def test_an_ordered_set_no_capture_runs_or_a_malformed_table_is_refused_at_load(
    tmp_path, table, said
):
    body = table + '\n[[capture]]\nid = "a"\nproject = "p"\nargv = ["{python}", "-c", "1"]\n'
    with pytest.raises(rdc.CaptureError, match=said):
        rdc.load_manifest(_manifest(tmp_path, body))


def test_the_shared_manifest_s_full_check_selects_every_capture_and_labless_none_of_a_set():
    manifest = rdc.read_manifest(rdc.DEFAULT_MANIFEST)
    assert "quickstart-shell" in manifest.ordered
    assert "quickstart-kmod" in manifest.ordered
    every = manifest.captures
    full = rdc.select(every, labless=False, only=[], ordered=manifest.ordered)
    assert full == every
    labless = rdc.select(every, labless=True, only=[], ordered=manifest.ordered)
    assert not [c for c in labless if c.project in manifest.ordered]


def test_no_two_projects_share_a_coverage_or_report_destination():
    """A destination one project writes is never another's, nor inside or above another's."""
    owners: dict[str, set[str]] = {}
    for cap in rdc.read_manifest(rdc.DEFAULT_MANIFEST).captures:
        for flag in ["--cov-dir", "--dir"]:
            if flag in cap.argv:
                owners.setdefault(cap.argv[cap.argv.index(flag) + 1], set()).add(cap.project)
    clashes = [
        (a, b)
        for a, pa in owners.items()
        for b, pb in owners.items()
        if (a == b and len(pa) > 1)
        or (a != b and pa != pb and (b.startswith(a + "/") or a.startswith(b + "/")))
    ]
    assert not clashes, clashes
    # Checked after the clashes: a shared destination also lowers the count,
    # and the clash is the message that names the defect.
    assert len(owners) >= 4, owners  # both tracks' --cov-dir and report --dir
