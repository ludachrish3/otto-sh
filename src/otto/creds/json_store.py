"""The ``json`` creds store (spec 2026-09-06 creds-store §5.2).

A JSON object mapping inventory key → list of cred entries. ``$schema`` and
``_``-prefixed top-level keys are comment space. Parsed once, on first use —
construction does no I/O, so a lab with no referenced entry never opens it.
"""

import json
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from ..models.base import compact_validation_error
from ..models.host import CredSpec
from .errors import CredsError


def _validated(source: str, key: str, idx: int, entry: Any) -> CredSpec:
    try:
        return CredSpec.model_validate(entry)
    except ValidationError as e:
        raise CredsError(
            f"{source}: key {key!r}: creds[{idx}]: {compact_validation_error(e)}"
        ) from e


def parse_creds_document(data: object, *, source: str) -> dict[str, list[CredSpec]]:
    """``{key: [CredSpec, ...]}`` from a parsed creds document.

    Errors name *source*, the key, the index and the field.

    Two logins may not repeat under one key: the by-login merge (spec §6.1)
    could only resolve that by choosing, which is the one thing it never does.
    """
    if not isinstance(data, dict):
        raise CredsError(f"{source}: must be a JSON object mapping inventory key -> creds list")
    out: dict[str, list[CredSpec]] = {}
    for key, entries in data.items():
        if key == "$schema" or (isinstance(key, str) and key.startswith("_")):
            continue
        if not isinstance(entries, list):
            raise CredsError(f"{source}: key {key!r}: expected a list of creds")
        creds = [_validated(source, key, idx, entry) for idx, entry in enumerate(entries)]
        logins = [c.login for c in creds]
        dupes = sorted({login for login in logins if logins.count(login) > 1})
        if dupes:
            raise CredsError(f"{source}: key {key!r}: duplicate cred login {dupes[0]!r}")
        out[key] = creds
    return out


class JsonCredsStore:
    """Creds store over one JSON file; parsed once, on first use."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.label = f"json:{self.path}"
        self._creds: "dict[str, list[CredSpec]] | None" = None

    def _load(self) -> dict[str, list[CredSpec]]:
        if self._creds is None:
            try:
                data: Any = json.loads(self.path.read_text())
            except (OSError, json.JSONDecodeError) as e:
                raise CredsError(f"{self.path}: {e}") from e
            self._creds = parse_creds_document(data, source=str(self.path))
        return self._creds

    def lookup(self, key: str) -> list[CredSpec]:
        """Return a fresh list of the entries under *key*; ``[]`` when absent."""
        return list(self._load().get(key, []))

    def list_keys(self) -> "list[str] | None":
        """Every key in the file, sorted."""
        return sorted(self._load())

    def fingerprint(self) -> "str | None":
        """Return the file's path, mtime and size, or ``|missing`` if it does not exist."""
        try:
            st = self.path.stat()
        except OSError:
            return f"{self.path}|missing"
        return f"{self.path}|{st.st_mtime_ns}|{st.st_size}"

    def stat_paths(self) -> "list[Path] | None":
        """Return the one file this store's fingerprint is derived from."""
        return [self.path]
