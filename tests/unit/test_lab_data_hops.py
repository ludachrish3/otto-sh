"""Guard: lab data only defines hops where a hop is physically required.

Unix VMs are directly reachable on the private net; the hop was test
scaffolding (hop *functionality* is covered by test_hop_integration.py).
Only embedded (Zephyr) hosts legitimately hop, through basil.

Note: hosts with no ``os_type`` key default to ``"unix"`` per the HostSpec
model, so the guard treats missing ``os_type`` as ``"unix"``.
"""

from __future__ import annotations

import json

import pytest

from tests._fixtures.labdata import lab_data_path

_TECHS = ("tech1", "tech2")


@pytest.mark.parametrize("tech", _TECHS)
def test_no_unix_host_defines_a_hop(tech: str) -> None:
    hosts = json.loads(lab_data_path(tech).read_text())
    offenders = [h["element"] for h in hosts if h.get("os_type", "unix") == "unix" and "hop" in h]
    assert not offenders, (
        f"{tech}: Unix hosts must be directly reachable (no hop) — "
        f"hop functionality is covered by test_hop_integration.py. Offenders: {offenders}"
    )


@pytest.mark.parametrize("tech", _TECHS)
def test_embedded_hops_are_preserved(tech: str) -> None:
    hosts = json.loads(lab_data_path(tech).read_text())
    embedded = [h for h in hosts if h.get("os_type", "unix") != "unix"]
    if not embedded:
        pytest.skip(f"{tech}: no embedded hosts — hop-preservation check not applicable")
    # Every embedded host that needs a hop still declares one (regression guard
    # against an over-eager sweep deleting the real basil hops).
    hopped = [h["element"] for h in embedded if "hop" in h]
    assert hopped, f"{tech}: expected embedded hosts to retain their (basil) hop"
