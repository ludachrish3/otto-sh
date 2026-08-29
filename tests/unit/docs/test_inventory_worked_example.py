"""The inventory guide's worked example IS the tech1-inventory fixture (spec §15).

Each fenced JSON block on the page is preceded by an HTML comment naming the
fixture file it mirrors (``<!-- fixture: tech1-inventory/inventory.json -->``);
the block must parse to the same document. The TOML block names ``backend``
and ``supplies`` that must match the fixture's ``user-settings.toml`` (paths
differ by design — the page shows a home-relative layout, the fixture anchors
its paths to its own directory so it stays checkout-independent).

Documentation and fixture drift apart the moment either is edited alone: the
page teaches the shape, the fixture proves the shape loads, and a reader who
copies a page that no longer matches the fixture copies something nothing
tests. This guard makes the two one artefact.
"""

import json
import re

import tomli

from tests._fixtures.labdata import lab_data_dir
from tests._fixtures.paths import PROJECT_ROOT

_PAGE = PROJECT_ROOT / "docs" / "guide" / "configuration" / "inventory.md"
_FIXTURE = "tech1-inventory"
_BLOCK = re.compile(
    r"<!-- fixture: tech1-inventory/(?P<name>[\w.-]+) -->\s*"
    r"```(?P<lang>json|toml)\n(?P<body>.*?)```",
    re.DOTALL,
)
_EXPECTED = {"inventory.json", "creds.json", "lab.json", "user-settings.toml"}


def _blocks() -> "dict[str, tuple[str, str]]":
    # Collected as a LIST first: keying straight into a dict would let two
    # blocks carrying the same marker collapse to the last one silently, and a
    # page that mirrors one fixture file twice is a page defect — the earlier
    # block would be checked against nothing.
    marked = [(m["name"], (m["lang"], m["body"])) for m in _BLOCK.finditer(_PAGE.read_text())]
    names = [name for name, _ in marked]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    assert not duplicates, f"the page marks these fixture files more than once: {duplicates}"
    found = dict(marked)
    assert set(found) == _EXPECTED, sorted(found)
    return found


def test_json_blocks_equal_the_fixture_files() -> None:
    root = lab_data_dir() / _FIXTURE
    compared = []
    for name, (lang, body) in _blocks().items():
        if lang != "json":
            continue
        assert json.loads(body) == json.loads((root / name).read_text()), name
        compared.append(name)
    # Anchored: the loop above is vacuously green if the marked blocks stop
    # being JSON, so name the three files that must have been compared.
    assert sorted(compared) == ["creds.json", "inventory.json", "lab.json"]


def test_toml_block_declares_the_fixture_partition() -> None:
    lang, body = _blocks()["user-settings.toml"]
    assert lang == "toml", lang
    page = tomli.loads(body)["inventory"]
    fixture_text = (lab_data_dir() / _FIXTURE / "user-settings.toml").read_text()
    fixture = tomli.loads(fixture_text)["inventory"]
    assert page["backend"] == fixture["backend"]
    assert page["supplies"] == fixture["supplies"]
    # The paths differ ON PURPOSE — the page shows where a reader's own files
    # would live — so pin that they are stated at all, or a page that dropped
    # them would still pass the two assertions above.
    assert page["path"]
    assert page["creds_file"]
