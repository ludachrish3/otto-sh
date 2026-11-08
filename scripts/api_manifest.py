"""Read, validate and compare ``api/public.toml``, the list of otto's public namespaces.

Namespaces decide publicness: a name is public if and only if it is in a
declared namespace's ``__all__`` (spec §4). The ``[symbols]`` table carries
per-path facts about names that are already public and never makes a name
public. Each namespace entry records the
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

A ``[symbols."otto.module:Qualname"]`` table holds the per-symbol facts
(keyword-only spec §4): which leading parameters stay positional and why, and
which rights (construct, subclass, implementer) a class or method gives users.
The reader checks TOML facts only; which keys fit which kind is the dump
child's call, so :class:`Symbol` records the keys an entry wrote.
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
_WHY_NEEDED_AT = 2
SYMBOL_KEYS = {"positional", "why", "construct", "subclass", "implementer", "final"}
_SYMBOL_KEY_RE = re.compile(r"(\w+(?:\.\w+)*):\w+(?:\.\w+)*")


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


@dataclass(frozen=True)
class Symbol:
    """Per-symbol facts for one binding or member key (``otto.host:Host.run``).

    ``given`` holds the keys the entry wrote explicitly: an omitted key and an
    explicit default differ for the child's kind checks. ``final`` is None
    when the entry does not write it (the default: a ``subclass = false``
    class is decorated ``@final``); ``False`` is the sanctioned exemption.
    """

    key: str
    positional: "tuple[str, ...]" = ()
    why: str = ""
    construct: bool = True
    subclass: bool = True
    implementer: bool = False
    final: "bool | None" = None
    given: "frozenset[str]" = frozenset()


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


def _symbols_of(doc: dict) -> "dict[str, Symbol]":
    table = doc.get("symbols", {})
    if not isinstance(table, dict) or not all(isinstance(v, dict) for v in table.values()):
        raise ManifestError("[symbols]: expected tables, one per symbol")
    declared = doc.get("namespaces", {})
    out: dict[str, Symbol] = {}
    for key, entry in table.items():
        match = _SYMBOL_KEY_RE.fullmatch(key)
        if match is None:
            raise ManifestError(f"symbols.{key}: not an otto binding key ('otto.module:Qualname')")
        if match.group(1) not in declared:
            raise ManifestError(
                f"symbols.{key}: {match.group(1)} is not under a declared namespace"
            )
        extra = sorted(set(entry) - SYMBOL_KEYS)
        if extra:
            raise ManifestError(f"symbols.{key}: unknown key(s) {extra}")
        positional = entry.get("positional", [])
        if not isinstance(positional, list) or not all(
            isinstance(n, str) and n.isidentifier() for n in positional
        ):
            raise ManifestError(f"symbols.{key}: positional is a list of names")
        why = entry.get("why", "")
        if not isinstance(why, str):
            raise ManifestError(f"symbols.{key}: why must be a string")
        flags = {}
        for flag, default in (("construct", True), ("subclass", True), ("implementer", False)):
            flags[flag] = entry.get(flag, default)
            if not isinstance(flags[flag], bool):
                raise ManifestError(f"symbols.{key}: {flag} must be true or false")
        if flags["construct"] is False and positional:
            raise ManifestError(f"symbols.{key}: construct = false carries no positional")
        if len(positional) >= _WHY_NEEDED_AT and not why:
            raise ManifestError(f"symbols.{key}: two or more names need a why")
        final = entry.get("final")
        if final is not None and not isinstance(final, bool):
            raise ManifestError(f"symbols.{key}: final must be true or false")
        if final is False and flags["subclass"] is not False:
            raise ManifestError(f"symbols.{key}: final = false needs subclass = false beside it")
        if final is False and not why.strip():
            raise ManifestError(f"symbols.{key}: final = false needs a why naming the subclasses")
        out[key] = Symbol(key, tuple(positional), why, final=final, given=frozenset(entry), **flags)
    return out


def parse_symbols(text: str) -> "dict[str, Symbol]":
    """Parse manifest *text*'s ``[symbols.*]`` tables; raise :class:`ManifestError`."""
    return _symbols_of(_load_toml(text))


def parse_manifest(text: str) -> dict[str, Namespace]:
    """Parse manifest *text* into ``{namespace: Namespace}``; raise :class:`ManifestError`."""
    doc = _load_toml(text)
    unknown_top = sorted(set(doc) - {"namespaces", "formats", "symbols"})
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
    _symbols_of(doc)
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


def load_symbols(path: Path) -> "dict[str, Symbol]":
    """Read the manifest at *path* and parse its symbols; raise :class:`ManifestError`."""
    return parse_symbols(_read(path))


def manifest_breaks(parent: dict[str, Namespace], current: dict[str, Namespace]) -> list[str]:
    """Return the breaking changes from *parent* to *current*, one message each."""
    breaks = [f"namespace removed: {name}" for name in sorted(set(parent) - set(current))]
    breaks.extend(
        f"stability downgraded: {name} stable -> provisional"
        for name in sorted(set(parent) & set(current))
        if parent[name].stability == "stable" and current[name].stability == "provisional"
    )
    return breaks
