"""register_suite_class() — registers an ``OttoSuite`` subclass as a Typer subcommand.

Registration happens at class-definition time (import time), via
``OttoSuite.__init_subclass__`` calling ``register_suite_class()`` for every
``Test*``-named subclass, storing each suite's built sub-app into the
module-level :data:`SUITES` registry. ``cli/test.py``'s ``suite_app`` resolves
suite subcommands lazily from that registry through a shared
``RegistryBackedGroup`` (see ``cli/invoke.py``), so no Typer app mutation
happens here or at ``cli/test.py`` import time.

No heavy imports at module load: the runner reaches the suite-run engine
(:func:`otto.suite.run.run_suite`) through a lazy import so the pytest-touching
core is pulled only when a suite command actually runs, not at registration
(import) time.
"""

import dataclasses
import inspect
from typing import TYPE_CHECKING, Any

import typer

from ..params import build_options, options_params
from ..registry import Registry
from ..result import CommandResult
from ..utils import DRY_RUN_HEADLINE, Status

if TYPE_CHECKING:
    from _typeshed import DataclassInstance


@dataclasses.dataclass(frozen=True)
class SuiteEntry:
    """One registered suite: its Typer sub-app + source file + suite class."""

    name: str
    sub_app: typer.Typer
    file: str
    cls: type


# ---------------------------------------------------------------------------
# Module-level registry — populated by register_suite_class() (called from
# OttoSuite.__init_subclass__) as test files are imported during startup;
# consumed lazily by cli/test.py's RegistryBackedGroup.
# ---------------------------------------------------------------------------
SUITES: Registry[SuiteEntry] = Registry(
    "test suite", register_hint="subclass otto.suite.OttoSuite with a Test*-prefixed name"
)
"""Registered ``OttoSuite`` subclasses, keyed by class name; populated at import time."""


# ---------------------------------------------------------------------------
# Parameter builders
# ---------------------------------------------------------------------------


def _options_params(opts_cls: "type[DataclassInstance]") -> list[inspect.Parameter]:
    """Convert an Options dataclass into inspect.Parameters for Typer.

    Thin wrapper around :func:`otto.params.options_params` kept for
    internal backward compatibility.
    """
    return options_params(opts_cls)


def bound_test_names(suite_class: type) -> list[str]:
    """Return the suite's ``test_*`` methods, in definition order across the MRO.

    Read off the bound class, not from a pytest collection: the class object
    IS the binding, so this needs no conftest, no fixtures and no import of
    anything the suite would touch at run time. Parametrizations are not
    expanded — they are a collection-time fact, and a dry run that pretended
    to know them would be inventing exactly the kind of detail this contract
    exists to stop inventing.
    """
    names: list[str] = []
    for klass in reversed(suite_class.__mro__):
        for name, member in vars(klass).items():
            if name.startswith("test") and callable(member) and name not in names:
                names.append(name)
    return names


def _print_suite_dry_run(ctx: typer.Context, suite_class: type) -> None:
    """Print ``otto test``'s preview: the suite bound, the tests, and nothing run.

    Printed to the console rather than logged, for the reason the CLI seam
    prints its own block that way — a dry run whose output is empty is a bug,
    so the announcement must not be foldable by a log mode or a level.
    """
    from rich import print as rprint
    from rich.markup import escape

    names = bound_test_names(suite_class)
    rprint(f"[magenta]{escape(DRY_RUN_HEADLINE)}[/magenta]")
    rprint(f"  would run: {escape(ctx.command_path)}")
    rprint(
        f"  suite: {escape(suite_class.__name__)} imported and bound; "
        f"{len(names)} test(s), no test body will run"
    )
    for name in names:
        rprint(f"    - {escape(name)}")


# ---------------------------------------------------------------------------
# register_suite_class
# ---------------------------------------------------------------------------


def register_suite_class(suite_class: type) -> None:
    """Register an OttoSuite subclass as an ``otto test`` subcommand.

    Called automatically by ``OttoSuite.__init_subclass__`` for every subclass
    whose name matches pytest's own collection rule (``Test*``). Builds a Typer
    sub-app from the class's ``Options`` inner class (if present) and registers
    it into :data:`SUITES`; ``cli/test.py``'s ``suite_app`` resolves it lazily
    by name through its ``RegistryBackedGroup``.
    """
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

    def runner(**kw: Any) -> "CommandResult | None":
        ctx = kw.pop("ctx")
        opts_instance = (
            build_options(_opts_cls, kw)
            if (_opts_cls is not None and dataclasses.is_dataclass(_opts_cls))
            else None
        )

        # The dry-run preview, and the reason this leaf opts out of the CLI's
        # seam default (stamped below). Reaching this line already IS the
        # preview's substance: the suite module imported and this class bound
        # at registration time, its Options dataclass built above, and every
        # `test_*` on the class is therefore known — all without pytest
        # collecting, and with no device contacted. Printing the bound tests
        # and returning is where `otto test -n` stops; run_suite (the thing
        # that runs bodies) is never reached.
        from ..host.host import is_dry_run

        if is_dry_run():
            _print_suite_dry_run(ctx, _suite_cls)
            return None

        # Call the suite-run library directly (class-first). The engine derives
        # the suite's source file itself (inspect.getfile) and returns a
        # SuiteRunResult. Lazy import keeps the pytest-touching engine off the
        # import-time path.
        from ..context import get_context
        from .run import RUN_OPTIONS_KEY, RunOptions, run_suite

        stored = ctx.meta.get(RUN_OPTIONS_KEY)
        run_options = stored if isinstance(stored, RunOptions) else RunOptions()
        result = run_suite(
            _suite_cls,
            options=opts_instance,
            run_options=run_options,
            output_dir=get_context().output_dir,
        )
        # The leaf-invoke renderer (cli/invoke.render_leaf_value) derives the
        # process exit code from this result's ssh-like retcode, so the suite
        # runner never touches typer's exit machinery. `msg=""` and `value=""`
        # keep the renderer SILENT — pytest already printed everything a user
        # needs, and a message here would append a red line to every failing
        # run. The retcode is pytest's own rc (1-5), passed through verbatim.
        return CommandResult(
            Status.Success if result.exit_code == 0 else Status.Failed,
            value="",
            command=f"pytest {_suite_cls.__name__}",
            retcode=result.exit_code,
        )

    runner.__signature__ = inspect.Signature(params)  # ty: ignore[unresolved-attribute]
    # Opt this leaf out of the CLI's `--dry-run` seam default: the branch above
    # IS `otto test -n`'s preview, and it can only run if the seam lets the
    # body start. Stamped on the leaf rather than on the `test` COMMAND SPEC so
    # the suite-less selection path (`otto test --tests ...`, whose group
    # callback runs the preamble itself) keeps the safe default and stops at
    # the seam instead of running the selection for real.
    runner.__cli_dry_run_preview__ = True  # ty: ignore[unresolved-attribute]
    runner.__name__ = suite_class.__name__
    runner.__doc__ = suite_class.__doc__ or f"Run the {suite_class.__name__} test suite."

    sub_app = typer.Typer()
    sub_app.command(suite_class.__name__)(runner)

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
        SuiteEntry(name=suite_class.__name__, sub_app=sub_app, file=suite_file, cls=suite_class),
        origin=suite_class.__module__,
        overwrite=same_file,
    )
