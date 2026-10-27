"""Docker Compose orchestration: bring stacks up/down and register containers as live hosts.

The public surface (re-exported from :mod:`otto.docker`) is:

- :func:`compose_up` — bring a stack up; returns ``{service: DockerContainerHost}``.
- :func:`compose_down` — stop a stack and remove its container hosts from the lab.
- :func:`composed` — async context manager wrapping the above.
- :func:`get_container_host` — lab lookup by id (typed convenience).
- :func:`get_user_compose_project` — name a stack so concurrent runs don't collide.
"""

import asyncio
import contextlib
import getpass
import logging
import re
import shlex
from collections.abc import AsyncIterator, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import PurePath
from typing import TYPE_CHECKING

from ..config.project import Project
from ..config.scope import DockerCompose
from ..host.docker_host import DockerContainerHost, compose_container_probe
from ..host.errors import HostCommandError
from ..host.factory import apply_providers
from ..host.host import Host, is_dry_run, refuse_declined_fact
from ..host.mount import Mount
from ..host.unix_host import UnixHost
from ..result import CommandNotRunError, CommandResult
from ..utils import Status

if TYPE_CHECKING:
    from ..labs.loader import Lab

logger = logging.getLogger(__name__)

# Brief pause before re-running `up -d` after a transient libnetwork race so
# the daemon's freshly-created network has settled. Module-level so tests can
# zero it out.
_NETWORK_RACE_RETRY_BACKOFF_S = 1.0

# A just-Started container can briefly not appear in `docker ps` on a busy
# remote daemon, so resolving its id polls up to this many times, sleeping this
# long between attempts. Module-level so tests can shrink them. The common case
# (container already visible) returns on the first attempt with no sleep.
_CONTAINER_ID_RESOLVE_ATTEMPTS = 4
_CONTAINER_ID_RESOLVE_BACKOFF_S = 0.5


def _is_transient_network_race(output: str) -> bool:
    """Return True when ``up -d`` failed with the libnetwork "network created then not found" race.

    A convergent re-run gets past this.

    The daemon Creates the ``_default`` network and Creates the container, then
    fails at "Starting" with ``failed to set up container networking: network
    <proj>_default not found`` because the just-created network isn't yet
    visible to the container's networking setup. Re-running ``up -d`` (which is
    convergent) starts the already-created container once the network settles.
    Gated on this specific signature so genuine compose failures (bad file,
    pull denied, port clash) are *not* retried and propagate immediately.
    """
    low = output.lower()
    return "failed to set up container networking" in low and "not found" in low


async def _run_compose(parent: Host, command: str) -> CommandResult:
    """Run one built ``docker compose`` *command* on *parent*, unbounded.

    Unbounded on purpose: an image build or pull has no defensible bound, and
    a made-up constant would be wrong on a slower builder. Every whole-stack
    ``up``, ``config`` and ``down`` runs through here, so that decision is
    stated once. A per-service ``stop``/``rm`` and the observe verbs keep the
    host's default bound: they never pull or build.
    """
    return await parent.exec(command, timeout=float("inf"))


async def up_stack(parent: Host, command: str, *, project: str) -> CommandResult:
    """Run one ``docker compose … up`` *command* on *parent*, re-running it once on the race.

    The one ``up`` for every path that starts a stack, so the retry cannot be
    left behind by one of them. ``up -d`` is convergent: after the libnetwork
    race (``_is_transient_network_race``) the re-run starts the
    already-created container once the network settles. Any other failure
    fails the same way twice, so it is never retried.

    If the race recurs on the re-run, the parent's daemon is degraded, not
    merely racing: read ``journalctl -u docker`` there around the failure.
    The levers, roughly in order, are a longer
    ``_NETWORK_RACE_RETRY_BACKOFF_S`` or a third attempt, a
    ``docker network prune`` before ``up``, or a daemon restart between runs.

    Raises:
        HostCommandError: ``up`` failed for another reason, or raced twice;
            the message carries docker's own output.
    """
    result = await _run_compose(parent, command)
    if not result.is_ok and _is_transient_network_race(result.value):
        logger.debug(
            f"[docker] {project} hit a transient network race on up; "
            f"retrying once after {_NETWORK_RACE_RETRY_BACKOFF_S}s"
        )
        await asyncio.sleep(_NETWORK_RACE_RETRY_BACKOFF_S)
        result = await _run_compose(parent, command)
    if not result.is_ok:
        raise HostCommandError(
            f"docker compose up failed for {project} on {parent.id}: {result.value}"
        )
    return result


def get_user_compose_project(project_name: str, suffix: str | None = None) -> str:
    """Return a compose project name unique enough to coexist with other runs.

    Format: ``otto-<project>-<suffix>``. *suffix* defaults to the OS username,
    or ``OTTO_COMPOSE_SUFFIX`` if set in the environment. Lowercase only —
    compose project names must be lowercase.
    """
    # Deferred import: otto.models.settings pulls pydantic_settings + dotenv (26
    # modules) and `otto docker --help` never resolves a project name (import
    # budget).
    from ..models.settings import OttoEnvSettings

    # Fresh OttoEnvSettings() (not the get_env() singleton) so OTTO_COMPOSE_SUFFIX
    # is re-read each call — callers/tests set it per-invocation.
    raw_suffix = suffix or OttoEnvSettings().compose_suffix or _safe_username()
    return f"otto-{project_name}-{raw_suffix}".lower()


def _slug_segment(name: str) -> str:
    """Lower-case *name* and squash compose-illegal characters to ``-``."""
    return re.sub(r"[^a-z0-9_-]+", "-", name.lower())


