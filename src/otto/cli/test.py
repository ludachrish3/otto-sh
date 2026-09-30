"""Run tests by name or by marker expression.

``otto test`` is one command::

    otto test [NAMES...] [-m EXPR] [run flags] [test-verb options]
    otto test --list-tests [NAMES...] [-m EXPR]
    otto test --list-markers

**Names.** Each name is resolved against every configured repo's pytest
collection. ``test_reboot`` selects every test of that name, in any class,
module or repo (every parametrization of it); ``TestDevice`` selects every
test in every class of that name; ``TestDevice::test_reboot`` selects that
test in every such class. ``-m EXPR`` narrows the named tests, or, with no
names, selects on its own. A run needs at least one name or ``-m``. An
unknown name is a usage error with did-you-mean suggestions.

Options and names mix freely: ``otto test TestDevice --iterations 5`` and
``otto test --iterations 5 TestDevice`` are the same command.

**Test-verb options.** Every options class registered for the ``test`` verb
(``register_options(Cls, verbs=["test"])``) adds its fields as flags here.
Tests read the parsed values with ``ctx.options(Cls)``. A field whose name
matches one of the run flags below is refused when the command is built.

**Markers**

``integration``
    Requires live Vagrant VMs.  Skip with ``-m "not integration"``.

``timeout(seconds)``
    Fail the test if it runs longer than *seconds*.

``retry(n)``
    Retry a failing test up to *n* times before reporting failure.

**Listing**

``--list-tests``    List what the names and ``-m`` select, as a tree per repo:
                    module, then class, then test. Needs no lab.

``--list-markers``  List the markers available to ``-m`` and exit.

**Run flags**

``--markers / -m EXPRESSION``
    pytest ``-m`` marker expression applied after collection.

``--random / --no-random``
    Shuffle test order (the default), or run in collection order: file, then
    source order within the file.

``--seed N``
    Fix the random-order seed (the value a previous run logged).

``--iterations / -i N``
    Repeat each test N times within a single setup/teardown cycle (0 = disabled).

``--duration / -d SECONDS``
    Repeat tests for N seconds within a single setup/teardown cycle (0 = disabled).

``--threshold FLOAT``
    Minimum per-test pass rate percentage required in stability mode (0-100,
    default: 100).

``--results PATH``
    Write test results (JUnit XML) to PATH (default: auto-written to the log directory).

When both ``--iterations`` and ``--duration`` are specified, testing stops when
either limit is reached first.

``--cov``
    Fetch ``.gcda`` files from remote hosts after the run finishes and
    place them in a ``cov/`` directory in the run's output directory.

``--cov-dir PATH``
    Write coverage data to ``PATH`` instead of the default
    ``<output_dir>/cov``.  Implies ``--cov``.  The directory is created
    if missing; if it already exists and is non-empty, the command
    aborts unless ``--overwrite-cov-dir`` is also given.

``--overwrite-cov-dir``
    Clear the contents of the ``--cov-dir`` destination before the run
    so stale data from a previous invocation cannot be mixed with the
    new results.

``--cov-clean / --no-cov-clean``
    Delete ``.gcda`` files on remote hosts before the test run.
    Enabled by default; use ``--no-cov-clean`` to keep stale data.

``--cov-report / -r``
    After coverage collection, render an HTML report.  Implies ``--cov``.
    Default location: ``<output_dir>/cov_report``.

``--cov-report-dir PATH``
    Write the HTML report to ``PATH`` instead of the default.  Implies
    ``--cov-report`` (and therefore ``--cov``).  Empty/overwrite rules
    match ``--cov-dir``: created if missing, aborts if non-empty unless
    ``--overwrite-cov-report-dir`` is also given.

``--overwrite-cov-report-dir``
    Clear the contents of ``--cov-report-dir`` before the report is rendered.

``--project-name STR``
    Title shown in the HTML report header (only used with ``--cov-report``).

``--cov-tickets-json PATH``
    Also write a machine-readable per-ticket coverage summary (``format: 1``,
    versioned independently of the internal ``store.json``) to this path.
    Implies ``--cov-report`` (and therefore ``--cov``). ``[coverage.tickets]``
    must be configured at all, checked immediately — an unconfigured table
    aborts before the test run starts rather than after a long run finishes.
    A *configured* ``[coverage.tickets]`` whose git walk simply matched no
    commits is a different, genuinely unknowable-in-advance case: that still
    logs a warning and skips the export after the run rather than failing it
    (matching this command's other post-run coverage steps).

``--monitor``
    Enable host performance monitoring for the duration of the run.  Samples
    every host (or those matched by ``--monitor-hosts``) on a fixed interval
    and emits per-test start/end events automatically.  At the end of the run
    a format:1 monitor export (one session) is written to
    ``<output_dir>/monitor.json``.

``--monitor-interval SECONDS``
    Sampling interval for ``--monitor`` (default: 5).

``--monitor-output PATH``
    Override the destination for the captured monitor data.  Format inferred
    from the suffix: ``.json`` (default) writes a self-contained format:1
    export, ``.db`` writes a SQLite database loadable via ``otto monitor
    <path>``.

``--monitor-hosts REGEX``
    Restrict ``--monitor`` to host IDs this regex FULLY matches
    (``re.fullmatch``): ``sensor`` does not select ``sensor-1`` — write
    ``sensor.*``.  A regex matching none of the hosts the run may walk stops
    the run before any test executes; hosts that matched but cannot be sampled
    only disable collection, with a warning naming them.

**Dry run.** ``otto -n test NAMES`` builds and validates the test-verb options
exactly as a run would, then prints them with the tests the run would run:
the run's own pytest collection, ``--collect-only``. It imports the test
files it collects and runs no test, so parametrizations are expanded and
``-m`` is evaluated.

**Examples**::

    otto test --list-markers
    otto test --list-tests
    otto test --list-tests -m slow
    otto test --list-tests TestMyDevice
    otto test test_login
    otto test TestB::test_login test_plain
    otto test -m slow
    otto test TestMyDevice --firmware 2.1
    otto test --iterations 50 --threshold 95 TestMyDevice
    otto test --duration 300 --threshold 90 TestMyDevice
    otto test --cov TestMyDevice
    otto test --cov-dir /tmp/myrun --overwrite-cov-dir TestMyDevice
    otto test --cov --cov-report TestMyDevice
"""

