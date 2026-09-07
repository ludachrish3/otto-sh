"""The ``CredsStore`` protocol (spec 2026-09-06 creds-store §5.1).

A store answers "which credentials does the machine with this inventory key
accept" with the same :class:`~otto.models.host.CredSpec` entries a lab file
spells. It is the LOWEST creds layer: an inventory record and the lab file
override it by login (spec §6). Files whose stat IS the store's freshness are
declared through :class:`otto.inventory.protocol.SupportsStatPaths`, reused
rather than redeclared.
"""

from typing import Protocol, runtime_checkable

from ..models.host import CredSpec


@runtime_checkable
class CredsStore(Protocol):
    """Credentials by inventory key."""

    @property
    def label(self) -> str:
        """Name this store in provenance and errors, e.g. ``json:/home/me/lab_data/creds.json``."""
        ...

    def lookup(self, key: str) -> list[CredSpec]:
        """Return the entries for *key* — ``[]`` when the store holds none for it.

        Raises :class:`~otto.creds.errors.CredsError` only when the store
        cannot answer at all (unreadable file, unreachable service).
        """
        ...

    def list_keys(self) -> "list[str] | None":
        """Return every key this store holds, sorted; ``None`` if it cannot enumerate."""
        ...

    def fingerprint(self) -> "str | None":
        """Return a value that changes whenever the entries may have; ``None`` = not cacheable."""
        ...
