"""The file-op counter: every path syscall of the whole process tree, bucketed."""

import json
from pathlib import Path

import pytest

from tests._fixtures.budget_harness import load_harness

harness = load_harness()

WS = "/tmp/fixture-root/"
BUCKETS = harness.PathBuckets(
    site_packages=["/venv/lib/python3.10/site-packages/"],
    stdlib=["/py/lib/python3.10/"],
    otto_src="/src/otto/",
    workspace=[WS],
)


def _parse(text: str):
    return harness.parse_file_ops(text, workspace_prefixes=[WS], buckets=BUCKETS)


def test_every_path_syscall_counts_including_opens_and_listings():
    ops = _parse(
        '100 newfstatat(AT_FDCWD, "/src/otto/host/__init__.py", {...}, 0) = 0\n'
        '100 openat(AT_FDCWD, "/src/otto/host/__pycache__/x.pyc", O_RDONLY) = 3\n'
        "100 getdents64(3, 0x0 /* 4 entries */, 32768) = 96\n"
    )
    assert ops.total == 3
    assert ops.by_bucket == {"otto:host": 2, "fd": 1}


def test_child_processes_count_and_are_named_by_the_program_they_exec():
    ops = _parse(
        '100 openat(AT_FDCWD, "/py/lib/python3.10/os.py", O_RDONLY) = 3\n'
        '101 execve("/usr/bin/gcc", ["gcc"], 0x0 /* 1 var */) = 0\n'
        '101 openat(AT_FDCWD, "/usr/lib/gcc/x.so", O_RDONLY) = 3\n'
    )
    assert ops.total == 3
    assert ops.by_process == {"python": 1, "gcc": 2}


def test_kernel_virtual_filesystems_are_excluded():
    ops = _parse('100 openat(AT_FDCWD, "/proc/self/status", O_RDONLY) = 3\n')
    assert ops.total == 0


def test_workspace_is_the_subset_under_the_fixture_root_and_home():
    ops = _parse(
        f'100 newfstatat(AT_FDCWD, "{WS}genrepo/tests", {{...}}, 0) = 0\n'
        "100 newfstatat(AT_FDCWD, "
        '"/venv/lib/python3.10/site-packages/asyncssh/__init__.py", {...}, 0) = 0\n'
    )
    assert (ops.total, ops.workspace) == (2, 1)
    assert ops.by_bucket == {"workspace": 1, "site:asyncssh": 1}


def test_an_unfinished_call_counts_once():
    ops = _parse(
        '100 openat(AT_FDCWD, "/py/lib/python3.10/os.py", O_RDONLY <unfinished ...>\n'
        "100 <... openat resumed>) = 3\n"
    )
    assert ops.total == 1


def test_dist_info_and_extension_names_reduce_to_the_import_name():
    site = "/venv/lib/python3.10/site-packages/"
    b = BUCKETS.bucket
    assert b(site + "pydantic_core-2.33.dist-info/RECORD") == "site:pydantic_core"
    assert b(site + "_cffi_backend.cpython-310-aarch64-linux-gnu.so") == "site:_cffi_backend"
    assert b(site + "six.py") == "site:six"


def test_a_root_directory_itself_is_charged_to_its_root():
    """The import system stats each sys.path directory per import; that is not "other"."""
    ops = _parse(
        '100 newfstatat(AT_FDCWD, "/py/lib/python3.10", {...}, 0) = 0\n'
        '100 newfstatat(AT_FDCWD, "/src/otto", {...}, 0) = 0\n'
        '100 newfstatat(AT_FDCWD, "/venv/lib/python3.10/site-packages", {...}, 0) = 0\n'
        '100 newfstatat(AT_FDCWD, "/tmp/fixture-root", {...}, 0) = 0\n'
        '100 newfstatat(AT_FDCWD, "/src/ottox/y.py", {...}, 0) = 0\n'
    )
    assert ops.by_bucket == {
        "stdlib": 1,
        "otto:*": 1,
        "site:*": 1,
        "workspace": 1,
        "other": 1,
    }
    assert ops.workspace == 1


