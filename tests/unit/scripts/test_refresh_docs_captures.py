"""The capture runner: a manifest in, deterministic artifacts out, drift reported (spec §5).

No otto command is run here — the manifest points at ``python -c`` so the
tests exercise substitution, redaction, expected-exit handling and the
--check diff without a bed or a project.
"""

import contextlib
import fcntl
import json
import os
import signal
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

from otto.config.env import without_otto_env
from scripts import refresh_docs_captures as rdc
from scripts.docs_display_env import DISPLAY_ENV
from tests._fixtures._host_pool import DOCKER_HOST
from tests._fixtures.bed_locks import bed_lock_root
from tests._fixtures.paths import CHECKOUT_ROOT
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


def test_the_per_command_sudo_prompt_is_redacted_to_one_placeholder(tmp_path):
    """A logged elevated command keeps its shape and loses only the nonce."""
    from otto.host.local_host import LocalHost

    ctx = _ctx(tmp_path)
    first, second = (LocalHost()._elevate("insmod x.ko").command for _ in range(2))
    assert first != second, "premise: each command gets its own prompt"
    redacted = {rdc.redact(f"INFO @dut | {cmd}", ctx.default_rules()) for cmd in (first, second)}
    assert redacted == {"INFO @dut | SUDO_PROMPT='otto-sudo-<nonce>:' sudo -S insmod x.ko"}


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
        "INFO     @zephyr37_fat   | kernel uptime          host.py:<n>\n"
        "DEBUG    framed write cmd='x'                    session.py:<n>\n"
        "see host.py:12 for details\n"
    )


def _log_line(message: str, source: str) -> str:
    """One log record as Rich renders it: the source column ends at the display width."""
    width = int(DISPLAY_ENV["COLUMNS"])
    return message + " " * (width - len(message) - len(source)) + source + "\n"


def test_a_redacted_source_column_does_not_depend_on_the_line_number_s_width(tmp_path):
    """Rich pads the message so the ``file.py:<line>`` column ends at the right edge.

    A line number that gains or loses a digit moves that padding by one, so a
    redaction that replaced only the digits still drifted whenever an edit
    carried a log call across 999/1000. The two lines below are one log record
    from before and after such an edit; they must normalize to the same text.
    """
    ctx = _ctx(tmp_path)
    before = _log_line("DEBUG    begin marker matched on", "session.py:1001")
    after = _log_line("DEBUG    begin marker matched on", "session.py:999")
    normalized = rdc.normalize(before, ctx.default_rules())
    assert normalized == rdc.normalize(after, ctx.default_rules())
    assert normalized == _log_line("DEBUG    begin marker matched on", "session.py:<n>")


def test_a_redacted_value_in_the_message_does_not_move_the_source_column(tmp_path):
    """A manifest rule that replaces a number in the message narrows the message too.

    The padding Rich put in front of the source column took up that number's
    width, so a guest whose uptime gained or lost a digit drifted by a space
    after ``Uptime: <n> ms``. The column on a record's first row is aligned
    after every rule has run. (A record Rich wraps is not covered: the wrap
    point itself moves with the source column's width and the raw value's.)
    """
    ctx = _ctx(tmp_path)
    rules = [*ctx.default_rules(), (r"Uptime: \d+ ms", "Uptime: <n> ms")]
    short = _log_line("INFO     @zephyr37_fat > | Uptime: 3900123 ms", "host.py:3103")
    long = _log_line("INFO     @zephyr37_fat > | Uptime: 13900123 ms", "host.py:3103")
    normalized = rdc.normalize(short, rules)
    assert normalized == rdc.normalize(long, rules)
    assert normalized == _log_line("INFO     @zephyr37_fat > | Uptime: <n> ms", "host.py:<n>")


