"""The run-tree contract: where each run's directory sits, and what lands in it.

Every run's directory sits under its command's directory in the xdir
(:func:`command_output_dir`, :func:`run_output_dir`), and every path below it
that is keyed by host or product comes from here too, so writers and readers of
either level cannot drift apart. The path builders are pure: they touch no
filesystem. Two named helpers do: :func:`is_run_dir` (inspection: it reads a
run dir's marker) and :func:`make_run_dir` (creation: the one creator of a run
dir, which writes the marker).

.. code-block:: text

    <xdir>/<command>/<timestamp>[_<subcommand>]/   = <run>, below

    <run>/
      .otto-run                           the marker: otto made this run dir
      logs/<host_id>/<product>/product/   Product.get_logs output
      logs/<host_id>/<product>/debug/     the product's debug_log_globs haul
      logs/<host_id>/debug/               the host's debug_log_globs haul
      cov/<host_id>/<product>/            .gcda, board.info, capture.json
      cov_report/                         the HTML coverage report (no destination given)

The xdir defaults to ``$OTTO_HOME/otto-xdir`` (:func:`otto.home_root.default_xdir`).
``debug`` is reserved as a product name because it is a sibling of the
product directories under ``logs/<host_id>/``.
"""

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

LOGS_DIR = "logs"
COV_DIR = "cov"
COV_REPORT_DIR = "cov_report"
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


def command_dir_name(command: str) -> str:
    """Return the directory name of *command* (or a subcommand): hyphens become underscores."""
    return command.replace("-", "_")


def command_output_dir(xdir: Path, command: str) -> Path:
    """``<xdir>/<command>`` — where each run of *command* gets its directory.

    The one place a command's directory is spelled: the writers
    (:func:`otto.logger.management.create_output_dir`, the library default in
    :mod:`otto.output_dir`), the pruning and any reader that looks for a run
    all start here.
    """
    return xdir / command_dir_name(command)


RUN_DIR_NAME_RE: "re.Pattern[str]" = re.compile(r"^\d{8}_\d{6}_\d{3}(_.+)?$")
"""Matches exactly the names :func:`run_dir_name` writes: ``YYYYMMDD_HHMMSS_mmm[_<sub>]``."""

RUN_MARKER = ".otto-run"
"""The empty dotfile at the top of every run dir otto allocates (:func:`make_run_dir`).

``--log-days`` deletes a directory only when it has otto's run name AND this
marker (:func:`is_run_dir`), so a project's own ``test/20200101_000000_000/``
under ``--xdir .`` is never touched. A dotfile, so ``ls`` of a run dir does
not show it."""


@dataclass(frozen=True)
class RunEntry:
    """The CLI command (and subcommand) a run belongs to: what names its directories."""

    command: str
    subcommand: "str | None" = None


def run_dir_name(started: datetime, subcommand: "str | None" = None) -> str:
    """Return a run dir's name: *started* in UTC to the millisecond, then ``_<subcommand>``."""
    # %f is microseconds; the name keeps milliseconds (drop the last 3 digits).
    stamp = started.astimezone(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")[:-3]
    return stamp if subcommand is None else f"{stamp}_{command_dir_name(subcommand)}"


def run_output_dir(xdir: Path, entry: RunEntry, *, started: "datetime | None" = None) -> Path:
    """``<xdir>/<command>/<timestamp>[_<subcommand>]`` — one run's directory, not created.

    The one builder of a run dir's path, for the CLI and library calls alike;
    *started* defaults to now.
    """
    when = started if started is not None else datetime.now(tz=timezone.utc)
    return command_output_dir(xdir, entry.command) / run_dir_name(when, entry.subcommand)


def make_run_dir(path: Path, *, exist_ok: bool = False) -> Path:
    """Create the run dir *path* (parents too) and write its :data:`RUN_MARKER`; return it.

    The ONE place a run dir is created and marked. *exist_ok* is the sanctioned
    variation: the CLI refuses a collision (``False``), a library call reuses
    its memoised dir (``True``).

    Raises:
        FileExistsError: *path* exists and *exist_ok* is false.
    """
    path.mkdir(parents=True, exist_ok=exist_ok)
    (path / RUN_MARKER).touch()
    return path


def is_run_dir(path: Path) -> bool:
    """Whether *path* is a run dir otto made: run-named, not a symlink, and marked.

    The one test of "this is a run", for the ``--log-days`` prune and every
    reader that looks for runs. The name is matched first and costs nothing;
    only a run-named entry is asked about the filesystem. A symlink is never a
    run (:func:`otto.utils.is_real_dir`, the one rule a deleting walk enters
    by), so a walk that trusts this answer cannot reach through a link into
    another tree.
    """
    from .utils import is_real_dir

    return (
        bool(RUN_DIR_NAME_RE.match(path.name))
        and is_real_dir(path)
        and (path / RUN_MARKER).is_file()
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


def cov_dir(base: Path) -> Path:
    """``<base>/cov`` — a run's staged coverage.

    Where ``otto test --cov`` and ``otto cov get`` put counters and captures,
    and what ``otto cov report`` reads from each run dir it is given.
    """
    return base / COV_DIR


def run_dir_of_cov(cov_dir: Path) -> Path:
    """Return the run dir *cov_dir* was built from: the :func:`cov_dir` inverse.

    The one place a run dir is recovered from its ``cov/``. The report names
    the run each capture came from, and its refusals name the directory to
    report; both start from a cov dir they were handed, never from a
    capture's own path. Pure and unvalidated, like :func:`run_dir_of`.
    """
    return cov_dir.parent


def product_label(owner: str, product: str) -> str:
    """``<owner>/<product>`` — the one spelling of a product's identity in messages and labels.

    *owner* is the host id the product runs on, or a unit tier's name for a
    product a unit tier reports with no host. It is the same
    ``<host_id>/<product>`` join the cov tree uses on disk
    (:func:`cov_product_dir_in`), and the label a coverage run carries.
    """
    return f"{owner}/{product}"


def cov_report_dir(base: Path) -> Path:
    """``<base>/cov_report`` — a run's HTML coverage report when no destination is given."""
    return base / COV_REPORT_DIR


def cov_host_dir(base: Path, host_id: str) -> Path:
    """``<base>/cov/<host_id>``."""
    return cov_dir(base) / host_id


def cov_product_dir_in(cov_dir: Path, host_id: str, product: str) -> Path:
    """``<cov_dir>/<host_id>/<product>`` — one product's dir under an ALREADY-RESOLVED cov dir.

    *cov_dir* is the local staging root a caller already holds, a
    resolved ``<run>/cov`` (see :func:`cov_dir`). It is the one
    spelling of the ``<host_id>/<product>`` join, so :func:`cov_product_dir`
    — which starts from a run dir instead — delegates here rather than
    repeating it.
    """
    validate_product_name(product)
    return cov_dir / host_id / product


def cov_product_dir(base: Path, host_id: str, product: str) -> Path:
    """``<base>/cov/<host_id>/<product>`` — one product's staged counters and capture."""
    return cov_product_dir_in(cov_dir(base), host_id, product)
