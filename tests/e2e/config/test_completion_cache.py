"""Integration tests for the completion cache fast path.

These tests invoke the installed ``otto`` entrypoint as a subprocess with the
``_OTTO_COMPLETE`` environment variable set, exercising the real
shell-completion code path.  (The installed entrypoint rather than ``python -m
otto``: Click matches ``_OTTO_COMPLETE`` against ``sys.argv[0]``, which ``-m``
sets to ``__main__``.)  The controlled environment and subprocess-coverage
wiring come from :func:`tests.e2e._otto_subprocess.run_otto`.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from otto.config.completion_cache import SCHEMA_VERSION
from otto.config.home import workspace_key
from otto.config.remote_completion_cache import REMOTE_CACHE_FILENAME
from tests.e2e._otto_subprocess import REPO1, REPO_E2E, assert_no_output_dir, run_otto

pytestmark = [pytest.mark.hostless, pytest.mark.xdist_group("completion_cache")]


def _run_otto(
    argv: list[str],
    *,
    xdir: Path | None,
    comp_words: str | None = None,
    comp_cword: str | None = None,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run ``otto ARGV`` against the repo1 fixture, optionally in completion mode.

    *comp_words* / *comp_cword* add the trio a completing shell would export;
    everything else (clean env, subprocess coverage, ``OTTO_XDIR``) comes from
    the shared runner.
    """
    env: dict[str, str] = {}
    if comp_words is not None:
        env["_OTTO_COMPLETE"] = "complete_bash"
        env["COMP_WORDS"] = comp_words
        env["COMP_CWORD"] = comp_cword or "1"
    if extra_env:
        env.update(extra_env)

    # Pin the user-level home under the test's own tmp dir. The completion
    # cache lives in the WORKSPACE HOME now, not the xdir, so without this
    # every test here would read and write the developer's real ~/.otto.
    if xdir is not None:
        env.setdefault("OTTO_HOME", str(_home(xdir)))
    return run_otto(argv, xdir=xdir, sut_dirs=REPO1, extra_env=env)


def _home(xdir: Path) -> Path:
    """This test's private OTTO_HOME, kept beside its xdir."""
    return xdir / "otto-home"


def _cache_file(xdir: Path) -> Path:
    """Where the cache lands: the workspace home, keyed by the SUT-dir set.

    Derived through ``workspace_key`` rather than spelled out, so this helper
    cannot drift from the product's own keying -- which is the whole reason the
    cache moved out of the xdir.
    """
    return _home(xdir) / workspace_key([REPO1]) / "completion_cache.json"


def _read_cache(xdir: Path) -> dict:
    return json.loads(_cache_file(xdir).read_text())


def test_slow_path_seeds_cache(tmp_path: Path) -> None:
    """A non-completion invocation writes the cache for the next run."""
    result = _run_otto(["--help"], xdir=tmp_path)
    assert result.returncode == 0, result.stderr

    cache = _read_cache(tmp_path)
    # Freshly seeded cache carries the current top-level schema stamp;
    # stale-version invalidation is separately pinned by the unit test that
    # writes SCHEMA_VERSION - 1 (tests/unit/config/test_completion_cache_unit.py).
    assert cache["schema"] == SCHEMA_VERSION
    sections = cache["sections"]
    assert set(sections) == {"names", "tests"}
    names = sections["names"]
    assert isinstance(names["generated_at"], int)
    assert names["tainted"] is False
    payload = names["payload"]
    instruction_names = {i["name"] for i in payload["instructions"]}
    suite_names = {s["name"] for s in payload["suites"]}
    assert "test-instruction" in instruction_names
    assert {"TestDevice", "TestCoverageProduct"} <= suite_names
    # Host IDs from tests/_fixtures/lab_data/tech1/lab.json — co-cached
    # alongside instructions/suites so `otto host <TAB>` hits the fast path.
    assert {"test1", "test2", "test3"} <= set(payload["hosts"])
    # docker-capable parents are cached separately so `otto docker --on <TAB>`
    # only suggests hosts that can actually run containers. All three unix
    # VMs are docker-capable (test1/test2 gained docker for the e2e pool).
    assert payload["docker_hosts"] == ["test1", "test2", "test3"]
    # `otto --help` is informational (it seeds the completion cache under
    # the workspace home, not an output dir) — no per-invocation run dir is created.
    assert_no_output_dir(tmp_path)


