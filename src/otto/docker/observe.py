"""The read-only docker questions: what a host's daemon prints, verbatim.

Each function here resolves otto's part -- which parent, which compose
project -- runs one docker command per host, and returns
docker's text whole. Nothing is parsed, shortened or re-columned: a person
who knows docker sees docker.
"""

import shlex
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..errors import FieldError
from ..host.errors import HostUnreachableError
from ..host.host import refuse_declined_fact
from ..host.unix_host import UnixHost
from ..logger.mode import LogMode
from ..result import CommandResult
from ..utils import Status
from .compose import use_case_project

if TYPE_CHECKING:
    from ..config.lab import Lab


class DockerVerbError(FieldError, ValueError):
    """A docker verb's input is unusable; nothing was touched.

    ``field`` names the offending parameter (``parent``, ``repo``, ``images``,
    ``tag``, ``container``, ``follow``) or is ``None`` for a refusal about the
    selection as a whole. The CLI spells the field in the verb's flags at
    its one translation site.
    """


def _capable(lab: "Lab") -> "Iterator[tuple[str, UnixHost]]":
    """Yield ``(id, host)`` for each docker-capable unix host of *lab*, in lab order."""
    for hid, host in lab.hosts.items():
        if isinstance(host, UnixHost) and host.docker_capable:
            yield hid, host


def capable_ids(lab: "Lab") -> "list[str]":
    """Return the docker-capable unix host ids of *lab*, sorted."""
    return sorted(hid for hid, _ in _capable(lab))


def default_docker_parent(lab: "Lab") -> UnixHost:
    """Return the parent a docker verb uses when none is named: the one rule.

    Exactly one docker-capable unix host in *lab* is it. Several: the one with
    the strictly highest ``docker_priority``. A tie at the top (every host at
    the default 0 included) refuses and names the tied hosts: otto never picks
    a parent by file order. No docker-capable host refuses too.
    """
    capable = [host for _, host in _capable(lab)]
    if not capable:
        raise DockerVerbError(f"lab {lab.name!r} has no docker-capable unix host", field="parent")
    if len(capable) == 1:
        return capable[0]
    top = max(h.docker_priority for h in capable)
    ranked = sorted((h for h in capable if h.docker_priority == top), key=lambda h: h.id)
    if len(ranked) == 1:
        return ranked[0]
    names = ", ".join(h.id for h in ranked)
    raise DockerVerbError(
        f"lab {lab.name!r} has {len(ranked)} docker-capable hosts at priority {top} "
        f"({names}) — name one with --parent, or rank one higher with "
        f'"docker_priority" in lab.json',
        field="parent",
    )


def docker_parent(lab: "Lab", parent: "str | None") -> UnixHost:
    """Return the docker-capable unix host *parent* names in *lab*, or the default (one rule)."""
    if parent is None:
        return default_docker_parent(lab)
    candidate = lab.hosts.get(parent)
    if not isinstance(candidate, UnixHost) or not candidate.docker_capable:
        raise DockerVerbError(
            f"{parent!r} is not a docker-capable unix host in lab {lab.name!r}; "
            f"docker-capable hosts here: {capable_ids(lab)}",
            field="parent",
        )
    return candidate


def docker_parents(lab: "Lab", parent: "str | None") -> "list[UnixHost]":
    """Every docker-capable unix host of *lab* in lab order, or the one *parent* names.

    The ``--parent`` rule of the fan-out verbs, in one place: ``None`` is the
    whole fleet, a name is checked the way the build verbs check theirs. A
    lab with no docker-capable host is refused, not answered with silence.
    """
    if parent is not None:
        return [docker_parent(lab, parent)]
    parents = [host for _, host in _capable(lab)]
    if not parents:
        raise DockerVerbError(f"lab {lab.name!r} has no docker-capable unix host", field="parent")
    return parents


