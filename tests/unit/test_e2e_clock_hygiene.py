"""Drift guard: no raw ``page.clock`` absolute-time calls in test files.

Issue #161: playwright-python's ``parse_time`` treats numeric time as epoch
*seconds* (x1000 to the wire) while everything read from the page is epoch
*milliseconds* -- one raw ``pause_at(Date.now() + ...)`` jumped the virtual
clock to year ~58,000 and every downstream assertion agreed with the app on
the nonsense, because both read the same mocked clock. The unit-safe
wrappers in ``tests/_fixtures/_clock.py`` (datetime-only ``install_clock``,
ms-explicit ``pause_clock_at_ms`` with a built-in reality check) are the
only sanctioned way to set absolute virtual time.

Scans test SOURCE rather than imports: the mistake is a call-site idiom, so
the guard has to live at the call-site level. The relative-tick methods
(``fast_forward``, ``run_for``, ``resume``, ``uninstall``) take ms or no
time at all and stay allowed. Known blind spots (accepted for a drift
guard): an aliased handle (``c = page.clock; c.pause_at(...)``) or a
multiline call spelling dodges the regex -- this catches the idiom people
actually type, it is not airtight static analysis.
"""

import re
from pathlib import Path

_TESTS = Path(__file__).resolve().parents[1]  # tests/
_ALLOWED = _TESTS / "_fixtures" / "_clock.py"

# The absolute-time methods -- the ones whose numeric argument is
# seconds-vs-ms ambiguous. fast_forward/run_for (relative ticks, ms) are fine.
_ABSOLUTE_TIME_CALL = re.compile(r"\.clock\.(install|pause_at|set_fixed_time|set_system_time)\s*\(")


def test_no_raw_absolute_clock_calls_outside_the_wrapper() -> None:
    offenders: list[str] = []
    for path in sorted(_TESTS.rglob("*.py")):
        if path == _ALLOWED:
            continue
        for lineno, line in enumerate(path.read_text().splitlines(), start=1):
            if _ABSOLUTE_TIME_CALL.search(line):
                offenders.append(f"{path.relative_to(_TESTS.parent)}:{lineno}: {line.strip()}")
    assert not offenders, (
        "raw page.clock absolute-time call(s) found -- use tests/_fixtures/_clock.py's "
        "install_clock/pause_clock_at_ms instead (numeric time is epoch-SECONDS to "
        "playwright-python; issue #161):\n" + "\n".join(offenders)
    )
