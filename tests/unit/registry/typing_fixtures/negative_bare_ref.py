"""Negative typing fixture: a bare ``Ref`` is not a record of the entry type."""

from otto.registry import ClassEntry, Ref, Registry


class Frame:
    """A class a class-valued registry holds."""


def bare_ref() -> None:
    frames: Registry[ClassEntry[Frame]] = Registry(
        "frame", entry=ClassEntry, register_hint="register_frame()"
    )
    frames.register("b", Ref("m:Frame"))  # ty: ignore[invalid-argument-type]
