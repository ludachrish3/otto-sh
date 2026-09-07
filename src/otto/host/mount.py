"""Directories a container shares with the host running its docker daemon.

A bind mount or named volume gives one directory two names: one inside the
container, one on the parent. otto DERIVES the pairing (from ``docker
inspect``, see :mod:`otto.docker.mounts`) rather than accepting a
declaration -- see :doc:`/guide/cli/docker/index`'s "Shared directories"
section for why, and for how callers use it.

The matching here is deliberately pure: it takes a list of
:class:`Mount` and a path and knows nothing about hosts or docker, which
is what lets it be tested exhaustively against hand-built tables.
"""

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Mount:
    """One directory shared between a container and its parent host."""

    container_path: Path
    """Where the directory appears INSIDE the container."""

    parent_path: Path
    """Absolute path on the PARENT host -- the machine running the daemon.

    For a bind this is the source directory the compose file named; for a
    named volume it is docker's own ``/var/lib/docker/volumes/<name>/_data``,
    which is real and readable on the parent but normally root-owned.
    """

    kind: str
    """``"bind"`` or ``"volume"``. Kept verbatim from docker rather than
    collapsed to a bool: a caller deciding whether to write through the
    parent side wants to know it is reaching into docker's own tree."""

    name: "str | None" = None
    """The named volume's name; ``None`` for a bind."""

    read_only: bool = False
    """Whether the CONTAINER's view is read-only. The parent side is
    unaffected -- writing a read-only mount from the parent is exactly what
    makes it useful for seeding fixture data."""


def _is_under(base: Path, path: Path) -> bool:
    """Whether *path* is *base* or lies beneath it, comparing COMPONENTS.

    Not a string prefix test: ``/var/lib/app`` must not claim
    ``/var/lib/application/x``.
    """
    return path == base or base in path.parents


def mount_for(mounts: "list[Mount]", container_path: "str | Path") -> "Mount | None":
    """Return the mount covering *container_path*, or ``None``.

    Longest prefix wins. Compose permits one mount nested inside another,
    and a first-match answer would translate through the outer mount and
    name a parent path where the file does not exist.
    """
    return _best(mounts, Path(container_path), key=lambda m: m.container_path)


def mount_for_parent(mounts: "list[Mount]", parent_path: "str | Path") -> "Mount | None":
    """Return the mount covering *parent_path* on the parent host, or ``None``."""
    return _best(mounts, Path(parent_path), key=lambda m: m.parent_path)


def _best(mounts: "list[Mount]", path: Path, *, key: "Callable[[Mount], Path]") -> "Mount | None":
    best: "Mount | None" = None
    for mount in mounts:
        base: Path = key(mount)
        if not _is_under(base, path):
            continue
        if best is None or len(base.parts) > len(key(best).parts):
            best = mount
    return best


def translate(mount: Mount, path: "str | Path", *, to_parent: bool) -> Path:
    """Re-root *path* onto the other side of *mount*.

    Raises:
        ValueError: *path* does not lie under the side it was read from.
            Callers that are merely ASKING should use :func:`mount_for`.
    """
    source, dest = (
        (mount.container_path, mount.parent_path)
        if to_parent
        else (mount.parent_path, mount.container_path)
    )
    return dest / Path(path).relative_to(source)
