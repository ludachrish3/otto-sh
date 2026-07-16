"""Shared shell-completion helpers used across CLI subapps.

Host-id completion must be lab-scoped everywhere host ids are accepted —
``otto host``, ``otto tunnel add --hosts``, ``otto docker --on`` (issue
#138) — so the lab-selection walk and the cache-then-live host-id resolution
live here rather than in any one subapp.
"""

import typer


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
    that backs suite/instruction completion, wiped by
    ``--clear-autocomplete-cache``). Falls through to a live ``lab.json``
    scan on cache miss so first-run completion still works.
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
