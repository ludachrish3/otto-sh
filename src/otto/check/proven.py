"""The environments otto has PROVEN its link/tunnel features on (spec 2026-09-24 §3.4).

One packaged data file, ``proven.json``, is the single home for this data: the
checks label every fingerprinted component against it and the docs build
renders it into the "Known-good environments" page. Rows are added only from
otto's own bed runs; a user's ``--report`` proposes a row, it never adds one.
"""

import enum
import json
import re
from dataclasses import dataclass
from importlib import resources

ORDERED_COMPONENTS = ["iproute2", "kernel", "socat", "bash"]
"""Components with a version order; the rest are compared by membership."""

_SS_RE = re.compile(r"^ss(\d{6})$")
_DOTTED_RE = re.compile(r"^(\d+(?:\.\d+)*)")


class RangeLabel(enum.Enum):
    """Where one fingerprinted version sits relative to the proven range."""

    WITHIN = "within"
    OLDER = "older"
    NEWER = "newer"
    OUTSIDE = "outside"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ProvenEntry:
    """One proven version, and where and when otto proved it."""

    version: str
    where: str
    when: str


@dataclass(frozen=True)
class ProvenRange:
    """The whole proven-range file."""

    revision: str
    components: dict[str, list[ProvenEntry]]


def load_proven_range() -> ProvenRange:
    """Load the packaged ``proven.json``."""
    text = resources.files("otto.check").joinpath("proven.json").read_text(encoding="utf-8")
    raw = json.loads(text)
    return ProvenRange(
        revision=raw["revision"],
        components={
            name: [ProvenEntry(**entry) for entry in entries]
            for name, entries in raw["components"].items()
        },
    )


def version_key(component: str, version: str) -> list[int] | None:
    """Sortable key for *version* of *component*, or ``None`` when unparseable.

    iproute2 used date versions (``ss170501``) before dotted ones, so a date
    version is prefixed ``0`` and a dotted one ``1``: every date-era release
    sorts before every dotted release.
    """
    if component == "iproute2":
        ss = _SS_RE.match(version)
        if ss:
            return [0, int(ss.group(1))]
        dotted = _dotted(version)
        return [1, *dotted] if dotted is not None else None
    return _dotted(version)


def _dotted(version: str) -> list[int] | None:
    """Parse the leading ``N.N.N`` of *version* (``6.8.0-86-generic`` → ``[6, 8, 0]``)."""
    match = _DOTTED_RE.match(version)
    return [int(part) for part in match.group(1).split(".")] if match else None


def label_against_range(proven: ProvenRange, component: str, version: str | None) -> RangeLabel:
    """Label *version* of *component* against *proven*."""
    entries = proven.components.get(component, [])
    if version is None or not entries:
        return RangeLabel.UNKNOWN
    if component not in ORDERED_COMPONENTS:
        known = {e.version for e in entries}
        return RangeLabel.WITHIN if version in known else RangeLabel.OUTSIDE
    key = version_key(component, version)
    keys = [k for k in (version_key(component, e.version) for e in entries) if k is not None]
    if key is None or not keys:
        return RangeLabel.UNKNOWN
    if key < min(keys):
        return RangeLabel.OLDER
    if key > max(keys):
        return RangeLabel.NEWER
    return RangeLabel.WITHIN