def test_a_bytecode_cache_path_is_charged_as_the_source_it_mirrors():
    """A ``.pyc`` read from the harness's cache is its module's cost, not "other"."""
    prefix = "/home/u/.cache/otto/import-budget-pycache"
    ops = harness.parse_file_ops(
        f'100 openat(AT_FDCWD, "{prefix}/venv/lib/python3.10/site-packages/rich/'
        'console.cpython-310.pyc", O_RDONLY) = 3\n'
        f'100 openat(AT_FDCWD, "{prefix}{WS}genrepo/pylib/m.cpython-310.pyc", O_RDONLY) = -1\n'
        f'100 openat(AT_FDCWD, "{prefix}-other/x.pyc", O_RDONLY) = 3\n',
        workspace_prefixes=[WS],
        buckets=harness.PathBuckets(
            site_packages=["/venv/lib/python3.10/site-packages/"],
            stdlib=["/py/lib/python3.10/"],
            otto_src="/src/otto/",
            workspace=[WS],
        ),
        bytecode_prefix=prefix,
    )
    assert ops.by_bucket == {"site:rich": 1, "workspace": 1, "other": 1}
    assert ops.workspace == 1


def test_missing_strace_fails_by_name(monkeypatch):
    monkeypatch.setattr(harness.shutil, "which", lambda name: None)
    with pytest.raises(RuntimeError, match="apt-get install strace"):
        harness.strace_executable()


def test_measure_file_ops_counts_a_real_surface():
    ops = harness.measure_file_ops(harness.surface_by_key("version_repo"))
    assert isinstance(ops, harness.FileOps)
    assert ops.total > 0
    assert ops.by_process == {"python": ops.total}
    assert "stdlib" in ops.by_bucket
    assert any(b.startswith("otto:") for b in ops.by_bucket)


def _ssh_exec():
    return harness.surface_by_key("host_ssh_exec")


def test_an_ssh_surface_that_never_reached_ssh_is_a_violation():
    """Exit 1 alone is not the refused connection: an unknown host id also exits 1.

    `otto host <typo> exec` prints "No host with ID" and raises `typer.Exit(1)`,
    which escapes as no exception at all. If the exit code were the whole check,
    a misspelled id or a lab source that failed to load would pass the SSH
    surfaces green, and their shrunken count would read as a saving.
    """
    unknown_host = {"exit_code": 1, "exception": None}
    violations = harness.check_exit(_ssh_exec(), unknown_host)
    assert violations
    assert "no exception" in violations[0]


def test_the_refused_connection_satisfies_an_ssh_surface():
    refused = {
        "exit_code": 1,
        "exception": "builtins.ConnectionRefusedError: "
        "[Errno 111] Connect call failed ('127.0.0.1', 40123)",
    }
    assert harness.check_exit(_ssh_exec(), refused) == []


def test_an_unexpected_exception_is_named_in_the_violation():
    surface = harness.surface_by_key("version_repo")
    crashed = {"exit_code": 1, "exception": "builtins.KeyError: 'boom'"}
    [violation] = harness.check_exit(surface, crashed)
    assert "builtins.KeyError: 'boom'" in violation


def test_ceiling_is_ten_percent_with_a_small_absolute_floor():
    assert harness.ceiling(1000, 0.10) == 1100
    assert harness.ceiling(20, 0.10) == 25
    assert harness.ceiling(0, 0.10) == 5