def use_case_project(lab_name: str, use_case: str, suffix: "str | None" = None) -> str:
    """Compose project for *use_case* deployed into *lab_name* (spec §9).

    ``<lab>-<usecase>-<suffix>`` — deliberately NO ``otto-`` prefix: the
    deployment belongs to the product, not to the tool that enabled it.
    The lab segment is load-bearing: ``--remove-orphans`` reaps within a
    project, and one docker host can serve several labs — two labs must
    never share a project.

    ALL THREE segments are slugged by the same rule. Docker requires
    ``-p`` to match ``[a-z0-9][a-z0-9_-]*``, and it does not care which
    segment carried the offending character: a lab named ``"Unix Lab"``, a
    use-case named ``"smoke test"`` and a ``$USER`` of ``"first.last"`` are
    the identical defect, and slugging only the first of them would have made
    the other two fail as a raw docker error instead of a name otto can use.
    The leading character is repaired for the same reason -- a lab named
    ``"_scratch"`` slugs cleanly and is still rejected by docker.
    """
    # Same deferred-import + fresh-read rationale as get_user_compose_project.
    from ..models.settings import OttoEnvSettings

    raw = suffix or OttoEnvSettings().compose_suffix or _safe_username()
    project = "-".join(_slug_segment(part) for part in (lab_name, use_case, raw))
    return project.lstrip("-_") or _slug_segment(use_case)


def _safe_username() -> str:
    try:
        return getpass.getuser()
    except KeyError:
        return "anon"


def _project_parent_host(lab: "Lab", parent: str | None) -> UnixHost:
    """Pick the parent host for a project's compose stack (spec §2).

    Explicit *parent*, else the lab's default parent: the one rule
    (:func:`~otto.docker.observe.docker_parent`), the same host a use-case
    deploys on; the project has no say. Every refusal is the rule's own
    :class:`~otto.docker.observe.DockerVerbError` on the ``parent`` field.
    """
    # Function-scope: observe imports this module.
    from .observe import docker_parent

    return docker_parent(lab, parent)


def compose_command(
    project: str,
    *args: str,
    files: "Sequence[str | PurePath]" = (),
    env_file: "str | PurePath | None" = None,
    env: "Mapping[str, str] | None" = None,
) -> str:
    """``[env K=V …] docker compose -p P [-f F …] [--env-file E] ARGS…``, every token quoted.

    The one place a ``docker compose`` command is spelled: the use-case and
    per-project paths, their dry-run previews and the observe verbs all build
    here, so quoting and flag order cannot drift between them.

    *args* are the subcommand, docker's own flags and any service names, one
    token each; each is shell-quoted here, so no caller pre-quotes. *files*
    keep their order (later files override earlier ones). *env* becomes an
    ``env K=V`` prefix, keys sorted so the command is stable, each key and
    each value quoted on its own.
    """
    # ast-grep-ignore: compose-command-through-builder
    words: "list[str]" = ["docker", "compose", "-p", project]
    for path in files:
        words += ["-f", str(path)]
    if env_file is not None:
        words += ["--env-file", str(env_file)]
    words += args
    prefix = (
        ["env", *(f"{shlex.quote(k)}={shlex.quote(env[k])}" for k in sorted(env))] if env else []
    )
    return " ".join([*prefix, *(shlex.quote(w) for w in words)])


async def _stack_already_up(parent: Host, project_name: str) -> bool | None:
    """True/False if any container runs under *project_name*, ``None`` if unknown.

    Three states, because the two callers want opposite things from a failed
    ``docker ps`` and folding it into False served only one of them:

    - :func:`compose_up` treats unknown as "not up" and runs ``up -d``, which
      is convergent, so the wrong guess costs nothing. Raising there would
      turn a probe hiccup into a hard failure on a path that self-heals.
    - :func:`composed` cannot do that: it uses the answer to decide whether
      the stack was someone ELSE's and only tears down what it brought up.
      Unknown read as False means "nobody had it", so the teardown yanks a
      stack an outer fixture is holding — precisely what that contract
      promises not to do. It raises instead.

    ``None`` is NOT the answer for a dry run, even though the type can hold
    it: "unknown" here means *the probe ran and could not tell*, and
    ``compose_up`` responds to it by running the convergent ``up -d`` — an
    ACTION. A dry run must not buy that arm with a shrug, so it refuses.
    """
    result = await parent.exec(
        f"docker ps -q --filter label=com.docker.compose.project={shlex.quote(project_name)}"
    )
    refuse_declined_fact(result, asked=f"stack_already_up({project_name!r})")
    if not result.status.is_ok:
        logger.warning(
            f"[docker] could not tell whether {project_name} was already running on "
            f"{parent.id}: {result.value}"
        )
        return None
    return bool(result.value.strip())


async def _resolve_container_id(
    parent: Host,
    project_name: str,
    service: str,
) -> str | None:
    """Look up the running container id for ``project_name/service`` on *parent*.

    Called right after a successful ``up -d``, where compose has already
    reported the container Started — but on a busy remote daemon the just-
    Started container can briefly not yet appear in ``docker ps``. A single-shot
    lookup then misses it and the service is silently skipped (0 containers
    registered). Poll up to ``_CONTAINER_ID_RESOLVE_ATTEMPTS`` times with a
    short backoff; since compose has guaranteed the container exists, this only
    waits out the visibility lag rather than masking a missing container.
    Returns ``None`` if it never becomes visible within the bounded polls.

    A dry run's decline is refused rather than polled: ``None`` would mean
    "compose started it and it never appeared", the caller warns and skips
    the service, and four rounds of ``_CONTAINER_ID_RESOLVE_BACKOFF_S`` would
    be spent waiting for a container nobody asked docker about.
    """
    # #737 moves this lookup onto wait_for_async with a deadline; the count
    # loop stays as it is until then so the two changes do not cross.
    # ast-grep-ignore: no-handrolled-deadline-poll
    for attempt in range(_CONTAINER_ID_RESOLVE_ATTEMPTS):
        result = await parent.exec(compose_container_probe(project_name, service))
        refuse_declined_fact(result, asked=f"resolve_container_id({project_name}/{service})")
        if result.status.is_ok:
            cid = result.value.strip().splitlines()
            if cid:
                return cid[0]
        else:
            # Not folded silently into "not visible yet": a permission-denied
            # or timed-out `docker ps` is a different problem from a slow
            # daemon, and compose_up's "none resolved" error tells the user to
            # read these warnings.
            logger.warning(
                f"[docker] looking up {project_name}/{service} on {parent.id} failed: "
                f"{result.value}"
            )
        if attempt < _CONTAINER_ID_RESOLVE_ATTEMPTS - 1:
            await asyncio.sleep(_CONTAINER_ID_RESOLVE_BACKOFF_S)
    return None


