"""Every shipped example project's settings parse as otto reads them, so no page teaches a
shape otto refuses: each [[products]]/[[dev_tools]] entry under the reserved-key rule —
`class` or `kind`, an optional `variant` — and each whole file as settings load does."""

from pathlib import Path

import pytest
import tomli

from otto.config.project import compile_settings
from otto.models.base import load_toml_settings
from otto.models.settings import DeclaredEntrySpec
from tests._fixtures.paths import CHECKOUT_ROOT

SETTINGS = sorted((CHECKOUT_ROOT / "docs" / "examples").glob("*/.otto/settings.toml"))
PROJECTS = [*(p.parent.parent for p in SETTINGS), CHECKOUT_ROOT / "tests" / "project_hier"]


@pytest.mark.parametrize("path", SETTINGS, ids=lambda p: p.parent.parent.name)
def test_example_entries_parse(path: Path) -> None:
    data = tomli.loads(path.read_text())
    entries = [*data.get("products", []), *data.get("dev_tools", [])]
    for raw in entries:
        DeclaredEntrySpec.model_validate(raw)


@pytest.mark.parametrize("project", PROJECTS, ids=lambda p: p.name)
def test_every_example_settings_file_validates(project: Path) -> None:
    """The whole file goes through the loader's own parse and compile, at its project root."""
    text = (project / ".otto" / "settings.toml").read_text()
    compile_settings(load_toml_settings(text), project)
