"""
Docker container host.

A :class:`DockerContainerHost` is a thin wrapper that satisfies the otto
:class:`Host` protocol by delegating every operation through a *parent*
host that runs the docker daemon. ``run`` / ``oneshot`` become
``parent.oneshot("docker exec ...")``; ``get`` / ``put`` are two-step
``docker cp`` via the parent's filesystem; ``interact`` opens a PTY-backed
``docker exec -it`` over the parent's existing SSH connection.

This avoids any duplication of connection/transport machinery. Hops "just
work" because the parent owns the hop chain — the container piggybacks.

Persistent shell state across separate ``run()`` calls is **not** preserved
in this MVP: each command spawns a fresh ``docker exec``. Multiple commands
inside a single ``run([...])`` call also each spawn a fresh exec — chain
with ``&&`` if they need to share state. A future version may layer a
persistent ``docker exec -i bash`` session on top of the parent's SSH
channel multiplexing.
"""

from __future__ import annotations

import shlex
from dataclasses import (
    dataclass,
    field,
)
from pathlib import Path
from typing import TYPE_CHECKING

from ..logger import getOttoLogger
from ..utils import CommandStatus, Status
from .host import BaseHost, Host, isDryRun
from .repeat import RepeatRunner

if TYPE_CHECKING:
    import re

    from .session import Expect, HostSession

logger = getOttoLogger()