import inspect
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, Optional, cast, get_args, get_origin

import typer
from rich import print as rprint
from typer.core import TyperCommand
from typing_extensions import override

from ..models import MIN_INTERVAL_SECONDS
from .completers import completion_source

if TYPE_CHECKING:
    from rich.panel import Panel
    from rich.tree import Tree

    from ..config.repo import Repo
    from ..suite.plugin import SelectedTest
    from ..suite.run import Listing


TEST_HELP = "Run tests by name or marker expression."
"""The one-line help ``otto --help`` and ``otto test --help`` both show."""


# ---------------------------------------------------------------------------
# Completion sources
# ---------------------------------------------------------------------------


@completion_source(kind="tests")
def _names_completer(ctx: typer.Context, incomplete: str) -> list[str]:  # noqa: ARG001 — required by Typer autocompletion callback signature
    """Completion source for ``NAMES``: the names pytest collected, from each repo's table.

    Every test base name, every class around a test and each
    ``Class::test`` pair (:func:`~otto.config.repo.selectable_names`), as the
    last pytest collection of each file recorded them
    (:func:`otto.config.collected_tests.completion_view`). The first TAB with
    no table waits once for a whole-tree collection in a bounded child
    process; every later TAB answers at once with what each file held when
    last collected, and a child started behind the answer, once the check
    window has lapsed, brings the tables up to date. ``NAMES`` is variadic,
    so each word completes one name, by plain prefix. Parametrizations
    collapse to their base (``test_x`` runs every ``test_x[...]``).

    Reads the table itself rather than the ``names`` section every other
    completer reads: test names are not in it, so ``otto ho<TAB>`` never
    validates the test tree.
    """
    from ..config import get_repos
    from ..config.collected_tests import completion_view

    names = completion_view(get_repos()).names
    return [name for name in names if name.startswith(incomplete)]


