"""Single source of truth for test lab-data paths and host builders.

Centralizes the lab JSON location so test modules never hand-roll
``Path(__file__).parents[N] / "lab_data" / ...`` arithmetic (which breaks
whenever a file moves to a different depth). Import from here (or via the
re-exports in :mod:`tests.conftest`).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from otto.host.unix_host import UnixHost

_LAB_DATA_DIR = Path(__file__).parent / "lab_data"


def lab_data_dir() -> Path:
    """Directory holding the per-tech lab-data trees (``tech1``/``tech2``)."""
    return _LAB_DATA_DIR


def lab_data_path(tech: str = "tech1") -> Path:
    """Path to a tech's ``hosts.json`` (default the primary ``tech1`` lab)."""
    return _LAB_DATA_DIR / tech / "hosts.json"


def host_data(ne: str, tech: str = "tech1") -> dict[str, Any]:
    """Return the raw host dict for ``ne`` from the lab JSON."""
    hosts = json.loads(lab_data_path(tech).read_text())
    for host in hosts:
        if host["element"] == ne:
            return host
    raise KeyError(f"NE {ne!r} not found in {lab_data_path(tech)}")


def make_host(ne: str, **kwargs: Any) -> UnixHost:
    """Build a UnixHost from lab data with optional field overrides."""
    data = host_data(ne)
    return UnixHost(
        ip=data["ip"],
        element=data["element"],
        creds=data["creds"],
        board=data.get("board"),
        is_virtual=data.get("is_virtual", False),
        **kwargs,
    )
