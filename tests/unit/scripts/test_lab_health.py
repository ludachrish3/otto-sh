"""Routing regression tests for ``scripts/lab_health.py``.

Guards the os_type-literal rot exposed when commit 41cf70c renamed the lab
data's console hosts from ``os_type: "embedded"`` to ``os_type: "zephyr"``: the
health probe kept routing only ``os_type == "embedded"`` to the console check,
so every Zephyr host fell into the SSH check and crashed on the missing
``creds`` key. The contract under test is reach-path, not a literal: a host
without its own ``creds`` must be probed via the hop/console path, never the
direct-SSH path.

The BusyBox bed widened that contract without changing its nature — the reach
path is still read off the entry's shape, but a hop now outranks creds. Those
guests are ``os_type: "unix"`` WITH their own root login, yet their ``ip`` is
``127.0.0.1`` on their hop, so an SSH probe would silently interrogate whatever
machine runs this script and report a healthy bed no matter what the guests are
doing.
"""

import socket
import subprocess
import sys
import threading

from scripts import lab_health
from scripts.lab_health import (
    _CONSOLE_PROBE,
    DEFAULT_HOSTS,
    _hop_index,
    _load_hosts,
    _print_report,
)


def _route_probes(monkeypatch):
    """Run ``_print_report`` over the real lab data with both probes stubbed,
    recording which host each probe was asked to handle."""
    hosts = _load_hosts(DEFAULT_HOSTS)
    hops = _hop_index(hosts)
    seen: dict[str, list[str]] = {"unix": [], "embedded": []}

    def fake_unix(host):
        seen["unix"].append(host["element"])
        return {"ok": True, "status": "up", "info": ""}

    def fake_embedded(host, hops):
        seen["embedded"].append(host["element"])
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
    assert any("creds" not in h for h in hosts), (
        "fixture sanity: expected some credential-less hosts in the lab data — "
        "with none, every assertion below is skipped and this guard cannot fail"
    )
    for host in hosts:
        if "creds" not in host:
            assert host["element"] not in seen["unix"], (
                f"{host['element']} (os_type={host.get('os_type')!r}) was routed to "
                "the SSH probe but carries no creds"
            )
            assert host["element"] in seen["embedded"]


def test_zephyr_hosts_route_to_console_probe(monkeypatch):
    hosts, seen = _route_probes(monkeypatch)
    zephyr = [h["element"] for h in hosts if h.get("os_type") == "zephyr"]
    assert zephyr, "fixture sanity: expected some zephyr hosts in the lab data"
    assert set(zephyr) <= set(seen["embedded"])
    assert not (set(zephyr) & set(seen["unix"]))


def test_unix_hosts_route_to_ssh_probe(monkeypatch):
    """Creds AND no hop is the direct-SSH shape. The `not h.get("hop")` half
    arrived with the BusyBox bed: those guests are ``os_type: "unix"`` and carry
    their own creds, but their ``ip`` is their hop's loopback, so the SSH path
    would probe the machine running this script. They are pinned to the console
    path by the companion assertion below, not merely excluded from this one."""
    hosts, seen = _route_probes(monkeypatch)
    ssh_hosts = {h["element"] for h in hosts if "creds" in h and not h.get("hop")}
    assert ssh_hosts, "fixture sanity: expected some credentialed hopless unix hosts"
    assert ssh_hosts <= set(seen["unix"])
    assert not (ssh_hosts & set(seen["embedded"]))

    hop_fronted = {h["element"] for h in hosts if "creds" in h and h.get("hop")}
    assert hop_fronted, (
        "fixture sanity: expected cred-carrying hop-fronted guests (the BusyBox "
        "bed) in the lab data — with none, the routing half this test guards is "
        "never exercised"
    )
    assert hop_fronted <= set(seen["embedded"])
    assert not (hop_fronted & set(seen["unix"]))


def test_a_cred_carrying_guest_behind_a_hop_is_probed_via_the_hop(monkeypatch):
    """bb guests have creds AND a hop; SSHing 127.0.0.1 would probe the dev VM
    itself. Routing must send them down the console path."""
    guest = {
        "ip": "127.0.0.1",
        "element": "bb1161",
        "os_type": "unix",
        "hop": "carrot_seed",
        "creds": [{"login": "root", "password": "otto"}],
        "telnet_options": {"port": 2316},
    }
    assert lab_health._is_ssh_host(guest) is False
    hop = {
        "ip": "10.10.200.11",
        "element": "carrot",
        "board": "seed",
        "creds": [{"login": "vagrant", "password": "vagrant"}],
    }
    assert lab_health._is_ssh_host(hop) is True

    seen = {}

    def fake_run_ssh(ip, user, password, cmd, timeout=25.0):
        seen["ip"] = ip
        seen["cmd"] = cmd
        return 0, "OK login", ""

    monkeypatch.setattr(lab_health, "_run_ssh", fake_run_ssh)
    res = lab_health._check_embedded(guest, {"carrot_seed": hop})
    assert seen["ip"] == "10.10.200.11"  # probed FROM the hop
    assert " 2316" in seen["cmd"]  # honors telnet_options.port
    assert res == {"ok": True, "status": "up", "info": "login prompt"}


def test_the_console_probe_reports_a_login_prompt_as_ok():
    """Drive the real probe script against a scripted telnet-ish server.

    The fake serves what a BusyBox guest's telnetd actually answers with — an
    IAC option negotiation byte run followed by the login banner — so the probe
    is exercised end to end, script text included, rather than through a stub
    of its own output.
    """
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]

    def serve():
        conn, _ = srv.accept()
        with conn:
            conn.recv(64)
            conn.sendall(b"\xff\xfd\x18bb1161 login: ")

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        proc = subprocess.run(
            [sys.executable, "-c", _CONSOLE_PROBE, "127.0.0.1", str(port)],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        # Bounded: the server has already answered by the time the probe exits,
        # so this only reaps the thread — it is not a wait FOR the answer.
        thread.join(timeout=5)
    finally:
        srv.close()
    assert proc.stdout.strip() == "OK login"


def test_restart_units_include_the_busybox_bed(monkeypatch):
    """``make qemu-restart`` is the bed's recovery command; a glob naming only
    the zephyr units would restart nothing at all on carrot."""
    captured = {}

    def fake_run_ssh(ip, user, password, cmd, timeout=25.0):
        captured["cmd"] = cmd
        return 0, "", ""

    monkeypatch.setattr(lab_health, "_run_ssh", fake_run_ssh)
    hosts = [
        {
            "ip": "10.10.200.11",
            "element": "carrot",
            "board": "seed",
            "creds": [{"login": "vagrant", "password": "vagrant"}],
        },
        {
            "ip": "127.0.0.1",
            "element": "bb1161",
            "hop": "carrot_seed",
            "creds": [{"login": "root", "password": "otto"}],
        },
    ]
    rc = lab_health._restart_qemu(hosts, lab_health._hop_index(hosts))
    assert rc == 0
    assert "busybox-qemu-*.service" in captured["cmd"]
    assert "zephyr-qemu-*.service" in captured["cmd"]