@dataclass
class HostOutput:
    """What one host's daemon printed for one command."""

    host_id: str
    command: str
    """The exact docker command run on the host."""
    result: CommandResult
    """Docker's output verbatim in ``value``, and whether docker succeeded."""


@dataclass
class ObserveReport:
    """One :class:`HostOutput` per host asked, in the order they were asked."""

    hosts: "list[HostOutput]"

    @property
    def ok(self) -> bool:
        """Every host's command succeeded."""
        return all(h.result.is_ok for h in self.hosts)


@dataclass(frozen=True)
class LogsTarget:
    """One resolved docker command and the host it runs on."""

    parent: UnixHost
    command: str
    """The whole command, as ``exec`` runs it."""
    follow_command: "str | None" = None
    """The same command with docker's ``-f``, for ``follow_logs``; ``None`` when
    the verb has no live form (a listing)."""


def _logs_target(parent: UnixHost, prefix: str, flags: str, names: str) -> LogsTarget:
    """``<prefix> logs<flags><names>`` and its live form, ``-f`` right after the verb."""
    return LogsTarget(parent, f"{prefix} logs{flags}{names}", f"{prefix} logs -f{flags}{names}")


async def _exec_targets(targets: "list[LogsTarget]", *, asked: str) -> ObserveReport:
    """Run each target's command on its host, quietly, keeping each answer whole.

    A host whose transport fails is reported in otto's words (never docker's)
    and the others still run; a dry run's decline is not caught here.
    """
    outputs: "list[HostOutput]" = []
    for target in targets:
        try:
            result = await target.parent.exec(target.command, log=LogMode.QUIET)
        except (OSError, ConnectionError, HostUnreachableError) as e:
            result = CommandResult(
                Status.Failed,
                value=f"otto: host {target.parent.id!r} unreachable: {e!r}",
                command=target.command,
                retcode=-1,
            )
            outputs.append(
                HostOutput(host_id=target.parent.id, command=target.command, result=result)
            )
            continue
        refuse_declined_fact(result, asked=f"{asked}({target.parent.id})")
        outputs.append(HostOutput(host_id=target.parent.id, command=target.command, result=result))
    return ObserveReport(hosts=outputs)


async def run_on(parents: "list[UnixHost]", command: str, *, asked: str) -> ObserveReport:
    """Run *command* on every parent and keep each answer whole.

    A failing host does not stop the others: the report carries docker's
    error for it (or otto's, for a host that could not be reached) and ``ok``
    is False. A dry run's declined answer is refused
    (``refuse_declined_fact``) rather than reported as empty output -- an
    empty listing is a fact about a daemon, and none was measured.

    Each command runs ``LogMode.QUIET``: the report is what the caller prints,
    and the console relaying docker's lines as they arrive would show every
    listing twice.
    """
    return await _exec_targets([LogsTarget(parent, command) for parent in parents], asked=asked)


async def list_containers(parent: "str | None" = None, *, all: bool = False) -> ObserveReport:  # noqa: A002 -- docker's flag name
    """``docker ps [-a]`` on every docker-capable host, or the one *parent* names."""
    from ..config.fleet import get_lab

    command = "docker ps -a" if all else "docker ps"
    return await run_on(docker_parents(get_lab(), parent), command, asked="list_containers")


async def list_images(parent: "str | None" = None) -> ObserveReport:
    """``docker images`` on every docker-capable host, or the one *parent* names."""
    from ..config.fleet import get_lab

    return await run_on(docker_parents(get_lab(), parent), "docker images", asked="list_images")


IMAGES_PROBE = r"docker images --format '{{.Repository}}:{{.Tag}}\t{{.ID}}'"
"""The images question in a shape made for a program, one ``ref<TAB>id`` per line.

The tab is docker's own two-character ``\\t`` escape, never a literal tab: a
telnet, proxied or console parent types the command into a PTY shell whose
line editor would take a literal tab for completion and drop it.
"""
CONTAINERS_PROBE = r"docker ps -a --format '{{.Names}}\t{{.ID}}'"
"""The containers question, every container (a stopped one's logs are still docker's to print)."""


