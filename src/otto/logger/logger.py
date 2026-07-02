"""otto's logger accessor.

``'otto'`` is a plain ``logging.Logger`` (no subclass). otto modules emit via
``get_logger()`` / ``logging.getLogger(__name__)``; child loggers propagate
to ``'otto'``, where the CLI attaches handlers (see ``otto.logger.management``).
Configuring/replacing handlers is up to the application — otto-the-library only
emits (and ``otto.logger`` attaches a ``NullHandler``).
"""

from logging import Logger, getLogger


def get_logger(name: str | None = None) -> Logger:
    """Return the ``'otto'`` logger (or the ``'otto.<name>'`` child)."""
    return getLogger(f"otto.{name}" if name else "otto")