async def compose_up(
    project: Project,
    lab: "Lab",
    *,
    parent: str | None = None,
    project_name: str | None = None,
    build: bool = False,
) -> dict[str, DockerContainerHost]:
    """Bring up *project*'s compose stack on a parent host.

    Convergent at the project-name level (spec §8): ``up -d --remove-orphans``
    is issued whether or not a stack with the same *project_name* is already
    running on *parent* — a broader deployment adds services onto a live
    stack, and a displaced provider's now-orphaned container is reaped in the
    same pass. The already-up probe is retained only to decide OWNERSHIP: it
    answers whether THIS call is the one that must roll the stack back on a
    later failure, never whether ``up`` itself runs. Returns a dict mapping
    each declared service to its :class:`~otto.host.docker_host.DockerContainerHost`, with the hosts
    also registered in ``lab.hosts`` so ``--list-hosts`` and
    ``otto host <id>`` see them.

    Args:
        build: When True and the project declares ``[[docker.images]]``, run
            :func:`~otto.docker.build.build_images` first. The default is
            docker's own: ``up`` builds nothing, and a service whose image is
            missing gets docker's error.

    Raises:
        ~otto.result.CommandNotRunError: this is a dry run. Bringing a stack
            up is the package's most consequential verb and its return type
            -- a dict of LIVE container hosts -- is the least able to say it
            did not happen. ``{}`` reads as "the stack registered nothing",
            which is a real and different outcome this function raises on.
    """
    settings = project.docker_settings
    if not settings.composes:
        raise ValueError(
            f"Project {project.name!r} has no [[docker.composes]] entries; nothing to up."
        )

    parent_host = _project_parent_host(lab, parent)
    proj = project_name or get_user_compose_project(project.name)

    if build and settings.images:
        # A missing Dockerfile or context is settled from configuration: refused
        # under a dry run too, and before anything is touched.
        from .build import check_project_images  # late: `compose <-> build` cycle

        check_project_images(project)

    # Below _project_parent_host, so a dry run still fails on an unknown host, a
    # non-UnixHost parent or one that is not docker_capable -- those refusals
    # are settled from configuration and must fire identically either way.
    # Above everything else, because everything else is a device touch or a
    # mutation of `lab.hosts`.
    if is_dry_run():
        raise CommandNotRunError(
            f"compose_up({project.name}: {proj})",
            parent_host.id,
            "No image was built, no file was staged, no container was started "
            "and no host was registered.",
        )

    if build and settings.images:
        # Late import to avoid a circular `compose <-> build` import.
        from .build import build_images

        results = await build_images(project, parent_host)
        for name, built in results.items():
            if not built.is_ok:
                # value, not msg: the captured build output is the diagnosis.
                raise HostCommandError(
                    f"build for image {name!r} failed before compose up: {built.result.value}"
                )

    from .staging import stage_compose_files

    # Stage under the compose-project key (e.g. ``otto-project1-vagrant`` or a
    # ``OTTO_COMPOSE_SUFFIX``-suffixed variant) rather than ``project.name`` so
    # concurrent ``otto docker compose up`` invocations with different suffixes
    # don't ``rm -rf`` each other's compose dir mid-stage.
    remote_files = await stage_compose_files(parent_host, proj, list(settings.composes))
    remote_file_strs = [str(p) for p in remote_files]

    # `is True`, so an UNKNOWN answer (a failed probe) means this call is
    # treated as the owner and will roll back on a later failure — the
    # opposite default would strand a stack nobody claims. This is now used
    # ONLY for that ownership decision: `up -d` itself always runs below,
    # convergent regardless of what this probe answered.
    brought_up_here = await _stack_already_up(parent_host, proj) is not True
    try:
        return await _up_and_register(project, lab, parent_host, proj, remote_file_strs)
    except BaseException:
        # Every raise below this point happens AFTER `up -d` has run, and the
        # caller cannot clean up what it never received: `composed()` arms its
        # try/finally only on a successful return, and a direct caller has no
        # handle either. Silently returning {} used to be accidentally safe
        # for `composed()` — it entered the try and tore the stack down — so
        # failing loud without this would be strictly worse than the bug.
        # Only what WE started; a stack that was already up is someone else's.
        if brought_up_here:
            await roll_back(lab, [StartedStack(parent_host, proj, remote_file_strs)])
        raise


@dataclass
class StartedStack:
    """A stack one call started, and the files it was started from: what a rollback undoes."""

    parent: UnixHost
    project: str
    files: "list[str]"
    """The staged ``-f`` set, in order, exactly as ``up`` was given it."""
    env_file: "str | None" = None
    """The staged ``--env-file``, when ``up`` was given one."""
    services: "list[str]" = field(default_factory=list)
    """The services ``up`` named, when it named any; empty means all the files declare."""


