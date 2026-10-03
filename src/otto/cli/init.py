"""``otto init`` — scaffold a new otto repo, or check an existing one.

The doctor and the scaffolder are the :mod:`otto.init` library; this leaf
parses the flags, asks its questions before anything is written, renders the
library's reports, and prints the Next steps panel. See docs/cli/init.md.
"""

import dataclasses
import os
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import typer

if TYPE_CHECKING:
    # Annotation-only: ``otto.cli.init`` sits on a budgeted CLI surface
    # (scripts/import_budget.py), so every real ``otto.init`` use below is a
    # function-local import.
    from rich.console import RenderableType

    from ..init import DoctorReport, InitConfig, ScaffoldReport

_FLAGS = {"root": "--path", "kmodcov_dir": "--kmodcov-dir"}
"""Field-to-flag spelling for usage_error_from. No "areas": every area flag names a fixed area."""


async def init_command(
    all_areas: Annotated[
        bool, typer.Option("--all", help="Scaffold every missing area without prompting.")
    ] = False,
    schemas: Annotated[
        bool,
        typer.Option(
            "--schemas",
            help=(
                "Scaffold (or refresh, if present) the schemas area: .otto/schemas + editor wiring."
            ),
        ),
    ] = False,
    lab: Annotated[
        bool,
        typer.Option(
            "--lab", help="Scaffold the lab area (lab_data/lab.json + inventory.json + creds.json)."
        ),
    ] = False,
    tests: Annotated[
        bool,
        typer.Option(
            "--tests",
            help=(
                "Scaffold the tests area (example tests + conftest), plus the "
                "instructions area when missing: its init module declares the "
                "options the tests read."
            ),
        ),
    ] = False,
    instructions: Annotated[
        bool,
        typer.Option(
            "--instructions",
            help="Scaffold the instructions area (the init module, under the first libs dir).",
        ),
    ] = False,
    kmodcov: Annotated[
        bool,
        typer.Option(
            "--kmodcov",
            help=(
                "Scaffold (or refresh, if present) the kmodcov area: vendor the otto_kmodcov "
                "library, a commented [[dev_tools]] entry, and a consumer starter."
            ),
        ),
    ] = False,
    kmodcov_dir: Annotated[
        str,
        typer.Option("--kmodcov-dir", help="Where --kmodcov vendors the library (repo-relative)."),
    ] = "third_party/otto_kmodcov",
    name: Annotated[
        str,
        typer.Option("--name", help="Product name for settings.toml (default: directory name)."),
    ] = "",
    version: Annotated[
        str, typer.Option("--version", help="Product version for settings.toml.")
    ] = "0.1.0",
    path: Annotated[
        Path, typer.Option("--path", file_okay=False, help="Repo root to operate on.")
    ] = Path(),
) -> None:
    """Scaffold a new otto repo, or validate an existing one's setup.

    Registered as a bare-function loader (``"otto.cli.init:init_command"``);
    as a plain ``async def`` it runs under the full command lifecycle via the
    leaf-invoke wrapper's coroutine bridge (``cli/invoke._wrap_invoke``) —
    registration is the only opt-in.
    """
    from ..init import InitInputError, check_repo, scaffold, scaffold_candidates
    from .invoke import usage_error_from

    requested = [
        area
        for area, on in (
            ("schemas", schemas),
            ("lab", lab),
            ("tests", tests),
            ("instructions", instructions),
            ("kmodcov", kmodcov),
        )
        if on
    ]
    interactive = not (all_areas or requested)
    try:
        config = _config(path, name=name, version=version, kmodcov_dir=kmodcov_dir)
        candidates = scaffold_candidates(
            config.root, requested=requested, all_areas=all_areas or interactive
        )
        if interactive:
            answers = _ask(config, candidates, name_given=bool(name))
            config, candidates = answers.config, answers.areas
        report = scaffold(config, candidates)
        doctor = check_repo(config.root)
    except InitInputError as e:
        raise usage_error_from(e, flags=_FLAGS) from e
    _print_report(report, config.root)
    _print_doctor(doctor, config.root, scaffolded=set(report.areas))
    from rich import get_console

    console = get_console()
    steps = next_steps_panel(
        config.root, sut_dirs=os.environ.get(_sut_dirs_var(), ""), width=console.width
    )
    console.print(steps.renderable, soft_wrap=steps.soft_wrap)
    if not doctor.ok:
        raise typer.Exit(code=1)