@completion_source(kind="markers")
def _markers_completer(ctx: typer.Context, incomplete: str) -> list[str]:  # noqa: ARG001 — required by Typer autocompletion callback signature
    """Completion source for ``-m``/``--markers``: the markers pytest knows, and otto's own.

    Every marker pytest registered when it last collected the whole tree
    (declared in a pytest config file, by a conftest or by a plugin), every
    marker a recorded test applies, and :data:`otto.suite.markers.OTTO_MARKERS`;
    read, seeded and refreshed exactly as ``NAMES`` is. The expression rule
    (:func:`otto.utils.complete_marker_expression`) completes the identifier
    being typed and keeps the rest of the expression.
    """
    from ..config import get_repos
    from ..config.collected_tests import completion_view
    from ..suite.markers import OTTO_MARKERS
    from ..utils import complete_marker_expression

    markers = {*completion_view(get_repos()).markers, *OTTO_MARKERS}
    return complete_marker_expression(sorted(markers), incomplete)


# ---------------------------------------------------------------------------
# Listing
# ---------------------------------------------------------------------------


def _render_panels(panels: "list[Panel]") -> None:
    from rich.table import Table

    table = Table(show_header=False, show_footer=False, box=None, expand=True, padding=(0, 1, 1, 1))
    for _ in panels:
        table.add_column(ratio=1)
    table.add_row(*panels)
    rprint(table)


def _builtin_markers_panel() -> "Panel":
    """Render the markers otto itself provides, from the one table that registers them."""
    from rich.panel import Panel
    from rich.text import Text

    from ..suite.markers import OTTO_MARKERS

    content = Text("\n".join(f"• {line}" for line in OTTO_MARKERS.values()))
    return Panel(
        content,
        title=Text("otto (built in)", style="bold not dim"),
        border_style="dim",
        padding=(1, 5, 1, 1),
    )


def list_markers_callback(value: bool) -> None:
    """Print the markers available to --markers (one panel per repo, plus otto's) and exit.

    A repo's markers are the ones pytest knew when it last collected the
    repo's tests: each one a pytest config file, a plugin or a conftest
    registered, and each one a test applies. A cold table is collected first
    (``otto.suite.run._refresh_tables``); a warm one is read as it is, with
    no collection.
    """
    if not value:
        return
    from ..config import get_repos
    from ..suite.markers import OTTO_MARKERS
    from ..suite.run import _refresh_tables

    panels = []
    code = 0
    repos = get_repos()
    for repo, table in zip(repos, _refresh_tables(repos, cold_only=True), strict=True):
        markers: list[str] = []
        if table is None:
            code = 1  # the collection that could not finish logged why
        else:
            # otto's own are pytest-registered too; its panel shows them once.
            markers = [m for m in table.markers if m not in OTTO_MARKERS]
        panels.append(repo.get_markers_panel(markers))
    panels.append(_builtin_markers_panel())
    _render_panels(panels)
    raise typer.Exit(code)


def _module_label(path: Path, roots: "list[Path]") -> str:
    """Name a test module by its path under the test directory that holds it.

    A module directly in a test directory is its file name; one in a
    subdirectory keeps that directory (``router/test_basic.py``), so two
    modules with one basename stay apart. A module under no test directory
    falls back to its file name.
    """
    for root in roots:
        if path.is_relative_to(root):
            return path.relative_to(root).as_posix()
    return path.name


def tests_tree(repo: "Repo", tests: "list[SelectedTest]") -> "Tree":
    """Build one repo's tree: module file, then class (at every depth), then test.

    *tests* are what a pytest session selected, shown in the order given.
    Labels are plain text, never markup, so a parametrization id such as
    ``test_x[a]`` prints as written.
    """
    from rich.text import Text
    from rich.tree import Tree

    tree = Tree(Text(f"{repo.name} {repo.version}", style="bold"))
    if not tests:
        tree.add(Text("(no tests found)", style="dim"))
        return tree
    roots = [Path(root) for root in repo.tests]
    nodes: dict[tuple[str, ...], Tree] = {}
    for test in tests:
        path = str(test.path)
        if (path,) not in nodes:
            nodes[(path,)] = tree.add(Text(_module_label(Path(path), roots)))
        parent = nodes[(path,)]
        classes: list[str] = list(test.classes)
        for depth, cls in enumerate(classes, start=1):
            key = (path, *classes[:depth])
            if key not in nodes:
                nodes[key] = parent.add(Text(cls))
            parent = nodes[key]
        parent.add(Text(test.name))
    return tree


