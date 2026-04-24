"""Integration tests for the completion cache fast path.

These tests invoke ``python -m otto`` as a subprocess with the
``_OTTO_COMPLETE`` environment variable set, exercising the real
shell-completion code path. Subprocess coverage is captured via
``coverage.process_startup()`` (enabled by ``coverage_subprocess.pth``)
and combined into the parent run by pytest-cov's ``parallel = true``.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parent.parent.parent.parent
REPO1 = PROJECT_ROOT / 'tests' / 'repo1'
COVERAGERC = PROJECT_ROOT / '.coveragerc'
# sitecustomize.py in this dir calls coverage.process_startup(); prepending
# it to PYTHONPATH opts each subprocess into subprocess-coverage without
# affecting the rest of the suite (a global .pth file deadlocks
# test_coverage_e2e.py's asyncssh subprocesses).
COVERAGE_BOOTSTRAP = PROJECT_ROOT / 'tests' / '_coverage_bootstrap'
# Use the installed `otto` entrypoint so Click sees sys.argv[0] == 'otto'
# and reacts to _OTTO_COMPLETE. Running via `python -m otto` gives Click
# the program name `__main__`, which it won't match against our env var.
OTTO_BIN = Path(sys.executable).parent / 'otto'


def _run_otto(
    argv: list[str],
    *,
    xdir: Path | None,
    comp_words: str | None = None,
    comp_cword: str | None = None,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    env = {
        # Clean-ish environment: keep PATH/HOME but drop anything that
        # might leak a user's real OTTO_* configuration into the test.
        'PATH': os.environ.get('PATH', ''),
        'HOME': os.environ.get('HOME', ''),
        'OTTO_SUT_DIRS': str(REPO1),
        # Subprocess coverage: coverage_subprocess.pth runs
        # coverage.process_startup() when this env var points at a config.
        'COVERAGE_PROCESS_START': str(COVERAGERC),
        # Prepend the coverage bootstrap dir so sitecustomize.py runs
        # coverage.process_startup() before otto imports anything.
        'PYTHONPATH': os.pathsep.join(
            [str(COVERAGE_BOOTSTRAP), os.environ.get('PYTHONPATH', '')]
        ).rstrip(os.pathsep),
    }
    if xdir is not None:
        env['OTTO_XDIR'] = str(xdir)
    if comp_words is not None:
        env['_OTTO_COMPLETE'] = 'complete_bash'
        env['COMP_WORDS'] = comp_words
        env['COMP_CWORD'] = comp_cword or '1'
    if extra_env:
        env.update(extra_env)

    return subprocess.run(
        [str(OTTO_BIN), *argv],
        env=env,
        capture_output=True,
        text=True,
        cwd=str(PROJECT_ROOT),
        timeout=60,
    )


def _cache_file(xdir: Path) -> Path:
    return xdir / '.otto' / 'completion_cache.json'


def _read_cache(xdir: Path) -> dict:
    return json.loads(_cache_file(xdir).read_text())


def test_slow_path_seeds_cache(tmp_path: Path) -> None:
    """A non-completion invocation writes the cache for the next run."""
    result = _run_otto(['--help'], xdir=tmp_path)
    assert result.returncode == 0, result.stderr

    cache = _read_cache(tmp_path)
    assert len(cache) == 1
    entry = next(iter(cache.values()))
    assert entry['schema_version'] == 3
    assert isinstance(entry['generated_at'], int)
    instruction_names = {i['name'] for i in entry['instructions']}
    suite_names = {s['name'] for s in entry['suites']}
    assert 'test-instruction' in instruction_names
    assert {'TestDevice', 'TestCoverageProduct'} <= suite_names
    # Host IDs from tests/lab_data/tech1/hosts.json — co-cached alongside
    # instructions/suites so `otto host <TAB>` hits the fast path.
    assert {'carrot_seed', 'tomato_seed', 'pepper_seed'} <= set(entry['hosts'])


def test_slow_path_seeds_cache_with_option_schemas(tmp_path: Path) -> None:
    """Cached suites/instructions carry their option schemas for reconstruction."""
    result = _run_otto(['--help'], xdir=tmp_path)
    assert result.returncode == 0, result.stderr

    entry = next(iter(_read_cache(tmp_path).values()))
    # Pick a suite that we know has user-defined Options — TestDevice in repo1.
    test_device = next(s for s in entry['suites'] if s['name'] == 'TestDevice')
    opts = test_device['options']
    assert opts, "expected TestDevice to have cached options"
    option_names = {o['name'] for o in opts}
    # Options dataclass fields — suite-specific plus inherited RepoOptions.
    assert {'firmware', 'check_interfaces'} <= option_names
    # Runner options (markers/iterations/...) live on the parent callback
    # and must NOT appear in per-suite cached schemas.
    assert option_names.isdisjoint(
        {'markers', 'iterations', 'duration', 'threshold', 'results'},
    )
    # Each option dict carries the keys the rebuilder needs
    first = opts[0]
    assert set(first.keys()) >= {'name', 'flags', 'kind', 'default', 'help'}
    assert first['kind'] in {'str', 'int', 'float', 'bool', 'path', 'str_list'}


def test_fast_path_uses_cache_for_top_level_completion(tmp_path: Path) -> None:
    """After the cache is seeded, `otto <TAB>` returns the known subcommands."""
    seed = _run_otto(['--help'], xdir=tmp_path)
    assert seed.returncode == 0

    result = _run_otto(
        [],
        xdir=tmp_path,
        comp_words='otto ',
        comp_cword='1',
    )
    assert result.returncode == 0, result.stderr
    # Click completion emits one candidate per line as "plain,<name>".
    names = {line.split(',', 1)[-1] for line in result.stdout.splitlines() if line}
    assert {'run', 'test', 'monitor', 'cov', 'host'} <= names


def test_fast_path_returns_cached_instructions(tmp_path: Path) -> None:
    """`otto run <TAB>` should list instructions from the cache."""
    _run_otto(['--help'], xdir=tmp_path)

    result = _run_otto(
        [],
        xdir=tmp_path,
        comp_words='otto run ',
        comp_cword='2',
    )
    assert result.returncode == 0, result.stderr
    assert 'test-instruction' in result.stdout


def test_fast_path_returns_cached_suites(tmp_path: Path) -> None:
    """`otto test <TAB>` should list registered suite names."""
    _run_otto(['--help'], xdir=tmp_path)

    result = _run_otto(
        [],
        xdir=tmp_path,
        comp_words='otto test ',
        comp_cword='2',
    )
    assert result.returncode == 0, result.stderr
    assert 'TestDevice' in result.stdout
    assert 'TestCoverageProduct' in result.stdout


def test_touching_test_file_invalidates_cache(tmp_path: Path) -> None:
    """Bumping a tracked test file's mtime produces a new fingerprint entry."""
    _run_otto(['--help'], xdir=tmp_path)
    before = _read_cache(tmp_path)
    assert len(before) == 1
    old_fp = next(iter(before))

    # Bump mtime of a tracked file (fingerprint uses path|mtime_ns|size).
    tracked = REPO1 / 'tests' / 'test_example.py'
    st = tracked.stat()
    new_mtime = st.st_mtime_ns + 1_000_000_000  # +1s in ns
    os.utime(tracked, ns=(st.st_atime_ns, new_mtime))
    try:
        _run_otto(['--help'], xdir=tmp_path)
        after = _read_cache(tmp_path)
        assert len(after) == 2, f"expected a new fingerprint entry, got {list(after)}"
        assert old_fp in after  # stale entries are left in place
    finally:
        os.utime(tracked, ns=(st.st_atime_ns, st.st_mtime_ns))


