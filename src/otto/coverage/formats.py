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