async def roll_back(lab: "Lab", started: "list[StartedStack]") -> None:
    """Take down every stack in *started*: the one rollback for every path that starts one.

    The rule: a rollback undoes exactly what its call did. Each stack goes
    down through the files it was started from, naming the services its ``up``
    named, so containers under the same project label that its ``up`` did not
    start stay up, and nothing is
    unregistered. On a start's one parent, registration is the last step and
    is all or nothing (:func:`register_stack_hosts`), so a failed start
    registered nothing, and a placeholder or a peer's id is never this call's
    to remove.

    Each ``down`` is a compensating action: an interrupt that lands while it
    runs waits for it, up to the run's teardown deadline
    (:func:`~otto.lifecycle.compensate`), because a half-run rollback is worse
    than none.

    Never raises and never masks: the caller is already propagating the real
    error. A failed ``down``, raised or returned, is reported, though: residue
    the user has to clean up by hand deserves to be named. Under a held
    interrupt, compensate re-raises the cancellation once the ``down``
    resolves, so its own warning names a failure instead of this function's
    line, and any later stack in *started* is not reached.
    """
    # Imported here, not at module scope: otto.lifecycle is only needed once a
    # compensating action actually runs (import-budget guard).
    from ..lifecycle import compensate

    for stack in started:
        what = (
            f"{stack.project} could not be rolled back on {stack.parent.id}; "
            f"whatever its up created there is still up"
        )
        try:
            result = await compensate(
                compose_down_project(
                    stack.parent,
                    stack.project,
                    lab=lab,
                    remove_ids_under=None,
                    files=stack.files,
                    env_file=stack.env_file,
                    services=stack.services,
                ),
                what=f"rolling back {stack.project} on {stack.parent.id}",
            )
        except Exception as e:  # noqa: BLE001 — a rollback may not mask the real error
            # .error, not .exception: the traceback the user needs belongs to the
            # error being propagated, not to the rollback that failed after it.
            logger.error(f"[docker] {what}: {e}")  # noqa: TRY400 — see above
            continue
        if not result.is_ok:
            logger.error(f"[docker] {what}: {result.value}")


def merge_declared_users(composes: "Iterable[DockerCompose]") -> "dict[str, str]":
    """Union the ``users`` declarations of *composes*; conflicting values refuse.

    Same-service agreement is fine (the same compose seen twice, or two files
    declaring the same identity); a genuine disagreement has no right answer
    otto should invent.
    """
    merged: dict[str, str] = {}
    for c in composes:
        for svc, u in c.users:
            if svc in merged and merged[svc] != u:
                raise ValueError(
                    f"conflicting declared users for service {svc!r}: "
                    f"{merged[svc]!r} vs {u!r} — declare one value per service "
                    f"across the composes in play"
                )
            merged[svc] = u
    return merged


async def register_stack_hosts(
    lab: "Lab",
    parent: UnixHost,
    *,
    compose_project: str,
    id_project: str,
    services: "list[str]",
    users: "dict[str, str] | None" = None,
) -> "dict[str, DockerContainerHost]":
    """Resolve container ids and register hosts for *services* of one stack.

    ``id_project`` is the middle segment of the host id
    (``<parent>.<id_project>.<service>``): the project name on the legacy per-project
    path, the use-case name on the deploy path (spec §9).

    *users* is the declared per-service access user (``users = {...}`` in
    ``[[docker.composes]]``, merged across the composes in play by
    :func:`merge_declared_users`). A service absent from it simply registers
    with no declared user, deferring to the image's ``USER`` — the mapping is
    per service, never a stack-wide default.
    """
    # Function-scope: this coroutine is only ever entered after a compose up
    # has already run, so a bare `otto docker --help` must not pay
    # otto.docker.mounts's import cost (import budget). No cycle at call
    # time: `mounts.py` imports only stdlib plus `..host.host` and
    # `..host.mount`, both of which this module already pulls at module
    # scope.
    from .mounts import inspect_mounts

    resolved: "list[tuple[str, str]]" = []
    for service in services:
        cid = await _resolve_container_id(parent, compose_project, service)
        if not cid:
            logger.warning(
                f"[docker] could not resolve container id for {compose_project}/{service}; "
                f"skipping registration"
            )
            continue
        resolved.append((service, cid))

    # One inspect for the whole stack, after every id is known -- per
    # container would double the round trips this function already spends.
    # Guarded rather than relying solely on `inspect_mounts`'s own empty-list
    # short-circuit: the every-service-failed path (every `continue` above
    # fired) has nothing to inspect, and skipping the call here is what lets
    # it reach the `if not hosts:` refusal below without a round trip.
    mounts_by_id: "dict[str, list[Mount]]" = {}
    if resolved:
        mounts_by_id = await inspect_mounts(parent, [cid for _, cid in resolved])

    # Built and stamped in full before the lab is touched, so a raising
    # provider leaves the lab exactly as it was.
    hosts: dict[str, DockerContainerHost] = {}
    for service, cid in resolved:
        host = DockerContainerHost(
            parent=parent,
            container_id=cid,
            project=id_project,
            service=service,
            compose_project=compose_project,
            user=(users or {}).get(service),
            mounts=mounts_by_id.get(cid, []),
        )
        # A container's lab is its PARENT's lab, never the lab it is registered
        # INTO: in a multi-lab session that lab is the composite ("a+b"), a name
        # no component owns and no `lab_patterns` entry matches — so the very
        # project that declared this compose would stop seeing its own containers.
        # The parent was built by the factory and carries the component name.
        # An unattributed parent leaves this empty and falls through to
        # ``Lab.add_host``'s backstop, which stays a pure backstop.
        host.source_lab = parent.source_lab
        # ``replace`` rather than a plain assignment: LabInfo is frozen, but
        # the dict behind its ``metadata`` is not. Sharing the parent's record
        # would give every container of that parent one table, so a write on
        # any of them would surface on all of them and on the parent — the
        # aliasing LabInfo.__post_init__ exists to prevent. replace() re-runs
        # __post_init__, which copies the dict.
        host.lab_info = replace(parent.lab_info)
        # Same products a factory-built host gets: the match table and the
        # providers see a container by its own attributes (service, id, os).
        # AFTER the source_lab stamp — that is what the project-targeting gate on
        # both seams reads to decide whether a project may reach this host.
        apply_providers(host)
        hosts[service] = host

    # Register in the lab so otto host <id> finds it, all or nothing: a start
    # re-registers on every call, replacing a placeholder from
    # register_declared_container_hosts or a prior start's entry for the same
    # service (e.g. after a container restart changed its id), so this is an
    # explicit delete-then-add (add_host would reject the duplicate id
    # outright), and a failure part-way puts back every entry it replaced.
    replaced: "list[tuple[str, Host | None]]" = []
    try:
        for host in hosts.values():
            replaced.append((host.id, lab.hosts.pop(host.id, None)))
            lab.add_host(host)
    except BaseException:
        for host_id, previous in reversed(replaced):
            lab.hosts.pop(host_id, None)
            if previous is not None:
                lab.hosts[host_id] = previous
        raise

    if not hosts:
        # Same silent-success shape as `_up_and_register`'s empty-`services`
        # case, reached the other way: every service resolved to no running
        # container (this loop's `continue` skipped every one). The
        # usual cause is that they all exited immediately — a container that is
        # not running is not a host otto can drive.
        raise HostCommandError(
            f"none of compose stack {compose_project}'s {len(services)} service(s) resolved to a "
            f"running container on {parent.id}, so no host was registered — the usual "
            f"cause is that they all exited immediately; see the per-service warnings "
            f"above, then `{compose_command(compose_project, 'logs')}` on {parent.id}"
        )

    return hosts


