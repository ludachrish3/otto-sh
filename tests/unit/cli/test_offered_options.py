"""A covering class's flags are attached to a command per invocation, and a failure stays there.

:func:`~otto.cli.invoke.offered_options` is the one offer: under a real parse
(a dispatch, the command's own ``--help``) it resolves the registrations that
cover the command (:func:`~otto.params.covering`) and merges their fields with
the command's own parameters reserved, so a collision or a class that cannot
be imported stops that command only. Under a completion parse it is the union
of :func:`~otto.cli.invoke.live_candidates`, each class described on its own,
which cannot fail.
"""

import contextlib
import sys
from collections.abc import Callable, Generator
from typing import Annotated, Any

import pytest
import typer
from typer.testing import CliRunner

import otto
from otto.cli import invoke
from otto.cli.invoke import (
    ACCEPTS_OPTIONS_ATTR,
    LAB_FREE_ATTR,
    OWN_OPTIONS_ATTR,
    Candidate,
    OwnOptions,
    command_spec,
    live_candidates,
    offered_options,
    take_attached,
)
from otto.cli.main import PENDING_SUBCMD_ARGS_KEY, app
from otto.cli.registry import register_cli_command
from otto.params import OptionsOrigin, options_key, options_params, register_options
from otto.registry import get_completion_names, registering_project, set_completion_names
from tests._fixtures import plugin_commands
from tests._fixtures.bootstrap_seam import fake_bootstrap_result, install_composed
from tests._fixtures.dispatch import DispatchRunner
from tests._fixtures.entry_runner import CliRun, complete
from tests._fixtures.fake_project import fake_project
from tests._fixtures.plugin_commands import Acme

runner = CliRunner()

ENV = {"OTTO_LAB": "", "ACME_DEBUG": None, "COLUMNS": "200"}

SHAPES = [["flash", "erase"], ["flash", "image", "write"], ["burn"], ["lazyburn"], ["solo"]]

UNIMPORTABLE = "tests_offered_options_nowhere:Missing"
"""A registration whose module does not exist."""


@otto.options
class Variant:
    variant: Annotated[str, typer.Option(help="Which board variant.")] = "a"


@otto.options
class OtherDebug:
    """Spells ``--debug-level`` too, introduced on its own: it collides with :class:`Acme`."""

    debug_level: Annotated[int, typer.Option(help="Another debug level.")] = 0


@otto.options
class Healthy:
    speed: Annotated[int, typer.Option(help="How fast.")] = 1


@otto.options
class ShortH:
    hush: Annotated[bool, typer.Option("-h", help="Hush.")] = False


@otto.options
class Lvl:
    lvl: Annotated[int, typer.Option("--lvl", help="A level.")] = 0


@otto.options
class Port:
    port: Annotated[str, typer.Option(help="The class's port.")] = "none"


def _register(cls: "type | str", verbs: "list[str]", *, project: "str | None" = "a") -> None:
    with registering_project(project) if project else contextlib.nullcontext():
        register_options(cls, verbs=verbs)


@pytest.fixture
def plugins(monkeypatch):
    """The fixture plugin commands registered as accepting, with two projects discovered."""
    install_composed(monkeypatch, fake_bootstrap_result([fake_project("a"), fake_project("b")]))
    with plugin_commands.registered(accepts_options=True):
        yield plugin_commands


@pytest.fixture
def acme_everywhere(plugins):
    """:class:`Acme` registered by project ``a`` for every fixture command."""
    _register(Acme, ["flash", "burn", "lazyburn", "solo"])
    return plugins


def _otto(argv: "list[str]", **env: "str | None") -> Any:
    return runner.invoke(app, argv, env={**ENV, **env})


def _ok(argv: "list[str]") -> Any:
    result = _otto(argv)
    assert result.exit_code == 0, (result.output, result.exception)
    return result


# --- every plugin shape ----------------------------------------------------------


@pytest.mark.parametrize("argv", SHAPES)
def test_every_plugin_shape_shows_a_covering_flag_in_its_help(argv, acme_everywhere):
    assert "--debug-level" in _ok([*argv, "--help"]).output


@pytest.mark.parametrize("argv", SHAPES)
def test_every_plugin_shape_dispatches_with_a_covering_flag(argv, acme_everywhere):
    _ok([*argv, "--debug-level", "3"])
    [received] = plugin_commands.RECEIVED
    assert received.command == " ".join(argv)
    assert "debug_level" not in received.kwargs
    assert take_attached(plugin_commands.CONTEXTS[-1]) == {"debug_level": 3}


