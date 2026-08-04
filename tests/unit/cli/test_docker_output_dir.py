"""The docker no-output-dir set covers read-only subcommands only."""

from otto.cli.docker import _NO_OUTPUT_DIR_SUBCOMMANDS


def test_ps_is_no_output_dir() -> None:
    assert "ps" in _NO_OUTPUT_DIR_SUBCOMMANDS


def test_mutating_subcommands_create_dir() -> None:
    for sub in ("build", "up", "down"):
        assert sub not in _NO_OUTPUT_DIR_SUBCOMMANDS


def test_marker_survives_resolution_to_the_command_callback() -> None:
    """The set must reach the RESOLVED subcommands' callbacks through typer's shim.

    Same pin as ``test_verb_output_dir.py``'s synthesis leg: since the
    ``@async_typer_command`` strip, ``__cli_output_dir__`` travels through
    typer's own callback shim alone — a typer upgrade that stops
    ``update_wrapper``-ing registered callbacks would silently give ``ps``
    an output dir, and only this test would notice.
    """
    import typer

    from otto.cli.docker import docker_app

    grp = typer.main.get_group(docker_app)
    for sub in ("build", "up", "down", "ps"):
        expected = sub not in _NO_OUTPUT_DIR_SUBCOMMANDS
        assert getattr(grp.commands[sub].callback, "__cli_output_dir__", True) is expected, sub
