"""First-party default instructions: thin wrappers over :mod:`otto.project`.

``otto run install`` and its five siblings are ORDINARY INSTRUCTIONS -- the
same decorator a repo uses, the same lifecycle bridge, the same generated
``--help`` -- whose entire body is a forward to
:mod:`otto.project.orchestrator`. Bootstrap imports this module before any
repo's init runs, so every lab has them.

NEVER AN OVERRIDE POINT. A repo customizes lab behavior by registering a
:class:`~otto.project.actions.ProjectActions` subclass; these wrappers, the
``ensure_*`` fixtures, and anything else that calls the orchestrator all pick
that change up for free. A repo that tries to claim one of these names is
refused at registration instead (:func:`otto.cli.run.instruction`), because two
code paths to "install the lab" is exactly the split-brain the project layer
exists to prevent.

So every body below is dispatch and nothing else -- flag names in, orchestrator
keywords out. A wrapper thick enough to have a bug of its own belongs in the
orchestrator, where the fixtures reach it too.
"""

from typing import Annotated

import typer

from ..cli.run import instruction
from ..result import Result
from ..utils import Status
from . import orchestrator
from .state import InstallState

_STATE_ANSWERS: "dict[InstallState, Result]" = {
    InstallState.INSTALLED: Result(Status.Success, value="lab is installed"),
    InstallState.UNINSTALLED: Result(Status.Failed, msg="lab is uninstalled"),
    InstallState.PARTIAL: Result(
        Status.Error,
        msg="lab is partially installed; otto run install --ensure recovers it",
    ),
}
"""``otto run status``' answer, as a value the leaf renderer can exit on.

RETURNED, never raised: ``typer.Exit`` outside ``otto.cli`` would couple the
project layer to the CLI framework (the suite runner's ``CommandResult`` is the
same call), and a returned value is also what lets a caller who imports this
instruction READ the answer instead of catching it. The three codes come from
the Result family's own mapping (:attr:`otto.result.Result.exit_code` is
``status.value`` when the result is not ok), which is why the states are
spelled as statuses here rather than as bare integers: Success exits 0, Failed
1, Error 2.

Frozen dataclasses, so sharing one instance per state is safe.
"""

_STATE_STYLES: "dict[InstallState, str]" = {
    InstallState.INSTALLED: "green",
    InstallState.UNINSTALLED: "dim",
    InstallState.PARTIAL: "yellow",
}


@instruction()
async def install(
    ensure: Annotated[
        bool, typer.Option(help="Converge: check state first, recover a partial install.")
    ] = False,
    recover_partial: Annotated[
        bool, typer.Option(help="With --ensure: uninstall a PARTIAL lab before installing fresh.")
    ] = True,
) -> Result:
    """Install every repo's products on the lab, dependencies first.

    Plain, this is fail-fast: the first repo that will not install stops the
    walk, because a dependent stacked on a dependency known to be missing
    produces a lab nobody can reason about.

    --ensure converges instead: the lab's current state is read and only the
    missing work is done, which is what the ensure_installed fixture does
    before a test session. --no-recover-partial then keeps a PARTIAL lab's
    remnants in place rather than tearing them down first.
    """
    return await orchestrator.install(ensure=ensure, recover_partial=recover_partial)


@instruction()
async def uninstall(
    product_logs: Annotated[
        bool, typer.Option(help="Haul each repo's product logs off before that repo comes down.")
    ] = True,
    debug_logs: Annotated[
        bool, typer.Option(help="Sweep every host's debug logs once, after every repo is down.")
    ] = True,
) -> Result:
    """Uninstall every repo's products, dependents first.

    Best-effort, unlike the install: every repo is attempted and the first
    failure is what is reported, because a repo that will not come down must
    not strand the ones behind it.
    """
    return await orchestrator.uninstall(get_product_logs=product_logs, get_debug_logs=debug_logs)


@instruction()
async def cleanup(
    product_logs: Annotated[
        bool, typer.Option(help="Haul each repo's product logs off before that repo comes down.")
    ] = True,
    debug_logs: Annotated[
        bool, typer.Option(help="Sweep every host's debug logs once, after every repo is down.")
    ] = True,
) -> Result:
    """Uninstall every repo, and remove its dev tools and the hosts' toolchains.

    Strictly more than uninstall: each repo also gives up its own dev tools,
    and the host-global toolchain tools come off at the very end -- one
    toolchain serves every owner on a host, so no single repo may remove it.
    """
    return await orchestrator.cleanup(get_product_logs=product_logs, get_debug_logs=debug_logs)


@instruction("get-logs")
async def get_logs(
    product_logs: Annotated[bool, typer.Option(help="Gather every repo's product logs.")] = True,
    debug_logs: Annotated[bool, typer.Option(help="Sweep every host's debug logs once.")] = True,
    require_product_logs: Annotated[
        bool, typer.Option(help="Fail when a product that declares logs surrendered none.")
    ] = False,
) -> Result:
    """Gather logs from the lab without changing it.

    Product logs are owner-scoped and hauled per repo; the debug sweep is
    host-level and happens once. --require-product-logs turns an empty haul
    into a failure, for a run whose whole purpose was the logs.
    """
    return await orchestrator.get_logs(
        product=product_logs, debug=debug_logs, require_product_logs=require_product_logs
    )


@instruction()
async def install_tools(
    dev: Annotated[bool, typer.Option(help="Install each repo's own dev tools.")] = True,
    toolchain: Annotated[
        bool, typer.Option(help="Also place each host's shared toolchain tools.")
    ] = False,
) -> Result:
    """Install the lab's tooling: each repo's dev tools, optionally the toolchains.

    The toolchain half is off by default and host-global when asked for: one
    toolchain is shared by every owner on a host, so it is placed once rather
    than per repo.
    """
    return await orchestrator.install_tools(dev=dev, toolchain=toolchain)


@instruction()
async def status() -> Result:
    """Report each repo's install state, and the lab's.

    THE EXIT CODE IS THE ANSWER, so a script branches on it without parsing
    the table: 0 fully installed, 1 fully uninstalled, 2 partial. Three codes
    rather than a boolean for the same reason InstallState has three members
    -- a half-installed lab and a clean one need different handling, and
    reporting them alike is how remnants get installed over.

    A repo with nothing to say about its install state (no products anywhere,
    no registered actions) is absent from the table rather than listed with a
    made-up state.
    """
    from rich import print as rprint
    from rich.table import Table

    report = await orchestrator.status()
    if report.repos:
        table = Table(show_header=False, box=None, padding=(0, 2, 0, 0))
        for repo_name, state in report.repos.items():
            table.add_row(repo_name, f"[{_STATE_STYLES[state]}]{state.value}[/]")
        rprint(table)
    return _STATE_ANSWERS[report.overall]
