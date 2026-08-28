"""Pydantic boundary specs for the ``lab.json`` v2 wrapper layers.

``LabEntrySpec`` is one value of the top-level ``labs`` table (keyed by lab
name) — what belongs to the lab as a whole: its reservable ``resources`` and
opaque ``metadata``. ``ElementSpec`` is one ``elements`` entry: identity
(``name`` / ``id``), lab membership as fullmatch patterns, opaque
``metadata``, and the host entries it groups. Neither carries an operational
host field; ``ElementSpec.flatten()`` stamps ``element`` / ``element_id``
onto copies of its host dicts so the flat host-dict API (the factory,
``host_identity``, custom backends) is untouched by the file shape.
``ElementKey`` is an element's identity — ``ElementSpec.key`` — the value the
loader and the multi-source merge key elements by.
"""

import re
from dataclasses import dataclass
from typing import Any

from pydantic import Field, field_validator, model_validator
from typing_extensions import override

from .base import OttoModel

HOISTED_HOST_KEYS: frozenset[str] = frozenset({"element", "element_id", "labs", "resources"})
"""Keys that live ABOVE the host entry in v2 and are errors inside one."""


@dataclass(frozen=True)
class ElementKey:
    """An element's identity: its ``name`` and its optional repeat ``id``.

    A frozen dataclass rather than the ``(name, id)`` pair it replaces
    (``.ast-grep/rules/no-tuple-return.yml``): frozen and hashable, so it is
    still the dict key the multi-source element merge needs (spec §6), while an
    identity component added later cannot break an unpacking site.
    """

    name: str
    """The element's ``name`` — the host id's ``slug(element)`` part."""

    id: int | None = None
    """The element's ``id``; ``None`` when the name alone is the identity."""

    @override
    def __str__(self) -> str:
        """Render as ``('dut', 1)`` — the pair users see in the file.

        Error messages name an element by this (``f"duplicate element {key}"``),
        not by ``repr``: the dataclass repr leaks a type name the ``lab.json``
        author never typed, while the pair is exactly the ``name`` / ``id``
        they wrote.
        """
        return f"({self.name!r}, {self.id!r})"


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


class ElementSpec(OttoModel):
    """One ``elements`` entry: identity, membership, metadata, and its hosts."""

    name: str
    """Element name — the host id's ``slug(element)`` part."""

    id: int | None = None
    """Repeat disambiguator (today's ``element_id``); ``None`` when unique."""

    labs: list[str] = Field(min_length=1)
    """Membership patterns, ``re.fullmatch``-ed against a lab name."""

    metadata: dict[str, Any] = Field(default_factory=dict)
    """Opaque element-level user data; copied onto each host as ``element_metadata``."""

    hosts: list[dict[str, Any]] = Field(min_length=1)
    """Raw host entries; validated by the host specs after :meth:`flatten`."""

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

    @model_validator(mode="after")
    def _patterns_compile_and_hosts_carry_no_hoisted_keys(self) -> "ElementSpec":
        for pattern in self.labs:
            _reject_bad_pattern(self.name, pattern)
        for idx, host in enumerate(self.hosts):
            hoisted = sorted(k for k in host if k in HOISTED_HOST_KEYS)
            if hoisted:
                raise ValueError(
                    f"element {self.name!r}: hosts[{idx}] carries {hoisted[0]!r}, which "
                    f"now lives on the element / the 'labs' table, not the host entry"
                )
        return self

    @property
    def key(self) -> ElementKey:
        """This element's :class:`ElementKey` — the unit of multi-source replacement (spec §6)."""
        return ElementKey(self.name, self.id)

    def matches(self, lab: str) -> bool:
        """Whether this element is a member of *lab* (any pattern fullmatches)."""
        return any(re.fullmatch(p, lab) for p in self.labs)

    def flatten(self) -> list[dict[str, Any]]:
        """Return copies of the host entries, ``element`` / ``element_id`` stamped on."""
        identity: dict[str, Any] = {"element": self.name}
        if self.id is not None:
            identity["element_id"] = self.id
        return [{**dict(h), **identity} for h in self.hosts]