@dataclass(slots=True)
class DockerContainerHost(BaseHost):
    """A Docker container exposed as a first-class otto host.

    Construction is normally done by :mod:`otto.docker.compose` after a
    successful ``docker compose up``; tests instantiate it directly with a
    mocked parent.
    """

    parent: 'Host'
    """The lab host running the docker daemon. Owns auth, hop chain, and
    the SSH connection used to reach the daemon. Typed as
    :class:`Host` (the protocol) so :class:`LocalHost` can also serve when
    the dev machine itself runs docker."""

    container_id: str
    """Docker container id or unique name. Resolved by
    :func:`otto.docker.compose.compose_up` via
    ``docker compose -p <proj> ps -q <service>``."""

    project: str
    """Owning project name (the repo's settings ``name``). Combined with
    *parent* and *service* to form the host id."""

    service: str
    """Compose service name (e.g. ``api``)."""

    compose_project: str
    """The ``-p`` value passed to ``docker compose`` for this stack. Stored
    so other commands (``logs``, ``ps``, ``down``) can scope correctly."""

    name: str = field(default='', init=False)
    """Human-readable host name. Filled in :meth:`__post_init__`."""

    id: str = field(default='', init=False)
    """Unique host id used as the key in ``Lab.hosts`` and on the CLI.
    Format: ``<parent_id>.<project>.<service>``."""

    is_virtual: bool = field(default=True, init=False)
    """Containers are always virtual by definition."""

    log: bool = field(default=True, repr=False)
    """Whether this host's command/output should appear in logs."""

    log_stdout: bool = field(default=True, repr=False)
    """Whether output is mirrored to stdout in addition to log files."""

    resources: set[str] = field(default_factory=set[str])
    """Reservation tags. Containers participate in the same reservation
    system as RemoteHosts; the compose module typically copies the parent's
    tags so concurrent test runs serialize through reservations."""

    _repeater: RepeatRunner = field(init=False, repr=False)
    """Periodic-task runner. Required by :class:`BaseHost`."""

    def __post_init__(self) -> None:
        parent_id = getattr(self.parent, 'id', getattr(self.parent, 'name', 'localhost'))
        self.id = f"{parent_id}.{self.project}.{self.service}".lower()
        self.name = f"{parent_id}:{self.service}"
        self._repeater = RepeatRunner(run_cmds=self.run)

    ####################
    #  Command execution
    ####################

    async def _ensure_running(self) -> None:
        """Make sure ``self.container_id`` points at a running container.

        When the host was created from declared settings (e.g. by
        :func:`register_declared_container_hosts` at lab-load time of any
        new ``otto`` invocation), ``container_id`` is initially the empty
        placeholder string. The compose-up that registered the *real*
        container id lives in another process's memory and isn't visible
        here.

        Resolve lazily: ask the parent for any container labeled with
        ``com.docker.compose.project={self.compose_project}`` and
        ``com.docker.compose.service={self.service}``. If found, cache the
        id on ``self``. If not, raise a clear "run `otto docker up`" error.
        """
        if self.container_id:
            return

        result = await self.parent.oneshot(
            f"docker ps -q "
            f"--filter label=com.docker.compose.project={shlex.quote(self.compose_project)} "
            f"--filter label=com.docker.compose.service={shlex.quote(self.service)}"
        )
        cid = result.output.strip().splitlines()[0] if result.status.is_ok and result.output.strip() else ""
        if not cid:
            raise RuntimeError(
                f"Container {self.id!r} is declared but not running. "
                f"Run `otto docker up` (or call compose_up()) for project "
                f"{self.project!r} first."
            )
        self.container_id = cid

    async def _docker_exec(self, cmd: str, *, interactive: bool = False) -> str:
        """Build the ``docker exec`` invocation that runs *cmd* inside the container."""
        await self._ensure_running()
        flags = '-i' if not interactive else '-it'
        return f"docker exec {flags} {self.container_id} sh -c {shlex.quote(cmd)}"

    async def oneshot(
        self,
        cmd: str,
        timeout: float | None = None,
    ) -> CommandStatus:
        """Run a single command in the container via the parent.

        Stateless and concurrent-safe — each call spawns a fresh
        ``docker exec``. For multi-step workflows that must share shell
        state, combine commands with ``&&`` in a single string.
        """
        if isDryRun():
            return self._dry_run_result(cmd)
        wrapped = await self._docker_exec(cmd)
        result = await self.parent.oneshot(wrapped, timeout=timeout)
        # Replace the wrapped command in the result so callers see what
        # they asked for, not the docker-exec wrapper.
        return CommandStatus(
            command=cmd,
            output=result.output,
            status=result.status,
            retcode=result.retcode,
        )

    async def _run_one(
        self,
        cmd: str,
        expects: 'list[Expect] | None' = None,
        timeout: float | None = 10.0,
    ) -> CommandStatus:
        """Execute one command; aliases :meth:`oneshot` (no persistent shell)."""
        if expects:
            raise NotImplementedError(
                "DockerContainerHost does not support expect-driven prompts in run(). "
                "Use parent.run() directly for interactive flows on the docker host, "
                "or open_session() once a persistent exec channel is implemented."
            )
        return await self.oneshot(cmd, timeout=timeout)

    async def open_session(self, name: str) -> 'HostSession':
        raise NotImplementedError(
            "DockerContainerHost.open_session is not implemented in this MVP. "
            "Use oneshot() / run() with chained commands; or call open_session() "
            "on the parent and run docker exec there."
        )

    async def send(self, text: str) -> None:
        raise NotImplementedError(
            "DockerContainerHost.send requires a persistent session, "
            "which is not implemented in this MVP."
        )

    async def expect(
        self,
        pattern: 'str | re.Pattern[str]',
        timeout: float = 10.0,
    ) -> str:
        raise NotImplementedError(
            "DockerContainerHost.expect requires a persistent session, "
            "which is not implemented in this MVP."
        )

    ####################
    #  Interactive shell
    ####################

    async def _interact(self) -> None:
        """Open an interactive shell inside the container via the parent's SSH conn."""
        # Importing here to keep this module importable without asyncssh.
        from .interact import run_ssh_login
        from .remoteHost import RemoteHost

        if not isinstance(self.parent, RemoteHost):
            raise NotImplementedError(
                f"DockerContainerHost.interact() requires an SSH-based parent host; "
                f"got parent of type {type(self.parent).__name__}."
            )
        if self.parent.term != 'ssh':
            raise NotImplementedError(
                f"DockerContainerHost.interact() requires parent.term == 'ssh'; "
                f"got {self.parent.term!r}. Telnet parents cannot tunnel an "
                f"interactive docker exec."
            )
        await self._ensure_running()

        conn = await self.parent._connections.ssh()
        # Pick a sensible default shell. /bin/sh is universal in Linux
        # containers; users can override by running `docker exec` directly
        # if they want bash.
        cmd = f"docker exec -it {shlex.quote(self.container_id)} /bin/sh"
        await run_ssh_login(conn=conn, host_name=self.name, command=cmd)

    ####################
    #  File transfer
    ####################

    @staticmethod
    def _stage_dir(container_id: str) -> Path:
        """Per-container staging directory on the parent filesystem."""
        return Path(f"/tmp/otto-docker-stage/{container_id}")

    async def put(
        self,
        src_files: 'list[Path] | Path',
        dest_dir: Path,
    ) -> tuple[Status, str]:
        """Upload local files into the container.

        Two-step: ``parent.put`` to a per-container staging dir, then
        ``docker cp`` from there into the container. The staging dir is
        cleaned up unconditionally so a failed transfer doesn't leak.
        """
        files = src_files if isinstance(src_files, list) else [src_files]
        if isDryRun():
            return self._dry_run_transfer("PUT", files, dest_dir)
        await self._ensure_running()

        stage = self._stage_dir(self.container_id)
        try:
            mkdir = await self.parent.oneshot(f"mkdir -p {shlex.quote(str(stage))}")
            if not mkdir.status.is_ok:
                return Status.Error, f"failed to create staging dir on parent: {mkdir.output}"

            stage_status, msg = await self.parent.put(files, stage)
            if not stage_status.is_ok:
                return stage_status, msg

            for f in files:
                staged = stage / f.name
                cp = await self.parent.oneshot(
                    f"docker cp {shlex.quote(str(staged))} "
                    f"{shlex.quote(self.container_id)}:{shlex.quote(str(dest_dir))}"
                )
                if not cp.status.is_ok:
                    return Status.Error, f"docker cp failed: {cp.output}"
            return Status.Success, ""
        finally:
            await self.parent.oneshot(f"rm -rf {shlex.quote(str(stage))}")

    async def get(
        self,
        src_files: 'list[Path] | Path',
        dest_dir: Path,
    ) -> tuple[Status, str]:
        """Download files from the container to the local machine.

        Two-step: ``docker cp`` from the container into a per-container
        staging dir on the parent, then ``parent.get`` to the local dir.
        """
        files = src_files if isinstance(src_files, list) else [src_files]
        if isDryRun():
            return self._dry_run_transfer("GET", files, dest_dir)
        await self._ensure_running()

        stage = self._stage_dir(self.container_id)
        try:
            mkdir = await self.parent.oneshot(f"mkdir -p {shlex.quote(str(stage))}")
            if not mkdir.status.is_ok:
                return Status.Error, f"failed to create staging dir on parent: {mkdir.output}"

            staged_paths: list[Path] = []
            for f in files:
                staged = stage / f.name
                cp = await self.parent.oneshot(
                    f"docker cp {shlex.quote(self.container_id)}:{shlex.quote(str(f))} "
                    f"{shlex.quote(str(staged))}"
                )
                if not cp.status.is_ok:
                    return Status.Error, f"docker cp failed: {cp.output}"
                staged_paths.append(staged)

            return await self.parent.get(staged_paths, dest_dir)
        finally:
            await self.parent.oneshot(f"rm -rf {shlex.quote(str(stage))}")

    ####################
    #  Cleanup
    ####################

    async def close(self) -> None:
        """No-op — the parent owns the connection. Stops any background
        tasks attached to this container host."""
        await self._repeater.stop_all()


__all__ = ["DockerContainerHost"]