def test_a_captured_artifact_is_aligned_after_the_manifest_rules(tmp_path):
    """The capture run itself normalizes: a manifest rule's narrowing is re-padded too."""
    ctx = _ctx(tmp_path)
    line = _log_line("INFO     @zephyr37_fat > | Uptime: 13900123 ms", "host.py:3103")
    program = tmp_path / "log.py"
    program.write_text(f"print({line.rstrip()!r})\n")
    m = _manifest(
        tmp_path,
        f'[[capture]]\nid = "log"\nargv = ["{{python}}", "{program}"]\n'
        'redact = [["Uptime: \\\\d+ ms", "Uptime: <n> ms"]]\n',
    )
    rdc.refresh(rdc.load_manifest(m), ctx)
    artifact = (ctx.captures_dir / "log.txt").read_text()
    assert artifact.splitlines()[1:] == [
        _log_line("INFO     @zephyr37_fat > | Uptime: <n> ms", "host.py:<n>").rstrip("\n")
    ]


def test_a_line_wider_than_the_display_keeps_one_space_before_its_source(tmp_path):
    """A message that already fills the display keeps one space before its source column."""
    ctx = _ctx(tmp_path)
    message = "DEBUG    " + "x" * int(DISPLAY_ENV["COLUMNS"])
    assert rdc.normalize(f"{message}  session.py:12\n", ctx.default_rules()) == (
        f"{message} session.py:<n>\n"
    )


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


# --- A capture's env, a set's always-run teardown, and the docker host lease ---

_ECHO_SUFFIX = ["{python}", "-c", "import os; print(os.environ.get('OTTO_COMPOSE_SUFFIX'))"]


def test_a_capture_env_reaches_the_command_after_the_otto_strip(tmp_path, monkeypatch):
    monkeypatch.setenv("OTTO_COMPOSE_SUFFIX", "leaked")
    cap = rdc.Capture(id="a", argv=_ECHO_SUFFIX, env={"OTTO_COMPOSE_SUFFIX": "docs"})
    assert rdc.run_capture(cap, _ctx(tmp_path)).splitlines()[-1] == "docs"


def test_a_capture_env_does_not_reach_the_next_capture(tmp_path):
    ctx = _ctx(tmp_path)
    rdc.run_capture(
        rdc.Capture(id="a", argv=_ECHO_SUFFIX, env={"OTTO_COMPOSE_SUFFIX": "docs"}), ctx
    )
    assert rdc.run_capture(rdc.Capture(id="b", argv=_ECHO_SUFFIX), ctx).splitlines()[-1] == "None"


@pytest.mark.parametrize(
    "env", ['["A"]', "{ A = 1 }", '"A=1"', '{ "" = "v" }', '{ "A=B" = "v" }', '{ "1A" = "v" }']
)
def test_a_capture_env_is_a_table_of_strings(tmp_path, env):
    m = _manifest(tmp_path, f'[[capture]]\nid = "a"\nargv = ["x"]\nenv = {env}\n')
    with pytest.raises(rdc.CaptureError, match=r"'a': env is a table of strings"):
        rdc.load_manifest(m)


def test_a_capture_env_cannot_override_what_the_runner_pins(tmp_path):
    ctx = _ctx(tmp_path)
    (ctx.examples_root / "p").mkdir(parents=True)
    for name in ctx.pinned_env(rdc.Capture(id="x", argv=[], project="p")):
        cap = rdc.Capture(id="a", argv=["true"], project="p", env={name: "v"})
        with pytest.raises(rdc.CaptureError, match=rf"env sets \['{name}'\]"):
            rdc.run_capture(cap, ctx)


def test_the_env_is_the_stripped_environ_plus_the_pins_plus_the_capture_s(tmp_path):
    ctx = _ctx(tmp_path)
    (ctx.examples_root / "p").mkdir(parents=True)
    cap = rdc.Capture(id="a", argv=[], project="p", env={"OTTO_COMPOSE_SUFFIX": "docs"})
    assert ctx.env(cap) == {**without_otto_env(os.environ), **ctx.pinned_env(cap), **cap.env}


def test_the_termynal_captures_share_the_runner_s_display_pins(tmp_path, monkeypatch):
    from scripts import capture_docs_termynal, docs_display_env

    monkeypatch.setitem(docs_display_env.DISPLAY_ENV, "COLUMNS", "77")
    ctx = _ctx(tmp_path)
    pinned = ctx.pinned_env(rdc.Capture(id="a", argv=[]))
    termynal = capture_docs_termynal._otto_env(tmp_path / "demo")
    for name, value in docs_display_env.DISPLAY_ENV.items():
        assert termynal[name] == pinned[name] == value


