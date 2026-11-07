"""The browser's format versions mirror the declared ones (dump spec §13.1).

``web/src/data/exportFormat.ts`` (the monitor export) and
``web/src/data/streamFormat.ts`` (the live stream) cannot import
``otto.models.formats``, so they spell its lists again. This holds each pair
equal: a version added to or dropped from one side fails here, in every Python
lane, before the browser ships with a reader or writer that disagrees.
"""

import ast
import re
from pathlib import Path

import pytest

from otto.models import formats
from tests._fixtures.paths import CHECKOUT_ROOT

DATA = CHECKOUT_ROOT / "web" / "src" / "data"


def _ts_list(ts: Path, name: str) -> list:
    found = re.search(
        rf"^export const {name} = (\[[^\]]*\]) as const", ts.read_text(), re.MULTILINE
    )
    assert found, f"{name} is not declared in {ts}"
    return ast.literal_eval(found.group(1))


@pytest.mark.parametrize(
    ("ts", "name"),
    [
        ("exportFormat.ts", "MONITOR_EXPORT_READ_VERSIONS"),
        ("exportFormat.ts", "MONITOR_EXPORT_WRITE_VERSIONS"),
        ("streamFormat.ts", "MONITOR_STREAM_READ_VERSIONS"),
        ("streamFormat.ts", "MONITOR_STREAM_WRITE_VERSIONS"),
    ],
)
def test_the_browser_list_mirrors_the_declared_one(ts, name):
    assert _ts_list(DATA / ts, name) == getattr(formats, name)


def test_the_browser_observed_at_header_mirrors_the_server_one():
    """``eventApi.ts`` reads the header ``otto.monitor.server`` stamps (#866).

    The browser cannot import the Python constant, so it spells the name
    again; a rename on one side alone would silently drop the observation.
    """
    from otto.monitor.server import OBSERVED_AT_HEADER

    found = re.search(
        r'^const OBSERVED_AT_HEADER = "([^"]+)";',
        (DATA / "eventApi.ts").read_text(),
        re.MULTILINE,
    )
    assert found, "OBSERVED_AT_HEADER is not declared in eventApi.ts"
    assert found.group(1) == OBSERVED_AT_HEADER


def test_the_browser_same_event_runs_the_shared_cases_file():
    """``sameevent.test.ts`` drives the SAME cases file ``same_event`` is tested on.

    One file of cases holds the server's comparison and its browser mirror to
    one rule; a test that loaded a copy would let the two drift.
    """
    source = (DATA.parent / "__tests__" / "sameevent.test.ts").read_text()
    assert "tests/_fixtures/same_event_cases.json" in source
    assert (CHECKOUT_ROOT / "tests" / "_fixtures" / "same_event_cases.json").is_file()
