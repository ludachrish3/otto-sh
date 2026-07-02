"""otto — asyncio test orchestrator for embedded and networked lab systems.

Public names are exported lazily (PEP 562): a bare ``import otto`` pulls almost
nothing, and each name below resolves its source module on first attribute
access. This keeps programmatic/library use cheap — the CLI/Typer and lab graph
load only when the relevant API (or the console entry point ``otto:app``) is
actually used. ``from otto import options`` then ``@options`` on an Options
class still works (re-export of ``pydantic.dataclasses.dataclass``).
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pydantic.dataclasses import dataclass as options

    from otto.cli import app
    from otto.logger import get_otto_logger
    from otto.logger import get_otto_logger as get_logger
    from otto.result import CommandResult, Result, Results

    from .configmodule import all_hosts, get_host, get_lab, run_on_all_hosts
    from .context import OttoContext, get_context, open_context, try_get_context

__all__ = [
    "CommandResult",
    "OttoContext",
    "Result",
    "Results",
    "all_hosts",
    "app",
    "get_context",
    "get_host",
    "get_lab",
    "get_logger",
    "get_otto_logger",
    "open_context",
    "options",
    "run_on_all_hosts",
    "try_get_context",
]

# name -> (source module, attribute) resolved on first access by __getattr__.
_LAZY_EXPORTS: dict[str, tuple[str, str]] = {
    "options": ("pydantic.dataclasses", "dataclass"),
    "app": ("otto.cli", "app"),
    "get_otto_logger": ("otto.logger", "get_otto_logger"),
    "get_logger": ("otto.logger", "get_otto_logger"),
    "all_hosts": ("otto.configmodule", "all_hosts"),
    "get_host": ("otto.configmodule", "get_host"),
    "get_lab": ("otto.configmodule", "get_lab"),
    "run_on_all_hosts": ("otto.configmodule", "run_on_all_hosts"),
    "OttoContext": ("otto.context", "OttoContext"),
    "get_context": ("otto.context", "get_context"),
    "open_context": ("otto.context", "open_context"),
    "try_get_context": ("otto.context", "try_get_context"),
    "Result": ("otto.result", "Result"),
    "CommandResult": ("otto.result", "CommandResult"),
    "Results": ("otto.result", "Results"),
}


def __getattr__(name: str) -> object:
    """PEP 562 lazy resolver for otto's public exports."""
    import importlib

    if name in _LAZY_EXPORTS:
        module_name, attr = _LAZY_EXPORTS[name]
        return getattr(importlib.import_module(module_name), attr)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