async def _up_and_register(
    project: Project,
    lab: "Lab",
    parent: UnixHost,
    proj: str,
    remote_file_strs: list[str],
) -> dict[str, DockerContainerHost]:
    """Bring the stack up and register its containers — the rollbackable half.

    ``up -d --remove-orphans`` is issued UNCONDITIONALLY (spec §8): the
    caller's already-up probe decides only who owns rollback on a later
    failure, never whether this runs. A stack that was already up still gets
    a convergent re-run, which is how a broader deployment can add services
    onto it and reap a displaced provider's now-orphaned container.
    """
    settings = project.docker_settings
    logger.info(f"[docker] composing {proj} on {parent.id} (convergent)")
    await up_stack(
        parent,
        compose_command(proj, "up", "-d", "--remove-orphans", files=remote_file_strs),
        project=proj,
    )

    # Enumerate services. Project-declared list is authoritative for the
    # mapping we return; cross-check against the live list and warn on drift.
    declared_services: list[str] = []
    for compose in settings.composes:
        declared_services.extend(compose.services)
    declared_services = list(dict.fromkeys(declared_services))  # dedupe, preserve order

    live = await _run_compose(
        parent, compose_command(proj, "config", "--services", files=remote_file_strs)
    )
    live_services: set[str] = set()
    if live.is_ok:
        live_services = {s.strip() for s in live.value.splitlines() if s.strip()}
        if declared_services and set(declared_services) != live_services:
            logger.warning(
                f"[docker] declared services {sorted(declared_services)} differ from "
                f"compose-listed services {sorted(live_services)} for {proj}"
            )
    elif declared_services:
        # A cross-check only: the declared list is authoritative, so losing the
        # comparison costs a warning, not the stack.
        logger.warning(
            f"[docker] could not list {proj}'s services to cross-check the declared "
            f"ones: {live.value}"
        )
    else:
        # Nothing declared AND nothing listed: `services` below would be empty,
        # the registration loop would not run, and compose_up would return {} —
        # which `otto docker compose up` prints as "0 container(s) registered" in green,
        # exit 0. A stack that is UP and unusable must not report success.
        raise HostCommandError(
            f"listing {proj}'s services on {parent.id} failed and the project declares "
            f"none of its own, so no container host can be registered — add "
            f"`services = [...]` to [[docker.composes]] to name them: {live.value}"
        )

    services = declared_services or sorted(live_services)
    if not services:
        # ValueError, not a host error: nothing on the parent failed. The
        # compose file simply declares no services, which is the same class of
        # refusal as _project_parent_host's "not docker_capable".
        raise ValueError(
            f"compose stack {proj} is up on {parent.id} but names no services, so there "
            "is nothing to register — check the compose file's `services:` block"
        )

    return await register_stack_hosts(
        lab,
        parent,
        compose_project=proj,
        id_project=project.name,
        services=services,
        users=merge_declared_users(project.docker_settings.composes),
    )


