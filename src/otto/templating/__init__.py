"""Jsonnet templates for labs and inventories.

Every name is exported lazily (PEP 562), the shape every otto package shares:
importing this package, or naming an error class, loads no Jsonnet binding.
Only :func:`~otto.templating.binding.load_binding` imports one.
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .binding import JsonnetBinding as JsonnetBinding
    from .binding import load_binding as load_binding
    from .errors import RenderCancelled as RenderCancelled
    from .errors import RenderError as RenderError
    from .errors import TemplatingError as TemplatingError
    from .errors import TemplatingUnavailableError as TemplatingUnavailableError

# name -> the module that defines it, imported on first access by __getattr__.
_LAZY_ATTRS: dict[str, str] = {
    "JsonnetBinding": "otto.templating.binding",
    "load_binding": "otto.templating.binding",
    "RenderCancelled": "otto.templating.errors",
    "RenderError": "otto.templating.errors",
    "TemplatingError": "otto.templating.errors",
    "TemplatingUnavailableError": "otto.templating.errors",
}


def __getattr__(name: str) -> object:
    """PEP 562 lazy resolver for otto.templating's public exports."""
    import importlib

    if name in _LAZY_ATTRS:
        return getattr(importlib.import_module(_LAZY_ATTRS[name]), name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    """Include the lazy exports in dir()/tab-completion; the module dict holds none of them."""
    return sorted(set(globals()) | set(_LAZY_ATTRS))


__all__ = [
    "JsonnetBinding",
    "RenderCancelled",
    "RenderError",
    "TemplatingError",
    "TemplatingUnavailableError",
    "load_binding",
]