def _config(path: Path, **kwargs: str) -> "InitConfig":
    from ..init import InitConfig

    return InitConfig.for_repo(path, **kwargs)


@dataclasses.dataclass(frozen=True)
class _Answers:
    """The interactive run's answers: the config to scaffold with, and the chosen areas."""

    config: "InitConfig"
    areas: list[str]


def _ask(config: "InitConfig", candidates: list[str], *, name_given: bool) -> _Answers:
    """Ask every question before anything is written.

    The product name and version only when settings will be scaffolded; then
    one confirmation per candidate, skipping an area a chosen one already
    pulls in (the library scaffolds it as a prerequisite).
    """
    from ..init import scaffold_prerequisites

    if "settings" in candidates:
        # str() pins what click already guarantees: with a str default and no
        # ``type=``, prompt returns str — but its declared return type is Any.
        name = (
            config.name
            if name_given
            else str(typer.prompt("Product name", default=config.root.name))
        )
        version = str(typer.prompt("Version", default=config.version))
        config = dataclasses.replace(config, name=name, version=version)
    chosen: list[str] = []
    for area in candidates:
        if area in scaffold_prerequisites(config.root, chosen):
            continue
        if typer.confirm(f"Scaffold the {area} area?", default=True):
            chosen.append(area)
    return _Answers(config, chosen)


def _shown(path: Path, root: Path) -> str:
    """*path* relative to *root* when inside it (a ``libs`` dir may not be)."""
    return str(path.relative_to(root)) if path.is_relative_to(root) else str(path)


def _print_report(report: "ScaffoldReport", root: Path) -> None:
    """Print each file the scaffolder touched, then its notices, verbatim."""
    for write in report.writes:
        typer.echo(f"{write.outcome} {_shown(write.path, root)}")
    for notice in report.notices:
        typer.echo(notice)


_STATUS = {
    "ok": "[green]✓[/green]",
    "failed": "[red]✗[/red]",
    "absent": "[yellow]not present[/yellow]",
    "blocked": "[yellow]blocked[/yellow]",
}
"""The verdict table's status cell for each doctor verdict state."""


def _print_doctor(doctor: "DoctorReport", root: Path, *, scaffolded: set[str]) -> None:
    """Render the verdict table, the inventory and creds labels, and the warnings."""
    from rich import print as rprint
    from rich.markup import escape
    from rich.table import Table

    table = Table(title=f"otto init — {root}", show_header=True)
    table.add_column("area")
    table.add_column("status")
    table.add_column("detail", overflow="fold")
    for verdict in doctor.verdicts:
        status = (
            "[green]scaffolded[/green]"
            if verdict.name in scaffolded and verdict.state == "ok"
            else _STATUS[verdict.state]
        )
        # escape(): a problem quotes pydantic (`[type=extra_forbidden, …]`) and
        # the author's own regexes — both tag-shaped, and both silently
        # swallowed by rich markup if handed over raw.
        table.add_row(verdict.name, status, escape("\n".join(verdict.problems) or verdict.detail))
    rprint(table)
    if doctor.inventory_label is not None:
        rprint(f"inventory: {escape(doctor.inventory_label)}")
    if doctor.creds_label is not None:
        rprint(f"creds:     {escape(doctor.creds_label)}")
    if doctor.warnings:
        # Advisory only — printed after the verdict table, never folded into
        # it, and never part of the exit code.
        rprint("\n[bold yellow]Warnings[/bold yellow]")
        for warning in doctor.warnings:
            rprint(f"  [yellow]•[/yellow] {escape(warning)}")


_COMPLETION_SCRIPT = "~/.bash_completions/otto.sh"
"""Where typer's bash installer (``otto --install-completion``) writes the completion script."""

_BOX_PAD = 2
"""The boxed panel's horizontal padding, each side."""

_BOX_OVERHEAD = 2 * (1 + _BOX_PAD)
"""Columns the box takes from a line: a border and the padding, each side."""

_COMMAND_INDENT = 8
"""How far the copyable commands are indented under their heading."""


def _sut_dirs_var() -> str:
    from ..config.env import SUT_DIRS_ENV_VAR

    return SUT_DIRS_ENV_VAR


def _already_active(root: Path, sut_dirs: str) -> bool:
    """Return True when *sut_dirs* (an ``OTTO_SUT_DIRS`` value) already names *root*."""
    from ..config.env import split_path_list

    return any(entry.resolve() == root for entry in split_path_list(sut_dirs))


