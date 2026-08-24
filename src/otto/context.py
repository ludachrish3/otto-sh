"""otto's per-invocation runtime composition root.

Owns the active Lab, the per-invocation runtime flags, and the host lifecycle
scope. Propagated via a ContextVar so the bare module accessors
(otto.config.all_hosts/get_host) can stay zero-argument, while explicit
passing (OttoContext methods, open_context) is first-class.
"""

import asyncio
import functools
import logging
import re
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import asynccontextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, TypeVar, cast

from typing_extensions import Self

if TYPE_CHECKING:
    from pathlib import Path

    from .config.lab import Lab
    from .config.scope import ProjectScope
    from .host import Results, UnixHost
    from .host.remote_host import RemoteHost

T = TypeVar("T")

logger = logging.getLogger(__name__)

LIBRARY_LAB_NAME = "<library>"
"""Sentinel ``Lab.name`` for the minimal, host-less context a library caller
gets for free.

``otto.suite.run._session_context`` installs ``OttoContext(lab=Lab(name=LIBRARY_LAB_NAME))``
around a session when no context is already active (e.g. ``run_suite()``/
``run_selection()`` called outside ``async with otto.open_context(...)``). That
Lab carries no hosts, so any ``get_host()`` call inside such a run fails loud —
:meth:`OttoContext.get_host` checks this constant to append a hint pointing at
``open_context`` (see below) ONLY for that sentinel lab; a real, lab-backed
unknown-host error is untouched.

Lives here rather than in ``otto.suite.run`` (where the sentinel is actually
installed) because this is the lower module in the import graph: ``otto.suite``
already imports ``otto.context``, and ``otto.context`` must never import from
``otto.suite`` (that would cycle back through ``otto.suite.run``'s own
``from ..context import ...``). Defining the shared constant on the
already-imported side keeps both directions acyclic.
"""


class HostScope:
    """Owns hosts handed out during a command; closes any still-connected on exit.

    The deterministic backstop that replaces RemoteHost.__del__: a host created
    and passed around without an explicit ``async with`` is still closed when
    the scope exits. Registration is deduped by object identity; close() is
    assumed idempotent so an early per-host close and the sweep never collide.
    """

    def __init__(self) -> None:
        self._hosts: "list[RemoteHost]" = []

    def register(self, host: "RemoteHost") -> None:
        """Add *host* to the scope for deferred close on exit, deduplicating by identity."""
        if any(host is h for h in self._hosts):  # dedup by object identity
            return
        self._hosts.append(host)

    def rebuild_connections(self) -> None:
        """Drop per-loop connection state on every registered host.

        For hosts opened inside an inner pytest session (``otto test`` /
        ``run_suite``): their transports are bound to pytest's now-closed
        event loops, and no later loop can drive them — a cross-loop close
        only raises into the sweep's failure logging. Rebuilding (the same
        ``rebuild_connections`` pattern ``otto test --cov`` already uses to
        refresh hosts after ``pytest.main()`` returns) abandons the dead
        per-loop state so the post-run sweep closes only what the CURRENT
        loop actually owns. Real remote cleanup for suite-opened hosts
        belongs to the suite's own fixtures, on the loop that opened them.
        Hosts without the hook (fakes, minimal BaseHosts) are left as-is.
        """
        for host in self._hosts:
            rebuild = getattr(host, "rebuild_connections", None)
            if rebuild is not None:
                rebuild()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> None:
        # Close on the Host *contract* (idempotent close()), not the
        # RemoteHost-private ``_connected``: DockerContainerHost / LocalHost are
        # BaseHosts without ``_connected``, so treat a missing attr as "needs
        # closing" (close() no-ops when nothing is open).
        # Drain the list first: the lifecycle wrapper enters/exits this scope
        # once per asyncio.run, and a command may run several (suite pre/post
        # phases), so a swept host must not be re-closed by the next cycle.
        hosts, self._hosts = self._hosts, []
        remaining = [h for h in hosts if getattr(h, "_connected", True)]
        # Dependency-ranked sweep (chaos spec): a host that another registered
        # host names as its ``parent`` (DockerContainerHost documents
        # close-before-parent — its docker exec channel drains over the
        # parent's still-open transport) closes only after its dependents.
        # Within a rank closes run concurrently; failures are logged per host
        # — never silently swallowed — and never stop the remaining ranks.
        while remaining:
            parent_ids = {id(getattr(h, "parent", None)) for h in remaining}
            rank = [h for h in remaining if id(h) not in parent_ids]
            if not rank:
                rank = remaining  # parent cycle (impossible by construction): close all, don't spin
            results = await asyncio.gather(*(h.close() for h in rank), return_exceptions=True)
            for host, result in zip(rank, results, strict=True):
                if isinstance(result, BaseException):
                    logger.warning(
                        f"otto: closing host {getattr(host, 'id', host)!r} failed "
                        f"during scope sweep: {result!r}"
                    )
            closed = {id(h) for h in rank}
            remaining = [h for h in remaining if id(h) not in closed]


