"""Flags attached to a command through its ``get_params`` behave as the command's own.

:func:`~otto.cli.invoke.attach_params` wraps every leaf's ``get_params``
(:func:`~otto.cli.invoke.wrap_leaf_callbacks` calls it): the command's own
parameters, then the invocation's attached ones
(:func:`~otto.cli.invoke.attached_params`), then the help option. Click's
parse, both help renderers and shell completion read that one list. What was
attached is kept in the context's ``ctx.meta``, never on the command, and
:func:`~otto.cli.invoke.take_attached` takes the attached values out of
``ctx.params`` before the command's function is called.

:func:`~otto.config._typer.carrier` turns ``inspect.Parameter`` s into Typer's
own parameters and Typer's value conversion. Nothing is attached yet
(``attached_params`` answers ``()``), so these tests replace it with a fixed
carrier.
"""

import dataclasses
import enum
from pathlib import Path
from typing import Annotated, Any

import pytest
import typer
from pydantic import Field
from typer.testing import CliRunner

import otto
from otto.cli import invoke
from otto.cli.invoke import (
    ACCEPTS_OPTIONS_ATTR,
    OWN_OPTIONS_ATTR,
    command_path,
    leaf_accepts_options,
    own_params,
    take_attached,
    wrap_leaf_callbacks,
)
from otto.cli.main import PENDING_SUBCMD_ARGS_KEY, app
from otto.cli.registry import CommandSpec, resolve_spec_command
from otto.config._typer import Carrier, carrier, given_names
from otto.errors import OttoError
from otto.params import options_params
from tests._fixtures import plugin_commands
from tests._fixtures.bootstrap_seam import fake_bootstrap_result, install_composed
from tests._fixtures.fake_project import fake_project
from tests._fixtures.plugin_commands import Acme
from tests._fixtures.typer_internals import runner_invokes

runner = CliRunner()

ENV = {"OTTO_LAB": "", "ACME_DEBUG": None, "COLUMNS": "200"}


class Mode(enum.Enum):
    fast = "fast"
    slow = "slow"


SECRET = "s3cret-token"


@otto.options
class Shape:
    out: Annotated[Path, typer.Option(help="Where the image goes.")] = Path("img")
    mode: Annotated[Mode, typer.Option(help="How fast to go.")] = Mode.fast
    tags: Annotated[list[str], typer.Option(help="A tag; repeat for more.")] = Field(
        default_factory=list
    )
    size: Annotated[tuple[int, int], typer.Option(help="Width and height.")] = (0, 0)
    loud: Annotated[bool, typer.Option(help="Say more.")] = False
    token: Annotated[str, typer.Option(help="The access token.")] = dataclasses.field(
        default=SECRET, repr=False
    )


FIXED: Carrier = carrier([*options_params(Shape), *options_params(Acme)])
NAMES = ["out", "mode", "tags", "size", "loud", "token", "debug_level"]

CONTROL_CALLS: "list[dict[str, Any]]" = []
control = typer.Typer()


@control.command()
def _control(
    out: Annotated[Path, typer.Option()] = Path("img"),
    mode: Annotated[Mode, typer.Option()] = Mode.fast,
    tags: Annotated[list[str], typer.Option()] = [],  # noqa: B006 — Typer's own spelling
    size: Annotated[tuple[int, int], typer.Option()] = (0, 0),
    loud: Annotated[bool, typer.Option()] = False,
    token: Annotated[str, typer.Option(show_default=False)] = SECRET,
    debug_level: Annotated[int, typer.Option(envvar="ACME_DEBUG")] = 0,
) -> None:
    CONTROL_CALLS.append(dict(locals()))


WORDS = ["--out", "x", "--mode", "slow", "--tags", "a", "--tags", "b", "--size", "1", "2", "--loud"]


@pytest.fixture
def plugins(monkeypatch):
    """The fixture plugin commands registered, with one project discovered."""
    install_composed(monkeypatch, fake_bootstrap_result([fake_project("acme")]))
    with plugin_commands.registered():
        yield plugin_commands


