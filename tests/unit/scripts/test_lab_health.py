"""Routing regression tests for ``scripts/lab_health.py``.

Guards the osType-literal rot exposed when commit 41cf70c renamed the lab
data's console hosts from ``osType: "embedded"`` to ``osType: "zephyr"``: the
health probe kept routing only ``osType == "embedded"`` to the console check,
so every Zephyr host fell into the SSH check and crashed on the missing
``creds`` key. The contract under test is reach-path, not a literal: a host
without its own ``creds`` must be probed via the hop/console path, never the
direct-SSH path.
"""
from scripts import lab_health
from scripts.lab_health import DEFAULT_HOSTS, _hop_index, _load_hosts, _print_report


def _route_probes(monkeypatch):
    """Run ``_print_report`` over the real lab data with both probes stubbed,
    recording which host each probe was asked to handle."""
    hosts = _load_hosts(DEFAULT_HOSTS)
    hops = _hop_index(hosts)
    seen: dict[str, list[str]] = {"unix": [], "embedded": []}

    def fake_unix(host):
        seen["unix"].append(host["ne"])
        return {"ok": True, "status": "up", "info": ""}

    def fake_embedded(host, hops):
        seen["embedded"].append(host["ne"])
        return {"ok": True, "status": "up", "info": ""}

    monkeypatch.setattr(lab_health, "_check_unix", fake_unix)
    monkeypatch.setattr(lab_health, "_check_embedded", fake_embedded)
    _print_report(hosts, hops)
    return hosts, seen


def test_no_credless_host_is_sent_to_the_ssh_probe(monkeypatch):
    """The SSH probe dereferences ``host['creds']``; routing a credential-less
    host there is exactly the 41cf70c crash. Such hosts must use the console
    probe instead."""
    hosts, seen = _route_probes(monkeypatch)
    for host in hosts:
        if "creds" not in host:
            assert host["ne"] not in seen["unix"], (
                f"{host['ne']} (osType={host.get('osType')!r}) was routed to "
                "the SSH probe but carries no creds"
            )
            assert host["ne"] in seen["embedded"]


def test_zephyr_hosts_route_to_console_probe(monkeypatch):
    hosts, seen = _route_probes(monkeypatch)
    zephyr = [h["ne"] for h in hosts if h.get("osType") == "zephyr"]
    assert zephyr, "fixture sanity: expected some zephyr hosts in the lab data"
    assert set(zephyr) <= set(seen["embedded"])
    assert not (set(zephyr) & set(seen["unix"]))


def test_unix_hosts_route_to_ssh_probe(monkeypatch):
    hosts, seen = _route_probes(monkeypatch)
    ssh_hosts = {h["ne"] for h in hosts if "creds" in h}
    assert ssh_hosts, "fixture sanity: expected some credentialed unix hosts"
    assert ssh_hosts <= set(seen["unix"])
    assert not (ssh_hosts & set(seen["embedded"]))
