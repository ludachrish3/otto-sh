"""The tiers page's unit ``toolchain`` example is a table the settings model accepts.

A block that only looks like settings teaches a shape the loader refuses; this
loads the exact block, under a unit tier, through ``SettingsModel`` and checks
every field arrives as written.
"""

import re

import tomli

from otto.models.settings import SettingsModel
from tests._fixtures.paths import CHECKOUT_ROOT

_PAGE = CHECKOUT_ROOT / "docs" / "cli" / "cov" / "tiers.md"
_BLOCK = re.compile(
    r"<!-- pinned by tests/unit/docs/test_unit_tier_toolchain_example.py -->\s*"
    r"```toml\n(?P<body>.*?)```",
    re.DOTALL,
)


def _block() -> str:
    found = [m["body"] for m in _BLOCK.finditer(_PAGE.read_text())]
    assert len(found) == 1, (
        f"expected one pinned toolchain block on {_PAGE.name}, found {len(found)}"
    )
    return found[0]


def test_the_unit_toolchain_example_loads_through_the_settings_model() -> None:
    raw = tomli.loads(_block())
    raw["coverage"]["tiers"]["unit"].update(kind="unit", precedence=2)

    settings = SettingsModel.model_validate({"name": "demo", "version": "1.0.0", **raw})

    toolchain = settings.coverage.tiers["unit"].toolchain
    assert toolchain is not None
    assert toolchain.coverage_tool == "gcov-12"
    assert toolchain.lcov == "/opt/lcov2/bin/lcov"
    assert toolchain.lcov_args == ["--rc", "geninfo_unexecuted_blocks=1"]