def test_a_gated_surface_over_its_ceiling_fails_with_a_breakdown(tmp_path, monkeypatch):
    surface = harness.surface_by_key("dispatch_repo_warm")
    monkeypatch.setattr(
        harness,
        "read_ceilings",
        lambda: {
            "dispatch_repo_warm": {
                "file_ops": 1000,
                "workspace": 20,
                "by_bucket": {"stdlib": 900, "workspace": 20},
                "by_process": {"python": 1000},
            }
        },
    )
    result = {
        "exit_code": 0,
        "file_ops": {
            "total": 1700,
            "workspace": 20,
            "by_bucket": {"stdlib": 900, "workspace": 20, "site:asyncssh": 598},
            "by_process": {"python": 1688, "gcc": 12},
        },
    }
    violations = harness.check_surface(surface, result, floor_file_ops=1000)
    text = "\n".join(violations)
    assert "file_ops" in text
    assert "1700" in text
    assert "1100" in text
    assert "+598 site:asyncssh" in text
    assert "+12 process gcc" in text


def test_a_tracked_surface_is_never_gated():
    tracked = next(s for s in harness.SURFACES if s.tracked)
    assert harness.check_surface(tracked, {"exit_code": 1, "file_ops": {"total": 10**9}}) == []


def test_shrinking_is_an_advisory_never_a_failure(monkeypatch):
    surface = harness.surface_by_key("dispatch_repo_warm")
    monkeypatch.setattr(
        harness,
        "read_ceilings",
        lambda: {
            "dispatch_repo_warm": {
                "file_ops": 1000,
                "workspace": 20,
                "by_bucket": {},
                "by_process": {},
            }
        },
    )
    result = {
        "exit_code": 0,
        "file_ops": {"total": 500, "workspace": 20, "by_bucket": {}, "by_process": {}},
    }
    assert harness.check_surface(surface, result, floor_file_ops=1000) == []
    assert any("file_ops" in n for n in harness.advisories(surface, result))


def _ops(total: int, *, by_bucket: dict | None = None, by_process: dict | None = None):
    return harness.FileOps(
        total=total,
        workspace=0,
        by_bucket=by_bucket or {},
        by_process=by_process or {"python": total},
    )


def _gate_pair(
    monkeypatch, *, floor_total: int, surface_total: int, baseline_total: int | None = None
) -> None:
    """Make ``--check`` see ``version_repo`` and ``dispatch_repo_warm`` measure as given.

    Listed surface first, floor second, so the test also proves ``main``
    measures the floor before any surface whose target divides by it. Each
    baseline is its own measurement unless *baseline_total* overrides both.
    """
    floor = harness.surface_by_key(harness.RATIO_FLOOR)
    surface = harness.surface_by_key("dispatch_repo_warm")

    def baseline(total: int) -> dict:
        total = total if baseline_total is None else baseline_total
        return {"file_ops": total, "workspace": 20, "by_bucket": {}, "by_process": {}}

    def result(total: int) -> dict:
        ops = {"total": total, "workspace": 20, "by_bucket": {}, "by_process": {}}
        return {"exit_code": 0, "exception": None, "file_ops": ops}

    monkeypatch.setattr(
        harness,
        "read_ceilings",
        lambda: {floor.key: baseline(floor_total), surface.key: baseline(surface_total)},
    )
    results = {floor.key: result(floor_total), surface.key: result(surface_total)}
    monkeypatch.setattr(harness, "SURFACES", [surface, floor])
    monkeypatch.setattr(harness, "measure_surface", lambda s: results[s.key])


def test_a_surface_over_its_target_ratio_fails_check_and_under_it_passes(monkeypatch, capsys):
    """Through ``--check``: the same surface, red over its target and green under it.

    Against a floor of 1000, ``dispatch_repo_warm``'s target admits exactly
    ``target * 1000`` file operations; one more fails.
    """
    target = harness.surface_by_key("dispatch_repo_warm").target_ratio
    limit = int(target * 1000)
    _gate_pair(monkeypatch, floor_total=1000, surface_total=limit + 1)
    assert harness.main(["--check"]) == 1
    out = capsys.readouterr().out
    fail = next(line for line in out.splitlines() if "FAIL" in line)
    for fact in ["`dispatch_repo_warm`", f"file_ops {limit + 1}", "version_repo's 1000"]:
        assert fact in fail, (fact, fail)
    assert f"over its target of {target:g}x (at most {limit})" in fail, fail

    _gate_pair(monkeypatch, floor_total=1000, surface_total=limit)
    assert harness.main(["--check"]) == 0
    out = capsys.readouterr().out
    assert "FAIL" not in out, out
    assert "import budget: OK" in out, out


