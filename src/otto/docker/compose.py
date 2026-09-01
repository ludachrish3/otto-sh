"""Docker Compose orchestration: bring stacks up/down and register containers as live hosts.

The public surface (re-exported from :mod:`otto.docker`) is:

- :func:`compose_up` — bring a stack up; returns ``{service: DockerContainerHost}``.
- :func:`compose_down` — stop a stack and remove its container hosts from the lab.
- :func:`composed` — async context manager wrapping the above.
- :func:`compose_ps` — list running stacks on a parent.
- :func:`get_container_host` — lab lookup by id (typed convenience).
- :func:`get_user_compose_project` — name a stack so concurrent runs don't collide.
"""

import asyncio
import contextlib
import getpass
import json
import logging
import re
import shlex
from collections.abc import AsyncIterator, Iterable
from dataclasses import replace
from typing import Any

from ..config.lab import Lab
from ..config.repo import DockerCompose, Repo
from ..host.docker_host import DockerContainerHost
from ..host.errors import HostCommandError
from ..host.host import Host, is_dry_run, refuse_declined_fact
from ..host.unix_host import UnixHost
from ..result import CommandNotRunError, CommandResult
from ..utils import Status

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


def get_user_compose_project(repo_name: str, suffix: str | None = None) -> str:
    """Return a compose project name unique enough to coexist with other runs.

    Format: ``otto-<repo>-<suffix>``. *suffix* defaults to the OS username,
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
    return f"otto-{repo_name}-{raw_suffix}".lower()


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


def _resolve_parent(repo: Repo, lab: Lab, on: str | None) -> UnixHost:
    """Pick a parent host for *repo*'s compose stack (spec §14).

    Order: explicit *on* > the repo's sole use-case placement > error.
    ``[[docker.composes]]`` is a pure file inventory now — it carries no
    placement of its own — so a per-repo verb with no *on* falls through to
    the repo's declared ``[[docker.use_cases]]`` fragments. The per-repo
    primitives stay public (spec §11); a repo whose use-cases place onto
    several hosts is ambiguous here and must pass *on*. The chosen host must
    be ``docker_capable``.
    """
    candidate = on
    if candidate is None:
        from .resolve import SelectedFragment, Selection, resolve_placement

        frags = [SelectedFragment(repo, f) for f in repo.docker_settings.use_cases]
        if not frags:
            raise ValueError(
                f"No docker host specified for repo {repo.name!r}. Pass on=<host_id>, "
                f"or declare [[docker.use_cases]] with a role/placement."
            )
        # The Selection's own `use_case` name is a carrier here, never a
        # claim about which fragment is "the" one: `resolve_placement` never
        # reads it (`_place_fragment` labels each refusal from the fragment
        # itself), but naming it after repo.name rather than borrowing
        # frags[0]'s name avoids attributing every fragment's placement to
        # whichever one happened to sort first.
        placed = resolve_placement(Selection(repo.name, frags), lab)
        if len(placed) != 1:
            raise ValueError(
                f"repo {repo.name!r}'s use-cases place onto {sorted(placed)} — "
                f"ambiguous for a per-repo verb; pass on=<host_id>."
            )
        candidate = next(iter(placed))

    if candidate not in lab.hosts:
        raise ValueError(
            f"Docker host {candidate!r} is not in lab {lab.name!r}. "
            f"Available hosts: {sorted(lab.hosts)}"
        )
    host = lab.hosts[candidate]
    if not isinstance(host, UnixHost):
        raise TypeError(f"Docker host {candidate!r} must be a UnixHost; got {type(host).__name__}")
    if not host.docker_capable:
        raise ValueError(
            f"Host {candidate!r} is not docker_capable. Mark it in lab.json with "
            f'"docker_capable": true.'
        )
    return host


async def _compose_cmd(
    parent: Host, project_name: str, files: list[str], action: str, *, extra: str = ""
) -> CommandResult:
    file_args = " ".join(f"-f {shlex.quote(f)}" for f in files)
    cmd = f"docker compose -p {shlex.quote(project_name)} {file_args} {action}"
    if extra:
        cmd += f" {extra}"
    # Unbounded on purpose: an image build/pull has no defensible bound, and a
    # made-up constant would be wrong on a slower builder. `inf` states that.
    return await parent.exec(cmd, timeout=float("inf"))


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
            rf"\[docker] could not tell whether {project_name} was already running on "
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
    for attempt in range(_CONTAINER_ID_RESOLVE_ATTEMPTS):
        result = await parent.exec(
            f"docker ps -q "
            f"--filter label=com.docker.compose.project={shlex.quote(project_name)} "
            f"--filter label=com.docker.compose.service={shlex.quote(service)}"
        )
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
                rf"\[docker] looking up {project_name}/{service} on {parent.id} "
                f"failed: {result.value}"
            )
        if attempt < _CONTAINER_ID_RESOLVE_ATTEMPTS - 1:
            await asyncio.sleep(_CONTAINER_ID_RESOLVE_BACKOFF_S)
    return None


async def compose_up(
    repo: Repo,
    lab: Lab,
    *,
    on: str | None = None,
    project_name: str | None = None,
    build: bool = True,
) -> dict[str, DockerContainerHost]:
    """Bring up *repo*'s compose stack on a parent host.

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
        build: When True (the default) and the repo declares
            ``[[docker.images]]``, run :func:`~otto.docker.build.build_images` first so locally-
            built images exist on the parent before compose tries to pull
            them. The build is idempotent via the context-hash skip, so this
            is cheap when nothing changed. Pass ``build=False`` if the
            compose file references only published images (or if you
            already built explicitly).

    Raises:
        ~otto.result.CommandNotRunError: this is a dry run. Bringing a stack
            up is the package's most consequential verb and its return type
            -- a dict of LIVE container hosts -- is the least able to say it
            did not happen. ``{}`` reads as "the stack registered nothing",
            which is a real and different outcome this function raises on.
    """
    settings = repo.docker_settings
    if not settings.composes:
        raise ValueError(f"Repo {repo.name!r} has no [[docker.composes]] entries; nothing to up.")

    parent = _resolve_parent(repo, lab, on)
    proj = project_name or get_user_compose_project(repo.name)

    # Below _resolve_parent, so a dry run still fails on an unknown host, a
    # non-UnixHost parent or one that is not docker_capable -- those refusals
    # are settled from configuration and must fire identically either way.
    # Above everything else, because everything else is a device touch or a
    # mutation of `lab.hosts`.
    if is_dry_run():
        raise CommandNotRunError(
            f"compose_up({repo.name}: {proj})",
            parent.id,
            "No image was built, no file was staged, no container was started "
            "and no host was registered.",
        )

    if build and settings.images:
        # Late import to avoid a circular `compose <-> build` import.
        from .build import build_images

        results = await build_images(repo, parent, rebuild=False)
        for name, res in results.items():
            if not res.is_ok:
                # value, not msg: the captured build output is the diagnosis.
                raise HostCommandError(
                    f"build for image {name!r} failed before compose up: {res.value}"
                )

    from .staging import stage_compose_files

    # Stage under the compose-project key (e.g. ``otto-repo1-vagrant`` or a
    # ``OTTO_COMPOSE_SUFFIX``-suffixed variant) rather than ``repo.name`` so
    # concurrent ``otto docker up`` invocations with different suffixes
    # don't ``rm -rf`` each other's compose dir mid-stage.
    remote_files = await stage_compose_files(parent, proj, list(settings.composes))
    remote_file_strs = [str(p) for p in remote_files]

    # `is True`, so an UNKNOWN answer (a failed probe) means this call is
    # treated as the owner and will roll back on a later failure — the
    # opposite default would strand a stack nobody claims. This is now used
    # ONLY for that ownership decision: `up -d` itself always runs below,
    # convergent regardless of what this probe answered.
    brought_up_here = await _stack_already_up(parent, proj) is not True
    try:
        return await _up_and_register(repo, lab, parent, proj, remote_file_strs)
    except BaseException:
        # Every raise below this point happens AFTER `up -d` has run, and the
        # caller cannot clean up what it never received: `composed()` arms its
        # try/finally only on a successful return, and a direct caller has no
        # handle either. Silently returning {} used to be accidentally safe
        # for `composed()` — it entered the try and tore the stack down — so
        # failing loud without this would be strictly worse than the bug.
        # Only what WE started; a stack that was already up is someone else's.
        if brought_up_here:
            await _rollback_partial_up(repo, lab, parent, proj)
        raise


async def _rollback_partial_up(repo: Repo, lab: Lab, parent: UnixHost, proj: str) -> None:
    """Best-effort teardown of a stack ``compose_up`` started but cannot hand over.

    Never raises and never masks: the caller is already propagating the real
    error, and a failed rollback must not replace it. It is reported, though —
    residue the user has to clean up by hand deserves to be named.
    """
    try:
        result = await compose_down(repo, lab, on=parent.id, project_name=proj)
    except Exception as e:  # noqa: BLE001 — a rollback may not mask the real error
        # .error, not .exception: the traceback the user needs belongs to the
        # error we are propagating, not to the rollback that failed after it.
        logger.error(  # noqa: TRY400 — see above
            rf"\[docker] {proj} could not be rolled back on {parent.id} and is still "
            f"up: {e}"
        )
        return
    if not result.is_ok:
        logger.error(
            rf"\[docker] {proj} could not be rolled back on {parent.id} and is still "
            f"up: {result.value}"
        )


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
    lab: Lab,
    parent: UnixHost,
    *,
    compose_project: str,
    id_project: str,
    services: "list[str]",
    users: "dict[str, str] | None" = None,
) -> "dict[str, DockerContainerHost]":
    """Resolve container ids and register hosts for *services* of one stack.

    ``id_project`` is the middle segment of the host id
    (``<parent>.<id_project>.<service>``): the repo name on the legacy per-repo
    path, the use-case name on the deploy path (spec §9).

    *users* is the declared per-service access user (``users = {...}`` in
    ``[[docker.composes]]``, merged across the composes in play by
    :func:`merge_declared_users`). A service absent from it simply registers
    with no declared user, deferring to the image's ``USER`` — the mapping is
    per service, never a stack-wide default.
    """
    hosts: dict[str, DockerContainerHost] = {}
    for service in services:
        cid = await _resolve_container_id(parent, compose_project, service)
        if not cid:
            logger.warning(
                rf"\[docker] could not resolve container id for {compose_project}/{service}; "
                f"skipping registration"
            )
            continue
        host = DockerContainerHost(
            parent=parent,
            container_id=cid,
            project=id_project,
            service=service,
            compose_project=compose_project,
            user=(users or {}).get(service),
        )
        # A container's lab is its PARENT's lab, never the lab it is registered
        # INTO: in a multi-lab session that lab is the composite ("a+b"), a name
        # no component owns and no `lab_patterns` entry matches — so the very
        # repo that declared this compose would stop seeing its own containers.
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
        # Register in the lab so otto host <id> finds it. compose_up is
        # idempotent and re-registers on every call — replacing a placeholder
        # from register_declared_container_hosts, or a prior compose_up's
        # entry for the same service (e.g. after a container restart changed
        # its id) — so this is an explicit delete-then-add rather than a
        # silent overwrite (add_host would reject the duplicate id outright).
        lab.hosts.pop(host.id, None)
        lab.add_host(host)
        hosts[service] = host

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
            f"above, then `docker compose -p {compose_project} logs` on {parent.id}"
        )

    return hosts


async def _up_and_register(
    repo: Repo,
    lab: Lab,
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
    settings = repo.docker_settings
    logger.info(rf"\[docker] composing {proj} on {parent.id} (convergent)")
    # `up -d` is convergent, so a transient libnetwork race (network
    # Created then reported "not found" when the container attaches) is
    # retried once with a brief backoff — the re-run starts the already-
    # created container cleanly. A genuine failure fails identically on
    # the retry and propagates, so this never masks a real error.
    #
    # Follow-up if this single retry doesn't stabilize `otto docker up`:
    # the tell is the RuntimeError below STILL reporting "network ... not
    # found" *after* the retry (i.e. attempt 1 raced too). That means the
    # parent daemon is degraded, not merely racing — pull `journalctl -u
    # docker` on the parent (the docker_capable host) around the failure.
    # Levers, roughly in order: widen the retry (range(2) -> range(3) and/or
    # a longer _NETWORK_RACE_RETRY_BACKOFF_S); a pre-`up` `docker network
    # prune` on the parent; or restart the daemon between runs.
    for attempt in range(2):
        up = await _compose_cmd(parent, proj, remote_file_strs, "up -d --remove-orphans")
        if up.is_ok:
            break
        if attempt == 0 and _is_transient_network_race(up.value):
            logger.debug(
                rf"\[docker] {proj} hit a transient network race on up; "
                f"retrying once after {_NETWORK_RACE_RETRY_BACKOFF_S}s"
            )
            await asyncio.sleep(_NETWORK_RACE_RETRY_BACKOFF_S)
            continue
        raise HostCommandError(f"docker compose up failed: {up.value}")

    # Enumerate services. Project-declared list is authoritative for the
    # mapping we return; cross-check against the live list and warn on drift.
    declared_services: list[str] = []
    for compose in settings.composes:
        declared_services.extend(compose.services)
    declared_services = list(dict.fromkeys(declared_services))  # dedupe, preserve order

    live = await _compose_cmd(parent, proj, remote_file_strs, "config", extra="--services")
    live_services: set[str] = set()
    if live.is_ok:
        live_services = {s.strip() for s in live.value.splitlines() if s.strip()}
        if declared_services and set(declared_services) != live_services:
            logger.warning(
                rf"\[docker] declared services {sorted(declared_services)} differ from "
                f"compose-listed services {sorted(live_services)} for {proj}"
            )
    elif declared_services:
        # A cross-check only: the declared list is authoritative, so losing the
        # comparison costs a warning, not the stack.
        logger.warning(
            rf"\[docker] could not list {proj}'s services to cross-check the declared "
            f"ones: {live.value}"
        )
    else:
        # Nothing declared AND nothing listed: `services` below would be empty,
        # the registration loop would not run, and compose_up would return {} —
        # which `otto docker up` prints as "0 container(s) registered" in green,
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
        # refusal as _resolve_parent's "no docker host specified".
        raise ValueError(
            f"compose stack {proj} is up on {parent.id} but names no services, so there "
            "is nothing to register — check the compose file's `services:` block"
        )

    return await register_stack_hosts(
        lab,
        parent,
        compose_project=proj,
        id_project=repo.name,
        services=services,
        users=merge_declared_users(repo.docker_settings.composes),
    )


async def compose_down(
    repo: Repo,
    lab: Lab,
    *,
    on: str | None = None,
    project_name: str | None = None,
    stop_timeout: int = 1,
) -> CommandResult:
    """Tear down *repo*'s compose stack and unregister its container hosts.

    *stop_timeout* is the per-container graceful-shutdown grace period in
    seconds passed to ``docker compose down --timeout``. Defaults to 1s
    rather than docker's default of 10s — otto's typical workload is
    integration tests with disposable stacks where waiting 10s on every
    teardown adds up fast (4 tests x 10s = 40s of wall time on the
    serialized ``docker_e2e`` group). Pass a larger value for stacks where
    graceful shutdown matters.

    Returns the ``docker compose down`` command's
    :class:`~otto.result.CommandResult`. A repo with no ``[[docker.composes]]``
    yields a ``Status.Skipped`` result that never ran (``retcode`` ``-1``).
    A failed tear-down is logged and returned, never raised — callers sweep
    the rest of their repos.

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
    settings = repo.docker_settings
    if not settings.composes:
        return CommandResult(Status.Skipped, value="", command="", retcode=-1)

    parent = _resolve_parent(repo, lab, on)
    proj = project_name or get_user_compose_project(repo.name)

    if is_dry_run():
        raise CommandNotRunError(
            f"compose_down({repo.name}: {proj})",
            parent.id,
            "No container was stopped and no host was unregistered from the lab.",
        )

    from .staging import stage_compose_files

    # See compose_up() for the staging-key rationale: keyed on the compose
    # project (suffix-aware) so concurrent stacks don't collide.
    #
    # Caught, not propagated: staging now raises when it cannot prepare its
    # dirs, and this function's contract is that a failed tear-down is
    # RETURNED. Letting it raise would stop `otto docker down` mid-sweep with
    # repos 2..n still up, and inside `composed()`'s finally it would replace
    # the body's real exception with teardown noise — the thing compensate()
    # exists to prevent.
    try:
        remote_files = await stage_compose_files(parent, proj, list(settings.composes))
    except RuntimeError as e:
        # .error, not .exception: this is returned as a failed CommandResult,
        # so the caller decides how loud to be about it.
        logger.error(  # noqa: TRY400 — see above
            rf"\[docker] cannot stage compose files to tear {proj} down: {e}"
        )
        return CommandResult(Status.Failed, value=str(e), command="", retcode=1)
    result = await _compose_cmd(
        parent,
        proj,
        [str(p) for p in remote_files],
        "down",
        extra=f"--timeout {int(stop_timeout)}",
    )
    if not result.is_ok:
        logger.error(rf"\[docker] compose down failed: {result.value}")

    # Unregister any hosts that came from this stack (shared with the
    # use-case teardown path, which sweeps `<parent>.<usecase>.` instead).
    await unregister_container_hosts(lab, f"{parent.id}.{repo.name.lower()}.")

    return result


