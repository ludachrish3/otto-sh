"""User text in a CLI status line prints literally: no markup, no ``:emoji:`` codes (#843).

Every line here interpolates a host id, a product, a library message or a
label the user wrote. Rich would delete ``[bold]x[/bold]`` as a style tag and
turn ``:smile:`` into an emoji, so each line carries its user text as a
:class:`rich.text.Text` span and otto's own colour as that ``Text``'s style.
The new interfaces are imported inside each test, so collection never fails.
"""

import ast
import asyncio
import contextlib
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
import typer

from tests._fixtures.literal_text import assert_one_verbatim_line
from tests._fixtures.paths import CHECKOUT_ROOT

HOSTILE_ID = "[bold]x[/bold]:smile:"
"""A tag Rich would delete and an emoji code Rich would substitute."""

EMOJI = "\U0001f604"
"""What Rich renders ``:smile:`` as."""

CLI_DIR = CHECKOUT_ROOT / "src" / "otto" / "cli"


def _swallowing_exit(render):
    """Run *render*, letting a renderer's own ``typer.Exit`` end it quietly."""
    with contextlib.suppress(typer.Exit):
        render()


# ── print_error ─────────────────────────────────────────────────────────────


def test_print_error_prints_hostile_text_verbatim(monkeypatch, capsys):
    from otto.cli.invoke import print_error

    monkeypatch.setenv("COLUMNS", "400")
    print_error(f"host {HOSTILE_ID} failed")
    out = capsys.readouterr().out
    assert_one_verbatim_line(out, f"host {HOSTILE_ID} failed")
    assert EMOJI not in out


def test_print_error_keeps_the_spans_of_an_incoming_text(monkeypatch):
    import rich
    from rich.console import Console
    from rich.text import Text

    from otto.cli.invoke import print_error

    console = Console(record=True, force_terminal=True, color_system="standard", width=80)
    monkeypatch.setattr(rich, "_console", console)
    print_error(Text.assemble(("bold part", "bold"), " [x]"))
    assert console.export_text(clear=False).splitlines() == ["bold part [x]"]
    styled = console.export_text(styles=True)
    # Red is the base; the caller's bold span survives on top of it.
    assert "\x1b[1;31mbold part" in styled
    assert "\x1b[31m [x]" in styled


# ── the status lines ────────────────────────────────────────────────────────


def _cov_clean(status_name=None):
    def render():
        from otto.cli.cov import _render_clean_report
        from otto.coverage.reports import CleanReport
        from otto.result import Result
        from otto.utils import Status

        products = {} if status_name is None else {"p": Result(Status[status_name])}
        _render_clean_report(CleanReport(hosts={HOSTILE_ID: products}))

    return render


def _build_report(entry_factory):
    def render():
        from otto.cli import docker as docker_cli
        from otto.docker.reports import BuildReport

        docker_cli._render_build_report(BuildReport(projects=[entry_factory()]))

    return render


def _built_entry():
    from otto.docker.reports import ImageBuild, ProjectBuild
    from otto.result import CommandResult
    from otto.utils import Status

    ok = CommandResult(Status.Success, value="built", retcode=0)
    return ProjectBuild(
        HOSTILE_ID, HOSTILE_ID, "built", {"api": ImageBuild("api", ["api:1"], "sha1", ok)}
    )


def _no_images_entry():
    from otto.docker.reports import ProjectBuild

    return ProjectBuild(HOSTILE_ID, HOSTILE_ID, "no_images")


def _host_report():
    from otto.cli import docker as docker_cli
    from otto.docker.reports import HostReport

    docker_cli._render_host_report(HostReport(hosts={HOSTILE_ID: []}), done="removed")


def _stack(*, registered):
    from otto.docker.deployment import UseCaseStack
    from otto.docker.resolve import Selection

    container = MagicMock()
    container.id = HOSTILE_ID
    container.container_id = "abcdef123456789"
    by_host = {HOSTILE_ID: {"api": container}} if registered else {}
    return UseCaseStack(
        use_case=HOSTILE_ID,
        selection=Selection(use_case=HOSTILE_ID, fragments=[]),
        projects={HOSTILE_ID: "c"},
        by_host=by_host,
    )


def _stack_report(*, registered):
    def render():
        from otto.cli import docker as docker_cli

        docker_cli._print_stack_report(_stack(registered=registered))

    return render


def _displacement():
    from otto.docker.resolve import Displacement

    return Displacement(HOSTILE_ID, HOSTILE_ID, 0, "real", 1)


