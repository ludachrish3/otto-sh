"""Root help is served from the ``names`` section — cache-or-load, never degraded.

Every test here runs the REAL console entry (``otto._shim:main``) in a
subprocess against a generated project with a private ``OTTO_HOME``. In-process
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

from tests._fixtures.generated_project import generate_project
from tests._fixtures.warm_tab import (
    TYPER_ENTRY,
    cli_env,
    cold_tab,
    run_otto,
    shim_tab,
    typer_tab,
    without_installers,
)

_PLUGIN_INIT = '''
"""Init module for the generated project: registers third-party CLI commands."""

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
# counting sys.modules after a cold bootstrap of this fixture's project with
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
"""The lazily-imported half of the generated project's plugin."""

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


@pytest.fixture
def plugin_project(tmp_path):
    """A generated project whose init module registers third-party CLI commands."""
    project = generate_project(tmp_path, files=20, dirs=3)
    (project / "pylib" / "genproject_instructions.py").write_text(_PLUGIN_INIT)
    (project / "pylib" / "genrepo_lazy.py").write_text(_PLUGIN_LAZY)
    return project, cli_env(project, tmp_path / "home")


def _arm_marker(project: Path) -> Path:
    """Make the project's init module touch a marker file when it is imported.

    The marker is the bootstrap detector these tests use: it appears if and
    only if phase 2 ran. It sits at the project root, which keys no section — a
    file the digests can see would invalidate the very cache under test.
    """
    marker = project / "init-ran"
    (project / "pylib" / "genproject_instructions.py").write_text(
        _PLUGIN_INIT + f"\nopen({str(marker)!r}, 'a').close()\n"
    )
    return marker


@pytest.fixture
def broken_project(tmp_path):
    """A generated project whose init module raises after registering one command."""
    project = generate_project(tmp_path, files=10, dirs=2)
    (project / "pylib" / "genproject_instructions.py").write_text(_BROKEN_INIT)
    return project, cli_env(project, tmp_path / "home")


def _cache_file(home: Path) -> Path:
    found = list(home.rglob("completion_cache.json"))
    assert found, f"no completion cache under {home}"
    return found[0]


def test_cold_cache_help_still_lists_third_party_commands(plugin_project):
    """Cache-or-load: a cold cache must fall back to a full load, never degrade.

    The FIRST run has nothing to read, so the only way ``plug`` can appear on
    the screen is that ``entry()`` bootstrapped and the project's init module
    actually ran. A fast path that answered from an empty cache would print a
    help screen missing every third-party command and still exit 0.
    """
    _project, env = plugin_project
    result = run_otto(env, "--help")
    assert result.returncode == 0, result.stderr
    assert "plug" in result.stdout
    assert "A plugin group registered directly" in result.stdout


def test_warm_help_is_byte_identical_to_cold_help(plugin_project):
    """The cached screen is the SAME screen, not an approximation of it.

    Command names AND their one-line helps, including the placeholder a
    command with no declared help gets — which the two stub builders spell
    from one helper precisely so this can hold.
    """
    _project, env = plugin_project
    cold = run_otto(env, "--help")
    assert cold.returncode == 0, cold.stderr
    warm = run_otto(env, "--help")
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


def test_warm_help_runs_no_user_code(plugin_project):
    """The point of the section: a warm root help never bootstraps.

    Proven by a side effect the init module leaves in the filesystem rather
    than by counting I/O — this is the correctness half; the import-budget
    harness owns the cost half.
    """
    project, env = plugin_project
    marker = _arm_marker(project)
    run_otto(env, "--help")
    assert marker.is_file(), "the cold run did not import the init module at all"
    marker.unlink()

    run_otto(env, "--help")
    assert not marker.exists(), "warm root help still imported the project's init module"


@pytest.mark.parametrize("argv", [(), ("--help",), ("-h",)])
def test_every_root_help_spelling_takes_the_fast_path(plugin_project, argv):
    """Bare ``otto``, ``--help`` and ``-h`` all render the root screen."""
    project, env = plugin_project
    marker = _arm_marker(project)
    run_otto(env, "--help")  # seed
    marker.unlink()

    result = run_otto(env, *argv)
    assert "plug" in result.stdout
    assert not marker.exists(), f"{argv} bootstrapped instead of reading the names section"


