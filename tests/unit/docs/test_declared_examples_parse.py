"""Every [[products]]/[[dev_tools]] entry in a shipped example project parses under the
reserved-key rule — `class` or `kind`, an optional `variant` — so no page teaches a shape
otto refuses."""

from pathlib import Path

import pytest
import tomli

from otto.models.settings import DeclaredEntrySpec
from tests._fixtures.paths import PROJECT_ROOT

SETTINGS = sorted((PROJECT_ROOT / "docs" / "examples").glob("*/.otto/settings.toml"))


@pytest.mark.parametrize("path", SETTINGS, ids=lambda p: p.parent.parent.name)
def test_example_entries_parse(path: Path) -> None:
    data = tomli.loads(path.read_text())
    entries = [*data.get("products", []), *data.get("dev_tools", [])]
    for raw in entries:
        DeclaredEntrySpec.model_validate(raw)
