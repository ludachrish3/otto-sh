"""
The docker CLI's answers, each read in one place.

Every caller that asks a docker daemon something through a host's shell — the
``docker_image`` product kind, the image builder and the coverage reader —
reads the answer here, so one fix reaches all of them: what a failed command
said (:func:`said`), whether it said a resource does not exist
(:func:`missing`), and the remove-by-name command (:func:`rm_container_command`).
"""

import shlex
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from ..result import Result

DockerResource = Literal["container"]
"""A docker object :func:`missing` can be asked about."""


def said(result: "Result") -> str:
    """Return what a failed host command reported, its message first."""
    value = result.value
    text = result.msg or (value.strip() if isinstance(value, str) else "")
    return text or f"status {result.status.name}"


def missing(result: "Result", resource: DockerResource) -> bool:
    """Return whether a failed docker command said *resource* does not exist.

    Docker spells it ``No such <resource>`` in the daemon's answer and ``no such
    <resource>`` in some clients', in the message or the output, so the match is
    case-insensitive over both. A declined command (dry run) said nothing: it is
    never missing, and its ``.value`` (which raises) is never read.
    """
    from ..utils import Status  # function-local: keeps this module's import light

    if result.status is Status.NotRun:
        return False
    value = result.value
    text = f"{result.msg} {value if isinstance(value, str) else ''}"
    return f"no such {resource}" in text.lower()


def rm_container_command(name: str, *, force: bool) -> str:
    """Return the one ``docker rm`` of the container *name* (quoted here) and its anonymous volumes.

    Without *force* docker refuses a running, paused or restarting container;
    with it, the container is stopped (SIGKILL) first. ``-v`` removes the
    container's anonymous volumes only, never a named one.
    """
    flags = "-f -v" if force else "-v"
    return f"docker rm {flags} {shlex.quote(name)}"
