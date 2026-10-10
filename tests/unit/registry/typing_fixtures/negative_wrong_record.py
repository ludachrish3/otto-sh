"""Negative typing fixture: a bare class is not a record of the entry type."""

from otto.registry import ClassEntry, Registry


class Frame:
    """A class a class-valued registry holds."""


def wrong_record() -> None:
    frames: Registry[ClassEntry[Frame]] = Registry(
        "frame", entry=ClassEntry, register_hint="register_frame()"
    )
    frames.register("c", Frame)  # ty: ignore[invalid-argument-type]