def _selected_tests(names: "list[str]", markers: str) -> "Listing":
    """Collect what *names* and *markers* select, as a run would, running nothing.

    :func:`otto.suite.run.selected_tests`. A name no repo holds ends it as
    it ends a run: a usage error with did-you-mean suggestions, or, with no
    collected test to suggest from, "No tests matched the selection." (exit 1).
    """
    from ..suite.run import NoTestsMatchedError, selected_tests
    from ..suite.selection import UnknownSelectionError

    try:
        return selected_tests(names, markers=markers)
    except UnknownSelectionError as e:
        raise typer.BadParameter(str(e), param_hint="NAMES") from None
    except NoTestsMatchedError:
        rprint("[red]No tests matched the selection.[/red]")
        raise typer.Exit(code=1) from None


def _print_trees(listing: "Listing") -> None:
    """Print *listing* as one tree per repo, a repo with no selected test included."""
    from ..config import get_repos

    for repo in get_repos():
        rprint(tests_tree(repo, listing.tests.get(repo.name, [])))


DRY_RUN_TESTS_HEADLINE = "dry run: pytest collected these tests; nothing ran"
"""Heads the test tree ``otto -n test`` prints after the options block."""


# ---------------------------------------------------------------------------
# The command
# ---------------------------------------------------------------------------


