"""How a lab source is reached through the inventory view, whatever it implements (#1176).

A source may implement any of the optional capabilities
(:class:`~otto.labs.protocol.SupportsInventoryView`,
:class:`~otto.labs.protocol.SupportsLabStatus`,
:class:`~otto.labs.protocol.SupportsLabScope`,
:class:`~otto.labs.protocol.SupportsLabContribution`) or none of them. Each function
here is the one place that decides what a source receives: the capability
when it has it, otherwise today's legacy call with what a legacy backend can
take, :attr:`~otto.inventory.InventoryView.bare_inventory`.

Every function that takes a view first applies :func:`labs_view`, so the view a
source receives records each bare-reference refusal as a drop
(``otto.labs.drops.record_drop``). That is how a refusal becomes a drop
without ``otto.inventory`` importing ``otto.labs``.
"""

from typing import TYPE_CHECKING, Any

from ..inventory.errors import InventoryError
from .drops import record_drop
from .errors import LabRepositoryError
from .protocol import (
    LabContribution,
    LabRepository,
    LabScope,
    LabStatus,
    SupportsInventoryView,
    SupportsLabContribution,
    SupportsLabScope,
    SupportsLabStatus,
)

if TYPE_CHECKING:
    from ..inventory.view import InventoryView
    from .loader import Lab

LISTING_ERRORS = (ValueError, LabRepositoryError, InventoryError)
"""What listing labs may fail with: a bad source, a source's own error, an inventory's.

``otto --list-labs`` catches these to fall back to each project's own panel,
and a project's panel catches them to show the reason in place of its labs
(:meth:`~otto.config.project.Project.get_lab_panel`). A ``ValueError`` covers
:class:`~otto.labs.LabSourceConstructionError` and settings errors."""


def labs_view(*, view: "InventoryView") -> "InventoryView":
    """Return *view* with ``otto.labs.drops.record_drop`` as its refusal hook.

    A bare reference the view refuses (lab templating D1) is then recorded
    as one drop, by :meth:`~otto.inventory.RefusedDefault.refuse`, on every
    route that resolves through it.
    """
    return view.with_refusal_hook(hook=record_drop)


def lab_statuses(*, repository: LabRepository, view: "InventoryView") -> list[LabStatus]:
    """Return every lab *repository* declares, with whether it can be loaded now.

    The source's :meth:`~otto.labs.protocol.SupportsLabStatus.list_lab_status`
    when it has one; otherwise every :meth:`~otto.labs.LabRepository.list_labs`
    name, available.
    """
    view = labs_view(view=view)
    if isinstance(repository, SupportsLabStatus):
        return repository.list_lab_status(view=view)
    return [LabStatus(name=name) for name in repository.list_labs()]


def load_lab_through(
    *,
    repository: LabRepository,
    name: str,
    preferences: dict[str, dict[str, Any]] | None = None,
    view: "InventoryView",
) -> "Lab":
    """Load lab *name* from *repository*, resolving its references through *view*.

    The source's :meth:`~otto.labs.protocol.SupportsInventoryView.load_lab_in_view`
    when it has one. Otherwise its :meth:`~otto.labs.LabRepository.load_lab`
    receives :attr:`~otto.inventory.InventoryView.bare_inventory`: the process
    ``default`` unchanged (``None`` when none is configured), or, in a project
    that refuses bare references, the
    :class:`~otto.inventory.RefusedDefault` stand-in that refuses each one.

    Raises whatever the source raises, including
    :class:`~otto.inventory.errors.BareReferenceRefused`.
    """
    view = labs_view(view=view)
    if isinstance(repository, SupportsInventoryView):
        return repository.load_lab_in_view(name, preferences=preferences, view=view)
    return repository.load_lab(name, preferences=preferences, inventory=view.bare_inventory)


def load_contribution_through(
    *,
    repository: LabRepository,
    name: str,
    preferences: dict[str, dict[str, Any]] | None = None,
    view: "InventoryView",
) -> LabContribution | None:
    """Return *repository*'s contribution to lab *name*, or ``None`` when it has no such capability.

    The source's :meth:`~otto.labs.protocol.SupportsLabContribution.load_contribution`
    through *view*. ``None`` for a source without it: the caller loads that
    one through :func:`load_lab_through`, and its links come resolved.
    """
    if not isinstance(repository, SupportsLabContribution):
        return None
    return repository.load_contribution(name, preferences=preferences, view=labs_view(view=view))


def lab_scope_of(*, repository: LabRepository) -> LabScope | None:
    """Return *repository*'s :class:`~otto.labs.LabScope`, or ``None`` when it declares none."""
    if isinstance(repository, SupportsLabScope):
        return repository.lab_scope()
    return None
