"""Root help is served from the ``names`` section — cache-or-load, never degraded.

Every test here runs the REAL console entry (``otto._shim:main``) in a
subprocess against a generated repo with a private ``OTTO_HOME``. In-process
would prove nothing: ``entry()``'s decision is made from ``sys.argv`` before
Typer parses anything, the caches are keyed on a workspace home, and bootstrap
is a process-global singleton.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests._fixtures.generated_repo import generate_repo

_ENTRY = [sys.executable, "-c", "from otto._shim import main; main()"]

_PLUGIN_INIT = '''
"""Init module for the generated repo: registers third-party CLI commands."""

import typer

import otto

plug = typer.Typer(
    help="A plugin group registered directly, not through the @cli_command decorator."
)


@plug.command("alpha")
def alpha() -> None:
    """Alpha leaf."""


# TWO commands, so the app does not flatten to its single leaf: `plug` is a
# real GROUP, which is the cached-stub branch that rebuilds child metadata.
@plug.command("beta")
def beta() -> None:
    """Beta leaf."""


otto.register_cli_command("plug", plug)


# BOTH REGISTRATION SHAPES. `plug` above goes through a DIRECT
# `register_cli_command` call; this leaf goes through the `@cli_command`
# DECORATOR, whose registration call executes inside otto.cli.registry. That
# is the shape whose origin was once misattributed to otto itself (the
# decorator frame, not the plugin module), which made the cache collector
# classify the leaf as a BUILT-IN and drop it from the `commands` payload —
# so warm help silently lost every decorated plugin leaf while direct
# registrations survived. Costs the shared fixture nothing: the decorator
# lives in the already-imported otto.cli.registry, so bootstrap's module set
# is unchanged — measured by hand at implementation time (2026-09-02) by
# counting sys.modules after a cold bootstrap of this fixture's repo with
# and without the decorated leaf: 629 == 629. No test pins this; re-measure
# the same way before hanging anything heavier off the shared init.
@otto.cli_command(name="dec-leaf", help="Decorated plugin leaf.", lab_free=True)
async def dec_leaf() -> None:
    """Decorated plugin leaf."""


# THE THREE HELP-LESS SHAPES. A LAZY loader with no `help=` leaves the spec's
# help None (only a live Typer app has one to read), so the registry stub and
# the cache-backed stub must agree on the placeholder — and the cache-backed
# builder has three branches, one per shape:
#   helpless  -> no children, no options -> the synthesized-CommandSpec branch
#   muteleaf  -> cached options          -> build_stub_command
#   mutegroup -> cached children         -> build_stub_group
otto.register_cli_command("helpless", "genrepo_lazy:solo_app")
otto.register_cli_command("muteleaf", "genrepo_lazy:opt_app")
otto.register_cli_command("mutegroup", "genrepo_lazy:mute_app")
'''

_PLUGIN_LAZY = '''
"""The lazily-imported half of the generated repo's plugin."""

from typing import Annotated

import typer

solo_app = typer.Typer()


@solo_app.command("helpless")
def helpless(count: int = 1) -> None:
    pass


opt_app = typer.Typer()


@opt_app.command("muteleaf")
def muteleaf(
    flag: Annotated[bool, typer.Option("--flag", help="A flag.")] = False,
) -> None:
    pass


mute_app = typer.Typer()


@mute_app.command("one")
def one() -> None:
    """First child."""


@mute_app.command("two")
def two() -> None:
    """Second child."""
'''

_SUITE_FILE = '''
"""A TOP-LEVEL test file, which is the only place a suite can register from.

``Repo.iter_test_files`` reads the top level of each configured tests dir and
``import_test_files`` executes what it returns at bootstrap, so a ``Test*``
subclass here lands in the ``SUITES`` registry and therefore in the ``names``
section's ``suites`` payload.
"""

from otto.suite import OttoSuite


class TestGenrepoSuite(OttoSuite):
    """The one registered suite, so ``otto test <TAB>`` has a name to print."""

    async def test_noop(self) -> None:
        pass
'''

_BROKEN_INIT = '''
"""Init module that registers one command and then fails to load."""

import typer

import otto

before = typer.Typer(help="Registered before the failure.")


@before.command("ok")
def ok() -> None:
    """Fine."""


otto.register_cli_command("before-boom", before)