def _displacements():
    from otto.cli import docker as docker_cli

    docker_cli._print_displacements([_displacement()])


def _not_run_error():
    from otto.result import CommandNotRunError

    return CommandNotRunError(HOSTILE_ID, HOSTILE_ID)


def _docker_dry_run_decline():
    from otto.cli import docker as docker_cli

    async def declined():
        raise _not_run_error()

    with patch("otto.host.host.is_dry_run", return_value=True):
        asyncio.run(docker_cli._run_docker(declined()))


def _doctor():
    from otto.cli.init import _print_doctor
    from otto.init.doctor import DoctorReport

    report = DoctorReport(
        verdicts=[], warnings=[HOSTILE_ID], inventory_label=HOSTILE_ID, creds_label=HOSTILE_ID
    )
    _print_doctor(report, Path("/r"), scaffolded=set())


def _probe(results_factory):
    def render():
        from otto.cli.probe import print_probe_report

        print_probe_report(results_factory())

    return render


def _probe_unreachable():
    from otto.cli.probe import UNREACHABLE, ProbeResult

    return [ProbeResult(HOSTILE_ID, UNREACHABLE, detail=HOSTILE_ID)]


def _reservation_cli_gate():
    from otto.cli import reservation
    from otto.reservations import ReservationBackendError

    opts = SimpleNamespace(holder=None, skip_reservation_check=False)
    with (
        patch("otto.cli.invoke.maybe_root_options", return_value=opts),
        patch("otto.declared.get_projects", return_value=[]),
        patch.object(
            reservation,
            "build_reservation_gate",
            side_effect=ReservationBackendError(HOSTILE_ID),
        ),
    ):
        reservation._reservation_gate(SimpleNamespace(meta={}))  # ty: ignore[invalid-argument-type]


def _whoami():
    from otto.cli import reservation

    report = SimpleNamespace(username=HOSTILE_ID, source="--holder", backend_name="local")
    gate = SimpleNamespace(identity=object(), identity_report=lambda: report)
    opts = SimpleNamespace(labs=[HOSTILE_ID])
    with patch("otto.cli.invoke.maybe_root_options", return_value=opts):
        reservation.whoami(SimpleNamespace(meta={"otto_reservation": gate}))  # ty: ignore[invalid-argument-type]


def _lab_context(kind):
    def render():
        from otto.cli.invoke import report_lab_context_error
        from otto.session import LabBuildError

        report_lab_context_error(LabBuildError("x", field=None, kind=kind, detail=HOSTILE_ID))

    return render


def _lab_context_reservation():
    from otto.cli.invoke import report_lab_context_error
    from otto.reservations import ReservationBackendError

    report_lab_context_error(ReservationBackendError(HOSTILE_ID))


def _reservation_warning():
    from otto.cli.invoke import present_reservation_gate

    gate = SimpleNamespace(evaluate=lambda: SimpleNamespace(warning=HOSTILE_ID))
    ctx = SimpleNamespace(meta={"otto_reservation": gate})
    present_reservation_gate(ctx)  # ty: ignore[invalid-argument-type]


def _dry_run_decline(value):
    def render():
        from otto.cli.invoke import _render_dry_run_decline

        _render_dry_run_decline(value)

    return render


def _skipped_result():
    from otto.cli.invoke import _render_ok_result
    from otto.result import Result
    from otto.utils import Status

    _render_ok_result(Result(Status.Skipped, msg=HOSTILE_ID), None)