@pytest.fixture
def attached(monkeypatch, plugins):
    monkeypatch.setattr(invoke, "attached_params", lambda ctx, spec: FIXED.params)
    return plugins


def _otto(argv: "list[str]", **env: "str | None") -> Any:
    return runner.invoke(app, argv, env={**ENV, **env})


def _otto_on(root: Any, argv: "list[str]") -> Any:
    """``otto <argv>`` run by the one root command object *root*, as a long-lived CLI would."""
    with runner_invokes(root):
        return runner.invoke(app, argv, env=ENV)


def _ok(argv: "list[str]", **env: "str | None") -> Any:
    result = _otto(argv, **env)
    assert result.exit_code == 0, (result.output, result.exception)
    return result


def _context(argv: "list[str]", **env: "str | None") -> Any:
    """The leaf's context after ``otto <argv>`` ran to completion."""
    _ok(argv, **env)
    return plugin_commands.CONTEXTS[-1]


def _flags(params: Any) -> "list[str]":
    return [flag for param in params for flag in param.opts]


# --- help ------------------------------------------------------------------------


def test_help_lists_an_attached_flag_after_the_commands_own_and_before_help(attached):
    ctx = _context(["flash", "image", "write"])
    params = ctx.command.get_params(ctx)
    flags = _flags(params)
    assert flags.index("--force") < flags.index("--out") < flags.index("--debug-level")
    # The help option is last. Its own spellings come in no fixed order: click
    # builds them from a set (``Command.get_help_option_names``).
    assert params[-1] is ctx.command.get_help_option(ctx)

    out = _ok(["flash", "image", "write", "--help"]).output
    assert out.index("--force") < out.index("--out") < out.index("--debug-level")
    assert out.index("--debug-level") < out.index("--help")


def test_rich_help_shows_its_help_text_and_hides_a_hidden_default(attached):
    out = _ok(["flash", "erase", "--help"]).output
    assert "Where the image goes." in out
    assert "Acme's debug level." in out
    assert "--token" in out
    assert "The access token." in out
    assert SECRET not in out


# --- dispatch --------------------------------------------------------------------

OWN_KWARGS = {
    "flash erase": {},
    "flash image write": {"force": False},
    "burn": {"loud": False},
    "lazyburn": {"count": 1},
    "solo": {},
}


@pytest.mark.parametrize(
    "argv", [["flash", "erase"], ["flash", "image", "write"], ["burn"], ["lazyburn"], ["solo"]]
)
def test_a_dispatch_parses_it_and_the_function_receives_only_its_own(attached, argv):
    _ok([*argv, "--debug-level", "3"])
    [received] = plugin_commands.RECEIVED
    assert received.command == " ".join(argv)
    assert received.kwargs == OWN_KWARGS[received.command]
    assert take_attached(plugin_commands.CONTEXTS[-1])["debug_level"] == 3


def test_take_attached_returns_what_the_parser_held(attached):
    ctx = _context(["flash", "erase", "--debug-level", "3", "--out", "x"])
    taken = take_attached(ctx)
    assert sorted(taken) == sorted(NAMES)
    assert taken["debug_level"] == 3
    assert taken["out"] == Path("x")
    assert ctx.params == {}, "an attached value was left for the function"
    assert take_attached(ctx) is taken


def test_the_parser_records_where_an_attached_value_came_from(attached):
    typed = _context(["flash", "erase", "--debug-level", "3"])
    assert given_names(typed, ["debug_level"]) == {"debug_level"}

    from_env = _context(["flash", "erase"], ACME_DEBUG="5")
    assert given_names(from_env, ["debug_level"]) == {"debug_level"}
    assert take_attached(from_env)["debug_level"] == 5

    unset = _context(["flash", "erase"])
    assert given_names(unset, ["debug_level"]) == frozenset()


# --- conversion ------------------------------------------------------------------


