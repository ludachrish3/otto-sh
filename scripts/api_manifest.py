"""Read, validate and compare ``api/public.toml``, the list of otto's public namespaces.

The manifest lists NAMESPACES, never names: a name is public if and only if it
is in a declared namespace's ``__all__`` (spec §4). Each entry records the
tier, the stability and an optional free-text *pending* note::

    [namespaces."otto.host"]
    tier = 1
    stability = "provisional"
    pending = "spec 3 (host construction)"

Removing a namespace, or moving one from ``stable`` back to ``provisional``,
is a breaking change (:func:`manifest_breaks`). Adding a namespace or
promoting one is free.

A ``[formats.<name>]`` table declares one retained versioned interface (dump
spec §13): ``reads`` and ``writes`` point at literal-list constants
(``"otto.link.sentinel:READ_VERSIONS"``). The manifest holds the pointers; the
dump records the values, and ``scripts/api_compat.py`` judges them.
"""

import re
from dataclasses import dataclass
from pathlib import Path

import tomli

STABILITIES = ["provisional", "stable"]
TIERS = [1, 2]
_KEYS = {"tier", "stability", "pending"}
_NAMESPACE_RE = re.compile(r"otto(?:\.\w+)*")
_FORMAT_NAME_RE = re.compile(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*")
_POINTER_RE = re.compile(r"otto(?:\.\w+)*:\w+")
_FORMAT_KEYS = {"reads", "writes"}


class ManifestError(Exception):
    """The manifest is missing, is not TOML, or holds an entry this module refuses."""


@dataclass(frozen=True)
class Namespace:
    """One declared public namespace."""

    name: str
    tier: int
    stability: str
    pending: str = ""


@dataclass(frozen=True)
class Format:
    """One declared versioned format: pointers to its reads and writes constants."""

    name: str
    reads: "str | None"
    writes: "str | None"


def _formats_of(doc: dict) -> "dict[str, Format]":
    table = doc.get("formats", {})
    if not isinstance(table, dict) or not all(isinstance(v, dict) for v in table.values()):
        raise ManifestError("[formats]: expected tables, one per format")
    out: dict[str, Format] = {}
    for name, entry in table.items():
        if not _FORMAT_NAME_RE.fullmatch(name):
            raise ManifestError(f"formats.{name}: a format name is kebab-case")
        extra = sorted(set(entry) - _FORMAT_KEYS)
        if extra:
            raise ManifestError(f"formats.{name}: unknown key(s) {extra}")
        if not entry:
            raise ManifestError(f"formats.{name}: declares neither reads nor writes")
        for key in sorted(entry):
            if not isinstance(entry[key], str) or not _POINTER_RE.fullmatch(entry[key]):
                raise ManifestError(
                    f"formats.{name}.{key}: {entry[key]!r} is not an otto pointer "
                    "('otto.module:CONSTANT')"
                )
        out[name] = Format(name, entry.get("reads"), entry.get("writes"))
    return out


def _load_toml(text: str) -> dict:
    try:
        return tomli.loads(text)
    except tomli.TOMLDecodeError as exc:
        raise ManifestError(f"not valid TOML: {exc}") from exc


def parse_formats(text: str) -> "dict[str, Format]":
    """Parse manifest *text*'s ``[formats.*]`` tables; raise :class:`ManifestError`."""
    return _formats_of(_load_toml(text))


def parse_manifest(text: str) -> dict[str, Namespace]:
    """Parse manifest *text* into ``{namespace: Namespace}``; raise :class:`ManifestError`."""
    doc = _load_toml(text)
    unknown_top = sorted(set(doc) - {"namespaces", "formats"})
    if unknown_top:
        raise ManifestError(f"unknown top-level key(s): {unknown_top}")
    table = doc.get("namespaces")
    if not isinstance(table, dict) or not table:
        raise ManifestError("no [namespaces] table, or it is empty")
    out: dict[str, Namespace] = {}
    for name, entry in table.items():
        if not _NAMESPACE_RE.fullmatch(name):
            raise ManifestError(f"{name!r} is not an otto module path")
        if not isinstance(entry, dict):
            raise ManifestError(f"{name}: expected a table")
        extra = sorted(set(entry) - _KEYS)
        if extra:
            raise ManifestError(f"{name}: unknown key(s) {extra}")
        tier = entry.get("tier")
        stability = entry.get("stability")
        pending = entry.get("pending", "")
        if tier not in TIERS:
            raise ManifestError(f"{name}: tier must be one of {TIERS}, got {tier!r}")
        if stability not in STABILITIES:
            raise ManifestError(
                f"{name}: stability must be one of {STABILITIES}, got {stability!r}"
            )
        if not isinstance(pending, str):
            raise ManifestError(f"{name}: pending must be a string")
        out[name] = Namespace(name, tier, stability, pending)
    _formats_of(doc)
    return out


def _read(path: Path) -> str:
    """Return the manifest's text exactly as the checker reads a committed one.

    Bytes decoded as strict UTF-8, with no newline translation: a lone CR stays
    a lone CR, which TOML refuses, so the developer loop and the checker agree.
    """
    try:
        data = path.read_bytes()
    except FileNotFoundError as exc:
        raise ManifestError(f"{path} does not exist") from exc
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ManifestError(f"{path} is not UTF-8: {exc}") from exc


def load_manifest(path: Path) -> dict[str, Namespace]:
    """Read and parse the manifest at *path*; a missing file is a :class:`ManifestError`."""
    return parse_manifest(_read(path))


def load_formats(path: Path) -> "dict[str, Format]":
    """Read the manifest at *path* and parse its formats; raise :class:`ManifestError`."""
    return parse_formats(_read(path))


def manifest_breaks(parent: dict[str, Namespace], current: dict[str, Namespace]) -> list[str]:
    """Return the breaking changes from *parent* to *current*, one message each."""
    breaks = [f"namespace removed: {name}" for name in sorted(set(parent) - set(current))]
    breaks.extend(
        f"stability downgraded: {name} stable -> provisional"
        for name in sorted(set(parent) & set(current))
        if parent[name].stability == "stable" and current[name].stability == "provisional"
    )
    return breaks
