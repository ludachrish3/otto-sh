"""Guard: lab data only defines hops where a hop is physically required.

The lab's Unix VMs are directly reachable on the private net; a hop on one of
them was test scaffolding (hop *functionality* is covered by
test_hop_integration.py). Hops are legitimate for exactly two populations, and
both are pinned below by name rather than waved through by category:

* the embedded (Zephyr) instances, which hop through basil;
* the BusyBox bed guests (``bb*``, spec
  ``docs/superpowers/specs/2026-08-20-busybox-bed-and-tier-migration-design.md``),
  which are Unix hosts and hop through carrot. They sit behind QEMU user-mode
  networking on their hop's loopback, so a hop is not scaffolding there — it is
  the only route that exists.

Widening the first guard to "Unix hosts may hop" would delete it, so the bed
guests are carved out by their ``busybox`` lab membership and then held to the
exact roster and hop the bed provisions.

Note: hosts with no ``os_type`` key default to ``"unix"`` per the HostSpec
model, so the guard treats missing ``os_type`` as ``"unix"``.
"""

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

# The only Unix hosts allowed to carry a hop, and the hop they carry. Same
# spirit as _EMBEDDED_TECHS: an exact roster, so both a lost bed guest and a
# stray sixth one fail loudly.
_BUSYBOX_TECHS = ("tech1",)
_BUSYBOX_GUESTS = ("bb1161", "bb1211", "bb1281", "bb1310", "bb1350")
_BUSYBOX_HOP = "carrot_seed"


def _is_bed_guest(host: dict) -> bool:
    """A BusyBox bed guest: Unix-family, but reachable only through its hop."""
    return "busybox" in host.get("labs", [])


@pytest.mark.parametrize("tech", _TECHS)
def test_no_unix_host_defines_a_hop(tech: str) -> None:
    hosts = json.loads(lab_data_path(tech).read_text())["hosts"]
    offenders = [
        h["element"]
        for h in hosts
        if h.get("os_type", "unix") == "unix" and "hop" in h and not _is_bed_guest(h)
    ]
    assert not offenders, (
        f"{tech}: Unix VMs must be directly reachable (no hop) — "
        f"hop functionality is covered by test_hop_integration.py, and the one "
        f"legitimate Unix exception (the BusyBox bed guests) is pinned by "
        f"test_busybox_bed_guests_hop_through_carrot. Offenders: {offenders}"
    )


@pytest.mark.parametrize("tech", _TECHS)
def test_busybox_bed_guests_hop_through_carrot(tech: str) -> None:
    """The carve-out above is only as honest as this pin: the Unix hosts that
    hop must be exactly the five bed guests, each through carrot."""
    hosts = json.loads(lab_data_path(tech).read_text())["hosts"]
    guests = [h for h in hosts if _is_bed_guest(h) and "hop" in h]
    expected = _BUSYBOX_GUESTS if tech in _BUSYBOX_TECHS else ()
    assert tuple(h["element"] for h in guests) == expected, (
        f"{tech}: the hop-fronted BusyBox bed roster drifted — a guest lost its "
        f"hop, was renamed, or a new Unix host claimed 'busybox' lab membership"
    )
    assert {h["hop"] for h in guests} == ({_BUSYBOX_HOP} if expected else set())


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
