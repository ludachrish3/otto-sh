"""Reference third-party CLI built on the reservation library (sample).

It follows the same rules as otto's own CLI, and copying it copies them:

1. **Parse** the flags. ``--holder`` and ``-R`` / ``--skip-reservation-check``
   are otto's own spellings.
2. **Construct** the gate through the library:
   :func:`~otto.reservations.gate_from_settings` resolves the identity before
   building the backend for it, and under ``-R`` builds no backend at all.
3. **Call** one library entry point:
   :meth:`~otto.reservations.ReservationGate.evaluate` for ``gate``, or
   :meth:`~otto.reservations.ReservationGate.report` for ``check``. An explicit
   ``Lab`` needs no otto context.
4. **Render** the result. Presentation is the caller's choice; this one prints
   plain lines.
5. **Translate** errors in one place (:func:`translate`), with otto's exit
   codes: 1 for a missing resource and 1 for an unavailable backend, whose
   message names ``-R``.

Nothing here decides a reservation question itself.

The helpers take a gate, so a test can hand them one around an in-memory
backend. The commands build theirs from settings:

>>> from otto.lab import Lab
>>> from otto.examples.reservations import ExampleReservationBackend
>>> from otto.examples.reservations_cli import check_report, translate
>>> from otto.reservations import ReservationGate, resolve_username
>>> demo = Lab(name="demo", resources={"lab-a"})
>>> def gate_for(user):
...     backend = ExampleReservationBackend(username=user)
...     return ReservationGate(backend=backend, identity=resolve_username(user))
>>> translate(lambda: check_report(gate_for("alice"), demo))
lab-a  lab demo  held
OK
0
>>> translate(lambda: check_report(gate_for("carol"), demo))
lab-a  lab demo  missing
User 'carol' does not hold all resources required by lab 'demo'. Missing:
  lab-a  lab demo  (held by: alice)
1

Run the full CLI. With no ``--backend`` it uses the ``none`` backend, so it
needs no scheduler::

    python -m otto.examples.reservations_cli --resource rack1 gate
    python -m otto.examples.reservations_cli --resource rack1 check
"""

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, cast

import typer

from otto.lab import Lab
from otto.reservations import (
    MissingReservationError,
    ReservationBackendError,
    ReservationGate,
    gate_from_settings,
)

__all__ = ["check_report", "translate"]

app = typer.Typer(add_completion=False, help="Third-party reservation-gate demo.")
"""The demo's command-line application; running the module as a script runs it."""


@dataclass(frozen=True)
class _Options:
    resources: list[str]
    backend: str
    holder: str | None
    skip: bool


def translate(action: Callable[[], int]) -> int:
    """Run *action*, turning the library's refusals into otto's exit codes in one place."""
    try:
        return action()
    except MissingReservationError as e:
        typer.echo(str(e))
        return 1
    except ReservationBackendError as e:
        typer.echo(
            f"reservation backend unavailable: {e}\n"
            "Pass --skip-reservation-check / -R to proceed without the check."
        )
        return 1


def run_gate(gate: ReservationGate, lab: Lab) -> int:
    """Evaluate *gate* for *lab* and print the outcome; a refusal raises for :func:`translate`."""
    outcome = gate.evaluate(lab)
    typer.echo(outcome.warning or "OK")
    return 0


def check_report(gate: ReservationGate, lab: Lab) -> int:
    """Print *gate*'s report for *lab*, one line per requirement, then the verdict."""
    report = gate.report(lab)
    for row in report.rows:
        held = "n/a" if row.held is None else ("held" if row.held else "missing")
        typer.echo(f"{row.resource}  {row.level} {row.owner}  {held}")
    if not report.covered:
        raise MissingReservationError.from_report(report)
    typer.echo("OK")
    return 0


def _options(ctx: typer.Context) -> _Options:
    return cast("_Options", ctx.obj)  # set by main(), which runs before every command


def _gate(opts: _Options) -> ReservationGate:
    return gate_from_settings(
        {"backend": opts.backend},
        Path.cwd(),
        holder=opts.holder,
        skip_reservation_check=opts.skip,
    )


def _lab(opts: _Options) -> Lab:
    return Lab(name="example", resources=set(opts.resources))


@app.callback()
def main(
    ctx: typer.Context,
    resource: Annotated[
        list[str] | None,
        typer.Option("--resource", help="Resource id this run needs (repeatable)."),
    ] = None,
    backend_name: Annotated[
        str,
        typer.Option("--backend", help="Registered backend name; 'none' needs no scheduler."),
    ] = "none",
    holder: Annotated[
        str | None,
        typer.Option("--holder", help="Check reservations as this user instead of $USER."),
    ] = None,
    skip: Annotated[
        bool,
        typer.Option(
            "--skip-reservation-check", "-R", help="Skip the check (break-glass); warns loudly."
        ),
    ] = False,
) -> None:
    """Parse the shared flags; the commands build the gate from them."""
    ctx.obj = _Options(list(resource or []), backend_name, holder, skip)


@app.command()
def gate(ctx: typer.Context) -> None:
    """Gate a run: refuse unless every required resource is held."""
    opts = _options(ctx)
    raise typer.Exit(translate(lambda: run_gate(_gate(opts), _lab(opts))))


@app.command()
def check(ctx: typer.Context) -> None:
    """Report every required resource and whether you hold it."""
    opts = _options(ctx)
    raise typer.Exit(translate(lambda: check_report(_gate(opts), _lab(opts))))


if __name__ == "__main__":
    app()
