"""The versions of the monitor's SQLite session archive (dump spec §13).

``api/public.toml`` points at these lists and the API dump records their
values. A database's version is its ``PRAGMA user_version``. Reading a version
promises nothing about writing into it: a live run appends to an existing
archive only if its version is in the write list. Dropping a version from
either list is a breaking change.

The module imports nothing, so the API-dump producer reads the lists without
importing a reader.
"""

MONITOR_DB_READ_VERSIONS: list[int] = [2]
"""``user_version`` values review mode reads (``read_sessions``)."""

MONITOR_DB_WRITE_VERSIONS: list[int] = [2]
"""``user_version`` values otto stamps on a new archive and will append to."""
