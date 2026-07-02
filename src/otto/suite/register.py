"""register_suite() — class decorator that registers an ``OttoSuite`` as a Typer subcommand.

Registration happens at class-definition time (import time), storing each
suite's built sub-app into the module-level :data:`SUITES` registry.
``cli/test.py``'s ``suite_app`` resolves suite subcommands lazily from that
registry through a shared ``RegistryBackedGroup`` (see ``cli/invoke.py``), so
no Typer app mutation happens here or at ``cli/test.py`` import time.

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
from ..registry import Registry


@dataclasses.dataclass(frozen=True)
class SuiteEntry:
    """One registered suite: its Typer sub-app + source file for attribution."""

    name: str
    sub_app: typer.Typer
    file: str


# ---------------------------------------------------------------------------
# Module-level registry — populated by @register_suite() as test files are
# imported during startup; consumed lazily by cli/test.py's RegistryBackedGroup.
# ---------------------------------------------------------------------------
SUITES: Registry[SuiteEntry] = Registry("test suite", register_hint="@otto.register_suite()")
"""Registered ``OttoSuite`` subclasses, keyed by class name; populated at import time."""


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
    class (if present) and registers it into :data:`SUITES`.  ``cli/test.py``'s
    ``suite_app`` resolves it lazily by name through its ``RegistryBackedGroup``.
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

        # run_suite() executes a suite via `pytest.main([suite_file, ...])`,
        # which makes pytest re-import suite_file under its own module name
        # (distinct from the `_otto_suite_*` name otto's own auto-scan uses)
        # every time a suite actually runs — a second, expected execution of
        # this decorator for the SAME class from the SAME file within one
        # process. Re-registration from the identical source file is that
        # expected re-import, not a collision, so it overwrites silently; a
        # different file registering the same class name is a real user
        # error and still fails loudly.
        same_file = suite_class.__name__ in SUITES and SUITES.get(suite_class.__name__).file == (
            suite_file
        )
        SUITES.register(
            suite_class.__name__,
            SuiteEntry(name=suite_class.__name__, sub_app=sub_app, file=suite_file),
            origin=suite_class.__module__,
            overwrite=same_file,
        )

        return suite_class

    return decorator