def test_slow_path_seeds_cache_with_option_schemas(tmp_path: Path) -> None:
    """Cached suites/instructions carry their option schemas for reconstruction."""
    result = _run_otto(["--help"], xdir=tmp_path)
    assert result.returncode == 0, result.stderr

    payload = _read_cache(tmp_path)["sections"]["names"]["payload"]
    # Pick a suite that we know has user-defined Options — TestDevice in repo1.
    test_device = next(s for s in payload["suites"] if s["name"] == "TestDevice")
    opts = test_device["options"]
    assert opts, "expected TestDevice to have cached options"
    option_names = {o["name"] for o in opts}
    # Options dataclass fields — suite-specific plus inherited RepoOptions.
    assert {"firmware", "check_interfaces"} <= option_names
    # Runner options (markers/iterations/...) live on the parent callback
    # and must NOT appear in per-suite cached schemas.
    assert option_names.isdisjoint(
        {"markers", "iterations", "duration", "threshold", "results"},
    )
    # Each option dict carries the keys the rebuilder needs
    first = opts[0]
    assert set(first.keys()) >= {"name", "flags", "kind", "default", "help"}
    assert first["kind"] in {"str", "int", "float", "bool", "path", "str_list"}


def test_fast_path_uses_cache_for_top_level_completion(tmp_path: Path) -> None:
    """After the cache is seeded, `otto <TAB>` returns the known subcommands."""
    seed = _run_otto(["--help"], xdir=tmp_path)
    assert seed.returncode == 0

    result = _run_otto(
        [],
        xdir=tmp_path,
        comp_words="otto ",
        comp_cword="1",
    )
    assert result.returncode == 0, result.stderr
    # Click completion emits one candidate per line as "plain,<name>".
    names = {line.split(",", 1)[-1] for line in result.stdout.splitlines() if line}
    assert {"run", "test", "monitor", "cov", "host"} <= names


def test_fast_path_returns_cached_instructions(tmp_path: Path) -> None:
    """`otto run <TAB>` should list instructions from the cache."""
    _run_otto(["--help"], xdir=tmp_path)

    result = _run_otto(
        [],
        xdir=tmp_path,
        comp_words="otto run ",
        comp_cword="2",
    )
    assert result.returncode == 0, result.stderr
    assert "test-instruction" in result.stdout


def test_fast_path_returns_cached_suites(tmp_path: Path) -> None:
    """`otto test <TAB>` should list registered suite names."""
    _run_otto(["--help"], xdir=tmp_path)

    result = _run_otto(
        [],
        xdir=tmp_path,
        comp_words="otto test ",
        comp_cword="2",
    )
    assert result.returncode == 0, result.stderr
    assert "TestDevice" in result.stdout
    assert "TestCoverageProduct" in result.stdout


def _host_completions(result: subprocess.CompletedProcess[str]) -> set[str]:
    """Parse Click's ``plain,<name>`` completion lines into a name set."""
    return {line.split(",", 1)[-1] for line in result.stdout.splitlines() if line}


# tech1/lab.json splits into two labs: `unix` (test1/test2/test3) and
# `embedded` (test4/zephyr*). Lab-scoped `otto host <TAB>` must show one, not both.
_UNIX = {"test1", "test2", "test3"}
# Every Zephyr element carries an underscore (`zephyr37_lfs`) and slugs to a
# hyphen (`zephyr37-lfs`) under the host-id rules: an underscore *inside* an
# element folds to a hyphen; only the `_` between element and board survives.
# None of these hosts declares a board, so every id here is the slug of the
# element alone.
_EMBEDDED = {"test4", "zephyr37-fat", "zephyr27-fat", "zephyr37-lfs"}


def test_host_completion_scoped_by_lab_flag(tmp_path: Path) -> None:
    """`otto -l unix host <TAB>` offers only the unix lab's hosts."""
    seed = _run_otto(["--help"], xdir=tmp_path)
    assert seed.returncode == 0, seed.stderr

    result = _run_otto(
        [],
        xdir=tmp_path,
        comp_words="otto -l unix host ",
        comp_cword="4",
    )
    assert result.returncode == 0, result.stderr
    names = _host_completions(result)
    # unix members + the always-present built-in `local` are offered;
    # embedded-lab hosts are filtered out.
    assert names >= _UNIX
    assert "local" in names
    assert names.isdisjoint(_EMBEDDED), f"embedded hosts leaked: {names & _EMBEDDED}"


def test_host_completion_scoped_by_lab_envvar(tmp_path: Path) -> None:
    """`OTTO_LAB=embedded otto host <TAB>` offers only the embedded lab's hosts."""
    seed = _run_otto(["--help"], xdir=tmp_path)
    assert seed.returncode == 0, seed.stderr

    result = _run_otto(
        [],
        xdir=tmp_path,
        comp_words="otto host ",
        comp_cword="2",
        extra_env={"OTTO_LAB": "embedded"},
    )
    assert result.returncode == 0, result.stderr
    names = _host_completions(result)
    assert names >= _EMBEDDED
    assert "local" in names
    assert names.isdisjoint(_UNIX), f"unix hosts leaked: {names & _UNIX}"


