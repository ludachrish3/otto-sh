"""
Docker Compose orchestration: bring stacks up/down, register containers
as live hosts in the active Lab, and idempotently re-enter when the same
stack is already running.

The public surface (re-exported from :mod:`otto.docker`) is:

- :func:`compose_up` — bring a stack up; returns ``{service: DockerContainerHost}``.
- :func:`compose_down` — stop a stack and remove its container hosts from the lab.
- :func:`composed` — async context manager wrapping the above.
- :func:`compose_ps` — list running stacks on a parent.
- :func:`get_container_host` — lab lookup by id (typed convenience).
- :func:`get_user_compose_project` — name a stack so concurrent runs don't collide.
"""

from __future__ import annotations

import asyncio
import contextlib
import getpass
import json
import shlex
from collections.abc import AsyncIterator
from typing import Any

from ..configmodule.lab import Lab
from ..configmodule.repo import DockerCompose, Repo
from ..host.docker_host import DockerContainerHost
from ..host.host import Host
from ..host.unix_host import UnixHost
from ..logger import get_otto_logger
from ..models.settings import OttoEnvSettings
from ..utils import Status

logger = get_otto_logger()

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
    """True when ``up -d`` failed with the libnetwork "network created then
    not found on attach" race, which a convergent re-run gets past.

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
    # Fresh OttoEnvSettings() (not the get_env() singleton) so OTTO_COMPOSE_SUFFIX
    # is re-read each call — callers/tests set it per-invocation.
    raw_suffix = suffix or OttoEnvSettings().compose_suffix or _safe_username()
    return f"otto-{repo_name}-{raw_suffix}".lower()


def _safe_username() -> str:
    try:
        return getpass.getuser()
    except Exception:
        return "anon"


def _resolve_parent(
    repo: Repo, lab: Lab, on: str | None, composes: list[DockerCompose]
) -> UnixHost:
    """Pick a parent host for *repo*'s compose stack.

    Order: explicit *on* > the first compose entry's ``default_host`` > error.
    The chosen host must be ``docker_capable``.
    """
    candidate = on
    if candidate is None:
        for compose in composes:
            if compose.default_host:
                candidate = compose.default_host
                break
    if candidate is None:
        raise ValueError(
            f"No docker host specified for repo {repo.name!r}. "
            f"Pass on=<host_id>, or set default_host in [[docker.composes]]."
        )

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
            f"Host {candidate!r} is not docker_capable. Mark it in hosts.json with "
            f'"docker_capable": true.'
        )
    return host


async def _compose_cmd(
    parent: Host, project_name: str, files: list[str], action: str, *, extra: str = ""
) -> tuple[Status, str]:
    file_args = " ".join(f"-f {shlex.quote(f)}" for f in files)
    cmd = f"docker compose -p {shlex.quote(project_name)} {file_args} {action}"
    if extra:
        cmd += f" {extra}"
    result = await parent.oneshot(cmd, timeout=None)
    return result.status, result.output


async def _stack_already_up(parent: Host, project_name: str) -> bool:
    """Return True if any container is running under *project_name* on *parent*."""
    result = await parent.oneshot(
        f"docker ps -q --filter label=com.docker.compose.project={shlex.quote(project_name)}"
    )
    return result.status.is_ok and bool(result.output.strip())


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
    """
    for attempt in range(_CONTAINER_ID_RESOLVE_ATTEMPTS):
        result = await parent.oneshot(
            f"docker ps -q "
            f"--filter label=com.docker.compose.project={shlex.quote(project_name)} "
            f"--filter label=com.docker.compose.service={shlex.quote(service)}"
        )
        if result.status.is_ok:
            cid = result.output.strip().splitlines()
            if cid:
                return cid[0]
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

    Idempotent at the project-name level: if a stack with the same
    *project_name* is already running on *parent*, this becomes a lookup
    instead of a fresh ``up``. Either way, returns a dict mapping each
    declared service to its :class:`~otto.host.docker_host.DockerContainerHost`, with the hosts
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
    """
    settings = repo.docker_settings
    if not settings.composes:
        raise ValueError(f"Repo {repo.name!r} has no [[docker.composes]] entries; nothing to up.")

    parent = _resolve_parent(repo, lab, on, list(settings.composes))
    proj = project_name or get_user_compose_project(repo.name)

    if build and settings.images:
        # Late import to avoid a circular `compose <-> build` import.
        from .build import build_images

        results = await build_images(repo, parent, rebuild=False)
        for name, (status, msg) in results.items():
            if not status.is_ok:
                raise RuntimeError(f"build for image {name!r} failed before compose up: {msg}")

    from .staging import stage_compose_files

    # Stage under the compose-project key (e.g. ``otto-repo1-vagrant`` or a
    # ``OTTO_COMPOSE_SUFFIX``-suffixed variant) rather than ``repo.name`` so
    # concurrent ``otto docker up`` invocations with different suffixes
    # don't ``rm -rf`` each other's compose dir mid-stage.
    remote_files = await stage_compose_files(parent, proj, list(settings.composes))
    remote_file_strs = [str(p) for p in remote_files]

    if not await _stack_already_up(parent, proj):
        logger.info(f"[docker] composing {proj} on {parent.id}")
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
            status, output = await _compose_cmd(parent, proj, remote_file_strs, "up -d")
            if status.is_ok:
                break
            if attempt == 0 and _is_transient_network_race(output):
                logger.debug(
                    f"[docker] {proj} hit a transient network race on up; "
                    f"retrying once after {_NETWORK_RACE_RETRY_BACKOFF_S}s"
                )
                await asyncio.sleep(_NETWORK_RACE_RETRY_BACKOFF_S)
                continue
            raise RuntimeError(f"docker compose up failed: {output}")
    else:
        logger.info(f"[docker] {proj} already running on {parent.id}; reusing")

    # Enumerate services. Project-declared list is authoritative for the
    # mapping we return; cross-check against the live list and warn on drift.
    declared_services: list[str] = []
    for compose in settings.composes:
        declared_services.extend(compose.services)
    declared_services = list(dict.fromkeys(declared_services))  # dedupe, preserve order

    live_status, live_out = await _compose_cmd(
        parent, proj, remote_file_strs, "config", extra="--services"
    )
    live_services: set[str] = set()
    if live_status.is_ok:
        live_services = {s.strip() for s in live_out.splitlines() if s.strip()}
        if declared_services and set(declared_services) != live_services:
            logger.warning(
                f"[docker] declared services {sorted(declared_services)} differ from "
                f"compose-listed services {sorted(live_services)} for {proj}"
            )
    services = declared_services or sorted(live_services)

    hosts: dict[str, DockerContainerHost] = {}
    for service in services:
        cid = await _resolve_container_id(parent, proj, service)
        if not cid:
            logger.warning(
                f"[docker] could not resolve container id for {proj}/{service}; "
                f"skipping registration"
            )
            continue
        host = DockerContainerHost(
            parent=parent,
            container_id=cid,
            project=repo.name,
            service=service,
            compose_project=proj,
            resources=set(parent.resources),
        )
        # Register in the lab so otto host <id> finds it.
        lab.hosts[host.id] = host  # type: ignore[assignment]
        hosts[service] = host

    return hosts


async def compose_down(
    repo: Repo,
    lab: Lab,
    *,
    on: str | None = None,
    project_name: str | None = None,
    stop_timeout: int = 1,
) -> Status:
    """Tear down *repo*'s compose stack and unregister its container hosts.

    *stop_timeout* is the per-container graceful-shutdown grace period in
    seconds passed to ``docker compose down --timeout``. Defaults to 1s
    rather than docker's default of 10s — otto's typical workload is
    integration tests with disposable stacks where waiting 10s on every
    teardown adds up fast (4 tests × 10s = 40s of wall time on the
    serialized ``docker_e2e`` group). Pass a larger value for stacks where
    graceful shutdown matters.
    """
    settings = repo.docker_settings
    if not settings.composes:
        return Status.Skipped

    parent = _resolve_parent(repo, lab, on, list(settings.composes))
    proj = project_name or get_user_compose_project(repo.name)

    from .staging import stage_compose_files

    # See compose_up() for the staging-key rationale: keyed on the compose
    # project (suffix-aware) so concurrent stacks don't collide.
    remote_files = await stage_compose_files(parent, proj, list(settings.composes))
    status, output = await _compose_cmd(
        parent,
        proj,
        [str(p) for p in remote_files],
        "down",
        extra=f"--timeout {int(stop_timeout)}",
    )
    if not status.is_ok:
        logger.error(f"[docker] compose down failed: {output}")

    # Unregister any hosts that came from this stack. Close each container
    # host first so its persistent session and repeater drain cleanly while
    # the parent's connection is still alive.
    parent_id = parent.id
    prefix = f"{parent_id}.{repo.name.lower()}."
    drop = [hid for hid in lab.hosts if hid.startswith(prefix)]
    for hid in drop:
        host = lab.hosts.pop(hid, None)
        if host is not None:
            try:
                await host.close()
            except Exception as e:
                logger.warning(f"[docker] error closing container host {hid}: {e}")

    return status


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
    """
    parent = _resolve_parent(repo, lab, on, list(repo.docker_settings.composes))
    proj = project_name or get_user_compose_project(repo.name)

    was_up = await _stack_already_up(parent, proj)

    hosts = await compose_up(repo, lab, on=on, project_name=proj, build=build)
    try:
        yield hosts
    finally:
        if own or not was_up:
            await compose_down(repo, lab, on=on, project_name=proj)


