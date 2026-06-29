"""register_suite() — class decorator that registers an ``OttoSuite`` as a Typer subcommand.

Registration happens at class-definition time (import time).  ``cli/test.py``
reads ``_SUITE_REGISTRY`` after ``configmodule`` has finished loading (which is
also when this module is first imported via test-file auto-scan) and adds the
pre-built sub-Typers to ``suite_app``.

No circular imports: this module never imports from ``otto.cli.test`` at module
level.  The runner function uses a lazy import so the actual test execution can
reach ``run_suite`` only when the CLI command is invoked, by which time
``cli/test.py`` is fully loaded.
"""

import dataclasses
import inspect
from collections.abc import Callable
from typing import Any

import typer

from ..params import build_options, options_params

# ---------------------------------------------------------------------------
# Module-level registry — populated by @register_suite() as test files are
# imported during startup; consumed by cli/test.py to build suite_app subcommands.
# ---------------------------------------------------------------------------
_SUITE_REGISTRY: list[tuple[str, typer.Typer]] = []


# ---------------------------------------------------------------------------
# Parameter builders
# ---------------------------------------------------------------------------


def _options_params(opts_cls: type) -> list[inspect.Parameter]:
    """Convert an Options dataclass into inspect.Parameters for Typer.

    Thin wrapper around :func:`otto.params.options_params` kept for
    internal backward compatibility.
    """
    return options_params(opts_cls)


# ---------------------------------------------------------------------------
# register_suite() decorator
# ---------------------------------------------------------------------------


def register_suite(*args: Any, **kwargs: Any) -> Callable[[type], type]:
    """Class decorator that registers an OttoSuite subclass as a ``suite_app`` subcommand.

    Usage::

        from otto import options

        @register_suite()
        class TestMyDevice(OttoSuite):
            \"\"\"Run device validation tests.\"\"\"

            @options
            class Options(RepoOptions):
                firmware: str = "latest"
                check_interfaces: bool = True

            async def test_something(self, suite_options):
                opts = suite_options  # fully-typed Options instance
                ...

    The decorated class is returned unchanged.  The decorator only has a
    side-effect: it builds a Typer sub-app from the class's ``Options`` inner
    class (if present) and appends it to ``_SUITE_REGISTRY``.  ``cli/test.py``
    reads the registry at module load time and adds the sub-apps to ``suite_app``.
    """

    def decorator(suite_class: type) -> type:
        opts_cls = getattr(suite_class, "Options", None)
        suite_file = inspect.getfile(suite_class)

        # Build the full parameter list for the Typer command. The leading
        # ``ctx`` is injected by Typer (recognised by its ``typer.Context``
        # annotation — not exposed as a CLI option) so the runner can read the
        # shared run options the ``otto test`` callback stored in ``ctx.meta``.
        params: list[inspect.Parameter] = [
            inspect.Parameter(
                "ctx",
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                annotation=typer.Context,
            )
        ]
        if opts_cls is not None and dataclasses.is_dataclass(opts_cls):
            params.extend(_options_params(opts_cls))

        # Capture values for the closure — avoids late-binding bugs
        _opts_cls = opts_cls
        _suite_cls = suite_class
        _suite_file = suite_file

        def runner(**kw: Any) -> None:
            ctx = kw.pop("ctx")
            opts_instance = (
                build_options(_opts_cls, kw)
                if (_opts_cls is not None and dataclasses.is_dataclass(_opts_cls))
                else None
            )

            # Lazy import — cli/test.py is fully loaded by the time any command runs
            from ..cli.test import run_suite

            run_suite(_suite_cls, _suite_file, opts_instance, ctx)

        runner.__signature__ = inspect.Signature(params)  # ty: ignore[unresolved-attribute]
        runner.__name__ = suite_class.__name__
        runner.__doc__ = suite_class.__doc__ or f"Run the {suite_class.__name__} test suite."

        sub_app = typer.Typer()
        sub_app.command(suite_class.__name__, *args, **kwargs)(runner)
        _SUITE_REGISTRY.append((suite_class.__name__, sub_app))

        return suite_class

    return decorator
