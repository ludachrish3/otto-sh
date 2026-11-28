"""The ``-l`` / ``-I`` / ``-E`` selection has one reader, and it answers on every path.

:func:`~otto.cli.invoke.selection_from_params` turns the root command's parsed
parameters into a :class:`~otto.config.scope.ScopeSelection`;
:func:`~otto.cli.invoke.scope_selection` calls it on the root context, the root
callback builds its record through it, and the two completers that read the
selection call :func:`scope_selection`. These tests drive real click contexts:
the app itself, or the root command's ``make_context``.
"""

from pathlib import Path

import click
import pytest
import typer
from typer.testing import CliRunner

from otto.cli import main as cli_main
from otto.cli.completers import selected_lab_names
from otto.cli.invoke import (
    OTTO_ROOT_ATTR,
    maybe_root_options,
    scope_selection,
    selection_from_params,
    validate_project_switches,
)
from otto.cli.main import app
from otto.cli.remote_completion import _collect_chain_params
from otto.config.scope import ScopeSelection
from tests._fixtures import plugin_commands
from tests._fixtures.bootstrap_seam import fake_bootstrap_result, install_composed
from tests._fixtures.clickctx import chain
from tests._fixtures.fake_project import fake_project

runner = CliRunner()

NO_LAB = {"OTTO_LAB": ""}
EXPECTED = ScopeSelection(labs=("a", "b"), include=("x-1", "y"), exclude=())


@pytest.fixture
def seen(monkeypatch):
    """The plugin commands registered, with projects ``X_1`` and ``y`` discovered."""
    install_composed(monkeypatch, fake_bootstrap_result([fake_project("X_1"), fake_project("y")]))
    with plugin_commands.registered():
        yield plugin_commands


def _root_context(args: "list[str]", **extra):
    """The root command's context after a completion parse of *args* (no callback runs)."""
    root = typer.main.get_command(app)
    return root.make_context("otto", list(args), resilient_parsing=True, **extra)


# --- the pure reader --------------------------------------------------------------


def test_the_pure_reader_takes_the_lists_as_they_are():
    got = selection_from_params(
        {"labs": ["a", "b"], "include_projects": ["x", "y"], "exclude_projects": None}
    )
    assert got == ScopeSelection(labs=("a", "b"), include=("x", "y"), exclude=())


def test_the_pure_reader_splits_nothing_and_ignores_what_is_not_a_list_of_strings():
    assert selection_from_params({"labs": "a", "include_projects": [3]}) == ScopeSelection(
        labs=(), include=(), exclude=()
    )
    assert selection_from_params({"include_projects": ["x,y"]}).include == ("x,y",)


# --- one selection on every path ----------------------------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        ["-l", "a+b", "-I", "X_1, y", "flash", "erase"],  # a dispatch
        ["-l", "a+b", "-I", "X_1, y", "flash", "erase", "--help"],  # a command's --help
    ],
    ids=["dispatch", "help"],
)
def test_one_selection_from_a_dispatch_and_from_help(argv, seen):
    result = runner.invoke(app, argv, env=NO_LAB)
    assert result.exit_code == 0, result.output
    assert seen.SELECTIONS, "the leaf's parameters were never asked for"
    assert set(seen.SELECTIONS) == {EXPECTED}
    if "--help" not in argv:
        assert [selection for _, selection in seen.CALLS] == [EXPECTED]


def test_otto_lab_gives_the_same_selection_as_the_flag(seen):
    result = runner.invoke(app, ["-I", "X_1, y", "flash", "erase"], env={"OTTO_LAB": "a+b"})
    assert result.exit_code == 0, result.output
    assert set(seen.SELECTIONS) == {EXPECTED}
    assert [selection for _, selection in seen.CALLS] == [EXPECTED]


def test_a_completion_parse_of_the_root_still_has_the_selection_split_and_normalised():
    """The root's parameter callbacks run under a completion parse; the root callback does not.

    This is what lets :func:`selection_from_params` split nothing: the values
    it reads have been through ``parse_lab_selection`` / ``parse_project_list``
    on this path too.
    """
    ctx = _root_context(["-l", "a", "-E", "Y_1,z"], obj=None)
    assert maybe_root_options(ctx) is None
    assert scope_selection(ctx) == ScopeSelection(labs=("a",), include=(), exclude=("y-1", "z"))


def test_a_top_level_command_resolved_before_the_root_callback_has_it(seen, monkeypatch):
    """The root group resolves its command before its callback records anything."""
    original = cli_main._OttoGroup.get_command
    read: "list[tuple[object, ScopeSelection]]" = []

    def get_command(self, ctx, cmd_name):
        read.append((maybe_root_options(ctx), scope_selection(ctx)))
        return original(self, ctx, cmd_name)

    monkeypatch.setattr(cli_main._OttoGroup, "get_command", get_command)
    result = runner.invoke(app, ["-l", "a+b", "-I", "X_1, y", "flash", "erase"], env=NO_LAB)
    assert result.exit_code == 0, result.output
    assert read, "the root group never resolved a command"
    assert read[0] == (None, EXPECTED)