def test_leases_parse_and_default_to_none(tmp_path):
    body = '[[capture]]\nid = "a"\nproject = "p"\nargv = ["x"]\n'
    assert rdc.read_manifest(_manifest(tmp_path, body)).leases == {}
    leased = body + '[leases]\ndocker_host = ["p"]\n'
    assert rdc.read_manifest(_manifest(tmp_path, leased)).leases == {"docker_host": ["p"]}


@pytest.mark.parametrize(
    ("table", "said"),
    [
        ('[leases]\nconsole = ["p"]\n', "maps a lease"),
        ('[leases]\ndocker_host = "p"\n', "maps a lease"),
        ('[leases]\ndocker_host = ["q"]\n', "no capture runs project 'q'"),
    ],
)
def test_a_bad_leases_table_is_refused(tmp_path, table, said):
    body = '[[capture]]\nid = "a"\nproject = "p"\nargv = ["x"]\n' + table
    with pytest.raises(rdc.CaptureError, match=said):
        rdc.read_manifest(_manifest(tmp_path, body))


def test_only_a_selection_holding_a_leased_project_takes_the_lease():
    caps = [rdc.Capture(id="a", argv=[], project="p"), rdc.Capture(id="b", argv=[], labless=True)]
    assert rdc.leased(caps, {"docker_host": ["p"]}) == ["docker_host"]
    assert rdc.leased(caps[1:], {"docker_host": ["p"]}) == []


def test_an_always_capture_followed_by_an_ordinary_one_is_refused(tmp_path):
    body = (
        '[[capture]]\nid = "a"\nproject = "p"\nalways = true\nargv = ["x"]\n'
        '[[capture]]\nid = "b"\nproject = "p"\nargv = ["x"]\n'
    )
    with pytest.raises(rdc.CaptureError, match="'b' follows the always capture 'a'"):
        rdc.load_manifest(_manifest(tmp_path, body))


def test_the_tail_rule_names_projectless_captures_as_one_group(tmp_path):
    body = (
        '[[capture]]\nid = "a"\nalways = true\nargv = ["x"]\n[[capture]]\nid = "b"\nargv = ["x"]\n'
    )
    with pytest.raises(rdc.CaptureError) as err:
        rdc.load_manifest(_manifest(tmp_path, body))
    assert "'b' follows the always capture 'a' of the captures with no project" in str(err.value)


def test_every_lease_a_manifest_may_name_is_one_held_leases_takes(monkeypatch):
    taken: list[str] = []

    @contextlib.contextmanager
    def taker(name: str):
        taken.append(name)
        yield

    monkeypatch.setattr(rdc, "_LEASE_TAKERS", {n: (lambda n=n: taker(n)) for n in rdc.LEASES})
    with rdc.held_leases(list(rdc.LEASES)):
        assert taken == list(rdc.LEASES)


def test_always_must_be_a_boolean(tmp_path):
    m = _manifest(tmp_path, '[[capture]]\nid = "a"\nalways = "yes"\nargv = ["x"]\n')
    with pytest.raises(rdc.CaptureError, match="'a': always is true or false"):
        rdc.load_manifest(m)


def test_unordered_must_be_a_boolean(tmp_path):
    m = _manifest(tmp_path, '[[capture]]\nid = "a"\nunordered = "yes"\nargv = ["x"]\n')
    with pytest.raises(rdc.CaptureError, match="'a': unordered is true or false"):
        rdc.load_manifest(m)


_TWO_LINES = '[[capture]]\nid = "u"\n{flag}argv = ["{{python}}", "-c", "print(1); print(2)"]\n'
_TWO_LINES_HEAD = "$ python -c 'print(1); print(2)'\n"


