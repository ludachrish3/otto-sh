"""A command's registration says whether it offers registered options classes' flags.

:func:`~otto.cli.invoke.leaf_accepts_options` is the one reader: beneath a
group, the callback's :data:`~otto.cli.invoke.ACCEPTS_OPTIONS_ATTR` marker, then
the registration's ``accepts_options``, then "the command is not lab-free"; for
a top-level command, the registration alone
(:func:`~otto.cli.invoke.spec_accepts_options`). A top-level single command
whose marker disagrees with its registration is refused when it is resolved.
:func:`~otto.cli.invoke.command_path` names a command from its registered name
down, with or without otto's root group.
"""

import json
from collections.abc import Callable
from typing import Any

import pytest
import typer
from typer.testing import CliRunner

from otto.cli.builtin_commands import builtin_command_names
from otto.cli.invoke import (
    ACCEPTS_OPTIONS_ATTR,
    LAB_FREE_ATTR,
    command_path,
    leaf_accepts_options,
    spec_accepts_options,
)
from otto.cli.registry import CLI_COMMANDS, CommandSpec, register_cli_command, resolve_spec_command
from tests._fixtures import plugin_commands
from tests._fixtures.bootstrapstub import bootstrap_stub
from tests._fixtures.clickctx import chain
from tests._fixtures.dispatch import DispatchRunner

T, F, U = True, False, None


def _callback(**attrs: Any) -> "Callable[[], None]":
    """A leaf callback carrying *attrs* (marker attribute name -> value)."""

    def leaf() -> None:  # pragma: no cover — only its attributes are read
        pass

    for name, value in attrs.items():
        setattr(leaf, name, value)
    return leaf


def _marker(marker: "bool | None") -> "dict[str, Any]":
    return {} if marker is None else {ACCEPTS_OPTIONS_ATTR: marker}


# --- the reader ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("field", "marker", "lab_free", "expected"),
    [
        (f, m, lf, m if m is not None else f if f is not None else not lf)
        for f in (T, F, U)
        for m in (T, F, U)
        for lf in (T, F)
    ],
)
def test_beneath_a_group_the_marker_then_the_field_then_not_lab_free(
    field, marker, lab_free, expected
):
    # lab_free on the registration ...
    spec = CommandSpec(name="g", loader=None, lab_free=lab_free, accepts_options=field)
    assert leaf_accepts_options(spec, _callback(**_marker(marker)), beneath_group=True) is expected
    # ... and by the lab-free marker on the callback alone.
    spec = CommandSpec(name="g", loader=None, lab_free=False, accepts_options=field)
    callback = _callback(**_marker(marker), **{LAB_FREE_ATTR: lab_free})
    assert leaf_accepts_options(spec, callback, beneath_group=True) is expected


@pytest.mark.parametrize(
    ("field", "lab_free", "expected"),
    [(T, T, T), (T, F, T), (F, T, F), (F, F, F), (U, T, F), (U, F, T)],
)
def test_a_top_level_command_is_judged_by_its_registration(field, lab_free, expected):
    spec = CommandSpec(name="c", loader=None, lab_free=lab_free, accepts_options=field)
    assert spec_accepts_options(spec) is expected
    assert leaf_accepts_options(spec, _callback(), beneath_group=False) is expected


def test_a_lab_free_marker_on_a_top_level_callback_does_not_change_the_answer():
    spec = CommandSpec(name="c", loader=None, lab_free=False)
    callback = _callback(**{LAB_FREE_ATTR: True})
    assert leaf_accepts_options(spec, callback, beneath_group=False) is True


DISAGREEING = [(T, F), (F, T), (U, F)]
"""(``accepts_options``, callback marker) pairs that disagree on a lab-bound command."""


@pytest.fixture
def marked(request):
    """A flattened plugin command ``marked`` whose marker disagrees with its registration."""
    field, marker = request.param
    app = typer.Typer()

    def body(level: int = 0) -> None:  # pragma: no cover — never dispatched here
        pass

    setattr(body, ACCEPTS_OPTIONS_ATTR, marker)
    app.command("marked")(body)
    register_cli_command("marked", app, accepts_options=field)
    try:
        yield
    finally:
        CLI_COMMANDS.unregister(name="marked")