def _run_flags(  # noqa: PLR0913 — one parameter per run flag, by design
    *,
    list_markers: Annotated[
        bool,
        typer.Option(
            "--list-markers",
            callback=list_markers_callback,
            is_eager=True,
            help="List the markers available to --markers and exit.",
        ),
    ] = False,
    list_tests: Annotated[
        bool,
        typer.Option(
            "--list-tests",
            help="List what the names and --markers select, by repo, module and class, and exit.",
        ),
    ] = False,
    markers: Annotated[
        str,
        typer.Option(
            "--markers",
            "-m",
            metavar="EXPRESSION",
            autocompletion=_markers_completer,
            help=(
                "pytest -m marker expression applied after collection. Narrows the "
                "named tests, or selects on its own when no name is given."
            ),
        ),
    ] = "",
    random_order: Annotated[
        bool,
        typer.Option(
            "--random/--no-random",
            help=(
                "Shuffle test order (pytest-randomly). On by default; the seed is "
                "logged at session start. --no-random runs tests in source order."
            ),
        ),
    ] = True,
    seed: Annotated[
        int | None,
        typer.Option(
            "--seed",
            metavar="N",
            help="Fix the random-order seed (the value a previous run logged). Implies --random.",
        ),
    ] = None,
    iterations: Annotated[
        int,
        typer.Option(
            "--iterations",
            "-i",
            help="Repeat each test N times within a single setup/teardown cycle (0 = disabled).",
        ),
    ] = 0,
    duration: Annotated[
        int,
        typer.Option(
            "--duration",
            "-d",
            help="Repeat tests for N seconds within a single setup/teardown cycle (0 = disabled).",
        ),
    ] = 0,
    threshold: Annotated[
        float,
        typer.Option(
            "--threshold",
            help="Minimum per-test pass rate percentage required in stability mode (0-100).",
        ),
    ] = 100.0,
    results: Annotated[
        str,
        typer.Option(
            "--results",
            metavar="PATH",
            help="Write test results (JUnit XML) to PATH (default: auto in log dir).",
        ),
    ] = "",
    cov: Annotated[
        Optional[bool],  # noqa: UP045 — typer hard-asserts on `X | None` option annotations
        typer.Option(
            "--cov/--no-cov",
            help=(
                "Collect coverage from the lab's instrumented products after the run "
                "finishes. Default: auto — on when an instrumented product is detected "
                "and [coverage] is configured. --cov forces it on (an error if nothing is "
                "instrumented); --no-cov forces it off."
            ),
        ),
    ] = None,
    cov_dir: Annotated[
        Path | None,
        typer.Option(
            "--cov-dir",
            help=(
                "Directory to write coverage data to. Implies --cov. "
                "Default when --cov is used alone: <output_dir>/cov."
            ),
            file_okay=False,
            dir_okay=True,
            resolve_path=True,
        ),
    ] = None,
    overwrite_cov_dir: Annotated[
        bool,
        typer.Option(
            "--overwrite-cov-dir",
            help=(
                "Allow --cov-dir to target an existing non-empty directory "
                "(its contents will be cleared before the run)."
            ),
        ),
    ] = False,
    cov_clean: Annotated[
        bool,
        typer.Option(
            help="Delete .gcda files on remote hosts before the test run.",
        ),
    ] = True,
    cov_report: Annotated[
        bool,
        typer.Option(
            "--cov-report",
            "-r",
            help=(
                "Generate an HTML coverage report after the run finishes. "
                "Implies --cov. Default location: <output_dir>/cov_report."
            ),
        ),
    ] = False,
    cov_report_dir: Annotated[
        Path | None,
        typer.Option(
            "--cov-report-dir",
            help=(
                "Directory to write the HTML coverage report to. "
                "Implies --cov-report (and --cov). "
                "Default when --cov-report is used alone: <output_dir>/cov_report."
            ),
            file_okay=False,
            dir_okay=True,
            resolve_path=True,
        ),
    ] = None,
    overwrite_cov_report_dir: Annotated[
        bool,
        typer.Option(
            "--overwrite-cov-report-dir",
            help=(
                "Allow --cov-report-dir to target an existing non-empty directory "
                "(its contents will be cleared before the report is rendered)."
            ),
        ),
    ] = False,
    project_name: Annotated[
        str,
        typer.Option(
            "--project-name",
            help="Title shown in the HTML report header (only used with --cov-report).",
        ),
    ] = "Coverage Report",
    cov_tickets_json: Annotated[
        Path | None,
        typer.Option(
            "--cov-tickets-json",
            help=(
                "Also write a machine-readable per-ticket coverage summary to this "
                "path. Implies --cov-report. Requires [coverage.tickets] to be "
                "configured (checked before the test run starts)."
            ),
        ),
    ] = None,
    monitor: Annotated[
        bool,
        typer.Option(
            help="Collect host performance metrics for the entire test run.",
        ),
    ] = False,
    monitor_interval: Annotated[
        float,
        typer.Option(
            "--monitor-interval",
            metavar="SECONDS",
            help="Sampling interval for --monitor.",
            min=MIN_INTERVAL_SECONDS,
        ),
    ] = 5.0,
    monitor_output: Annotated[
        Path | None,
        typer.Option(
            "--monitor-output",
            metavar="PATH",
            help=(
                "Override the destination for monitor data. Format inferred from "
                "suffix (.json or .db). Implies --monitor. "
                "Default: <output_dir>/monitor.json."
            ),
        ),
    ] = None,
    monitor_hosts: Annotated[
        str | None,
        typer.Option(
            "--monitor-hosts",
            metavar="REGEX",
            help=(
                "Regex matched against whole host IDs (fullmatch) to restrict --monitor: "
                "'sensor' does not select 'sensor-1' — write 'sensor.*'. Implies --monitor."
            ),
        ),
    ] = None,
) -> None:
    """Declare ``otto test``'s run flags; never called.

    Its signature is the command's run-flag parameters (see
    :func:`_build_test_app`), so every flag is declared once, with its help,
    in one place.
    """


def _flag_name(param: inspect.Parameter) -> str:
    """Return the flag a run-flag parameter is typed as, for a collision message.

    Read off the ``typer.Option`` declaration (``--random/--no-random``), so
    a verb field named ``random_order`` is told which flag it collides with
    rather than a ``--random-order`` that does not exist.
    """
    for meta in getattr(param.annotation, "__metadata__", ()):
        # Inside `Annotated`, typer.Option's first declaration lands in
        # `default`; the rest are `param_decls`.
        first = getattr(meta, "default", None)
        decls = [first, *(getattr(meta, "param_decls", None) or [])]
        longs = [d for d in decls if isinstance(d, str) and d.startswith("--")]
        if longs:
            return longs[0]
    return f"--{param.name.replace('_', '-')}"


