"""First-party top-level command registrations — otto's own composition list.

The direct analog of the backend registries' ``_register_builtin_*``
functions: otto's ten subcommand groups travel the same public
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
        # monitor gates AND loads its lab itself, per-branch: reviewing a saved
        # `<source>` export reads a local file and never touches live hardware
        # or a lab, so it is both gate-exempt and lab-free by design, while
        # `--live` collection still gates and still requires a lab (it pulls
        # one in itself via otto.cli.invoke.ensure_lab_session, the same loud
        # way `otto reservation check` does). Uniform gate=True/lab_free=False
        # here would gate/lab-require the review branch too, which is a
        # behavior regression — and was, in fact, exactly the bug this
        # lab_free=True fixed (review mode hard-required --lab despite never
        # touching a lab).
        gate=False,
        lab_free=True,
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
        "tunnel",
        "otto.cli.tunnel:tunnel_app",
        help="Create, list, and remove host-resident bidirectional tunnels.",
        # Short-lived like reservation: discovery/teardown touch hosts (and are
        # reservation-gated, like host/run/test) but the group needs no
        # per-invocation output directory of its own.
        output_dir=False,
    )
    register_cli_command(
        "link",
        "otto.cli.link:link_app",
        help="Inspect and impair the lab's static links.",
        # Short-lived host-touching group like tunnel: no per-invocation
        # output directory of its own.
        output_dir=False,
    )
    register_cli_command(
        "init",
        "otto.cli.init:init_command",
        help="Scaffold a new otto repo or validate an existing one.",
        lab_free=True,
        output_dir=False,
        gate=False,
    )
