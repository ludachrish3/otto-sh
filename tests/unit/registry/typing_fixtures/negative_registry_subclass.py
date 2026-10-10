"""Negative typing fixture: ``Registry`` is final; no module subclasses it."""

from otto.registry import ClassEntry, Registry


class Sub(Registry[ClassEntry[object]]):  # ty: ignore[subclass-of-final-class]
    """A subclass ty must refuse."""
