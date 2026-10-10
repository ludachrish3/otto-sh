"""Conformance cases for the CLI command registry (a raw record seam with a decorator)."""

import functools

from otto.cli.registry import CLI_COMMANDS, CommandSpec, cli_command, register_cli_command
from tests.unit.registry import conformance

COVERS = ["otto.cli.registry:CLI_COMMANDS"]


def test_cli_commands_raw_case():
    conformance.assert_raw_registry(
        CLI_COMMANDS,
        make=lambda i: ("case-cmd", CommandSpec(name="case-cmd", loader=f"pkg.cmds:app{i}")),
    )


def test_cli_commands_wrapper_case():
    conformance.assert_wrapper_matches_raw(
        CLI_COMMANDS,
        via_wrapper=functools.partial(register_cli_command, "case-cmd", "pkg.cmds:app"),
        record_for=lambda: CommandSpec(name="case-cmd", loader="pkg.cmds:app"),
    )


def test_the_decorator_credits_the_decorating_module():
    def leaf() -> None: ...

    decorate = cli_command(name="case-deco", lab_free=True)
    conformance.from_module("case_decorating.init", decorate, leaf)
    assert CLI_COMMANDS.origin("case-deco") == "case_decorating.init"