def test_conversion_matches_a_command_that_declares_the_same_fields(attached):
    CONTROL_CALLS.clear()
    result = runner.invoke(control, WORDS, env=ENV)
    assert result.exit_code == 0, result.output
    [expected] = CONTROL_CALLS

    ctx = _context(["flash", "erase", *WORDS])
    converted = FIXED.convert(take_attached(ctx))
    assert converted == expected
    assert converted["tags"] == ["a", "b"]
    assert converted["size"] == (1, 2)
    assert converted["out"] == Path("x")


def test_the_parser_has_already_parsed_an_attached_enum(attached):
    ctx = _context(["flash", "erase", "--mode", "slow"])
    assert take_attached(ctx)["mode"] is Mode.slow


def test_convert_returns_exactly_the_names_it_is_handed(attached):
    ctx = _context(["flash", "erase", "--tags", "a", "--tags", "b", "--mode", "slow"])
    parsed = take_attached(ctx)
    assert FIXED.convert({"tags": parsed["tags"]}) == {"tags": ["a", "b"]}
    assert FIXED.convert({"mode": parsed["mode"]}) == {"mode": Mode.slow}


# --- identity and state ----------------------------------------------------------


def test_attached_parameters_keep_their_identity_through_one_parse(attached):
    ctx = _context(["flash", "erase", "--debug-level", "3"])
    first = ctx.command.get_params(ctx)
    second = ctx.command.get_params(ctx)
    assert len(first) == len(second)
    assert all(a is b for a, b in zip(first, second, strict=True))
    attached_ones = [p for p in first if p.name in NAMES]
    assert all(any(p is q for q in FIXED.params) for p in attached_ones)
    assert len(attached_ones) == len(NAMES)
    assert not any(p.name in NAMES for p in own_params(ctx.command, ctx))


def test_nothing_is_left_on_the_command_between_invocations(monkeypatch, plugins):
    root = typer.main.get_command(app)  # the root group keeps its resolved commands
    monkeypatch.setattr(invoke, "attached_params", lambda ctx, spec: FIXED.params)
    assert _otto_on(root, ["flash", "erase", "--debug-level", "3"]).exit_code == 0
    [first] = plugin_commands.CONTEXTS
    kept = dict(vars(first.command))
    assert "--debug-level" in _otto_on(root, ["flash", "erase", "--help"]).output

    monkeypatch.setattr(invoke, "attached_params", lambda ctx, spec: ())
    assert _otto_on(root, ["flash", "erase"]).exit_code == 0
    [_, second] = plugin_commands.CONTEXTS
    assert second.command is first.command, "the test needs the one command object"
    assert "--debug-level" not in _otto_on(root, ["flash", "erase", "--help"]).output
    refused = _otto_on(root, ["flash", "erase", "--debug-level", "3"])
    assert refused.exit_code == 2
    assert "No such option" in refused.output
    assert vars(second.command) == kept, "an invocation left state on the command"


# --- completion ------------------------------------------------------------------


def _complete(words: str, cword: int) -> "list[str]":
    env = {"_OTTO_COMPLETE": "complete_bash", "COMP_WORDS": words, "COMP_CWORD": str(cword)}
    result = runner.invoke(app, [], env={**ENV, **env}, prog_name="otto")
    assert result.exit_code == 0, (result.output, result.exception)
    return result.output.split()


def test_shell_completion_offers_an_attached_flag_and_its_enum_values(attached):
    assert "--debug-level" in _complete("otto flash erase --de", 3)
    assert _complete("otto flash erase --mode ", 4) == ["fast", "slow"]


# --- skipping and errors ---------------------------------------------------------


def test_an_attached_name_the_command_already_has_is_skipped(attached):
    ctx = _context(["burn", "--loud", "--debug-level", "3"])
    params = ctx.command.get_params(ctx)
    [loud] = [p for p in params if p.name == "loud"]
    assert not any(loud is q for q in FIXED.params), "the command's own `loud` must win"
    [received] = plugin_commands.RECEIVED
    assert received.kwargs == {"loud": True}
    assert "loud" not in take_attached(ctx)
    assert _flags(params).count("--loud") == 1


BROKEN = "the flags of flash erase could not be built"