async def compose_down(
    project: Project,
    lab: "Lab",
    *,
    parent: str | None = None,
    project_name: str | None = None,
    stop_timeout: int = 1,
) -> CommandResult:
    """Tear down *project*'s compose stack and unregister its container hosts.

    *stop_timeout* is the per-container graceful-shutdown grace period in
    seconds passed to ``docker compose down --timeout``. Defaults to 1s
    rather than docker's default of 10s — otto's typical workload is
    integration tests with disposable stacks where waiting 10s on every
    teardown adds up fast (4 tests x 10s = 40s of wall time on the
    serialized ``docker_e2e`` group). Pass a larger value for stacks where
    graceful shutdown matters.

    Returns the ``docker compose down`` command's
    :class:`~otto.result.CommandResult`. A project with no ``[[docker.composes]]``
    yields a ``Status.Skipped`` result that never ran (``retcode`` ``-1``).
    A failed tear-down is logged and returned, never raised — callers sweep
    the rest of their projects.

    Raises:
        ~otto.result.CommandNotRunError: this is a dry run. The one exception
            to "never raised", and the arm has to be HERE rather than deeper:
            the ``except RuntimeError`` below catches
            :class:`~otto.result.CommandNotRunError` (it is a ``RuntimeError``
            by declaration), so a decline raised inside ``stage_compose_files``
            was caught and returned as ``Status.Failed`` -- a tear-down that
            never ran, reported as a tear-down that failed. Returning a
            ``Status.NotRun`` result instead of raising would keep the sweep
            going, but it would also let this function's other half -- the
            loop that pops container hosts out of ``lab.hosts`` and closes
            them -- run against a lab a dry run must leave alone.
    """
    settings = project.docker_settings
    if not settings.composes:
        return CommandResult(Status.Skipped, value="", command="", retcode=-1)

    parent_host = _project_parent_host(lab, parent)
    proj = project_name or get_user_compose_project(project.name)

    if is_dry_run():
        raise CommandNotRunError(
            f"compose_down({project.name}: {proj})",
            parent_host.id,
            "No container was stopped and no host was unregistered from the lab.",
        )

    from .staging import stage_compose_files

    # See compose_up() for the staging-key rationale: keyed on the compose
    # project (suffix-aware) so concurrent stacks don't collide.
    #
    # Caught, not propagated: staging now raises when it cannot prepare its
    # dirs, and this function's contract is that a failed tear-down is
    # RETURNED. Letting it raise would stop `otto docker compose down` mid-sweep with
    # projects 2..n still up, and inside `composed()`'s finally it would replace
    # the body's real exception with teardown noise — the thing compensate()
    # exists to prevent.
    try:
        # warn=False: this re-stages purely to recover the `-f` paths for
        # teardown. Staging's own `rm -rf` has already wiped the directory the
        # advisory would name by the time it could fire, so on this path it
        # reports a loss that has happened, about a stack being torn down.
        remote_files = await stage_compose_files(
            parent_host, proj, list(settings.composes), warn=False
        )
    except RuntimeError as e:
        # .error, not .exception: this is returned as a failed CommandResult,
        # so the caller decides how loud to be about it.
        logger.error(  # noqa: TRY400 — see above
            f"[docker] cannot stage compose files to tear {proj} down: {e}"
        )
        return CommandResult(Status.Failed, value=str(e), command="", retcode=1)
    # Through the files just staged, so only what they declare goes; then the
    # hosts that came from this stack are unregistered (the use-case teardown
    # sweeps `<parent>.<usecase>.` instead).
    return await compose_down_project(
        parent_host,
        proj,
        lab=lab,
        remove_ids_under=f"{parent_host.id}.{project.name.lower()}.",
        stop_timeout=stop_timeout,
        files=remote_files,
    )


async def compose_down_project(
    parent: UnixHost,
    compose_project: str,
    *,
    lab: "Lab",
    remove_ids_under: "str | None",
    stop_timeout: int = 1,
    files: "Sequence[str | PurePath] | None" = None,
    env_file: "str | PurePath | None" = None,
    services: "Sequence[str]" = (),
) -> CommandResult:
    """Take a compose stack down, then unregister its hosts: the one ``down``.

    Two shapes, chosen by *files* (``None`` for none; an empty list is refused):

    - **Files given** (with *env_file* when ``up`` had one): the ``up``
      command minus its verb, ``-f … [--env-file E] down [SERVICES]``, and
      nothing otto adds. Docker removes only the services those files declare
      (or *services*, when named) and the networks compose created for them;
      other containers under the same project label stay. Everything else is
      docker's own: its orphan setting, its interpolation (the shell's
      environment over the env file), its profiles.
    - **No files**: ``down --remove-orphans`` by the project label alone, which
      docker already carries, so nothing has to be re-rendered or re-staged
      merely to delete what it produced. It removes every container under the
      label, a displaced provider's orphan included.

    *remove_ids_under* is the container-host id prefix to unregister
    (``<parent>.<usecase>.``), or ``None`` to unregister nothing. A rollback
    (:func:`roll_back`) passes ``None``: the call it compensates for registered
    nothing, and popping ids it did not put there would yank a placeholder or
    a peer's registrations out of the lab.

    Never raises for a failed ``down``: the result is returned so a sweep over
    several hosts keeps going, exactly like :func:`compose_down`.

    Raises:
        ValueError: *files* is an empty list, a caller's bug: read as "no
            files" it would widen the down to every container under the
            project label.
        ~otto.result.CommandNotRunError: this is a dry run. Armed here, not
            inherited from ``exec``, because the second half of this function
            MUTATES ``lab.hosts`` -- a dry run must leave the lab alone, and a
            declined ``down`` result whose ``.value`` is read would raise from
            the logging line instead, naming the wrong thing.
    """
    if is_dry_run():
        raise CommandNotRunError(
            f"compose_down_project({compose_project})",
            parent.id,
            "No container was stopped and no host was unregistered from the lab.",
        )

    timeout_args = ["--timeout", str(int(stop_timeout))]
    if files is not None:
        if not files:
            # An empty list is a caller's bug, never "no files": read as the
            # label-only shape it would widen the down to every container
            # under the project.
            raise ValueError(f"compose_down_project({compose_project}): files=[] names no file")
        command = compose_command(
            compose_project, "down", *timeout_args, *services, files=files, env_file=env_file
        )
    else:
        command = compose_command(compose_project, "down", "--remove-orphans", *timeout_args)
    result = await _run_compose(parent, command)
    if not result.is_ok:
        logger.error(
            f"[docker] compose down failed for {compose_project} on {parent.id}: {result.value}"
        )
    if remove_ids_under is not None:
        await unregister_container_hosts(lab, remove_ids_under)
    return result


