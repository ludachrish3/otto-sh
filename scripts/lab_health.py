#!/usr/bin/env python3
"""Probe every lab VM + QEMU guest and report health + timestamps.

Reads the lab data (``tests/_fixtures/lab_data/tech1/lab.json`` by default) and, for
each defined host, reports reachability and a timestamp:

* **Unix VMs** (``os_type == "unix"``, no hop) are reached over SSH; the script
  reads each VM's wall clock and prints the **drift** against this machine's
  clock, so you can spot NTP/clock-skew problems at a glance.

* **Console guests behind a hop** are reached by SSHing to the hop and
  telnetting to the guest console (the same path otto uses). Two families
  qualify: the Zephyr instances (the ``EmbeddedHost`` family, no creds of their
  own) and the BusyBox bed guests (unix-family, own creds, but reachable only
  from inside their hop). Zephyr has no RTC, so these report **kernel uptime**
  + console responsiveness rather than wall-clock drift; a BusyBox guest
  answers its telnet ``login:`` prompt and is reported as such.

With ``--restart-qemu`` the script first restarts the ``zephyr-qemu-*``,
``zephyr-snmp-relay-*`` and ``busybox-qemu-*`` systemd units on each hop VM,
waits for the guests to boot, then runs the health check. Use it to recover a
wedged bed (e.g. after the embedded test gate reports "console wedged").

Exit status is non-zero if any host is unreachable/unresponsive.

Usage::

    scripts/lab_health.py
    scripts/lab_health.py --hosts tests/_fixtures/lab_data/tech1/lab.json
    scripts/lab_health.py --restart-qemu
"""

from __future__ import annotations

import argparse
import json
import re
import shlex
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_HOSTS = Path("tests/_fixtures/lab_data/tech1/lab.json")

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

# Boot settle after a restart: Zephyr guests are up in ~5 s, but the
# BusyBox guests boot a full Linux kernel under TCG on a 2-core VM and
# need ~15-30 s before telnetd answers.
_RESTART_SETTLE_S = 20

# SSH budget for the AUTHENTICATED console probe. Its four bounded reads (three
# login steps at `step` = 8 s, then uptime at 0.75 * step = 6 s) can spend 30 s
# before it gives up and prints "OK login", which overruns `_run_ssh`'s 25 s
# default — and an ssh timeout there reports HOP-FAIL for a guest that is
# merely slow to answer, i.e. the loudest possible verdict for the mildest
# possible fault. Raise both together if those budgets ever change. The
# credential-less call keeps the default; nothing about it got slower.
_LOGIN_PROBE_SSH_TIMEOUT_S = 45.0