def test_a_ratio_failure_names_its_largest_buckets_and_its_child_processes():
    """A ratio breach need not be a ceiling breach, so it carries its own breakdown."""
    surface = harness.surface_by_key("dispatch_repo_warm")
    ops = _ops(
        5000,
        by_bucket={"stdlib": 3000, "site:asyncssh": 600, "otto:host": 200},
        by_process={"python": 4988, "gcc": 12},
    )
    [message] = harness.check_ratio(surface, ops, 1000)
    lines = message.splitlines()
    assert "  3000 stdlib" in lines, message
    assert "  600 site:asyncssh" in lines, message
    assert "  12 process gcc" in lines, message
    assert not any("process python" in line for line in lines), message
    assert "`--report-json`" in message, message
    assert "child process" in message, message


def test_a_target_ratio_without_its_floor_is_the_callers_mistake():
    """A surface with a target is never checked against nothing: it raises, naming the floor."""
    surface = harness.surface_by_key("dispatch_repo_warm")
    with pytest.raises(ValueError, match="version_repo's file_ops"):
        harness.check_ratio(surface, _ops(10**6), None)
    assert harness.check_ratio(harness.surface_by_key("run"), _ops(10**6), None) == []


def test_a_stale_ceiling_prints_a_note_and_still_passes_check(monkeypatch, capsys):
    """Through ``--check`` itself: a NOTE line, no FAIL, and a passing exit."""
    _gate_pair(monkeypatch, floor_total=400, surface_total=400, baseline_total=1000)
    assert harness.main(["--check"]) == 0
    out = capsys.readouterr().out
    assert "  NOTE `dispatch_repo_warm`: file_ops 400" in out, out
    assert "FAIL" not in out, out
    assert "import budget: OK" in out, out


def test_a_counter_at_its_baseline_earns_no_note(monkeypatch):
    """The absolute floor puts a small counter under 80% of its ceiling by itself.

    A ``workspace`` of 1 has a ceiling of 6; measuring 1 again is the floor at
    work, not a stale baseline, so it must stay silent or every non-repo
    surface would print a NOTE on every run.
    """
    surface = harness.surface_by_key("run")
    monkeypatch.setattr(
        harness,
        "read_ceilings",
        lambda: {"run": {"file_ops": 1000, "workspace": 1, "by_bucket": {}, "by_process": {}}},
    )
    result = {"file_ops": {"total": 1000, "workspace": 1, "by_bucket": {}, "by_process": {}}}
    assert harness.advisories(surface, result) == []


def test_a_missing_baseline_fails_by_name(monkeypatch):
    """The message must carry the file, the interpreter, and the fix; never a skip."""
    monkeypatch.setattr(harness, "interpreter_tag", lambda: "9.99")
    surface = harness.surface_by_key("version_repo")
    result = {
        "exit_code": 0,
        "exception": None,
        "file_ops": {"total": 700, "workspace": 3, "by_bucket": {}, "by_process": {}},
    }
    [message] = harness.check_surface(surface, result)
    assert "9.99.json" in message
    assert "CPython 9.99" in message
    assert "make import-snapshot" in message
    assert "version_repo" in message


