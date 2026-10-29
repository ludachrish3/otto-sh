"""The ``cov/<host>/<product>/`` walk, written once for every consumer.

Collection stages each instrumented product's counters under
``<cov_dir>/<host_id>/<product>/`` (see
:func:`otto.layout.cov_product_dir_in`).  Capture production and the
reporter both have to walk that tree, and both have to refuse the
pre-product one-level layout rather than misread a host directory as a
product — so they walk it here, and the refusal reads the same wherever
it fires.
"""

from pathlib import Path
from typing import NamedTuple

from ..config.coverage_settings import CoverageConfigError


class ProductKey(NamedTuple):
    """One (host, product) pair of a cov dir: ``<cov_dir>/<host>/<product>/``.

    A named pair: it sorts host first, unpacks as ``host, product``, and is
    the key of every per-product map a collection builds.
    """

    host: str
    product: str

    @property
    def label(self) -> str:
        """``<host>/<product>``: how every message names the product."""
        return f"{self.host}/{self.product}"


def _holds_data_directly(host_dir: Path) -> bool:
    """Report whether *host_dir* itself carries a capture or counters (one-level tree)."""
    return (host_dir / "capture.json").is_file() or next(host_dir.glob("*.gcda"), None) is not None


def iter_product_dirs(cov_dir: Path) -> list[tuple[str, str, Path]]:
    """Return ``(host_id, product, dir)`` for every ``<cov_dir>/<host>/<product>/``, sorted.

    Nothing is filtered on content: a caller that only wants product dirs
    holding ``.gcda`` (or a ``capture.json``) applies that test itself.
    Non-directories at either level — the ``.otto_cov_meta.json`` sidecar,
    a stray file — are skipped silently, as are dot-directories (the
    capture producer's ``.otto-stage/`` scratch copy of a product's counters,
    present while that product captures, is one), and a *cov_dir* that is
    missing or empty yields no entries.

    Args:
        cov_dir: A coverage directory (``--cov-dir`` or ``<run>/cov``).

    Returns:
        One tuple per product directory, sorted by host id then product.

    Raises:
        otto.config.coverage_settings.CoverageConfigError: A host directory holds
            coverage data directly — the pre-product one-level tree. There
            is no migration shim; the message names the directory and the
            layout otto expects.
    """
    if not cov_dir.is_dir():
        return []
    found: list[tuple[str, str, Path]] = []
    for host_dir in sorted(cov_dir.iterdir()):
        if not host_dir.is_dir() or host_dir.name.startswith("."):
            continue
        if _holds_data_directly(host_dir):
            raise CoverageConfigError(
                f"{host_dir} holds coverage data directly under the host directory; otto "
                "expects cov/<host>/<product>/ — re-collect with this version of otto"
            )
        for product_dir in sorted(host_dir.iterdir()):
            if not product_dir.is_dir() or product_dir.name.startswith("."):
                continue
            found.append((host_dir.name, product_dir.name, product_dir))
    return found
