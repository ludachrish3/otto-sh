"""Report types the coverage verbs return.

``ok`` is derived from the results and nothing else, so a caller and the CLI
cannot disagree about whether the verb succeeded. Frozen and
equality-comparable; not hashable (they hold dicts and lists).
"""

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from ..utils import Status

if TYPE_CHECKING:
    from ..result import Result


@dataclass(frozen=True)
class FailedReset:
    """One product whose counters could not be cleared."""

    host: str
    product: str
    result: "Result"

    @property
    def reason(self) -> str:
        """Say why, preferring the result's message, then its value, then its status name."""
        return self.result.msg or str(self.result.value or "") or self.result.status.name


def _not_run(result: "Result") -> bool:
    return result.status is Status.NotRun


@dataclass(frozen=True)
class CleanReport:
    """:func:`~otto.coverage.collect.clean_coverage`'s outcome, per host and product."""

    hosts: "dict[str, dict[str, Result]]"
    """Host id -> product name -> that product's counter reset, in walk order."""

    @property
    def failed(self) -> list[FailedReset]:
        """Every reset that ran and failed, in host then product order."""
        return [
            FailedReset(host, product, result)
            for host, products in self.hosts.items()
            for product, result in products.items()
            if not result.is_ok and not _not_run(result)
        ]

    @property
    def cleared(self) -> list[tuple[str, str]]:
        """``(host, product)`` for every reset that succeeded."""
        return [
            (host, product)
            for host, products in self.hosts.items()
            for product, result in products.items()
            if result.is_ok and not _not_run(result)
        ]

    @property
    def not_run(self) -> list[tuple[str, str]]:
        """``(host, product)`` for every reset a dry run declined."""
        return [
            (host, product)
            for host, products in self.hosts.items()
            for product, result in products.items()
            if _not_run(result)
        ]

    @property
    def ok(self) -> bool:
        """True when no reset failed."""
        return not self.failed


@dataclass(frozen=True)
class GetReport:
    """:func:`~otto.coverage.get.get_coverage`'s outcome."""

    cov_dir: Path
    """Where the fetched coverage and the captures were written."""

    tier: str
    """The resolved tier's name."""

    captures: list[Path]
    """One ``capture.json`` per collected (host, product); never empty."""

    manual_captures: list[Path]
    """Copies written into the repo's committed manual store (manual-kind tiers)."""

    clean: "CleanReport | None"
    """The post-retrieval clean, when one was asked for."""

    @property
    def ok(self) -> bool:
        """True unless the asked-for clean failed."""
        return self.clean is None or self.clean.ok