def test_an_error_while_attaching_stops_that_command_and_no_other(monkeypatch, plugins, otto_cli):
    def attached_params(ctx: Any, spec: CommandSpec) -> Any:
        # A real parse (a dispatch, a --help) is the one that may fail; a
        # resilient one (a TAB, the completion cache's walk) never does.
        if command_path(ctx, spec) == ["flash", "erase"] and not ctx.resilient_parsing:
            raise OttoError(BROKEN)
        return ()

    monkeypatch.setattr(invoke, "attached_params", attached_params)
    for argv in (["flash", "erase"], ["flash", "erase", "--help"]):
        run = otto_cli(argv)
        assert run.code == 1, (argv, run)
        lines = [line for line in (run.stdout + run.stderr).splitlines() if line.strip()]
        assert len(lines) == 1, (argv, lines)
        assert BROKEN in lines[0]
    assert plugin_commands.CALLS == []

    assert otto_cli(["flash", "verify"]).code == 0
    assert [r.command for r in plugin_commands.RECEIVED] == ["flash verify"]

    group_help = otto_cli(["flash", "--help"])
    assert group_help.code == 0, group_help
    assert "erase" in group_help.stdout
    assert "Erase the board." in group_help.stdout

    assert otto_cli(["--help"]).code == 0


# --- nothing attached ------------------------------------------------------------


def _leaves(cmd: Any, ctx: Any) -> "list[tuple[Any, Any]]":
    """Every leaf beneath *cmd*, with a context of its own under *ctx*."""
    if not hasattr(cmd, "commands"):
        return [(cmd, ctx)]
    found = []
    for name in cmd.list_commands(ctx):
        ctx.meta[PENDING_SUBCMD_ARGS_KEY] = [name]  # the root resolves it real, not a stub
        sub = cmd.get_command(ctx, name)
        if sub is None:
            continue
        sub_ctx = sub.make_context(name, [], parent=ctx, resilient_parsing=True)
        found.extend(_leaves(sub, sub_ctx))
    return found


def test_with_nothing_attached_get_params_returns_the_commands_own_list(plugins):
    """With no class registered, a command parses its own parameters and nothing more.

    A project instruction is the one exception: its flags are its bodies'
    classes, which the invocation attaches (``OwnOptions.bodies``). Its
    function declares nothing, so it parses exactly those flags, then its
    own list (whatever Typer appends, and the help option).
    """
    from otto.params import options_params

    root = typer.main.get_command(app)
    root_ctx = root.make_context("otto", [], resilient_parsing=True)
    leaves = _leaves(root, root_ctx)
    assert len(leaves) > 20, "the walk found too few commands to say anything"
    with_bodies = []
    for leaf, ctx in leaves:
        assert "get_params" in vars(leaf), f"{ctx.command_path}: get_params is not wrapped"
        got = leaf.get_params(ctx)
        own = getattr(leaf.callback, OWN_OPTIONS_ATTR, None)
        if own is not None and own.bodies is not None:
            with_bodies.append(ctx.command_path)
            flags = dict.fromkeys(p.name for o in own.bodies() for p in options_params(o.cls))
            mine = own_params(leaf, ctx)
            assert [p.name for p in got] == [*flags, *(p.name for p in mine)], ctx.command_path
            assert got[len(flags) :] == mine, ctx.command_path
            continue
        assert got == type(leaf).get_params(leaf, ctx), ctx.command_path
        assert got == own_params(leaf, ctx), ctx.command_path
    assert "otto run install" in with_bodies


def test_a_leafs_accepts_marker_survives_the_wrapping():
    group = typer.Typer(name="g")

    @group.command("one")
    def one() -> None:
        """One."""

    @group.command("two")
    def two() -> None:
        """Two."""

    setattr(one, ACCEPTS_OPTIONS_ATTR, False)
    spec = CommandSpec(name="g", loader=group)
    cmd = wrap_leaf_callbacks(resolve_spec_command(spec), spec)
    leaf = cmd.commands["one"]
    assert getattr(leaf.callback, ACCEPTS_OPTIONS_ATTR) is False
    assert leaf_accepts_options(spec, leaf.callback, beneath_group=True) is False