_active: ContextVar["OttoContext | None"] = ContextVar("otto_context", default=None)


def get_context() -> "OttoContext":
    """Return the active ``OttoContext``, raising ``RuntimeError`` if none is installed."""
    ctx = _active.get()
    if ctx is None:
        raise RuntimeError(
            "No active OttoContext. Inside the CLI this is built by the top-level "
            "callback; in a script wrap your work in `async with otto.open_context(...)`."
        )
    return ctx


def try_get_context() -> "OttoContext | None":
    """Return the active ``OttoContext``, or ``None`` if none is installed."""
    return _active.get()


def set_context(ctx: "OttoContext") -> "Token[OttoContext | None]":
    """Install *ctx* as the active context and return the reset token."""
    return _active.set(ctx)


def reset_context(token: "Token[OttoContext | None]") -> None:
    """Restore the context ContextVar to the value it held before the matching ``set_context``."""
    _active.reset(token)


_cli_token: "Token[OttoContext | None] | None" = None


def set_cli_context(ctx: "OttoContext") -> None:
    """Install *ctx* as the CLI invocation's context, remembering the reset token.

    The CLI installs the context from deep inside the Typer callback
    (``cli.invoke.ensure_lab_context``) while the natural reset point is the
    console-script entry's ``finally`` — the two can't share a stack frame, so
    the token lives module-side. One CLI invocation per process; tests that
    drive the app via CliRunner are covered by the autouse ContextVar
    snapshot fixture in tests/conftest.py either way.
    """
    global _cli_token  # noqa: PLW0603 — module-level singleton/cache
    _cli_token = set_context(ctx)


def reset_cli_context() -> None:
    """Undo :func:`set_cli_context` if it ran; safe to call unconditionally."""
    global _cli_token  # noqa: PLW0603 — module-level singleton/cache
    if _cli_token is not None:
        reset_context(_cli_token)
        _cli_token = None


# Deferred to here (rather than the top-of-file imports) on purpose: importing
# otto.host at module scope pulls in otto.host.interact, which imports
# try_get_context from this module at ITS module scope. Doing so before
# try_get_context is defined above would raise ImportError on a fresh
# `import otto.context` (circular import). Only a plain value is needed here,
# so the deferred position is enough — no need to push this to TYPE_CHECKING.
from .host.host import DEFAULT_COMMAND_TIMEOUT  # noqa: E402


def _flags_hiding_every_match(
    matched: "list[Any]",
    *,
    include_containers: bool,
    include_local: bool,
) -> "list[str]":
    """Name the membership flags that hold back EVERY host in *matched*.

    The prediction behind the second half of D6's empty-selection guard (see
    :meth:`OttoContext.all_hosts`): a pattern matched, and the walk is about to
    yield nothing anyway because container hosts and the built-in ``local``
    host are not fleet members unless asked for. Answering "which flag would
    admit them" is what lets the error name the one edit that fixes it.

    Short-circuits on the first host that IS a fleet member — one survivor
    means the walk is not empty, so there is nothing to explain and no reason
    to keep classifying the rest.

    Args:
        matched: The hosts the pattern fullmatched, taken from the same lab
            mapping the walk itself iterates.
        include_containers: The walk's container flag, as passed by the caller.
        include_local: The walk's ``local`` flag, as passed by the caller.

    Returns:
        The flag names — sorted, so the message is stable — that between them
        hid every match. Empty when at least one match survives the flags,
        which is also what an empty *matched* returns: no matches is the OTHER
        failure, and it is already spoken for by the plain D6 message.
    """
    from .host.docker_host import DockerContainerHost
    from .host.local_host import LocalHost

    hiding: set[str] = set()
    for host in matched:
        if not include_containers and isinstance(host, DockerContainerHost):
            hiding.add("include_containers")
        elif not include_local and isinstance(host, LocalHost):
            hiding.add("include_local")
        else:
            return []
    return sorted(hiding)