@pytest.mark.parametrize(
    ("body", "unordered", "drifts"),
    [
        ("2\n1\n", True, False),  # reordered: the same lines
        ("2\n1\n", False, True),  # an ordered capture still sees the order
        ("1\n2\n2\n", True, True),  # an extra copy of a line
        ("1\n", True, True),  # a missing line
        ("1\n3\n", True, True),  # a changed line
    ],
    ids=["reordered", "ordered-reordered", "extra", "missing", "changed"],
)
def test_an_unordered_check_ignores_only_the_order(tmp_path, capsys, body, unordered, drifts):
    ctx = _ctx(tmp_path)
    flag = "unordered = true\n" if unordered else ""
    caps = rdc.load_manifest(_manifest(tmp_path, _TWO_LINES.format(flag=flag)))
    rdc.refresh(caps, ctx)
    assert (ctx.captures_dir / "u.txt").read_text() == _TWO_LINES_HEAD + "1\n2\n"
    (ctx.captures_dir / "u.txt").write_text(_TWO_LINES_HEAD + body)
    capsys.readouterr()
    assert rdc.check(caps, ctx) == (1 if drifts else 0)
    assert ("DRIFT u" in capsys.readouterr().out) is drifts


def _probe(lock: Path, out: Path) -> list[str]:
    """A capture argv that appends to *out* whether *lock* is held by someone else."""
    code = (
        "import fcntl, os, sys; fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT)\n"
        "try:\n    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB); state = 'free'\n"
        "except BlockingIOError:\n    state = 'held'\n"
        "open(sys.argv[2], 'a').write(state + '\\n')\n"
    )
    return ["{python}", "-c", code, str(lock), str(out)]


def _leased_manifest(tmp_path: Path, monkeypatch, captures: str) -> Path:
    examples = tmp_path / "examples"
    make_project(examples / "p")
    monkeypatch.setattr(rdc, "EXAMPLES", examples)
    # main() runs in this test's own scratch dir, never the machine-wide one.
    monkeypatch.setattr(rdc, "SCRATCH_DIR", tmp_path / "scratch")
    return _manifest(tmp_path, '[leases]\ndocker_host = ["p"]\n' + captures)


def test_every_capture_runs_inside_the_lease_cleanup_included(tmp_path, monkeypatch):
    lock = bed_lock_root() / f"unix_pool.{DOCKER_HOST}"  # the autouse per-test root
    seen = tmp_path / "seen"
    probe = json.dumps(_probe(lock, seen))
    m = _leased_manifest(
        tmp_path,
        monkeypatch,
        f'[[capture]]\nid = "body"\nproject = "p"\nrecord = false\nargv = {probe}\n'
        '[[capture]]\nid = "boom"\nproject = "p"\n'
        'argv = ["{python}", "-c", "raise SystemExit(3)"]\n'
        f'[[capture]]\nid = "skipped"\nproject = "p"\nrecord = false\nargv = {probe}\n'
        '[[capture]]\nid = "clean"\nproject = "p"\nalways = true\nrecord = false\n'
        f"argv = {probe}\n",
    )
    with pytest.raises(rdc.CaptureError, match="'boom' exited 3"):
        rdc.main(["--manifest", str(m), "--captures-dir", str(tmp_path / "out")])
    # body and clean ran, both while the lease was held; skipped never ran.
    assert seen.read_text().splitlines() == ["held", "held"]
    assert _lease_is_free()  # released once main returned


def test_a_failing_cleanup_after_a_failure_keeps_the_first_error(tmp_path, monkeypatch, capsys):
    m = _leased_manifest(
        tmp_path,
        monkeypatch,
        '[[capture]]\nid = "boom"\nproject = "p"\n'
        'argv = ["{python}", "-c", "raise SystemExit(3)"]\n'
        '[[capture]]\nid = "clean"\nproject = "p"\nalways = true\nrecord = false\n'
        'argv = ["{python}", "-c", "raise SystemExit(4)"]\n',
    )
    with pytest.raises(rdc.CaptureError, match="'boom' exited 3"):
        rdc.main(["--manifest", str(m), "--captures-dir", str(tmp_path / "out")])
    assert "'clean' exited 4" in capsys.readouterr().err


def test_a_failing_cleanup_on_a_good_run_fails_the_run(tmp_path, monkeypatch):
    m = _leased_manifest(
        tmp_path,
        monkeypatch,
        '[[capture]]\nid = "clean"\nproject = "p"\nalways = true\nrecord = false\n'
        'argv = ["{python}", "-c", "raise SystemExit(4)"]\n',
    )
    with pytest.raises(rdc.CaptureError, match="'clean' exited 4"):
        rdc.main(["--manifest", str(m), "--captures-dir", str(tmp_path / "out")])