def test_ceilings_round_trip_gated_surfaces_only(tmp_path, monkeypatch):
    """The file stores baselines, sorted, and never a tracked surface."""
    monkeypatch.setattr(harness, "CEILINGS_DIR", tmp_path)
    ops = harness.FileOps(
        total=10, workspace=2, by_bucket={"stdlib": 10}, by_process={"python": 10}
    )
    harness.write_ceilings({"run": ops, "tracked_init": ops})
    path = harness.ceilings_path()
    assert path == tmp_path / f"{harness.interpreter_tag()}.json"
    text = path.read_text()
    assert json.loads(text) == {
        "interpreter": harness.interpreter_tag(),
        "surfaces": {
            "run": {
                "file_ops": 10,
                "workspace": 2,
                "by_bucket": {"stdlib": 10},
                "by_process": {"python": 10},
            }
        },
    }
    assert text == json.dumps(json.loads(text), indent=2, sort_keys=True) + "\n"
    assert harness.read_ceilings() == json.loads(text)["surfaces"]


def test_a_breakdown_is_capped_and_says_what_it_left_out():
    measured = harness.FileOps(by_bucket={f"site:p{i:02d}": 100 - i for i in range(20)})
    lines = harness.breakdown_diff({"by_bucket": {}, "by_process": {}}, measured)
    assert len(lines) == harness.BREAKDOWN_LINES
    assert lines[0] == "+100 site:p00 (0 → 100)"
    assert lines[-1] == "... and 9 more that grew"


@pytest.mark.parametrize(
    "argv",
    [["--check", "--report"], ["--update", "--report-json", "x.json"], ["--check", "--update"]],
)
def test_a_measuring_flag_never_combines_with_a_gating_one(argv, monkeypatch):
    """``--check --report`` used to measure and exit 0 without checking anything."""
    monkeypatch.setattr(harness, "measure_surface", lambda _s: pytest.fail("measured"))
    with pytest.raises(SystemExit) as exc:
        harness.main(argv)
    assert exc.value.code == 2


def test_a_measurement_warms_the_bytecode_cache_first(tmp_path, monkeypatch):
    """Every measured child reads a WARM cache, so measuring must compile it first.

    Without the warm-up every module would miss on every run: deterministic,
    but a cost no installation pays after its first run. The cache is pointed
    at an empty ``tmp_path`` and the once-per-process memo cleared, so the
    only thing that can put otto's bytecode there is the measurement itself
    (no measured child writes bytecode).
    """
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    prefix = harness.bytecode_prefix()
    assert prefix.startswith(str(tmp_path)), prefix
    otto_init = Path(harness._interpreter_paths()["otto_src"], "__init__.py")
    harness._warm_bytecode.cache_clear()
    try:
        harness.measure_surface(harness.surface_by_key("import_otto"))
        cached = harness.cached_bytecode(prefix, otto_init)
        assert cached.is_file(), cached
    finally:
        # Later measurements in this worker must warm the real cache again.
        harness._warm_bytecode.cache_clear()


def test_an_unwritable_bytecode_cache_fails_by_name(tmp_path, monkeypatch):
    """A cache nobody can write to would record cold counts as baselines; refuse instead."""
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("")
    monkeypatch.setenv("XDG_CACHE_HOME", str(blocker))
    harness._warm_bytecode.cache_clear()
    try:
        with pytest.raises(RuntimeError, match="writable"):
            harness._warm_bytecode()
    finally:
        harness._warm_bytecode.cache_clear()


def test_ceilings_from_another_interpreter_fail_by_name(tmp_path, monkeypatch):
    monkeypatch.setattr(harness, "CEILINGS_DIR", tmp_path)
    harness.ceilings_path().write_text(json.dumps({"interpreter": "9.99", "surfaces": {}}))
    with pytest.raises(ValueError, match=r"9\.99"):
        harness.read_ceilings()


def test_a_baseline_missing_a_counter_fails_by_name(tmp_path, monkeypatch):
    monkeypatch.setattr(harness, "CEILINGS_DIR", tmp_path)
    harness.ceilings_path().write_text(
        json.dumps(
            {
                "interpreter": harness.interpreter_tag(),
                "surfaces": {"run": {"file_ops": 10, "by_bucket": {}, "by_process": {}}},
            }
        )
    )
    with pytest.raises(ValueError, match=r"`run`.*workspace"):
        harness.read_ceilings()
