"""What the docs validator asks of a public-surface declaration.

A leaf module: it imports nothing from ``scripts``. ``scripts/api_teaching.py``
(the validator) asks a :class:`Declaration`; ``scripts/api_agreement.py``
builds a :class:`StaticDeclaration` from runtime reports. Both import it from
here, so neither imports the other at module level.
"""

from dataclasses import dataclass, field
from typing import Protocol


class Declaration(Protocol):
    """What the validator asks of a declaration."""

    def is_namespace(self, module: str) -> bool:
        """Return True when *module* is a declared namespace."""
        ...

    def declares(self, module: str, name: str) -> bool:
        """Return True when *name* is in declared namespace *module*'s list."""
        ...

    def underscore_members(self, module: str, name: str) -> "set[str] | None":
        """Return the underscore members of declared class *module*:*name*, else None."""
        ...


@dataclass
class StaticDeclaration:
    """A declaration held as ``{namespace: names}``, for tests and the report CLI."""

    members: dict[str, set[str]]
    private: "dict[str, set[str]]" = field(default_factory=dict)

    def is_namespace(self, module: str) -> bool:
        """Return True when *module* is a key of :attr:`members`."""
        return module in self.members

    def declares(self, module: str, name: str) -> bool:
        """Return True when *name* is listed for *module*."""
        return name in self.members.get(module, set())

    def underscore_members(self, module: str, name: str) -> "set[str] | None":
        """Return ``private["module:name"]``, or None when that key is absent."""
        return self.private.get(f"{module}:{name}")
