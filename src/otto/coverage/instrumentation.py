"""Detect coverage instrumentation per product and decide whether retrieval runs.

Detection is local: every product's :meth:`~otto.host.product.Product.instrumented`
reads the artifact on the otto machine, so this costs no host round trip and
runs before anything executes. The decision table (spec §8):

======  ================  ==================  =====================================
mode    instrumented set  ``[coverage]``      outcome
======  ================  ==================  =====================================
auto    some              yes                 on; listing at INFO
auto    some              no                  WARNING naming the products; off
auto    none              any                 off; listing at DEBUG
forced  none              any                 refuses before anything runs
any on  partial           yes                 WARNING listing the missing; on
======  ================  ==================  =====================================

The refusal is :class:`~otto.coverage.errors.CoverageNotInstrumentedError`,
which carries this report as structure as well as text so a console caller can
render :meth:`InstrumentationReport.table`.

``None`` (unknown) counts as not instrumented everywhere, and the table says
``unknown`` so the remedy is visible.
"""

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from rich import box
from rich.markup import escape
from rich.table import Table

from .errors import CoverageNotInstrumentedError

if TYPE_CHECKING:
    from ..config.repo import Repo
    from ..host.product import Product

logger = logging.getLogger(__name__)

_LABEL = {True: "yes", False: "no", None: "unknown"}
_REMEDY = (
    "unknown = the product cannot tell: override Product.instrumented(), or set "
    "`instrumented = true` on the [[products]] entry"
)


def _rows_text(rows: "list[InstrumentationRow]") -> str:
    """Render ``  host: product — label`` per row.

    Appends :data:`_REMEDY` once, at the end, whenever any row's verdict is
    ``None`` — the one place the "unknown" label's remedy needs to live so
    every renderer (``describe()``, both partial-instrumentation warnings)
    shows it without re-spelling it.
    """
    lines = [f"  {r.host_id}: {r.product} — {_LABEL[r.verdict]}" for r in rows]
    if any(r.verdict is None for r in rows):
        lines.append(f"  ({_REMEDY})")
    return "\n".join(lines)


@dataclass(frozen=True)
class InstrumentationRow:
    """One (host, product) verdict."""

    host_id: str
    product: str
    verdict: bool | None


@dataclass
class InstrumentationReport:
    """Every coverage host's products and whether each is instrumented."""

    rows: list[InstrumentationRow]

    def instrumented(self) -> list[InstrumentationRow]:
        """Rows verified instrumented (``True`` only)."""
        return [r for r in self.rows if r.verdict is True]

    def missing(self) -> list[InstrumentationRow]:
        """Rows that are not instrumented — ``False`` and ``unknown`` alike."""
        return [r for r in self.rows if r.verdict is not True]

    def table(self) -> Table:
        """Build a Rich table of the verdicts (the console rendering).

        Every string handed to Rich is ESCAPED first. The remedy caption ends
        in ``the [[products]] entry``, and Rich reads ``[word]`` as a style tag
        and deletes it — unescaped, the caption renders as "the [] entry",
        which points the reader at nothing. Host ids and product names are
        validated as single path segments, which does not exclude a bracket,
        so they go through the same door rather than relying on that.
        """
        caption = escape(_REMEDY) if any(r.verdict is None for r in self.rows) else None
        table = Table(box=box.ROUNDED, title="coverage instrumentation", caption=caption)
        table.add_column("host")
        table.add_column("product")
        table.add_column("instrumented")
        for row in self.rows:
            table.add_row(escape(row.host_id), escape(row.product), _LABEL[row.verdict])
        return table

    def describe(self) -> str:
        """Plain-text verdicts for log lines and error messages."""
        if not self.rows:
            return "no products on any coverage host"
        return _rows_text(self.rows)


def instrumented_products(host: Any) -> "list[Product]":
    """*host*'s products whose build is known to be instrumented."""
    return [p for p in host.products if p.instrumented() is True]


def detect(hosts: Iterable[Any]) -> InstrumentationReport:
    """One row per product per host, in host then declaration order."""
    rows = [
        InstrumentationRow(host.id, product.name, product.instrumented())
        for host in hosts
        for product in host.products
    ]
    return InstrumentationReport(rows)


def detect_for_lab(repos: "list[Repo] | None" = None) -> InstrumentationReport:
    """Detect over every host the ``[coverage].hosts`` selector matches.

    With no ``[coverage]`` table the selector's own default applies: every
    host in the fleet. Container hosts are included — a product can live in
    a container (spec §13).
    """
    from ..config import all_hosts, get_repos
    from .config import get_cov_config, load_hosts_pattern

    if repos is None:
        repos = get_repos()
    cov_config = get_cov_config(repos)
    pattern = load_hosts_pattern(cov_config)
    return detect(all_hosts(pattern=pattern, include_containers=True))


def decide_coverage(
    mode: bool | None,
    report: InstrumentationReport,
    *,
    has_cov_config: bool,
    command: str,
) -> bool:
    """Apply the module's decision table; return whether retrieval runs.

    *mode*: ``True`` forced on (``--cov`` / ``otto cov get``), ``False``
    forced off (``--no-cov``), ``None`` auto. *command* names the invocation
    in messages.
    """
    if mode is False:
        return False
    instrumented = report.instrumented()
    missing = report.missing()
    if mode is True:
        if not instrumented:
            # Both variants carry the report as structure as well as text: a
            # console caller renders `table()`, while the message keeps the
            # plain `describe()` listing for the run log and for any caller
            # that is not a console (a log file cannot hold a Rich table).
            if not report.rows:
                raise CoverageNotInstrumentedError(
                    f"{command}: no products on any coverage host — coverage cannot be collected.",
                    report,
                )
            raise CoverageNotInstrumentedError(
                f"{command}: no instrumented product — coverage cannot be collected.\n"
                f"{report.describe()}",
                report,
            )
    else:
        # auto
        if not instrumented:
            logger.debug(
                "%s: no instrumented products; coverage stays off\n%s", command, report.describe()
            )
            return False
        if not has_cov_config:
            logger.warning(
                "%s: instrumented product(s) found (%s) but no [coverage] table is configured — "
                "add [coverage] (and a tier) to .otto/settings.toml to collect their counters",
                command,
                ", ".join(f"{r.product}@{r.host_id}" for r in instrumented),
            )
            return False
        logger.info(
            "%s: coverage retrieval on — instrumented products detected:\n%s",
            command,
            report.describe(),
        )
    # Common "on" tail: forced-True-with-instrumented and auto-with-config-and-instrumented
    # both land here, so the partial-instrumentation warning is written once.
    if missing:
        logger.warning(
            "%s: %d product(s) are not instrumented and will contribute no coverage:\n%s",
            command,
            len(missing),
            _rows_text(missing),
        )
    return True