def _cases():
    """One param per converted function: its render, and the exact line it must print.

    Every otto import stays inside the renders themselves, so collection never
    imports what a test exercises.
    """

    return [
        pytest.param(_cov_clean(), f"{HOSTILE_ID}: no instrumented products", id="cov-no-products"),
        pytest.param(_cov_clean("NotRun"), f"{HOSTILE_ID}/p: not run (dry run)", id="cov-not-run"),
        pytest.param(
            _cov_clean("Success"),
            f"{HOSTILE_ID}/p: counters cleared",
            id="cov-cleared",
        ),
        pytest.param(
            _build_report(_built_entry),
            f"{HOSTILE_ID}/api: built api:1  sha1  ({HOSTILE_ID})",
            id="docker-built",
        ),
        pytest.param(
            _build_report(_no_images_entry),
            f"docker: {HOSTILE_ID} declares no [[docker.images]] — "
            f"nothing to build on {HOSTILE_ID}",
            id="docker-no-images",
        ),
        pytest.param(_host_report, f"{HOSTILE_ID}: removed", id="docker-host-report"),
        pytest.param(
            _stack_report(registered=True),
            f"{HOSTILE_ID} on {HOSTILE_ID} (c): 1 container(s) registered:",
            id="docker-stack-headline",
        ),
        pytest.param(
            _stack_report(registered=True),
            f"  - {HOSTILE_ID}  →  abcdef123456",
            id="docker-stack-host",
        ),
        pytest.param(
            _stack_report(registered=False),
            f"docker: {HOSTILE_ID} registered no container on any host",
            id="docker-stack-empty",
        ),
        pytest.param(
            _displacements,
            None,  # the library's sentence, computed at run time
            id="docker-displacement",
        ),
        pytest.param(_docker_dry_run_decline, None, id="docker-dry-run-decline"),
        pytest.param(_doctor, f"inventory: {HOSTILE_ID}", id="init-inventory"),
        pytest.param(_doctor, f"creds:     {HOSTILE_ID}", id="init-creds"),
        pytest.param(_doctor, f"  • {HOSTILE_ID}", id="init-warning"),
        pytest.param(
            _probe(_probe_unreachable),
            f"  {HOSTILE_ID}: unreachable -- {HOSTILE_ID}",
            id="probe-line",
        ),
        pytest.param(
            _reservation_cli_gate,
            f"Reservation backend unavailable: {HOSTILE_ID}",
            id="reservation-gate",
        ),
        pytest.param(_whoami, f"username: {HOSTILE_ID}", id="whoami-username"),
        pytest.param(_whoami, f"lab:      {HOSTILE_ID}", id="whoami-lab"),
        pytest.param(
            _lab_context("sources"), f"Host source unavailable: {HOSTILE_ID}", id="lab-sources"
        ),
        pytest.param(
            _lab_context("inventory"), f"Inventory unavailable: {HOSTILE_ID}", id="lab-inventory"
        ),
        pytest.param(
            _lab_context_reservation,
            f"Reservation backend unavailable: {HOSTILE_ID}",
            id="lab-reservation",
        ),
        pytest.param(_reservation_warning, HOSTILE_ID, id="reservation-warning"),
        pytest.param(
            _dry_run_decline(SimpleNamespace(msg=HOSTILE_ID)), HOSTILE_ID, id="dry-run-msg"
        ),
        pytest.param(
            _dry_run_decline(SimpleNamespace(msg="", command=HOSTILE_ID)),
            f"dry run: this command was not run ({HOSTILE_ID})",
            id="dry-run-command",
        ),
        pytest.param(_skipped_result, f"skipped: {HOSTILE_ID}", id="skipped-result"),
    ]


@pytest.mark.parametrize(("render", "expected"), _cases())
def test_status_line_prints_hostile_text_verbatim(monkeypatch, capsys, render, expected):
    monkeypatch.setenv("COLUMNS", "400")
    if expected is None:
        expected = _late_expected(render)
    _swallowing_exit(render)
    out = capsys.readouterr().out
    assert_one_verbatim_line(out, expected)
    assert EMOJI not in out


def _late_expected(render) -> str:
    """The line a render whose words are the library's own must print."""
    if render is _displacements:
        return f"docker: {_displacement().describe()}"
    return str(_not_run_error())


def test_the_reservation_hint_follows_the_backend_line(monkeypatch, capsys):
    monkeypatch.setenv("COLUMNS", "400")
    _swallowing_exit(_lab_context_reservation)
    assert_one_verbatim_line(
        capsys.readouterr().out,
        "Pass --skip-reservation-check / -R to proceed without the check.",
    )


def test_the_probe_announcements_are_literal(monkeypatch, capsys):
    from otto.cli.probe import PROBE_HEADLINE, PROBE_NO_HOSTS

    monkeypatch.setenv("COLUMNS", "400")
    _probe(list)()
    _probe(_probe_unreachable)()
    out = capsys.readouterr().out
    assert_one_verbatim_line(out, PROBE_NO_HOSTS)
    assert_one_verbatim_line(out, PROBE_HEADLINE)


# ── the class guard ─────────────────────────────────────────────────────────

_ESCAPES = {"escape", "escape_markup"}
_PRINTERS = {"rprint", "print", "print_error", "fail"}


def _callee(node: ast.Call) -> str:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


def _escapes_inside(node: ast.AST) -> bool:
    return any(isinstance(sub, ast.Call) and _callee(sub) in _ESCAPES for sub in ast.walk(node))


