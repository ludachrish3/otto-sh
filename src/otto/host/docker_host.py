"""
Docker container host.

A :class:`~otto.host.docker_host.DockerContainerHost` satisfies the otto
:class:`~otto.host.host.Host` protocol by
delegating most operations through a *parent* host that runs the docker
daemon. ``exec`` becomes ``parent.exec("docker exec ...")``;
``get`` / ``put`` are two-step ``docker cp`` via the parent's filesystem;
``login`` opens a PTY-backed ``docker exec -it`` over the parent's
existing SSH connection.

``run`` (and ``open_session`` / ``send`` / ``expect``) use a persistent
``docker exec -it <ctr> sh`` session multiplexed on the parent's SSH
connection — shell state (``cd``, env vars, shell vars) persists across
calls, matching :class:`~otto.host.local_host.LocalHost` and
:class:`~otto.host.unix_host.UnixHost`. ``exec`` stays stateless and concurrent-safe.

Persistent-shell support requires an SSH-based :class:`~otto.host.unix_host.UnixHost` parent.
Local-host parents and telnet parents are rejected at session-open time —
the per-call ``exec`` path still works against any parent.
"""

import asyncio
import logging
import shlex
from dataclasses import (
    dataclass,
    field,
    replace,
)
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

from typing_extensions import override

from ..logger.mode import LogMode
from ..result import CommandNotRunError, CommandResult, Result
from ..utils import Arg, Opt, Status, cli_exposed
from .connections import teardown_step
from .dev_tool import DevTool
from .file_ops import PosixFileOps
from .host import BaseHost, Host, _validate_user, is_dry_run, refuse_declined_fact
from .inventory_ref import InventoryRef
from .lab_info import LabInfo
from .privilege import PosixPrivilege
from .product import Product

if TYPE_CHECKING:
    import re

from .power import PowerController
from .session import Expect, HostSession, SessionManager, ShellSession, _DockerSshSession
from .toolchain import Toolchain

