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
from tests._fixtures.settings_toml import (  # noqa: F401 — re-exported: the tests take them from here
    json_source,
    jsonnet_source,
    table,
    toml_value,
)

CREDS = [{"login": "admin", "password": "pw"}]
"""Logins a record or inline host carries, so every host builds."""


def write(path: Path, content: "str | dict[str, Any] | list[Any]") -> Path:
    """Write *content* (text, or a value as JSON) to *path*, parents created; return *path*."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content if isinstance(content, str) else json.dumps(content))
    return path


def inventory(root: Path, name: str, records: dict[str, dict[str, Any]], **keys: Any) -> str:
    """Write *records* as ``root/lab_data/<name>.json``; return its ``[[inventories]]`` table."""
    path = write(root / "lab_data" / f"{name}.json", records)
    return table("[[inventories]]", name=name, backend="json", path=str(path), **keys)


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


def lab_facts(sut: Project, name: str) -> dict[str, Any]:
    """Lab *name* of *sut*, loaded as ``--lab`` loads it, reduced to what must match.

    Each host keeps what it took from its record (address, interfaces, place,
    the reference and its ``extra``) and from otto's facts (terms, transfers,
    console, docker, resources, logins), so two spellings of one lab compare
    equal only when they build the same hosts.
    """
    from otto.session import build_lab

    lab = build_lab([sut], labs=[name])
    hosts = {
        host_id: {
            "ip": host.ip,
            "element": host.element.name,
            "valid_terms": list(host.valid_terms),
            "console": (host.console_options.server, host.console_options.port),
            "ref": (host.inventory_ref.name, host.inventory_ref.key, host.inventory_ref.qualified),
            "logins": [c.login for c in host.creds or []],
            "interfaces": {k: (i.ip, i.subnet) for k, i in host.interfaces.items()},
            "place": (host.site, host.rack, host.shelf, host.is_virtual),
            "docker": (host.docker_capable, host.docker_priority),
            "transfers": list(host.valid_transfers),
            "resources": sorted(host.resources),
            "extra": host.inventory_ref.extra,
        }
        for host_id, host in lab.hosts.items()
        if host_id != "local"  # the controller's own host, added to every lab
    }
    links = sorted((link.id, link.a.host, link.b.host, link.a.ip, link.b.ip) for link in lab.links)
    return {
        "hosts": hosts,
        "links": links,
        "resources": sorted(lab.resources),
        "metadata": lab.metadata,
    }


def listed_labs(sut: Project) -> list[str]:
    """Every lab *sut* lists, through the inventory view, as ``--list-labs`` lists them."""
    op = operation([sut])
    return [status.name for status in op.repository.lab_status_report(view=op.view).labs]