@pytest.mark.parametrize("marked", DISAGREEING, indirect=True)
def test_a_marker_that_disagrees_on_a_flattened_command_is_refused_when_resolved(
    marked, capsys, monkeypatch
):
    monkeypatch.setattr("otto.bootstrap.bootstrap", bootstrap_stub)
    monkeypatch.setattr("sys.argv", ["otto", "marked"])
    monkeypatch.setenv("COLUMNS", "400")  # one line: rich would wrap the message
    wrapper = typer.Typer()

    @wrapper.command()
    def otto_entry() -> None:
        from otto.cli.main import entry

        entry()

    result = CliRunner().invoke(wrapper, [])
    out = result.output + capsys.readouterr().err
    assert result.exit_code == 1, (out, result.exception)
    lines = [line for line in out.splitlines() if line.strip()]
    assert len(lines) == 1, out
    assert "'marked'" in lines[0]
    assert "set accepts_options= on the registration" in lines[0]
    assert "Traceback" not in out


@pytest.mark.parametrize("marked", DISAGREEING, indirect=True)
def test_the_cache_rebuild_walk_serialises_a_disagreeing_command(marked):
    # The walk entry() runs to rebuild a stale cache, on root --help and on a
    # TAB: it resolves every top-level command real, under resilient contexts.
    # A child it cannot build is kept by name only, so the command's own
    # `--level` in its node is what shows it was built.
    from otto.cli.main import PENDING_SUBCMD_ARGS_KEY, app
    from otto.config.completion_tree import serialize_tree

    got = serialize_tree(typer.main.get_command(app), pending_key=PENDING_SUBCMD_ARGS_KEY)
    assert "--level" in json.dumps(got.tree["commands"]["marked"]["params"])


@pytest.mark.parametrize("marked", DISAGREEING, indirect=True)
def test_a_completion_parse_resolves_a_disagreeing_command(marked):
    from otto.cli.main import app

    root = typer.main.get_command(app)
    ctx = root.make_context("otto", ["marked"], resilient_parsing=True)
    assert root.get_command(ctx, "marked").name == "marked"


# --- otto's own registrations -------------------------------------------------------


def test_ottos_own_registrations():
    accepts = {n: spec_accepts_options(CLI_COMMANDS.get(n)) for n in builtin_command_names()}
    assert {n for n, a in accepts.items() if a} == {"run", "test", "host", "cov", "docker"}


def test_the_six_lab_free_cov_commands_do_not_accept_and_get_does():
    spec = CLI_COMMANDS.get("cov")
    cov = resolve_spec_command(spec)

    def leaf(*path: str) -> Any:
        node = cov
        for name in path:
            node = node.commands[name]
        return node

    def accepts(*path: str) -> bool:
        return leaf_accepts_options(spec, leaf(*path).callback, beneath_group=True)

    lab_free = [
        ("check-stamp",),
        ("kmodcov", "export"),
        ("kmodcov", "check"),
        ("embedded-gcov", "export"),
        ("embedded-gcov", "check"),
        ("embedded-gcov", "finish"),
    ]
    assert [accepts(*path) for path in lab_free] == [False] * 6
    assert accepts("get") is True


# --- the command's path -------------------------------------------------------------


@pytest.fixture
def contexts():
    """:data:`plugin_commands.CONTEXTS`, empty on the way in and on the way out."""
    plugin_commands.CONTEXTS.clear()
    try:
        yield plugin_commands.CONTEXTS
    finally:
        plugin_commands.CONTEXTS.clear()


def test_the_path_starts_with_the_registered_name_with_and_without_ottos_root(contexts):
    # Under otto's root: `otto host r1 install`. The host id is an argument of
    # the `host` group, not a context, so it is not in the path.
    host = CommandSpec(name="host", loader=None)
    assert command_path(chain("otto", "host", "install", root_params={}), host) == [
        "host",
        "install",
    ]
    # A top-level single command under otto's root.
    solo = CommandSpec(name="solo", loader=None)
    assert command_path(chain("otto", "solo", root_params={}), solo) == ["solo"]

    # Without otto's root: the sub-app's own root is the top-level command.
    flash = CommandSpec(name="flash", loader=None)
    result = DispatchRunner().invoke(plugin_commands.flash_app, ["erase"], spec_name="flash")
    assert result.exit_code == 0, (result.output, result.exception)
    assert [command_path(ctx, flash) for ctx in contexts] == [["flash", "erase"]]
    # The first word is the registration's, not the context's.
    burn = CommandSpec(name="burn", loader=None)
    assert [command_path(ctx, burn) for ctx in contexts] == [["burn", "erase"]]

    contexts.clear()
    result = DispatchRunner().invoke(plugin_commands.solo_app, [], spec_name="solo")
    assert result.exit_code == 0, (result.output, result.exception)
    assert [command_path(ctx, solo) for ctx in contexts] == [["solo"]]