@dataclass
class OttoContext:
    """The active per-invocation runtime: chosen lab, runtime flags, and host lifecycle scope."""

    lab: "Lab"
    dry_run: bool = False
    log_command_output: bool = True
    output_dir: "Path | None" = None
    scope: HostScope = field(default_factory=HostScope)

    include_projects: tuple[str, ...] = ()
    """Repo names forced ACTIVE this invocation (``-I``), PEP-503-normalized ON READ.

    Populated from the root CLI callback via ``RootOptions``; empty for
    library contexts. Read only through :func:`otto.config.scope.active` —
    nothing else may re-derive activation from these tuples.

    Nothing normalizes on WRITE. This dataclass stays plain (no
    ``__post_init__``), so any caller may store whatever spelling it holds and
    the stored tuple is NOT guaranteed normalized;
    :func:`otto.config.scope.active` normalizes both the stored values and the
    queried name before comparing. The invariant is therefore enforced where it
    is read rather than merely asked for here — a docstring-only version would
    let ``exclude_projects=("My_Repo",)`` be silently ignored, which is an
    explicit switch failing OPEN.
    """

    exclude_projects: tuple[str, ...] = ()
    """Repo names forced INACTIVE this invocation (``-E``), PEP-503-normalized ON READ.

    Same write/read contract as :attr:`include_projects`. Read by
    :func:`otto.config.scope.active` for the verdict and by
    :func:`otto.config.scope.switched_off` for attribution.
    """

    def get_host(self, host_id: str, **overrides: Any) -> "UnixHost":
        """Look up *host_id* in the active lab, apply any keyword overrides, and register it."""
        from .config.fleet import _apply_option_overrides

        host = self.lab.resolve_handle(host_id)
        if host is None:
            # The sentinel LIBRARY_LAB_NAME lab is what run_suite()/run_selection()
            # install for a library caller with no active context (see
            # otto.suite.run._session_context) — it never carries hosts, so
            # get_host() always fails here. Point a caller who hits this at the
            # real fix (open_context) rather than leaving them staring at an
            # empty "Available: []". A normal, lab-backed miss is untouched.
            breadcrumb = (
                " — no lab is loaded; run inside 'async with otto.open_context(lab=...)'"
                if self.lab.name == LIBRARY_LAB_NAME
                else ""
            )
            raise KeyError(
                f"No host {host_id!r} in lab {self.lab.name!r}. "
                f"Available: {sorted(self.lab.hosts)}{breadcrumb}"
            )
        resolved = _apply_option_overrides(cast("Any", host), **overrides)
        self.scope.register(resolved)
        return cast("UnixHost", resolved)

    @functools.cached_property
    def scopes(self) -> "dict[str, ProjectScope]":
        """Each repo's resolved fleet of interest for this run (spec §5), computed once.

        Display and abort data — ``status --full`` renders it and
        :func:`~otto.config.scope.require_current_scope` refuses on it. Fleet
        iteration does NOT read the stored :attr:`~otto.config.scope.ProjectScope.universe`; it
        re-asks :func:`~otto.config.scope.repo_targets` per host through
        :func:`~otto.config.scope.scoped_ids`, so a container registered after
        this property was first touched is still scoped correctly.

        Lazy, not computed in ``__post_init__``, because resolving it needs the
        repos — and reaching them runs ``bootstrap()``. A context is built in
        plenty of places that never walk a fleet (library FD-model callers, the
        ``LIBRARY_LAB_NAME`` sentinel session, unit tests holding a hand-built
        ``Lab``), and making every one of them pay for — or fail on — a
        composition root they never asked for would be the wrong trade.

        An empty mapping is the honest answer whenever the repos cannot be
        reached, and it feeds the whole-lab fallback (§6): no declarations, no
        narrowing, today's behavior. That covers the sentinel lab explicitly
        and any bootstrap failure by catching it — a fleet walk must not be the
        surface that first reports a broken composition root, which the CLI
        entry already does with a message built for it.
        """
        from .config.scope import resolve_scopes
        from .host.builtin_hosts import BUILTIN_LOCAL_HOST_ID

        if self.lab.name == LIBRARY_LAB_NAME:
            return {}
        try:
            from .config import get_ordered_repos

            repos = get_ordered_repos()
        except Exception as exc:  # noqa: BLE001 — no repos reachable ⇒ no declarations ⇒ fallback
            logger.debug(f"otto: fleet scoping unavailable ({exc!r}); walking the whole lab")
            return {}
        scopes = resolve_scopes(
            repos,
            self.lab.component_names,
            self.lab.hosts,
            # `local` is the runner, never fleet — and its `source_lab` is
            # stamped with whichever component the CLI listed first, so keying
            # membership on that stamp would make `-l a+b` and `-l b+a` resolve
            # differently. `include_local=True` remains the walk's own opt-in.
            exclude_ids=frozenset({BUILTIN_LOCAL_HOST_ID}),
        )
        declared = [scope for scope in scopes.values() if scope.declared]
        if declared:
            # Once per context, and only when something actually narrowed: the
            # fallback is not news, and a line printed on every run is a line
            # nobody reads on the run that mattered.
            union = frozenset().union(*(scope.universe for scope in declared))
            excluded = sum(1 for scope in scopes.values() if scope.excluded)
            logger.info(
                f"fleet of interest: {len(union)} of {len(self.lab.hosts)} lab hosts "
                f"({len(scopes)} repos, {excluded} excluded)"
            )
        return scopes

    def _admissible_ids(self, owner: "str | None") -> "set[str]":
        """Host ids this run's fleet walks may reach, re-derived live for *owner*.

        The one place the two fleet surfaces get their base set from, so
        widening one cannot silently leave the other behind.

        Args:
            owner: ``Repo.name`` whose universe bounds the walk, or ``None``
                for the union across declaring repos.

        Returns:
            The admissible ids.

        Raises:
            otto.bootstrap.ProjectScopeError: Nothing is admissible while some
                repo declared a ``[project]`` scope (spec §10 row 5) — framed
                against *owner* when the walk was bound to one, so a repo whose
                own fleet is empty is not reported as the whole fleet's. Also
                when *owner* names a repo this run never resolved.
        """
        from .config.scope import require_nonempty_fleet, scoped_ids

        admissible = scoped_ids(self.lab.hosts, self.scopes, owner)
        require_nonempty_fleet(self.scopes, admissible, owner)
        return admissible

    def all_hosts(
        self,
        pattern: "re.Pattern[str] | None" = None,
        *,
        include_containers: bool = False,
        include_local: bool = False,
        _scope_owner: "str | None" = None,
        **overrides: Any,
    ) -> "Iterator[RemoteHost]":
        """Yield this run's fleet of interest, optionally narrowed by *pattern*.

        The base set is the ambient project universe (spec §6) — the hosts some
        repo's ``[project]`` declaration admits, re-derived live — and *not*
        the whole loaded lab. When no repo declares ``[project]``, that IS the
        whole loaded lab, so product-less projects are untouched.

        *pattern* selects a SUBSET of that base set with ``re.fullmatch``, never
        ``re.search`` (D6): ``sensor`` no longer selects ``sensor-1``; write
        ``sensor.*``. A pattern that fullmatches none of a NON-EMPTY base set
        raises :class:`~otto.config.scope.EmptySelectionError` rather than
        yielding nothing — a silently empty sweep is the one failure worse than
        a crash.

        A pattern that DID match and whose every match the two membership flags
        below then removed raises the same class, with the other of its two
        messages: it names the flag rather than the regex, because widening a
        regex that already matched is the wrong edit and the natural first
        guess. The base-set count both messages quote is taken before those
        flags, so the *denominator* still describes the whole walkable fleet.

        The built-in ``local`` host (the machine otto runs on, injected by
        ``load_lab`` for targeted ``otto host local`` use) is NOT part of the
        fleet: deploy/monitor/coverage sweeps must never silently operate on
        the runner itself, and it is not a ``RemoteHost``. Pass
        ``include_local=True`` to opt it in; ``get_host("local")`` always
        resolves it. Both flags apply AFTER scoping, which is what keeps
        ``include_local=True`` working under a declaration. Their exclusions
        stay silent when *pattern* is ``None``: an unnarrowed walk over a fleet
        that happens to hold only containers is the long-standing "empty lab,
        empty walk" case, not a selection anyone typed and got wrong.

        Args:
            pattern: Compiled regex fullmatched against each host's ``id``.
                ``None`` (the default) yields the whole base set.
            include_containers: Also yield
                :class:`~otto.host.docker_host.DockerContainerHost` entries.
            include_local: Also yield the built-in ``local`` host.
            _scope_owner: Internal. ``Repo.name`` whose universe bounds this
                walk; the repo-scoped context view supplies it, and a plain
                context leaves it ``None`` for the union. Underscored because
                it is a seam between context objects, not a call-site knob —
                which host set a walk gets must come from WHICH OBJECT the call
                goes through, never from an argument each call site remembers
                (spec §7). It also cannot be spelled ``owner``: that name is
                already a *method* kwarg forwarded to owner-accepting host verbs.
            **overrides: Per-call protocol/option overrides; see
                ``otto.config.fleet._apply_option_overrides``.

        Yields:
            RemoteHost: Each selected host, registered into the lifecycle scope.

        Raises:
            otto.config.scope.EmptySelectionError: *pattern* is not ``None`` and
                either fullmatches nothing in the base set, or fullmatches only
                hosts ``include_containers``/``include_local`` hold out of the
                walk. The instance's ``excluded_by`` tells the two apart.
            otto.bootstrap.ProjectScopeError: The base set is empty while some
                repo declared a ``[project]`` scope.
        """
        from .config.fleet import _apply_option_overrides
        from .config.scope import EmptySelectionError
        from .host.docker_host import DockerContainerHost
        from .host.local_host import LocalHost

        # Computed here rather than in a wrapper: this is a generator, so the
        # body runs at first `next()` — which is when the walk actually happens
        # and therefore when the lab's host mapping is the one being walked.
        selected = self._admissible_ids(_scope_owner)
        if pattern is not None and selected:
            # `and selected`: with an EMPTY base set the pattern is not what
            # went wrong, and blaming it would send the reader to fix a regex
            # over a lab that holds nothing to select. That case is already
            # spoken for — loudly by `_admissible_ids` when a repo declared a
            # scope, and deliberately silently otherwise, which is the
            # long-standing "an empty lab yields an empty walk" behavior every
            # lab-less CLI path (e.g. `otto cov get` reaching its own
            # validation) still depends on.
            base_size = len(selected)
            matched = {host_id for host_id in selected if pattern.fullmatch(host_id)}
            if not matched:
                raise EmptySelectionError(pattern.pattern, base_size)
            # The other way a selection ends up empty, and the one the count
            # above cannot see: the pattern matched, and the membership flags
            # below then removed every match. Reading the survivors off
            # `self.lab.hosts.values()` — the SAME mapping the yield loop walks,
            # in the same order — is what keeps this prediction and that loop
            # from ever disagreeing about who is a fleet member.
            hidden_by = _flags_hiding_every_match(
                [host for host in self.lab.hosts.values() if host.id in matched],
                include_containers=include_containers,
                include_local=include_local,
            )
            if hidden_by:
                raise EmptySelectionError(
                    pattern.pattern,
                    base_size,
                    excluded_by=hidden_by,
                    matched_size=len(matched),
                )
            selected = matched
        for host in self.lab.hosts.values():
            if host.id not in selected:
                continue
            if not include_containers and isinstance(host, DockerContainerHost):
                continue
            if not include_local and isinstance(host, LocalHost):
                continue
            resolved = _apply_option_overrides(cast("Any", host), **overrides)
            self.scope.register(resolved)
            yield resolved

    async def do_for_all_hosts(  # noqa: PLR0913 — wide host-dispatch API
        self,
        method: "Callable[..., Awaitable[T]]",
        *args: Any,
        pattern: "re.Pattern[str] | None" = None,
        concurrent: bool = True,
        include_containers: bool = False,
        include_local: bool = False,
        term: "str | None" = None,
        transfer: "str | None" = None,
        ssh_options: "Any" = None,
        telnet_options: "Any" = None,
        sftp_options: "Any" = None,
        scp_options: "Any" = None,
        ftp_options: "Any" = None,
        nc_options: "Any" = None,
        userland_options: "Any" = None,
        _scope_owner: "str | None" = None,
        **kwargs: Any,
    ) -> "dict[str, T | BaseException]":
        """Call *method* on every matching host and return a ``{host_id: result}`` mapping.

        When *concurrent* is ``True`` (default), all calls are gathered in
        parallel via ``asyncio.gather``; exceptions from individual hosts are
        captured as values rather than propagated. When ``False``, hosts are
        called sequentially and exceptions are likewise captured. Fleet
        membership follows :meth:`all_hosts` in full — the ambient project
        universe, ``pattern`` as a fullmatch within it, its empty-selection
        guard, and the ``local`` exclusion unless ``include_local=True``.

        ``_scope_owner`` is the same internal seam :meth:`all_hosts` documents
        and is forwarded verbatim. It is spelled with a leading underscore
        partly to stay out of ``**kwargs``, which is where a plain ``owner=``
        belongs: that one is forwarded to *method* for owner-accepting host
        verbs, and the two must never be the same word.
        """
        hosts = list(
            self.all_hosts(
                pattern=pattern,
                include_containers=include_containers,
                include_local=include_local,
                _scope_owner=_scope_owner,
                term=term,
                transfer=transfer,
                ssh_options=ssh_options,
                telnet_options=telnet_options,
                sftp_options=sftp_options,
                scp_options=scp_options,
                ftp_options=ftp_options,
                nc_options=nc_options,
                userland_options=userland_options,
            )
        )
        if concurrent:
            results = await asyncio.gather(
                *(method(h, *args, **kwargs) for h in hosts),
                return_exceptions=True,
            )
            return dict(zip([h.id for h in hosts], results, strict=True))
        out: dict[str, T | BaseException] = {}
        for h in hosts:
            try:
                out[h.id] = await method(h, *args, **kwargs)
            except BaseException as exc:  # noqa: PERF203,BLE001 — collect-results, intentionally catches all
                out[h.id] = exc
        return out

    async def run_on_all_hosts(  # noqa: PLR0913 — wide host-dispatch API
        self,
        cmds: "list[str] | str",
        pattern: "re.Pattern[str] | None" = None,
        concurrent: bool = True,
        timeout: float = DEFAULT_COMMAND_TIMEOUT,
        *,
        _scope_owner: "str | None" = None,
        include_containers: bool = False,
        term: "str | None" = None,
        transfer: "str | None" = None,
        ssh_options: "Any" = None,
        telnet_options: "Any" = None,
        sftp_options: "Any" = None,
        scp_options: "Any" = None,
        ftp_options: "Any" = None,
        nc_options: "Any" = None,
        userland_options: "Any" = None,
    ) -> "dict[str, Results | BaseException]":
        """Run one or more shell commands on every matching host and return a results mapping.

        Accepts a single command string or a list of commands executed in
        sequence on each host. Delegates concurrency and filtering to
        ``do_for_all_hosts``; exceptions from individual hosts are captured as
        values rather than propagated.

        ``_scope_owner`` is the internal seam :meth:`all_hosts` documents,
        forwarded verbatim so :class:`ProjectContextView` can bound this
        surface too. It narrows the FLEET and stamps nothing — ``host.run``
        takes no ``owner``, and a shell command belongs to whoever ran it.
        """
        cmd_list = [cmds] if isinstance(cmds, str) else cmds

        async def _run_list(host: "UnixHost") -> "Results":
            return await host.run(cmd_list, timeout=timeout)

        return await self.do_for_all_hosts(
            _run_list,
            pattern=pattern,
            concurrent=concurrent,
            _scope_owner=_scope_owner,
            include_containers=include_containers,
            term=term,
            transfer=transfer,
            ssh_options=ssh_options,
            telnet_options=telnet_options,
            sftp_options=sftp_options,
            scp_options=scp_options,
            ftp_options=ftp_options,
            nc_options=nc_options,
            userland_options=userland_options,
        )

    def for_repo(self, repo_name: str) -> "ProjectContextView":
        """Return this same context seen from *repo_name*'s side (spec §7).

        A facade, not a copy — see :class:`ProjectContextView`. Cheap enough to
        build per call; :func:`otto.project.actions.actions_for` builds one for
        every ``ProjectActions`` it constructs.

        Args:
            repo_name: ``Repo.name``. Not validated here: a name this run never
                resolved is refused by the first walk that goes through the
                view (:func:`otto.config.scope.scoped_ids`), where the resolved
                set is known and the message can list it. Validating at
                construction would make a view unbuildable in the contexts that
                legitimately have no scopes at all — a library context, an
                unavailable bootstrap — which are exactly the ones that walk
                the whole lab by design.

        Returns:
            The repo-scoped view.
        """
        return ProjectContextView(self, repo_name)