async def unregister_container_hosts(
    lab: "Lab", prefix: str, *, services: "list[str] | None" = None
) -> list[str]:
    """Close and pop every container host in *lab* whose id starts with *prefix*.

    Children before parent (each container host is closed while the parent's
    connection is still alive, so its persistent session drains cleanly), and
    best-effort: a host that fails to close is warned about and still popped,
    because leaving it in ``lab.hosts`` would advertise a container that has
    just been removed.

    *services* narrows the sweep to ``<prefix><service>`` ids -- the partial
    (``down USE_CASE SERVICE...``) teardown, which must leave the rest of the
    stack registered. Returns the ids that were removed.

    The narrowed ids are LOWER-CASED, because ``DockerContainerHost.id`` is
    (``f"{parent}.{project}.{service}".lower()``) and compose service names
    are not case-folded by docker. Without it, ``down USE_CASE MyApi`` stopped
    and ``rm -f``'d the container and then matched nothing here, leaving otto
    advertising a host for a container it had just removed -- the silent-wrong
    shape this module keeps being swept for. ``prefix`` is lowered too rather
    than trusted: every caller builds it from ids that are already lower, so
    lowering is a no-op for them and a repair for anyone who forgets.
    """
    prefix = prefix.lower()
    wanted = None if services is None else {f"{prefix}{s}".lower() for s in services}
    drop = [
        hid for hid in lab.hosts if hid.startswith(prefix) and (wanted is None or hid in wanted)
    ]
    for hid in drop:
        # `pop(..., None)` and the None check are not paranoia about our own
        # snapshot: `await host.close()` below yields, so a concurrent
        # teardown (a peer instruction, an outer fixture's compensating
        # action) can pop an id out of `lab.hosts` between the snapshot and
        # our turn. Popping it again then returns None, and closing None
        # would replace a benign race with an AttributeError inside a
        # best-effort sweep.
        host = lab.hosts.pop(hid, None)
        if host is not None:
            try:
                await host.close()
            except Exception as e:  # noqa: BLE001 — best-effort teardown, logs warning
                logger.warning(f"[docker] error closing container host {hid}: {e}")
    return drop


@contextlib.asynccontextmanager
async def composed(
    project: Project,
    lab: "Lab",
    *,
    parent: str | None = None,
    project_name: str | None = None,
    own: bool = False,
    build: bool = False,
) -> AsyncIterator[dict[str, DockerContainerHost]]:
    """Context manager wrapping ``compose_up`` / ``compose_down``.

    By default the stack is **not** torn down on exit if it was already
    running on entry — this lets a suite-level fixture hold the stack
    while inner instructions also call ``composed`` without yanking it
    from each other. Pass ``own=True`` to force teardown.

    *build* is forwarded to :func:`compose_up`.

    Raises:
        ~otto.result.CommandNotRunError: this is a dry run. Its own arm
            rather than an inherited one: with ``own=True`` the probe below
            is skipped and the decline would come from ``compose_up``, with
            ``own=False`` it would come from ``_stack_already_up``, so the
            documented entry point of this package would name one of two
            different callees depending on a flag. It also declines before
            the ``finally`` exists, so no teardown is armed for a stack that
            was never brought up.
    """
    parent_host = _project_parent_host(lab, parent)
    proj = project_name or get_user_compose_project(project.name)

    if is_dry_run():
        raise CommandNotRunError(
            f"composed({project.name}: {proj})",
            parent_host.id,
            "No stack was brought up, so none was torn down either.",
        )

    # Only consulted when `own` is False (see the gate in the finally below),
    # so do not pay for the probe — or fail on it — when the caller has
    # already said it owns the stack.
    was_up = False
    if not own:
        probed = await _stack_already_up(parent_host, proj)
        if probed is None:
            raise HostCommandError(
                f"cannot tell whether {proj} was already running on {parent_host.id}, so "
                "composed() cannot promise to leave a peer's stack alone; pass "
                "own=True to tear down unconditionally"
            )
        was_up = probed

    hosts = await compose_up(project, lab, parent=parent, project_name=proj, build=build)
    try:
        yield hosts
    finally:
        if own or not was_up:
            # Teardown is a compensating action: an interrupt landing while
            # compose_down runs must not strand a half-torn stack (chaos
            # spec: shielded compensating actions). compensate() holds the
            # cancellation until the down completes (bounded by the teardown
            # deadline), then re-raises it.
            # Imported here, not at module scope: otto.lifecycle is only needed
            # once a compensating action actually runs, and a top-level import
            # drags it onto every CLI --help path (import-budget guard).
            from ..lifecycle import compensate

            await compensate(
                compose_down(project, lab, parent=parent, project_name=proj),
                what=f"docker compose down {proj}",
            )


def _default_parent_or_none(lab: "Lab") -> UnixHost | None:
    """Return the lab's default docker parent by the one rule, or ``None`` when it refuses.

    The placeholder walks run at the start of every otto invocation, long
    before a caller names a use-case, so a lab with no unambiguous parent
    (a tie, or no docker-capable host) is normal there: they register
    nothing and say nothing (spec §7).
    """
    # Function-scope: a bare `otto docker --help` must not pay observe.py's
    # import cost (import budget); both modules are fully loaded by the time
    # this RUNS, so there is no cycle.
    from .observe import DockerVerbError, default_docker_parent

    try:
        return default_docker_parent(lab)
    except DockerVerbError:
        return None


