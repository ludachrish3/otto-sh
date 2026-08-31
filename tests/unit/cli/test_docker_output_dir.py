"""The docker no-output-dir set covers read-only subcommands only."""

from otto.cli.docker import _DRY_RUN_PREVIEW_SUBCOMMANDS, _NO_OUTPUT_DIR_SUBCOMMANDS


def test_read_only_subcommands_are_no_output_dir() -> None:
    """`ps` and `use-cases` both only READ — neither produces an artifact.

    `use-cases` is pure configuration (selection, placement, env keys), so it
    contacts nothing and writes nothing; giving it a per-invocation output dir
    would litter one per tab-complete-shaped inventory query.
    """
    assert set(_NO_OUTPUT_DIR_SUBCOMMANDS) == {"ps", "use-cases"}


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
    for sub in ("build", "up", "down", "ps", "use-cases"):
        expected = sub not in _NO_OUTPUT_DIR_SUBCOMMANDS
        assert getattr(grp.commands[sub].callback, "__cli_output_dir__", True) is expected, sub


def test_only_the_deploying_verbs_own_their_dry_run_preview() -> None:
    """`up`/`down` opt out of the seam default; the other three do not.

    The seam (``otto.cli.invoke.stop_at_dry_run_seam``) exits 0 with a generic
    block BEFORE a leaf body unless the leaf stamps itself. `up`/`down` must
    reach `deploy`/`teardown` so the resolved plan — spec §12's exact compose
    command — is what a dry run prints. `build` and `ps` keep the safe default
    (they have no preview of their own to render), and `use-cases` never needed
    one: it is read-only and runs identically either way.
    """
    assert set(_DRY_RUN_PREVIEW_SUBCOMMANDS) == {"up", "down"}


def test_the_preview_marker_survives_resolution_to_the_command_callback() -> None:
    """Same shim pin as the output-dir marker, for the other per-leaf flag.

    ``_leaf_declares_preview`` reads ``__cli_dry_run_preview__`` off the
    RESOLVED command's callback. A typer upgrade that stopped
    ``update_wrapper``-ing registered callbacks would silently seam-stop
    ``otto docker up -n`` and delete the shipped preview, exit 0, with nothing
    else noticing.
    """
    import typer

    from otto.cli.docker import docker_app
    from otto.cli.invoke import DRY_RUN_PREVIEW_ATTR

    grp = typer.main.get_group(docker_app)
    for sub in ("build", "up", "down", "ps", "use-cases"):
        expected = sub in _DRY_RUN_PREVIEW_SUBCOMMANDS
        assert getattr(grp.commands[sub].callback, DRY_RUN_PREVIEW_ATTR, False) is expected, sub
