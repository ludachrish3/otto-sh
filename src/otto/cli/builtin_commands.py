"""First-party top-level command registrations — otto's own composition list.

The direct analog of the backend registries' ``_register_builtin_*``
functions: otto's nine subcommand groups travel the same public
:func:`~otto.cli.registry.register_cli_command` path a third-party plugin
uses, with lazy ``"module:attr"`` loaders so nothing imports until dispatch.
"""

from .registry import CLI_COMMANDS, register_cli_command


def register_builtin_commands() -> None:
    """Register otto's built-in subcommand groups (idempotent)."""
    if "run" in CLI_COMMANDS:
        return
    register_cli_command(
        "run", "otto.cli.run:run_app", help="Run a registered instruction on the lab."
    )
    register_cli_command(
        "test", "otto.cli.test:suite_app", help="Run a registered OttoSuite test suite."
    )
    register_cli_command(
        "monitor",
        "otto.cli.monitor:monitor_app",
        help="Launch an interactive performance dashboard.",
        # monitor gates itself, per-branch: historical `--file` replay reads a
        # local file and never touches live hardware, so it is gate-exempt by
        # design, while live collection still gates. A uniform gate=True here
        # would gate the replay branch too, which is a behavior regression.
        gate=False,
    )
    register_cli_command(
        "cov",
        "otto.cli.cov:cov_app",
        help="Generate coverage reports from otto test --cov output.",
        # Standard per-invocation output dirs apply to `cov get` (it produces
        # captures + debug artifacts); `report` and `clean` opt out per-leaf
        # via their `__cli_output_dir__` markers in otto/cli/cov.py.
        gate=False,
    )
    register_cli_command(
        "host", "otto.cli.host:host_app", help="Run commands and transfer files on lab hosts."
    )
    register_cli_command(
        "docker",
        "otto.cli.docker:docker_app",
        help="Build images and orchestrate compose stacks on docker-capable lab hosts.",
        gate=False,
    )
    register_cli_command(
        "reservation",
        "otto.cli.reservation:reservation_app",
        help="Inspect and verify lab reservations.",
        # Reservation queries need the backend + identity (repo settings), not
        # the lab, and never a live host: `whoami` runs with no lab at all;
        # `check` loads the lab itself — lazily, loudly — because the lab
        # defines the required-resource list.
        lab_free=True,
        output_dir=False,
        gate=False,
    )
    register_cli_command(
        "schema",
        "otto.cli.schema:schema_app",
        help="Export JSON Schema for lab.json / settings.toml / reservations.",
        lab_free=True,
        output_dir=False,
        gate=False,
    )
    register_cli_command(
        "init",
        "otto.cli.init:init_command",
        help="Scaffold a new otto repo or validate an existing one.",
        lab_free=True,
        output_dir=False,
        gate=False,
    )