def test_a_command_entry_reaches_that_command_only(plugins):
    _register(Variant, ["flash erase"])
    assert "--variant" in _ok(["flash", "erase", "--help"]).output
    refused = _otto(["flash", "verify", "--variant", "x"])
    assert refused.exit_code == 2
    assert "No such option" in refused.output


# --- a command hung on `run` (§6 item 14) ----------------------------------------


@contextlib.contextmanager
def _hung_on_run(**commands: "Callable[..., Any]") -> "Generator[None]":
    """Hang each of *commands* on ``otto run`` under its name for the block."""
    from otto.cli.run import run_app

    before = list(run_app.registered_commands)
    for name, function in commands.items():
        run_app.command(name)(function)
    try:
        yield
    finally:
        run_app.registered_commands[:] = before


HUNG: "list[dict[str, Any]]" = []


async def _hung(level: int = 1) -> None:
    """A command hung on run."""
    HUNG.append({"level": level})


def test_a_command_hung_on_run_gains_the_flags_of_a_run_class_and_dispatches(plugins):
    from otto.cli.run import run_app

    _register(Acme, ["run"])
    HUNG.clear()
    with _hung_on_run(hung=_hung):
        assert "--debug-level" in _ok(["run", "hung", "--help"]).output
        result = DispatchRunner().invoke(
            run_app,
            ["hung", "--level", "2", "--debug-level", "3"],
            env=ENV,
            async_leaves=True,
            accepts_options=True,
        )
    assert result.exit_code == 0, (result.output, result.exception)
    assert HUNG == [{"level": 2}], "the function was passed an attached name"


async def _burn_by_name(debug_level: int = 0) -> None:  # pragma: no cover — refused before it runs
    """Burn, with a parameter named like a run field."""


async def _burn_by_spelling(  # pragma: no cover — refused before it runs
    level: Annotated[int, typer.Option("--debug-level")] = 0,
) -> None:
    """Burn, with a flag spelled like a run field."""


async def _status() -> None:
    """Report the board's status."""
    HUNG.append({"status": True})


setattr(_status, LAB_FREE_ATTR, True)
setattr(_status, ACCEPTS_OPTIONS_ATTR, True)


@pytest.mark.parametrize(
    ("burn", "own"),
    [(_burn_by_name, "parameter 'debug_level'"), (_burn_by_spelling, "parameter 'level'")],
    ids=["by-name", "by-spelling"],
)
def test_its_own_parameter_that_spells_a_run_field_stops_it_and_its_siblings_run(
    burn, own, plugins, otto_cli
):
    _register(Acme, ["run"])
    HUNG.clear()
    with _hung_on_run(burn=burn, status=_status):
        stopped = otto_cli(["run", "burn"])
        assert stopped.code == 1, stopped
        line = stopped.text
        assert f"{own} of otto run burn" in line
        assert "field 'debug_level' of project 'a'" in line
        assert "Acme" in line

        assert "--debug-level" in _ok(["run", "status", "--help"]).output
        ran = otto_cli(["run", "status"])
    assert ran.code == 0, ran
    assert HUNG == [{"status": True}]


# --- otto's own commands -----------------------------------------------------------


def test_cov_get_gains_a_cov_class_and_check_stamp_does_not(plugins):  # §6 item 15
    _register(Variant, ["cov"])
    assert "--variant" in _ok(["cov", "get", "--help"]).output
    assert "--variant" not in _ok(["cov", "check-stamp", "--help"]).output


@pytest.mark.parametrize(
    "argv", [["link", "list"], ["tunnel", "list"], ["monitor"]], ids=["link", "tunnel", "monitor"]
)
def test_link_tunnel_and_monitor_offer_no_registered_flag(argv, plugins):
    _register(Variant, ["link", "tunnel", "monitor"])
    assert "--variant" not in _ok([*argv, "--help"]).output


def test_a_command_under_an_accepting_group_can_opt_out(monkeypatch, acme_everywhere):
    monkeypatch.setattr(plugin_commands.verify, ACCEPTS_OPTIONS_ATTR, False, raising=False)
    assert "--debug-level" not in _ok(["flash", "verify", "--help"]).output
    assert "--debug-level" in _ok(["flash", "erase", "--help"]).output


# --- a failure stays with its command -----------------------------------------------


