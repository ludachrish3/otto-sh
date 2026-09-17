"""The run-tree contract: where per-host and per-product artifacts land.

Every path below a run's output directory that is keyed by host or product
comes from here, so the logs tree and the coverage tree cannot drift apart.
Pure: nothing here touches the filesystem.

.. code-block:: text

    <run>/
      logs/<host_id>/<product>/product/   Product.get_logs output
      logs/<host_id>/<product>/debug/     the product's debug_log_globs haul
      logs/<host_id>/debug/               the host's debug_log_globs haul
      cov/<host_id>/<product>/            .gcda, board.info, capture.json

``debug`` is reserved as a product name because it is a sibling of the
product directories under ``logs/<host_id>/``.
"""

from pathlib import Path

LOGS_DIR = "logs"
COV_DIR = "cov"
PRODUCT_LOGS_DIR = "product"
DEBUG_DIR = "debug"

RESERVED_PRODUCT_NAMES: frozenset[str] = frozenset({DEBUG_DIR})
"""Names a product may not take — each names a sibling directory in the tree."""

_FORBIDDEN_CHARS = ("/", "\\", "\0")


def validate_product_name(name: str) -> None:
    """Reject a product name that is not a single safe path segment.

    Raises:
        ValueError: empty, ``.``/``..``, a path separator or NUL inside, or a
            reserved name (:data:`RESERVED_PRODUCT_NAMES`).
    """
    if not name:
        raise ValueError("product name must not be empty")
    if name in (".", ".."):
        raise ValueError(f"product name {name!r} is not a directory name")
    for ch in _FORBIDDEN_CHARS:
        if ch in name:
            raise ValueError(f"product name {name!r} must be a single path segment (no {ch!r})")
    if name in RESERVED_PRODUCT_NAMES:
        raise ValueError(
            f"product name {name!r} is reserved (it names a sibling directory in the logs tree)"
        )


def host_logs_dir(base: Path, host_id: str) -> Path:
    """``<base>/logs/<host_id>``."""
    return base / LOGS_DIR / host_id


def run_dir_of(host_logs_path: Path) -> Path:
    """Return the run dir *host_logs_path* was built from: the :func:`host_logs_dir` inverse.

    The one place the tree is walked back UP. A caller that holds only a host's
    log root needs the run dir to key anything else off it, and the ``Host``
    protocol exposes ``log_dest`` and nothing above it; spelling the two
    ``.parent`` hops at each call site is how the inverse drifts away from the
    builder. Pure, and unvalidated: it is the path minus its two trailing
    segments, whatever they are.

    (The parameter is not named ``host_logs_dir`` only because that is the
    builder's own name, one scope out.)
    """
    return host_logs_path.parent.parent


def host_debug_dir(base: Path, host_id: str) -> Path:
    """``<base>/logs/<host_id>/debug`` — the host-level ``debug_log_globs`` haul."""
    return host_logs_dir(base, host_id) / DEBUG_DIR


def product_logs_dir(base: Path, host_id: str, product: str) -> Path:
    """``<base>/logs/<host_id>/<product>/product`` — ``Product.get_logs`` output."""
    validate_product_name(product)
    return host_logs_dir(base, host_id) / product / PRODUCT_LOGS_DIR


def product_debug_dir(base: Path, host_id: str, product: str) -> Path:
    """``<base>/logs/<host_id>/<product>/debug`` — the product's debug haul."""
    validate_product_name(product)
    return host_logs_dir(base, host_id) / product / DEBUG_DIR


def cov_host_dir(base: Path, host_id: str) -> Path:
    """``<base>/cov/<host_id>``."""
    return base / COV_DIR / host_id


def cov_product_dir_in(cov_dir: Path, host_id: str, product: str) -> Path:
    """``<cov_dir>/<host_id>/<product>`` — one product's dir under an ALREADY-RESOLVED cov dir.

    *cov_dir* is the local staging root a caller already holds: the CLI's
    ``--cov-dir`` (an arbitrary user path) or ``<run>/cov``. It is the one
    spelling of the ``<host_id>/<product>`` join, so :func:`cov_product_dir`
    — which starts from a run dir instead — delegates here rather than
    repeating it.
    """
    validate_product_name(product)
    return cov_dir / host_id / product


def cov_product_dir(base: Path, host_id: str, product: str) -> Path:
    """``<base>/cov/<host_id>/<product>`` — one product's staged counters and capture."""
    return cov_product_dir_in(base / COV_DIR, host_id, product)