def _check_selection(ctx: Any, params: "dict[str, Any]") -> None:
    """Refuse a contradiction in the parsed arguments alone: no lab, no side effect.

    Run at parse time (``_OttoTestCommand``), before the invoke preamble
    loads a lab or creates a run directory, so a usage error costs neither.
    The names-or-``-m`` requirement is an argument rule; every rule between
    the run flags is the library's (``RunOptions``), constructed here once
    and stored on the context for the leaf.
    """
    from typer._click.exceptions import UsageError

    from ..params import OptionsValidationError
    from ..suite.run import RUN_OPTIONS_KEY, RunOptions
    from .invoke import usage_error_from

    if not params.get("names") and not params.get("markers"):
        raise UsageError("otto test needs at least one test name or -m EXPR (see otto test --help)")
    fields = {name: params[name] for name in _RUN_FIELD_NAMES if name in params}
    for name in _PATH_FIELD_NAMES:
        # ctx.params still holds click's raw string for a Path flag: Typer's
        # own convertor (string -> pathlib.Path) runs only when it calls the
        # leaf, but RunOptions is built here, at parse time, on every run —
        # not just when a usage error fires. _run reads the instance off
        # ctx.meta rather than the leaf's own (converted) kwargs, so this is
        # the only place that conversion happens; without it, RunOptions
        # would carry a plain str for every real invocation with a
        # destination flag.
        if fields.get(name) is not None:
            fields[name] = Path(fields[name])
    try:
        ctx.meta[RUN_OPTIONS_KEY] = RunOptions(**fields)
    except OptionsValidationError as e:
        raise usage_error_from(e, flags=RUN_FLAGS) from e


class _OttoTestCommand(TyperCommand):
    """The ``otto test`` command: ``--list-tests`` and usage checks run at parse time.

    ``--list-tests`` prints and exits here, before the invoke preamble, so it
    loads no lab and creates no run directory, like ``--help``.
    """

    @override
    def parse_args(self, ctx: Any, args: list[str]) -> list[str]:
        from typer._click.exceptions import UsageError

        rest = super().parse_args(ctx, args)
        if ctx.resilient_parsing:
            return rest
        params = ctx.params
        try:
            if params.get("list_tests"):
                names = list(params.get("names") or [])
                listing = _selected_tests(names, params.get("markers") or "")
                _print_trees(listing)
                ctx.exit(listing.exit_code)
            _check_selection(ctx, params)
        except UsageError as e:
            # Raised with no context, click would print the error panel alone;
            # with this command's, it adds the Usage line and the Try-help hint,
            # exactly as for its own missing-argument error.
            if e.ctx is None:
                e.ctx = ctx
            raise
        return rest


def _run(ctx: typer.Context, names: "list[str]", verb_kwargs: "dict[str, Any]") -> None:
    """Run (or dry-run) ``otto test``: bind the verb's options, preflight or run.

    The run options were constructed and checked at parse time
    (:func:`_check_selection`); every rule between them is the library's.
    ``CoverageNotInstrumentedError`` is deliberately NOT caught here: its
    per-product verdict table is rendered once, on the top-level boundary in
    :mod:`otto.cli.main` (:func:`~otto.cli.invoke.render_instrumentation_refusal`).
    """
    if ctx.resilient_parsing:
        return

    from ..context import get_context
    from ..coverage.config import DestinationError
    from ..params import OptionsValidationError, verb_option_classes
    from ..suite.run import RUN_OPTIONS_KEY
    from .invoke import dry_run_requested, usage_error_from

    run_options = ctx.meta[RUN_OPTIONS_KEY]
    otto_ctx = get_context()
    try:
        otto_ctx.bind_verb_options("test", verb_kwargs)
        instances = [otto_ctx.options(origin.cls) for origin in verb_option_classes("test")]
    except OptionsValidationError as e:
        raise usage_error_from(e) from e

    if dry_run_requested(ctx):
        from ..suite.run import prepare_run
        from .invoke import print_preview_dry_run

        try:
            prepare_run(run_options, dry_run=True)
        except (OptionsValidationError, DestinationError) as e:
            raise usage_error_from(e, flags=RUN_FLAGS) from e
        print_preview_dry_run(ctx, instances)
        listing = _selected_tests(names, run_options.markers)
        if not listing.exit_code and not any(listing.tests.values()):
            rprint("[red]No tests matched the selection.[/red]")
            raise typer.Exit(code=1)
        rprint(f"[magenta]{DRY_RUN_TESTS_HEADLINE}[/magenta]")
        _print_trees(listing)
        if listing.exit_code:
            raise typer.Exit(code=listing.exit_code)
        return

    from ..suite.run import NoTestsMatchedError, run_tests
    from ..suite.selection import UnknownSelectionError

    try:
        result = run_tests(
            names, run_options=run_options, options=instances, output_dir=otto_ctx.output_dir
        )
    except (OptionsValidationError, DestinationError) as e:
        raise usage_error_from(e, flags=RUN_FLAGS) from e
    except UnknownSelectionError as e:
        raise typer.BadParameter(str(e), param_hint="NAMES") from None
    except NoTestsMatchedError:
        rprint("[red]No tests matched the selection.[/red]")
        raise typer.Exit(code=1) from None

    if result.exit_code != 0:
        raise typer.Exit(code=result.exit_code)