@pytest.mark.parametrize("argv", [("run", "--help"), ("--help", "extra"), ("host", "-h")])
def test_subcommand_help_still_takes_the_full_path(plugin_project, argv):
    """Only the EXACT root-help argv family is fast — never a membership test.

    ``otto run --help`` has to resolve the real ``run`` group to list the
    instructions the project registered, and a scan for a help token anywhere in
    argv would hand it a name list instead.
    """
    project, env = plugin_project
    marker = _arm_marker(project)
    run_otto(env, "--help")  # seed the cache so the fast path is available at all
    marker.unlink()

    run_otto(env, *argv)
    assert marker.is_file(), f"{argv} took the root-help fast path"


def test_a_broken_init_module_writes_a_tainted_cache(broken_project):
    """Bootstrap errors must not be cached away.

    A contained init failure means the collected names are partial. The
    broken file's stats are stable until someone edits it, so the digest
    would never move — an untainted write would serve that partial screen
    silently and forever. Both halves are asserted: the taint reaches disk,
    and the NEXT run consequently takes the full path (which is observable
    because the full path re-prints the framed warning).
    """
    _project, env = broken_project
    first = run_otto(env, "--help")
    assert first.returncode == 0, first.stderr
    assert "failed to load genproject_instructions" in first.stderr

    data = json.loads(_cache_file(Path(env["OTTO_HOME"])).read_text())
    assert data["sections"]["names"]["tainted"] is True
    assert data["sections"]["shim"]["tainted"] is True

    second = run_otto(env, "--help")
    assert second.returncode == 0, second.stderr
    assert "failed to load genproject_instructions" in second.stderr, (
        "the tainted names section was served — the warning went silent"
    )
    # And the screen stayed complete: the command registered before the raise
    # is still listed, because the full load ran again.
    assert "before-boom" in second.stdout


def _corrupt_names(env: dict, key: str, value) -> None:
    """Seed the cache, then replace one key of its ``names`` payload.

    The digest is over the project's FILES, so editing the payload leaves the
    section perfectly valid — which is the point: the shape check is the only
    thing standing between a corrupt entry and click's help/completion
    pipeline.
    """
    assert run_otto(env, "--help").returncode == 0
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
def test_a_corrupt_names_payload_falls_back_to_the_full_load(plugin_project, commands):
    """A malformed ``commands`` must cost a bootstrap, not a traceback.

    The root group iterates ``commands`` — and indexes into each entry — deep
    inside click's help pipeline, outside any containment ``entry()`` can
    offer, so the shape is checked one level DEEP at the point the snapshot is
    installed.

    ``commands`` ONLY: root help never touches ``instructions`` (it belongs
    to ``otto run``), so parametrizing this test over it would add a case
    that passes whatever the guard does. Its home is the completion test
    below.
    """
    _project, env = plugin_project
    _corrupt_names(env, "commands", commands)

    result = run_otto(env, "--help")
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
    (
        "key",
        "value",
        "comp_words",
        "comp_cword",
        "expected",
        "compare_to_control",
        "project_fixture",
    ),
    [
        pytest.param(
            "commands",
            {"plug": "x"},
            "otto ",
            "1",
            "plug",
            False,
            "plugin_project",
            id="commands-not-a-list",
        ),
        pytest.param(
            "commands",
            ["plug", "other"],
            "otto ",
            "1",
            "plug",
            False,
            "plugin_project",
            id="commands-list-of-non-dicts",
        ),
        # `instructions` reaches `_attach_cached_stubs`, which iterates it
        # RAW — a different consumer from `commands`, on a different argv,
        # and one root help never exercises. It compares against a control
        # run of the SAME completion against the clean warm cache: "no crash"
        # is not the property that matters here, "same answer as an
        # uncorrupted cache" is. That comparison only HAS power while the
        # control is non-empty — it lists `install`.
        pytest.param(
            "instructions",
            "nope",
            "otto run ",
            "2",
            "install",
            True,
            "plugin_project",
            id="instructions-not-a-list",
        ),
    ],
)
def test_a_corrupt_names_payload_never_tracebacks_into_the_shell(
    request, key, value, comp_words, comp_cword, expected, compare_to_control, project_fixture
):
    """THE SIBLING SITE. Completion must fall through, never crash the shell.

    ``entry()``'s completion branch and its root-help branch read the same
    section for the same reasons; the first cut of this task shape-checked
    only one of them, and the unchecked one was the branch whose own comment
    reads "Completion must never traceback into the shell". A corrupt cache
    turned a silent fallback into a rendered rich traceback and an empty
    completion list, mid-TAB.

    Parametrized over EVERY key the fast path hands to a raw iterator, each
    on the argv that reaches it, against the project named by *project_fixture*.
    *expected* is the name the fallback bootstrap must then produce. For
    ``instructions``, *compare_to_control* proves the stronger
    property directly: the corrupted run's stdout is BYTE-IDENTICAL to a clean
    run of the exact same completion, i.e. the fallback bootstrap produced the
    SAME answer a warm-but-uncorrupted cache would have, not merely "rc 0 and
    no traceback".

    EVERY control must be non-empty or that comparison is decorative — it can
    only catch a name appearing, never a name being lost, and losing names is
    the regression direction a degraded serve produces.
    """
    _project, env = request.getfixturevalue(project_fixture)
    control = None
    if compare_to_control:
        run_otto(env, "--help")  # warm the cache before it is corrupted
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