@dataclass(frozen=True)
class ObservedImages:
    """Image references and ids a daemon listed, in its order.

    *answered* separates "the daemon said there are none" (``True``, empty
    lists: a fact) from "the probe failed or was declined, or its answer had no
    row otto could read" (``False``: no fact).
    """

    refs: list[str]
    ids: list[str]
    answered: bool


@dataclass(frozen=True)
class ObservedContainers:
    """Container names and ids a daemon listed, in its order.

    *answered* separates "the daemon said there are none" (``True``, empty
    lists: a fact) from "the probe failed or was declined, or its answer had no
    row otto could read" (``False``: no fact).
    """

    names: list[str]
    ids: list[str]
    answered: bool


def _tab_pairs(text: str) -> "tuple[list[str], list[str]]":
    """Split ``--format`` lines into their two columns; a line without a tab is skipped."""
    firsts: "list[str]" = []
    seconds: "list[str]" = []
    for line in text.splitlines():
        first, tab, second = line.strip().partition("\t")
        if not tab or not first or not second:
            continue
        firsts.append(first)
        seconds.append(second)
    return firsts, seconds


def _answered(text: "str | None", parsed: "list[str]") -> bool:
    """Whether a probe's answer is a fact: empty (nothing there), or with a row otto read.

    A non-empty answer that parses to no row (a PTY expanding the tab to
    spaces, a ``--format`` regression) is "could not read it", never "none":
    recording it would replace good hints with empty ones for a whole TTL.
    """
    return text is not None and (not text.strip() or bool(parsed))


async def _probe(host_id: str, command: str) -> "str | None":
    """Run a probe quietly on *host_id*; ``None`` when the daemon did not answer.

    Transport and refusal errors propagate; the caller decides whether to swallow them.
    """
    from ..config.fleet import get_lab

    parent = docker_parent(get_lab(), host_id)
    result = await parent.exec(command, log=LogMode.QUIET)
    return result.value if result.status.is_ok and isinstance(result.value, str) else None


async def observed_images(host_id: str) -> ObservedImages:
    """Ask *host_id*'s daemon for its images once more, in a shape made for a program.

    A completion hint, never a fact a verb reports: a failed or declined
    probe is ``answered=False``, an empty answer is ``answered=True`` with
    empty lists, an answer with no readable row is ``answered=False``, and
    ``<none>:<none>`` (a dangling layer) is dropped because nobody types it.
    """
    text = await _probe(host_id, IMAGES_PROBE)
    refs, ids = _tab_pairs(text or "")
    kept = [(r, i) for r, i in zip(refs, ids, strict=True) if r != "<none>:<none>"]
    return ObservedImages([r for r, _ in kept], [i for _, i in kept], _answered(text, refs))


async def observed_containers(host_id: str) -> ObservedContainers:
    """Ask *host_id*'s daemon for every container once more, in a shape made for a program."""
    text = await _probe(host_id, CONTAINERS_PROBE)
    names, ids = _tab_pairs(text or "")
    return ObservedContainers(names, ids, _answered(text, names))


def logs_flags(*, tail: "str | None", since: "str | None", timestamps: bool) -> str:
    """Docker's own log flags, in docker's spelling, as a suffix (leading space) or ``""``."""
    parts: "list[str]" = []
    if tail is not None:
        parts.append(f"--tail {shlex.quote(tail)}")
    if since is not None:
        parts.append(f"--since {shlex.quote(since)}")
    if timestamps:
        parts.append("-t")
    return "".join(f" {p}" for p in parts)


@dataclass(frozen=True)
class _ComposeSite:
    """One acting host of a use-case: where a compose verb runs, and on what."""

    parent: UnixHost
    prefix: str
    """``docker compose -p <project>``: the project label is the whole input."""
    names: str
    """The services the verb names on this host, as a quoted suffix, or ``""``."""


