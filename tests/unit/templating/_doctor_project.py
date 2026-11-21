"""Templated projects the doctor, ``--list-labs`` and dispatch are compared on.

Each project is real (:func:`~tests.unit.templating._lab_project.project`): a
``physical`` json inventory beside a ``jsonnet`` source whose entry renders
one lab per site, so a test picks how many labs an entry emits by the records
it writes. :func:`catalog` is the view ``otto init`` hands the doctor, :func:`check`
a doctor pass of a project through it, and :func:`lab_problems` the lab
verdict's problems that pass gives.
"""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from otto.config.project import Project
from otto.init import DoctorReport, check_project

from ._lab_project import CREDS, inventory, jsonnet_source, project, write

SITES = """
local o = import 'otto.libsonnet';
function(otto)
  o.layout({
    instances: o.groupBy(otto.inventories.physical, ['site']),
    lab(i): 'site-' + i.id.site,
    elementName(i, role, record, n): i.id.site + '-' + role + n,
    roles: {
      dut: {
        match: o.inGroup('dut'), count: 1,
        host: { label: 'dut', fn(r): { os_type: %(os_type)s } },
      },
    },
  })
"""
"""One lab per site, ``site-<site>``, each with one DUT from ``physical``."""


def sites_entry(*, os_type: str = "'unix'") -> str:
    """Return :data:`SITES` with the DUT's ``os_type`` expression *os_type*."""
    return SITES % {"os_type": os_type}


def records(*sites: str) -> dict[str, dict[str, Any]]:
    """One DUT record per site of *sites*, keyed ``dut-<site>``."""
    return {
        f"dut-{site}": {"ip": f"10.0.{i}.1", "site": site, "groups": ["dut"], "creds": CREDS}
        for i, site in enumerate(sites, start=1)
    }


def entry_path(root: Path) -> Path:
    """The one entry file a :func:`templated` project renders."""
    return root / "lab_data" / "layouts" / "sites.jsonnet"


def templated(
    root: Path,
    *,
    name: str = "sut",
    sites: "tuple[str, ...]" = ("east",),
    entry: "str | None" = None,
    extra: str = "",
    inventory_keys: "dict[str, Any] | None" = None,
    **source: Any,
) -> Project:
    """A project rendering :func:`sites_entry` (or *entry*) over its ``physical`` inventory."""
    write(entry_path(root), sites_entry() if entry is None else entry)
    source.setdefault("inventories", ["physical"])
    settings = (
        inventory(root, "physical", records(*sites), **(inventory_keys or {}))
        + extra
        + jsonnet_source(paths=[str(entry_path(root))], **source)
    )
    return project(root, settings, name=name)


def catalog(*projects: Project) -> Any:
    """The active catalog over *projects*, as ``otto init`` builds it."""
    from otto.inventory.config import deferred_inventory_view

    return deferred_inventory_view(projects=list(projects))


def lab_problems(report: DoctorReport) -> list[str]:
    """The lab verdict's problems; ``[]`` for a verdict that did not fail."""
    return list(report.verdict(name="lab").problems)


def check(root: Path, view: Any = None) -> DoctorReport:
    """``check_project`` of *root* with *view* as its catalog."""
    return check_project(root, catalog=view)


@contextmanager
def recorded_renders(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[Any]]:
    """Record every :class:`~otto.templating.render.RenderRequest` that reaches the renderer."""
    from otto.templating import render as render_module

    requests: list[Any] = []
    real: Callable[..., Any] = render_module.render

    def recording(request: Any) -> Any:
        requests.append(request)
        return real(request)

    monkeypatch.setattr(render_module, "render", recording)
    yield requests
