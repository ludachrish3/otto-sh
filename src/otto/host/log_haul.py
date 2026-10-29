"""One debug-glob haul, shared by hosts and products.

A literal entry is fetched as declared; an entry with a glob metacharacter
needs the host's ``glob`` and fails LOUD without it, because a silently
skipped log set looks exactly like a host that had no logs.

A literal entry that is not there is nothing to collect, the same as a glob
with no matches: a product's uninstall commonly removes its own log file, and
the next haul (a second cleanup) must not fail over it.
"""

import logging
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from ..result import Result
from ..utils import Status

logger = logging.getLogger(__name__)


async def haul_globs(host: Any, globs: Sequence[str], dest: Path, *, who: str) -> Result:
    """Fetch every *globs* match on *host* into *dest*.

    *who* names the owner of the glob list in the failure message
    (``"host h1"`` or ``"product 'app'"``). Zero matches is success without a
    transfer: several transfer backends report a no-file transfer as failure.

    A literal entry costs no round trip of its own: only when the fetch fails
    does the haul ask the host which literals are missing. Those are noted at
    INFO and dropped, and the rest are fetched again; a fetch that failed with
    every path present is reported. A host with no ``exists`` reports the failed
    fetch as it is.

    Only ``test -e`` exiting 1 counts as missing. A probe that could not answer
    (the session died, retcode -1) proves nothing, so the failed fetch stands:
    a host that went away must fail the haul, not pass it as "no logs".
    """
    paths: list[Path] = []
    literals: list[Path] = []
    for entry in globs:
        if any(ch in entry for ch in "*?["):
            glob = getattr(host, "glob", None)
            if glob is None:
                return Result(
                    Status.Error,
                    msg=(
                        f"{who}: debug_log_globs entry {entry!r} is a glob pattern, but "
                        f"{type(host).__name__} has no glob support — declare concrete "
                        "paths or override get_debug_logs."
                    ),
                )
            paths.extend(Path(p) for p in await glob(entry))
        else:
            paths.append(Path(entry))
            literals.append(Path(entry))
    if not paths:
        return Result(Status.Success)
    fetched = await host.get(paths, dest)
    exists = getattr(host, "exists", None)
    if fetched.is_ok or fetched.status is Status.NotRun or exists is None or not literals:
        return fetched
    missing = [path for path in literals if await _absent(host, path)]
    if not missing:
        return fetched
    for path in missing:
        logger.info(f"{who}: {path} is not there; nothing to collect")
    remaining = [path for path in paths if path not in missing]
    if not remaining:
        return Result(Status.Success)
    return await host.get(remaining, dest)


async def _absent(host: Any, path: Path) -> bool:
    """Return True only when ``test -e`` exited 1, never when the probe failed to run."""
    # Function-local: every host imports this module, and only a haul probes.
    from .shell_quote import path_operand

    result = await host.exec(f"test -e {path_operand(path)}")
    return result.retcode == 1