def _compose_sites(
    use_case: str,
    services: "Sequence[str]",
    *,
    parent: "str | None",
    provide: "Mapping[str, str] | None",
    fleet: bool = False,
) -> "list[_ComposeSite]":
    """Resolve *use_case* as ``deploy`` does; one site per acting host.

    One parent by default: *parent* named, or the lab's default parent by the
    one rule (a tie refuses, field ``parent``). With *fleet* and no *parent*
    -- ``compose ps``, a listing -- every docker-capable host in lab order is
    asked instead (a tied lab, which ``deploy`` refuses, is no refusal there).
    With a services filter, each site names only the requested services its
    acting host runs: a host cannot be asked for a service it does not run.
    """
    # Function-local: the listing verbs must not import the deployment stack.
    from ..config.fleet import get_lab
    from . import deployment

    if fleet and parent is None:
        asked: "list[str | None]" = [host.id for host in docker_parents(get_lab(), None)]
    else:
        asked = [parent]
    sites: "list[_ComposeSite]" = []
    for asked_parent in asked:
        resolution = deployment.resolve_use_case(use_case, parent=asked_parent, provide=provide)
        wanted = deployment.validated_services(resolution.selection, list(services) or None)
        acting = deployment.acting_hosts(
            resolution.placed, resolution.order, wanted, use_case=use_case
        )
        for host in acting:
            parent_host = deployment.parent_for(resolution.lab, host.host_id)
            project = shlex.quote(use_case_project(parent_host.source_lab, use_case))
            names = (
                "".join(f" {shlex.quote(s)}" for s in host.services) if wanted is not None else ""
            )
            sites.append(_ComposeSite(parent_host, f"docker compose -p {project}", names))
    return sites


async def compose_ps(
    use_case: str,
    *,
    all: bool = False,  # noqa: A002 -- docker's flag name
    parent: "str | None" = None,
    provide: "Mapping[str, str] | None" = None,
) -> ObserveReport:
    """``docker compose -p <project> ps [-a]`` on *parent*, or on every docker-capable host."""
    verb = "ps -a" if all else "ps"
    targets = [
        LogsTarget(site.parent, f"{site.prefix} {verb}")
        for site in _compose_sites(use_case, (), parent=parent, provide=provide, fleet=True)
    ]
    return await _exec_targets(targets, asked="compose_ps")


def resolve_compose_logs(
    use_case: str,
    services: "Sequence[str]" = (),
    *,
    parent: "str | None" = None,
    provide: "Mapping[str, str] | None" = None,
    tail: "str | None" = None,
    since: "str | None" = None,
    timestamps: bool = False,
) -> "list[LogsTarget]":
    """Resolve the ``docker compose -p <project> logs …`` command on one parent.

    *parent* names it, ``None`` the lab's default parent by the one rule: logs
    never fans out over the fleet. An empty *services* means every service
    (docker's own meaning for ``compose logs`` with no names), unlike
    ``deploy``'s ``services=[]``. Otherwise the command names only the
    requested services.
    """
    flags = logs_flags(tail=tail, since=since, timestamps=timestamps)
    return [
        _logs_target(site.parent, site.prefix, flags, site.names)
        for site in _compose_sites(use_case, services, parent=parent, provide=provide)
    ]


async def compose_logs(
    use_case: str,
    services: "Sequence[str]" = (),
    *,
    parent: "str | None" = None,
    provide: "Mapping[str, str] | None" = None,
    tail: "str | None" = None,
    since: "str | None" = None,
    timestamps: bool = False,
) -> ObserveReport:
    """Return the use-case's compose logs on one parent, docker's text whole.

    An empty *services* means every service (docker's own meaning for
    ``compose logs`` with no names), unlike ``deploy``'s ``services=[]``.
    """
    targets = resolve_compose_logs(
        use_case,
        services,
        parent=parent,
        provide=provide,
        tail=tail,
        since=since,
        timestamps=timestamps,
    )
    return await _exec_targets(targets, asked="compose_logs")


