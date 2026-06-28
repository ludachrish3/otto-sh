#!/usr/bin/env python3
"""Probe every lab VM + Zephyr QEMU instance and report health + timestamps.

Reads the lab data (``tests/_fixtures/lab_data/tech1/hosts.json`` by default) and, for
each defined host, reports reachability and a timestamp:

* **Unix VMs** (``os_type == "unix"``) are reached over SSH; the script reads
  each VM's wall clock and prints the **drift** against this machine's clock,
  so you can spot NTP/clock-skew problems at a glance.

* **Embedded console instances** (the ``EmbeddedHost`` family — e.g.
  ``os_type == "zephyr"``) carry no SSH creds of their own: they sit behind an
  SSH hop VM and are reached by SSHing to the hop and telnetting to the guest
  console (the same path otto uses). Zephyr has no RTC, so these report
  **kernel uptime** + console responsiveness rather than wall-clock drift.

With ``--restart-qemu`` the script first restarts the ``zephyr-qemu-*`` and
``zephyr-snmp-relay-*`` systemd units on each hop VM, waits for the guests to
boot, then runs the health check. Use it to recover a wedged bed (e.g. after
the embedded test gate reports "console wedged").

Exit status is non-zero if any host is unreachable/unresponsive.

Usage::

    scripts/lab_health.py
    scripts/lab_health.py --hosts tests/_fixtures/lab_data/tech1/hosts.json
    scripts/lab_health.py --restart-qemu
"""

from __future__ import annotations

import argparse
import json
import shlex
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_HOSTS = Path("tests/_fixtures/lab_data/tech1/hosts.json")

# Non-interactive SSH: no host-key prompts, no known_hosts churn (the lab VMs
# get rebuilt often), quiet, and a bounded connect so a dead VM fails fast.
_SSH_OPTS = [
    "-o",
    "StrictHostKeyChecking=no",
    "-o",
    "UserKnownHostsFile=/dev/null",
    "-o",
    "LogLevel=ERROR",
    "-o",
    "ConnectTimeout=10",
]

# Flag a Unix VM whose clock differs from this host by more than this (seconds).
_DRIFT_WARN_S = 2.0

# Runs on the hop VM (which has python3) to probe a guest telnet console: open
# the port, nudge the shell, and read the uptime line back. Prints one of
# "OK <ms>" / "NOOUT" (TCP open but the guest emitted nothing — the classic
# wedge) / "CONNFAIL <err>". The port is argv[2], NOT hardcoded: the x86 net
# beds expose the in-guest shell on :23 (reached over their TAP), but the ARM
# serial beds bridge UART to a telnet listener on a loopback /32 at 2323+. A
# hardcoded :23 would connect to the hop's own 0.0.0.0:23 telnetd for those
# loopback addresses and report a false "up" — so honor telnet_options.port.
_CONSOLE_PROBE = r"""
import re, socket, sys, time
ip = sys.argv[1]
port = int(sys.argv[2])
try:
    s = socket.create_connection((ip, port), timeout=4)
except Exception as e:
    print("CONNFAIL", e)
    raise SystemExit(0)
s.settimeout(4)
try:
    s.sendall(b"\r\nkernel uptime\r\n")
    time.sleep(1.2)
    data = b""
    while True:
        chunk = s.recv(512)
        if not chunk:
            break
        data += chunk
except Exception:
    pass
finally:
    s.close()
if not data:
    print("NOOUT")
else:
    m = re.search(rb"Uptime:\s*(\d+)\s*ms", data)
    print("OK", m.group(1).decode() if m else "?")
"""


def _load_hosts(path: Path) -> list[dict]:
    return json.loads(path.read_text())


def _ssh_user_pass(creds: dict[str, str]) -> tuple[str, str]:
    """Pick the SSH login. Prefer ``vagrant``; otherwise the first cred."""
    if "vagrant" in creds:
        return "vagrant", creds["vagrant"]
    user = next(iter(creds))
    return user, creds[user]