def _stops_erase_only(otto_cli: "Callable[[list[str]], CliRun]", *says: str) -> None:
    for argv in (["flash", "erase"], ["flash", "erase", "--help"]):
        run = otto_cli(argv)
        assert run.code == 1, (argv, run)
        assert "Traceback" not in run.text
        assert run.text.startswith("error: "), run.text
        for said in says:
            assert said in run.text, (argv, run.text)
    assert plugin_commands.RECEIVED == []

    assert otto_cli(["flash", "verify"]).code == 0
    assert [r.command for r in plugin_commands.RECEIVED] == ["flash verify"]

    group_help = otto_cli(["flash", "--help"])
    assert group_help.code == 0, group_help
    assert "erase" in group_help.stdout
    assert "Erase the board." in group_help.stdout
    assert otto_cli(["--help"]).code == 0


def test_a_collision_stops_its_command_and_nothing_else(plugins, otto_cli):
    _register(Acme, ["flash"], project="a")
    _register(OtherDebug, ["flash erase"], project="b")
    _stops_erase_only(otto_cli, "'debug_level'", "project 'a'", "project 'b'", "otto flash erase")


def test_an_import_error_in_a_covering_class_stops_its_command_only(plugins, otto_cli):
    _register(UNIMPORTABLE, ["flash erase"])
    _stops_erase_only(otto_cli, UNIMPORTABLE, "cannot be imported", "project 'a'")


@pytest.mark.parametrize(
    ("cls", "spelling", "owner"),
    [(ShortH, "-h", "parameter 'help'"), (Lvl, "--lvl", "parameter 'level'")],
    ids=["help-option", "default-value-style"],
)
def test_a_field_spelled_like_a_commands_own_flag_collides(cls, spelling, owner, plugins, otto_cli):
    """``-h`` is the help option's, and a ``typer.Option`` default spells its own flag."""
    board = typer.Typer(name="board")

    @board.command("go")
    def go(level: int = typer.Option(0, "--lvl")) -> None:  # pragma: no cover — refused first
        """Go."""

    @board.command("stop")
    def stop() -> None:  # pragma: no cover — only listed
        """Stop."""

    register_cli_command("board", board, lab_free=True, accepts_options=True)
    _register(cls, ["board"])
    run = otto_cli(["board", "go"])
    assert run.code == 1, run
    line = run.text
    assert f"flag {spelling} of field" in line
    assert f"{owner} of otto board go" in line


@pytest.fixture
def counting_merge(monkeypatch) -> "list[str]":
    """Every ``what=`` the offer's merge is called with."""
    asked: list[str] = []
    merge = invoke.merge_option_params

    def counted(*args: Any, **kwargs: Any) -> Any:
        asked.append(kwargs["what"])
        return merge(*args, **kwargs)

    monkeypatch.setattr(invoke, "merge_option_params", counted)
    return asked


def test_nothing_is_built_for_a_command_that_was_not_asked_for(counting_merge, acme_everywhere):
    _ok(["flash", "verify"])
    _ok(["flash", "--help"])
    assert counting_merge, "the test needs the merge to be watched"
    assert "otto flash erase" not in counting_merge


def test_a_group_option_of_the_same_name_is_not_reserved(plugins):  # Review Focus 5
    seen: list[typer.Context] = []
    board = typer.Typer(name="board")

    @board.callback()
    def board_main(port: Annotated[str, typer.Option()] = "group") -> None:
        """A board with a port of its own."""

    @board.command("erase")
    def board_erase(ctx: typer.Context) -> None:
        seen.append(ctx)

    @board.command("verify")
    def board_verify() -> None:  # pragma: no cover — only listed
        """Verify."""

    register_cli_command("board", board, lab_free=True, accepts_options=True)
    _register(Port, ["board"])
    _ok(["board", "--port", "X", "erase", "--port", "Y"])
    [ctx] = seen
    assert ctx.parent is not None
    assert ctx.parent.params == {"port": "X"}
    assert take_attached(ctx) == {"port": "Y"}


def test_a_sub_app_driven_without_ottos_root_offers_every_projects_flags(plugins):
    _register(Acme, ["flash"], project="b")
    dispatch = DispatchRunner()
    shown = dispatch.invoke(
        plugin_commands.flash_app, ["erase", "--help"], env=ENV, accepts_options=True
    )
    assert shown.exit_code == 0, (shown.output, shown.exception)
    assert "--debug-level" in shown.output
    ran = dispatch.invoke(
        plugin_commands.flash_app,
        ["erase", "--debug-level", "3"],
        env=ENV,
        spec_name="flash",
        accepts_options=True,
    )
    assert ran.exit_code == 0, (ran.output, ran.exception)
    assert [r.command for r in plugin_commands.RECEIVED] == ["flash erase"]
    assert take_attached(plugin_commands.CONTEXTS[-1]) == {"debug_level": 3}