def _run_params() -> "list[inspect.Parameter]":
    """Return the run flags as keyword-only parameters, in declaration order."""
    return [
        p.replace(kind=inspect.Parameter.KEYWORD_ONLY)
        for p in inspect.signature(_run_flags).parameters.values()
    ]


def _run_flag_map() -> "dict[str, str]":
    """Field name to flag for every run flag, plus the value spellings library messages use."""
    flags = {p.name: _flag_name(p) for p in _run_params()}
    flags.update({"cov=False": "--no-cov", "random_order=False": "--no-random"})
    flags["cov"] = "--cov"
    flags["random_order"] = "--random"
    return flags


RUN_FLAGS: "dict[str, str]" = _run_flag_map()
"""Field name to flag, built from the run-flag signature so a rename cannot drift."""

# The run flags that are RunOptions fields (the two --list-* flags are not).
_RUN_FIELD_NAMES = frozenset(
    p.name for p in _run_params() if p.name not in {"list_markers", "list_tests"}
)


def _is_path_annotation(annotation: object) -> bool:
    """Report whether *annotation* is a ``Path`` (or subclass), plain, unioned or ``Annotated``.

    Unwraps one layer of ``Annotated`` (a run flag's is always
    ``Annotated[X, typer.Option(...)]``) to reach ``X``, then checks ``X``
    itself and, when ``X`` is a union (``Path | None``), each of its members
    — so a bare ``Annotated[Path, ...]`` (no ``__args__`` on ``Path`` itself)
    and a ``Path`` subclass both match, not just the ``Path | None`` shape
    every current run flag happens to use.
    """
    if hasattr(annotation, "__metadata__"):
        annotation = getattr(annotation, "__origin__", annotation)  # Annotated[X, ...] -> X
    candidates = get_args(annotation) if get_origin(annotation) is not None else (annotation,)
    return any(isinstance(t, type) and issubclass(t, Path) for t in candidates)


# The run flags typed as an optional Path (Typer's own leaf wrapper converts
# these from the string ``ctx.params`` holds to ``pathlib.Path`` before
# calling the leaf, via its `determine_type_convertor`; ``_check_selection``
# runs at parse time, before that wrapper ever sees them, so it applies the
# same conversion itself — see the loop in `_check_selection`).
_PATH_FIELD_NAMES = frozenset(p.name for p in _run_params() if _is_path_annotation(p.annotation))


def test_verb_params() -> "list[inspect.Parameter]":
    """Return every ``test``-verb option as a parameter, checked against the run flags.

    A verb field whose name is a run flag's, or ``names``, raises
    ``OptionsCollisionError``. The completion cache serialises exactly this
    list, so the fast path can offer the verb's flags without the registry.
    """
    from ..params import merge_option_params, verb_option_classes

    reserved = {p.name: f"otto test's {_flag_name(p)}" for p in _run_params()} | {
        "names": "otto test's NAMES"
    }
    return merge_option_params(verb_option_classes("test"), what="otto test", reserved=reserved)