# Runs on the hop VM (which has python3) to probe a guest telnet console: open
# the port, nudge the shell, and read what comes back. Prints one of
# "OK <ms>" (an uptime, in milliseconds — from a Zephyr "Uptime: N ms" line, or
# from /proc/uptime once the login branch below has a shell) / "OK login" (a
# telnet login prompt and nothing more: alive and answering, but this probe
# could not get past it) / "OK refused" (the console REFUSED the credentials
# the guest's own lab entry carries — see the login branch) / "OK ?" (talking,
# but neither shape) / "NOOUT" (TCP open but the guest emitted nothing — the
# classic wedge) / "CONNFAIL <err>".
#
# The port is argv[2], NOT hardcoded: the x86 net beds and the BusyBox guests
# expose the in-guest shell on :23 (reached over their TAPs), but the ARM serial
# beds bridge UART to a telnet listener on a loopback /32 at 2323+. A hardcoded
# :23 would connect to the hop's own 0.0.0.0:23 telnetd for those loopback
# addresses and report a false "up" — so honor telnet_options.port.
#
# argv[3]/argv[4] are an OPTIONAL user/password, and supplying them selects a
# different probe entirely: log in, then read /proc/uptime. Only the BusyBox
# bed guests get them, because only they carry creds of their own (see
# `_check_embedded`). The reason the login is worth the extra reads: the
# guests run under `Restart=always` — a guest kernel panic exits qemu with
# status 0 under -no-reboot, so systemd's only workable policy restarts it —
# and a freshly restarted guest serves the very same "<host> login: " banner as
# one that has been healthy for a day. Without an uptime, "OK login" cannot
# tell a stable bed from one silently panic-looping, which is the bed's actual
# failure mode. The one thing this branch must never print is an uptime it did
# not read.
#
# A login that does not reach a shell fails in two ways, and they are two
# findings, not one. A console that goes QUIET — no challenge, no answer, an
# EOF — keeps the "OK login" fallback: the unauthenticated sighting is still
# true, this probe simply could not measure further, and printing anything
# worse would turn a slow guest into a false alarm. A console that REFUSES the
# credentials is different in kind: those creds come from the guest's own
# committed lab entry, so a refusal is deterministic evidence that the image's
# /etc/shadow and the lab data disagree — a broken credential bake. That prints
# "OK refused" and `_check_embedded` renders it as a not-ok row, which is spec
# §6's requirement that a bad bake fail `vm-health` by name instead of
# surfacing later as the first bed test's login failure.
#
# The credential-less call is untouched, byte for byte. The Zephyr consoles
# reach this same script and have no login to offer; typing a username at that
# shell is the regression this arrangement exists to prevent.
_CONSOLE_PROBE = r"""
import re, socket, sys, time
ip = sys.argv[1]
port = int(sys.argv[2])
user, password = (sys.argv[3], sys.argv[4]) if len(sys.argv) > 4 else ("", "")
# Seconds each login step may wait. argv[5] is a TEST AFFORDANCE and nothing
# else: `_check_embedded` never passes it, so every real probe runs on the 8/6
# defaults that `_LOGIN_PROBE_SSH_TIMEOUT_S` is sized against. It exists because
# the branch worth testing -- a console that answers `login:` and then goes
# quiet -- is only reachable by letting a budget expire, and a suite that waits
# 8 real seconds to watch that happen pays it on every hostless gate and every
# CI interpreter. Shortening the budget keeps the observation exactly as it was
# (the console is held open and silent; the probe still times out) and only
# changes how long the test stares at it.
step = float(sys.argv[5]) if len(sys.argv) > 5 else 8.0
uptime_budget = step * 0.75
try:
    s = socket.create_connection((ip, port), timeout=4)
except Exception as e:
    print("CONNFAIL", e)
    raise SystemExit(0)
s.settimeout(4)

def read_until(needles, budget):
    # Read until one of *needles* (lowercase bytes) shows up in the accumulated
    # buffer, or *budget* seconds pass. Returns (needle_or_None, buffer). The
    # needles are ordered by the caller and checked in that order, so a refusal
    # marker is recognised ahead of a prompt character that may share a chunk
    # with it.
    end = time.time() + budget
    buf = b""
    while True:
        left = end - time.time()
        if left <= 0:
            return None, buf
        try:
            s.settimeout(left)
            chunk = s.recv(512)
        except Exception:
            return None, buf
        if not chunk:
            return None, buf
        buf += chunk
        low = buf.lower()
        for n in needles:
            if n in low:
                return n, buf

if user:
    hit, seen = read_until([b"login:"], step)
    if hit is None:
        print("OK ?" if seen else "NOOUT")
        raise SystemExit(0)
    s.sendall(user.encode() + b"\r\n")
    hit, _ = read_until([b"password:"], step)
    if hit is None:
        print("OK login")
        raise SystemExit(0)
    s.sendall(password.encode() + b"\r\n")
    # "incorrect" and a second "login:" are how a refusal announces itself, and
    # "#" is the ash root prompt. Three outcomes, three verdicts: a refusal is a
    # broken credential bake and says so; a timeout or an EOF (hit is None) is
    # an unmeasurable console and keeps the "OK login" fallback; only "#" is a
    # shell to read an uptime from. See the note above the script for why those
    # first two must not be collapsed.
    #
    # Listed refusal-first because read_until scans its needles in order, so a
    # buffer holding both resolves to the refusal. That precedence now picks a
    # VERDICT rather than merely stopping the probe, so what makes it safe is
    # worth stating: the guest images bake no /etc/motd and no /etc/issue
    # (`scripts/build_busybox_guest_images.py` writes passwd/shadow/group and
    # nothing else), so the only thing between the password and the "#" is
    # BusyBox's own ash banner, which contains neither needle.
    hit, _ = read_until([b"incorrect", b"login:", b"#"], step)
    if hit in (b"incorrect", b"login:"):
        print("OK refused")
        raise SystemExit(0)
    if hit != b"#":
        print("OK login")
        raise SystemExit(0)
    s.sendall(b"cat /proc/uptime\r\n")
    end = time.time() + uptime_budget
    up = None
    buf = b""
    while up is None:
        left = end - time.time()
        if left <= 0:
            break
        try:
            s.settimeout(left)
            chunk = s.recv(512)
        except Exception:
            break
        if not chunk:
            break
        buf += chunk
        # The whole /proc/uptime line, not a leading float: the command echo
        # and the prompt share this buffer, and "12.345 98.765" on a line of
        # its own is the only thing in it that is the file's contents.
        m = re.search(rb"(?m)^\s*(\d+\.\d+)\s+\d+\.\d+\s*$", buf)
        if m:
            up = round(float(m.group(1)) * 1000)
    try:
        s.sendall(b"exit\r\n")
    except Exception:
        pass
    s.close()
    print("OK", up if up is not None else "login")
    raise SystemExit(0)

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
    if m:
        print("OK", m.group(1).decode())
    elif b"login:" in data:
        print("OK login")
    else:
        print("OK", "?")
"""


