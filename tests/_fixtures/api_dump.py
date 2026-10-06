"""Run the API dump producer on a fixture tree, the way the regenerator does.

The child is always a subprocess: the test process never imports
``scripts/api_dump_child.py``. Fixture packages are named ``otto`` and shadow the
real one, because the child puts the fixture ``src`` first on ``sys.path``.
"""

from pathlib import Path
from typing import Any

from scripts import api_records, api_regen
from scripts.api_manifest import Format


def write_tree(root: Path, files: "dict[str, str]") -> Path:
    """Write each ``rel: text`` of *files* under ``root/src``; return ``root/src``."""
    for rel, text in files.items():
        path = root / "src" / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root / "src"


def run_child_json(
    src: Path,
    namespaces: "list[str]",
    *,
    assume_dir: bool = False,
    seed: str = "0",
    formats: "list[Format] | None" = None,
) -> "dict[str, Any]":
    """Run the producer child on *src* and return its JSON report."""
    return api_regen.run_child(
        api_regen.CurrentEnv().python(src),
        src,
        namespaces,
        seed=seed,
        assume_dir=assume_dir,
        formats=formats,
    )


def dump_of(src: Path, namespaces: "list[str]") -> "api_records.Dump":
    """Return the parsed dump of *src*; fail the test on any refusal."""
    data = run_child_json(src, namespaces)
    assert data["refusals"] == [], data
    assert data["provenance"] == [], data
    records = [api_records.parse_record(line) for line in data["records"]]
    return api_records.parse_dump(api_records.render_dump(records))