logger = logging.getLogger(__name__)


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
    but ``run`` / ``open_session`` / ``send`` / ``expect`` / ``login``
    additionally require an SSH-based :class:`~otto.host.unix_host.UnixHost` at runtime —
    they open a persistent ``docker exec`` channel on the parent's
    asyncssh connection. ``exec`` and file transfer work against any
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

    has_bash: bool = True
    """Whether this container has a working ``bash`` a command can be tagged
    and exec'd through (``bash -c 'exec -a …'``). Tunnel discovery
    (:mod:`otto.tunnel.discovery`) scans only ``has_bash`` hosts. Defaults to
    ``True`` but is a normal settable field, not ``init=False`` — minimal
    container images (``alpine``, ``centos6``, …) may lack bash, so this must
    be overridable per container."""

    user: "str | None" = None
    """Declared default access user for this container (``users = {...}`` in
    ``[[docker.composes]]``): the identity ``exec``/``run``/``login``/``put``
    act as when the call doesn't name one. ``None`` defers to the image's
    ``USER``. Values go to docker verbatim (``root``, ``1000``, ``1000:1000``,
    ``name:group``)."""

    log: LogMode = field(default=LogMode.NORMAL, repr=False)
    """Standing per-host logging disposition. ``QUIET`` keeps this host's command
    I/O in ``verbose.log`` but off the console; ``NEVER`` redacts it everywhere
    (warnings/errors are unaffected)."""

    log_stdout: bool = field(default=True, repr=False)
    """Whether output is mirrored to stdout in addition to log files."""

    lab_info: LabInfo = field(default_factory=LabInfo, repr=False)
    """The resolved lab this host was registered into (copied from the parent for containers)."""

    resources: frozenset[str] = field(default_factory=frozenset, repr=False)
    """Always empty — a container is never a reservable unit (spec 2026-08-28
    three-level-reservations §3). Declared rather than inherited: ``BaseHost``
    is not a dataclass, so its bare annotation creates no attribute and no
    dataclass field, and every concrete host dataclass must therefore declare
    the contract's fields itself (R11) — a read before anything assigns one is
    an ``AttributeError`` the type checker cannot see."""

    element_resources: frozenset[str] = field(default_factory=frozenset, repr=False)
    """Always empty — a container belongs to no element, and the loader that
    stamps this never builds one. Declared for the same reason as
    :attr:`resources` above."""

    inventory_ref: InventoryRef = field(default_factory=InventoryRef, repr=False)
    """Inventory provenance; empty unless this host was resolved from a record."""

    debug_log_globs: list[str] = field(default_factory=list, repr=False)
    """Container paths/glob patterns ``get_debug_logs`` fetches. Default empty.
    See :attr:`~otto.host.host.BaseHost.debug_log_globs`."""

    products: list[Product] = field(default_factory=list, repr=False)
    """Software-under-test deployed to this host. Default empty. See
    :attr:`~otto.host.host.BaseHost.products`."""

    dev_tools: list[DevTool] = field(default_factory=list, repr=False)
    """Repo-internal tooling deployed to this host. Default empty. See
    :attr:`~otto.host.host.BaseHost.dev_tools`."""

    toolchain: Toolchain = field(default_factory=Toolchain, repr=False)
    """Toolchain for this container's products. Defaults to the image's
    system-installed tools. See :attr:`~otto.host.host.BaseHost.toolchain`."""

    power_control: "PowerController | None" = field(default=None, repr=False)
    """Always None — LocalHost/DockerContainerHost are not power-controlled."""

    _session_mgr: SessionManager = field(init=False, repr=False)
    """Manages the persistent shell session(s) inside the container. The
    underlying transport is a ``docker exec -it`` channel multiplexed on the
    parent's SSH connection; opening is lazy and gated on the parent being
    an SSH-based :class:`UnixHost`."""

    _pending_run_user: "str | None" = field(default=None, init=False, repr=False)
    """Per-call ``user`` of the ``run()`` attempt CURRENTLY in flight.

    A parameter for the imminent open and nothing more: ``_run_one`` sets it
    once the container is known to be up, and clears it in a ``finally`` the
    moment the attempt ends — success or failure alike. It is deliberately not
    a memory of "the last user asked for": an attempt that never opened a
    channel must leave nothing behind for the NEXT opener to pick up, and the
    next opener may be ``send``/``expect``, which have no ``user`` parameter to
    correct a stale one with. ``None`` resolving to the declared :attr:`user`
    via ``_effective_user`` is what makes those openers land on the declared
    default exactly as a plain ``run()`` would."""

    _bound_run_user: "str | None" = field(default=None, init=False, repr=False)
    """User the default run channel ACTUALLY opened as. Meaningless unless
    ``_run_user_bound`` — ``None`` is itself a valid binding (image USER)."""

    _run_user_bound: bool = field(default=False, init=False, repr=False)
    """Whether an open of the default run channel has been recorded.

    Written ONLY by :meth:`_record_run_channel_open`, which the channel itself
    calls once its transport is up; cleared by ``close``/``rebuild_connections``.
    Nothing that merely *intends* to reach the channel may set it."""

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

        def _make_session(*, record: bool = True) -> ShellSession:
            from .unix_host import UnixHost

            if not (isinstance(self.parent, UnixHost) and self.parent.term == "ssh"):
                term = getattr(self.parent, "term", None)
                raise NotImplementedError(
                    f"DockerContainerHost persistent shell requires an SSH-based "
                    f"UnixHost parent; got {type(self.parent).__name__}"
                    + (f" with term={term!r}" if term is not None else "")
                    + ". Use exec() with chained `&&` commands instead, or "
                    "configure an SSH-based parent."
                )
            return _DockerSshSession(
                conn_provider=self.parent._connections.ssh,  # noqa: SLF001 — intra-package access to parent host's _connections
                container_id_getter=lambda: self.container_id,
                user_getter=self._user_for_an_imminent_open,
                on_open=self._record_run_channel_open if record else None,
            )

        return SessionManager(
            name=self.name,
            log_command=self._log_command,
            log_output=self._log_output,
            session_factory=_make_session,
            # Named auxiliary sessions open as the same user the run channel
            # would open as right now, but record nothing — see
            # :meth:`open_session`.
            named_session_factory=lambda: _make_session(record=False),
            exec_factory=self._exec_via_parent,
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
        and re-resolve.

        :func:`compose_up` **starts a container, so every caller of this method
        owes a dry run an arm above its own call**, and the list of them is the
        invariant: ``_docker_exec`` (via ``_exec_via_parent``), ``_run_one``,
        ``send``, ``put``, ``get`` and ``open_session`` return a decline;
        ``_expect_one``'s caller (``BaseHost.expect``) raises above it; ``login``
        announces and returns. A new caller that forgets is a dry run that
        starts a container. This paragraph asserted the invariant before it was
        true: ``open_session`` logged ``[DRY RUN] open_session(...)`` and then
        called this method anyway, and ``login`` had no dry-run arm at all
        (both fixed 2026-08-15).
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

    async def is_running(self) -> bool:
        """Whether the backing container is live right now — side-effect-free.

        Unlike ``_ensure_running``, this NEVER auto-starts the stack: it
        is the read-only liveness probe for paths that must not require
        docker at all (tunnel discovery and manage — a declared-but-down
        container trivially carries no processes). A placeholder whose
        container turns out to be running caches the resolved id, so
        subsequent ``exec`` calls skip both probe and auto-up.

        Side-effect-free is not the same as dry-run-safe: it is a ``bool``
        answering a question about a device, which is exactly the shape a
        dry run has no honest value for, and ``False`` is the actionable
        fiction ("the container is down"). It therefore declines, from
        ``_resolve_container_id``. otto's own two callers no longer
        depend on that — ``otto.tunnel.discovery._device_running`` refuses
        one level above — so this is a LIBRARY-surface backstop, for a caller
        outside otto that has no funnel of its own.

        Raises:
            ~otto.result.CommandNotRunError: this is a dry run and the
                container id is not already cached.
        """
        if self.container_id:
            return True
        cid = await self._resolve_container_id(log=LogMode.QUIET)
        if cid:
            self.container_id = cid
            return True
        return False

    async def _resolve_container_id(self, log: LogMode = LogMode.NORMAL) -> str:
        """Return the running container id for this service, or ``""``.

        ``""`` is this method's whole vocabulary for "not running", so a dry
        run's decline cannot be folded into it: :meth:`is_running` turns it
        into ``False`` and reports a container down that was never asked
        about, and ``_ensure_running`` turns it into an ``_auto_up`` that
        STARTS ONE. The refusal is here rather than in each caller because
        the fabrication is born here -- one funnel, two callers, no drift.

        Raises:
            ~otto.result.CommandNotRunError: this is a dry run, so nothing
                about this container's state was measured.
        """
        result = await self.parent.exec(
            f"docker ps -q "
            f"--filter label=com.docker.compose.project={shlex.quote(self.compose_project)} "
            f"--filter label=com.docker.compose.service={shlex.quote(self.service)}",
            log=log,
        )
        refuse_declined_fact(result, asked=f"is_running({self.id})")
        if result.status.is_ok and result.value.strip():
            return result.value.strip().splitlines()[0]
        return ""

    async def _auto_up(self) -> str:
        """Bring the owning stack up and return this service's container id.

        Called when the container is declared but not running. Two routes,
        chosen by whether ``self.project`` names a declared use-case (spec
        §9) or a legacy per-repo compose: a placeholder registered by
        ``register_declared_container_hosts``'s use-case branch carries the
        use-case's name in ``project`` — the very field a legacy placeholder
        carries the REPO's name in — so this is the one place that has to
        tell the two apart before deciding what to bring up. Both routes use
        ``build=False`` so access never triggers an image rebuild — a
        missing image fails fast with an actionable error.

        If an active repo happens to share its name with a declared use-case,
        a LEGACY placeholder for that repo (``project`` = the repo's name)
        still routes to the use-case pipeline — the two placeholder shapes
        are told apart by this ONE field and the use-case check runs first.
        This is intentional (spec allows either namespace and does not
        reserve one from the other) rather than a bug to guard against here;
        a schema-level refusal of the collision, if wanted, belongs beside
        settings validation, not in this per-call routing.

        Raises:
            ~otto.result.CommandNotRunError: :func:`~otto.docker.deployment.deploy`
                or :func:`~otto.docker.compose.compose_up` declined, unwrapped.
                See the arms below for why it is spelled out.
        """
        from ..config import get_repos as _get_repos
        from ..docker.resolve import declared_use_cases

        repos = _get_repos()

        if self.project in declared_use_cases(repos):
            from ..docker.deployment import deploy

            logger.debug(
                rf"\[docker] container {self.id!r}: {self.project!r} routes to the "
                f"use-case auto-start pipeline (a same-named repo, if any, would "
                f"never reach here)"
            )
            logger.info(
                rf"\[docker] container {self.id!r} not running; "
                f"auto-starting use-case {self.project!r}"
            )
            try:
                stack = await deploy(self.project, build=False)
            except CommandNotRunError:
                # Same reasoning as the legacy branch's bare raise below:
                # `deploy` declines a dry run by raising `CommandNotRunError`
                # with its own resolved-plan message, and refiling that as a
                # generic "auto-start failed" would tell the operator a start
                # was attempted and failed when nothing was attempted at all.
                raise
            except Exception as e:
                raise RuntimeError(
                    f"Container {self.id!r} is declared but not running, and "
                    f"auto-start failed: {e}. Run `otto docker up {self.project}` "
                    f"first."
                ) from e

            # `stack.by_host`, NOT the flattened `stack.hosts`: a use-case can
            # span several parents, and two of them can legally declare the
            # same service name (`_declared_services` warns on the collision,
            # it does not refuse it). Reading the flattened map would hand
            # THIS container -- bound to `self.parent` for every subsequent
            # `docker exec` -- a container id that belongs to a DIFFERENT
            # parent, which fails far from here with a bare "no such
            # container" instead of this method's actionable refusal.
            by_parent = stack.by_host.get(self.parent.id, {})
            host = by_parent.get(self.service)
            cid = host.container_id if host is not None else ""
            if not cid:
                raise RuntimeError(
                    f"Container {self.id!r} is declared but not running. "
                    f"Auto-start of use-case {self.project!r} did not produce "
                    f"a container for service {self.service!r} on {self.parent.id}. "
                    f"Run `otto docker up {self.project}` first."
                )
            return cid

        from ..config import get_lab as _get_lab
        from ..docker.compose import compose_up

        logger.info(
            rf"\[docker] container {self.id!r} not running; "
            f"auto-starting stack {self.compose_project!r}"
        )
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
        except CommandNotRunError:
            # BEFORE the wide arm, which would otherwise STRIP THE TYPE:
            # `compose_up` declines a dry run by raising
            # `CommandNotRunError`, and refiling that as `RuntimeError("...
            # auto-start failed: ...")` tells the operator a start was
            # attempted and failed when nothing was attempted at all. It is
            # the last of the conversions the dry-run contract work removed
            # from `compose_down` and its siblings — see
            # docs/superpowers/specs/2026-08-15-dry-run-contract-design.md.
            # A fabricated failure is worse here than a missing guard would
            # be: it is indistinguishable from a real one, so the decline
            # stops being visible as a decline anywhere upstream.
            #
            # UNREACHABLE TODAY, and closed anyway. `_ensure_running` — the
            # only caller of `_auto_up` — asks `_resolve_container_id` first,
            # and that refuses a dry run outright (`refuse_declined_fact`), so
            # no dry run reaches this call. The premise is one plausible
            # refactor from changing: a cached container id, or any second
            # caller that skips the probe, and the wide arm below starts
            # fabricating.
            #
            # Bare `raise`, not a rewrap: the decline's own message already
            # names `compose_up(<repo>: <project>)` and says no image was
            # built, no file staged, no container started.
            raise
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

    def _effective_user(self, user: "str | None") -> "str | None":
        """Per-call beats declared; neither → ``None`` (the image's ``USER`` prevails)."""
        return user if user is not None else self.user

    def _user_for_an_imminent_open(self) -> "str | None":
        """Return the user a channel opening RIGHT NOW must run as.

        A LIVE default channel's own binding wins. That is what keeps a named
        auxiliary session landing beside the run channel rather than somewhere
        else: ``_pending_run_user`` is scoped to a single ``run()`` attempt and
        is already cleared by the time ``open_session`` is called, so reading it
        alone would open the named session as the declared default while the run
        channel sits on another user. Falling back to the pending intent covers
        the case this is really for — the run channel itself opening, which by
        definition happens when no live one exists.
        """
        if self._run_channel_is_bound:
            return self._bound_run_user
        return self._effective_user(self._pending_run_user)

    def _record_run_channel_open(self, user: "str | None") -> None:
        """Record that the default run channel just opened as *user*.

        THE ONLY WRITER of the bind record, and it is called from
        :meth:`~otto.host.session._DockerSshSession._open` — i.e. by the open
        itself, once the transport is up. Recording at the point of INTENT
        instead (in ``_run_one``, before ``run_cmd``) is what the first cut of
        this feature did, and it was wrong twice over: a channel that
        ``send()``/``expect()`` opened was never recorded at all, so a
        subsequent ``run(user=...)`` cheerfully "bound" a user the live shell
        was not running as; and an open that failed left a record that refused
        the next legitimate call.
        """
        self._bound_run_user = user
        self._run_user_bound = True

    def _forget_run_channel_binding(self) -> None:
        """Drop the bind record and the pending intent — the channel is gone.

        Both ``close`` and ``rebuild_connections`` end with no default channel,
        so the next open is free to pick any user; clearing the intent as well
        returns the host to its DECLARED default rather than silently carrying
        the last per-call user across a teardown.
        """
        self._pending_run_user = None
        self._bound_run_user = None
        self._run_user_bound = False

    @property
    def _run_channel_is_bound(self) -> bool:
        """Whether a LIVE default run channel exists whose user was recorded.

        Both halves matter. Without the record, a channel could be live with
        no idea what it opened as. Without liveness, a channel whose handshake
        failed after its transport came up — or one whose ``close`` raised
        partway — would keep refusing calls against a shell that is gone.
        """
        return self._run_user_bound and self._session_mgr.has_live_default_session

    async def _docker_exec(
        self, cmd: str, *, interactive: bool = False, user: "str | None" = None
    ) -> str:
        """Build the ``docker exec`` invocation that runs *cmd* inside the container."""
        await self._ensure_running()
        flags = "-i" if not interactive else "-it"
        u = f" -u {shlex.quote(user)}" if user is not None else ""
        return f"docker exec {flags}{u} {self.container_id} sh -c {shlex.quote(cmd)}"

    @override
    async def _exec_one(
        self,
        cmd: str,
        timeout: float,
        log: LogMode = LogMode.NORMAL,
        user: "str | None" = None,
    ) -> CommandResult:
        """Run a single command in the container via the parent.

        Stateless and concurrent-safe — each call spawns a fresh
        ``docker exec``. ``run()`` is the stateful counterpart that
        preserves shell state across calls.
        """
        return await self._exec_via_parent(cmd, timeout, log=log, user=user)

    async def _exec_via_parent(
        self,
        cmd: str,
        timeout: float,
        log: LogMode = LogMode.NORMAL,
        user: "str | None" = None,
    ) -> CommandResult:
        """Wrap *cmd* in ``docker exec`` and dispatch through the parent.

        Declines a dry run HERE rather than relabelling the parent's decline
        below, for three reasons that all point the same way. The parent
        returns a :class:`~otto.result.NotRunResult` whose ``value`` raises by
        contract, and ``dataclasses.replace`` reads every init field — so the
        relabel line, whose only job is cosmetic, would be where the contract
        fired. The parent's decline also names the ``docker exec`` WRAPPER, and
        a decline that names a command the caller never issued is the wrong
        error however it is delivered. And ``_docker_exec`` below calls
        ``_ensure_running``, which resolves (and can auto-start) the container
        on the daemon — a dry run must reach neither.

        Public ``exec``/``run`` short-circuit above this in ``BaseHost``, so
        today nothing arrives here under a dry run. This method is also
        ``SessionManager``'s ``exec_factory``, which is reached by a different
        route, and a seam that dispatches to a device answers for itself.
        SessionManager's exec_factory reaches here without a user — the
        declared default applies, which is the spec's effective-user rule,
        not an accident.
        """
        if is_dry_run():
            return self._dry_run_result(cmd, log)
        wrapped = await self._docker_exec(cmd, user=self._effective_user(user))
        result = await self.parent.exec(wrapped, timeout=timeout, log=self._effective_log(log))
        # Replace the wrapped command in the result so callers see what they
        # asked for, not the docker-exec wrapper. `replace` rather than a field
        # list so new CommandResult fields are carried through automatically.
        return replace(result, command=cmd)

    @override
    async def _run_one(
        self,
        cmd: str,
        timeout: float,
        expects: "list[Expect] | None" = None,
        log: LogMode = LogMode.NORMAL,
        user: "str | None" = None,
    ) -> CommandResult:
        """Execute one command on the persistent in-container shell.

        Shell state (``cd``, env vars, shell vars) persists across calls,
        matching :class:`~otto.host.local_host.LocalHost` and
        :class:`~otto.host.unix_host.UnixHost`. Requires an SSH-based
        :class:`~otto.host.unix_host.UnixHost` parent.

        The channel's user is BOUND, not per-call: ``docker exec -u`` is
        settled when the channel opens and cannot be renegotiated on a live
        shell. So this method does not bind anything — it declares an INTENT
        (:attr:`_pending_run_user`) and lets the open do the binding, via
        :meth:`_record_run_channel_open`. ``send()`` and ``expect()`` open the
        very same channel and set no intent, which resolves to the declared
        :attr:`user`; whichever call gets there first, the record describes the
        shell that is actually running.

        Against that record: if a LIVE channel opened as a different user than
        this call asks for, refuse — the alternative is silently answering as
        somebody else. If no channel is live, there is nothing to conflict
        with, so the call proceeds and its open binds. ``None`` (the image's
        ``USER``) is a binding like any other. :meth:`close` and
        :meth:`rebuild_connections` drop the channel and the record together.

        The intent is scoped to THIS attempt: set only once the container is
        known to be up, and dropped in a ``finally`` whether the command
        succeeded, failed, or never reached a shell. An attempt that dies
        before its channel opens must leave nothing behind, because the next
        thing to open the channel may be ``send``/``expect``, which take no
        ``user`` and so cannot correct a stale one — the residue would open
        the channel as a user this caller never successfully obtained and
        then refuse every plain ``run()`` against it.
        """
        if is_dry_run():
            return self._dry_run_result(cmd, log)
        effective = self._effective_user(user)
        if self._run_channel_is_bound and effective != self._bound_run_user:
            raise RuntimeError(
                f"{self.name}: the persistent run channel is bound to user "
                f"{self._bound_run_user!r} and this call asked for {effective!r} — "
                f"close() or rebuild_connections() to rebind"
            )
        await self._ensure_running()
        self._pending_run_user = user
        try:
            return await self._session_mgr.run_cmd(
                cmd, expects=expects, timeout=timeout, log=self._effective_log(log)
            )
        finally:
            self._pending_run_user = None

    @override
    async def open_session(self, name: str) -> "HostSession":
        """Open a named persistent shell session inside the container.

        The dry-run arm sits ABOVE ``_ensure_running()``, which is the whole
        point of it: the previous shape logged the ``[DRY RUN]`` line and then
        ran ``_ensure_running()`` anyway, so a dry run could resolve the
        container and — via ``_auto_up`` → ``compose_up`` — START ONE. The
        daemon is not asked, no container is started, and the handle is a
        :class:`~otto.host.session.DeclinedSession` — see
        ``BaseHost._dry_run_session``.

        **User:** a named session opens as whatever the run channel machinery
        would bind at the time this session opens — see
        ``_user_for_an_imminent_open``. In practice that is the LIVE run
        channel's own user if there is one, and the declared :attr:`user`
        otherwise. It READS that record but never writes it: opening a named
        session neither refuses because the run channel is on another user nor
        makes a later ``run(user=...)`` refuse. ``user`` is not a parameter
        here; a named session on a different user is out of scope.
        """
        if is_dry_run():
            return self._dry_run_session(name)
        await self._ensure_running()
        return await self._session_mgr.open_session(name)

    @override
    async def send(self, text: str, log: LogMode = LogMode.NORMAL) -> None:
        """Send raw text to the container's persistent session."""
        effective = self._effective_log(log)
        if is_dry_run():
            # The folded mode, not the default NORMAL: a dry run must not put a
            # send on the console that a real run keeps off it. No NEVER guard
            # here on purpose -- `_log_command` returns before it logs on NEVER,
            # and that is the ONE home for the decision; a second copy here
            # reads as redundant and gets deleted, taking the real one's twin
            # with it. Building the f-string first costs nothing that matters:
            # `text` is already a live `str` and `repr` only copies it.
            self._log_command(f"[DRY RUN] send({text!r})", effective)
            return
        await self._ensure_running()
        await self._session_mgr.send(text, log=effective)

    @override
    async def _expect_one(self, pattern: "str | re.Pattern[str]", timeout: float) -> str:
        """Wait for a pattern in the container's session output stream."""
        await self._ensure_running()
        return await self._session_mgr.expect(pattern, timeout)

    ####################
    #  Interactive shell
    ####################

    @override
    async def _login(self, user: "str | None" = None) -> None:
        """Open an interactive shell inside the container via the parent's SSH conn.

        ``user`` lands the shell as that identity (``docker exec -u``); the
        declared per-service default applies when the call names none, and
        the image's ``USER`` when neither is set.
        """
        # Importing here to keep this module importable without asyncssh.
        from .interact import run_ssh_login
        from .unix_host import UnixHost

        if not isinstance(self.parent, UnixHost):
            raise NotImplementedError(
                f"DockerContainerHost.login() requires an SSH-based parent host; "
                f"got parent of type {type(self.parent).__name__}."
            )
        if self.parent.term != "ssh":
            raise NotImplementedError(
                f"DockerContainerHost.login() requires parent.term == 'ssh'; "
                f"got {self.parent.term!r}. Telnet parents cannot tunnel an "
                f"interactive docker exec."
            )
        await self._ensure_running()

        conn = await self.parent._connections.ssh()  # noqa: SLF001 — intra-package access to parent host's _connections
        effective = self._effective_user(user)
        u = f" -u {shlex.quote(effective)}" if effective is not None else ""
        # /bin/sh is universal in Linux containers; users can override by
        # running `docker exec` directly if they want bash.
        cmd = f"docker exec -it{u} {shlex.quote(self.container_id)} /bin/sh"
        await run_ssh_login(conn=conn, host_name=self.name, command=cmd)

    ####################
    #  File transfer
    ####################

    @staticmethod
    def _stage_dir(container_id: str) -> Path:
        """Per-container staging directory on the parent filesystem."""
        return Path(f"/tmp/otto-docker-stage/{container_id}")  # noqa: S108 — deliberate staging path

    @override
    @cli_exposed(success="Transfer complete.", dry_run_preview=True)
    async def put(
        self,
        src_files: Annotated[
            list[Path] | Path, Arg(variadic=True, elem_type=Path, help="Local file(s) to upload.")
        ],
        dest_dir: Path,
        mode: Annotated[
            int | str | None,
            Opt(help="Octal permission bits for the uploaded file(s), e.g. 755, 0644, 0o4755."),
        ] = None,
        user: Annotated[
            str | None,
            Opt(
                help="chown landed files to this user inside the container "
                "(defaults to the declared service user)."
            ),
        ] = None,
    ) -> Result:
        """Upload local files into the container.

        Two-step: ``parent.put`` to a per-container staging dir, then
        ``docker cp`` from there into the container. The staging dir is
        cleaned up unconditionally so a failed transfer doesn't leak.

        *mode* is applied with a ``chmod`` **inside the container** after the
        copies land — deliberately not by stamping the staging copy and
        trusting ``docker cp`` to preserve it, which would put undocumented
        third-party behaviour in the trust path.

        *user*, once resolved through ``_effective_user`` (falling back to
        the declared service :attr:`user` when omitted), chowns the landed
        files to that identity after they land — ``chown`` itself runs as
        root, since the image's default user may not own what ``docker cp``
        just placed. ``chown <user>`` accepts every form ``chown`` itself
        accepts, verbatim: a name, a UID, or the ``UID:GID``/``name:group``
        owner:group spelling. A chown failure never fails silently: every
        landed file flips to an error entry naming the file, the user, and
        the reason.

        Returns a :class:`~otto.result.Result` whose ``value`` maps each source
        path (as passed) to its per-file outcome, matching
        :meth:`~otto.host.host.BaseHost.put`.
        """
        from .transfer import aggregate_transfer, chmod_command, parse_file_mode

        files = src_files if isinstance(src_files, list) else [src_files]
        if user is not None:
            _validate_user(user)
        if is_dry_run():
            return self._dry_run_transfer("PUT", files, dest_dir, mode)
        mode_check = parse_file_mode(mode)
        if not mode_check.is_ok:
            return aggregate_transfer({f: Result(Status.Error, msg=mode_check.msg) for f in files})
        resolved_mode: int | None = mode_check.value
        effective = self._effective_user(user)
        await self._ensure_running()

        stage = self._stage_dir(self.container_id)
        # Bound before the try: nothing evaluated inside the finally may
        # raise ahead of the teardown guard and mask the body's exception.
        host_name = self.name
        try:
            mkdir = await self.parent.exec(f"mkdir -p {shlex.quote(str(stage))}")
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
                # Unbounded on purpose: this command's duration IS the
                # transfer into the container, which scales with the file's
                # size — a wall-clock bound here is meaningless (see nc.py).
                cp = await self.parent.exec(
                    f"docker cp {shlex.quote(str(staged))} "
                    f"{shlex.quote(self.container_id)}:{shlex.quote(str(dest_dir))}",
                    timeout=float("inf"),
                )
                if not cp.status.is_ok:
                    per_file[f] = Result(Status.Error, msg=f"docker cp failed: {cp.value}")
                else:
                    per_file[f] = Result(Status.Success, value=dest_dir / f.name)
            # chown BEFORE chmod: chown-ing an already-chmod'd file clears
            # setuid/setgid (S_ISUID/S_ISGID) on most filesystems, which
            # would silently defeat a `mode="4755"` + `user=...` put. Running
            # chown first means the mode landed last is the one that sticks.
            # A side effect, still loud: a file whose chown just failed is no
            # longer Status.Success, so the chmod pass below skips it rather
            # than setting a mode on a file with admittedly-wrong ownership.
            if effective is not None:
                await self._chown_landed_as_root(per_file, effective)
            if resolved_mode is not None:
                landed = [
                    r.value for r in per_file.values() if r.status is Status.Success and r.value
                ]
                if landed:
                    if effective is not None:
                        # The effective user may not own what docker cp landed;
                        # only root can be relied on to chmod it.
                        wrapped = await self._docker_exec(
                            chmod_command(resolved_mode, landed), user="root"
                        )
                        chmod = await self.parent.exec(wrapped, timeout=float("inf"))
                    else:
                        # self.exec runs INSIDE the container (parent.exec would
                        # chmod the staging copy, about to be deleted).
                        chmod = await self.exec(chmod_command(resolved_mode, landed))
                    if not chmod.status.is_ok:
                        for f, r in per_file.items():
                            if r.status is Status.Success:
                                per_file[f] = Result(
                                    Status.Error,
                                    value=r.value,
                                    msg=(
                                        f"{f}: transferred, but setting mode "
                                        f"0o{resolved_mode:o} failed: "
                                        f"{chmod.value or f'chmod exited {chmod.retcode}'}"
                                    ),
                                )
            return aggregate_transfer(per_file)
        finally:
            with teardown_step(host_name, "staging-dir removal"):
                await self.parent.exec(f"rm -rf {shlex.quote(str(stage))}")

    async def _chown_landed_as_root(self, per_file: "dict[Path, Result]", effective: str) -> None:
        """Chown every landed file to *effective*, run as root inside the container.

        Root performs the chown because the effective user may not own what
        ``docker cp`` just placed. Mutates *per_file* in place: a failed
        chown is never silent — every still-``Success`` entry flips to
        ``Error``, naming the file, the user, and the reason.
        """
        landed = [r.value for r in per_file.values() if r.status is Status.Success and r.value]
        if not landed:
            return
        paths = " ".join(shlex.quote(str(p)) for p in landed)
        wrapped = await self._docker_exec(f"chown {shlex.quote(effective)} {paths}", user="root")
        chown = await self.parent.exec(wrapped, timeout=float("inf"))
        if chown.status.is_ok:
            return
        for f, r in per_file.items():
            if r.status is Status.Success:
                per_file[f] = Result(
                    Status.Error,
                    value=r.value,
                    msg=(
                        f"{f}: transferred, but chown to {effective!r} failed: "
                        f"{chown.value or f'chown exited {chown.retcode}'}"
                    ),
                )

    @override
    @cli_exposed(success="Download complete.", dry_run_preview=True)
    async def get(
        self,
        src_files: Annotated[
            list[Path] | Path,
            Arg(variadic=True, elem_type=Path, help="Remote file(s) to download."),
        ],
        dest_dir: Path,
        user: Annotated[
            str | None,
            Opt(
                help="Accepted for interface uniformity; reads are ownership-indifferent, "
                "so containers ignore it."
            ),
        ] = None,
    ) -> Result:
        """Download files from the container to the local machine.

        Two-step: ``docker cp`` from the container into a per-container
        staging dir on the parent, then ``parent.get`` to the local dir.

        *user* is accepted and validated but otherwise ignored: a read never
        changes what owns anything, so there is nothing to chown (spec §4).

        Returns a :class:`~otto.result.Result` whose ``value`` maps each source
        path (as passed) to its per-file outcome, matching
        :meth:`~otto.host.host.BaseHost.get`.
        """
        from .transfer import aggregate_transfer

        files = src_files if isinstance(src_files, list) else [src_files]
        if user is not None:
            _validate_user(user)
        if is_dry_run():
            return self._dry_run_transfer("GET", files, dest_dir)
        await self._ensure_running()

        stage = self._stage_dir(self.container_id)
        # Bound before the try: nothing evaluated inside the finally may
        # raise ahead of the teardown guard and mask the body's exception.
        host_name = self.name
        try:
            mkdir = await self.parent.exec(f"mkdir -p {shlex.quote(str(stage))}")
            if not mkdir.status.is_ok:
                msg = f"failed to create staging dir on parent: {mkdir.value}"
                return aggregate_transfer({f: Result(Status.Error, msg=msg) for f in files})

            staged_paths: list[Path] = []
            for i, f in enumerate(files):
                staged = stage / f.name
                # Unbounded on purpose: this command's duration IS the
                # transfer out of the container, which scales with the file's
                # size — a wall-clock bound here is meaningless (see nc.py).
                cp = await self.parent.exec(
                    f"docker cp {shlex.quote(self.container_id)}:{shlex.quote(str(f))} "
                    f"{shlex.quote(str(staged))}",
                    timeout=float("inf"),
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
            with teardown_step(host_name, "staging-dir removal"):
                await self.parent.exec(f"rm -rf {shlex.quote(str(stage))}")

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

        The run channel's user binding goes with the channel — a fresh
        manager opens a fresh ``docker exec``, so the next ``run()`` is free
        to name any user.
        """
        self._session_mgr = self._build_session_mgr()
        self._forget_run_channel_binding()

    ####################
    #  Cleanup
    ####################

    @override
    async def close(self) -> None:
        """Tear down the persistent session.

        The parent's underlying connection is owned by the parent and is not
        closed here — but this host *must* close before its parent so the
        session's docker exec channel can drain cleanly.

        Clearing the run channel's user binding is part of the teardown: the
        next ``run()`` reopens a channel and may bind a different user. It
        happens in a ``finally`` because a teardown that fails half-way still
        leaves no channel this host may claim to know the user of — the
        alternative is a raising ``close()`` that also poisons every later
        ``run(user=...)`` with a refusal.
        """
        try:
            await self._session_mgr.close_all()
        finally:
            self._forget_run_channel_binding()


__all__ = ["DockerContainerHost"]