def test_a_tainted_cache_is_not_rewritten_on_every_invocation(broken_project):
    """A tainted entry is never served, so rewriting it is pure cost.

    And it is not a one-off: the entry the write replaces is byte-for-byte
    the entry that was just refused, so a workspace with a broken init module
    would rewrite the cache on EVERY invocation, forever — defeating the skip
    Task 5 shipped, on Task 5's own rationale (a write on a network
    filesystem needs a commit and invalidates client cache), at exactly the
    moment a user is TABbing repeatedly to work out what broke.
    """
    _project, env = broken_project
    run_otto(env, "--help")
    cache = _cache_file(Path(env["OTTO_HOME"]))
    first = cache.stat().st_mtime_ns

    second = run_otto(env, "--help")
    # The warning still prints — the taint keeps the FULL PATH, and only the
    # redundant write is skipped.
    assert "failed to load genproject_instructions" in second.stderr
    assert cache.stat().st_mtime_ns == first, "a tainted entry was rewritten unchanged"


def test_editing_the_broken_init_still_rewrites_the_tainted_cache(broken_project):
    """...and the skip must not defeat invalidation.

    The other direction of the pair, mirroring Task 5's. Touching the broken
    file moves the ``names`` digest (it is a resolved init path), so the
    stored entry is no longer current and the write has to land — otherwise
    fixing the file could never take effect.
    """
    project, env = broken_project
    run_otto(env, "--help")
    cache = _cache_file(Path(env["OTTO_HOME"]))
    first = cache.stat().st_mtime_ns

    init = project / "pylib" / "genproject_instructions.py"
    init.write_text(init.read_text() + "\n# edited, still broken\n")

    run_otto(env, "--help")
    assert cache.stat().st_mtime_ns != first, "a moved digest did not rewrite the tainted entry"


def test_completion_serves_names_despite_a_stale_tests_section(plugin_project):
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
    project, env = plugin_project
    marker = _arm_marker(project)
    run_otto(env, "--help")  # seed
    assert marker.is_file()
    marker.unlink()

    # Move the TESTS digest only.
    nested = next(project.rglob("sub*/test_*.py"))
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


_TEST_VERB_INIT = """
import otto


@otto.options(verbs=["test"])
class FirmwareOpts:
    firmware: str = "latest"
"""


