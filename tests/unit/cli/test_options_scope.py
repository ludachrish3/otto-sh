"""A project that does not take part in an invocation offers none of its registered flags.

:func:`~otto.cli.invoke.kept_projects` is the one answer to which projects take
part in what a command offers: the invocation's ``-l`` names and ``-I``/``-E``
switches (``otto.config.scope.participating`` at its command-line tier), never a
loaded lab, asked once per invocation. :func:`~otto.cli.invoke.offered_options`
passes it to ``otto.params.covering`` and keeps a body class only when its
project is kept, so a project out of scope has nothing imported and nothing
offered. The world is ``tests/_fixtures/scope_worlds.py``'s.
"""

import inspect
import sys
from typing import Annotated, Any

import pydantic
import pytest
import typer
from typer.testing import CliRunner

import otto
from otto.cli import invoke
from otto.cli.invoke import OWN_OPTIONS_ATTR, OwnOptions
from otto.cli.main import app
from otto.cli.registry import CLI_COMMANDS, register_cli_command
from otto.params import OptionsOrigin
from tests._fixtures import plugin_commands
from tests._fixtures.entry_runner import complete
from tests._fixtures.scope_worlds import B_FLAGS_MODULE, ENV, LibFlags

runner = CliRunner()


def _otto(argv: "list[str]", **env: "str | None") -> Any:
    return runner.invoke(app, argv, env={**ENV, **env}, prog_name="otto")


def _ok(argv: "list[str]", **env: "str | None") -> Any:
    result = _otto(argv, **env)
    assert result.exit_code == 0, (argv, result.output, result.exception)
    return result


def _no_such_option(argv: "list[str]") -> Any:
    result = _otto(argv)
    assert result.exit_code == 2, (argv, result.output, result.exception)
    assert "No such option" in result.output, result.output
    return result


def _calls(monkeypatch, name: str) -> "list[Any]":
    """Record each call of ``otto.cli.invoke.<name>``, which still answers."""
    calls: "list[Any]" = []
    function = getattr(invoke, name)

    def watched(*args: Any, **kwargs: Any) -> Any:
        calls.append(args)
        return function(*args, **kwargs)

    monkeypatch.setattr(invoke, name, watched)
    return calls


# --- out of scope: no flag ----------------------------------------------------------


def test_the_out_of_scope_projects_flag_is_no_such_option_and_absent_from_help(
    scope_world, otto_cli
):
    run = otto_cli(["-l", "alpha1", "flash", "erase", "--b-level", "1"])
    assert run.code == 2, run
    assert "No such option" in run.text
    assert "'b'" not in run.text
    assert "project" not in run.text
    assert "-I" not in run.text
    assert "--include-projects" not in run.text
    assert plugin_commands.RECEIVED == []

    shown = otto_cli(["-l", "alpha1", "flash", "erase", "--help"])
    assert shown.code == 0, shown
    assert "--a-level" in shown.stdout
    assert "--free" in shown.stdout
    assert "--b-level" not in shown.stdout
    assert "--lib-level" not in shown.stdout


@pytest.mark.parametrize(
    "argv",
    [["-l", "beta1"], ["-l", "alpha1", "-I", "b"], []],
    ids=["matching_lab", "inclusion", "no_lab"],
)
def test_a_matching_lab_an_inclusion_or_no_lab_brings_the_flag_back(argv, scope_world):
    seen = scope_world.reads(scope_world.b_flags)
    _ok([*argv, "flash", "erase", "--b-level", "1"])
    assert [r.command for r in plugin_commands.RECEIVED] == ["flash erase"]
    assert [read["BFlags"].b_level for read in seen] == [1]
    assert "--b-level" in _ok([*argv, "flash", "erase", "--help"]).output


def test_an_exclusion_wins_over_a_lab_whose_patterns_match(scope_world):
    _no_such_option(["-l", "alpha1", "-E", "a", "flash", "erase", "--a-level", "1"])
    _ok(["-l", "alpha1", "flash", "erase", "--a-level", "1"])
    assert [r.command for r in plugin_commands.RECEIVED] == ["flash erase"]


