"""The browser's monitor-export versions mirror the declared ones (dump spec §13.1).

``web/src/data/exportFormat.ts`` cannot import ``otto.models.formats``, so it
spells the two lists again. This holds them equal: a version added to or
dropped from one side fails here, in every Python lane, before the browser
ships with a reader that disagrees.
"""

import ast
import re

import pytest

from otto.models import formats
from tests._fixtures.paths import PROJECT_ROOT

TS = PROJECT_ROOT / "web" / "src" / "data" / "exportFormat.ts"


def _ts_list(name: str) -> list:
    found = re.search(
        rf"^export const {name} = (\[[^\]]*\]) as const", TS.read_text(), re.MULTILINE
    )
    assert found, f"{name} is not declared in {TS}"
    return ast.literal_eval(found.group(1))


@pytest.mark.parametrize("name", ["MONITOR_EXPORT_READ_VERSIONS", "MONITOR_EXPORT_WRITE_VERSIONS"])
def test_the_browser_list_mirrors_the_declared_one(name):
    assert _ts_list(name) == getattr(formats, name)
