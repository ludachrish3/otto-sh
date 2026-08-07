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
# Which techs are EXPECTED to carry embedded hosts. tech2's lab is all-unix by
# design, so it has no hops to preserve. Declared here rather than skipped at
# runtime: a runtime "no embedded hosts" skip would also fire if a sweep
# deleted tech1's embedded hosts, which is exactly the accident the guard
# exists to catch — and the two-sided assert below fails loudly if tech2 ever
# gains embedded hosts without joining this tuple.
_EMBEDDED_TECHS = ("tech1",)


@pytest.mark.parametrize("tech", _TECHS)
def test_no_unix_host_defines_a_hop(tech: str) -> None:
    hosts = json.loads(lab_data_path(tech).read_text())["hosts"]
    offenders = [h["element"] for h in hosts if h.get("os_type", "unix") == "unix" and "hop" in h]
    assert not offenders, (
        f"{tech}: Unix hosts must be directly reachable (no hop) — "
        f"hop functionality is covered by test_hop_integration.py. Offenders: {offenders}"
    )


@pytest.mark.parametrize("tech", _TECHS)
def test_embedded_hops_are_preserved(tech: str) -> None:
    hosts = json.loads(lab_data_path(tech).read_text())["hosts"]
    embedded = [h for h in hosts if h.get("os_type", "unix") != "unix"]
    expectation = (
        "missing — the over-eager-sweep accident this guard exists to catch"
        if tech in _EMBEDDED_TECHS
        else "unexpectedly present — add the tech to _EMBEDDED_TECHS"
    )
    assert bool(embedded) == (tech in _EMBEDDED_TECHS), (
        f"{tech}: embedded hosts {expectation} (found: {[h['element'] for h in embedded]!r})"
    )
    # EVERY embedded host still declares its hop (regression guard against an
    # over-eager sweep deleting the real basil hops — a partial sweep is the
    # same accident, so "at least one survived" is not good enough).
    unhopped = [h["element"] for h in embedded if "hop" not in h]
    assert not unhopped, f"{tech}: embedded hosts lost their (basil) hop: {unhopped}"
