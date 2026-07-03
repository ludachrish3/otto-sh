"""Runtime access to declarative ``[coverage.tiers]`` config.

Settings are validated at the boundary by
:class:`otto.models.settings.CoverageSettingsSpec`; at runtime the repo
exposes plain dicts.  This module turns those dicts into ordered
:class:`TierConfig` values for the CLI and reporter.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .colors import DEFAULT_TIER_COLORS
from .store.model import TIER_SYSTEM


@dataclass(frozen=True)
class TierConfig:
    """One declared coverage tier, ready for runtime use."""

    name: str
    kind: str
    precedence: int
    color: str
    harvest_dirs: list[Path] = field(default_factory=list)
    max_age_days: int | None = None


def _expand_harvest_dir(raw: str, sut_dir: Path | None) -> Path:
    """Expand ``${sut_dir}`` in a harvest-dir entry, mirroring ``Repo._expand_string``.

    Runtime reads the raw ``[coverage.tiers]`` dict (not the ``${sut_dir}``-
    expanded ``lab_settings`` view), so the substitution the rest of the
    settings file enjoys is applied here on read. With no *sut_dir* the string
    passes through unchanged.
    """
    if sut_dir is not None:
        raw = raw.replace("${sut_dir}", f"{sut_dir}")
    return Path(raw)


def _tier_from_entry(name: str, entry: dict[str, Any], sut_dir: Path | None) -> TierConfig:
    kind = entry["kind"]
    max_age = entry.get("max_age")
    return TierConfig(
        name=name,
        kind=kind,
        precedence=int(entry["precedence"]),
        color=entry.get("color") or DEFAULT_TIER_COLORS[kind],
        harvest_dirs=[_expand_harvest_dir(p, sut_dir) for p in entry.get("harvest_dirs") or []],
        max_age_days=int(max_age[:-1]) if max_age else None,
    )


def load_tiers(cov_config: dict[str, Any], sut_dir: Path | None = None) -> list[TierConfig]:
    """Ordered tier configs (highest precedence first).

    An empty/missing ``tiers`` table yields the implicit ``system`` tier
    only — identical to pre-tier behavior. When *sut_dir* is given, each
    tier's ``harvest_dirs`` has ``${sut_dir}`` expanded (mirroring the rest
    of the repo's settings expansion).
    """
    raw = cov_config.get("tiers") or {}
    if not raw:
        return [
            TierConfig(
                name=TIER_SYSTEM,
                kind="e2e",
                precedence=1,
                color=DEFAULT_TIER_COLORS["e2e"],
            )
        ]
    tiers = [_tier_from_entry(name, entry, sut_dir) for name, entry in raw.items()]
    return sorted(tiers, key=lambda t: t.precedence)


def resolve_get_tier(tiers: list[TierConfig], name: str | None) -> TierConfig:
    """Resolve the target tier for ``otto cov get``.

    ``None`` selects the sole e2e-kind tier; ambiguity or an unknown name
    raises ``ValueError`` listing the candidates.
    """
    if name is not None:
        for t in tiers:
            if t.name == name:
                return t
        raise ValueError(
            f"unknown tier {name!r}; configured tiers: {', '.join(t.name for t in tiers)}"
        )
    e2e = [t for t in tiers if t.kind == "e2e"]
    if len(e2e) != 1:
        raise ValueError(
            "cannot pick a default tier: "
            f"{len(e2e)} e2e-kind tiers configured ({', '.join(t.name for t in e2e)}); "
            "pass --tier NAME"
        )
    return e2e[0]
