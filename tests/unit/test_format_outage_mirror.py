"""Parity pin for the Python mirror of ``formatOutage`` (issue #161).

A test-side mirror of app logic has no compiler tying it to the original
(the "cross-language reference" trap), so both sides are pinned to ONE
shared case table: ``tests/_fixtures/format_outage_cases.json``, whose
expected strings were computed by the real JS semantics. The TS side is
asserted by ``web/src/data/time.test.ts``'s fixture-parity describe; this is
the Python side. If either implementation drifts, exactly one of the two
suites fails, naming the divergent side.

The table deliberately includes the shapes that bit us: whole hours (JS
prints ``2h``, a naive float-print gives ``2.0h``), the ``toFixed(1)``
trailing zero (``10.0h`` IS correct there), ``Math.round`` half-up
boundaries (24.5s -> 25s; Python's banker's round gives 24), the
``toFixed(1)`` tie cases (x.25 hours -- 75min is ``1.3h`` in JS, ``1.2h``
under Python's ``:.1f``; caught by the fable review, see js_to_fixed_1),
the sub-minute ``60s`` quirk at 59_999ms, and issue #161's
absurd-magnitude value.
"""

import json
from pathlib import Path

import pytest

from tests._fixtures._time_mirror import format_outage

_CASES = json.loads(
    (Path(__file__).resolve().parents[1] / "_fixtures" / "format_outage_cases.json").read_text()
)


@pytest.mark.parametrize(("ms", "text"), [(c["ms"], c["text"]) for c in _CASES])
def test_mirror_matches_the_shared_case_table(ms: float, text: str) -> None:
    assert format_outage(ms) == text