def test_ottos_root_group_carries_the_root_marker():
    assert getattr(cli_main._OttoGroup, OTTO_ROOT_ATTR) is True


def test_a_root_that_is_not_ottos_answers_no_selection():
    assert scope_selection(chain("flash", "erase")) == ScopeSelection(
        labs=(), include=(), exclude=()
    )


def test_the_record_and_the_reader_agree(seen):
    result = runner.invoke(app, ["-l", "a+b", "-I", "X_1", "-E", "Y", "flash", "erase"], env=NO_LAB)
    assert result.exit_code == 0, result.output
    [(record, selection)] = seen.CALLS
    assert record is not None
    assert record.labs == list(selection.labs)
    assert list(record.include_projects) == list(selection.include)
    assert list(record.exclude_projects) == list(selection.exclude)


def test_no_lab_is_recorded_as_none():
    """With no ``-l`` the record's ``labs`` stays ``None``, as the no-lab path reads it."""
    with plugin_commands.registered():
        result = runner.invoke(app, ["flash", "erase"], env=NO_LAB)
        assert result.exit_code == 0, result.output
        [(record, selection)] = plugin_commands.CALLS
    assert record is not None
    assert record.labs is None
    assert selection == ScopeSelection(labs=(), include=(), exclude=())


# --- the validation takes the selection ---------------------------------------------


def test_validation_needs_no_root_record(monkeypatch, capsys):
    install_composed(monkeypatch, fake_bootstrap_result([fake_project("acme")]))
    with pytest.raises(typer.Exit) as e:
        validate_project_switches(ScopeSelection(labs=(), include=("acm",), exclude=()))
    captured = capsys.readouterr()
    assert e.value.exit_code == 2
    assert "acm" in captured.out + captured.err


def test_a_link_command_with_an_unknown_name_still_exits_2_from_the_preamble(monkeypatch):
    """``link`` reads the selection nowhere before its body: the preamble checks it."""
    install_composed(monkeypatch, fake_bootstrap_result([fake_project("acme")]))
    result = runner.invoke(app, ["-I", "acm", "link", "list"], env=NO_LAB)
    assert result.exit_code == 2, result.output
    assert "no project 'acm'" in result.output


# --- the completers read through it --------------------------------------------------


def test_the_completers_return_what_the_reader_returns():
    root = _root_context(["-l", "a+b", "-I", "X_1", "-E", "q,r"])
    group = click.Context(click.Group("host"), parent=root, info_name="host")
    group.params = {"host_id": "dut1", "hop": "", "term": None}
    leaf = click.Context(click.Command("get"), parent=group, info_name="get")
    selection = scope_selection(leaf)
    assert selection == ScopeSelection(labs=("a", "b"), include=("x-1",), exclude=("q", "r"))

    assert selected_lab_names(leaf) == list(selection.labs)
    params = _collect_chain_params(leaf)
    assert params.labs == list(selection.labs)
    assert params.include_projects == list(selection.include)
    assert params.exclude_projects == list(selection.exclude)
    assert params.host_id == "dut1"


# --- one preamble reads the selection once -------------------------------------------


def test_both_preamble_gates_read_the_selection_the_root_parsed_not_the_record(monkeypatch, capsys):
    """One preamble, one selection: the switch check and the bootstrap gate agree.

    The root parsed ``-E project2`` but the record holds no selection, as on a
    path before the root callback recorded. ``project2`` failed to load: the
    switch check finds the name, and the bootstrap gate, reading the same
    selection, demotes the error to a warning instead of failing the command.
    """
    from otto import bootstrap as bs
    from otto.cli import invoke
    from otto.cli.registry import CommandSpec
    from tests._fixtures.preamble_ctx import FakeLeafCtx

    project = fake_project("project2", root=Path("/projects/project2"))
    error = bs.BootstrapError(project.root, source="init", cause=ImportError("x"))
    install_composed(monkeypatch, fake_bootstrap_result([project], errors=[error]))
    ctx = FakeLeafCtx(CommandSpec(name="probe", loader=None, lab_free=True))
    ctx.parent = chain("otto", root_params={"exclude_projects": ["project2"]})
    assert ctx.meta["_otto_root_options"].exclude_projects == ()
    checked = []
    validate = invoke.validate_project_switches
    monkeypatch.setattr(
        invoke, "validate_project_switches", lambda sel: checked.append(sel) or validate(sel)
    )
    monkeypatch.setattr(invoke, "settle_options", lambda: None)
    monkeypatch.setattr(invoke, "stop_at_dry_run_seam", lambda ctx, spec: None)

    invoke.command_preamble(ctx)  # does not exit: the error is demoted, not fatal

    assert checked == [ScopeSelection(labs=(), include=(), exclude=("project2",))]
    assert "inactive for this run" in capsys.readouterr().err