raise RuntimeError("genrepo init is broken on purpose")
'''


def _env(repo: Path, home: Path) -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith("OTTO_")}
    env["OTTO_SUT_DIRS"] = str(repo)
    env["OTTO_HOME"] = str(home)
    # Never write bytecode into the fixture tree: it would make the second run
    # differ from the first for a reason that has nothing to do with the cache.
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    # Pin the render width so cold and warm help are comparable byte for byte.
    env["COLUMNS"] = "100"
    return env


def _run(env: dict, *argv: str) -> subprocess.CompletedProcess:
    return subprocess.run([*_ENTRY, *argv], env=env, capture_output=True, text=True, check=False)


@pytest.fixture
def plugin_repo(tmp_path):
    """A generated repo whose init module registers third-party CLI commands."""
    repo = generate_repo(tmp_path, files=20, dirs=3)
    (repo / "pylib" / "genrepo_instructions.py").write_text(_PLUGIN_INIT)
    (repo / "pylib" / "genrepo_lazy.py").write_text(_PLUGIN_LAZY)
    return repo, _env(repo, tmp_path / "home")


@pytest.fixture
def suite_repo(tmp_path):
    """``plugin_repo`` plus ONE registered suite, for the ``otto test <TAB>`` param.

    A VARIANT rather than a suite added to ``plugin_repo`` itself: that fixture
    is shared with byte-identical-output assertions
    (``test_warm_help_is_byte_identical_to_cold_help``), with marker-based
    bootstrap detectors, and with the three sibling params of the completion
    test — and registering a suite imports ``otto.suite`` (hence pytest) inside
    every one of their bootstraps. This param needs a non-empty
    ``otto test <TAB>`` control and nothing else, so it gets its own tree.

    Without it the control was ``'\\n'`` and the corrupted-vs-control assert
    read ``'\\n' == '\\n'``: it could catch a name APPEARING and never a name
    being LOST, which is the direction that matters — a degraded serve that
    hands click an empty ``suites`` list exits 0, prints no traceback, and
    silently drops every suite name.
    """
    repo = generate_repo(tmp_path, files=20, dirs=3)
    (repo / "pylib" / "genrepo_instructions.py").write_text(_PLUGIN_INIT)
    (repo / "pylib" / "genrepo_lazy.py").write_text(_PLUGIN_LAZY)
    (repo / "tests" / "test_suite_probe.py").write_text(_SUITE_FILE)
    return repo, _env(repo, tmp_path / "home")


def _arm_marker(repo: Path) -> Path:
    """Make the repo's init module touch a marker file when it is imported.

    The marker is the bootstrap detector these tests use: it appears if and
    only if phase 2 ran. It sits at the SUT root, which keys no section — a
    file the digests can see would invalidate the very cache under test.
    """
    marker = repo / "init-ran"
    (repo / "pylib" / "genrepo_instructions.py").write_text(
        _PLUGIN_INIT + f"\nopen({str(marker)!r}, 'a').close()\n"
    )
    return marker


@pytest.fixture
def broken_repo(tmp_path):
    """A generated repo whose init module raises after registering one command."""
    repo = generate_repo(tmp_path, files=10, dirs=2)
    (repo / "pylib" / "genrepo_instructions.py").write_text(_BROKEN_INIT)
    return repo, _env(repo, tmp_path / "home")


def _cache_file(home: Path) -> Path:
    found = list(home.rglob("completion_cache.json"))
    assert found, f"no completion cache under {home}"
    return found[0]


def test_cold_cache_help_still_lists_third_party_commands(plugin_repo):
    """Cache-or-load: a cold cache must fall back to a full load, never degrade.

    The FIRST run has nothing to read, so the only way ``plug`` can appear on
    the screen is that ``entry()`` bootstrapped and the repo's init module
    actually ran. A fast path that answered from an empty cache would print a
    help screen missing every third-party command and still exit 0.
    """
    _repo, env = plugin_repo
    result = _run(env, "--help")
    assert result.returncode == 0, result.stderr
    assert "plug" in result.stdout
    assert "A plugin group registered directly" in result.stdout


def test_warm_help_is_byte_identical_to_cold_help(plugin_repo):
    """The cached screen is the SAME screen, not an approximation of it.

    Command names AND their one-line helps, including the placeholder a
    command with no declared help gets — which the two stub builders spell
    from one helper precisely so this can hold.
    """
    _repo, env = plugin_repo
    cold = _run(env, "--help")
    assert cold.returncode == 0, cold.stderr
    warm = _run(env, "--help")
    assert warm.returncode == 0, warm.stderr

    # Non-vacuity, and the two registration shapes BY NAME: the direct-call
    # group and the @cli_command-decorated leaf must both be on the cold
    # screen before byte-identity can prove anything about them. The leaf is
    # the shape that regressed — origin misattribution made the collector
    # treat it as a built-in, so warm help dropped it while the group
    # survived.
    assert "plug" in cold.stdout
    assert "dec-leaf" in cold.stdout
    assert warm.stdout == cold.stdout
    for name in ("helpless", "muteleaf", "mutegroup"):
        assert f"(run `otto {name} -h` for details)" in warm.stdout


def test_warm_help_runs_no_user_code(plugin_repo):
    """The point of the section: a warm root help never bootstraps.

    Proven by a side effect the init module leaves in the filesystem rather
    than by counting I/O — this is the correctness half; the import-budget
    harness owns the cost half.
    """
    repo, env = plugin_repo
    marker = _arm_marker(repo)
    _run(env, "--help")
    assert marker.is_file(), "the cold run did not import the init module at all"
    marker.unlink()

    _run(env, "--help")
    assert not marker.exists(), "warm root help still imported the repo's init module"


@pytest.mark.parametrize("argv", [(), ("--help",), ("-h",)])
def test_every_root_help_spelling_takes_the_fast_path(plugin_repo, argv):
    """Bare ``otto``, ``--help`` and ``-h`` all render the root screen."""
    repo, env = plugin_repo
    marker = _arm_marker(repo)
    _run(env, "--help")  # seed
    marker.unlink()

    result = _run(env, *argv)
    assert "plug" in result.stdout
    assert not marker.exists(), f"{argv} bootstrapped instead of reading the names section"


@pytest.mark.parametrize("argv", [("run", "--help"), ("--help", "extra"), ("host", "-h")])
def test_subcommand_help_still_takes_the_full_path(plugin_repo, argv):
    """Only the EXACT root-help argv family is fast — never a membership test.

    ``otto run --help`` has to resolve the real ``run`` group to list the
    instructions the repo registered, and a scan for a help token anywhere in
    argv would hand it a name list instead.
    """
    repo, env = plugin_repo
    marker = _arm_marker(repo)
    _run(env, "--help")  # seed the cache so the fast path is available at all
    marker.unlink()

    _run(env, *argv)
    assert marker.is_file(), f"{argv} took the root-help fast path"


def test_a_broken_init_module_writes_a_tainted_cache(broken_repo):
    """Bootstrap errors must not be cached away.

    A contained init failure means the collected names are partial. The
    broken file's stats are stable until someone edits it, so the digest
    would never move — an untainted write would serve that partial screen
    silently and forever. Both halves are asserted: the taint reaches disk,
    and the NEXT run consequently takes the full path (which is observable
    because the full path re-prints the framed warning).
    """
    _repo, env = broken_repo
    first = _run(env, "--help")
    assert first.returncode == 0, first.stderr
    assert "failed to load genrepo_instructions" in first.stderr

    data = json.loads(_cache_file(Path(env["OTTO_HOME"])).read_text())
    assert data["sections"]["names"]["tainted"] is True
    assert data["sections"]["tests"]["tainted"] is True

    second = _run(env, "--help")
    assert second.returncode == 0, second.stderr
    assert "failed to load genrepo_instructions" in second.stderr, (
        "the tainted names section was served — the warning went silent"
    )
    # And the screen stayed complete: the command registered before the raise
    # is still listed, because the full load ran again.
    assert "before-boom" in second.stdout


def _corrupt_names(env: dict, key: str, value) -> None:
    """Seed the cache, then replace one key of its ``names`` payload.

    The digest is over the repo's FILES, so editing the payload leaves the
    section perfectly valid — which is the point: the shape check is the only
    thing standing between a corrupt entry and click's help/completion
    pipeline.
    """
    assert _run(env, "--help").returncode == 0
    cache = _cache_file(Path(env["OTTO_HOME"]))
    data = json.loads(cache.read_text())
    data["sections"]["names"]["payload"][key] = value
    cache.write_text(json.dumps(data))


@pytest.mark.parametrize(
    "commands",
    [
        pytest.param({"plug": "not a list"}, id="not-a-list"),
        # A LIST of non-dicts passes `isinstance(commands, list)` and then
        # tracebacks on the first `.get("name")`. Newly reachable at root help,
        # which never read the cache before this task.
        pytest.param(["plug", "other"], id="list-of-non-dicts"),
    ],
)
def test_a_corrupt_names_payload_falls_back_to_the_full_load(plugin_repo, commands):
    """A malformed ``commands`` must cost a bootstrap, not a traceback.

    The root group iterates ``commands`` — and indexes into each entry — deep
    inside click's help pipeline, outside any containment ``entry()`` can
    offer, so the shape is checked one level DEEP at the point the snapshot is
    installed.

    ``commands`` ONLY: root help never touches ``suites`` or ``instructions``
    (they belong to ``otto test`` / ``otto run``), so parametrizing this test
    over them would add two cases that pass whatever the guard does. Their
    home is the completion test below.
    """
    _repo, env = plugin_repo
    _corrupt_names(env, "commands", commands)

    result = _run(env, "--help")
    assert result.returncode == 0, result.stderr
    assert "Traceback" not in result.stderr
    assert "plug" in result.stdout, "the full load should have re-registered the plugin"


def _complete(env: dict, comp_words: str, comp_cword: str) -> subprocess.CompletedProcess:
    """Run one completion invocation against *env*'s cache, as the shell would."""
    return subprocess.run(
        [str(Path(sys.executable).with_name("otto"))],
        env={
            **env,
            "_OTTO_COMPLETE": "complete_bash",
            "COMP_WORDS": comp_words,
            "COMP_CWORD": comp_cword,
        },
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.parametrize(
    ("key", "value", "comp_words", "comp_cword", "expected", "compare_to_control", "repo_fixture"),
    [
        pytest.param(
            "commands",
            {"plug": "x"},
            "otto ",
            "1",
            "plug",
            False,
            "plugin_repo",
            id="commands-not-a-list",
        ),
        pytest.param(
            "commands",
            ["plug", "other"],
            "otto ",
            "1",
            "plug",
            False,
            "plugin_repo",
            id="commands-list-of-non-dicts",
        ),
        # `suites` and `instructions` reach `_attach_cached_stubs`, which
        # iterates them RAW — a different consumer from `commands`, on a
        # different argv, and one root help never exercises. `read_cache`
        # rejected a non-list for both; nothing did between this task's first
        # commit and this fix. Both compare against a control run of the
        # SAME completion against the clean warm cache: "no crash" is not
        # the property that matters here, "same answer as an uncorrupted
        # cache" is. That comparison only HAS power while the control is
        # non-empty, which is why `suites` runs against `suite_repo` — see
        # that fixture for what the empty control failed to catch.
        pytest.param(
            "suites",
            7,
            "otto test ",
            "2",
            "TestGenrepoSuite",
            True,
            "suite_repo",
            id="suites-not-a-list",
        ),
        pytest.param(
            "instructions",
            "nope",
            "otto run ",
            "2",
            "install",
            True,
            "plugin_repo",
            id="instructions-not-a-list",
        ),
    ],
)
def test_a_corrupt_names_payload_never_tracebacks_into_the_shell(
    request, key, value, comp_words, comp_cword, expected, compare_to_control, repo_fixture
):
    """THE SIBLING SITE. Completion must fall through, never crash the shell.

    ``entry()``'s completion branch and its root-help branch read the same
    section for the same reasons; the first cut of this task shape-checked
    only one of them, and the unchecked one was the branch whose own comment
    reads "Completion must never traceback into the shell". A corrupt cache
    turned a silent fallback into a rendered rich traceback and an empty
    completion list, mid-TAB.

    Parametrized over EVERY key the fast path hands to a raw iterator, each
    on the argv that reaches it, against the repo named by *repo_fixture*.
    *expected* is the name the fallback bootstrap must then produce. For
    ``suites`` and ``instructions``, *compare_to_control* proves the stronger
    property directly: the corrupted run's stdout is BYTE-IDENTICAL to a clean
    run of the exact same completion, i.e. the fallback bootstrap produced the
    SAME answer a warm-but-uncorrupted cache would have, not merely "rc 0 and
    no traceback".

    EVERY control must be non-empty or that comparison is decorative — it can
    only catch a name appearing, never a name being lost, and losing names is
    the regression direction a degraded serve produces. ``suites`` therefore
    runs against ``suite_repo``, whose control lists ``TestGenrepoSuite``.
    """
    _repo, env = request.getfixturevalue(repo_fixture)
    control = None
    if compare_to_control:
        _run(env, "--help")  # warm the cache before it is corrupted
        control = _complete(env, comp_words, comp_cword)

    _corrupt_names(env, key, value)

    result = _complete(env, comp_words, comp_cword)
    assert result.returncode == 0, result.stderr
    assert "Traceback" not in result.stderr
    if expected is not None:
        assert expected in result.stdout, (
            f"the fallback bootstrap should have produced {expected!r}: {result.stdout!r}"
        )
    if control is not None:
        assert result.stdout == control.stdout, (
            "a corrupted cache produced different completion output than an "
            "uncorrupted one — the fallback bootstrap must be indistinguishable "
            f"to the user: corrupted={result.stdout!r} control={control.stdout!r}"
        )


def test_a_tainted_cache_is_not_rewritten_on_every_invocation(broken_repo):
    """A tainted entry is never served, so rewriting it is pure cost.

    And it is not a one-off: the entry the write replaces is byte-for-byte
    the entry that was just refused, so a workspace with a broken init module
    would rewrite the cache on EVERY invocation, forever — defeating the skip
    Task 5 shipped, on Task 5's own rationale (a write on a network
    filesystem needs a commit and invalidates client cache), at exactly the
    moment a user is TABbing repeatedly to work out what broke.
    """
    _repo, env = broken_repo
    _run(env, "--help")
    cache = _cache_file(Path(env["OTTO_HOME"]))
    first = cache.stat().st_mtime_ns

    second = _run(env, "--help")
    # The warning still prints — the taint keeps the FULL PATH, and only the
    # redundant write is skipped.
    assert "failed to load genrepo_instructions" in second.stderr
    assert cache.stat().st_mtime_ns == first, "a tainted entry was rewritten unchanged"


def test_editing_the_broken_init_still_rewrites_the_tainted_cache(broken_repo):
    """...and the skip must not defeat invalidation.

    The other direction of the pair, mirroring Task 5's. Touching the broken
    file moves the ``names`` digest (it is a resolved init path), so the
    stored entry is no longer current and the write has to land — otherwise
    fixing the file could never take effect.
    """
    repo, env = broken_repo
    _run(env, "--help")
    cache = _cache_file(Path(env["OTTO_HOME"]))
    first = cache.stat().st_mtime_ns

    init = repo / "pylib" / "genrepo_instructions.py"
    init.write_text(init.read_text() + "\n# edited, still broken\n")

    _run(env, "--help")
    assert cache.stat().st_mtime_ns != first, "a moved digest did not rewrite the tainted entry"


def test_completion_serves_names_despite_a_stale_tests_section(plugin_repo):
    """A TAB must not validate the corpus to answer ``otto <TAB>``.

    THE STALE CORPUS IS THE POINT. Before the split, the completion fast path
    read the merged view, so an edit anywhere under the corpus invalidated
    BOTH sections and every TAB fell through to a full bootstrap — paying for
    a test-name floor the invocation never consulted. Now ``entry()`` installs
    the ``names`` section alone, a nested test file keys only ``tests``, and
    the command list is still served from cache.

    Run through the installed console script rather than ``python -c``: click
    derives the completion env-var name from ``sys.argv[0]``, so its
    completion machinery only fires under the real ``otto`` name.
    """
    repo, env = plugin_repo
    marker = _arm_marker(repo)
    _run(env, "--help")  # seed
    assert marker.is_file()
    marker.unlink()

    # Move the TESTS digest only.
    nested = next(repo.rglob("sub*/test_*.py"))
    nested.write_text("def test_x():\n    pass\n\ndef test_added():\n    pass\n")

    result = subprocess.run(
        [str(Path(sys.executable).with_name("otto"))],
        env={**env, "_OTTO_COMPLETE": "complete_bash", "COMP_WORDS": "otto ", "COMP_CWORD": "1"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert not marker.exists(), "completion bootstrapped over a stale tests section"
    assert "plug" in result.stdout, f"third-party command missing: {result.stdout}"
    # The decorated leaf completes too — completion reads the same `commands`
    # payload as root help, so the origin-misattribution drop hit both.
    assert "dec-leaf" in result.stdout, f"decorated leaf missing: {result.stdout}"
