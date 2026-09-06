"""Pydantic boundary specs for the ``lab.json`` v2 wrapper layers.

``LabEntrySpec`` is one value of the top-level ``labs`` table (keyed by lab
name) — what belongs to the lab as a whole: its reservable ``resources`` and
opaque ``metadata``. ``ElementSpec`` is one ``elements`` entry: identity
(``name`` / ``id``), lab membership as fullmatch patterns, opaque
``metadata``, and the host entries it groups. Neither carries an operational
host field; the element reaches the factory as one
:class:`~otto.host.element.Element` (``ElementSpec.to_element``), so a host
entry describes the host and nothing else.
``ElementSpec.key`` is the element's identity — ``slug(name)`` — the value the
loader and the multi-source merge key elements by.
"""

import re
from typing import TYPE_CHECKING, Any

from pydantic import Field, field_validator, model_validator

from .base import OttoModel

if TYPE_CHECKING:
    from ..host.element import Element

HOISTED_HOST_KEYS: frozenset[str] = frozenset({"element", "element_id", "labs"})
"""Keys that live above the host entry and are errors inside one.

``element`` and ``element_id`` are the element's ``name``/``id``, ``labs`` its
membership.

``resources`` left this set with spec 2026-08-28 three-level-reservations: a
host entry may declare its own (a slot), beside the element's and the lab's.
"""


def resources_nonempty(v: set[str]) -> set[str]:
    """Reservation identifiers are opaque, non-empty strings.

    Spec 2026-08-28 three-level-reservations §2, §10. Shared by all three
    levels — :class:`LabEntrySpec`, :class:`ElementSpec`, and
    :class:`~otto.models.host.HostSpec`, which imports it from here — so they
    cannot drift into three different notions of a usable identifier. Public
    precisely because it is imported across modules: a leading underscore on a
    name another module depends on says "private" while meaning the opposite.
    """
    if any(not r.strip() for r in v):
        raise ValueError("resources must be non-empty strings")
    return v


def _strip_comment_keys(data: object) -> object:
    if isinstance(data, dict):
        return {k: v for k, v in data.items() if not (isinstance(k, str) and k.startswith("_"))}
    return data


def _reject_bad_pattern(element: str, pattern: str) -> None:
    """Raise naming *element* and *pattern* when *pattern* is not a valid regex.

    A free function rather than the loop body it is called from: the
    ``try``/``except`` belongs outside the loop (``PERF203``), and the error
    text is the whole point — spec §9 requires the element and the pattern.
    """
    try:
        re.compile(pattern)
    except re.error as e:
        raise ValueError(
            f"element {element!r}: labs pattern {pattern!r} is not a valid regex: {e}"
        ) from None


class LabEntrySpec(OttoModel):
    """One ``labs`` table value: the lab's declared resources and metadata."""

    resources: set[str] = Field(default_factory=set)
    """Reservation identifiers, matched byte-for-byte by the reservation backend."""

    metadata: dict[str, Any] = Field(default_factory=dict)
    """Opaque lab-level user data; otto never reads it."""

    @model_validator(mode="before")
    @classmethod
    def _strip(cls, data: object) -> object:
        return _strip_comment_keys(data)

    @field_validator("resources")
    @classmethod
    def _resources(cls, v: set[str]) -> set[str]:
        return resources_nonempty(v)


class ElementSpec(OttoModel):
    """One ``elements`` entry: identity, membership, metadata, and its hosts."""

    name: str
    """Element name — the host id's ``slug(element)`` part."""

    id: int | None = None
    """Data the author assigned to the element; never part of a host id, a name,
    an ordering, or a key (spec 2026-09-05 §2.1)."""

    labs: list[str] = Field(min_length=1)
    """Membership patterns, ``re.fullmatch``-ed against a lab name."""

    metadata: dict[str, Any] = Field(default_factory=dict)
    """Opaque element-level user data; reached as ``host.element.metadata``."""

    resources: set[str] = Field(default_factory=set)
    """Reservation identifiers for the element as one unit (spec 2026-08-28
    three-level-reservations §2).

    Optional and independent of the lab's and each host's — the element level
    is for equipment reserved whole (a chassis), where the lab is too coarse
    and a slot too fine.
    """

    hosts: list[dict[str, Any]] = Field(min_length=1)
    """Raw host entries; validated by the host specs as the file has them."""

    @model_validator(mode="before")
    @classmethod
    def _strip(cls, data: object) -> object:
        return _strip_comment_keys(data)

    @field_validator("name")
    @classmethod
    def _name_slugs_nonempty(cls, v: str) -> str:
        from ..host.remote_host import slug

        if not slug(v):
            raise ValueError(f"{v!r} slugs to an empty id (needs at least one letter or digit)")
        return v

    @field_validator("id")
    @classmethod
    def _id_nonnegative(cls, v: int | None) -> int | None:
        if v is not None and v < 0:
            raise ValueError(f"must be >= 0, got {v}")
        return v

    @field_validator("resources")
    @classmethod
    def _resources(cls, v: set[str]) -> set[str]:
        return resources_nonempty(v)

    @model_validator(mode="after")
    def _patterns_compile_and_hosts_carry_no_hoisted_keys(self) -> "ElementSpec":
        for pattern in self.labs:
            _reject_bad_pattern(self.name, pattern)
        for idx, host in enumerate(self.hosts):
            hoisted = sorted(k for k in host if k in HOISTED_HOST_KEYS)
            if hoisted:
                raise ValueError(
                    f"element {self.name!r}: hosts[{idx}] carries {hoisted[0]!r}, which "
                    f"now lives on the element, not the host entry"
                )
        return self

    @property
    def key(self) -> str:
        """This element's identity token — ``slug(name)`` (spec 2026-09-05 §2.3)."""
        from ..host.remote_host import slug

        return slug(self.name)

    def to_element(self) -> "Element":
        """Build the runtime :class:`~otto.host.element.Element` this entry declares.

        Built once per element by the loader and handed to every member host;
        ``labs`` and ``hosts`` stay here — they are file-shape, not element data.
        """
        from ..host.element import Element  # lazy: models must not import host at module level

        return Element(
            self.name,
            id=self.id,
            metadata=dict(self.metadata),
            resources=frozenset(self.resources),
        )

    def matches(self, lab: str) -> bool:
        """Whether this element is a member of *lab* (any pattern fullmatches)."""
        return any(re.fullmatch(p, lab) for p in self.labs)
