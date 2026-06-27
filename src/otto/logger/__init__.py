import logging

from . import management as management
from .logger import get_otto_logger as get_otto_logger

# Library-citizen default: attach a NullHandler so importing otto as a library
# is silent unless the application configures handlers. Idempotent.
_otto = logging.getLogger("otto")
if not any(isinstance(h, logging.NullHandler) for h in _otto.handlers):
    _otto.addHandler(logging.NullHandler())
