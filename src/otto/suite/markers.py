"""otto's built-in pytest markers, in one table.

``ensure`` declares the lab state a test needs (spec 2026-08-30, §4); ``retry``
is implemented by :mod:`otto.suite._retry`. Registered with pytest by
``OttoOptionsPlugin.pytest_configure`` (so ``--strict-markers`` accepts them)
and rendered by ``otto test --list-markers`` from this same table — a marker
added here is discoverable everywhere at once.
"""

ENSURE_VERBS: dict[str, str] = {
    "installed": "ensure_installed",
    "uninstalled": "ensure_uninstalled",
    "clean": "ensure_clean",
}
"""Converge step → the ``otto.project`` function that performs it.

The functions are the same ones ``otto run <verb> --ensure`` calls, so a
marker and the command cannot diverge on what a state means.
"""

ENSURE_NONE = "none"
"""The one-step path that converges nothing — an explicit opt-out under a marked class."""

OTTO_MARKERS: dict[str, str] = {
    "ensure": (
        "ensure(*steps): converge the lab through STEPS, in order, before the test "
        "body — steps: installed, uninstalled, clean; or the single step none (touch "
        "nothing). The closest marker (test, then class, then module) replaces the "
        "whole path; an unmarked test converges nothing."
    ),
    "retry": (
        "retry(n): re-run a failed test body until it passes, n TOTAL attempts "
        "(retry(2) = one retry); the body must be idempotent"
    ),
}
"""Marker name → the ``markers`` ini line pytest registers for it."""


def _vocabulary() -> str:
    return ", ".join([*ENSURE_VERBS, ENSURE_NONE])


def ensure_path_problem(args: tuple[object, ...]) -> str | None:
    """Why *args* is not a valid ``ensure`` path, or ``None`` when it is.

    Called at collection for every marked item so a typo errors the run before
    any test executes (spec §4.2).
    """
    if not args:
        return f"the marker needs at least one step ({_vocabulary()})"
    steps = [str(a) for a in args]
    for step in steps:
        if step not in ENSURE_VERBS and step != ENSURE_NONE:
            return f"unknown step {step!r}; steps are {_vocabulary()}"
    if ENSURE_NONE in steps and len(steps) > 1:
        return (
            f"{ENSURE_NONE!r} is a complete path and cannot be combined with other steps "
            f"({_vocabulary()})"
        )
    return None


def ensure_path(args: tuple[object, ...]) -> list[str]:
    """Return the converge steps a *validated* marker asks for, in order; ``none`` → ``[]``."""
    steps = [str(a) for a in args]
    return [] if steps == [ENSURE_NONE] else steps
