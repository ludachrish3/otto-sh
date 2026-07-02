"""Generic named registry for pluggable components.

Every otto extension seam (term/transfer backends, host classes, lab
repositories, CLI commands, ...) stores its entries in a :class:`Registry`:
one storage idiom, uniform fail-loud errors with did-you-mean suggestions,
and per-entry origin attribution. Domain modules keep their public
``register_*``/``build_*`` wrapper functions; this class is the shared engine
behind them.

>>> r: Registry[str] = Registry("demo backend", register_hint="register_demo()")
>>> r.register("json", "the-json-backend", origin="example")
>>> r.get("json")
'the-json-backend'
>>> r.names()
['json']
"""

import difflib
import inspect
from typing import Generic, TypeVar

T = TypeVar("T")
"""Type variable for the entry type stored in a :class:`Registry`."""


def caller_module(depth: int = 1) -> str:
    """Return the ``__name__`` of the module *depth* call frames above the caller."""
    frame = inspect.currentframe()
    for _ in range(depth + 1):
        frame = frame.f_back if frame is not None else None
    if frame is None:
        return "<unknown>"
    return frame.f_globals.get("__name__", "<unknown>")


class Registry(Generic[T]):
    """Named registry of pluggable components; fail-loud lookups with suggestions."""

    def __init__(self, kind: str, *, register_hint: str, collision_hint: str | None = None) -> None:
        """Create a registry for *kind* entries (e.g. ``"term backend"``).

        *register_hint* names the public registration function shown in lookup
        errors (e.g. ``"otto.register_term_backend()"``).

        *collision_hint* replaces the default "Pass overwrite=True to replace it
        deliberately." sentence in duplicate-registration errors. Pass it for a
        registry with no ``overwrite`` escape hatch (e.g. CLI commands), where
        the default sentence would point at a parameter that does not exist.
        """
        self._kind = kind
        self._register_hint = register_hint
        self._collision_hint = collision_hint or "Pass overwrite=True to replace it deliberately."
        self._entries: dict[str, T] = {}
        self._origins: dict[str, str] = {}

    def register(
        self, name: str, obj: T, *, overwrite: bool = False, origin: str | None = None
    ) -> None:
        """Register *obj* under *name*; duplicates are loud unless *overwrite*.

        *origin* attributes the entry (defaults to the caller's module); it is
        used in collision and listing messages.
        """
        entry_origin = origin if origin is not None else caller_module()
        if name in self._entries and not overwrite:
            raise ValueError(
                f"{self._kind} {name!r} is already registered by "
                f"{self._origins[name]!r}; second registration from "
                f"{entry_origin!r}. {self._collision_hint}"
            )
        self._entries[name] = obj
        self._origins[name] = entry_origin

    def get(self, name: str) -> T:
        """Return the entry registered under *name*.

        Raises:
            ValueError: If *name* is unknown; the message lists registered
                names, adds a did-you-mean suggestion, and points at the
                registration function.
        """
        try:
            return self._entries[name]
        except KeyError:
            known = ", ".join(self._entries) or "<none>"
            close = difflib.get_close_matches(name, list(self._entries), n=1)
            suggestion = f" Did you mean {close[0]!r}?" if close else ""
            raise ValueError(
                f"Unknown {self._kind} {name!r}.{suggestion} Registered: {known}. "
                f"Custom entries can be added via {self._register_hint}."
            ) from None

    def unregister(self, name: str) -> None:
        """Remove the entry registered under *name* (ValueError if unknown)."""
        self.get(name)  # reuse the rich unknown-name error
        del self._entries[name]
        del self._origins[name]

    def names(self) -> list[str]:
        """Return registered names in registration order."""
        return list(self._entries)

    def origin(self, name: str) -> str:
        """Return the module that registered *name* (ValueError if unknown)."""
        self.get(name)
        return self._origins[name]

    def items(self) -> list[tuple[str, T]]:
        """Return ``(name, entry)`` pairs in registration order."""
        return list(self._entries.items())

    def __contains__(self, name: str) -> bool:
        """Return whether *name* is registered."""
        return name in self._entries

    def __len__(self) -> int:
        """Return the number of registered entries."""
        return len(self._entries)
