"""Console-script entry: answer trivial invocations without importing the CLI.

otto's framework import graph costs ~2400 path syscalls, paid at MODULE IMPORT
— before ``otto.cli.main.entry`` runs a line. Skipping bootstrap inside
``entry`` cannot remove that, because the entry module IS the cost. Moving the
entry point earlier is the only thing that does.

Lives at ``otto._shim`` rather than ``otto.cli._shim`` deliberately:
``otto/cli/__init__.py`` is ``from .main import app``, so importing anything
under ``otto.cli`` loads 440 modules first. ``otto/__init__.py`` is PEP-562
lazy, so this module's import costs almost nothing.

Both entry paths route here — the ``otto`` console script (``pyproject.toml``'s
``[project.scripts]``) and ``python -m otto`` (``otto/__main__.py``). One fast
path, so the two cannot diverge.

Imports nothing from otto at module scope.
"""

import sys


def main() -> None:
    """Answer ``--version`` directly; otherwise hand off to the full CLI."""
    # Exact match, never membership: `otto host put --version` is a real
    # subcommand invocation that needs the registry.
    if sys.argv[1:] == ["--version"]:
        from .version import get_version

        # Builtin print, not rich's — and the two are NOT interchangeable:
        # rich's ReprHighlighter colourises a version string on a TTY, so the
        # same binary would disagree with itself depending on which path
        # answered. `otto.cli.main.version_callback` carries the full
        # rationale and prints plainly for the same reason. Importing rich
        # here would also pay back the cost this module exists to avoid.
        # T201 exists to keep LIBRARY code off stdout; this module is the
        # console script itself, and stdout is its entire contract.
        print(f"otto version: {get_version()}")  # noqa: T201
        raise SystemExit(0)

    from .cli.main import entry

    entry()