def build_test_app(cached_options: "list[dict[str, Any]] | None" = None) -> typer.Typer:
    """Build ``otto test`` for the completion fast path, from the cache's verb options.

    *cached_options* is the ``names`` section's ``test_options``: the verb's
    flags as :func:`test_verb_params` serialised them. The registry is not
    populated on the fast path, so without them ``otto test --<TAB>`` would
    offer the run flags alone. ``None`` builds from the registry, as
    ``test_app`` does.
    """
    if cached_options is None:
        return _build_test_app()
    from ..config.completion_stubs import stub_params

    return _build_test_app(verb_params=stub_params(cached_options))


def _build_test_app(verb_params: "list[inspect.Parameter] | None" = None) -> typer.Typer:
    """Build ``otto test``: NAMES, the run flags, and every ``test``-verb option as a flag.

    Built on each access of ``test_app`` (the module ``__getattr__``), so
    the verb's flags are whatever is registered when the command is resolved.
    A verb field whose name is a run flag's, or ``names``, raises
    ``OptionsCollisionError`` here, before any test runs. *verb_params*
    replaces the registry's (the completion fast path's cached stubs).
    """
    from typer._click.globals import get_current_context

    from ..params import sensitive_field_names, verb_option_classes
    from .invoke import DRY_RUN_PREVIEW_ATTR, DRY_RUN_SELF_FINISHING_ATTR, SENSITIVE_FIELDS_ATTR

    run_params = _run_params()
    sensitive: set[str] = set()
    if verb_params is None:
        verb_params = test_verb_params()
        for origin in verb_option_classes("test"):
            sensitive |= sensitive_field_names(origin.cls)  # ty: ignore[invalid-argument-type]
    verb_names = [p.name for p in verb_params]

    names_param = inspect.Parameter(
        "names",
        inspect.Parameter.POSITIONAL_OR_KEYWORD,
        default=None,
        annotation=Annotated[
            Optional[list[str]],  # noqa: UP045 — Typer asserts on X | None
            typer.Argument(
                metavar="NAMES...",
                help="Tests to run: test_x, TestClass, or TestClass::test_x. Any number.",
                autocompletion=_names_completer,
                show_default=False,
            ),
        ],
    )

    def test(**kw: Any) -> None:
        # Typer calls the leaf with its parsed, converted values; the click
        # context is read, not injected, so the signature is exactly the flags.
        # The cast: typer's own commands hand out the vendored click base
        # class under the `typer.Context` alias (see `otto.cli.invoke`).
        ctx = cast("typer.Context", get_current_context())
        names = list(kw.pop("names", None) or [])
        verb_kwargs = {name: kw.pop(name) for name in verb_names}
        _run(ctx, names, verb_kwargs)

    params = [names_param, *run_params, *verb_params]
    # Both, not just the signature: the completion cache serialises a command
    # from `inspect.signature`, while typer reads the ANNOTATIONS to find each
    # parameter's `typer.Option` metadata.
    test.__signature__ = inspect.Signature(params)  # ty: ignore[unresolved-attribute]
    test.__annotations__ = {p.name: p.annotation for p in params}
    # Previews its own body under --dry-run (the collected test tree) and
    # finishes its own dry run after building the verb's options, so the
    # seam in the preamble leaves it alone.
    setattr(test, DRY_RUN_PREVIEW_ATTR, True)
    setattr(test, DRY_RUN_SELF_FINISHING_ATTR, True)
    setattr(test, SENSITIVE_FIELDS_ATTR, frozenset(sensitive))

    app = typer.Typer(
        add_completion=False, context_settings={"help_option_names": ["-h", "--help"]}
    )
    app.command(
        "test",
        cls=_OttoTestCommand,
        help=TEST_HELP,
        context_settings={"help_option_names": ["-h", "--help"]},
    )(test)
    return app


def __getattr__(name: str) -> Any:
    """Build ``test_app`` on every access, so it carries the verb's current flags."""
    if name == "test_app":
        return _build_test_app()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
