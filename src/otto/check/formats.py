"""The versions of the report ``otto check --report`` writes (dump spec §13).

``api/public.toml`` points at these lists and the API dump records their
values. A ``*_READ_VERSIONS`` list is every version otto accepts as retained
input; a ``*_WRITE_VERSIONS`` list is every version it emits. Dropping a
version from either is a breaking change; adding one is how otto grows
backwards compatibility. Each reader accepts exactly its read list and each
writer stamps a version taken from its write list.

The module imports nothing, so the API-dump producer reads the lists without
importing a reader.
"""

CHECK_REPORT_WRITE_VERSIONS: list[str] = ["otto-check/1"]
"""``schema`` values of the ``--report`` JSON a user attaches to an issue."""