def test_no_status_line_escapes_into_a_markup_string():
    """No CLI line prints ``escape()``-d text inside a markup string.

    ``escape`` guards the markup but not the ``:emoji:`` codes, and a site that
    forgets it swallows the tag outright. A line carries user text as a
    :class:`~rich.text.Text` span instead; ``print_error``/``fail`` already
    print their message as one, so an ``escape`` there doubles the backslash.
    """
    offenders = []
    for path in sorted(CLI_DIR.glob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and _callee(node) in _PRINTERS and node.args):
                continue
            if _escapes_inside(node.args[0]):
                offenders.append(f"{path.name}:{node.lineno}")
    assert offenders == [], f"escape() inside a printed line: {offenders}"


# ── the markup-f-string guard (#844) ────────────────────────────────────────

_MARKUP_TAG = re.compile(r"\[/?[A-Za-z#]")

_MARKUP_FSTRING_ALLOWLIST = {
    ("invoke.py", "f'[green]{success}[/green]'"): (
        "`success` is the command author's own success message, not lab or user data"
    ),
    ("invoke.py", "f'[green]{policy.none_message}[/green]'"): (
        "the command author's declared none-message, not lab or user data"
    ),
    ("test.py", "f'[magenta]{DRY_RUN_TESTS_HEADLINE}[/magenta]'"): (
        "otto's own headline constant, shared with the docs"
    ),
}
"""A printed markup f-string that is knowingly legitimate: ``(file, unparsed f-string)`` to why."""


def _markup_fstring_offenders(source: str, filename: str) -> list[str]:
    """Printer calls in *source* with an f-string argument that holds a tag and a value.

    The shape that let user text slip into markup (``rprint(f"[red]{x}[/red]")``,
    and the two-step ``msg = escape(x); rprint(f"[tag]{msg}")``): any argument
    of a printer call, not just the first, that is an f-string with a literal
    ``[tag`` and an interpolation. ``print_literal`` is not a printer here: it
    never parses markup.
    """
    offenders = []
    for node in ast.walk(ast.parse(source, filename=filename)):
        if not (isinstance(node, ast.Call) and _callee(node) in _PRINTERS):
            continue
        for arg in node.args:
            if not isinstance(arg, ast.JoinedStr):
                continue
            if not any(isinstance(part, ast.FormattedValue) for part in arg.values):
                continue
            literal = "".join(
                part.value
                for part in arg.values
                if isinstance(part, ast.Constant) and isinstance(part.value, str)
            )
            if not _MARKUP_TAG.search(literal):
                continue
            if (filename, ast.unparse(arg)) in _MARKUP_FSTRING_ALLOWLIST:
                continue
            offenders.append(f"{filename}:{node.lineno} {ast.unparse(arg)}")
    return offenders


def test_no_status_line_interpolates_into_a_markup_fstring():
    """No CLI line interpolates a value into a printed ``[tag]`` f-string.

    A value there is parsed as markup unless something escaped it, and
    escaping still leaves its ``:emoji:`` codes. Carry the value as a
    :class:`~rich.text.Text` span (``Text.assemble``) instead. A site that is
    genuinely author text goes on :data:`_MARKUP_FSTRING_ALLOWLIST` with its reason.
    """
    offenders = []
    for path in sorted(CLI_DIR.glob("*.py")):
        offenders += _markup_fstring_offenders(path.read_text(), path.name)
    assert offenders == [], f"interpolation into a markup f-string: {offenders}"


@pytest.mark.parametrize(
    "source",
    [
        pytest.param('rprint(f"[red]{who}[/red]")', id="first-arg"),
        pytest.param('console.print("x", f"[bold]{who}")', id="later-arg"),
        pytest.param('print_error(f"[yellow]{who}")', id="print-error"),
        pytest.param('msg = escape(who)\nrprint(f"[yellow]{msg}")', id="two-step"),
        pytest.param('fail(f"{who} [/dim]")', id="closing-tag"),
    ],
)
def test_the_markup_fstring_guard_flags_each_shape(source):
    assert _markup_fstring_offenders(source, "synthetic.py") != []


@pytest.mark.parametrize(
    "source",
    [
        pytest.param('rprint(Text.assemble(("ok", "green"), f" {who}"))', id="text-span"),
        pytest.param('rprint(f"{who} done")', id="no-tag"),
        pytest.param('rprint("[green]done[/green]")', id="no-interpolation"),
        pytest.param('print_literal(f"[red]{who}")', id="print-literal"),
        pytest.param('log(f"[red]{who}")', id="not-a-printer"),
    ],
)
def test_the_markup_fstring_guard_passes_clean_sources(source):
    assert _markup_fstring_offenders(source, "synthetic.py") == []
