"""The image verbs that change a daemon: ``docker image rm`` and ``docker image prune``.

Each resolves otto's one part -- which hosts: the one *parent* names, or
every docker-capable host of the lab -- builds docker's own command from
docker's own flags, runs it on each host, and returns docker's text whole
in an :class:`~otto.docker.observe.ObserveReport`. A failing host does not
stop the others. Their listing sibling, :func:`~otto.docker.observe.list_images`
(``docker image ls``), lives with the read-only verbs.
"""

import shlex
from collections.abc import Sequence

from .observe import DockerVerbError, ObserveReport, decline_dry_run, docker_parents, run_on


def image_rm_command(
    images: "Sequence[str]", *, force: bool = False, no_prune: bool = False
) -> str:
    """``docker image rm [-f] [--no-prune] -- IMAGE...``, every image name shell-quoted.

    The ``--`` ends docker's flags, so an image whose name starts with ``-``
    (``otto docker image rm -- -f``) is removed as named, never read as a flag.
    """
    flags = (" -f" if force else "") + (" --no-prune" if no_prune else "")
    names = "".join(f" {shlex.quote(image)}" for image in images)
    return f"docker image rm{flags} --{names}"


def image_prune_command(
    *,
    all: bool = False,  # noqa: A002 -- docker's flag name
    filters: "Sequence[str]" = (),
) -> str:
    """``docker image prune [-a] -f [--filter F]...``, every filter shell-quoted.

    Always ``-f``: without it docker asks for confirmation on the parent's
    terminal, which no one is at.
    """
    flags = " -a" if all else ""
    filter_flags = "".join(f" --filter {shlex.quote(f)}" for f in filters)
    return f"docker image prune{flags} -f{filter_flags}"


async def remove_images(
    images: "Sequence[str]",
    *,
    force: bool = False,
    no_prune: bool = False,
    parent: "str | None" = None,
) -> ObserveReport:
    """``docker image rm`` *images* on every docker-capable host, or the one *parent* names.

    Each name goes to docker verbatim (a reference, a short or a full id);
    an image a host does not hold is that host's docker error, and the
    other hosts still run. docker never asks before ``image rm``, so *force*
    is docker's own ``-f`` (remove an image a container still uses, or
    untag a reference several images share), passed through unchanged.

    Args:
        images: The images to remove; at least one.
        force: docker's ``-f``.
        no_prune: docker's ``--no-prune``: keep the untagged parent layers.
        parent: The docker-capable host to run on, by id; ``None`` is every
            docker-capable host of the lab, in lab order.

    Returns:
        One :class:`~otto.docker.observe.HostOutput` per host, docker's text whole.

    Raises:
        DockerVerbError: *images* is empty (field ``images``); *parent* is not
            a docker-capable unix host of the active lab, or the lab has none
            (field ``parent``).
        ~otto.result.CommandNotRunError: this is a dry run; the message names
            the exact command on every host, and nothing was removed.
    """
    from ..invocation import require_run

    if not images:
        raise DockerVerbError("name at least one image to remove", field="images")
    parents = docker_parents(require_run().lab, parent)
    command = image_rm_command(images, force=force, no_prune=no_prune)
    decline_dry_run("remove_images", parents, command, "No image was removed.")
    return await run_on(parents, command, asked="remove_images")


async def prune_images(
    *,
    all: bool = False,  # noqa: A002 -- docker's flag name
    force: bool = False,
    filters: "Sequence[str]" = (),
    parent: "str | None" = None,
) -> ObserveReport:
    """``docker image prune -f`` on every docker-capable host, or the one *parent* names.

    docker asks for confirmation before a prune unless given ``-f``, and otto
    never prompts, so *force* must be True: it is the caller saying yes, in
    docker's own word. Without it nothing is resolved and nothing runs.

    Args:
        all: docker's ``-a``: every image no container uses, not only the
            dangling ones.
        force: docker's ``-f``; required.
        filters: docker's ``--filter`` values (``until=24h``,
            ``label=key=value``, ``label!=key``), each passed to docker
            verbatim; docker judges them.
        parent: The docker-capable host to run on, by id; ``None`` is every
            docker-capable host of the lab, in lab order.

    Returns:
        One :class:`~otto.docker.observe.HostOutput` per host, docker's text whole.

    Raises:
        DockerVerbError: *force* is False (field ``force``); *parent* is not a
            docker-capable unix host of the active lab, or the lab has none
            (field ``parent``).
        ~otto.result.CommandNotRunError: this is a dry run; the message names
            the exact command on every host, and nothing was removed.
    """
    from ..invocation import require_run

    if not force:
        raise DockerVerbError(
            "image prune needs -f: docker would ask for confirmation, and otto never prompts",
            field="force",
        )
    parents = docker_parents(require_run().lab, parent)
    command = image_prune_command(all=all, filters=filters)
    decline_dry_run("prune_images", parents, command, "No image was removed.")
    return await run_on(parents, command, asked="prune_images")
