"""Negative typing fixture: ``Subscription`` is final; no module subclasses it."""

from otto.registry import Subscription


class Sub(Subscription[int]):  # ty: ignore[subclass-of-final-class]
    """A subclass ty must refuse."""