def test_a_command_covered_by_no_class_resolves_nothing(monkeypatch, plugins):
    carried: list[Any] = []
    carrier = invoke.carrier
    monkeypatch.setattr(invoke, "carrier", lambda params: carried.append(params) or carrier(params))
    _register("tests_offered_options_unimported:Never", ["burn"])
    _ok(["flash", "erase"])
    ctx = plugin_commands.CONTEXTS[-1]
    assert offered_options(ctx, command_spec(ctx)) is None
    assert "tests_offered_options_unimported" not in sys.modules
    assert carried == []


def test_run_and_test_are_offered_like_every_command(monkeypatch, plugins):
    """``otto run <name>`` and ``otto test`` take a covering class's flag from the one offer."""
    from otto.instruction_catalog import instruction

    answers: dict[str, Any] = {}
    offer = invoke.offered_options

    def watched(ctx: Any, spec: Any) -> Any:
        answer = offer(ctx, spec)
        answers[" ".join(invoke.command_path(ctx, spec))] = answer
        return answer

    monkeypatch.setattr(invoke, "offered_options", watched)

    async def cool() -> None:  # pragma: no cover — only its help is read
        """Cool the board."""

    instruction("cool")(cool)
    _register(Acme, ["run", "test"])
    assert _ok(["run", "cool", "--help"]).output.count("--debug-level") == 1
    assert _ok(["test", "--help"]).output.count("--debug-level") == 1
    offered = {path: [p.name for p in answer.params] for path, answer in answers.items()}
    assert offered == {"run cool": ["debug_level"], "test": ["debug_level"]}


# --- the candidate union (D12): a completion parse cannot fail --------------------


def _broken(how: str) -> None:
    _register(Acme, ["flash"], project="a")
    if how == "two_classes_that_collide":
        _register(OtherDebug, ["flash erase"], project="b")
    else:
        _register(UNIMPORTABLE, ["flash erase"], project="b")
    _register(Healthy, ["flash erase"], project="b")


BROKEN = ["two_classes_that_collide", "a_class_that_cannot_be_imported"]


@pytest.mark.parametrize("broken", BROKEN)
def test_a_cold_tab_completes_where_the_live_command_would_stop(broken, plugins):
    _broken(broken)
    words = complete("otto flash erase --", ENV)
    assert words.count("--debug-level") == 1
    assert "--speed" in words
    assert "--help" in words


@pytest.mark.parametrize("broken", BROKEN)
def test_a_warm_tab_completes_where_the_live_command_would_stop(broken, plugins):
    _broken(broken)
    saved = get_completion_names()
    set_completion_names({"commands": [], "instructions": []})
    try:
        words = complete("otto flash erase --", ENV)
    finally:
        set_completion_names(saved)
    assert words.count("--debug-level") == 1
    assert "--speed" in words


@pytest.mark.parametrize("broken", BROKEN)
def test_root_help_rebuilds_the_cache_where_the_live_command_would_stop(
    broken, monkeypatch, plugins, otto_cli
):
    from otto.config import completion_tree

    walked: list[Any] = []
    serialize = completion_tree.serialize_tree

    def watched(*args: Any, **kwargs: Any) -> Any:
        walked.append(serialize(*args, **kwargs))
        return walked[-1]

    monkeypatch.setattr(completion_tree, "serialize_tree", watched)
    # A stale cache this workspace may write: the rebuild runs, and its
    # result is not stored (the fake projects have no workspace on disk).
    monkeypatch.setattr("otto.cli.main._cache_is_writable", lambda projects: True)
    monkeypatch.setattr("otto.config.completion_cache.cache_is_stale", lambda *a, **k: True)
    monkeypatch.setattr("otto.config.completion_cache.write_sections", lambda *a, **k: None)
    _broken(broken)
    run = otto_cli(["--help"])
    assert run.code == 0, run
    assert walked, "root help did not rebuild the cache: the walk was not reached"
    erase = walked[-1].tree["commands"]["flash"]["commands"]["erase"]
    assert [p["name"] for p in erase["params"]] == ["help"]


