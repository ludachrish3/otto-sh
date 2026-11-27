"""The lab a run uses, built from the projects' configuration exactly as the CLI builds it."""

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from typing_extensions import final

from .errors import LabBuildError

if TYPE_CHECKING:
    from ..config.project import Project
    from ..inventory.view import InventoryView
    from ..labs.loader import Lab


def merge_host_preferences(*, projects: "list[Project]") -> dict[str, dict[str, Any]]:
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


@final
@dataclass(frozen=True, kw_only=True)
class BuiltLab:
    """A built lab and the inventory view it was built through."""

    lab: "Lab"
    """The lab, as :func:`build_lab` returns it."""

    view: "InventoryView"
    """The operation's inventory view, with every name built, which the lab resolved through."""


def build_lab(
    projects: "list[Project]", *, labs: list[str], view: "InventoryView | None" = None
) -> "Lab":
    """Build lab(s) *labs* exactly as ``otto --lab`` does, and return the lab.

    The lab of :func:`build_lab_and_view`, whose description of the build,
    its arguments and its errors hold here unchanged; call that instead to
    keep the inventory view the lab was built through.
    """
    return build_lab_and_view(projects, labs=labs, view=view).lab


def build_lab_and_view(
    projects: "list[Project]", *, labs: list[str], view: "InventoryView | None" = None
) -> BuiltLab:
    """Build lab(s) *labs* exactly as ``otto --lab`` does, and keep the view it read through.

    A caller that reads the inventories again after the build (``otto lab
    explain`` asks each one how old its snapshot is) reads them through the
    returned :attr:`BuiltLab.view`, so nothing is built twice.

    *labs* are component lab names that are already split: a ``+``-joined
    string such as ``"a+b"`` is not split here (``otto --lab a+b`` and
    ``open_context(lab="a+b")`` split it before this point), so pass
    ``["a", "b"]``.

    Every project's ``[[lab.sources]]`` aggregates, in ``OTTOPATH`` order,
    into one composite: all declared sources are live, and a later source's
    host record overrides an earlier one's. The ``[host_preferences]`` merge
    (:func:`merge_host_preferences`) applies to every host. The process
    inventory view (:func:`~otto.inventory.build_inventory_view`, every
    configured inventory by name) is built here, once, and handed to the
    load, so every source sees the same one. Building it does no I/O, but a
    BROKEN inventory declaration refuses here rather than surfacing later as
    "no inventory is configured". *view*, when given, is the view of an
    operation that loads more than once (the ``otto init`` doctor's pass, one
    view over every load and its listing): it is used instead, its every name
    built now in the order a view built here would be
    (:func:`~otto.inventory.config.require_eager`), so a broken declaration
    refuses with the same error either way. The load decides every component's
    availability once, in one status report over every source, the same
    report ``otto --list-labs`` shows. That much is the load step
    (``otto.session.lab.load_lab_and_view``), called here. Last, each
    project's declared docker services become placeholder hosts, so they list
    and complete before ``compose up``.

    Raises what ``load_lab_and_view`` raises.
    """
    built = load_lab_and_view(projects, labs=labs, view=view)

    from ..docker.compose import register_declared_container_hosts

    register_declared_container_hosts(built.lab, projects)
    return built


def load_lab_and_view(
    projects: "list[Project]", *, labs: list[str], view: "InventoryView | None" = None
) -> BuiltLab:
    """Load lab(s) *labs* as :func:`build_lab_and_view` does, and register nothing.

    The load step of :func:`build_lab_and_view`, whose description of *labs*,
    the sources, the inventory view and *view* holds here: the sources, the
    inventory view, the load, and the translation of their errors into
    :class:`~otto.session.LabBuildError`. No declared container host is
    registered: it is for a caller that only needs to know whether the lab
    loads, and an unknown lab is refused in the words a build gives it.

    Raises :class:`~otto.session.LabBuildError`: no labs, no projects (none
    was discovered: :func:`~otto.config.project.no_project_found_message`), an unknown lab, an
    unavailable lab (its last declaration says so, or a source whose scope
    holds it failed), a bad source or unknown backend, a broken inventory
    declaration, or a call made while an init import is running (lab sources
    are built only after every project has registered its backends). A source
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
    from ..labs.errors import LabNotFoundError, LabRepositoryError, LabUnavailableError
    from ..labs.loader import load_lab
    from ..labs.sources import build_lab_sources

    try:
        repository = build_lab_sources(projects=projects)
    except (ValueError, LabRepositoryError) as e:
        raise LabBuildError(
            f"host source unavailable: {e}", field=None, kind="sources", detail=str(e)
        ) from e

    # Function-local: `otto.inventory` pulls ~77 otto modules onto every
    # caller, and a Lab-object open_context never builds a lab at all.
    from ..inventory.config import build_inventory_view, require_eager
    from ..inventory.errors import InventoryError

    try:
        if view is None:
            view = build_inventory_view(projects=projects)
        else:
            require_eager(view)
    except InventoryError as e:
        raise LabBuildError(
            f"inventory unavailable: {e}", field=None, kind="inventory", detail=str(e)
        ) from e

    try:
        lab = load_lab(
            labs,
            preferences=merge_host_preferences(projects=projects),
            repository=repository,
            view=view,
        )
    except LabNotFoundError as e:
        raise LabBuildError(str(e), field="labs", kind="unknown_lab", detail=str(e)) from e
    except LabUnavailableError as e:
        raise LabBuildError(str(e), field="labs", kind="unavailable_lab", detail=str(e)) from e
    return BuiltLab(lab=lab, view=view)