@pytest.mark.parametrize("mode", [[], ["--check"]])
def test_after_a_failure_an_always_capture_writes_no_artifact(tmp_path, monkeypatch, capsys, mode):
    """Refreshed or checked, a teardown run after a failure is recorded nowhere."""
    m = _leased_manifest(
        tmp_path,
        monkeypatch,
        '[[capture]]\nid = "boom"\nproject = "p"\n'
        'argv = ["{python}", "-c", "raise SystemExit(3)"]\n'
        '[[capture]]\nid = "clean"\nproject = "p"\nalways = true\nargv = ["true"]\n',
    )
    with pytest.raises(rdc.CaptureError, match="'boom' exited 3"):
        rdc.main([*mode, "--manifest", str(m), "--captures-dir", str(tmp_path / "out")])
    assert not (tmp_path / "out" / "clean.txt").exists()
    out = capsys.readouterr().out
    assert "captured clean" not in out
    assert "MISSING" not in out
    assert "DRIFT" not in out


def test_a_store_assertion_failure_still_runs_the_cleanup(tmp_path, monkeypatch):
    """The report step fails on its assertion, after its command succeeded."""
    marker = tmp_path / "cleaned"
    m = _leased_manifest(
        tmp_path,
        monkeypatch,
        '[[capture]]\nid = "report"\nproject = "p"\nargv = ["true"]\n'
        '[[capture.store]]\npath = "{tmp}/missing.json"\nfile = "x"\nline = 1\n'
        'tier = "system"\nhits = 1\n'
        f'[[capture]]\nid = "clean"\nproject = "p"\nalways = true\nrecord = false\n'
        f'argv = ["touch", "{marker}"]\n',
    )
    with pytest.raises(rdc.CaptureError, match="cannot read the store"):
        rdc.main(["--manifest", str(m), "--captures-dir", str(tmp_path / "out")])
    assert marker.exists()


def test_the_runner_waits_for_a_pooled_docker_lease_held_elsewhere(tmp_path):
    """The docker e2e's pooled fixture, in another process, holds test3: the runner waits.

    The holder releases only after the runner has started asking, and stamps the
    moment it lets go; the runner's lease must come after that stamp. Order, not
    a duration, so a loaded machine cannot pass or fail it by timing. The holder
    inherits this test's own bed-lock root through the environment.
    """
    asking = tmp_path / "asking"
    with subprocess.Popen(
        [
            sys.executable,
            "-c",
            textwrap.dedent("""
                import pathlib, sys, time
                from tests._fixtures._host_pool import DOCKER_HOST, lease_unix_host
                asking = pathlib.Path(sys.argv[1])
                with lease_unix_host((DOCKER_HOST,)):
                    print("held", flush=True)
                    while not asking.exists():
                        time.sleep(0.01)
                    time.sleep(0.5)
                    print(time.time(), flush=True)
            """),
            str(asking),
        ],
        env={**os.environ, "PYTHONPATH": str(CHECKOUT_ROOT)},
        stdout=subprocess.PIPE,
        text=True,
    ) as holder:  # closes the pipe on the way out
        assert holder.stdout is not None
        assert holder.stdout.readline().strip() == "held"
        asking.touch()
        with rdc.held_leases(["docker_host"]):
            acquired = time.time()
        released = float(holder.stdout.readline())
        holder.wait(timeout=10)
    assert acquired >= released


def test_list_takes_no_lease(tmp_path, monkeypatch):
    from tests._fixtures import _host_pool

    monkeypatch.setattr(_host_pool, "docker_host_lease", lambda: pytest.fail("leased"))
    body = '[leases]\ndocker_host = ["p"]\n[[capture]]\nid = "a"\nproject = "p"\nargv = ["x"]\n'
    assert rdc.main(["--list", "--manifest", str(_manifest(tmp_path, body))]) == 0


def test_an_interrupt_propagates_without_the_teardown():
    stepped: list[str] = []

    def step(cap: rdc.Capture, recorded: bool) -> None:
        stepped.append(cap.id)
        if cap.id == "a":
            raise KeyboardInterrupt

    caps = [rdc.Capture(id="a", argv=[]), rdc.Capture(id="t", argv=[], always=True)]
    with pytest.raises(KeyboardInterrupt):
        rdc._run_set(caps, step)
    assert stepped == ["a"]