def test_a_name_in_both_switches_is_refused_before_the_flag_is_read(
    monkeypatch, scope_world, otto_cli
):
    asked = _calls(monkeypatch, "kept_projects")
    run = otto_cli(["-I", "b", "-E", "b", "flash", "erase", "--b-level", "1"])
    assert run.code == 2, run
    assert "appear in both --include-projects and --exclude-projects" in run.text
    assert "No such option" not in run.text
    assert asked == []
    assert plugin_commands.RECEIVED == []


def test_a_filtered_class_is_never_imported(scope_world):
    _ok(["-l", "alpha1", "flash", "erase"])
    _ok(["-l", "alpha1", "flash", "erase", "--help"])
    assert [r.command for r in plugin_commands.RECEIVED] == ["flash erase"]
    assert B_FLAGS_MODULE not in sys.modules

    _ok(["-l", "beta1", "flash", "erase"])
    assert B_FLAGS_MODULE in sys.modules  # the probe sees an import when one happens


# --- a failure of an out-of-scope project stays out ----------------------------------


def test_an_import_error_in_scope_stops_the_command_and_out_of_scope_does_not(
    scope_world, otto_cli
):
    scope_world.register("scope_world_nowhere:Missing", ["flash"], project="b")
    assert otto_cli(["-l", "alpha1", "flash", "erase"]).code == 0
    assert [r.command for r in plugin_commands.RECEIVED] == ["flash erase"]

    run = otto_cli(["-l", "beta1", "flash", "erase"])
    assert run.code == 1, run
    assert run.text.startswith("error: "), run.text
    assert run.text.count("error:") == 1, run.text
    assert "scope_world_nowhere:Missing" in run.text
    assert "Traceback" not in run.text
    assert [r.command for r in plugin_commands.RECEIVED] == ["flash erase"]


@otto.options
class ALevel:
    level: Annotated[int, typer.Option(help="a's shared level.")] = 0


@otto.options
class BLevel:
    level: Annotated[int, typer.Option(help="b's shared level.")] = 0


def test_two_projects_that_collide_stop_the_command_only_when_both_take_part(scope_world, otto_cli):
    scope_world.register(ALevel, ["flash"], project="a")
    scope_world.register(BLevel, ["flash"], project="b")

    run = otto_cli(["flash", "erase"])
    assert run.code == 1, run
    assert run.text.startswith("error: "), run.text
    assert "project 'a'" in run.text
    assert "project 'b'" in run.text
    assert plugin_commands.RECEIVED == []

    assert otto_cli(["-E", "b", "flash", "erase"]).code == 0
    assert [r.command for r in plugin_commands.RECEIVED] == ["flash erase"]

    from otto.bootstrap import bootstrap

    assert [p.name for p in bootstrap().projects] == ["a", "b", "lib"]
    for switches in ([], ["-E", "b"]):
        for argv in (["--help"], ["run", "--list-instructions"], ["link", "--help"]):
            ran = otto_cli([*switches, *argv])
            assert ran.code == 0, (switches, argv, ran)
        ran = otto_cli([*switches, "schema", "--help"])
        assert ran.code == 0, (switches, ran)


def test_a_hook_reads_the_out_of_scope_class_as_defaults_even_with_its_envvar_set(scope_world):
    seen = scope_world.reads(scope_world.b_flags)
    _ok(["-l", "alpha1", "flash", "erase"], B_LEVEL="7")
    assert [read["BFlags"].b_level for read in seen] == [0]

    _ok(["-l", "beta1", "flash", "erase"], B_LEVEL="7")
    assert [read["BFlags"].b_level for read in seen] == [0, 7]


# --- a library project ------------------------------------------------------------------


