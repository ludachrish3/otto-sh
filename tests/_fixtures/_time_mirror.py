"""Python mirror of ``web/src/data/time.ts``'s ``formatOutage``.

Lets a browser spec that drives the virtual clock deterministically compute
the EXACT banner text a fully-known outage gap should produce, instead of
polling for a real-time value. A mirror has no compiler tying it to the TS
original, so it is pinned by a shared case table instead:
``format_outage_cases.json`` (this directory) holds JS-computed expected
strings, asserted against the real ``formatOutage`` by
``web/src/data/time.test.ts`` and against this mirror by
``tests/unit/test_format_outage_mirror.py``. Change any of the three and the
parity tests say which side drifted.

Issue #161's flake component lived here: whole hours were printed via
``f"{hours}h"`` with ``hours`` a float (``495192148.0h``) where JS prints the
int form, and Python's banker's ``round()`` diverges from ``Math.round``
(half toward +inf) on .5 boundaries.
"""

import math
from decimal import ROUND_HALF_UP, Decimal


def js_round(x: float) -> int:
    """``Math.round`` -- round half toward +inf, not Python's half-to-even
    (24.5s must format as JS's 25s, not 24s)."""
    return math.floor(x + 0.5)


def js_to_fixed_1(x: float) -> str:
    """``Number.toFixed(1)`` -- on a tie between two 1-decimal candidates the
    spec picks the LARGER; Python's ``:.1f`` rounds half-to-even. They
    diverge on exactly-representable x.25 ties: JS ``(1.25).toFixed(1)`` is
    ``"1.3"``, ``f"{1.25:.1f}"`` is ``"1.2"`` (fable review 2026-07-19 --
    a 75-minute outage formatted differently on the two sides). ``Decimal``
    from a float takes the EXACT binary value toFixed also sees, and
    durations are non-negative, so ROUND_HALF_UP == "pick the larger"."""
    return str(Decimal(x).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))


def format_outage(ms: float) -> str:
    if ms < 60_000:
        return f"{js_round(ms / 1000)}s"
    mins = js_round(ms / 60_000)
    if mins < 60:
        return f"{mins}m"
    hours = mins / 60
    # int-print whole hours (JS `${2}h` -> "2h"); toFixed(1) semantics
    # otherwise, which DOES keep a trailing zero when rounding lands on one
    # ("10.0h"). Both shapes and the .25-tie cases are pinned in the table.
    return f"{int(hours)}h" if hours == int(hours) else f"{js_to_fixed_1(hours)}h"