def _load_hosts(path: Path) -> list[dict]:
    """Flatten a lab.json's ``elements`` into the flat host dicts this script reads.

    Since lab.json v2 a host entry lives under its element and no longer
    carries ``element``/``element_id`` itself — they are stamped back on here,
    which is exactly what otto's own loader does before host validation.
    Restated rather than imported, like :func:`_host_id`: this script
    deliberately runs with no otto on the path.
    """
    doc = json.loads(path.read_text())
    hosts: list[dict] = []
    for element in doc.get("elements", []):
        for host in element["hosts"]:
            flat = {**host, "element": element["name"]}
            if element.get("id") is not None:
                flat["element_id"] = element["id"]
            hosts.append(flat)
    return hosts


def _ssh_user_pass(creds: list[dict]) -> tuple[str, str]:
    """Pick the SSH login. Prefer ``vagrant``; otherwise the first cred entry."""
    by_login = {c["login"]: c["password"] for c in creds}
    if "vagrant" in by_login:
        return "vagrant", by_login["vagrant"]
    first = creds[0]
    return first["login"], first["password"]


def _run_ssh(
    ip: str, user: str, password: str, remote_cmd: str, timeout: float = 25.0
) -> tuple[int, str, str]:
    """Run ``remote_cmd`` on ``ip`` over password SSH. Returns (rc, out, err)."""
    cmd = ["sshpass", "-p", password, "ssh", *_SSH_OPTS, f"{user}@{ip}", remote_cmd]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)  # noqa: S603 — trusted args
        return p.returncode, p.stdout.strip(), p.stderr.strip()
    except subprocess.TimeoutExpired:
        return 124, "", f"ssh timed out after {timeout:.0f}s"


def _slug(value: str) -> str:
    """Lower-case, non-alphanumeric runs to ``-`` — otto's ``slug()``, restated."""
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")


def _host_id(host: dict) -> str:
    """Compose otto's host id from *host*'s element/element_id/board/slot fields.

    Mirrors :func:`otto.host.remote_host.make_host_id`; restated rather than
    imported because this script deliberately runs with no otto on the path.
    A host with no ``board`` — every Unix VM and every Zephyr guest since the
    board was dropped from the lab data — has the bare element (plus any
    ``element_id``) as its id, so defaulting the board to anything at all
    invents an id no host answers to.
    """
    element_id = host.get("element_id")
    element_id_str = "" if element_id is None else f"{element_id}"
    ne = f"{_slug(host['element'])}{element_id_str}"
    board = host.get("board")
    if board is None:
        return ne
    slot = host.get("slot")
    slot_str = "" if slot is None else f"{slot}"
    return f"{ne}_{_slug(board)}{slot_str}"


def _hop_index(hosts: list[dict]) -> dict[str, dict]:
    """Map an otto host id to its host entry, for hop lookups by ``hop`` value."""
    return {_host_id(h): h for h in hosts}