async def resolve_logs(
    container: str,
    *,
    parent: "str | None" = None,
    tail: "str | None" = None,
    since: "str | None" = None,
    timestamps: bool = False,
    follow: bool = False,
) -> LogsTarget:
    """Name the parent and the ``docker logs`` command for *container*.

    One contract: *container* is a docker name or id, handed to ``docker logs``
    on the parent verbatim; an unknown one is docker's error, not otto's. A lab
    container host id is just a name here: otto does not look it up. *parent*
    names the host, ``None`` the default parent. With *follow*, a parent not
    reached by SSH is refused.
    """
    from ..config.fleet import get_lab

    parent_host = docker_parent(get_lab(), parent)
    if follow:
        _require_ssh_parent(parent_host)
    flags = logs_flags(tail=tail, since=since, timestamps=timestamps)
    return _logs_target(parent_host, "docker", flags, f" {shlex.quote(container)}")


async def container_logs(
    container: str,
    *,
    parent: "str | None" = None,
    tail: "str | None" = None,
    since: "str | None" = None,
    timestamps: bool = False,
) -> ObserveReport:
    """One container's logs, docker's text whole."""
    target = await resolve_logs(
        container, parent=parent, tail=tail, since=since, timestamps=timestamps
    )
    return await _exec_targets([target], asked="container_logs")


def _require_ssh_parent(parent: UnixHost) -> None:
    """Refuse a follow on a parent not reached by SSH (the bridge needs one)."""
    if parent.term != "ssh":
        raise DockerVerbError(
            f"follow needs an SSH parent; {parent.id} is reached by {parent.term}",
            field="follow",
        )


async def follow_logs(targets: "list[LogsTarget]") -> int | None:
    """Follow one resolved logs command live, on the user's terminal.

    The host layer has no streaming exec, so a follow rides the PTY bridge
    ``otto host <container> login`` uses: the command runs in the foreground
    on the parent's SSH connection, output arrives as docker writes it,
    Ctrl-C reaches docker as SIGINT and ends the follow, and the transcript lands in
    ``session.log`` like every bridged session. SSH parents only, one host
    at a time; both are refused by field before any connection is opened.

    Returns:
        docker's own exit status when it ended on its own (``1`` for a
        container docker does not know); ``128 + signal`` when it died by one
        (Ctrl-C gives ``130``, the shell convention); ``None`` only when the
        user disconnected with Ctrl+] or stdin hit EOF.

    Raises:
        DockerVerbError: more than one target, or a parent not reached by SSH.
        ~otto.result.CommandNotRunError: this is a dry run; nothing was opened.
    """
    from ..host.host import is_dry_run
    from ..host.interact import run_ssh_login
    from ..result import CommandNotRunError

    if len(targets) != 1:
        hosts = [t.parent.id for t in targets]
        where = f"covers {hosts}" if hosts else "covers no host"
        raise DockerVerbError(
            f"one terminal follows one host; this use-case {where} -- name the host "
            f"(`parent`), or drop follow to print every host's logs once",
            field="follow",
        )
    target = targets[0]
    _require_ssh_parent(target.parent)
    command = target.follow_command
    if command is None:
        raise DockerVerbError(
            f"follow needs a logs command; {target.command!r} has no live form",
            field="follow",
        )
    if is_dry_run():
        raise CommandNotRunError(f"follow_logs({target.parent.id})", target.parent.id, command)
    conn = await target.parent._live_connections().ssh()  # noqa: SLF001 -- intra-package access, as DockerContainerHost._login does
    return await run_ssh_login(conn=conn, host_name=target.parent.name, command=command)
