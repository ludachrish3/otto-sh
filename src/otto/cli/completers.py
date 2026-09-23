"""Shared shell-completion helpers used across CLI subapps.

Host-id completion must be lab-scoped everywhere host ids are accepted —
``otto host``, ``otto tunnel add --hosts``, ``otto docker --on`` (issue
#138) — so the lab-selection walk and the cache-then-live host-id resolution
live here rather than in any one subapp.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TypeVar

import typer

_F = TypeVar("_F", bound=Callable[..., Any])


def completion_source(**source: Any) -> Callable[[_F], _F]:
    """Declare how a completer's answer can be reproduced WITHOUT calling it.

    The completion shim (``otto._shim_complete``) answers a warm TAB from the
    cache alone, so every completer must say, in data, which payload key it
    reads and how it filters (spec §3.4). The tree serialiser
    (:mod:`otto.config.completion_tree`) copies this dict into the cached
    tree; a completer WITHOUT one is serialised ``live`` — a hand-over — and
    ``tests/unit/config/test_completion_tree.py`` fails by name, so a new
    completer cannot silently become a slow TAB. Change the completer's
    filter and this declaration together, or the shim's answer will differ
    from Typer's — the differential test is the net.

    Typer's ``compat_autocompletion`` wrapper additionally keeps only the
    values that start with the fragment; every source kind the shim knows
    filters by prefix already, so that extra filter is a no-op it mirrors
    for free.
    """

    def mark(fn: _F) -> _F:
        # Decorator attribute, read by the tree serialiser (otto.config.completion_tree).
        fn.__completion_source__ = dict(source)  # ty: ignore[unresolved-attribute]
        return fn

    return mark


def selected_lab_names(ctx: typer.Context) -> list[str]:
    """Return the lab(s) selected for this completion, or ``[]`` if none.

    ``-l``/``--lab`` (and its ``OTTO_LAB`` envvar) are declared on the *root*
    ``otto`` callback, not on the sub-group whose context the completer
    receives, so walk up the parent chain and return the first real ``labs``
    list found. Click populates that param from both the flag and the envvar —
    already split into a list — even during resilient (completion) parsing, so
    this single read covers every way a lab can be chosen.

    Defensive against non-Context objects (unit tests pass mocks): only a
    genuine ``dict`` ``params`` carrying a non-empty ``list`` of ``str`` counts,
    and the walk is depth-capped so a self-referential mock can't loop forever.
    """
    node: object = ctx
    for _ in range(25):
        if node is None:
            break
        params = getattr(node, "params", None)
        if isinstance(params, dict):
            labs = params.get("labs")
            if isinstance(labs, list) and labs and all(isinstance(x, str) for x in labs):
                return labs
        node = getattr(node, "parent", None)
    return []


def lab_scoped_host_ids(ctx: typer.Context) -> list[str]:
    """Every completable host id, scoped to the selected lab when one is chosen.

    When ``-l``/``--lab``/``OTTO_LAB`` names a lab, only that lab's hosts are
    returned (plus the always-present built-in hosts like ``local``); with no
    lab selected, the whole fleet is returned.

    Prefers the completion-cache entry populated by the slow path (same file
    that backs suite/instruction completion, wiped by ``otto cache clear``).
    Falls through to a live ``lab.json`` scan on cache miss so first-run
    completion still works.
    """
    from ..config import get_completion_names, get_repos
    from ..config.completion_cache import collect_host_ids

    labs = selected_lab_names(ctx)
    cached = get_completion_names()

    if labs:
        # Lab selected → offer only that lab's hosts. Prefer the per-lab cache
        # map; fall through to a live, lab-scoped scan on cache miss. The
        # built-in hosts belong to every lab, so seed them here (the buckets
        # store pure membership) — matching collect_host_ids' live behaviour.
        by_lab = cached.get("hosts_by_lab") if cached is not None else None
        if isinstance(by_lab, dict):
            from ..host.builtin_hosts import builtin_host_ids

            return sorted(
                set(builtin_host_ids()).union(
                    *(by_lab.get(lab, []) for lab in labs),
                )
            )
        return collect_host_ids(get_repos(), lab_names=labs)
    if cached is not None and isinstance(cached.get("hosts"), list):
        return list(cached["hosts"])
    return collect_host_ids(get_repos())


@dataclass(frozen=True)
class HostGroupRequest:
    """What ``otto host`` was given before the verb: the host id and any ``--term``."""

    host_id: str
    term: str | None


def host_group_request(ctx: typer.Context) -> HostGroupRequest:
    """Return the ``host_id`` and ``--term`` on ``otto host``, or ``HostGroupRequest("", None)``.

    The group callback returns early under ``ctx.resilient_parsing``, so
    ``ctx.meta`` is empty during completion and this walk — the same
    depth-capped, mock-tolerant shape as :func:`selected_lab_names` — is the
    only source. Each key is taken from the innermost context carrying it.
    """
    found: dict[str, object] = {}
    node: object = ctx
    for _ in range(25):
        if node is None:
            break
        params = getattr(node, "params", None)
        if isinstance(params, dict):
            for key in ("host_id", "term"):
                if key not in found and key in params:
                    found[key] = params[key]
        node = getattr(node, "parent", None)
    host_id = found.get("host_id")
    term = found.get("term")
    host = host_id if isinstance(host_id, str) else ""
    scoped_term = term if isinstance(term, str) and term else None
    return HostGroupRequest(host_id=host, term=scoped_term)


def filter_logins(
    entries: list[dict[str, Any]], *, flavour: str, term: str | None, incomplete: str
) -> list[str]:
    """Return the logins a verb offers from one host's ``logins_by_host`` entries, sorted.

    ``flavour="direct"`` drops proxied logins (what ``has_direct_cred`` refuses);
    a typed *term* keeps only creds scoped to it or unscoped (what ``cred_for``
    selects). A login may appear under several protocol-scoped entries — one
    entry per login in the result. The shim (``otto._shim_complete._host_logins``)
    mirrors this function — change both, and the differential test is the net.
    """
    out = []
    for e in entries:
        if flavour == "direct" and e.get("proxy"):
            continue
        protocols = e.get("protocols") or []
        if term and protocols and term not in protocols:
            continue
        login = str(e.get("login", ""))
        if login and login.startswith(incomplete):
            out.append(login)
    return sorted(set(out))


def host_user_completer(flavour: str) -> Callable[[typer.Context, str], list[str]]:
    """Build the ``--user`` completer for one verb flavour (``"any"`` or ``"direct"``).

    Cache-then-live like every neighbour: the ``logins_by_host`` snapshot,
    else :func:`~otto.config.completion_cache.collect_logins_by_host` — both
    data-only, neither builds a host.
    """

    @completion_source(
        kind="payload",
        key="logins_by_host",
        host_scoped=True,
        term_scoped=True,
        flavour=flavour,
        sort=True,
    )
    def _complete(ctx: typer.Context, incomplete: str) -> list[str]:
        from ..config import get_completion_names, get_repos
        from ..config.completion_cache import collect_logins_by_host

        request = host_group_request(ctx)
        if not request.host_id:
            return []
        cached = get_completion_names()
        by_host = cached.get("logins_by_host") if cached is not None else None
        if not isinstance(by_host, dict):
            by_host = collect_logins_by_host(get_repos())
        entries = by_host.get(request.host_id, [])
        return filter_logins(
            entries if isinstance(entries, list) else [],
            flavour=flavour,
            term=request.term,
            incomplete=incomplete,
        )

    return _complete
