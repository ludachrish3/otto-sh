"""
The docker CLI's answers, each read in one place.

Every caller that asks a docker daemon something through a host's shell — the
``docker_image`` product kind, the image builder, the ``otto docker`` image
verbs and the coverage reader —
reads the answer here, so one fix reaches all of them: what a failed command
said (:func:`said`), whether it said a resource does not exist
(:func:`missing`), the remove commands (:func:`rm_container_command`,
:func:`image_rm_command`), an image's id (:func:`image_id_of`), a container's state
(:func:`inspect_container`), the parent's clock (:func:`parent_clock`) and the
coverage scan's leftovers (:func:`scan_labels`, :func:`sweep_stale_scans`).
"""

import enum
import json
import logging
import shlex
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from collections.abc import Sequence

    from ..result import CommandResult, Result
    from .host import Host

logger = logging.getLogger(__name__)

DockerResource = Literal["container", "image"]
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


def image_rm_command(
    images: "Sequence[str]", *, force: bool = False, no_prune: bool = False
) -> str:
    """``docker image rm [-f] [--no-prune] -- IMAGE...``, every image name shell-quoted.

    The one image removal: ``otto docker image rm``, a product's uninstall, the
    coverage scan's cleanup and the stale-scan sweep all render it here. The
    ``--`` ends docker's flags, so an image whose name starts with ``-``
    (``otto docker image rm -- -f``) is removed as named, never read as a flag.
    """
    flags = (" -f" if force else "") + (" --no-prune" if no_prune else "")
    names = "".join(f" {shlex.quote(image)}" for image in images)
    return f"docker image rm{flags} --{names}"


IMAGE_ID_FORMAT = "{{.Id}}"
"""The ``docker image inspect`` format of an image's id, as the daemon prints it (``sha256:…``)."""


@dataclass(frozen=True)
class ImageId:
    """What the daemon said an image reference's id is."""

    id: str | None
    """The id line, or ``None`` when the inspect declined, failed or printed nothing."""

    result: "CommandResult"
    """The inspect's own answer, whole: a failure's text is the caller's to report."""


async def image_id_of(host: "Host", reference: str, *, timeout: float = 60) -> ImageId:
    """Return the id of the image *reference* names on *host*'s docker daemon."""
    from ..utils import Status  # function-local: keeps this module's import light

    result = await host.exec(
        f"docker image inspect --format {shlex.quote(IMAGE_ID_FORMAT)} {shlex.quote(reference)}",
        timeout=timeout,
    )
    if result.status is Status.NotRun or not result.status.is_ok:
        return ImageId(None, result)
    return ImageId(str(result.value).strip() or None, result)


_INSPECT_TEMPLATE = (
    '{"status":{{json .State.Status}},"running":{{json .State.Running}},'
    '"image":{{json .Image}},"ref":{{json .Config.Image}},'
    '"user":{{json .Config.User}},"groups":{{json .HostConfig.GroupAdd}}}'
)
"""Exactly the six fields otto reads. Never ``{{json .}}``: that prints the container's whole
config, its environment (secrets) included, into the command log."""


@dataclass(frozen=True)
class ContainerState:
    """The parts of one ``docker container inspect`` otto decides on."""

    status: str
    """``.State.Status``: ``created``, ``running``, ``paused``, ``restarting``, ``exited``,
    ``removing`` or ``dead``."""

    running: bool
    """``.State.Running``."""

    image_id: str
    """``.Image``: the immutable id of the image the container was created from."""

    image_ref: str
    """``.Config.Image``: the reference the container was created with."""

    user: str
    """``.Config.User``; empty means the image's own user."""

    group_add: list[str]
    """``.HostConfig.GroupAdd``; a ``null`` is no groups."""


@dataclass(frozen=True)
class ContainerInspection:
    """One container inspect: its answer, and the state it parsed to."""

    result: "CommandResult"
    state: ContainerState | None
    """Set only when the inspect succeeded and its output is the template's."""

    @property
    def declined(self) -> bool:
        """Whether the inspect was declined (dry run)."""
        from ..utils import Status

        return self.result.status is Status.NotRun

    @property
    def absent(self) -> bool:
        """Whether docker said no container of the name exists."""
        return missing(self.result, "container")


