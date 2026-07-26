"""SpaRenderer: emit the covapp SPA coverage report (Plan C).

The pivot renderer — this is what makes the built ``covapp`` bundle (Task 2)
THE coverage report. It copies the static bundle (``index.html`` + ``dist/``)
into the report directory and writes the JS data chunks (Task 1's
``spa_data.emit_chunks``) that bundle consumes at ``file://`` or any served
subpath (spec §2 — no server, no ES modules, no network fetches).

Mirrors the retired Jinja renderer's missing-dist degrade
(``_copy_static``) without importing from it.
"""

import logging
import shutil
from pathlib import Path

from ... import _webassets
from ..store.model import CoverageStore
from .spa_data import emit_chunks, make_stamp

logger = logging.getLogger(__name__)

STATIC_DIR = _webassets.COVAPP
"""The built covapp bundle (``make web`` -> ``vite build --config vite.covapp.config.ts``).

Not committed — a hostless unit-test checkout that skipped ``make web`` has
no ``index.html``/``dist/`` here; ``SpaRenderer._copy_bundle`` degrades
gracefully rather than failing the whole report.
"""


class SpaRenderer:
    """Render a :class:`~otto.coverage.store.model.CoverageStore` to a covapp SPA report directory.

    Args:
        output_dir: Directory to write the report into (created if needed).
        project_name: Title shown in the report (``IndexPayload.project_name``).
        extra_markers: Extra source exclusion-marker strings (spec §8),
            forwarded from ``[coverage.exclusions].markers`` via the
            reporter. Scanned alongside the built-in ``LCOV_EXCL_*`` markers
            when annotating each file's source (see ``spa_data.emit_chunks``).
        prefix: Strip this leading directory from file paths *shown* in the
            report (display only, like ``genhtml --prefix``). Files outside
            the prefix display unchanged; chunk names always use the full
            canonical path, never the display path.
    """

    def __init__(
        self,
        output_dir: Path,
        project_name: str = "Coverage Report",
        *,
        extra_markers: list[str] | None = None,
        prefix: Path | None = None,
    ) -> None:
        self.output_dir = output_dir
        self.project_name = project_name
        self.extra_markers: list[str] = list(extra_markers or [])
        self.prefix = prefix

    def render(self, store: CoverageStore) -> None:
        """Render the full SPA report: copy the bundle, then emit the data chunks."""
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self._copy_bundle()
        emit_chunks(
            store,
            self.output_dir,
            project_name=self.project_name,
            prefix=self.prefix,
            extra_markers=self.extra_markers,
            stamp=make_stamp(),
        )
        logger.info("Report written to %s", self.output_dir / "index.html")

    def _copy_bundle(self) -> None:
        """Copy the built covapp bundle to the report root, excluding sourcemaps.

        ``*.map`` is excluded deliberately: the bundle's hidden sourcemap
        (``dist/covapp.js.map``, ~4.8MB, kept for the TS coverage fold) would
        otherwise bloat every single emitted report by that much.
        """
        if not (STATIC_DIR / "index.html").exists():
            # The bundle is built by `make web` (vite), not committed.
            # Degrade — but say exactly what is missing and how to get it,
            # same rationale as the retired Jinja renderer's dist-less case.
            logger.warning(
                "Coverage report bundle (covapp) is missing — the report cannot "
                "render without it. Run `make web` to build the frontend assets."
            )
            return
        shutil.copytree(
            STATIC_DIR,
            self.output_dir,
            ignore=shutil.ignore_patterns("*.map"),
            dirs_exist_ok=True,
        )