def _run_ssh(
    ip: str, user: str, password: str, remote_cmd: str, timeout: float = 25.0
) -> tuple[int, str, str]:
    """Run ``remote_cmd`` on ``ip`` over password SSH. Returns (rc, out, err)."""
    cmd = ["sshpass", "-p", password, "ssh", *_SSH_OPTS, f"{user}@{ip}", remote_cmd]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)  # noqa: S603 — trusted args
        return p.returncode, p.stdout.strip(), p.stderr.strip()
    except subprocess.TimeoutExpired:
        return 124, "", f"ssh timed out after {timeout:.0f}s"


def _hop_index(hosts: list[dict]) -> dict[str, dict]:
    """Map an otto hop id (``{ne}_{board}``) to its host entry."""
    return {f"{h['element']}_{h.get('board', 'seed')}": h for h in hosts}


def _is_ssh_host(host: dict) -> bool:
    """True if we log in directly over SSH (the ``UnixHost`` family); False if
    the host is an embedded console reached via a hop (``EmbeddedHost`` /
    ``ZephyrHost`` family).

    Route on the credential shape, not a hardcoded ``os_type`` literal. The SSH
    probe dereferences the host's own ``creds``; embedded consoles carry none
    and borrow their hop's. Keying on ``os_type == "embedded"`` silently
    misrouted every console — straight into a ``KeyError('creds')`` — once the
    lab data moved to ``os_type: "zephyr"`` (commit 41cf70c). The credential
    shape also survives the next os_type rename, and correctly keeps a Unix VM
    that fronts a guest (its own ``hop``, e.g. ``pepper``) on the SSH path.
    """
    return "creds" in host


def _check_unix(host: dict) -> dict:
    """Reachability + clock drift for a Unix VM."""
    user, password = _ssh_user_pass(host["creds"])
    before = time.time()
    rc, out, err = _run_ssh(host["ip"], user, password, "date -u +%s.%N")
    after = time.time()
    if rc != 0 or not out:
        return {"ok": False, "status": "UNREACHABLE", "info": err or f"rc={rc}"}
    try:
        remote_epoch = float(out.split()[0])
    except (ValueError, IndexError):
        return {"ok": False, "status": "BAD-CLOCK", "info": out[:40]}
    # Compare the remote clock to the midpoint of the request window to absorb
    # most of the round-trip latency.
    drift = remote_epoch - (before + after) / 2
    iso = datetime.fromtimestamp(remote_epoch, timezone.utc).strftime("%H:%M:%S")
    return {"ok": True, "status": "up", "info": f"{iso}Z", "drift": drift}


def _check_embedded(host: dict, hops: dict[str, dict]) -> dict:
    """Console responsiveness + uptime for an embedded QEMU guest."""
    hop = hops.get(host.get("hop", ""))
    if hop is None:
        return {"ok": False, "status": "NO-HOP", "info": f"hop {host.get('hop')!r} not in lab"}
    user, password = _ssh_user_pass(hop["creds"])
    # ARM serial beds carry the console on telnet_options.port (2323+); x86 net
    # beds have no telnet_options and use the in-guest shell on :23.
    port = host.get("telnet_options", {}).get("port", 23)
    remote_cmd = f"python3 -c {shlex.quote(_CONSOLE_PROBE)} {shlex.quote(host['ip'])} {port}"
    rc, out, err = _run_ssh(hop["ip"], user, password, remote_cmd)
    if rc != 0:
        return {"ok": False, "status": "HOP-FAIL", "info": err or f"rc={rc}"}
    if out.startswith("OK"):
        parts = out.split()
        ms = parts[1] if len(parts) > 1 else "?"
        uptime = f"up {int(ms) // 1000}s" if ms.isdigit() else "up ?"
        return {"ok": True, "status": "up", "info": uptime}
    if out.startswith("NOOUT"):
        return {"ok": False, "status": "WEDGED", "info": "TCP open, no shell output"}
    return {"ok": False, "status": "DOWN", "info": out[:40] or "no console"}


