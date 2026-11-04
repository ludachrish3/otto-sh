"""Runtime access to declarative ``[coverage.tiers]`` config.

Settings are validated at the boundary by
:class:`otto.models.settings.CoverageSettingsSpec`; at runtime the project
exposes plain dicts.  This module turns those dicts into ordered
:class:`TierConfig` values for the CLI and reporter.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .colors import DEFAULT_TIER_COLORS
from .errors import CoverageInputError
from .store.model import TIER_SYSTEM

if TYPE_CHECKING:
    from ..models.settings import CoverageTierSpec


@dataclass(frozen=True)
class TierToolchain:
    """A unit tier's ``toolchain`` table, every value as written in the settings.

    The unit preflight resolves each value (a ``PATH`` command or a path) and
    overlays the set ones on what the build's stamps choose.
    """

    coverage_tool: str | None = None
    lcov: str | None = None
    lcov_args: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class TierConfig:
    """One declared coverage tier, ready for runtime use."""

    name: str
    kind: str
    precedence: int
    color: str
    max_age_days: int | None = None
    products: dict[str, list[Path]] = field(default_factory=dict)
    """``unit``-kind only: each product view's build dirs, as written (unanchored)."""
    toolchain: TierToolchain | None = None
    """``unit``-kind only: the tier's own capture tools; ``None`` when no table is set."""

    @property
    def harvests_unit(self) -> bool:
        """Whether a report harvests unit counters for this tier (a unit tier with a view)."""
        return self.kind == "unit" and bool(self.products)


def _tier_from_spec(name: str, spec: "CoverageTierSpec") -> TierConfig:
    toolchain = spec.toolchain
    return TierConfig(
        name=name,
        kind=spec.kind,
        precedence=spec.precedence,
        color=spec.color or DEFAULT_TIER_COLORS[spec.kind],
        max_age_days=int(spec.max_age[:-1]) if spec.max_age else None,
        products={product: list(dirs) for product, dirs in spec.products.items()},
        toolchain=None
        if toolchain is None
        else TierToolchain(
            coverage_tool=toolchain.coverage_tool,
            lcov=toolchain.lcov,
            lcov_args=list(toolchain.lcov_args),
        ),
    )


def load_tiers(cov_config: dict[str, Any], *, root: Path) -> list[TierConfig]:
    """Ordered tier configs (highest precedence first).

    An empty/missing ``tiers`` table yields the implicit ``system`` tier
    only — identical to pre-tier behavior. Each entry is validated through
    :class:`~otto.models.settings.CoverageTierSpec` against *root*, with the
    refusals of settings load, so a library caller handing a raw dict gets
    the same answer the project's settings would. Product dirs stay
    unanchored; :func:`otto.coverage.unit_filter.plan_unit_views` anchors
    them against the project root at consumption time.

    Raises:
        otto.config.coverage_settings.CoverageConfigError: an entry the
            settings model refuses, prefixed ``[coverage.tiers.<name>]``.
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
    # Function-local: the settings models are pydantic-heavy, and a tier load
    # is the only coverage path that needs them.
    from pydantic import ValidationError

    from ..config.coverage_settings import CoverageConfigError
    from ..models.base import compact_validation_error
    from ..models.settings import CoverageTierSpec

    tiers = []
    for name, entry in raw.items():
        try:
            spec = CoverageTierSpec.model_validate(entry, context={"root": root})
        except ValidationError as e:
            raise CoverageConfigError(
                f"[coverage.tiers.{name}] {compact_validation_error(e)}"
            ) from None
        tiers.append(_tier_from_spec(name, spec))
    return sorted(tiers, key=lambda t: t.precedence)


def resolve_get_tier(tiers: list[TierConfig], name: str | None) -> TierConfig:
    """Resolve the target tier for ``otto cov get``.

    ``None`` selects the sole e2e-kind tier; ambiguity or an unknown name
    raises :class:`~otto.coverage.errors.CoverageInputError` (``field="tier"``)
    listing the candidates.
    """
    if name is not None:
        for t in tiers:
            if t.name == name:
                return t
        raise CoverageInputError(
            f"unknown tier {name!r}; configured tiers: {', '.join(t.name for t in tiers)}",
            field="tier",
        )
    e2e = [t for t in tiers if t.kind == "e2e"]
    if len(e2e) != 1:
        raise CoverageInputError(
            "cannot pick a default tier: "
            f"{len(e2e)} e2e-kind tiers configured ({', '.join(t.name for t in e2e)}); "
            "name one of them",
            field="tier",
        )
    return e2e[0]
