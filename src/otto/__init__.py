"""otto — asyncio test orchestrator for embedded and networked lab systems.

Public names are exported lazily (PEP 562): a bare ``import otto`` pulls almost
nothing, and each name below resolves its source module on first attribute
access. This keeps programmatic/library use cheap — the CLI/Typer and lab graph
load only when the relevant API (or the console entry point ``otto:app``) is
actually used. ``from otto import options`` then ``@options`` on an Options
class still works (re-export of ``pydantic.dataclasses.dataclass``).
"""

import logging as _logging
from typing import TYPE_CHECKING

# Library-citizen default: attach a NullHandler to the 'otto' logger so a bare
# `import otto` is silent unless the application configures handlers. Lives
# here (not otto.logger) so it fires on ANY import of the otto package, not
# only on `import otto.logger` / access to a lazy export that pulls it in.
# stdlib-only; idempotent (safe under repeated import / reload).
_otto_logger = _logging.getLogger("otto")
if not any(isinstance(h, _logging.NullHandler) for h in _otto_logger.handlers):
    _otto_logger.addHandler(_logging.NullHandler())

if TYPE_CHECKING:
    from pydantic.dataclasses import dataclass as options

    from otto.cli import app
    from otto.cli.registry import cli_command, register_cli_command
    from otto.host.app_shell import AppShell, Parsed
    from otto.host.login_proxy import Cred, register_login_proxy
    from otto.result import CommandResult, Result, Results, ShellResult
    from otto.suite.run import RunOptions, run_suite

    from .config import all_hosts, get_host, get_lab, load_lab, run_on_all_hosts
    from .context import OttoContext, get_context, open_context, try_get_context

__all__ = [
    "AppShell",
    "CommandResult",
    "Cred",
    "OttoContext",
    "Parsed",
    "Result",
    "Results",
    "RunOptions",
    "ShellResult",
    "all_hosts",
    "app",
    "cli_command",
    "get_context",
    "get_host",
    "get_lab",
    "load_lab",
    "open_context",
    "options",
    "register_cli_command",
    "register_login_proxy",
    "run_on_all_hosts",
    "run_suite",
    "try_get_context",
]

# name -> (source module, attribute) resolved on first access by __getattr__.
_LAZY_EXPORTS: dict[str, tuple[str, str]] = {
    "options": ("pydantic.dataclasses", "dataclass"),
    "app": ("otto.cli", "app"),
    "all_hosts": ("otto.config", "all_hosts"),
    "get_host": ("otto.config", "get_host"),
    "get_lab": ("otto.config", "get_lab"),
    "load_lab": ("otto.config", "load_lab"),
    "run_on_all_hosts": ("otto.config", "run_on_all_hosts"),
    "OttoContext": ("otto.context", "OttoContext"),
    "get_context": ("otto.context", "get_context"),
    "open_context": ("otto.context", "open_context"),
    "try_get_context": ("otto.context", "try_get_context"),
    "Result": ("otto.result", "Result"),
    "CommandResult": ("otto.result", "CommandResult"),
    "Results": ("otto.result", "Results"),
    "register_cli_command": ("otto.cli.registry", "register_cli_command"),
    "cli_command": ("otto.cli.registry", "cli_command"),
    "Cred": ("otto.host.login_proxy", "Cred"),
    "register_login_proxy": ("otto.host.login_proxy", "register_login_proxy"),
    "AppShell": ("otto.host.app_shell", "AppShell"),
    "Parsed": ("otto.host.app_shell", "Parsed"),
    "ShellResult": ("otto.result", "ShellResult"),
    "run_suite": ("otto.suite.run", "run_suite"),
    "RunOptions": ("otto.suite.run", "RunOptions"),
}


def __getattr__(name: str) -> object:
    """PEP 562 lazy resolver for otto's public exports."""
    import importlib

    if name in _LAZY_EXPORTS:
        module_name, attr = _LAZY_EXPORTS[name]
        return getattr(importlib.import_module(module_name), attr)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