def test_the_typer_fast_path_completes_the_test_verbs_flags(tmp_path):
    """`otto test --fi<TAB>` on a warm cache offers the verb's flag without a bootstrap.

    Driven through ``otto.cli.main.entry()`` itself, not the shim: this is the
    Typer fast path the shim hands over to. The ``names`` section carries the
    test verb's serialized options, as it carries every instruction's for
    ``otto run``; without them the fast path built ``otto test`` with the run
    flags alone.
    """
    project = generate_project(tmp_path, files=4, dirs=1)
    marker = project / "init-ran"
    (project / "pylib" / "genproject_instructions.py").write_text(
        _TEST_VERB_INIT + f"\nopen({str(marker)!r}, 'a').close()\n"
    )
    env = cli_env(project, tmp_path / "home")
    run_otto(env, "--help")  # seed
    assert marker.is_file()
    marker.unlink()

    result = subprocess.run(
        [sys.executable, "-c", TYPER_ENTRY],
        env={
            **env,
            "_OTTO_COMPLETE": "complete_bash",
            "COMP_WORDS": "otto test --fi",
            "COMP_CWORD": "2",
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert not marker.exists(), "the TAB bootstrapped instead of reading the cache"
    assert "--firmware" in result.stdout.split(), result.stdout + result.stderr


# ── the flags of options classes, warm ─────────────────────────────────────────

_COVERING_INIT = '''
from typing import Annotated

import typer

import otto


@otto.options
class Debugging:
    debug_level: Annotated[int, typer.Option(help="How loud.")] = 0


otto.register_options(Debugging, verbs=["cov", "plug"])

plug = typer.Typer(help="A plugin group.")
nest = typer.Typer(help="A nested group.")
plug.add_typer(nest, name="nest")


@nest.command("leaf")
def leaf() -> None:
    """A nested leaf."""


@nest.command("other")
def other() -> None:
    """A second leaf, so the nested group stays a group."""


otto.register_cli_command("plug", plug)
'''

_BROKEN_COVERING = {
    "two_classes_that_collide": """

@otto.options
class Clash:
    louder: Annotated[int, typer.Option("--debug-level")] = 0
    clash_only: Annotated[int, typer.Option()] = 0


otto.register_options(Clash, verbs=["plug"])
""",
    "a_class_that_cannot_be_imported": """
otto.register_options("genproject_gone:Missing", verbs=["plug"])
""",
}
"""What a project adds to :data:`_COVERING_INIT` so the live ``otto plug nest leaf`` stops."""

_SHARED = """
from typing import Annotated

import typer

import otto


@otto.options
class Shared:
    reach: Annotated[int, typer.Option(help="How far.")] = 0
"""


def _body(project: str, name: str) -> str:
    """An init module that declares, as *project*, the body of *name* over ``Shared``."""
    return f'''
from otto.instructions import instruction
from otto.project import ProjectActions, register_project_actions


@register_project_actions
class {project.title()}Actions(ProjectActions):
    @instruction(options=Shared, help="{name.title()}.")
    async def {name}(self, opts: Shared) -> None:
        """{name.title()} the bed."""
'''


def _armed(project: Path, init: str) -> Path:
    """Write *init* as *project*'s init module, touching a marker when it is imported."""
    marker = project / "init-ran"
    (project / "pylib" / "genproject_instructions.py").write_text(
        init + f"\nopen({str(marker)!r}, 'a').close()\n"
    )
    return marker


def test_the_typer_fast_path_completes_a_covering_flag_on_cov_and_on_a_plugin_command(tmp_path):
    """A warm TAB offers a registered class's flag on a built-in and on a plugin command.

    Driven through ``otto.cli.main.entry()``, the path the shim hands over
    to: the cache records the class's rows and the uses that cover each
    command, and the TAB rebuilds them without a bootstrap (the init marker
    is not recreated).
    """
    project = generate_project(tmp_path, files=4, dirs=1)
    marker = _armed(project, _COVERING_INIT)
    env = cli_env(project, tmp_path / "home")
    run_otto(env, "--help")  # seed
    assert marker.is_file()
    marker.unlink()

    assert "--debug-level" in typer_tab(env, "otto cov get --de")
    assert "--debug-level" in typer_tab(env, "otto plug nest leaf --de")
    assert not marker.exists(), "the TAB bootstrapped instead of reading the cache"


@pytest.mark.parametrize("broken", list(_BROKEN_COVERING))
def test_a_warm_tab_and_a_cold_tab_agree_where_the_live_command_would_stop(tmp_path, broken):
    """Two classes that collide, or one that cannot be imported, stop the live command only.

    The command's own parse and ``--help`` refuse; a TAB, warm or cold,
    offers the union of what can be described, and both offer the same.
    """
    project = generate_project(tmp_path, files=4, dirs=1)
    _armed(project, _COVERING_INIT + _BROKEN_COVERING[broken])
    env = cli_env(project, tmp_path / "home")
    assert run_otto(env, "plug", "nest", "leaf", "--help").returncode != 0

    words = "otto plug nest leaf --"
    cold = cold_tab(env, tmp_path / "cold-home", words)
    run_otto(env, "--help")  # seed
    warm = typer_tab(env, words)
    assert warm == cold
    assert warm.count("--debug-level") == 1, warm


def _one_class_on_two_bodies(tmp_path: Path) -> "tuple[dict, list[str]]":
    alpha = generate_project(tmp_path / "a", files=2, dirs=1, name="alpha")
    beta = generate_project(tmp_path / "b", files=2, dirs=1, name="beta")
    (alpha / "pylib" / "alpha_shared.py").write_text(_SHARED)
    (alpha / "pylib" / "alpha_instructions.py").write_text(
        "from alpha_shared import Shared\n" + _body("alpha", "provision")
    )
    (beta / "pylib" / "beta_instructions.py").write_text(
        "from alpha_shared import Shared\n" + _body("beta", "deploy")
    )
    env = {**cli_env(alpha, tmp_path / "home"), "OTTOPATH": f"{alpha}{os.pathsep}{beta}"}
    return env, ["otto run provision --", "otto run deploy --"]


def _a_body_class_also_registered_for_a_plugin_command(tmp_path: Path) -> "tuple[dict, list[str]]":
    project = generate_project(tmp_path, files=2, dirs=1)
    plugin = _COVERING_INIT.replace("Debugging, verbs", "Shared, verbs").replace('"cov", ', "")
    (project / "pylib" / "genproject_instructions.py").write_text(
        _SHARED + _body("genproject", "provision") + plugin
    )
    return cli_env(project, tmp_path / "home"), ["otto run provision --", "otto plug nest leaf --"]


_REUSE_WORLDS = {
    "one_class_on_two_instruction_bodies_of_different_projects": _one_class_on_two_bodies,
    "a_body_class_also_registered_for_a_plugin_command": (
        _a_body_class_also_registered_for_a_plugin_command
    ),
}


@pytest.mark.parametrize("reuse", list(_REUSE_WORLDS))
def test_a_reused_class_is_offered_warm_on_every_command_that_uses_it(tmp_path, reuse):
    """Each command that uses one class offers its flags, warm through the shim and the fast path.

    Each answer equals the cold TAB's, Typer's completion installers aside
    (see the note in the body). A ``run`` instruction prepares its own
    options, so its flags are its node's own; a plugin command takes them from
    the cache's recorded candidates.
    """
    env, lines = _REUSE_WORLDS[reuse](tmp_path)
    cold = {words: cold_tab(env, tmp_path / "cold-home", words) for words in lines}
    run_otto(env, "--help")  # seed
    for words in lines:
        warm = typer_tab(env, words)
        assert "--reach" in warm, (words, warm)
        # On a project's new `run` instruction the three answers disagree on
        # Typer's own --install-completion and --show-completion: a
        # difference that exists without options classes, held apart here
        # until it is fixed. Refs #1383
        shim = shim_tab(env, words)
        assert without_installers(shim) == without_installers(cold[words]), words
        assert without_installers(warm) == without_installers(cold[words]), words


_REPEATED_SWITCH = "import dataclasses\n" + _COVERING_INIT.replace(
    'debug_level: Annotated[int, typer.Option(help="How loud.")] = 0',
    "feature: Annotated[list[bool], typer.Option('--feature/--no-feature')] = "
    "dataclasses.field(default_factory=list)",
)
""":data:`_COVERING_INIT` whose covering class holds one repeated switch instead."""


def test_a_repeated_switch_is_offered_again_after_it_is_given_on_every_path(tmp_path):
    """A ``list[bool]`` switch stays repeatable: live, the shim and the fast path offer it again.

    Click offers an option already given only when it repeats, so a stub of
    the switch that did not repeat would hide both spellings after the first.
    """
    project = generate_project(tmp_path, files=4, dirs=1)
    assert _REPEATED_SWITCH != _COVERING_INIT
    _armed(project, _REPEATED_SWITCH)
    env = cli_env(project, tmp_path / "home")
    words = "otto plug nest leaf --feature --"
    cold = cold_tab(env, tmp_path / "cold-home", words)
    assert {"--feature", "--no-feature"} <= set(cold), cold
    run_otto(env, "--help")  # seed
    assert without_installers(typer_tab(env, words)) == without_installers(cold)
    assert without_installers(shim_tab(env, words)) == without_installers(cold)


KINDS = {
    "none": "--plain",
    "echo": "--file",
    "static": "--mode",
    "payload": "--source",
    "tests": "--pick-test",
    "markers": "--pick-marker",
    "observed": "--pick-image",
    "live": "--undeclared",
}
"""One flag of the shim fixture's plugin class per completion source kind.

``blink_all``'s own class declares each as ``--run-<name>``, with the same type
and completer. The observed kind's completer is the fleet image completer: a
container completer falls back to the default parent, which a cold TAB outside
``otto docker`` cannot read."""


@pytest.fixture(scope="module")
def shim_world(tmp_path_factory) -> "tuple[dict, Path]":
    """The shim fixture project with a written cache, and its init marker unarmed."""
    from tests._fixtures.shim_project import make_shim_project

    root = tmp_path_factory.mktemp("shim-world")
    project = make_shim_project(root)
    init = project / "pylib" / "shimsut_init" / "__init__.py"
    marker = project / "init-ran"
    init.write_text(init.read_text() + f"\nopen({str(marker)!r}, 'a').close()\n")
    env = cli_env(project, root / "home")
    assert run_otto(env, "--help").returncode == 0  # seed
    assert marker.is_file()
    marker.unlink()
    return env, marker


@pytest.mark.parametrize(("kind", "flag"), KINDS.items())
def test_an_attached_flags_value_gets_what_a_run_options_value_gets_today(shim_world, kind, flag):
    """On the fast path a candidate flag's value is completed as a ``run`` option's of its kind.

    A differential, not a fixed expectation: ``otto run blink-all
    --run-<name>`` is a cached ``run`` option of the same type and completer.
    """
    env, marker = shim_world
    attached = typer_tab(env, f"otto plug nest leaf {flag} ")
    own = typer_tab(env, f"otto run blink-all --run-{flag[2:]} ")
    assert attached == own, kind
    assert not marker.exists(), "the TAB bootstrapped instead of reading the cache"


@pytest.mark.parametrize(("kind", "flag"), KINDS.items())
def test_the_attached_flag_is_offered_whatever_its_source(shim_world, kind, flag):
    """The flag is offered, and it takes its value: the word after it is that value.

    ``--debug`` after the flag is the flag's value, so it was never given and
    is offered again; the flag itself was.
    """
    env, marker = shim_world
    assert flag in typer_tab(env, "otto plug nest leaf --"), kind
    after = typer_tab(env, f"otto plug nest leaf {flag} --debug 3 --")
    assert "--debug" in after, (kind, after)
    assert flag not in after, (kind, after)
    assert not marker.exists(), "the TAB bootstrapped instead of reading the cache"


@pytest.mark.parametrize(
    ("line", "debug_given"),
    [
        ("otto plug nest leaf --size 1 ", None),
        ("otto plug nest leaf --size 1 2 ", None),
        ("otto plug nest leaf --size 1 --debug 3 --", False),  # --debug is the second value
        ("otto plug nest leaf --size 1 2 --debug 3 --", True),
    ],
)
def test_the_handed_over_tuple_line_completes_warm_as_it_does_cold(
    shim_world, tmp_path, line, debug_given
):
    """The shim hands a two-value option over; the fast path then answers as a cold TAB does.

    The option takes two words: after one it still wants one, after two the
    line is the command's again.
    """
    env, marker = shim_world
    warm = typer_tab(env, line)
    cold = cold_tab(env, tmp_path / "cold-home", line)
    marker.unlink(missing_ok=True)  # the cold TAB's full load imported the init module
    assert warm == cold
    if debug_given is not None:
        assert ("--debug" not in warm) is debug_given, warm
