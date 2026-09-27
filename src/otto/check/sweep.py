"""The age rule both checks' leftover sweeps follow, and the lines they say.

Every ``otto link check`` and ``otto tunnel check`` run starts by sweeping the
hosts it is about to use for ``otto-check`` leftovers: a namespace, a listener
or an echo, and the throwaway tunnel an echo names. Those leftovers carry no
record of which run made them, so a sweep cannot tell a killed run's leftover
from the live artifact of a check running at the same time on the same host.
It tells them apart by age instead: a leftover older than
:data:`~otto.check.sweep.SWEEP_MIN_AGE_S` is older than any run could still be using, so it is
removed; a younger one may belong to a running check, so it is left and the
run says so with :func:`left_line`. Both checks judge a run's leftovers
together, by the oldest of them (:func:`run_age`). A leftover whose age cannot
be read is removed, because a sweep that silently stops cleaning up is the
worse failure.

Each check derives its own worst-case run length from its own timeouts, in
``otto.tunnel._tunnel_plan.worst_case_run_s`` and
``otto.link.check.worst_case_run_s``, with the two charges defined here:
:func:`probe_worst_s` and :func:`run_end_worst_s`. A unit test keeps the
bound above both.
"""

from .fingerprint import CHECK_HOST_TIMEOUT

COMMAND_ALLOWANCE_S = 2.0
"""What a worst-case run length charges each host command that has no timer of its own.

A launch, a kill, a ``ps`` scan or a ``tc`` change returns in about 0.1 s on
the unix bed. Two seconds is twenty times that, for a loaded host. A command
that takes much longer than this, on every call, is outside the derived worst
case (see :data:`~otto.check.sweep.SWEEP_MIN_AGE_S`)."""

SWEEP_MIN_AGE_S = 2700
"""How old a check leftover must be before a sweep removes it, in seconds (45 minutes).

It must exceed the longest either check can run, derived from each check's own
timeouts. Every host command a check sends is cut off after
``otto.check.CHECK_HOST_TIMEOUT`` (30 s): a host still silent then counts
as down, and the run ends once its teardown has run. So each probe that waits
on the network (round trips, a bulk transfer, a ping, a timed connect) is
charged its own timers, capped at that cut-off (:func:`probe_worst_s`); every
other command is charged :data:`COMMAND_ALLOWANCE_S`; every sleep and readiness
deadline is charged in full; and the run ends on a host that goes quiet
(:func:`run_end_worst_s`).

``otto tunnel check`` (``otto.tunnel._tunnel_plan.worst_case_run_s``), both
protocols, one leftover tunnel in the sweep, as before the columns + the TCP
column + the UDP column + the run end:

- 3 hops, the unix bed's path: 45.6 + 383.4 + 373.4 + 150 = 952 s, about 16 min;
- 5 hosts that each run an echo (4 hops and a full-proof ``--dest``):
  72 + 512.2 + 502.2 + 210 = 1296 s, about 22 min.

``otto link check`` (``otto.link.check.worst_case_run_s``), every feature, two
placement hosts, ``--live`` in both directions: 2 x 354 (each host's sandbox
pass) + 32 (``--live``'s state reads and listener sweep) + 2 x 418.5 (each
direction's live cycle) + 120 (the run end) = 1697 s, about 28 min.

45 minutes clears the longest of these by half again. A longer path, or a host
slow on every command, can still run past it: a concurrent sweep may then take
that run's leftovers, and ``otto tunnel check`` says so in its teardown row
rather than blaming the tunnel. What the bound costs is how long a killed
run's leftovers stay: idle echoes and listeners, a throwaway tunnel on a
scratch port, and a sandbox namespace on its own addresses. Later runs step
around all of them.
"""


_SECONDS_UNTIL_S = 120
"""Ages below this are said in seconds; from it on, in whole minutes."""


def probe_worst_s(timers_s: float) -> float:
    """Charge one probe command whose own timers can run *timers_s* seconds.

    The command's own round trip (:data:`COMMAND_ALLOWANCE_S`) is added, and the
    total is capped at ``otto.check.CHECK_HOST_TIMEOUT``: a probe still
    running then ends the run as host-unreachable, which :func:`run_end_worst_s`
    charges instead.
    """
    return min(timers_s + COMMAND_ALLOWANCE_S, CHECK_HOST_TIMEOUT)


def run_end_worst_s(teardown_waits: int) -> float:
    """Charge a run that ends because a host stopped answering.

    The command that found the host silent waits the full
    ``otto.check.CHECK_HOST_TIMEOUT``, and the teardown that follows waits
    it again *teardown_waits* times, once per command it sends that host.
    """
    return (teardown_waits + 1) * CHECK_HOST_TIMEOUT


def run_age(ages: list[int | None]) -> int | None:
    """One run's age, from the ages of the leftovers it left: its oldest one's.

    Both checks judge a run whole, never leftover by leftover: a killed run's
    last echo or listener may have started only seconds before it died, and
    it goes with the rest of that run. ``None`` when ``ps`` could not give the
    age of any one of them, which :func:`sweepable` sweeps.

    >>> from otto.check.sweep import run_age
    >>> run_age([40, 3000, 5]), run_age([40, None])
    (3000, None)
    """
    return None if None in ages else max(a for a in ages if a is not None)


def sweepable(age_s: int | None) -> bool:
    """Whether a leftover *age_s* seconds old may be swept: older than the bound, or of unknown age.

    >>> from otto.check.sweep import SWEEP_MIN_AGE_S, sweepable
    >>> sweepable(SWEEP_MIN_AGE_S + 1), sweepable(40), sweepable(None)
    (True, False, True)
    """
    return age_s is None or age_s > SWEEP_MIN_AGE_S


def age_text(age_s: int) -> str:
    """Say *age_s* the way the sweep lines do: seconds under two minutes, else whole minutes.

    >>> from otto.check.sweep import age_text
    >>> age_text(40), age_text(125), age_text(7200)
    ('40 s', '2 min', '120 min')
    """
    return f"{age_s} s" if age_s < _SECONDS_UNTIL_S else f"{age_s // 60} min"


def swept_note(age_s: int | None, *, verb: str = "started") -> str:
    """Say why a swept leftover was taken, and how old it was, for the parenthesis after it.

    >>> from otto.check.sweep import swept_note
    >>> swept_note(900)
    'earlier or concurrent run, started 15 min ago'
    >>> swept_note(None, verb="created")
    'earlier or concurrent run, age unknown'
    """
    age = "age unknown" if age_s is None else f"{verb} {age_text(age_s)} ago"
    return f"earlier or concurrent run, {age}"


def left_line(what: str, host: str, age_s: int, *, verb: str = "started") -> str:
    """Say that a leftover too young to sweep was left: it may belong to a running check.

    >>> from otto.check.sweep import left_line
    >>> left_line("otto-check echo fwd-echo", "test2", 40)
    'left otto-check echo fwd-echo on test2 (started 40 s ago — may be a running check)'
    """
    return f"left {what} on {host} ({verb} {age_text(age_s)} ago — may be a running check)"