def test_host_completion_unscoped_shows_all_hosts(tmp_path: Path) -> None:
    """Without a lab, `otto host <TAB>` still offers the whole fleet."""
    seed = _run_otto(["--help"], xdir=tmp_path)
    assert seed.returncode == 0, seed.stderr

    result = _run_otto([], xdir=tmp_path, comp_words="otto host ", comp_cword="2")
    assert result.returncode == 0, result.stderr
    names = _host_completions(result)
    assert names >= (_UNIX | _EMBEDDED | {"local"})


def test_touching_test_file_invalidates_cache(tmp_path: Path) -> None:
    """Bumping a tracked test file's mtime forces a rebuild with new digests."""
    _run_otto(["--help"], xdir=tmp_path)
    before = _read_cache(tmp_path)["sections"]

    # Bump mtime of a tracked file (the digests use path|mtime_ns|size).
    tracked = REPO1 / "tests" / "test_example.py"
    st = tracked.stat()
    new_mtime = st.st_mtime_ns + 1_000_000_000  # +1s in ns
    os.utime(tracked, ns=(st.st_atime_ns, new_mtime))
    try:
        _run_otto(["--help"], xdir=tmp_path)
        after = _read_cache(tmp_path)["sections"]
        # A TOP-LEVEL test file keys BOTH sections (it can register a suite,
        # and it is a test source), so both digests must move. The sections
        # map is rewritten in place: under v15 there is exactly one entry per
        # section, so stale digests no longer pile up as dead top-level keys.
        for name in ("names", "tests"):
            assert after[name]["fingerprint"] != before[name]["fingerprint"], (
                f"{name} digest did not move on a top-level test edit"
            )
    finally:
        os.utime(tracked, ns=(st.st_atime_ns, st.st_mtime_ns))


def test_no_xdir_still_caches(tmp_path: Path) -> None:
    """Without OTTO_XDIR the cache still works -- it lives in the workspace home.

    THIS TEST PREVIOUSLY ASSERTED THE OPPOSITE, and the inversion is the point.
    The cache used to be stored under the xdir, so ``_cache_path`` returned
    None when no xdir was set and an operator who never set one had completion
    caching silently disabled on every invocation -- paying the slow path
    forever without being told. The workspace home is a stable per-user
    location derived from ``OTTO_SUT_DIRS`` alone, so that case no longer
    exists.
    """
    home = tmp_path / "otto-home"
    result = _run_otto(["--help"], xdir=None, extra_env={"OTTO_HOME": str(home)})
    assert result.returncode == 0, result.stderr
    assert list(home.rglob("completion_cache.json")), (
        f"no cache written under {home}; no-xdir must no longer disable caching"
    )


def test_fast_path_without_matching_cache_falls_through(tmp_path: Path) -> None:
    """Completion with an empty xdir should still succeed (cache miss → slow path)."""
    result = _run_otto(
        [],
        xdir=tmp_path,
        comp_words="otto ",
        comp_cword="1",
    )
    assert result.returncode == 0, result.stderr
    names = {line.split(",", 1)[-1] for line in result.stdout.splitlines() if line}
    assert {"run", "test"} <= names
    # Slow path should have written the cache on the way out.
    assert _cache_file(tmp_path).exists()


def test_fast_path_completes_plugin_group_children(tmp_path: Path) -> None:
    """`otto e2etool <TAB>` on the FAST path lists the plugin group's children.

    Bootstrap is skipped during completion, so the plugin's Typer app never
    materializes — the children must come back from the cache's serialized
    child metadata (closing the boundary previously documented in
    extending-cli.md).
    """
    plugin_repo = {"OTTO_SUT_DIRS": str(REPO_E2E)}
    seed = _run_otto(["--help"], xdir=tmp_path, extra_env=plugin_repo)
    assert seed.returncode == 0, seed.stderr

    result = _run_otto(
        [],
        xdir=tmp_path,
        comp_words="otto e2etool ",
        comp_cword="2",
        extra_env=plugin_repo,
    )
    assert result.returncode == 0, result.stderr
    assert "ping" in result.stdout
    assert "pong" in result.stdout


def test_slow_path_descends_into_subcommand(tmp_path: Path) -> None:
    """`otto run <TAB>` on a cache miss must list live instruction names.

    Pins that completion descent resolves the real run group via the
    pending-token snapshot alone (no cache, no COMP_WORDS token sniffing).
    """
    result = _run_otto(
        [],
        xdir=tmp_path,
        comp_words="otto run ",
        comp_cword="2",
    )
    assert result.returncode == 0, result.stderr
    assert "test-instruction" in result.stdout