def test_no_xdir_disables_caching(tmp_path: Path) -> None:
    """Without OTTO_XDIR, no cache file is written and the CLI still works."""
    result = _run_otto(['--help'], xdir=None)
    assert result.returncode == 0, result.stderr
    assert not _cache_file(tmp_path).exists()


def test_fast_path_without_matching_cache_falls_through(tmp_path: Path) -> None:
    """Completion with an empty xdir should still succeed (cache miss → slow path)."""
    result = _run_otto(
        [],
        xdir=tmp_path,
        comp_words='otto ',
        comp_cword='1',
    )
    assert result.returncode == 0, result.stderr
    names = {line.split(',', 1)[-1] for line in result.stdout.splitlines() if line}
    assert {'run', 'test'} <= names
    # Slow path should have written the cache on the way out.
    assert _cache_file(tmp_path).exists()


def test_fast_path_returns_static_parent_options(tmp_path: Path) -> None:
    """`otto test --<TAB>` must include parent-callback options like --cov.

    Regression guard: the old fast path attached empty placeholder sub-Typers
    that stripped these options from the completion surface.
    """
    _run_otto(['--help'], xdir=tmp_path)

    result = _run_otto(
        [],
        xdir=tmp_path,
        comp_words='otto test --',
        comp_cword='2',
    )
    assert result.returncode == 0, result.stderr
    flags = set(result.stdout.splitlines())
    # Click emits "plain,--flag\n"; match on substring to cover both shapes.
    blob = result.stdout
    # Runner options (--markers / --iterations / --threshold / ...) live on the
    # ``otto test`` parent callback alongside --cov / --cov-dir / --list-suites.
    for flag in ('--cov', '--cov-dir', '--list-suites',
                 '--markers', '--iterations', '--duration',
                 '--threshold', '--results'):
        assert flag in blob, f'{flag!r} missing from: {flags!r}'