def _parse_state(text: str) -> ContainerState | None:
    """Return the state the template printed, or ``None`` for anything else."""
    try:
        data = json.loads(text)
        groups = data["groups"]
        if groups is None:
            groups = []
        fields = (data["status"], data["image"], data["ref"], data["user"])
        if (
            not all(isinstance(f, str) for f in fields)
            or not isinstance(data["running"], bool)
            or not isinstance(groups, list)
            or not all(isinstance(g, str) for g in groups)
        ):
            return None
        return ContainerState(
            status=data["status"],
            running=data["running"],
            image_id=data["image"],
            image_ref=data["ref"],
            user=data["user"],
            group_add=list(groups),
        )
    except (KeyError, TypeError, ValueError):
        return None


async def inspect_container(host: "Host", name: str, *, timeout: float = 60) -> ContainerInspection:
    """Inspect the container *name* on *host*'s docker daemon, once, for every caller."""
    from ..utils import Status  # function-local: keeps this module's import light

    result = await host.exec(
        f"docker container inspect --format {shlex.quote(_INSPECT_TEMPLATE)} {shlex.quote(name)}",
        timeout=timeout,
    )
    if result.status is Status.NotRun or not result.status.is_ok:
        return ContainerInspection(result, None)
    return ContainerInspection(result, _parse_state(str(result.value)))


# ── the coverage scan's leftovers ─────────────────────────────────────────────

SCAN_LABEL = "otto.scan"
"""Every container and image a coverage scan creates carries ``otto.scan=1``."""

SCAN_EXPIRES_LABEL = "otto.scan.expires"
"""…and ``otto.scan.expires=<epoch>``, on the docker parent's clock: when a sweep may remove it."""

_CLOCK_COMMAND = "date +%s"


def scan_labels(expires: int) -> dict[str, str]:
    """Return the labels of a scan resource that may be swept from the parent time *expires*."""
    return {SCAN_LABEL: "1", SCAN_EXPIRES_LABEL: str(expires)}


@dataclass(frozen=True)
class ParentClock:
    """The docker parent's clock, read once."""

    now: int | None
    """Epoch seconds, or ``None`` when the read declined, failed or printed no integer."""

    result: "CommandResult"


async def parent_clock(host: "Host") -> ParentClock:
    """Read the clock of *host*, the host that runs docker (``date +%s``).

    Scan expiries are stamped and compared on this one clock, so runner clocks
    never enter and two runners sharing a daemon agree.
    """
    from ..utils import Status

    result = await host.exec(_CLOCK_COMMAND, timeout=60)
    if result.status is Status.NotRun or not result.status.is_ok:
        return ParentClock(None, result)
    text = str(result.value).strip()
    return ParentClock(int(text) if text.isdigit() else None, result)


@dataclass(frozen=True)
class SweepReport:
    """What one :func:`sweep_stale_scans` removed, and what it could not do."""

    removed: list[str]
    """``"container <id>"`` / ``"image <id>"``, in removal order."""

    errors: list[str]
    """Each failed command, named, with what it said."""


class _Outcome(enum.Enum):
    OK = "ok"
    GONE = "gone"
    FAILED = "failed"


@dataclass(frozen=True)
class _Answer:
    outcome: _Outcome
    result: "CommandResult | None"


class _SweepDeclinedError(Exception):
    """A sweep command was declined (dry run): the sweep stops, and that is no failure."""


async def _ask(
    host: "Host",
    cmd: str,
    errors: list[str],
    *,
    tolerate: DockerResource | None = None,
) -> _Answer:
    """Run one sweep command; a failure or a raise is an error, never an exception.

    A failure that says *tolerate* no longer exists is ``GONE``: a concurrent
    sweep removed it first. ``asyncio.CancelledError`` is a ``BaseException`` and
    propagates.
    """
    from ..utils import Status

    try:
        result = await host.exec(cmd, timeout=60)
    except Exception as e:  # noqa: BLE001 — a transport or host-side failure is this command's error
        errors.append(f"{cmd}: {type(e).__name__}: {e}")
        return _Answer(_Outcome.FAILED, None)
    if result.status is Status.NotRun:
        raise _SweepDeclinedError(cmd)
    if result.status.is_ok:
        return _Answer(_Outcome.OK, result)
    if tolerate is not None and missing(result, tolerate):
        return _Answer(_Outcome.GONE, result)
    errors.append(f"{cmd}: {said(result)}")
    return _Answer(_Outcome.FAILED, None)


