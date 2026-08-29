"""Remote-path shell completion for `otto host get` / `put`.

See docs/superpowers/specs/2026-08-06-remote-path-completion-design.md.
Completion-time rules: never raise, never print, fail closed to []; the
reservation gate runs before any lab contact; SSH hosts only (the listing
itself uses the host's exec seam, so telnet later means relaxing the gate,
not a new mechanism).
"""

import asyncio
import contextlib
import posixpath
import shlex
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..config.remote_completion_cache import ListingEntry

if TYPE_CHECKING:
    from collections.abc import Iterator

    import typer

LIST_DEADLINE_SECONDS = 2.0
"""Hard wall-clock budget for the remote ``ls``; a slower host completes nothing."""


@dataclass(frozen=True)
class SplitPath:
    """An in-progress path argument, split for completion."""

    directory: str
    """The directory whose contents complete the argument (may be ``~``)."""
    prefix: str
    """The typed basename fragment entries must start with."""


def split_incomplete(incomplete: str) -> SplitPath:
    """Split the shell's incomplete word into (directory to list, name prefix)."""
    if incomplete in ("", "~"):
        return SplitPath(directory="~", prefix="")
    if incomplete.endswith("/"):
        stripped = incomplete.rstrip("/") or "/"
        return SplitPath(directory=stripped, prefix="")
    head, tail = posixpath.split(incomplete)
    return SplitPath(directory=head or "~", prefix=tail)


def listing_command(directory: str) -> str:
    """Build the remote `ls` command for *directory*, safely quoted.

    ``-1`` one per line, ``-A`` dotfiles, ``-L`` dereference symlinks (a
    symlinked dir must complete as a dir), ``-p`` marks dirs with ``/`` —
    parsing is just "trailing slash = directory".  A leading ``~`` is
    rewritten to ``"$HOME"`` (unquoted) so the remote shell expands it;
    everything else goes through :func:`shlex.quote`.  ``~user`` forms are
    not supported and are quoted literally.
    """
    if directory == "~":
        quoted = '"$HOME"'
    elif directory.startswith("~/"):
        rest = directory[2:]
        quoted = '"$HOME"/' + shlex.quote(rest) if rest else '"$HOME"'
    else:
        quoted = shlex.quote(directory)
    return f"LC_ALL=C ls -1ALp -- {quoted}"


def parse_listing(stdout: str) -> "list[ListingEntry]":
    """Parse `ls -1ALp` output: one name per line, trailing ``/`` = directory."""
    entries: list[ListingEntry] = []
    for line in stdout.splitlines():
        if not line:
            continue
        if line.endswith("/"):
            entries.append(ListingEntry(name=line[:-1], is_dir=True))
        else:
            entries.append(ListingEntry(name=line, is_dir=False))
    return entries


def present(entries: "list[ListingEntry]", split: SplitPath, kind: str) -> "list[str]":
    """Turn listing entries into completion strings for the shell.

    ``kind="dir"`` offers only directories (`put` dest).  Dotfiles appear
    only when the typed prefix itself starts with ``.`` (shell convention).
    Directories get a trailing ``/`` so the shell descends instead of
    closing the word.
    """
    out: list[str] = []
    for e in entries:
        if kind == "dir" and not e.is_dir:
            continue
        if not e.name.startswith(split.prefix):
            continue
        if e.name.startswith(".") and not split.prefix.startswith("."):
            continue
        full = posixpath.join(split.directory, e.name)
        out.append(full + "/" if e.is_dir else full)
    return sorted(out)


####################
#  The context chain
####################


@dataclass(frozen=True)
class _ChainParams:
    """Everything the completer recovers from the Click context chain."""

    host_id: str
    hop: str
    term: "str | None"
    labs: "list[str]"
    as_user: "str | None"


def _collect_chain_params(ctx: "typer.Context") -> _ChainParams:
    """Walk the (possibly mocked) context chain for the params completion needs.

    The ``otto host`` group callback returns early under
    ``ctx.resilient_parsing``, so ``ctx.meta`` is empty during completion and
    this walk is the *only* source for these values.

    Same defensive, depth-capped walk as
    :func:`otto.cli.completers.selected_lab_names`: only a genuine ``dict``
    ``params`` counts, each key is taken from the innermost context that
    carries it, and a self-referential mock cannot loop forever.
    """
    found: dict[str, Any] = {}
    node: object = ctx
    for _ in range(25):
        if node is None:
            break
        params = getattr(node, "params", None)
        if isinstance(params, dict):
            for key in ("host_id", "hop", "term", "labs", "as_user"):
                if key not in found and key in params:
                    found[key] = params[key]
        node = getattr(node, "parent", None)
    labs = found.get("labs")
    return _ChainParams(
        host_id=found.get("host_id") or "",
        hop=found.get("hop") or "",
        term=found.get("term") or None,
        labs=[x for x in labs if isinstance(x, str)] if isinstance(labs, list) else [],
        as_user=found.get("as_user") or None,
    )