_OWNER_MISMATCH = """\
a fleet walk through repo '{repo}'s context view was passed owner={passed!r}.

A repo-scoped view supplies owner='{repo}' itself. Naming a different one asks
a repo-scoped object to act for another repo, which is the cross-repo bleed the
view exists to prevent -- and rewriting it to '{repo}' silently, as the safe
direction invites, would leave the caller believing they had acted on
{passed!r} while a healthy-looking results mapping said nothing.

Drop the owner= and let the view supply it, walk through the plain context if
the sweep really is host-global, or pass with_owner=False for a host verb that
takes no owner at all."""
"""The view's refusal for a walk that names an owner other than its own repo.

Narrow by design: an owner EQUAL to the view's repo is redundant, not wrong,
and passes through -- ``super().install()`` from a subclass that still spells
the old argument must keep working. What is refused is the disagreement.
"""


class ProjectContextView:
    """One repo's face on the live context: its fleet, and its owner stamp (spec §7).

    Returned by :meth:`OttoContext.for_repo` and handed to every
    :class:`~otto.project.actions.ProjectActions` by
    :func:`~otto.project.actions.actions_for`, so a repo's ``install()`` reads
    ``self.ctx.do_for_all_hosts(_dispatch_install)`` — no ``owner=``, no
    universe plumbing.

    THE POINT IS WHERE THE SCOPE COMES FROM. Both narrowings — which hosts a
    walk may reach, and which owner's products the host verb touches — are
    properties of the OBJECT the call goes through, never of arguments each
    call site has to remember. An argument is forgettable exactly once per new
    call site, and the failure mode of forgetting it is a walk that silently
    reaches further than it should: repo A's uninstall taking repo B's products
    off a host they share. Through this view that mistake is unspellable.

    A FACADE OVER THE SAME CONTEXT, not a copy. Everything else —
    :meth:`~OttoContext.get_host`, the runtime flags, the lifecycle scope,
    links, options — is delegated live by ``__getattr__``, so an
    ``output_dir`` set after the view was built still arrives, and a host
    handed out here registers into the ONE :class:`HostScope` that closes it.

    ``get_host`` is deliberately NOT narrowed (§6): explicit targeting beats
    scoping, and a repo naming a jump host it does not own must still reach it.

    Owner-less host verbs stay dispatchable through
    :meth:`do_for_all_hosts` by passing ``with_owner=False``. That opt-out is
    explicit rather than inferred from the callable's signature: signature
    sniffing would silently skip the stamp for anything it could not read
    (``functools.partial``, ``*args`` wrappers, a C-implemented double), which
    turns an owner-scoping failure into the quietest possible bug. Its cost is
    that dispatching an owner-less callable and forgetting the flag raises
    ``TypeError`` at the host — loud, per-host, and captured in the results
    mapping like any other host failure.
    """

    def __init__(self, ctx: "OttoContext", repo_name: str) -> None:
        self._ctx = ctx
        """The live context every unbound attribute is read from."""

        self._repo_name = repo_name
        """The repo this view acts for — the walk's bound universe and its owner stamp."""

    def __getattr__(self, name: str) -> Any:
        """Delegate anything this view does not override to the live context.

        ``__getattr__`` runs only after normal lookup fails, so ``_ctx`` (bound
        in ``__init__``) resolves from the instance dict and cannot recurse
        here.
        """
        return getattr(self._ctx, name)

    def all_hosts(self, *args: Any, **kwargs: Any) -> "Iterator[RemoteHost]":
        """Yield this repo's fleet of interest — :meth:`OttoContext.all_hosts`, bound.

        Every argument is forwarded unchanged; only the base set differs.
        Overriding this surface separately from :meth:`do_for_all_hosts` is
        load-bearing rather than tidy: ``status``/``is_clean``/``owns_products``
        read the iteration surface directly, and a view that bound only its
        dispatch seam would answer those about the whole union.
        """
        return self._ctx.all_hosts(*args, _scope_owner=self._repo_name, **kwargs)

    async def do_for_all_hosts(
        self,
        method: "Callable[..., Awaitable[T]]",
        *args: Any,
        with_owner: bool = True,
        **kwargs: Any,
    ) -> "dict[str, T | BaseException]":
        """Call *method* on this repo's fleet, with ``owner=`` supplied for it.

        Args:
            method: The dispatch helper, called as ``method(host, ...)``.
            *args: Positional arguments forwarded to *method*.
            with_owner: Whether to supply ``owner=<repo>`` to *method*. Pass
                ``False`` for a host verb that takes no owner — see the class
                docstring for why this is a flag and not a signature check.
            **kwargs: Forwarded to :meth:`OttoContext.do_for_all_hosts`, which
                splits its own walk knobs out and hands the rest to *method*.
                An ``owner`` here must equal this view's repo; see Raises.

        Returns:
            ``{host_id: result | exception}``, exactly as the context's own
            dispatch returns.

        Raises:
            otto.bootstrap.ProjectScopeError: *kwargs* names an ``owner`` other
                than this view's repo. Raised BEFORE the walk, because it is
                the caller's mistake and not a host's — nothing is contacted,
                and it never lands in the results mapping as a per-host value.
        """
        if with_owner:
            if "owner" in kwargs and kwargs["owner"] != self._repo_name:
                from .bootstrap import ProjectScopeError  # function-scope: import-light

                raise ProjectScopeError(
                    "",  # a caller mistake, so there is no settings.toml to send them to
                    _OWNER_MISMATCH.format(repo=self._repo_name, passed=kwargs["owner"]),
                )
            kwargs["owner"] = self._repo_name
        return await self._ctx.do_for_all_hosts(
            method, *args, _scope_owner=self._repo_name, **kwargs
        )

    async def run_on_all_hosts(
        self, *args: Any, **kwargs: Any
    ) -> "dict[str, Results | BaseException]":
        """Run commands on this repo's fleet — :meth:`OttoContext.run_on_all_hosts`, bound.

        Bound rather than delegated, because delegation would leave one
        unscoped fleet walk reachable on an object whose whole purpose is to
        bound them — and it is the surface a subclass reaches for when no host
        verb fits. No owner is stamped: ``host.run`` takes none.
        """
        return await self._ctx.run_on_all_hosts(*args, _scope_owner=self._repo_name, **kwargs)


@asynccontextmanager
async def open_context(
    *,
    lab: "Lab | str | list[str]",
    dry_run: bool = False,
    log_command_output: bool = True,
    search_paths: "list[Path] | None" = None,
) -> "AsyncIterator[OttoContext]":
    """Build, install, and tear down an OttoContext for library / script use.

    Pass a Lab, or a lab name / list of names to load via load_lab. On exit the
    host scope closes any still-connected hosts and the contextvar is reset.
    Does NOT run a reservation check — that is a CLI concern; a script that wants
    one calls otto.reservations.check_reservations explicitly.
    """
    from .bootstrap import bootstrap

    bootstrap()  # composition root — idempotent; registers user init-module components
    from .config import load_lab
    from .config.lab import Lab

    resolved_lab = lab if isinstance(lab, Lab) else load_lab(lab, search_paths or [])
    ctx = OttoContext(lab=resolved_lab, dry_run=dry_run, log_command_output=log_command_output)
    token = set_context(ctx)
    try:
        async with ctx.scope:
            yield ctx
    finally:
        reset_context(token)