def _expired(listing: str, kind: str, now: int) -> list[str]:
    """Return the ids in an ``<id> <expires>`` *listing* whose expiry is at or before *now*.

    An expiry that is not an integer is never removed: the sweep removes only
    what it can prove expired.
    """
    ids = []
    for line in listing.splitlines():
        rid, _, expires = line.strip().partition(" ")
        if not rid:
            continue
        expires = expires.strip()
        if not expires.isdigit():
            logger.warning(
                "scan %s %s: %s %r is not an epoch; left alone",
                kind,
                rid,
                SCAN_EXPIRES_LABEL,
                expires,
            )
            continue
        if int(expires) <= now:
            ids.append(rid)
    return ids


async def _sweep(host: "Host", now: int | None, removed: list[str], errors: list[str]) -> None:
    from ..utils import Status

    if now is None:
        try:
            clock = await parent_clock(host)
        except Exception as e:  # noqa: BLE001 — no clock is this sweep's error, not a raise
            errors.append(f"{_CLOCK_COMMAND}: {type(e).__name__}: {e}")
            return
        if clock.now is None:
            if clock.result.status is Status.NotRun:
                raise _SweepDeclinedError(_CLOCK_COMMAND)
            errors.append(f"{_CLOCK_COMMAND}: {said(clock.result)}")
            return
        now = clock.now
    label = shlex.quote(f"label={SCAN_LABEL}=1")
    containers: list[str] = []
    listed = await _ask(
        host,
        f"docker ps -a --filter {label} --format "
        + shlex.quote(f'{{{{.ID}}}} {{{{.Label "{SCAN_EXPIRES_LABEL}"}}}}'),
        errors,
    )
    if listed.result is not None:
        containers = _expired(str(listed.result.value), "container", now)
    images: list[str] = []
    listed = await _ask(host, f"docker image ls -q --no-trunc --filter {label}", errors)
    ids = str(listed.result.value).split() if listed.result is not None else []
    if ids:
        fmt = shlex.quote(f'{{{{.Id}}}} {{{{index .Config.Labels "{SCAN_EXPIRES_LABEL}"}}}}')
        quoted = " ".join(shlex.quote(i) for i in ids)
        inspected = await _ask(host, f"docker image inspect --format {fmt} {quoted}", errors)
        if inspected.result is not None:
            images = _expired(str(inspected.result.value), "image", now)
    for cid in containers:
        cmd = rm_container_command(cid, force=True)
        if (await _ask(host, cmd, errors, tolerate="container")).outcome is _Outcome.OK:
            removed.append(f"container {cid}")
    for iid in images:
        cmd = image_rm_command([iid])
        if (await _ask(host, cmd, errors, tolerate="image")).outcome is _Outcome.OK:
            removed.append(f"image {iid}")


async def sweep_stale_scans(host: "Host", *, now: int | None = None) -> SweepReport:
    """Remove every expired coverage-scan container, then image, on the docker parent *host*.

    The one sweep, shared by the coverage reader (which passes the clock it
    already read as *now*) and ``make bed-reap``. Only resources labelled
    ``otto.scan=1`` whose ``otto.scan.expires`` is at or before the parent's
    clock are removed, so a live scan's resources are never touched. It never
    raises except for cancellation: every failed or raised command is an entry
    in :attr:`SweepReport.errors` and the sweep goes on; a declined one (dry
    run) stops it quietly.
    """
    removed: list[str] = []
    errors: list[str] = []
    try:
        await _sweep(host, now, removed, errors)
    except _SweepDeclinedError:
        return SweepReport(removed, [])
    return SweepReport(removed, errors)
