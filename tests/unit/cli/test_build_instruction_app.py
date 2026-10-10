"""The CLI builds a registered instruction's Typer app from its data entry."""

import dataclasses

from typer.testing import CliRunner

from otto.cli.run import build_instruction_app
from otto.instructions import STANDALONE_INSTRUCTIONS, InstructionEntry

# Invoked on the Typer app itself, never on `typer.main.get_command(app)`'s
# result: `typer.testing.CliRunner.invoke` resolves the command from the
# Typer instance ITSELF (it calls `get_command` again internally), and the
# already-resolved command it would otherwise receive comes from typer's
# vendored click fork, which is not a safe `CliRunner` target on its own
# (see tests/unit/cli/test_lifecycle_bridge.py).
runner = CliRunner()


@dataclasses.dataclass
class _Opts:
    debug: bool = False


async def _deploy(opts: _Opts) -> None:
    """Deploy the widget."""


async def _deploy_no_opts() -> None:
    """Deploy the widget."""


def _registered(entry: InstructionEntry) -> InstructionEntry:
    """Register *entry*: the app reads its owner from the table it is served from."""
    STANDALONE_INSTRUCTIONS.register(entry.name, entry)
    return entry


def test_build_instruction_app_exposes_the_options_fields_as_flags():
    entry = _registered(
        InstructionEntry(name="deploy", module="m", handler=_deploy, options_cls=_Opts)
    )
    app = build_instruction_app(entry)
    (command,) = app.registered_commands
    assert command.name == "deploy"
    assert "--debug" in runner.invoke(app, ["--help"]).output


def test_build_instruction_app_uses_the_entry_help():
    entry = _registered(
        InstructionEntry(name="deploy", module="m", handler=_deploy_no_opts, help="Push it.")
    )
    app = build_instruction_app(entry)
    out = runner.invoke(app, ["--help"]).output
    assert "Push it." in out
    assert "Deploy the widget." not in out  # help= takes precedence over the docstring


def test_build_instruction_app_falls_back_to_the_docstring():
    entry = _registered(InstructionEntry(name="deploy", module="m", handler=_deploy_no_opts))
    app = build_instruction_app(entry)
    assert "Deploy the widget." in runner.invoke(app, ["--help"]).output
