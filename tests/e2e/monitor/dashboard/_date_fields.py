"""Read and type the dashboard's react-aria date fields, segment by segment.

A date field is a ``DateInput`` group of segments (``<div role="spinbutton">``,
one per ``data-type``: month/day/year/hour/minute, ``second`` at second
granularity, and ``dayPeriod`` on a 12-hour locale), not an ``<input>``, so
``.fill()`` does not apply. The fields show the browser's local wall clock,
so both helpers speak naive local ``datetime``s: a test reads a field, does
its arithmetic and types the result back, whatever timezone it runs in.
"""

from datetime import datetime

from playwright.sync_api import Locator

DATE_TIME_INPUT = ':has(> [data-type="month"]):has(> [data-type="hour"])'
"""One date-and-time field: the element whose own children are its segments
(the range picker's two, the event editor's Start and End). Matching the
segments' parent, not a role, holds for both, whatever their wrappers."""


def _segment(field: Locator, kind: str) -> Locator:
    return field.locator(f'[data-type="{kind}"]')


def read_date_field(field: Locator) -> datetime:
    """*field*'s displayed instant, as a naive local wall-clock ``datetime``."""

    def seg(kind: str) -> int:
        return int(_segment(field, kind).inner_text())

    hour = seg("hour")
    period = _segment(field, "dayPeriod")
    if period.count() > 0:
        is_pm = period.inner_text().strip().upper() == "PM"
        hour = (hour % 12) + (12 if is_pm else 0)
    second = seg("second") if _segment(field, "second").count() > 0 else 0
    return datetime(seg("year"), seg("month"), seg("day"), hour, seg("minute"), second)  # noqa: DTZ001 — naive local wall-clock by design


def set_date_field(field: Locator, dt: datetime) -> None:
    """Type the naive local wall-clock *dt* into *field*, every segment.

    Each segment is clicked before it is typed into. react-aria segments
    auto-advance focus as they are typed, but through a React state update
    between keystrokes, and ``keyboard.type()`` lands faster than that
    round-trip: later digits would land on whichever segment was focused when
    they were sent. Targeting each segment by its ``data-type`` puts every
    keystroke where it is meant to go.
    """
    keyboard = field.page.keyboard
    twelve_hour = _segment(field, "dayPeriod").count() > 0
    hour = (dt.hour % 12 or 12) if twelve_hour else dt.hour
    parts = [
        ("month", dt.month, 2),
        ("day", dt.day, 2),
        ("year", dt.year, 4),
        ("hour", hour, 2),
        ("minute", dt.minute, 2),
    ]
    if _segment(field, "second").count() > 0:
        parts.append(("second", dt.second, 2))
    for kind, value, width in parts:
        _segment(field, kind).click()
        keyboard.type(str(value).zfill(width))
    if twelve_hour:
        period = _segment(field, "dayPeriod")
        want = "PM" if dt.hour >= 12 else "AM"
        if period.inner_text().strip().upper() != want:
            period.click()
            keyboard.press("ArrowUp")
