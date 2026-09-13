"""The first-party options classes: the flags otto's six project instructions carry.

Each is the base a repo's override MUST inherit (the table refuses an override
that does not), so the first-party flags stay on ``otto run <name>`` however
many repos add their own, and ``super()`` can read them off the subclass
instance. Field names are the flag spellings ``otto run`` has always shown.
"""

from typing import Annotated

import typer

from .. import options


@options
class InstallOptions:
    """``otto run install``'s flags."""

    ensure: Annotated[
        bool, typer.Option(help="Converge: check state first, recover a partial install.")
    ] = False
    recover_partial: Annotated[
        bool, typer.Option(help="With --ensure: uninstall a PARTIAL lab before installing fresh.")
    ] = True


@options
class UninstallOptions:
    """``otto run uninstall``'s flags."""

    product_logs: Annotated[
        bool, typer.Option(help="Haul each repo's product logs off before that repo comes down.")
    ] = True
    debug_logs: Annotated[
        bool, typer.Option(help="Sweep every host's debug logs once, after every repo is down.")
    ] = True


@options
class CleanupOptions:
    """``otto run cleanup``'s flags."""

    product_logs: Annotated[
        bool, typer.Option(help="Haul each repo's product logs off before that repo comes down.")
    ] = True
    debug_logs: Annotated[
        bool, typer.Option(help="Sweep every host's debug logs once, after every repo is down.")
    ] = True
    reset_impairments: Annotated[
        bool, typer.Option(help="Repair every lab link, clearing otto's netem impairments.")
    ] = True
    remove_tunnels: Annotated[
        bool, typer.Option(help="Reap every otto tunnel in the lab -- the very last step.")
    ] = True


@options
class GetLogsOptions:
    """``otto run get-logs``'s flags."""

    product_logs: Annotated[bool, typer.Option(help="Gather every repo's product logs.")] = True
    debug_logs: Annotated[bool, typer.Option(help="Sweep every host's debug logs once.")] = True
    require_product_logs: Annotated[
        bool, typer.Option(help="Fail when a product that declares logs surrendered none.")
    ] = False


@options
class InstallToolsOptions:
    """``otto run install-tools``'s flags."""

    dev: Annotated[bool, typer.Option(help="Install each repo's own dev tools.")] = True
    toolchain: Annotated[
        bool, typer.Option(help="Also place each host's shared toolchain tools.")
    ] = False


@options
class StatusOptions:
    """``otto run status``'s flags."""

    full: Annotated[
        bool,
        typer.Option(help="Also report cleanliness: dev tools, toolchains, impairments, tunnels."),
    ] = False
