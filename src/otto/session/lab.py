"""The lab a run uses, built from the repos' configuration exactly as the CLI builds it."""

from typing import TYPE_CHECKING, Any

from .errors import LabBuildError

if TYPE_CHECKING:
    from ..config.lab import Lab
    from ..config.repo import Repo


def merge_host_preferences(repos: "list[Repo]") -> dict[str, dict[str, Any]]:
    """Merge every repo's ``[host_preferences]`` in ``OTTO_SUT_DIRS`` order.

    Later repos overlay earlier ones. A selection (a list) is atomic: the last
    repo to set a ``(selector, capability)`` wins it. An option table (a dict)
    merges per key.
    """
    merged: dict[str, dict[str, Any]] = {}
    for repo in repos:
        for selector, entries in repo.host_preferences.items():
            dest = merged.setdefault(selector, {})
            for key, val in entries.items():
                if isinstance(val, list):
                    dest[key] = list(val)
                else:
                    dest.setdefault(key, {}).update(val)
    return merged


def build_lab(repos: "list[Repo]", labs: list[str]) -> "Lab":
    """Build lab(s) *labs* exactly as ``otto --lab`` does.

    *labs* are component lab names that are already split: a ``+``-joined
    string such as ``"a+b"`` is not split here (``otto --lab a+b`` and
    ``open_context(lab="a+b")`` split it before this point), so pass
    ``["a", "b"]``.

    Every repo's ``[[lab.sources]]`` aggregates, in ``OTTO_SUT_DIRS`` order,
    into one composite: all declared sources are live, and a later source's
    host record overrides an earlier one's. The ``[host_preferences]`` merge
    (:func:`merge_host_preferences`) applies to every host. The process
    inventory is built here, once, and handed to the load, so every source
    sees the same one. Building it does no I/O, but a BROKEN ``[inventory]``
    declaration refuses here rather than surfacing later as "no inventory is
    configured". Last, each repo's declared docker services become
    placeholder hosts, so they list and complete before ``compose up``.

    Raises :class:`~otto.session.LabBuildError`: no labs, an unknown lab, a
    bad source or unknown backend, a broken inventory declaration, or a call
    made while an init import is running (lab sources are built only after
    every repo has registered its backends). A source
    that fails only at load time (malformed lab data, composite conflicts)
    propagates as :class:`otto.labs.LabRepositoryError
    <otto.labs.errors.LabRepositoryError>`, as it does on the CLI.
    """
    if not labs:
        raise LabBuildError("no lab selected: name one or more labs", field="labs", kind="no_labs")
    from ..config.lab import load_lab
    from ..labs import LabNotFoundError, LabRepositoryError, build_lab_sources

    try:
        repository = build_lab_sources(repos)
    except (ValueError, LabRepositoryError) as e:
        raise LabBuildError(
            f"host source unavailable: {e}", field=None, kind="sources", detail=str(e)
        ) from e

    # Function-local: `otto.inventory` pulls ~77 otto modules onto every
    # caller, and a Lab-object open_context never builds a lab at all.
    from ..inventory import InventoryError, build_inventory

    try:
        inventory = build_inventory(repos)
    except InventoryError as e:
        raise LabBuildError(
            f"inventory unavailable: {e}", field=None, kind="inventory", detail=str(e)
        ) from e

    try:
        lab = load_lab(
            labs,
            preferences=merge_host_preferences(repos),
            repository=repository,
            inventory=inventory,
        )
    except LabNotFoundError as e:
        raise LabBuildError(str(e), field="labs", kind="unknown_lab", detail=str(e)) from e

    from ..docker.compose import register_declared_container_hosts

    register_declared_container_hosts(lab, repos)
    return lab
