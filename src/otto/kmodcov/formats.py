"""The interface numbers otto accepts from a built otto_kmodcov module (dump spec §13).

``api/public.toml`` points at these lists and the API dump records their
values. A ``*_READ_VERSIONS`` list is every version otto accepts as retained
input; a ``*_WRITE_VERSIONS`` list is every version it emits. Dropping a
version from either is a breaking change; adding one is how otto grows
backwards compatibility. Each reader accepts exactly its read list and each
writer stamps a version taken from its write list.

The module imports nothing, so the API-dump producer reads the lists without
importing a reader.
"""

KMODCOV_INTERFACE_READ_VERSIONS: list[int] = [2]
"""``+kmodcov<n>`` interface numbers a built ``.ko``'s ``MODULE_VERSION`` may carry
for otto to drive it."""