####################
#  The completer
####################


def remote_path_completer(ctx: "typer.Context", incomplete: str, kind: str = "any") -> "list[str]":
    """Completion source for remote path arguments on ``otto host get`` / ``put``.

    Fail-closed and silent: every error path returns ``[]``.  The reservation
    gate runs before any lab contact — before the host is even constructed —
    and ``-R`` never bypasses it (there is no channel for the loud-skip
    warning here, so the break-glass flag would be a silent one).

    *kind* is ``"any"`` for a source path and ``"dir"`` when only directories
    are meaningful (a ``put`` destination).
    """
    try:
        with _silenced():
            return _complete(ctx, incomplete, kind)
    except Exception:  # noqa: BLE001 — a completer that raises prints a traceback mid-TAB
        return []


@contextlib.contextmanager
def _silenced() -> "Iterator[None]":
    """Guarantee the "never prints" half of the contract for the block's duration.

    The root callback returns early under ``ctx.resilient_parsing``
    (:mod:`otto.cli.main`), so logging is never configured during completion —
    no handler is installed anywhere in the chain. Python then falls back to
    :data:`logging.lastResort`, which writes every ``WARNING``-and-above record
    straight to stderr, i.e. into the middle of the user's TAB.

    :class:`~otto.logger.mode.LogMode` cannot cover this: by its own definition
    it governs *command I/O* only, and ``logger.warning``/``logger.error`` are
    explicitly out of its reach. So the fallback itself has to go — attaching a
    :class:`logging.NullHandler` to the root logger stops ``lastResort`` for
    every logger in the process. Restored unconditionally, because
    ``remote_path_completer`` is also callable in-process from tests.
    """
    import logging

    handler = logging.NullHandler()
    root = logging.getLogger()
    root.addHandler(handler)
    try:
        yield
    finally:
        root.removeHandler(handler)


def _complete(ctx: "typer.Context", incomplete: str, kind: str) -> "list[str]":
    """Run the completer's real body, wrapped by :func:`remote_path_completer`'s catch-all."""
    chain = _collect_chain_params(ctx)
    if not chain.host_id or not chain.labs:
        return []
    if not _reservation_allows(chain):
        return []
    host, token = _load_host(chain)
    try:
        if getattr(host, "term", None) != "ssh":
            return []
        split = split_incomplete(incomplete)
        entries = _cached_listing_for(host.id, split.directory)
        if entries is None:  # `[]` is a cached-empty directory, not a miss
            fetched = _live_listing(host, split.directory)
            if fetched is None:  # the listing failed — cache nothing
                return []
            _store_listing_for(host.id, split.directory, fetched)
            entries = fetched
        return present(entries, split, kind)
    finally:
        _release_context(token)


####################
#  Seams: cache, host construction, live listing
####################


def _cached_listing_for(host_id: str, directory: str) -> "list[ListingEntry] | None":
    """Read the cached listing for ``(host_id, directory)``.

    ``None`` is a miss; ``[]`` is a cached *empty* directory.
    """
    from ..config.remote_completion_cache import cached_listing

    return cached_listing(host_id, directory, datetime.now(tz=timezone.utc))


def _store_listing_for(host_id: str, directory: str, entries: "list[ListingEntry]") -> None:
    """Record a freshly fetched listing (best-effort; the cache never fails a TAB)."""
    from ..config.remote_completion_cache import store_listing

    store_listing(host_id, directory, entries, datetime.now(tz=timezone.utc))


def _load_host(chain: _ChainParams) -> "tuple[Any, Any]":
    """Build the lab + host exactly as the real command would; return (host, ctx token).

    Purely local work — config parse and object construction. No connection is
    opened until the exec in :func:`_live_listing`. The returned token must be
    handed to :func:`_release_context`; the caller owns that in a ``finally``.

    Mirrors :func:`otto.cli.host.resolve_cli_host` (hop resolution then
    ``--term`` override-copy) so completion sees the same host the command
    will use.
    """
    from ..config import get_host, get_repos
    from ..config.fleet import _apply_option_overrides
    from ..context import OttoContext, set_context
    from .invoke import build_lab_from_repos

    repos = get_repos()
    lab = build_lab_from_repos(repos, chain.labs)
    token = set_context(OttoContext(lab=lab))
    try:
        host = get_host(chain.host_id)
        if chain.hop:
            host.hop = get_host(chain.hop).id
            host.rebuild_connections()
        if chain.term:
            host = _apply_option_overrides(host, term=chain.term)
    except Exception:
        _release_context(token)
        raise
    return host, token


