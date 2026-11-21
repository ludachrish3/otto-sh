"""Real projects with a ``jsonnet`` lab source, for the jsonnet source's tests.

Each project is written to disk with
:func:`~tests._fixtures.project_scaffold.make_project`, so its sources are
prepared and built through the real settings path, and every inventory it
names is a ``json`` inventory file beside its lab data. :func:`operation` is
what one otto operation holds: the host source every project's sources build
into, and the one inventory view they read through.
"""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from otto.config.project import Project
from otto.inventory import InventoryView
from otto.labs import CompositeLabRepository
from tests._fixtures.project_scaffold import make_project

CREDS = [{"login": "admin", "password": "pw"}]
"""Logins a record or inline host carries, so every host builds."""


def toml_value(value: Any) -> str:
    """*value* as an inline TOML value: tables inline, strings as basic strings."""
    if isinstance(value, dict):
        return (
            "{ " + ", ".join(f"{json.dumps(k)} = {toml_value(v)}" for k, v in value.items()) + " }"
        )
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(toml_value(v) for v in value) + "]"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    return json.dumps(str(value))


def table(header: str, **keys: Any) -> str:
    """A TOML table (``[x]`` or ``[[x]]`` *header*) holding *keys* as inline values."""
    return header + "\n" + "".join(f"{k} = {toml_value(v)}\n" for k, v in keys.items()) + "\n"


def write(path: Path, content: "str | dict[str, Any] | list[Any]") -> Path:
    """Write *content* (text, or a value as JSON) to *path*, parents created; return *path*."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content if isinstance(content, str) else json.dumps(content))
    return path


def inventory(root: Path, name: str, records: dict[str, dict[str, Any]], **keys: Any) -> str:
    """Write *records* as ``root/lab_data/<name>.json``; return its ``[[inventories]]`` table."""
    path = write(root / "lab_data" / f"{name}.json", records)
    return table("[[inventories]]", name=name, backend="json", path=str(path), **keys)


def jsonnet_source(**keys: Any) -> str:
    """Return a ``backend = "jsonnet"`` ``[[lab.sources]]`` table holding *keys*."""
    return table("[[lab.sources]]", backend="jsonnet", **keys)


def json_source(*paths: Path) -> str:
    """Return a ``backend = "json"`` ``[[lab.sources]]`` table reading *paths*."""
    return table("[[lab.sources]]", backend="json", paths=[str(p) for p in paths])


def project(root: Path, settings: str, *, name: str = "sut") -> Project:
    """A real project at *root* whose ``.otto/settings.toml`` holds *settings*."""
    return Project(root=make_project(root, name=name, extra=settings))


@dataclass(frozen=True)
class Operation:
    """What one otto operation holds: the host source and its one inventory view."""

    repository: CompositeLabRepository
    view: InventoryView


def operation(projects: "list[Project]") -> Operation:
    """Build *projects*' host source and the inventory view of one operation over them."""
    from otto.inventory import build_inventory_view
    from otto.labs.sources import build_lab_sources

    return Operation(
        repository=build_lab_sources(projects=projects),
        view=build_inventory_view(projects=projects),
    )