def test_fast_path_returns_static_parent_options(tmp_path: Path) -> None:
    """`otto test --<TAB>` must include parent-callback options like --cov.

    Regression guard: the old fast path attached empty placeholder sub-Typers
    that stripped these options from the completion surface.
    """
    _run_otto(["--help"], xdir=tmp_path)

    result = _run_otto(
        [],
        xdir=tmp_path,
        comp_words="otto test --",
        comp_cword="2",
    )
    assert result.returncode == 0, result.stderr
    flags = set(result.stdout.splitlines())
    # Click emits "plain,--flag\n"; match on substring to cover both shapes.
    blob = result.stdout
    # Runner options (--markers / --iterations / --threshold / ...) live on the
    # ``otto test`` parent callback alongside --cov / --cov-dir / --list-suites.
    for flag in (
        "--cov",
        "--cov-dir",
        "--list-suites",
        "--markers",
        "--iterations",
        "--duration",
        "--threshold",
        "--results",
    ):
        assert flag in blob, f"{flag!r} missing from: {flags!r}"


def test_fast_path_returns_rebuilt_suite_options(tmp_path: Path) -> None:
    """`otto test TestDevice --<TAB>` must include cached Options-derived flags."""
    _run_otto(["--help"], xdir=tmp_path)

    result = _run_otto(
        [],
        xdir=tmp_path,
        comp_words="otto test TestDevice --",
        comp_cword="3",
    )
    assert result.returncode == 0, result.stderr
    blob = result.stdout
    # --firmware comes from the TestDevice.Options dataclass and proves the
    # Annotated[...] reconstruction path survived serialization round-trip.
    assert "--firmware" in blob, f"--firmware missing from: {blob!r}"
    # Runner options now live on the parent callback, not each suite — they
    # must NOT appear in per-suite completion.
    for flag in ("--markers", "--iterations", "--threshold"):
        assert flag not in blob, f"{flag!r} unexpectedly in per-suite output: {blob!r}"


def test_fast_path_returns_host_ids_for_host_subcommand(tmp_path: Path) -> None:
    """`otto host <TAB>` must complete host IDs from the configured lab.json.

    The completer in ``otto.cli.host`` runs during completion before
    :func:`otto.bootstrap.bootstrap` registers repo init modules — this test
    proves host IDs reach the shell.
    """
    _run_otto(["--help"], xdir=tmp_path)

    result = _run_otto(
        [],
        xdir=tmp_path,
        comp_words="otto host ",
        comp_cword="2",
    )
    assert result.returncode == 0, result.stderr
    # tech1/lab.json in repo1 defines test1, test2, test3.
    blob = result.stdout
    for host_id in ("test1", "test2", "test3"):
        assert host_id in blob, f"{host_id!r} missing from: {blob!r}"


def test_cache_clear_removes_both_cache_files(tmp_path: Path) -> None:
    """`otto cache clear` (successor to the removed root flag) unlinks BOTH cache files."""
    _run_otto(["--help"], xdir=tmp_path)
    cache_file = _cache_file(tmp_path)
    assert cache_file.exists()

    # The remote-path sidecar is only written by a live remote listing; seed
    # it directly rather than driving that path for real.
    sidecar = cache_file.with_name(REMOTE_CACHE_FILENAME)
    sidecar.write_text("{}")

    result = _run_otto(["cache", "clear"], xdir=tmp_path)
    assert result.returncode == 0, result.stderr
    assert not cache_file.exists()
    assert not sidecar.exists(), "sidecar survived otto cache clear"


@pytest.mark.serial_timing
def test_ttl_expiry_invalidates_cache(tmp_path: Path) -> None:
    """Cache entries older than the TTL are treated as a miss."""
    import time as _time

    _run_otto(["--help"], xdir=tmp_path)
    data = _read_cache(tmp_path)
    # Push every section 25h into the past — past the 24h TTL.
    stale_ts = int(_time.time()) - 25 * 60 * 60
    for section in data["sections"].values():
        section["generated_at"] = stale_ts
    _cache_file(tmp_path).write_text(json.dumps(data))

    # Run completion; fast path must reject the stale sections and rewrite.
    result = _run_otto(
        [],
        xdir=tmp_path,
        comp_words="otto ",
        comp_cword="1",
    )
    assert result.returncode == 0, result.stderr

    refreshed = _read_cache(tmp_path)["sections"]
    now = int(_time.time())
    # At least one section must carry a fresh generated_at. Using "any"
    # rather than pinning both keeps the test robust against sibling tests
    # that race to bump tracked file mtimes.
    assert any(abs(s["generated_at"] - now) < 60 for s in refreshed.values()), (
        f"no refreshed section after TTL expiry: {refreshed!r}"
    )