@pytest.mark.parametrize("argv", [["-E", "lib"], ["-l", "alpha1"]], ids=["excluded", "lab"])
def test_a_required_library_project_is_judged_like_any_other(argv, scope_world, otto_cli):
    _no_such_option([*argv, "flash", "erase", "--lib-level", "1"])
    seen = scope_world.reads(LibFlags)
    _ok([*argv, "flash", "erase", "--a-level", "1"], LIB_LEVEL="4")
    assert [read["LibFlags"].lib_level for read in seen] == [0]

    scope_world.fail_to_load(scope_world.lib)
    run = otto_cli([*argv, "flash", "erase"])
    assert run.code == 1, run
    assert "project 'lib' failed to load and is inactive for this run" in run.text
    assert "active project 'a' requires it" in run.text


# --- the selection is validated early, and never by a TAB ----------------------------


def test_an_unknown_include_name_is_reported_before_the_flag_and_by_help(scope_world, otto_cli):
    for argv in (
        ["-I", "acm", "flash", "erase", "--a-level", "1"],
        ["-I", "acm", "flash", "erase", "--help"],
    ):
        run = otto_cli(argv)
        assert run.code == 2, (argv, run)
        # Today's usage error, word for word, and nothing else: no help, no option error.
        assert (run.stdout + run.stderr).splitlines() == ["no project 'acm'"], run
    assert plugin_commands.RECEIVED == []


def test_a_completion_parse_validates_nothing_and_never_raises(monkeypatch, scope_world):
    asked = _calls(monkeypatch, "kept_projects")
    words = complete("otto -I acm flash erase --", ENV)
    assert {"--a-level", "--b-level", "--lib-level", "--free"} <= set(words)
    assert asked == []


def test_the_flags_follow_the_lab_names_not_the_loaded_lab(
    monkeypatch, scope_world, recording_repository
):
    reads_at_offer: "list[list[str]]" = []
    offer = invoke.offered_options

    def watched(ctx: Any, spec: Any) -> Any:
        reads_at_offer.append(list(recording_repository.reads))
        return offer(ctx, spec)

    monkeypatch.setattr(invoke, "offered_options", watched)

    assert "--b-level" not in _ok(["-l", "alpha1", "flash", "erase", "--help"]).output
    assert reads_at_offer
    assert recording_repository.reads == []

    # ``flash`` uses the lab from here on, so its preamble reads the lab source.
    CLI_COMMANDS.unregister(name="flash")
    register_cli_command("flash", plugin_commands.flash_app, lab_free=False, accepts_options=True)
    reads_at_offer.clear()
    refused = _otto(["-l", "alpha1", "flash", "erase", "--b-level", "1"])
    assert refused.exit_code == 2, refused.output
    assert "No such option" in refused.output
    # The failed parse alone asks whether the lab exists (§6 item 22): one load, after
    # every offer, and the lab is declared, so the parser's error stands.
    assert recording_repository.reads == ["build", "load_lab alpha1"]
    assert all(reads == [] for reads in reads_at_offer), reads_at_offer
    recording_repository.reads.clear()

    reads_at_offer.clear()
    parsed = _otto(["-l", "alpha1", "flash", "erase", "--a-level", "1"])
    assert "No such option" not in parsed.output, parsed.output
    # The declared lab loads, so the preamble goes on to prepare the options: that
    # last offer comes after the load, and every offer the parse made came before it.
    *parsing, preparing = reads_at_offer
    assert parsing
    assert all(reads == [] for reads in parsing), reads_at_offer
    assert preparing == ["build", "load_lab alpha1"]
    assert recording_repository.reads, "the preamble never read the lab source"


# --- one process, three invocations (§6 item 19) ------------------------------------


@otto.options
class ASecret:
    a_token: Annotated[pydantic.SecretStr, typer.Option(help="a's token.")] = pydantic.SecretStr("")


