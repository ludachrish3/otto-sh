"""
Docker container host.

A :class:`~otto.host.docker_host.DockerContainerHost` satisfies the otto
:class:`~otto.host.host.Host` protocol by
delegating most operations through a *parent* host that runs the docker
daemon. ``oneshot`` becomes ``parent.oneshot("docker exec ...")``;
``get`` / ``put`` are two-step ``docker cp`` via the parent's filesystem;
``interact`` opens a PTY-backed ``docker exec -it`` over the parent's
existing SSH connection.

``run`` (and ``open_session`` / ``send`` / ``expect``) use a persistent
``docker exec -it <ctr> sh`` session multiplexed on the parent's SSH
connection — shell state (``cd``, env vars, shell vars) persists across
calls, matching :class:`~otto.host.local_host.LocalHost` and
:class:`~otto.host.unix_host.UnixHost`. ``oneshot`` stays stateless and concurrent-safe.

Persistent-shell support requires an SSH-based :class:`~otto.host.unix_host.UnixHost` parent.
Local-host parents and telnet parents are rejected at session-open time —
the per-call ``oneshot`` path still works against any parent.
"""

import asyncio
import shlex
from dataclasses import (
    dataclass,
    field,
)
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

from typing_extensions import override

from ..logger import get_logger
from ..logger.mode import LogMode
from ..result import CommandResult, Result
from ..utils import Arg, Status, cli_exposed
from .file_ops import PosixFileOps
from .host import BaseHost, Host, is_dry_run
from .privilege import PosixPrivilege
from .product import Product

if TYPE_CHECKING:
    import re

from .power import PowerController
from .session import Expect, HostSession, SessionManager, ShellSession, _DockerSshSession

logger = get_logger()