def _release_context(token: "Any") -> None:
    """Restore the context the completer installed for its lab."""
    from ..context import reset_context

    reset_context(token)


def _live_listing(host: "Any", directory: str) -> "list[ListingEntry] | None":
    """Run the remote ``ls`` under the hard deadline; close the connection either way.

    Returns the parsed entries on success — ``[]`` for a genuinely empty
    directory — and ``None`` when the command failed. The caller must not cache
    a ``None``: a transient permission error or a wedged session would
    otherwise be stored as "this directory is empty" and served for the whole
    listing TTL. Same rule the reservation gate follows — only an answer the
    far end actually gave is ever cached.

    ``timeout`` is passed to ``exec`` as well as wrapped in ``wait_for`` so the
    host layer can unwind on its own terms, with ``wait_for`` as the backstop
    for a host that ignores its own deadline.

    The coroutine runs under :func:`otto.lifecycle.run_command`, never a bare
    ``asyncio.run`` (house rule: ``tests/unit/test_no_bare_asyncio_run.py``).
    Besides the interrupt policy, that buys the completer a second closer: the
    active :class:`~otto.context.OttoContext`'s host scope is entered for the
    duration and swept at loop exit, so the host :func:`_load_host` constructed
    is closed even if the explicit ``host.close()`` below fails. The explicit
    close stays — it is the only closer for a ``--term``-override *copy*, which
    :meth:`~otto.context.OttoContext.get_host` never registered — and double
    closing is safe: ``HostScope`` documents ``close()`` as idempotent, the
    sweep skips hosts whose ``_connected`` is already ``False``, and
    ``HostConnections.close`` clears each cached slot take-then-clear.

    ``teardown_deadline`` is pinned to :data:`LIST_DEADLINE_SECONDS` rather
    than otto's 10s command default: a TAB that already gave up on the listing
    must not then hold the shell for another ten seconds tearing the
    connection down. The bound only applies once teardown has been forced
    (interrupt or external cancellation), which is exactly the case where a
    completion process should be leaving.

    Runs at :attr:`~otto.logger.mode.LogMode.NEVER` — redacted from every sink
    — rather than ``QUIET``, which still keeps command I/O in ``verbose.log``.
    Note that ``LogMode`` governs command I/O only; :func:`_silenced` is what
    keeps non-command records off the user's terminal.
    """
    from ..lifecycle import run_command
    from ..logger.mode import LogMode

    async def _run() -> "Any":
        try:
            return await asyncio.wait_for(
                host.exec(
                    listing_command(directory),
                    timeout=LIST_DEADLINE_SECONDS,
                    log=LogMode.NEVER,
                ),
                timeout=LIST_DEADLINE_SECONDS,
            )
        finally:
            # Teardown is best-effort inside a completer: a connection that
            # refuses to close must not turn into a traceback mid-TAB.
            with contextlib.suppress(Exception):
                await host.close()

    try:
        result = run_command(_run(), teardown_deadline=LIST_DEADLINE_SECONDS)
    except SystemExit:
        # Narrow, deliberate: `run_command` answers an interrupt with
        # SystemExit(128+signum), a BaseException the module catch-all in
        # `remote_path_completer` does not (and must not) catch. A ^C mid-TAB
        # has to read as "no completions", not as a traceback — but only THIS
        # call's SystemExit is degraded; widening the catch-all to
        # BaseException would swallow KeyboardInterrupt and cancellation
        # everywhere else in the completer.
        return None
    if not getattr(result, "is_ok", False):
        return None
    return parse_listing(str(getattr(result, "value", "") or ""))


####################
#  The completion-side reservation gate
####################


