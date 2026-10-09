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
from tests._fixtures.paths import PROJECT_ROOT

DATA = PROJECT_ROOT / "web" / "src" / "data"


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
