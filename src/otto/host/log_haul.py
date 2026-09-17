"""One debug-glob haul, shared by hosts and products.

A literal entry is fetched as declared; an entry with a glob metacharacter
needs the host's ``glob`` and fails LOUD without it, because a silently
skipped log set looks exactly like a host that had no logs.
"""

from collections.abc import Sequence
from pathlib import Path
from typing import Any

from ..result import Result
from ..utils import Status


async def haul_globs(host: Any, globs: Sequence[str], dest: Path, *, who: str) -> Result:
    """Fetch every *globs* match on *host* into *dest*.

    *who* names the owner of the glob list in the failure message
    (``"host h1"`` or ``"product 'app'"``). Zero matches is success without a
    transfer: several transfer backends report a no-file transfer as failure.
    """
    paths: list[Path] = []
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
    if not paths:
        return Result(Status.Success)
    return await host.get(paths, dest)