def _required_for(chain: _ChainParams) -> "set[str]":
    """Every resource the selected lab requires, resolved without lab contact.

    Loads the lab a second time (:func:`_load_host` loads it again) so the
    gate stays self-contained and can run strictly first; the config files are
    hot in the page cache by then.

    What scopes the requirement is the explicit ``host_ids=`` argument, read
    off the context object directly — the same fleet the command gate uses
    (spec 2026-08-28 three-level-reservations §5), so completion never refuses
    a TAB over a resource the command it is completing would not demand.
    ``set_context`` is installed around that read for a different reason: it
    makes THIS lab the ambient one for everything reached from here, so a
    completion cannot resolve against whatever lab an outer context happened
    to hold; it is undone in the ``finally``.

    ``require_nonempty=False`` for the same reason the command gate uses it —
    an empty declared fleet is zero hosts in play, so the requirement is the
    lab-level set — and with one extra edge here: a raise would be swallowed
    by :func:`remote_path_completer`'s catch-all into an empty completion, so
    an abort on this path is not a loud failure but a dead TAB with no
    explanation anywhere.

    Every host the user NAMED joins the fleet, even when no repo declared it —
    the target and the ``--hop``. ``otto host <id> --hop <id>`` is deliberately
    unscoped (explicit targeting beats scoping), and :func:`_load_host` opens a
    session to the target THROUGH the hop, so a TAB about to contact either
    must demand that host's own slot. The hop is not a lesser target: reaching
    a fleet host through an unreserved jump box is still using the jump box.

    Through :meth:`~otto.config.lab.Lab.resolve_handle`, so a positional handle
    (``dut1`` for the first ``dut``) resolves to the id the command will
    actually contact rather than being dropped as an unknown host. That is a
    pure lookup over the mapping this function already built — it opens
    nothing, which is what keeps the gate strictly first. An unresolvable name
    answers ``None`` and is skipped: passing it on would be a ``ValueError``
    out of the walk, and :func:`remote_path_completer`'s catch-all would leave
    the user a dead TAB with no explanation.
    """
    from ..config import get_repos
    from ..context import OttoContext, reset_context, set_context
    from ..reservations.check import required_resources
    from .invoke import build_lab_from_repos

    lab = build_lab_from_repos(get_repos(), chain.labs)
    ctx = OttoContext(lab=lab)
    token = set_context(ctx)
    try:
        named = [lab.resolve_handle(h) for h in (chain.host_id, chain.hop) if h]
        host_ids = ctx.admissible_ids(require_nonempty=False) | {
            host.id for host in named if host is not None
        }
        return required_resources(lab, host_ids=host_ids)
    finally:
        reset_context(token)


def _reservation_allows(chain: _ChainParams) -> bool:
    """Completion-side reservation gate: cache first, then one live query.

    Completion-only: this cached gate exists for TAB latency. The command-time
    gate (:meth:`otto.reservations.check.ReservationGate.evaluate`) always
    queries the backend live and must never read this cache.

    Mirrors the command gate's semantics — the required set over the fleet of
    interest, a no-op when no ``[reservations]`` section is configured — with
    one deliberate difference: ``skip_reservation_check`` is hard-wired
    ``False`` because ``-R``'s loud warning has nowhere to go during
    completion, and a silent break-glass is not one.

    Backend failures propagate to :func:`remote_path_completer`'s catch-all
    (fail closed to ``[]``) and store nothing: only an answer the backend
    actually gave is ever cached.
    """
    from ..config import get_repos
    from ..config.remote_completion_cache import (
        cached_reservation_ok,
        store_reservation_set,
        store_reservation_windows,
    )
    from ..reservations import build_reservation_gate, is_null_backend
    from ..reservations.identity import resolve_username
    from ..reservations.protocol import SupportsReservationWindows

    repos = get_repos()
    if not any(getattr(r, "reservation_settings", None) for r in repos):
        return True

    required = _required_for(chain)
    if not required:
        return True

    username = resolve_username(chain.as_user).username
    now = datetime.now(tz=timezone.utc)
    cached = cached_reservation_ok(username, required, now)
    if cached is not None:
        return cached

    gate = build_reservation_gate(
        repos,
        as_user=chain.as_user,
        skip_reservation_check=False,
        cwd_fallback=Path.cwd(),
    )
    backend = gate.backend
    # THE shared predicate, not a third isinstance: this gate, the verdict in
    # check_reservations and the held column of ``otto reservation check`` all
    # have to reach the same conclusion about what backend "none" means, and
    # three spellings of it is how they drift apart.
    if backend is None or is_null_backend(backend):
        return True
    if isinstance(backend, SupportsReservationWindows):
        windows = backend.get_reservation_windows(username)
        store_reservation_windows(username, windows, now)
        active = {w.resource for w in windows if w.start <= now <= w.end}
        return required <= active
    held = backend.get_reserved_resources(username)
    store_reservation_set(username, held, now)
    return required <= held
