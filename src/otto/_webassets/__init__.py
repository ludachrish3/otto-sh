"""Registry of otto's in-package web build artifacts.

These directories are BUILT (by ``make web``), gitignored, and embedded in
the wheel by uv_build's non-VCS-aware file selection — otto labs are
air-gapped, so the frontends must ship inside the package and resolve via
``Path(__file__)`` in source checkouts, editable installs, and wheels alike.

One registry, one ``.gitignore`` glob (``src/otto/_webassets/*/``), one
``web-clean`` target, one ``wheel-check`` loop. The unit-test lane
neutralizes every consumer of these paths by default (see
``tests/_fixtures/webassets.py``); the drift guard
(``tests/unit/test_webassets_guard.py``) pins the coupling.
"""

from pathlib import Path

_ROOT = Path(__file__).parent

#: React dashboard build; served whole at ``/static`` (URLs: /static/dist/*).
MONITOR = _ROOT / "monitor"
#: covapp coverage-SPA bundle (index.html + dist/); copied into every report.
COVAPP = _ROOT / "covapp"

#: Every in-package build artifact. New artifacts MUST be added here — the
#: drift guard fails otherwise.
ALL: dict[str, Path] = {"monitor": MONITOR, "covapp": COVAPP}