def _is_ssh_host(host: dict) -> bool:
    """Return True if we log in directly over SSH (the ``UnixHost`` family).

    Route on shape, not on ``os_type`` literals (see the history below).
    Two shapes are NOT direct-SSH: an embedded console (no ``creds`` of
    its own — it borrows the hop's), and a hop-fronted guest that carries
    its own creds but is only reachable from inside its hop (the BusyBox
    bed guests: ``ip`` is the hop's loopback, so a direct SSH from here
    would hit the wrong machine entirely). A host with creds and NO hop
    stays on the SSH path.

    The history the ``os_type`` half of that rule comes from: the SSH probe
    dereferences the host's own ``creds``, and keying the routing on
    ``os_type == "embedded"`` silently misrouted every console — straight into
    a ``KeyError('creds')`` — once the lab data moved to ``os_type: "zephyr"``
    (commit 41cf70c). Shape-based routing survives the next os_type rename;
    it is also why the BusyBox guests, which ARE ``os_type: "unix"``, need no
    new literal here — their hop is what routes them.
    """
    return "creds" in host and not host.get("hop")


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
    """Console responsiveness + uptime for a QEMU guest reached through its hop."""
    hop = hops.get(host.get("hop", ""))
    if hop is None:
        return {"ok": False, "status": "NO-HOP", "info": f"hop {host.get('hop')!r} not in lab"}
    user, password = _ssh_user_pass(hop["creds"])
    # ARM serial beds carry the console on telnet_options.port (2323+); x86 net
    # beds and the BusyBox guests have no telnet_options and use the in-guest
    # shell on :23, reached over their own TAP from the hop.
    port = host.get("telnet_options", {}).get("port", 23)
    remote_cmd = f"python3 -c {shlex.quote(_CONSOLE_PROBE)} {shlex.quote(host['ip'])} {port}"
    # A guest carrying creds of ITS OWN can be logged into for a real uptime;
    # that is the BusyBox bed's shape. A Zephyr console carries none (it
    # borrows its hop's, which are already in `user`/`password` above and are
    # the wrong credentials for the guest anyway), so it keeps the two-argument
    # call and the default budget — byte-identical to before this branch
    # existed. Read off the entry's shape, never an os_type literal, for the
    # same reason `_is_ssh_host` does.
    guest_login = ""
    if host.get("creds"):
        guest_user, guest_password = _ssh_user_pass(host["creds"])
        guest_login = guest_user
        remote_cmd += f" {shlex.quote(guest_user)} {shlex.quote(guest_password)}"
        rc, out, err = _run_ssh(
            hop["ip"], user, password, remote_cmd, timeout=_LOGIN_PROBE_SSH_TIMEOUT_S
        )
    else:
        rc, out, err = _run_ssh(hop["ip"], user, password, remote_cmd)
    if rc != 0:
        return {"ok": False, "status": "HOP-FAIL", "info": err or f"rc={rc}"}
    if out.startswith("OK"):
        parts = out.split()
        ms = parts[1] if len(parts) > 1 else "?"
        if ms == "refused":
            # The console answered, and it turned away the login committed in
            # this guest's OWN lab entry. Not-ok on purpose (spec §6): the row
            # names the guest and the credential, so a bad `/etc/shadow` bake
            # fails `vm-health` here rather than surfacing an hour later as the
            # first bed test's login failure. The probe prints this for the
            # refusal needles ONLY -- a console that merely goes quiet still
            # arrives as `login` below and is still reported healthy.
            return {
                "ok": False,
                "status": "BAD-CREDS",
                "info": f"{guest_login or '?'} login refused",
            }
        if ms == "login":
            return {"ok": True, "status": "up", "info": "login prompt"}
        uptime = f"up {int(ms) // 1000}s" if ms.isdigit() else "up ?"
        return {"ok": True, "status": "up", "info": uptime}
    if out.startswith("NOOUT"):
        return {"ok": False, "status": "WEDGED", "info": "TCP open, no shell output"}
    return {"ok": False, "status": "DOWN", "info": out[:40] or "no console"}


def _restart_qemu(hosts: list[dict], hops: dict[str, dict]) -> int:
    """Restart the QEMU + SNMP-relay units on every hop that fronts a guest."""
    # Select console guests by the SAME shape rule as ``_is_ssh_host`` /
    # ``_print_report`` (credentials plus hop), never by an ``os_type``
    # literal: keying on ``os_type == "embedded"`` here silently matched
    # nothing once the lab data moved to ``os_type: "zephyr"`` (commit
    # 41cf70c), and would today miss the BusyBox guests, which are
    # ``os_type: "unix"``.
    hop_ids = sorted({h["hop"] for h in hosts if not _is_ssh_host(h) and h.get("hop")})
    if not hop_ids:
        print("No console guests with a hop in the lab; nothing to restart.")
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
        # units. sudo -S reads the password from the piped echo. Every hop gets
        # every glob: on a hop with no busybox units the pattern matches
        # nothing and is a no-op, the same reason the zephyr globs are safe on
        # test1.
        units = "'zephyr-qemu-*.service' 'zephyr-snmp-relay-*.service' 'busybox-qemu-*.service'"
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
    """Parse arguments, probe lab hosts, print the health table, and return exit status."""
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
        help="restart Zephyr + BusyBox QEMU + relay units on each hop, then health-check",
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
        print("Restarting Zephyr + BusyBox QEMU instances…")
        if _restart_qemu(hosts, hops):
            print("One or more restarts failed; health may be incomplete.\n")
        # Give the guests time to boot and the relays to re-peer before probing.
        time.sleep(_RESTART_SETTLE_S)
        print()

    return 0 if _print_report(hosts, hops) else 1


if __name__ == "__main__":
    sys.exit(main())
