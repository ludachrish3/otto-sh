"""``Element`` — the network element a host belongs to, carried on the host.

One frozen object per element, built once by the lab loader
(:meth:`otto.models.lab.ElementSpec.to_element`) and shared by every host of
that element, so ``host_a.element is host_b.element`` holds for siblings.
It is the ONLY way a host exposes element data (spec 2026-09-05 §2.5):
``host.element.name``, ``.id``, ``.metadata``, ``.resources``.

``id`` is data — a number the lab author assigned — and never part of any
host id, name, ordering, or key (spec 2026-09-05 §2.1). ``slug`` is the
element's identity token and the prefix of every member host's id.

Lives beside :mod:`otto.host.lab_info` and :mod:`otto.host.inventory_ref`
for the same reason they do: the host package does not import upward, and
every package that builds hosts already depends on it.
"""

from dataclasses import dataclass, field
from typing import Any

from .remote_host import slug

# ``otto.models.lab.resources_nonempty`` is imported function-locally, on
# purpose: importing ``models.lab`` here at module scope first runs
# ``otto.models/__init__``, which imports ``models.host``, which imports
# ``Element`` from this still-initialising module and fails
# (``models.lab`` itself only names ``Element`` under ``TYPE_CHECKING``).
# ``remote_host`` carries no such cycle — its ``Element`` import is
# ``TYPE_CHECKING``-only — so ``slug``, which every construction needs, is
# imported normally.


@dataclass(frozen=True)
class Element:
    """One element — its name, optional id, opaque metadata, and reservation set.

    Hashing raises ``TypeError`` (``metadata`` is a dict); key collections by
    :attr:`slug`. ``metadata`` is copied once at construction — a SHALLOW copy,
    so anything nested inside it is still the table the caller passed — and the
    result is shared by every host of the element: treat it, and everything
    under it, as read-only.
    """

    name: str
    """The element's name exactly as the lab author wrote it."""

    id: int | None = None
    """An author-assigned number, or ``None`` — data, never identity (spec 2026-09-05 §2.1)."""

    metadata: dict[str, Any] = field(default_factory=dict)
    """Opaque element-level user data; otto never reads it."""

    resources: frozenset[str] = frozenset()
    """Reservation identifiers for the element as one unit (spec 2026-08-28
    three-level-reservations §2)."""

    def __post_init__(self) -> None:
        if not slug(self.name):
            raise ValueError(
                f"element name {self.name!r} slugs to an empty id "
                "(needs at least one letter or digit)"
            )
        if self.id is not None and self.id < 0:
            raise ValueError(f"element {self.name!r}: id must be >= 0, got {self.id}")
        if isinstance(self.resources, (str, bytes)):
            # A ``ValueError``, not the ``TypeError`` TRY004 would prefer: the
            # file layer refuses this same value with a pydantic
            # ``ValidationError`` (itself a ``ValueError``), so a caller that
            # already handles the file layer's refusal handles this one too.
            raise ValueError(  # noqa: TRY004 — matches the file layer; see above
                f"element {self.name!r}: resources must be a collection of identifiers, "
                f"not {type(self.resources).__name__} — a bare string iterates into "
                f"one resource per character"
            )
        # Coerced ONCE and validated through the result: the argument may be a
        # one-shot iterable, and a second pass over a spent generator would
        # store an empty set behind a validation that saw the real names.
        resources = frozenset(self.resources)
        from ..models.lab import resources_nonempty  # lazy: see the module comment

        resources_nonempty(set(resources))
        # ``frozen=True`` blocks rebinding, not mutation of the dict behind
        # ``metadata``; copying here is what makes sharing one instance safe.
        object.__setattr__(self, "metadata", dict(self.metadata))
        object.__setattr__(self, "resources", resources)

    @property
    def slug(self) -> str:
        """The identity token — ``slug(name)`` — and every member host's id prefix."""
        return slug(self.name)