async def compose_ps(parent: Host) -> list[dict[str, Any]]:
    """Return a list of dicts describing running containers on *parent*.

    Uses ``docker ps --format '{{json .}}'`` so the output is structured.
    """
    result = await parent.oneshot("docker ps --format '{{json .}}'")
    if not result.status.is_ok:
        return []
    out: list[dict[str, Any]] = []
    for line in result.output.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def register_declared_container_hosts(lab: Lab, repos: list[Repo]) -> int:
    """Pre-register *placeholder* container hosts in *lab* for every declared
    ``<parent>.<project>.<service>``.

    The placeholders carry an empty ``container_id`` so that any operation
    against a not-yet-up container fails with a clear "run `otto docker up`"
    message rather than a confusing not-found error. Once :func:`compose_up`
    runs, it overwrites the placeholder with a real entry containing the
    resolved container id.

    Returns the number of placeholders registered.
    """
    count = 0
    for repo in repos:
        settings = repo.docker_settings
        if not settings.composes:
            continue
        # Build a map of docker-capable parents in the lab, by id.
        capable: list[UnixHost] = [
            h for h in lab.hosts.values() if isinstance(h, UnixHost) and h.docker_capable
        ]
        if not capable:
            continue
        for compose in settings.composes:
            if compose.default_host:
                parents = [h for h in capable if h.id == compose.default_host]
            else:
                parents = capable
            for parent in parents:
                for service in compose.services:
                    placeholder = DockerContainerHost(
                        parent=parent,
                        container_id="",
                        project=repo.name,
                        service=service,
                        compose_project=get_user_compose_project(repo.name),
                        resources=set(parent.resources),
                    )
                    if placeholder.id in lab.hosts:
                        continue
                    lab.hosts[placeholder.id] = placeholder  # type: ignore[assignment]
                    count += 1
    return count


def get_container_host(host_id: str) -> DockerContainerHost:
    """Look up a registered container host by id. Raises if not present."""
    from ..configmodule import get_lab

    lab = get_lab()
    host = lab.hosts.get(host_id)
    if not isinstance(host, DockerContainerHost):
        raise KeyError(
            f"No container host registered with id {host_id!r}. "
            f"Did you call `otto docker up` (or `compose_up`) first?"
        )
    return host
