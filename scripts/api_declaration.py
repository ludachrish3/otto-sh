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

    def constructor_withdrawn(self, key: str) -> bool:
        """Return True when declared class *key* (``module:Class``) has ``construct = false``."""
        ...

    def declared_classes(self) -> "list[str]":
        """Return the sorted ``module:Class`` key of every class the API dump reported."""
        ...

    def positional_limit(self, key: str) -> "int | None":
        """Return how many leading positional arguments public callable *key* accepts.

        *key* is ``module:name`` for a function or a class's constructor and
        ``module:Class.method`` for a method (receiver not counted). None when
        the dump has no shape for it: an unknown key, a variadic signature, a
        builtin or withdrawn constructor.
        """
        ...

    def takes_receiver(self, key: str) -> bool:
        """Return True when *key* is a plain method: called via its class, it takes the receiver."""
        ...

    def bindings_named(self, name: str) -> "list[str]":
        """Return every declared ``module:name`` binding key of *name*, sorted."""
        ...

    def methods_named(self, name: str) -> "list[str]":
        """Return every ``module:Class.name`` key the dump recorded a shape for, sorted.

        Prose that says ``status(host)`` may mean a function or a hook of
        that name; this is how a caller learns the hooks exist. A nested
        class's constructor key (``module:Outer.Inner``) is listed here too,
        since a shape alone does not tell it from a method.
        """
        ...


@dataclass
class StaticDeclaration:
    """A declaration held as ``{namespace: names}``, for tests and the report CLI."""

    members: dict[str, set[str]]
    private: "dict[str, set[str]]" = field(default_factory=dict)
    withdrawn: "set[str]" = field(default_factory=set)
    shapes: "dict[str, int | None]" = field(default_factory=dict)
    receivers: "set[str]" = field(default_factory=set)  # the keys recorded as plain methods

    def is_namespace(self, module: str) -> bool:
        """Return True when *module* is a key of :attr:`members`."""
        return module in self.members

    def declares(self, module: str, name: str) -> bool:
        """Return True when *name* is listed for *module*."""
        return name in self.members.get(module, set())

    def underscore_members(self, module: str, name: str) -> "set[str] | None":
        """Return ``private["module:name"]``, or None when that key is absent."""
        return self.private.get(f"{module}:{name}")

    def constructor_withdrawn(self, key: str) -> bool:
        """Return True when *key* is in :attr:`withdrawn`."""
        return key in self.withdrawn

    def declared_classes(self) -> "list[str]":
        """Return the keys of :attr:`private`, sorted: every class the dump reported.

        That includes protocols and errors; a caller that wants only constructible
        classes filters them with the dump's records.
        """
        return sorted(self.private)

    def positional_limit(self, key: str) -> "int | None":
        """Return ``shapes[key]``, or None when the key has no recorded shape."""
        return self.shapes.get(key)

    def takes_receiver(self, key: str) -> bool:
        """Return True when *key* is in :attr:`receivers`."""
        return key in self.receivers

    def bindings_named(self, name: str) -> "list[str]":
        """Return ``module:name`` for every namespace of :attr:`members` listing *name*."""
        return [f"{m}:{name}" for m in sorted(self.members) if name in self.members[m]]

    def methods_named(self, name: str) -> "list[str]":
        """Return the keys of :attr:`shapes` whose last dotted segment is *name*."""
        return [k for k in sorted(self.shapes) if _method_name(k) == name]


def _method_name(key: str) -> "str | None":
    """Return the method name of a ``module:Class.method`` key, None for a binding key."""
    _, _, path = key.partition(":")
    owner, dot, method = path.rpartition(".")
    return method if dot and owner else None
