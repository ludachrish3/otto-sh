"""The versions of the documents whose models live in :mod:`otto.models` (dump spec §13).

``api/public.toml`` points at these lists and the API dump records their
values. A ``*_READ_VERSIONS`` list is every version otto accepts as retained
input; a ``*_WRITE_VERSIONS`` list is every version it emits. Dropping a
version from either is a breaking change; adding one is how otto grows
backwards compatibility.

The module imports nothing. ``otto.models.monitor`` and
``otto.models.settings`` never import it: they load on budgeted CLI
surfaces, so ``MonitorExport.format``, ``MonitorSessionFragment.format`` and
``ReservationFile.version`` keep a native ``Literal`` that spells the read
list again.
``tests/unit/models/test_declared_version_fields.py`` holds each ``Literal``'s
arguments equal to the list here, so a version is added or dropped in both
places in one commit.
"""

MONITOR_EXPORT_READ_VERSIONS: list[int] = [1]
"""Monitor export ``format`` versions review mode and the browser read."""

MONITOR_EXPORT_WRITE_VERSIONS: list[int] = [1]
"""Monitor export ``format`` versions otto and the browser emit."""

MONITOR_STREAM_READ_VERSIONS: list[int] = [1]
"""Live-stream fragment ``format`` versions the dashboard reads from ``/api/stream``.

The live stream is its own format, not the export document: each fragment is
a partial session record (``MonitorSessionFragment``), versioned on its own.
"""

MONITOR_STREAM_WRITE_VERSIONS: list[int] = [1]
"""Live-stream fragment ``format`` versions the collector and the browser stamp."""

RESERVATIONS_READ_VERSIONS: list[int] = [1]
"""JSON reservation file ``version`` values the built-in JSON backend reads."""