def register_declared_container_hosts(lab: "Lab", projects: list[Project]) -> int:
    """Pre-register *placeholder* container hosts in *lab* for every declared service triple.

    The placeholders carry an empty ``container_id`` so that any operation
    against a not-yet-up container fails with a clear "run `otto docker compose up`"
    message rather than a confusing not-found error. Once :func:`compose_up`
    (legacy projects) or :func:`~otto.docker.deployment.deploy` (use-case projects)
    runs, it overwrites the placeholder with a real entry containing the
    resolved container id.

    A project that declares ``[[docker.use_cases]]`` fragments takes the
    USE-CASE branch below INSTEAD OF the legacy composes walk (never both):
    its placeholders carry the use-case name as the id's middle segment
    (``<parent>.<usecase>.<service>``, spec §9) rather than the project name, and
    their ``project`` field names what to start: the use-case, which the
    container host's not-running error shows. A project with no ``use_cases``
    keeps today's per-project ids unchanged.

    Both branches register under the ONE parent the lab's default rule names
    (:func:`~otto.docker.observe.default_docker_parent`) and under none when
    the rule refuses — a lab with no unambiguous parent is normal at this
    point (this walk runs at the start of every otto invocation), not an error.

    Returns the number of placeholders registered.
    """
    count = 0
    parent: UnixHost | None = None
    resolved = False
    for project in projects:
        settings = project.docker_settings
        if not (settings.use_cases or settings.composes):
            continue
        # Resolved once, lazily: a lab none of whose projects declares docker
        # never pays the rule (or observe.py's import) on the every-invocation
        # preamble this walk runs in.
        if not resolved:
            parent, resolved = _default_parent_or_none(lab), True
        if parent is None:
            break  # the rule refuses: no project gets a placeholder
        if settings.use_cases:
            count += _register_use_case_placeholders(lab, project, parent)
            continue
        # Merged over the project's WHOLE compose set, once, before the loop —
        # not per compose. Reading each entry's own `users` in the loop would
        # make the first compose declaring a service win (the duplicate-id
        # skip below discards the later placeholder), so a project whose two
        # composes disagree would get a silent first-wins placeholder here and
        # a refusal from `compose_up`, which merges the same way this does.
        # One gate, one answer, both paths.
        users = merge_declared_users(settings.composes)
        for compose in settings.composes:
            # One parent, the lab's default by the one rule — the same one the
            # use-case walk and completion use, so all three agree on which
            # container hosts exist before the first `up`.
            for service in compose.services:
                placeholder = DockerContainerHost(
                    parent=parent,
                    container_id="",
                    project=project.name,
                    service=service,
                    compose_project=get_user_compose_project(project.name),
                    user=users.get(service),
                )
                # Same rule as compose_up's registration: the container
                # belongs to its parent's lab, not to whatever composite the
                # session assembled.
                placeholder.source_lab = parent.source_lab
                # Copied, not aliased — same reason as compose_up's.
                placeholder.lab_info = replace(parent.lab_info)
                if placeholder.id in lab.hosts:
                    continue
                # Same ingest a factory-built host runs, for the same
                # reason compose_up's registration runs it — and after the
                # duplicate skip, so a placeholder this walk discards
                # never runs a provider against a host nobody will see.
                apply_providers(placeholder)
                lab.add_host(placeholder)
                count += 1
    return count


def _register_use_case_placeholders(lab: "Lab", project: Project, parent: UnixHost) -> int:
    """Best-effort use-case placeholders for one project's fragments (spec §9).

    Every fragment's services are registered under *parent*, the one the
    lab's default rule names (:func:`_default_parent_or_none`, resolved once
    by the caller). The provider competition (spec §4) never runs here — a
    placeholder exists so `otto host <id>` and completion see something
    before the first `deploy`, which deploys the ACTUAL winning use-case. A
    fragment whose ``composes`` handles cannot be resolved to services
    contributes nothing and the rest are still tried; the walk of services
    and declared users is :func:`~otto.docker.deployment._declared`, the one
    answer for both.
    """
    # Function-scope: this walk runs in `cli/invoke.py`'s preamble on every
    # otto invocation, so a bare `otto docker --help` must not pay
    # deployment.py's import cost (import budget). No cycle at call time:
    # both modules are fully loaded by the time this RUNS.
    from .deployment import _declared
    from .resolve import SelectedFragment, UseCaseResolutionError

    count = 0
    for frag in project.docker_settings.use_cases:
        try:
            declared = _declared([SelectedFragment(project, frag)], report=False)
        except UseCaseResolutionError:
            continue  # a handle this project does not define: no placeholder, rest still tried
        services = declared.services
        if not services:
            continue
        # OUTSIDE the soft-skip on purpose. A handle that will not resolve is
        # normal here (this walk runs before any use-case is named), but two
        # of a fragment's composes naming DIFFERENT users for one service is a
        # settings mistake with no right answer — and swallowing it would hand
        # out placeholders whose identity disagrees with the container `deploy`
        # would later register. merge_declared_users is the one gate; it says
        # the same thing on both paths.
        users = merge_declared_users(declared.composes)

        compose_project = use_case_project(parent.source_lab, frag.name)
        for service in services:
            placeholder = DockerContainerHost(
                parent=parent,
                container_id="",
                project=frag.name,
                service=service,
                compose_project=compose_project,
                user=users.get(service),
            )
            # Same rule as the legacy walk's registration above: the
            # container belongs to its parent's lab, not to whatever
            # composite the session assembled.
            placeholder.source_lab = parent.source_lab
            # Copied, not aliased — same reason as the legacy walk's.
            placeholder.lab_info = replace(parent.lab_info)
            if placeholder.id in lab.hosts:
                continue
            # Same ingest as the legacy walk's, same parent.
            apply_providers(placeholder)
            lab.add_host(placeholder)
            count += 1
    return count


def get_container_host(host_id: str) -> DockerContainerHost:
    """Look up a registered container host by id. Raises if not present."""
    from ..invocation import require_run

    lab = require_run().lab
    host = lab.hosts.get(host_id)
    if not isinstance(host, DockerContainerHost):
        raise KeyError(
            f"No container host registered with id {host_id!r}. "
            f"Did you call `otto docker compose up` (or `compose_up`) first?"
        )
    return host
