"""In-memory reference :class:`~otto.storage.protocol.LabRepository` (sample).

A teaching/reference host-source backend: it holds a mapping of lab name to a
list of host dicts and builds real hosts via
:func:`otto.storage.create_host_from_dict`. It needs no files or network, so it
runs inside doctests and the conformance suite, and SUT authors can copy it as a
starting point.

Register it from an ``init`` module and select it by name::

    from otto.storage import register_lab_repository
    from otto.examples.lab_repository import ExampleLabRepository

    register_lab_repository("example", ExampleLabRepository)

then in ``.otto/settings.toml``::

    [lab]
    backend = "example"

Direct usage:

>>> from otto.examples.lab_repository import ExampleLabRepository
>>> repo = ExampleLabRepository()
>>> repo.list_labs()
['east', 'west']
>>> lab = repo.load_lab("east")
>>> lab.name
'east'
>>> len(lab.hosts)
1
"""

from pathlib import Path
from typing import Any

from ..configmodule.lab import Lab
from ..storage import (
    LabNotFoundError,
    create_host_from_dict,
)

# A tiny built-in dataset so the sample works out of the box (doctests +
# conformance). Each value is a list of host dicts as they'd appear in a
# hosts.json entry; the mapping key supplies lab membership here, so the
# host-level "labs" field is unnecessary.
_DEMO_LABS: dict[str, list[dict[str, Any]]] = {
    "east": [
        {
            "ip": "10.0.0.1",
            "element": "router1",
            "creds": [{"login": "admin", "password": "admin"}],
            "resources": ["router1"],
        },
    ],
    "west": [
        {
            "ip": "10.0.1.1",
            "element": "router2",
            "creds": [{"login": "admin", "password": "admin"}],
            "resources": ["router2"],
        },
    ],
}


class ExampleLabRepository:
    """In-memory :class:`~otto.storage.protocol.LabRepository` reference backend.

    Parameters
    ----------
    repo_dir : Path | None
        Accepted for factory/registry uniformity — :func:`otto.storage.build_lab_repository`
        constructs a custom backend as ``cls(repo_dir=..., **kwargs)``. This
        in-memory sample has no files to resolve, so it is ignored.
    labs : dict[str, list[dict]] | None
        Optional mapping of lab name to host dicts. Defaults to a small built-in
        demo dataset.
    """

    def __init__(
        self,
        *,
        repo_dir: Path | None = None,  # noqa: ARG002 — required by registry-seam constructor signature (build_lab_repository passes repo_dir= to all backends)
        labs: dict[str, list[dict[str, Any]]] | None = None,
    ) -> None:
        self._labs: dict[str, list[dict[str, Any]]] = (
            {k: list(v) for k, v in _DEMO_LABS.items()} if labs is None else labs
        )

    def load_lab(
        self,
        name: str,
        preferences: dict[str, dict[str, Any]] | None = None,
    ) -> Lab:
        """Build and return a ``Lab`` from the in-memory dataset.

        Raises
        ------
        LabNotFoundError
            If ``name`` is not in this backend's dataset.
        """
        if name not in self._labs:
            known = ", ".join(sorted(self._labs)) or "(none)"
            raise LabNotFoundError(f"Lab {name!r} not found. Known labs: {known}")
        lab = Lab(name=name)
        for host_data in self._labs[name]:
            host = create_host_from_dict(host_data, preferences=preferences)
            lab.add_host(host)
            lab.resources.update(host.resources)
        return lab

    def list_labs(self) -> list[str]:
        """Return a sorted list of all lab names in this backend's dataset."""
        return sorted(self._labs)
