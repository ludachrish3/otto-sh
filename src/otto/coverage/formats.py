"""The versions of otto's retained coverage files (dump spec §13).

``api/public.toml`` points at these lists and the API dump records their
values. A ``*_READ_VERSIONS`` list is every version otto accepts as retained
input; a ``*_WRITE_VERSIONS`` list is every version it emits. Dropping a
version from either is a breaking change; adding one is how otto grows
backwards compatibility. Each reader accepts exactly its read list and each
writer stamps a version taken from its write list.

The module imports nothing, so the API-dump producer reads the lists without
importing a reader.
"""

STORE_READ_VERSIONS: list[int] = [8]
"""``store.json`` versions ``CoverageStore.load`` accepts."""

STORE_WRITE_VERSIONS: list[int] = [8]
"""``store.json`` versions ``CoverageStore.save`` writes."""

CAPTURE_READ_VERSIONS: list[int] = [3]
"""``capture.json`` versions ``Capture.load`` accepts."""

CAPTURE_WRITE_VERSIONS: list[int] = [3]
"""``capture.json`` versions ``Capture.save`` writes."""

TICKETS_WRITE_VERSIONS: list[int] = [2]
"""``tickets.json`` versions ``build_ticket_export`` writes."""


def found_version_label(found: object) -> str:
    """Name the version stamp a retained file carried, for a reader's refusal.

    Distinguishes "no version key at all" (``"none"``) from "a key was
    present but is not the int the reader expects" (``"'2' (expected an
    int)"``, say, for a hand-edited file): the latter must not collapse to
    "none", which would hide that a value was found, just the wrong shape.
    Every reader of a ``*_READ_VERSIONS`` list phrases its refusal with this.
    """
    if found is None:
        return "none"
    if isinstance(found, int):
        return f"v{found}"
    return f"{found!r} (expected an int)"