def test_the_cache_walk_stores_a_commands_own_parameters_only(acme_everywhere):
    from otto.config.completion_tree import serialize_tree

    tree = serialize_tree(typer.main.get_command(app), pending_key=PENDING_SUBCMD_ARGS_KEY).tree
    flash = tree["commands"]["flash"]["commands"]
    assert [p["name"] for p in flash["erase"]["params"]] == ["help"]
    assert [p["name"] for p in flash["image"]["commands"]["write"]["params"]] == ["force", "help"]
    burn = [p["name"] for p in tree["commands"]["burn"]["params"]]
    assert "loud" in burn
    assert "debug_level" not in burn


def test_live_candidates_describes_each_class_on_its_own(plugins):
    _register(Acme, ["flash"], project="a")
    _register(OtherDebug, ["flash erase"], project="b")
    _register(UNIMPORTABLE, ["flash"], project="b")
    own = OwnOptions(bodies=lambda: [OptionsOrigin(Healthy, "c")])

    found = live_candidates(["flash", "erase"], own)
    assert [(c.key, c.project, c.entries) for c in found] == [
        (options_key(Healthy), "c", ("flash erase",)),
        (options_key(Acme), "a", ("flash",)),
        (options_key(OtherDebug), "b", ("flash erase",)),
    ]
    assert all(isinstance(c, Candidate) for c in found)
    assert [[p.name for p in c.params] for c in found] == [
        ["speed"],
        ["debug_level"],
        ["debug_level"],
    ]
    assert [c.key for c in live_candidates(["flash", "verify"], None)] == [options_key(Acme)]
    assert live_candidates(["flash", "erase"], own, covered=False)[0].key == options_key(Healthy)
    assert len(live_candidates(["flash", "erase"], own, covered=False)) == 1


# --- what a command says is its own ------------------------------------------------


@otto.options
class OwnBoard:
    mode: Annotated[str, typer.Option(help="The board's own mode.")] = "fast"


def _own_command(**reserved: str) -> typer.Typer:
    """A ``board go`` whose signature holds :class:`OwnBoard`'s fields and ``extra``."""
    import inspect

    board = typer.Typer(name="board")

    def go(ctx: typer.Context, **kw: Any) -> None:
        plugin_commands.CONTEXTS.append(ctx)
        plugin_commands.RECEIVED.append(plugin_commands.Received("board go", kw, 0))

    go.__signature__ = inspect.Signature(  # ty: ignore[unresolved-attribute] — Typer reads it
        [
            inspect.Parameter(
                "ctx", inspect.Parameter.POSITIONAL_OR_KEYWORD, annotation=typer.Context
            ),
            *options_params(OwnBoard),
            inspect.Parameter("extra", inspect.Parameter.KEYWORD_ONLY, default=0, annotation=int),
        ]
    )
    setattr(go, OWN_OPTIONS_ATTR, OwnOptions(cls=OwnBoard, project="p", reserved=reserved))
    board.command("go")(go)

    @board.command("stop")
    def stop() -> None:  # pragma: no cover — only listed
        """Stop."""

    return board


@otto.options
class Extra:
    extra: Annotated[int, typer.Option()] = 0


def test_an_own_class_is_the_commands_own(plugins):
    register_cli_command("board", _own_command(), lab_free=True, accepts_options=True)
    _register(Acme, ["board"])
    _ok(["board", "go", "--mode", "slow", "--debug-level", "2"])
    [received] = plugin_commands.RECEIVED
    assert received.kwargs == {"mode": "slow", "extra": 0}
    ctx = plugin_commands.CONTEXTS[-1]
    offer = offered_options(ctx, command_spec(ctx))
    assert offer is not None
    assert [p.name for p in offer.params] == ["debug_level"]
    assert offer.spec.group == "board"
    assert offer.spec.build == (OwnBoard, Acme)
    assert offer.convert({"debug_level": 2, "mode": "slow"}) == {"debug_level": 2, "mode": "slow"}


def test_a_collision_with_an_own_parameter_is_named_as_the_command_says(plugins, otto_cli):
    register_cli_command(
        "board", _own_command(extra="the board's 'extra'"), lab_free=True, accepts_options=True
    )
    _register(Extra, ["board go"], project="b")
    run = otto_cli(["board", "go"])
    assert run.code == 1, run
    line = run.text
    assert "field 'extra' of project 'b'" in line
    assert "collides with the board's 'extra'" in line