@dataclass(slots=True)
class DockerContainerHost(PosixPrivilege, PosixFileOps, BaseHost):
    """A Docker container exposed as a first-class otto host.

    Construction is normally done by :mod:`otto.docker.compose` after a
    successful ``docker compose up``; tests instantiate it directly with a
    mocked parent.
    """

    parent: "Host"
    """The lab host running the docker daemon. Owns auth, hop chain, and
    the SSH connection used to reach the daemon. Typed as
    :class:`~otto.host.host.Host` (the protocol) so the type-system surface stays narrow,
    but ``run`` / ``open_session`` / ``send`` / ``expect`` / ``interact``
    additionally require an SSH-based :class:`~otto.host.unix_host.UnixHost` at runtime —
    they open a persistent ``docker exec`` channel on the parent's
    asyncssh connection. ``oneshot`` and file transfer work against any
    parent."""

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

    name: str = field(default="", init=False)
    """Human-readable host name. Filled in ``__post_init__``."""

    id: str = field(default="", init=False)
    """Unique host id used as the key in ``Lab.hosts`` and on the CLI.
    Format: ``<parent_id>.<project>.<service>``."""

    is_virtual: bool = field(default=True, init=False)
    """Containers are always virtual by definition."""

    log: LogMode = field(default=LogMode.NORMAL, repr=False)
    """Standing per-host logging disposition. ``QUIET`` keeps this host's command
    I/O in ``verbose.log`` but off the console; ``NEVER`` redacts it everywhere
    (warnings/errors are unaffected)."""

    log_stdout: bool = field(default=True, repr=False)
    """Whether output is mirrored to stdout in addition to log files."""

    resources: set[str] = field(default_factory=set[str])
    """Reservation tags. Containers participate in the same reservation
    system as UnixHosts; the compose module typically copies the parent's
    tags so concurrent test runs serialize through reservations."""

    products: list[Product] = field(default_factory=list, repr=False)
    """Software-under-test deployed to this host. Default empty. See
    :attr:`~otto.host.host.BaseHost.products`."""

    power_control: "PowerController | None" = field(default=None, repr=False)
    """Always None — LocalHost/DockerContainerHost are not power-controlled."""

    _session_mgr: SessionManager = field(init=False, repr=False)
    """Manages the persistent shell session(s) inside the container. The
    underlying transport is a ``docker exec -it`` channel multiplexed on the
    parent's SSH connection; opening is lazy and gated on the parent being
    an SSH-based :class:`UnixHost`."""

    _ensure_lock: asyncio.Lock = field(init=False, repr=False)
    """Serializes :meth:`_ensure_running` so concurrent accesses to a
    down container trigger at most one auto-up."""

    def __post_init__(self) -> None:
        parent_id = getattr(self.parent, "id", getattr(self.parent, "name", "localhost"))
        self.id = f"{parent_id}.{self.project}.{self.service}".lower()
        self.name = f"{parent_id}:{self.service}"
        self._session_mgr = self._build_session_mgr()
        self._ensure_lock = asyncio.Lock()

    def _build_session_mgr(self) -> SessionManager:
        """Build a fresh SessionManager wired to this host.

        Called from :meth:`__post_init__` and :meth:`rebuild_connections`.
        """

        def _make_session() -> ShellSession:
            from .unix_host import UnixHost

            if not (isinstance(self.parent, UnixHost) and self.parent.term == "ssh"):
                term = getattr(self.parent, "term", None)
                raise NotImplementedError(
                    f"DockerContainerHost persistent shell requires an SSH-based "
                    f"UnixHost parent; got {type(self.parent).__name__}"
                    + (f" with term={term!r}" if term is not None else "")
                    + ". Use oneshot() with chained `&&` commands instead, or "
                    "configure an SSH-based parent."
                )
            return _DockerSshSession(
                conn_provider=self.parent._connections.ssh,  # noqa: SLF001 — intra-package access to parent host's _connections
                container_id_getter=lambda: self.container_id,
            )

        return SessionManager(
            name=self.name,
            log_command=self._log_command,
            log_output=self._log_output,
            session_factory=_make_session,
            oneshot_factory=self._oneshot_via_parent,
            creds=[],
            host_id=self.id,
        )

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
        id on ``self``. If not, auto-start the stack via :func:`compose_up`
        and re-resolve. ``compose_up`` is reached only on real-access paths
        — every dry-run path short-circuits on :func:`is_dry_run` before
        calling this method.
        """
        if self.container_id:
            return

        async with self._ensure_lock:
            # Double-checked: another waiter may have resolved it while we
            # were blocked on the lock.
            if self.container_id:
                return

            cid = await self._resolve_container_id()
            if not cid:
                cid = await self._auto_up()
            self.container_id = cid

    async def _resolve_container_id(self) -> str:
        """Return the running container id for this service, or ``""``."""
        result = await self.parent.oneshot(
            f"docker ps -q "
            f"--filter label=com.docker.compose.project={shlex.quote(self.compose_project)} "
            f"--filter label=com.docker.compose.service={shlex.quote(self.service)}"
        )
        if result.status.is_ok and result.value.strip():
            return result.value.strip().splitlines()[0]
        return ""

    async def _auto_up(self) -> str:
        """Bring the owning stack up and return this service's container id.

        Called when the container is declared but not running. Uses
        :func:`compose_up` with ``build=False`` so access never triggers an
        image rebuild — a missing image fails fast with an actionable error.
        """
        from ..configmodule import get_lab as _get_lab
        from ..configmodule import get_repos as _get_repos
        from ..docker.compose import compose_up

        logger.info(
            f"[docker] container {self.id!r} not running; "
            f"auto-starting stack {self.compose_project!r}"
        )
        repos = _get_repos()
        lab = _get_lab()
        repo = next((r for r in repos if r.name == self.project), None)
        if repo is None:
            raise RuntimeError(
                f"Container {self.id!r} is declared but not running, and no "
                f"repo named {self.project!r} is configured to auto-start it. "
                f"Run `otto docker up` for project {self.project!r} first."
            )

        try:
            hosts = await compose_up(
                repo,
                lab,
                on=self.parent.id,
                project_name=self.compose_project,
                build=False,
            )
        except Exception as e:
            raise RuntimeError(
                f"Container {self.id!r} is declared but not running, and "
                f"auto-start failed: {e}. Run `otto docker up` for project "
                f"{self.project!r} first."
            ) from e

        host = hosts.get(self.service)
        if host is None or not host.container_id:
            raise RuntimeError(
                f"Container {self.id!r} is declared but not running. "
                f"Auto-start of stack {self.compose_project!r} did not produce "
                f"a container for service {self.service!r}. Run `otto docker up` "
                f"for project {self.project!r} first."
            )
        return host.container_id

    async def _docker_exec(self, cmd: str, *, interactive: bool = False) -> str:
        """Build the ``docker exec`` invocation that runs *cmd* inside the container."""
        await self._ensure_running()
        flags = "-i" if not interactive else "-it"
        return f"docker exec {flags} {self.container_id} sh -c {shlex.quote(cmd)}"

    @override
    async def oneshot(
        self,
        cmd: str,
        timeout: float | None = None,
        log: LogMode = LogMode.NORMAL,
    ) -> CommandResult:
        """Run a single command in the container via the parent.

        Stateless and concurrent-safe — each call spawns a fresh
        ``docker exec``. ``run()`` is the stateful counterpart that
        preserves shell state across calls.
        """
        if is_dry_run():
            return self._dry_run_result(cmd)
        return await self._oneshot_via_parent(cmd, timeout, log=log)

    async def _oneshot_via_parent(
        self,
        cmd: str,
        timeout: float | None = None,
        log: LogMode = LogMode.NORMAL,
    ) -> CommandResult:
        """Wrap *cmd* in ``docker exec`` and dispatch through the parent."""
        wrapped = await self._docker_exec(cmd)
        result = await self.parent.oneshot(wrapped, timeout=timeout, log=self._effective_log(log))
        # Replace the wrapped command in the result so callers see what
        # they asked for, not the docker-exec wrapper.
        return CommandResult(
            status=result.status,
            value=result.value,
            command=cmd,
            retcode=result.retcode,
        )

    @override
    async def _run_one(
        self,
        cmd: str,
        expects: "list[Expect] | None" = None,
        timeout: float | None = 10.0,
        log: LogMode = LogMode.NORMAL,
    ) -> CommandResult:
        """Execute one command on the persistent in-container shell.

        Shell state (``cd``, env vars, shell vars) persists across calls,
        matching :class:`~otto.host.local_host.LocalHost` and
        :class:`~otto.host.unix_host.UnixHost`. Requires an SSH-based
        :class:`~otto.host.unix_host.UnixHost` parent.
        """
        if is_dry_run():
            return self._dry_run_result(cmd)
        await self._ensure_running()
        return await self._session_mgr.run_cmd(
            cmd, expects=expects, timeout=timeout, log=self._effective_log(log)
        )

    @override
    async def open_session(self, name: str) -> "HostSession":
        """Open a named persistent shell session inside the container."""
        if is_dry_run():
            self._log_command(f"[DRY RUN] open_session({name!r})")
        await self._ensure_running()
        return await self._session_mgr.open_session(name)

    @override
    async def send(self, text: str, log: LogMode = LogMode.NORMAL) -> None:
        """Send raw text to the container's persistent session."""
        effective = self._effective_log(log)
        if is_dry_run():
            if effective is not LogMode.NEVER:
                self._log_command(f"[DRY RUN] send({text!r})")
            return
        await self._ensure_running()
        await self._session_mgr.send(text, log=effective)

    @override
    async def expect(
        self,
        pattern: "str | re.Pattern[str]",
        timeout: float = 10.0,
    ) -> str:
        """Wait for a pattern in the container's session output stream."""
        if is_dry_run():
            self._log_command(
                "[DRY RUN] expect() skipped — pattern would never match without a live session"
            )
            return ""
        await self._ensure_running()
        return await self._session_mgr.expect(pattern, timeout)

    ####################
    #  Interactive shell
    ####################

    @override
    async def _interact(self, as_user: str | None = None) -> None:
        """Open an interactive shell inside the container via the parent's SSH conn.

        ``as_user`` (Task 9) is not supported here — a container exec has no
        login-proxy chain of its own; passing it raises loudly rather than
        being silently ignored.
        """
        if as_user is not None:
            raise NotImplementedError(
                f"{self.name}: --as-user is not supported on DockerContainerHost"
            ) from None
        # Importing here to keep this module importable without asyncssh.
        from .interact import run_ssh_login
        from .unix_host import UnixHost

        if not isinstance(self.parent, UnixHost):
            raise NotImplementedError(
                f"DockerContainerHost.interact() requires an SSH-based parent host; "
                f"got parent of type {type(self.parent).__name__}."
            )
        if self.parent.term != "ssh":
            raise NotImplementedError(
                f"DockerContainerHost.interact() requires parent.term == 'ssh'; "
                f"got {self.parent.term!r}. Telnet parents cannot tunnel an "
                f"interactive docker exec."
            )
        await self._ensure_running()

        conn = await self.parent._connections.ssh()  # noqa: SLF001 — intra-package access to parent host's _connections
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
        return Path(f"/tmp/otto-docker-stage/{container_id}")  # noqa: S108 — deliberate staging path

    @override
    @cli_exposed(success="Transfer complete.")
    async def put(
        self,
        src_files: Annotated[
            list[Path] | Path, Arg(variadic=True, elem_type=Path, help="Local file(s) to upload.")
        ],
        dest_dir: Path,
    ) -> Result:
        """Upload local files into the container.

        Two-step: ``parent.put`` to a per-container staging dir, then
        ``docker cp`` from there into the container. The staging dir is
        cleaned up unconditionally so a failed transfer doesn't leak.

        Returns a :class:`~otto.result.Result` whose ``value`` maps each source
        path (as passed) to its per-file outcome, matching
        :meth:`~otto.host.host.BaseHost.put`.
        """
        from .transfer import aggregate_transfer

        files = src_files if isinstance(src_files, list) else [src_files]
        if is_dry_run():
            return self._dry_run_transfer("PUT", files, dest_dir)
        await self._ensure_running()

        stage = self._stage_dir(self.container_id)
        try:
            mkdir = await self.parent.oneshot(f"mkdir -p {shlex.quote(str(stage))}")
            if not mkdir.status.is_ok:
                msg = f"failed to create staging dir on parent: {mkdir.value}"
                return aggregate_transfer({f: Result(Status.Error, msg=msg) for f in files})

            stage_result = await self.parent.put(files, stage)
            if not stage_result.is_ok:
                # Staged-but-not-copied files must not read as Success: the
                # batch aborted before any docker cp, so they never reached
                # the container. Keep failure entries; downgrade the rest.
                staged = stage_result.value or {}
                per_file = {}
                for f in files:
                    entry = staged.get(f, Result(Status.Error, msg="staging failed"))
                    if entry.is_ok:
                        entry = Result(
                            Status.Skipped,
                            msg="staged to parent; docker cp not attempted (staging batch failed)",
                        )
                    per_file[f] = entry
                return aggregate_transfer(per_file)

            per_file: dict[Path, Result] = {}
            for f in files:
                staged = stage / f.name
                cp = await self.parent.oneshot(
                    f"docker cp {shlex.quote(str(staged))} "
                    f"{shlex.quote(self.container_id)}:{shlex.quote(str(dest_dir))}"
                )
                if not cp.status.is_ok:
                    per_file[f] = Result(Status.Error, msg=f"docker cp failed: {cp.value}")
                else:
                    per_file[f] = Result(Status.Success, value=dest_dir / f.name)
            return aggregate_transfer(per_file)
        finally:
            await self.parent.oneshot(f"rm -rf {shlex.quote(str(stage))}")

    @override
    @cli_exposed(success="Download complete.")
    async def get(
        self,
        src_files: Annotated[
            list[Path] | Path,
            Arg(variadic=True, elem_type=Path, help="Remote file(s) to download."),
        ],
        dest_dir: Path,
    ) -> Result:
        """Download files from the container to the local machine.

        Two-step: ``docker cp`` from the container into a per-container
        staging dir on the parent, then ``parent.get`` to the local dir.

        Returns a :class:`~otto.result.Result` whose ``value`` maps each source
        path (as passed) to its per-file outcome, matching
        :meth:`~otto.host.host.BaseHost.get`.
        """
        from .transfer import aggregate_transfer

        files = src_files if isinstance(src_files, list) else [src_files]
        if is_dry_run():
            return self._dry_run_transfer("GET", files, dest_dir)
        await self._ensure_running()

        stage = self._stage_dir(self.container_id)
        try:
            mkdir = await self.parent.oneshot(f"mkdir -p {shlex.quote(str(stage))}")
            if not mkdir.status.is_ok:
                msg = f"failed to create staging dir on parent: {mkdir.value}"
                return aggregate_transfer({f: Result(Status.Error, msg=msg) for f in files})

            staged_paths: list[Path] = []
            for i, f in enumerate(files):
                staged = stage / f.name
                cp = await self.parent.oneshot(
                    f"docker cp {shlex.quote(self.container_id)}:{shlex.quote(str(f))} "
                    f"{shlex.quote(str(staged))}"
                )
                if not cp.status.is_ok:
                    # docker cp for one file failed — mark it and skip the rest,
                    # keyed by the source paths exactly as passed. files[:i] were
                    # already copied to parent staging but parent.get never runs
                    # for them (we return before that call) and the staging dir
                    # is removed in `finally`, so they never actually arrived
                    # locally: downgrade them to Skipped rather than omitting
                    # them, mirroring the put-path staging-downgrade above.
                    per_file: dict[Path, Result] = {
                        skipped: Result(
                            Status.Skipped, msg="staged but not fetched (later failure)"
                        )
                        for skipped in files[:i]
                    }
                    per_file[f] = Result(Status.Error, msg=f"docker cp failed: {cp.value}")
                    for skipped in files[i + 1 :]:
                        per_file[skipped] = Result(
                            Status.Skipped, msg="not attempted (earlier failure)"
                        )
                    return aggregate_transfer(per_file)
                staged_paths.append(staged)

            # parent.get keys its per-file dict by the staged paths; re-key it
            # back to the container source paths (as passed) so the caller sees
            # the keys it handed in.
            parent_result = await self.parent.get(staged_paths, dest_dir)
            staged_map = parent_result.value if isinstance(parent_result.value, dict) else {}
            fallback = Result(parent_result.status, msg=parent_result.msg)
            per_file = {f: staged_map.get(stage / f.name, fallback) for f in files}
            return aggregate_transfer(per_file)
        finally:
            await self.parent.oneshot(f"rm -rf {shlex.quote(str(stage))}")

    def rebuild_connections(self) -> None:
        """Drop any persistent session so the next call reopens it.

        Mirrors :meth:`~otto.host.unix_host.UnixHost.rebuild_connections` for the
        ``all_hosts() → host.rebuild_connections()`` pattern that ``otto
        test --cov`` uses to refresh hosts after pytest installs a new
        event loop. The container host doesn't own any raw transport
        (the parent does), but its ``_session_mgr`` may hold a
        ``ShellSession`` whose ``asyncssh`` process is bound to the old
        loop. Replacing the manager forces lazy re-opens against the
        parent's freshly-rebuilt SSH connection.
        """
        self._session_mgr = self._build_session_mgr()

    ####################
    #  Cleanup
    ####################

    @override
    async def close(self) -> None:
        """Tear down the persistent session.

        The parent's underlying connection is owned by the parent and is not
        closed here — but this host *must* close before its parent so the
        session's docker exec channel can drain cleanly.
        """
        await self._session_mgr.close_all()


__all__ = ["DockerContainerHost"]
