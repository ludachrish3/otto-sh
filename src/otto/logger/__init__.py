"""otto logging package: formatters, level aliases, and CLI-side management.

otto modules emit via ``logging.getLogger(__name__)`` directly — this package
holds no logger accessor of its own. The library-citizen ``NullHandler`` is
attached at ``otto/__init__.py`` (so it fires on ANY ``import otto``, not just
``import otto.logger``); see that module.

``management`` (CLI handler config: three-sink logging, output-dir rotation)
pulls in ``rich``, so it is exported lazily (PEP 562) — a bare
``import otto.logger`` (or importing a submodule like ``otto.logger.mode``,
which runs this package's ``__init__`` first) must stay rich-free.

``levels`` (the WARN/CRIT level-name aliases) is imported eagerly below —
it is stdlib-only (safe ahead of the lazy-export table) and its
``addLevelName`` side effect must be live before any formatter renders a
level name.
"""

from typing import TYPE_CHECKING

# Side-effect import: registers the WARN/CRIT level-name aliases (F401 exempt
# on __init__.py — see .ruff.toml).
from . import levels

if TYPE_CHECKING:
    from . import management as management
    from .management import install as install
    from .management import reset as reset

_LAZY_EXPORTS: dict[str, str] = {
    "management": "otto.logger.management",
}

_LAZY_ATTRS: dict[str, str] = {
    "install": "otto.logger.management",
    "reset": "otto.logger.management",
}
"""Attributes resolved OUT of a lazily-imported module (spec 2026-08-30 §3.4).

``otto.logger.install`` / ``otto.logger.reset`` are the embedder's pair, and
both live on ``management`` — so naming them here keeps
``from otto.logger import install, reset`` working without importing
``management`` (and therefore ``rich``) for the callers that merely
``import otto.logger``. Calling either one IS the opt-in, so paying for rich
at that moment is the point.
"""


def __getattr__(name: str) -> object:
    """PEP 562 lazy resolver for otto.logger's public exports."""
    import importlib

    if name in _LAZY_EXPORTS:
        return importlib.import_module(_LAZY_EXPORTS[name])
    if name in _LAZY_ATTRS:
        return getattr(importlib.import_module(_LAZY_ATTRS[name]), name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
