"""Generate coverage reports from ``otto test --cov`` output.

Merges ``.gcda`` files collected from one or more ``otto test`` runs,
processes them with ``lcov``, and renders a multi-tier HTML report.

**Usage**::

    otto cov report RUN_DIR1 [RUN_DIR2 ...] --report ./my_report

Each *RUN_DIR* is an ``otto test`` output directory containing a ``cov/``
subdirectory with per-host ``.gcda`` files.  Multiple directories can be
specified to stitch together coverage from separate test runs.

Per-host toolchains (``gcov``, ``lcov``) are resolved automatically from
host configuration in ``hosts.json`` or by inspecting ``.gcno`` files.
See the :doc:`/guide/coverage` and :doc:`/guide/host` documentation.

**Options**

``--report PATH``
    Where to place the generated HTML report (default: ``./cov_report``).

``--project-name STR``
    Title shown in the HTML report header.

``--tier NAME[=PATH]``
    Repeatable.  Add a coverage tier to the report.  ``NAME`` is a
    free-form label (e.g. ``unit``, ``manual``, ``integration``); ``PATH``
    is the lcov ``.info`` file feeding that tier.  The bare form
    ``--tier system`` (no path) refers to the implicit system tier
    produced by merging the supplied ``.gcda`` directories.

    The order of ``--tier`` flags is the precedence order: the first flag
    is the highest-precedence tier and wins the row coloring on the
    annotated source view.  If no ``--tier`` flags are given, defaults
    to ``--tier system``.

    Example::

        otto cov report runs/ \\
            --tier unit=u.info \\
            --tier system \\
            --tier integration=i.info \\
            --tier manual=m.info
"""

import asyncio
from pathlib import Path
from typing import Annotated

import typer

from ..coverage.reporter import TierSpec, run_coverage_report
from ..coverage.store.model import TIER_SYSTEM
from ..logger import getOttoLogger

logger = getOttoLogger()

cov_app = typer.Typer(
    name='cov',
    no_args_is_help=True,
    context_settings={
        'help_option_names': ['-h', '--help'],
    },
    help='Generate coverage reports from otto test --cov output.',
)


@cov_app.callback()
def cov_callback() -> None:
    """Generate coverage reports from otto test --cov output."""


def _parse_tier_specs(raw_tiers: list[str]) -> list[TierSpec]:
    """Parse repeated ``--tier NAME[=PATH]`` values into ordered tier specs.

    Order is preserved (= precedence order).  ``--tier system`` without a
    path is allowed and represents the implicit lcov-merged system tier.
    Any other tier without a path is rejected.
    """
    specs: list[TierSpec] = []
    seen: set[str] = set()
    for raw in raw_tiers:
        if "=" in raw:
            name, _, path_str = raw.partition("=")
            name = name.strip()
            if not name:
                raise typer.BadParameter(f"--tier value missing name: {raw!r}")
            if not path_str:
                raise typer.BadParameter(f"--tier value missing path: {raw!r}")
            path: Path | None = Path(path_str)
        else:
            name = raw.strip()
            if not name:
                raise typer.BadParameter("--tier value cannot be empty")
            if name != TIER_SYSTEM:
                raise typer.BadParameter(
                    f"Tier {name!r} requires a path: --tier {name}=PATH "
                    f"(only the {TIER_SYSTEM!r} tier may omit a path)"
                )
            path = None

        if name in seen:
            raise typer.BadParameter(f"Duplicate --tier name: {name!r}")
        seen.add(name)
        specs.append((name, path))

    return specs


@cov_app.command()
def report(
    output_dirs: Annotated[list[Path], typer.Argument(
        help='One or more otto test output directories containing cov/ subdirectories.',
    )],

    report_dir: Annotated[Path, typer.Option(
        '--report', '-r',
        help='Where to place the generated HTML report.',
    )] = Path('./cov_report'),

    project_name: Annotated[str, typer.Option(
        '--project-name',
        help='Title shown in the HTML report header.',
    )] = 'Coverage Report',

    tier: Annotated[list[str], typer.Option(
        '--tier',
        help=(
            'Add a coverage tier as NAME[=PATH]. Repeatable. '
            'Order is precedence order (first = highest). '
            'Use "--tier system" alone to position the implicit '
            'lcov-merged system tier. Defaults to "--tier system".'
        ),
    )] = [],
) -> None:
    """Generate a coverage report from otto test --cov output directories."""
    # Validate output directories
    for d in output_dirs:
        if not d.is_dir():
            logger.error('Output directory does not exist: %s', d)
            raise typer.Exit(1)

    # Parse tier specs (defaulting to system-only)
    try:
        tier_specs: list[TierSpec] = (
            _parse_tier_specs(tier) if tier else [(TIER_SYSTEM, None)]
        )
    except typer.BadParameter as e:
        logger.error('%s', e)
        raise typer.Exit(1) from e

    cov_dirs = [d / 'cov' for d in output_dirs]
    report_dir = report_dir.resolve()

    store = asyncio.run(run_coverage_report(
        cov_dirs, report_dir,
        project_name=project_name, tier_specs=tier_specs,
    ))
    if store is None:
        # run_coverage_report logged the specific warning (missing meta or
        # no host dirs); for the standalone command treat that as an error.
        logger.error(
            'Coverage report not generated — no valid coverage data in: %s',
            ', '.join(str(d) for d in output_dirs),
        )
        raise typer.Exit(1)

    logger.info(
        'Coverage: %.1f%% overall (%d files)',
        store.overall_pct(),
        store.file_count(),
    )
    logger.info('Report: %s', report_dir / 'index.html')
