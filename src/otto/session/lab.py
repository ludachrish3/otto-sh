"""The lab a run uses, built from the projects' configuration exactly as the CLI builds it."""

from typing import TYPE_CHECKING, Any

from .errors import LabBuildError

if TYPE_CHECKING:
    from ..config.project import Project
    from ..labs.loader import Lab


def merge_host_preferences(projects: "list[Project]") -> dict[str, dict[str, Any]]:
    """Merge every project's ``[host_preferences]`` in ``OTTOPATH`` order.

    Later projects overlay earlier ones. A selection (a list) is atomic: the last
    project to set a ``(selector, capability)`` wins it. An option table (a dict)
    merges per key.
    """
    merged: dict[str, dict[str, Any]] = {}
    for project in projects:
        for selector, entries in project.host_preferences.items():
            dest = merged.setdefault(selector, {})
            for key, val in entries.items():
                if isinstance(val, list):
                    dest[key] = list(val)
                else:
                    dest.setdefault(key, {}).update(val)
    return merged


def build_lab(projects: "list[Project]", labs: list[str]) -> "Lab":
    """Build lab(s) *labs* exactly as ``otto --lab`` does.

    *labs* are component lab names that are already split: a ``+``-joined
    string such as ``"a+b"`` is not split here (``otto --lab a+b`` and
    ``open_context(lab="a+b")`` split it before this point), so pass
    ``["a", "b"]``.

    Every project's ``[[lab.sources]]`` aggregates, in ``OTTOPATH`` order,
    into one composite: all declared sources are live, and a later source's
    host record overrides an earlier one's. The ``[host_preferences]`` merge
    (:func:`merge_host_preferences`) applies to every host. The process
    inventory is built here, once, and handed to the load, so every source
    sees the same one. Building it does no I/O, but a BROKEN ``[inventory]``
    declaration refuses here rather than surfacing later as "no inventory is
    configured". Last, each project's declared docker services become
    placeholder hosts, so they list and complete before ``compose up``.

    Raises :class:`~otto.session.LabBuildError`: no labs, no projects (none
    was discovered: :func:`~otto.config.project.no_project_found_message`), an unknown lab, a
    bad source or unknown backend, a broken inventory declaration, or a call
    made while an init import is running (lab sources are built only after
    every project has registered its backends). A source
    that fails only at load time (malformed lab data, composite conflicts)
    propagates as :class:`otto.labs.LabRepositoryError
    <otto.labs.errors.LabRepositoryError>`, as it does on the CLI.
    """
    if not labs:
        raise LabBuildError("no lab selected: name one or more labs", field="labs", kind="no_labs")
    if not projects:
        # Nothing was discovered, so nothing declares a lab source: say THAT,
        # not that no project declares [[lab.sources]].
        from ..config.project import no_project_found_message

        raise LabBuildError(no_project_found_message(), field=None, kind="no_project")
    from ..labs.errors import LabNotFoundError, LabRepositoryError
    from ..labs.loader import load_lab
    from ..labs.sources import build_lab_sources

    try:
        repository = build_lab_sources(projects)
    except (ValueError, LabRepositoryError) as e:
        raise LabBuildError(
            f"host source unavailable: {e}", field=None, kind="sources", detail=str(e)
        ) from e

    # Function-local: `otto.inventory` pulls ~77 otto modules onto every
    # caller, and a Lab-object open_context never builds a lab at all.
    from ..inventory.config import build_inventory
    from ..inventory.errors import InventoryError

    try:
        inventory = build_inventory(projects)
    except InventoryError as e:
        raise LabBuildError(
            f"inventory unavailable: {e}", field=None, kind="inventory", detail=str(e)
        ) from e

    try:
        lab = load_lab(
            labs,
            preferences=merge_host_preferences(projects),
            repository=repository,
            inventory=inventory,
        )
    except LabNotFoundError as e:
        raise LabBuildError(str(e), field="labs", kind="unknown_lab", detail=str(e)) from e

    from ..docker.compose import register_declared_container_hosts

    register_declared_container_hosts(lab, projects)
    return lab