def _lease_is_free() -> bool:
    fd = os.open(bed_lock_root() / f"unix_pool.{DOCKER_HOST}", os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    finally:
        os.close(fd)
    return True


def test_an_interrupt_still_releases_the_lease(tmp_path, monkeypatch):
    m = _leased_manifest(
        tmp_path, monkeypatch, '[[capture]]\nid = "a"\nproject = "p"\nargv = ["true"]\n'
    )

    def interrupted(cap: rdc.Capture, ctx: rdc.RunContext) -> str:
        raise KeyboardInterrupt

    monkeypatch.setattr(rdc, "run_capture", interrupted)
    with pytest.raises(KeyboardInterrupt) as err:
        rdc.main(["--manifest", str(m), "--captures-dir", str(tmp_path / "out")])
    # The traceback keeps main's frame alive, so a lease left to the garbage
    # collector is still held here; only one released on the way out is free.
    assert err.traceback
    assert _lease_is_free()


def test_a_sigterm_to_the_runner_alone_kills_the_capture_before_the_lease_ends(
    tmp_path, monkeypatch
):
    """The capture signals only its parent, the runner; its own child must die with the run."""
    pid_file = tmp_path / "child.pid"
    code = (
        "import os, signal, sys, time; open(sys.argv[1], 'w').write(str(os.getpid()));"
        " os.kill(os.getppid(), signal.SIGTERM); time.sleep(3)"
    )
    argv = json.dumps(["{python}", "-c", code, str(pid_file)])
    m = _leased_manifest(
        tmp_path, monkeypatch, f'[[capture]]\nid = "a"\nproject = "p"\nargv = {argv}\n'
    )
    seen: list[int] = []

    # Without the runner's own handler, this one only records the signal, so
    # the run finishes normally and the test fails instead of pytest dying.
    def record(signum: int, frame: object) -> None:
        seen.append(signum)

    previous = signal.signal(signal.SIGTERM, record)
    try:
        with pytest.raises(rdc.Terminated):
            rdc.main(["--manifest", str(m), "--captures-dir", str(tmp_path / "out")])
        assert signal.getsignal(signal.SIGTERM) is record  # put back
    finally:
        signal.signal(signal.SIGTERM, previous)
    with pytest.raises(ProcessLookupError):  # killed and reaped, not a zombie
        os.kill(int(pid_file.read_text()), 0)
    assert _lease_is_free()


# --- A set's teardown armed by its own preflight ---


def _armed(*, arm_fails: bool = False, earlier_fails: bool = False) -> "tuple[list[str], str]":
    """Run an arming set through ``_run_set``; return what ran and the first error's text."""
    caps = [
        rdc.Capture(id="other", argv=[], project="q"),
        rdc.Capture(id="pre", argv=[], project="p", arms_teardown=True),
        rdc.Capture(id="body", argv=[], project="p"),
        rdc.Capture(id="down", argv=[], project="p", always=True, armed_by="pre"),
        rdc.Capture(id="sweep", argv=[], project="p", always=True, armed_by="pre"),
    ]
    stepped: list[str] = []
    failing = {"pre"} if arm_fails else {"other"} if earlier_fails else {"body"}

    def step(cap: rdc.Capture, recorded: bool) -> None:
        stepped.append(cap.id)
        if cap.id in failing:
            raise rdc.CaptureError(f"{cap.id} failed")

    with pytest.raises(rdc.CaptureError) as err:
        rdc._run_set(caps, step)
    return stepped, str(err.value)


def test_a_failed_arming_capture_skips_its_set_s_teardown(capsys):
    stepped, first = _armed(arm_fails=True)
    assert stepped == ["other", "pre"]
    assert first == "pre failed"
    err = capsys.readouterr().err
    assert "skipped down: pre did not succeed in this run" in err
    assert "skipped sweep: pre did not succeed in this run" in err


def test_an_arming_capture_that_never_ran_skips_its_set_s_teardown():
    stepped, first = _armed(earlier_fails=True)
    assert stepped == ["other"]
    assert first == "other failed"


def test_an_armed_teardown_runs_after_a_later_failure():
    stepped, first = _armed()
    assert stepped == ["other", "pre", "body", "down", "sweep"]
    assert first == "body failed"


def test_an_unselected_arming_capture_leaves_the_teardown_unarmed(tmp_path, capsys):
    m = _manifest(
        tmp_path,
        '[[capture]]\nid = "pre"\nproject = "p"\narms_teardown = true\nargv = ["x"]\n'
        '[[capture]]\nid = "down"\nproject = "p"\nalways = true\nargv = ["x"]\n',
    )
    caps = rdc.select(rdc.load_manifest(m), labless=False, only=["down"])
    assert [c.armed_by for c in caps] == ["pre"]
    stepped: list[str] = []
    rdc._run_set(caps, lambda cap, _recorded: stepped.append(cap.id))
    assert stepped == []
    assert "skipped down" in capsys.readouterr().err


def test_the_manifest_binds_each_always_capture_to_its_set_s_arming_capture(tmp_path):
    m = _manifest(
        tmp_path,
        '[[capture]]\nid = "pre"\nproject = "p"\narms_teardown = true\nargv = ["x"]\n'
        '[[capture]]\nid = "down"\nproject = "p"\nalways = true\nargv = ["x"]\n'
        '[[capture]]\nid = "clean"\nproject = "q"\nalways = true\nargv = ["x"]\n',
    )
    assert {c.id: c.armed_by for c in rdc.load_manifest(m)} == {
        "pre": None,
        "down": "pre",
        "clean": None,
    }


@pytest.mark.parametrize(
    ("body", "said"),
    [
        ('[[capture]]\nid = "a"\narms_teardown = "yes"\nargv = ["x"]\n', "arms_teardown is true"),
        ('[[capture]]\nid = "a"\narms_teardown = true\nargv = ["x"]\n', "needs project"),
        (
            (
                '[[capture]]\nid = "a"\nproject = "p"\nalways = true\narms_teardown = true\n'
                'argv = ["x"]\n'
            ),
            "cannot be an always capture",
        ),
        (
            (
                '[[capture]]\nid = "a"\nproject = "p"\narms_teardown = true\nargv = ["x"]\n'
                '[[capture]]\nid = "b"\nproject = "p"\narms_teardown = true\nargv = ["x"]\n'
            ),
            "already has 'a'",
        ),
    ],
)
def test_load_manifest_refuses_a_bad_arming_capture(tmp_path, body, said):
    with pytest.raises(rdc.CaptureError, match=said):
        rdc.load_manifest(_manifest(tmp_path, body))


# --- [snippets]: one text several captures share ---


def test_a_snippet_is_expanded_where_an_argv_entry_names_it(tmp_path):
    m = _manifest(
        tmp_path,
        "[snippets]\nprobe = 'test -d /x'\n"
        '[[capture]]\nid = "a"\nargv = ["sh", "-c", "{snippet:probe} && {snippet:probe}"]\n'
        '[[capture]]\nid = "b"\nargv = ["{snippet:probe}", "{tmp}"]\n',
    )
    manifest = rdc.read_manifest(m)
    assert manifest.snippets == {"probe": "test -d /x"}
    assert [c.argv for c in manifest.captures] == [
        ["sh", "-c", "test -d /x && test -d /x"],
        ["test -d /x", "{tmp}"],
    ]


def test_a_snippet_no_entry_defines_is_refused(tmp_path):
    m = _manifest(tmp_path, '[[capture]]\nid = "a"\nargv = ["{snippet:nope}"]\n')
    with pytest.raises(rdc.CaptureError, match="capture 'a': no \\[snippets\\] entry 'nope'"):
        rdc.load_manifest(m)


@pytest.mark.parametrize(
    "table", ["snippets = 1\n", "[snippets]\nx = 1\n", "[snippets]\n'a-b' = 'x'\n"]
)
def test_snippets_map_a_name_to_a_text(tmp_path, table):
    with pytest.raises(rdc.CaptureError, match=r"\[snippets\] maps a name"):
        rdc.load_manifest(_manifest(tmp_path, table))
