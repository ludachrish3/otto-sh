"""Reference third-party CLI built directly on the reservation library (sample).

:mod:`otto.reservations` has no dependency on Typer, rich, or otto's own CLI
machinery — this small app is the starting point for wiring the same plumbing
into your own tool. Copy it, or run it as-is (see
``docs/guide/reservations.md`` → "Using the reservation library in your own
CLI" for the walkthrough):

1. **Build** a backend from settings with :func:`~otto.reservations.build_backend`.
   An unconfigured ``backend`` setting (the default here) resolves to
   :class:`~otto.reservations.NullReservationBackend` — the check becomes a
   silent no-op, so this module needs no real scheduler to run or test.
2. **Resolve** the effective identity with :func:`~otto.reservations.resolve_username`.
3. **Construct** a :class:`~otto.reservations.ReservationGate` and call
   :meth:`~otto.reservations.ReservationGate.evaluate`.
4. **Present** the outcome. ``evaluate()`` returns plain text with no markup
   baked in — otto's own CLI wraps it in rich markup
   (:func:`~otto.cli.invoke.present_reservation_gate`); this example uses a
   bare ``typer.echo`` to make the point that presentation is entirely the
   caller's choice.

:func:`run_check` is steps 3-4, kept separate from the Typer command so it is
directly testable against a :class:`~otto.reservations.NullReservationBackend`
or the :class:`~otto.examples.reservations.ExampleReservationBackend` sample —
no CLI invocation and no real scheduler involved:

>>> from otto.config.lab import Lab
>>> from otto.examples.reservations import ExampleReservationBackend
>>> from otto.reservations import resolve_username
>>> from otto.examples.reservations_cli import run_check
>>> demo = Lab(name="demo", resources={"lab-a"})
>>> run_check(demo, backend=ExampleReservationBackend(), identity=resolve_username("alice"))
alice: OK
0
>>> run_check(demo, backend=ExampleReservationBackend(), identity=resolve_username("carol"))
carol: User 'carol' does not hold all resources required by lab 'demo'. Missing:
  - lab-a (held by alice)
1

Run the full CLI (steps 1-2 included) directly — with no ``--backend`` it
resolves the Null fallback, so this needs no scheduler either::

    python -m otto.examples.reservations_cli --resource rack1
"""

from pathlib import Path
from typing import Annotated

import typer

from otto.config.lab import Lab
from otto.context import OttoContext, reset_context, set_context
from otto.reservations import (
    MissingReservationError,
    ReservationBackend,
    ReservationBackendError,
    ReservationGate,
    ResolvedIdentity,
    build_backend,
    resolve_username,
)

app = typer.Typer(add_completion=False, help="Third-party reservation-gate demo.")


def run_check(lab: Lab, *, backend: ReservationBackend, identity: ResolvedIdentity) -> int:
    """Evaluate the gate for *lab* and print the outcome; return a process exit code.

    0 on success (covered, or a silent no-op); 1 if *identity* is missing a
    required resource; 2 if the backend itself could not answer the query.
    """
    token = set_context(OttoContext(lab=lab))
    try:
        outcome = ReservationGate(backend=backend, identity=identity).evaluate()
    except MissingReservationError as e:
        typer.echo(f"{identity.username}: {e}")
        return 1
    except ReservationBackendError as e:
        typer.echo(f"{identity.username}: reservation backend unavailable: {e}")
        return 2
    finally:
        reset_context(token)
    status = outcome.warning or "OK"
    typer.echo(f"{identity.username}: {status}")
    return 0


@app.command()
def main(
    resource: Annotated[
        list[str] | None,
        typer.Option("--resource", help="Resource id this run needs (repeatable)."),
    ] = None,
    backend_name: Annotated[
        str,
        typer.Option("--backend", help="Registered backend name; 'none' needs no scheduler."),
    ] = "none",
    as_user: Annotated[
        str | None,
        typer.Option("--as-user", help="Query as this user instead of $USER."),
    ] = None,
) -> None:
    """Build a backend + identity from CLI flags, then delegate to `run_check`."""
    try:
        backend = build_backend({"backend": backend_name}, repo_dir=Path.cwd())
    except ReservationBackendError as e:
        typer.echo(f"reservation backend unavailable: {e}")
        raise typer.Exit(2) from e
    identity = resolve_username(as_user)
    lab = Lab(name="example", resources=set(resource or []))
    raise typer.Exit(run_check(lab, backend=backend, identity=identity))


if __name__ == "__main__":
    app()