async def compose_down_project(
    parent: UnixHost,
    compose_project: str,
    *,
    lab: Lab,
    remove_ids_under: "str | None",
    stop_timeout: int = 1,
) -> CommandResult:
    """Tear a merged stack down by PROJECT LABEL, then unregister its hosts.

    No ``-f`` on purpose: ``docker compose -p <proj> down`` finds the stack by
    the project label docker already carries, so a use-case teardown never
    has to re-run adapters, re-render templates or re-stage files merely to
    DELETE what they produced (spec §8). ``--remove-orphans`` is what makes a
    provider transition concrete — a displaced mock's container is an orphan
    of the merged file set and goes with the rest.

    *remove_ids_under* is the container-host id prefix to unregister
    (``<parent>.<usecase>.``), or ``None`` to unregister nothing.
    :func:`~otto.docker.deployment.deploy`'s rollback passes ``None``: it is
    compensating for a call that never got as far as registering anything, and
    popping ids it did not put there would yank a peer's registrations out of
    the lab.

    Never raises for a failed ``down``: the result is returned so a sweep over
    several hosts keeps going, exactly like :func:`compose_down`.

    Raises:
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

    result = await parent.exec(
        f"docker compose -p {shlex.quote(compose_project)} down --remove-orphans "
        f"--timeout {int(stop_timeout)}",
        timeout=float("inf"),
    )
    if not result.is_ok:
        logger.error(
            rf"\[docker] compose down failed for {compose_project} on {parent.id}: "
            f"{result.value}"
        )
    if remove_ids_under is not None:
        await unregister_container_hosts(lab, remove_ids_under)
    return result


async def unregister_container_hosts(
    lab: Lab, prefix: str, *, services: "list[str] | None" = None
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
                logger.warning(rf"\[docker] error closing container host {hid}: {e}")
    return drop


@contextlib.asynccontextmanager
async def composed(
    repo: Repo,
    lab: Lab,
    *,
    on: str | None = None,
    project_name: str | None = None,
    own: bool = False,
    build: bool = True,
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
    parent = _resolve_parent(repo, lab, on)
    proj = project_name or get_user_compose_project(repo.name)

    if is_dry_run():
        raise CommandNotRunError(
            f"composed({repo.name}: {proj})",
            parent.id,
            "No stack was brought up, so none was torn down either.",
        )

    # Only consulted when `own` is False (see the gate in the finally below),
    # so do not pay for the probe — or fail on it — when the caller has
    # already said it owns the stack.
    was_up = False
    if not own:
        probed = await _stack_already_up(parent, proj)
        if probed is None:
            raise HostCommandError(
                f"cannot tell whether {proj} was already running on {parent.id}, so "
                "composed() cannot promise to leave a peer's stack alone; pass "
                "own=True to tear down unconditionally"
            )
        was_up = probed

    hosts = await compose_up(repo, lab, on=on, project_name=proj, build=build)
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
                compose_down(repo, lab, on=on, project_name=proj),
                what=f"docker compose down {proj}",
            )


async def compose_ps(parent: Host) -> list[dict[str, Any]]:
    """Return a list of dicts describing running containers on *parent*.

    Uses ``docker ps --format '{{json .}}'`` so the output is structured.

    Best-effort by contract, like :func:`~otto.link.manage.read_link_states`:
    ``otto docker ps`` builds ONE table across every docker-capable host, so a
    single unreachable daemon must not hide the rest of the fleet. It does
    warn, though — an empty list is otherwise indistinguishable from a host
    that simply has no containers, which is the same silent-wrong shape this
    module is being swept for.

    The best-effort fold stops at a dry run's decline, for the reason the
    paragraph above already gives about the empty list. No arm at the top:
    ``docker ps`` IS this function's only device touch, so the refusal below
    already names the right thing, and letting the call reach the primitive
    keeps its ``[DRY RUN]`` announcement.

    Raises:
        ~otto.result.CommandNotRunError: this is a dry run.
    """
    result = await parent.exec("docker ps --format '{{json .}}'")
    refuse_declined_fact(result, asked=f"compose_ps({parent.id})")
    if not result.status.is_ok:
        logger.warning(
            rf"\[docker] could not list containers on {parent.id} — reporting none "
            f"for it: {result.value}"
        )
        return []
    out: list[dict[str, Any]] = []
    for raw_line in result.value.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            logger.debug(rf"\[docker] unparseable `docker ps` row on {parent.id}: {line!r}")
            continue
    return out


def register_declared_container_hosts(lab: Lab, repos: list[Repo]) -> int:
    """Pre-register *placeholder* container hosts in *lab* for every declared service triple.

    The placeholders carry an empty ``container_id`` so that any operation
    against a not-yet-up container fails with a clear "run `otto docker up`"
    message rather than a confusing not-found error. Once :func:`compose_up`
    (legacy repos) or :func:`~otto.docker.deployment.deploy` (use-case repos)
    runs, it overwrites the placeholder with a real entry containing the
    resolved container id.

    A repo that declares ``[[docker.use_cases]]`` fragments takes the
    USE-CASE branch below INSTEAD OF the legacy composes walk (never both):
    its placeholders carry the use-case name as the id's middle segment
    (``<parent>.<usecase>.<service>``, spec §9) rather than the repo name, so
    ``DockerContainerHost._auto_up`` can tell the
    two kinds apart and auto-start through the right pipeline. A repo with no
    ``use_cases`` keeps today's per-repo ids unchanged.

    Returns the number of placeholders registered.
    """
    count = 0
    for repo in repos:
        settings = repo.docker_settings
        if settings.use_cases:
            count += _register_use_case_placeholders(lab, repo)
            continue
        if not settings.composes:
            continue
        # Build a map of docker-capable parents in the lab, by id.
        capable: list[UnixHost] = [
            h for h in lab.hosts.values() if isinstance(h, UnixHost) and h.docker_capable
        ]
        if not capable:
            continue
        # Merged over the repo's WHOLE compose set, once, before the loop —
        # not per compose. Reading each entry's own `users` in the loop would
        # make the first compose declaring a service win (the duplicate-id
        # skip below discards the later placeholder), so a repo whose two
        # composes disagree would get a silent first-wins placeholder here and
        # a refusal from `compose_up`, which merges the same way this does.
        # One gate, one answer, both paths.
        users = merge_declared_users(settings.composes)
        for compose in settings.composes:
            # No per-compose placement any more (spec §14) — every
            # docker-capable host in the lab is a candidate parent
            # (pessimistic but stable; the actual bring-up picks one).
            for parent in capable:
                for service in compose.services:
                    placeholder = DockerContainerHost(
                        parent=parent,
                        container_id="",
                        project=repo.name,
                        service=service,
                        compose_project=get_user_compose_project(repo.name),
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
                    lab.add_host(placeholder)
                    count += 1
    return count


def _register_use_case_placeholders(lab: Lab, repo: Repo) -> int:
    """Best-effort use-case placeholders for one repo's fragments (spec §9).

    Each fragment is placed independently via
    :func:`~otto.docker.resolve.resolve_placement` — NOT via
    :func:`~otto.docker.resolve.select_fragments`, so the provider
    competition (spec §4) never runs here: a fragment that would lose its
    ``provides`` competition at deploy time still gets its own placeholder.
    That is deliberate (a placeholder exists so `otto host <id>` and
    completion see something before the first `deploy`), not a bug — its
    auto-up (``DockerContainerHost._auto_up``)
    deploys the ACTUAL winning use-case, which may register a different
    container under the same id and leave this one's id resolving to
    whatever the competition produced.

    Placement is best-effort PER FRAGMENT: one whose placement cannot be
    resolved from the lab that happens to be loaded right now (no host
    carries its role, an ambiguous scope, ...) — or whose ``composes``
    handles cannot be resolved to services — contributes no placeholder,
    and the rest of the repo's fragments are still tried. This walk runs at
    the start of every otto invocation, long before a caller names a
    specific use-case to bring up, so a fragment that cannot yet be placed
    is normal, not an error. :func:`~otto.docker.deployment.deploy` still
    refuses hard on the same conditions when the use-case is actually
    asked for — the single ``except UseCaseResolutionError`` below is what
    turns deploy's hard refusal into this walk's soft skip; there is no
    second, looser copy of the services-from-handles traversal here (see
    :func:`~otto.docker.deployment._declared`, delegated to below — the
    one walk that answers both services AND declared users).
    """
    # Function-scope: this walk runs in `cli/invoke.py`'s preamble on every
    # otto invocation, so a bare `otto docker --help` must not pay
    # deployment.py's or resolve.py's import cost (import budget). No cycle
    # at call time: `deployment` already imports from `compose` at module
    # scope, but by the time this function RUNS both modules are fully
    # loaded.
    from .deployment import _declared
    from .resolve import SelectedFragment, Selection, UseCaseResolutionError, resolve_placement

    count = 0
    for frag in repo.docker_settings.use_cases:
        sf = SelectedFragment(repo, frag)
        try:
            placed = resolve_placement(Selection(frag.name, [sf]), lab)
            declared = _declared([sf], report=False)
        except UseCaseResolutionError:
            continue
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

        for host_id in placed:
            parent = lab.hosts[host_id]
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
                lab.add_host(placeholder)
                count += 1
    return count


def get_container_host(host_id: str) -> DockerContainerHost:
    """Look up a registered container host by id. Raises if not present."""
    from ..config import get_lab

    lab = get_lab()
    host = lab.hosts.get(host_id)
    if not isinstance(host, DockerContainerHost):
        raise KeyError(
            f"No container host registered with id {host_id!r}. "
            f"Did you call `otto docker up` (or `compose_up`) first?"
        )
    return host