@dataclasses.dataclass(frozen=True)
class _Line:
    """One line of the Next steps content; ``command`` marks a line a user copies."""

    text: str = ""
    style: str = ""
    indent: int = 0
    command: bool = False


@dataclasses.dataclass(frozen=True)
class NextSteps:
    """The Next steps block, ready to print: ``console.print(renderable, soft_wrap=soft_wrap)``.

    ``soft_wrap`` is True for the unboxed fallback, whose command lines must
    reach the terminal whole; the boxed panel prints with it off, so its
    prose wraps inside the box.
    """

    renderable: "RenderableType"
    soft_wrap: bool


def _next_steps_lines(root: Path, sut_dirs: str) -> list[_Line]:
    from ..init.templates import EXAMPLE_LAB_NAME

    export = [] if _already_active(root, sut_dirs) else [f"export {_sut_dirs_var()}={root}"]
    source = f"source {_COMPLETION_SCRIPT}"
    blank = _Line()

    def heading(text: str) -> _Line:
        return _Line(text, "bold")

    def sub(text: str) -> _Line:
        return _Line(text, "bold cyan", 3)

    def prose(*lines: str, style: str = "") -> list[_Line]:
        return [_Line(line, style, 6) for line in lines]

    def commands(*lines: str) -> list[_Line]:
        return [_Line(line, "green", _COMMAND_INDENT, command=True) for line in lines]

    bashrc = [
        "`otto --install-completion` already added the `source` line to",
        "~/.bashrc, so completion needs nothing more there." + (" Add only:" if export else ""),
    ]
    return [
        heading("1. Activate otto in this shell:"),
        blank,
        *commands(*export, "otto --install-completion", source),
        blank,
        heading("2. Activate otto in future shells:"),
        blank,
        sub("~/.bashrc"),
        *prose(*bashrc),
        *([blank, *commands(*export)] if export else []),
        blank,
        sub("~/.profile  (if your login shell reads it instead of ~/.bashrc)"),
        *prose("Add both lines yourself:" if export else "Add this line yourself:"),
        blank,
        *commands(*export, source),
        blank,
        *prose(
            "Never put `otto --install-completion` itself in a startup file: it",
            "rewrites ~/.bashrc every time it runs.",
            style="yellow",
        ),
        blank,
        heading("3. Try it:"),
        blank,
        *commands(
            f"otto --lab {EXAMPLE_LAB_NAME} --list-hosts",
            "otto test --list-tests",
            f"otto --lab {EXAMPLE_LAB_NAME} test TestExample",
            f"otto --lab {EXAMPLE_LAB_NAME} test test_example_function",
            f"otto --lab {EXAMPLE_LAB_NAME} run smoke",
        ),
    ]


def next_steps_panel(root: Path, *, sut_dirs: str, width: int) -> NextSteps:
    """Build the Next steps block ``otto init`` prints last, for a *width*-column console.

    typer's bash installer writes ``~/.bash_completions/otto.sh`` and appends
    ``source '<that path>'`` to ``~/.bashrc`` once, so only the current shell
    needs a manual ``source``; it also rewrites ``~/.bashrc`` every time it
    runs, so it must never go in a startup file. The ``export`` lines are
    omitted when *sut_dirs* already names *root*.

    Boxed when every command fits inside the box. Otherwise the same lines
    print unboxed under a rule, soft-wrapped: rich folds or crops a line that
    is too long for a box, and a folded ``export`` line pasted line by line
    runs without error and sets the wrong value.
    """
    from rich.console import Group
    from rich.padding import Padding
    from rich.panel import Panel
    from rich.rule import Rule
    from rich.text import Text

    lines = _next_steps_lines(root, sut_dirs)
    widest = max(len(line.text) for line in lines if line.command)
    if widest + _COMMAND_INDENT + _BOX_OVERHEAD <= width:
        body = [
            Padding(Text(line.text, style=line.style), (0, 0, 0, line.indent))
            if line.text
            else Text("")
            for line in lines
        ]
        panel = Panel(
            Group(*body),
            title="[bold]Next steps[/bold]",
            title_align="left",
            padding=(1, _BOX_PAD),
            expand=False,
        )
        return NextSteps(panel, soft_wrap=False)
    # Literal-space indents, no Padding: anything that lays a line out in a
    # fixed width crops it, soft_wrap or not.
    unboxed = [Text.assemble(" " * line.indent, (line.text, line.style)) for line in lines]
    rule = Rule("[bold]Next steps[/bold]", align="left")
    return NextSteps(Group(rule, Text(""), *unboxed), soft_wrap=True)
