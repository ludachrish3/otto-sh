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


def test_the_browser_same_event_runs_the_shared_cases_file():
    """``sameevent.test.ts`` drives the SAME cases file ``same_event`` is tested on.

    One file of cases holds the server's comparison and its browser mirror to
    one rule; a test that loaded a copy would let the two drift.
    """
    source = (DATA.parent / "__tests__" / "sameevent.test.ts").read_text()
    assert "tests/_fixtures/same_event_cases.json" in source
    assert (CHECKOUT_ROOT / "tests" / "_fixtures" / "same_event_cases.json").is_file()


@pytest.mark.parametrize(
    "name", ["SOURCE_TOKEN_HEADER", "OBSERVED_AT_HEADER", "UNSAVED_HEADER", "SOURCE_NAME_HEADER"]
)
def test_the_browser_source_headers_mirror_the_server_ones(name):
    """``sourceApi.ts`` spells the headers ``otto.monitor.server`` declares (#866, #958).

    The browser cannot import the Python constants, so it spells each name
    again. A rename on one side alone would send every write without its
    token, and refuse it as aimed at another source, or silently drop the
    server's stamps.
    """
    from otto.monitor import server

    found = re.search(
        rf'^export const {name} = "([^"]+)";',
        (DATA / "sourceApi.ts").read_text(),
        re.MULTILINE,
    )
    assert found, f"{name} is not declared in sourceApi.ts"
    assert found.group(1) == getattr(server, name)
