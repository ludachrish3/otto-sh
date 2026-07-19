"""Unit-safe wrappers for Playwright's ``page.clock`` absolute-time calls.

Issue #161: playwright-python's ``parse_time`` treats a bare number as epoch
*seconds* and multiplies by 1000 for the wire, while everything a test reads
from the page (``Date.now()``) is epoch *milliseconds* -- so passing a ms
value straight to ``pause_at``/``install``/``set_fixed_time`` silently jumps
the virtual clock x1000 (epoch-microsecond scale, year ~58,000). Worse, the
app and the test both compute from that same virtual clock, so every
downstream assertion agrees on the nonsense and stays green.

These helpers make the units unambiguous at the signature (``datetime`` in,
or a parameter explicitly named ``*_ms``) and reality-check the resulting
clock against an independent reference. Test files must not call the
absolute-time ``page.clock`` methods directly -- a drift guard
(tests/unit/test_e2e_clock_hygiene.py) enforces that. The relative-tick
methods (``fast_forward``, ``run_for``) take ms natively and are safe to
call directly.
"""

from datetime import datetime, timezone

from playwright.sync_api import Page

# Orders of magnitude above legitimate drift between the virtual clock and
# its reference, orders below any ms/s/us confusion (x1000 = ~56 years off).
WALL_CLOCK_TOLERANCE_MS = 86_400_000


def install_clock(page: Page, at: datetime) -> None:
    """Install the virtual clock at *at*. ``datetime``-only on purpose: a
    numeric overload is exactly how the units mistake gets typed. Tz-aware
    only: playwright-python converts via ``.timestamp()``, which reads a
    NAIVE datetime as local time -- an hours-scale silent shift that would
    duck under pause_clock_at_ms's day tolerance (fable review)."""
    assert at.tzinfo is not None, "install_clock needs a tz-aware datetime"
    page.clock.install(time=at)


def pause_clock_at_ms(page: Page, epoch_ms: float, *, near: datetime | None = None) -> None:
    """Pause the virtual clock at *epoch_ms* (milliseconds, the unit
    ``Date.now()`` reads return), then assert the paused clock actually
    landed within a day of *near* (default: the host's wall clock -- pass an
    explicit anchor for scenarios deliberately staged in the past). The
    check runs against a reference the virtual clock cannot influence; no
    assertion downstream of a shared mocked clock can catch a scale error.
    """
    page.clock.pause_at(datetime.fromtimestamp(epoch_ms / 1000, tz=timezone.utc))
    assert near is None or near.tzinfo is not None, "near needs a tz-aware datetime"
    reference = near if near is not None else datetime.now(tz=timezone.utc)
    virtual_now_ms = page.evaluate("() => Date.now()")
    assert abs(virtual_now_ms - reference.timestamp() * 1000) < WALL_CLOCK_TOLERANCE_MS, (
        f"virtual clock at {virtual_now_ms} is not within a day of "
        f"{reference.isoformat()} -- a page.clock call mixed up ms vs seconds "
        "(issue #161)"
    )
