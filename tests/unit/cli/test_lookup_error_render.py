"""A host lookup that escapes a command prints its message as plain text.

``UnknownHostError`` and ``HostOutOfScopeError`` are ``KeyError`` subclasses,
and ``KeyError.__str__`` quotes its argument. Both are also ``OttoError``s, so
the root dispatch's error boundary prints ``str()`` of them: each must read as
the message itself, never as a quoted repr of it.
"""

from typing import Any

import pytest

from otto.cli.registry import CLI_COMMANDS, register_cli_command
from otto.config.scope import HostOutOfScopeError
from otto.host.targeting import UnknownHostError, unknown_host_error
from tests._fixtures.fleet import _lab, _project, add_builtin_local, dispatch_root


@pytest.fixture(autouse=True)
def _clean_registry():
    before = set(CLI_COMMANDS.names())
    yield
    for name in set(CLI_COMMANDS.names()) - before:
        CLI_COMMANDS.unregister(name=name)


def _looks_up(name: str, host_id: str) -> None:
    """Register a lab-bound plugin command *name* whose body looks up *host_id*."""

    async def leaf() -> None:
        from otto.context import get_context

        get_context().get_host(host_id)

    register_cli_command(name, leaf)


def _alpha_lab() -> Any:
    return add_builtin_local(
        _lab(("a-dut", "alpha"), ("a-jump", "alpha"), component_names=["alpha"])
    )


def test_an_escaped_unknown_host_prints_unquoted_and_exits_1(monkeypatch, tmp_path):
    """Fail-first: the boundary prints ``error: No host 'nope' …``, not a quoted repr."""
    _looks_up("lookup-probe", "nope")
    project = _project(tmp_path, "plain")

    result = dispatch_root(
        monkeypatch,
        ["lookup-probe"],
        lab=_alpha_lab(),
        projects=[project],
        env={"COLUMNS": "400"},
        through_entry=True,
    )

    assert result.exit_code == 1, result.output
    lines = [line for line in result.output.splitlines() if "No host 'nope'" in line]
    assert lines, (result.output, repr(result.exception))
    assert lines[0].startswith("error: No host 'nope' in lab 'alpha'."), result.output
    assert '"' not in lines[0], result.output


@pytest.mark.parametrize(
    "error",
    [
        unknown_host_error("nope", lab_name="alpha", available=["a-dut"]),
        HostOutOfScopeError("host 'a-dut' is in lab 'alpha' but outside this run's scope"),
    ],
    ids=["unknown", "out-of-scope"],
)
def test_both_lookup_errors_render_their_message_unquoted(error):
    """The two lookup errors share one rendering: ``str()`` is the message itself."""
    assert isinstance(error, KeyError)
    assert str(error) == error.args[0]
    if isinstance(error, UnknownHostError):
        assert str(error) == error.message