def test_three_invocations_in_one_process_each_see_their_own_flags(scope_world):
    scope_world.register(ASecret, ["flash"], project="a")
    in_scope = ["-n", "flash", "erase", "--a-level", "3", "--a-token", "s3cret"]

    for excluded in (False, True, False):
        switches = ["-E", "a"] if excluded else []
        shown = _ok([*switches, "flash", "erase", "--help"]).output
        if excluded:
            _no_such_option([*switches, *in_scope])
            _no_such_option([*switches, "flash", "erase", "--a-level", "3"])
            planned = _ok([*switches, "-n", "flash", "erase"]).output
            assert "--a-level" not in shown
            assert "AFlags" not in planned
            assert "ASecret" not in planned
            assert "<hidden>" not in planned
        else:
            planned = _ok([*switches, *in_scope]).output
            assert "--a-level" in shown
            [would_run] = [line for line in planned.splitlines() if "would run:" in line]
            assert "--a-level 3" in would_run
            assert "--a-token <hidden>" in would_run
            assert "AFlags: a_level=3" in planned, planned
            assert "ASecret: a_token=<hidden>" in planned, planned
            assert "s3cret" not in planned
        ran = _ok([*switches, "flash", "erase"])
        assert ran.exit_code == 0
    assert [r.command for r in plugin_commands.RECEIVED] == ["flash erase"] * 3


# --- what a command says is its own ----------------------------------------------------


@otto.options
class BodyB:
    body_level: Annotated[int, typer.Option(help="b's body level.")] = 0


@otto.options
class BBoard:
    b_board: Annotated[int, typer.Option(help="b's board level.")] = 0


def _rig(own: OwnOptions) -> typer.Typer:
    """A ``rig go`` command carrying *own*, with a ``rig stop`` beside it."""
    rig = typer.Typer(name="rig")

    def go(ctx: typer.Context) -> None:
        plugin_commands.CONTEXTS.append(ctx)
        plugin_commands.RECEIVED.append(plugin_commands.Received("rig go", {}, 0))

    go.__signature__ = inspect.Signature(  # ty: ignore[unresolved-attribute] — Typer reads it
        [
            inspect.Parameter(
                "ctx", inspect.Parameter.POSITIONAL_OR_KEYWORD, annotation=typer.Context
            )
        ]
    )
    setattr(go, OWN_OPTIONS_ATTR, own)
    rig.command("go")(go)

    @rig.command("stop")
    def stop() -> None:  # pragma: no cover — only listed
        """Stop."""

    return rig


@pytest.fixture
def rig(scope_world):
    """``rig_with(own, accepts_options=...)`` registers ``rig`` lab-free for the test."""

    def rig_with(own: OwnOptions, *, accepts_options: "bool | None" = None) -> None:
        register_cli_command("rig", _rig(own), lab_free=True, accepts_options=accepts_options)

    try:
        yield rig_with
    finally:
        CLI_COMMANDS.unregister(name="rig")


def test_a_body_whose_project_is_out_of_scope_has_no_flag_on_a_command_that_declares_bodies(rig):
    rig(OwnOptions(bodies=lambda: [OptionsOrigin(BodyB, "b")]))
    _no_such_option(["-l", "alpha1", "rig", "go", "--body-level", "1"])
    assert "--body-level" not in _ok(["-l", "alpha1", "rig", "go", "--help"]).output
    _ok(["-l", "beta1", "rig", "go", "--body-level", "1"])
    assert "--body-level" in _ok(["-l", "beta1", "rig", "go", "--help"]).output
    assert [r.command for r in plugin_commands.RECEIVED] == ["rig go"]


def test_a_leaf_that_keeps_every_project_ignores_the_selection(monkeypatch, scope_world, rig):
    rig(OwnOptions(every_project=True), accepts_options=True)
    scope_world.register(BBoard, ["rig"], project="b")
    asked = _calls(monkeypatch, "kept_projects")
    _ok(["-l", "alpha1", "rig", "go", "--b-board", "1"])
    assert "--b-board" in _ok(["-l", "alpha1", "rig", "go", "--help"]).output
    assert [r.command for r in plugin_commands.RECEIVED] == ["rig go"]
    assert asked == []