def _restart_qemu(hosts: list[dict], hops: dict[str, dict]) -> int:
    """Restart the QEMU + SNMP-relay units on every hop that fronts a guest."""
    # Select embedded guests by credential shape (no own ``creds`` — they
    # borrow the hop's), exactly like ``_is_ssh_host``/``_print_report``. Keying
    # on ``os_type == "embedded"`` here silently matched nothing once the lab
    # data moved to ``os_type: "zephyr"`` (commit 41cf70c) — the same trap
    # ``_is_ssh_host`` documents.
    hop_ids = sorted({h["hop"] for h in hosts if not _is_ssh_host(h) and h.get("hop")})
    if not hop_ids:
        print("No embedded guests with a hop in the lab; nothing to restart.")
        return 0
    failures = 0
    for hop_id in hop_ids:
        hop = hops.get(hop_id)
        if hop is None:
            print(f"  {hop_id}: not in lab — skipped")
            failures += 1
            continue
        user, password = _ssh_user_pass(hop["creds"])
        # `systemctl restart` accepts a unit glob, expanded against loaded
        # units. sudo -S reads the password from the piped echo.
        units = "'zephyr-qemu-*.service' 'zephyr-snmp-relay-*.service'"
        cmd = f"echo {shlex.quote(password)} | sudo -S systemctl restart {units}"
        rc, _out, err = _run_ssh(hop["ip"], user, password, cmd, timeout=60)
        if rc == 0:
            print(f"  {hop['element']} ({hop['ip']}): restarted QEMU + relay units")
        else:
            print(f"  {hop['element']} ({hop['ip']}): restart FAILED — {err or f'rc={rc}'}")
            failures += 1
    return failures


def _print_report(hosts: list[dict], hops: dict[str, dict]) -> bool:
    """Probe every host, print the table, and return True iff all are healthy."""
    local = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    print(f"Local reference clock: {local}Z\n")
    header = f"{'NE':<14}{'IP':<16}{'TYPE':<10}{'STATUS':<13}{'TIMESTAMP/UPTIME':<18}DRIFT"
    print(header)
    print("-" * len(header))

    all_ok = True
    drifts: list[tuple[str, float]] = []
    for host in hosts:
        ostype = host.get("os_type", "?")
        res = _check_unix(host) if _is_ssh_host(host) else _check_embedded(host, hops)
        all_ok = all_ok and res["ok"]

        drift_col = "—"
        if "drift" in res:
            drift_col = f"{res['drift']:+.2f}s"
            drifts.append((host["element"], res["drift"]))
        print(
            f"{host['element']:<14}{host['ip']:<16}{ostype:<10}"
            f"{res['status']:<13}{res['info']:<18}{drift_col}"
        )

    skewed = [(ne, d) for ne, d in drifts if abs(d) > _DRIFT_WARN_S]
    if skewed:
        print()
        print(
            f"⚠  clock drift > {_DRIFT_WARN_S:.0f}s on: "
            + ", ".join(f"{ne} ({d:+.2f}s)" for ne, d in skewed)
        )
    return all_ok


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--hosts",
        type=Path,
        default=DEFAULT_HOSTS,
        help=f"lab hosts JSON (default: {DEFAULT_HOSTS})",
    )
    parser.add_argument(
        "--restart-qemu",
        action="store_true",
        help="restart Zephyr QEMU + relay units on each hop, then health-check",
    )
    args = parser.parse_args(argv)

    if shutil.which("sshpass") is None:
        print("error: sshpass not found on PATH (needed for lab SSH).", file=sys.stderr)
        return 2
    if not args.hosts.exists():
        print(f"error: hosts file not found: {args.hosts}", file=sys.stderr)
        return 2

    hosts = _load_hosts(args.hosts)
    hops = _hop_index(hosts)

    if args.restart_qemu:
        print("Restarting Zephyr QEMU instances…")
        if _restart_qemu(hosts, hops):
            print("One or more restarts failed; health may be incomplete.\n")
        # Give the guests time to boot and the relays to re-peer before probing.
        time.sleep(10)
        print()

    return 0 if _print_report(hosts, hops) else 1


if __name__ == "__main__":
    sys.exit(main())