def test_fast_path_returns_rebuilt_suite_options(tmp_path: Path) -> None:
    """`otto test TestDevice --<TAB>` must include cached Options-derived flags."""
    _run_otto(['--help'], xdir=tmp_path)

    result = _run_otto(
        [],
        xdir=tmp_path,
        comp_words='otto test TestDevice --',
        comp_cword='3',
    )
    assert result.returncode == 0, result.stderr
    blob = result.stdout
    # --firmware comes from the TestDevice.Options dataclass and proves the
    # Annotated[...] reconstruction path survived serialization round-trip.
    assert '--firmware' in blob, f'--firmware missing from: {blob!r}'
    # Runner options now live on the parent callback, not each suite — they
    # must NOT appear in per-suite completion.
    for flag in ('--markers', '--iterations', '--threshold'):
        assert flag not in blob, f'{flag!r} unexpectedly in per-suite output: {blob!r}'


def test_fast_path_returns_host_ids_for_host_subcommand(tmp_path: Path) -> None:
    """`otto host <TAB>` must complete host IDs from the configured hosts.json.

    The completer in ``otto.cli.host`` runs during completion before
    ``applyRepoSettings()`` — this test proves host IDs reach the shell.
    """
    _run_otto(['--help'], xdir=tmp_path)

    result = _run_otto(
        [],
        xdir=tmp_path,
        comp_words='otto host ',
        comp_cword='2',
    )
    assert result.returncode == 0, result.stderr
    # tech1/hosts.json in repo1 defines carrot_seed, tomato_seed, pepper_seed.
    blob = result.stdout
    for host_id in ('carrot_seed', 'tomato_seed', 'pepper_seed'):
        assert host_id in blob, f'{host_id!r} missing from: {blob!r}'


def test_clear_autocomplete_cache_removes_file(tmp_path: Path) -> None:
    """The --clear-autocomplete-cache escape hatch unlinks the cache file."""
    _run_otto(['--help'], xdir=tmp_path)
    cache_file = _cache_file(tmp_path)
    assert cache_file.exists()

    result = _run_otto(['--clear-autocomplete-cache'], xdir=tmp_path)
    assert result.returncode == 0, result.stderr
    assert not cache_file.exists()


def test_ttl_expiry_invalidates_cache(tmp_path: Path) -> None:
    """Cache entries older than the TTL are treated as a miss."""
    import time as _time

    _run_otto(['--help'], xdir=tmp_path)
    data = _read_cache(tmp_path)
    # Push every entry 25h into the past — past the 24h TTL.
    stale_ts = int(_time.time()) - 25 * 60 * 60
    for entry in data.values():
        entry['generated_at'] = stale_ts
    _cache_file(tmp_path).write_text(json.dumps(data))

    # Run completion; fast path must reject the stale entries and rewrite.
    result = _run_otto(
        [],
        xdir=tmp_path,
        comp_words='otto ',
        comp_cword='1',
    )
    assert result.returncode == 0, result.stderr

    refreshed = _read_cache(tmp_path)
    now = int(_time.time())
    # At least one entry must carry a fresh generated_at. Using "any" rather
    # than pinning to the original fingerprint keeps the test robust against
    # sibling tests that race to bump tracked file mtimes.
    assert any(
        abs(entry['generated_at'] - now) < 60
        for entry in refreshed.values()
    ), f'no refreshed entry after TTL expiry: {refreshed!r}'
