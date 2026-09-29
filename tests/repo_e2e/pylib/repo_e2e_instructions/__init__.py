from otto import register_options

from . import plugin_commands  # import triggers @cli_command/register_cli_command
from .noop import noop

register_options("repo_e2e_instructions.options:E2EFixtureOptions", verbs=["test"])
