#!/usr/bin/env python3
"""Render the chaos reboot guest's seed, QEMU wrapper and systemd unit.

The Vagrantfile's chaos-guest provisioner calls this on test4 under the
bare system python3, so it is stdlib-only (no otto, pydantic or yaml
imports). ``CHAOS_GUEST`` is the ONE place the guest's identity lives:
every render function below reads it from there. The cloud-config files
are JSON under a ``#cloud-config`` header, which is valid YAML.

Its mirrors are pinned by three guards: the test file
``tests/unit/scripts/test_render_chaos_guest_seed.py``,
``tests/unit/host/test_chaos_guest_lab_entries.py`` and the lab data in
``tests/_fixtures/lab_data/tech1/lab.json``.

Usage (exactly one mode per call):

    python3 scripts/render_chaos_guest_seed.py --dest DIR
    python3 scripts/render_chaos_guest_seed.py --image-pin ARCH
    python3 scripts/render_chaos_guest_seed.py --wrapper ARCH
    python3 scripts/render_chaos_guest_seed.py --unit
"""

import argparse
import json
import shlex
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class GuestUser:
    """One guest login; ``sudo`` grants NOPASSWD:ALL."""

    login: str
    password: str
    sudo: bool  # NOPASSWD:ALL when True


@dataclass(frozen=True)
class ImagePin:
    """One pinned cloud image: arch, release, file name, SHA-256 and URL."""

    arch: str  # "arm64" | "amd64" (dpkg names)
    release: str
    filename: str
    sha256: str
    url: str


@dataclass(frozen=True)
class Nic:
    """One guest NIC; the tap/IP fields are None for the user-mode NIC."""

    netdev: str  # guest-side name: eth0/eth1/eth2
    mac: str
    tap: str | None  # None for the user-mode NIC
    guest_ip: str | None
    host_ip: str | None
    subnet: str | None


@dataclass(frozen=True)
class ChaosGuest:
    """The chaos guest's whole identity; every render function reads this."""

    element: str
    host_id: str
    hop: str
    lab: str
    unit: str
    home: str
    wrapper: str
    nat: Nic
    mgmt: Nic
    data: Nic
    users: tuple[GuestUser, ...]
    images: tuple[ImagePin, ...]
    instance_id: str  # bump by hand to force a re-seed
    hold_unit: str
    hold_file: str
    hold_timeout_stop_s: int
    reboot_job_timeout_s: int
    memory_mb: int
    cpus: int
    disk_gb: int
    packages: tuple[str, ...]


_IMAGE_BASE_URL = "https://cloud-images.ubuntu.com/releases/noble"
# Pinned 2026-11-10 from release-20261017/SHA256SUMS (same file at
# test4:/home/vagrant/chaos-guest/SHA256SUMS).
_RELEASE = "20261017"


def _pin(arch: str, sha256: str) -> ImagePin:
    filename = f"ubuntu-24.04-server-cloudimg-{arch}.img"
    return ImagePin(
        arch=arch,
        release=_RELEASE,
        filename=filename,
        sha256=sha256,
        url=f"{_IMAGE_BASE_URL}/release-{_RELEASE}/{filename}",
    )


CHAOS_GUEST = ChaosGuest(
    element="ubu2404",
    host_id="ubu2404-qemu",
    hop="test4",
    lab="chaos",
    unit="chaos-qemu-ubu2404.service",
    home="/home/vagrant/chaos-guest",
    wrapper="/home/vagrant/run-chaos-qemu-ubu2404.sh",
    nat=Nic("eth0", "52:54:00:c3:04:00", None, None, None, None),
    mgmt=Nic(
        "eth1", "52:54:00:c3:04:01", "ubeth-mgmt", "203.0.113.1", "203.0.113.2", "203.0.113.0/30"
    ),
    data=Nic(
        "eth2", "52:54:00:c3:04:02", "ubeth-data", "203.0.113.5", "203.0.113.6", "203.0.113.4/30"
    ),
    users=(GuestUser("vagrant", "vagrant", True), GuestUser("test", "Password1", False)),
    images=(
        _pin("arm64", "50e24e1fb79e89116e939d90aa7738f7af48c442fa1bc2510131e0b39c50348e"),
        _pin("amd64", "04e8f2461a16a44e78a370284471f29616cdd9baf1893df25807b71f985b6c94"),
    ),
    instance_id="ubu2404-chaos-1",
    hold_unit="chaos-shutdown-hold.service",
    hold_file="/run/chaos/shutdown_hold",
    hold_timeout_stop_s=600,
    reboot_job_timeout_s=90,
    memory_mb=1024,
    cpus=2,
    disk_gb=4,
    packages=("socat",),
)

ARCHES = ("arm64", "amd64")

_HOLD_SCRIPT = "/usr/local/sbin/chaos-shutdown-hold"
_UNIT_DIR = "/etc/systemd/system"
_MACHINES = {
    "arm64": ("qemu-system-aarch64", "virt", "cortex-a72"),
    "amd64": ("qemu-system-x86_64", "q35", "max"),
}
_AAVMF_CODE = "/usr/share/AAVMF/AAVMF_CODE.fd"


def image_pin(arch: str) -> ImagePin:
    """Return the pinned cloud image for ``arch``; exit 2 on an unknown arch."""
    for pin in CHAOS_GUEST.images:
        if pin.arch == arch:
            return pin
    print(f"unknown arch {arch!r}; known arches: {', '.join(ARCHES)}", file=sys.stderr)
    raise SystemExit(2)


def hold_unit_text() -> str:
    """Return the systemd unit that holds shutdown for the seconds in the hold file."""
    g = CHAOS_GUEST
    return (
        "[Unit]\n"
        f"Description=otto chaos: hold shutdown for the seconds in {g.hold_file}\n"
        "After=systemd-user-sessions.service ssh.service ssh.socket\n"
        "\n"
        "[Service]\n"
        "Type=oneshot\n"
        "RemainAfterExit=yes\n"
        f"ExecStart=/bin/mkdir -p {Path(g.hold_file).parent}\n"
        f"ExecStop={_HOLD_SCRIPT}\n"
        f"TimeoutStopSec={g.hold_timeout_stop_s}\n"
        "\n"
        "[Install]\n"
        "WantedBy=multi-user.target\n"
    )


def hold_script_text() -> str:
    """Return the ExecStop script that sleeps for the hold file's seconds."""
    hold = CHAOS_GUEST.hold_file
    return f'#!/bin/sh\nif [ -e {hold} ]; then\n  sleep "$(cat {hold})"\nfi\nexit 0\n'


def reboot_target_override_text() -> str:
    """Return the reboot.target drop-in that force-reboots after a job timeout."""
    return (
        f"[Unit]\nJobTimeoutSec={CHAOS_GUEST.reboot_job_timeout_s}\nJobTimeoutAction=reboot-force\n"
    )


def render_user_data() -> str:
    """Render cloud-init user-data (JSON under a #cloud-config header)."""
    g = CHAOS_GUEST
    users = []
    for u in g.users:
        entry: dict = {
            "name": u.login,
            "plain_text_passwd": u.password,
            "lock_passwd": False,
            "shell": "/bin/bash",
        }
        if u.sudo:
            entry["sudo"] = "ALL=(ALL) NOPASSWD:ALL"
        users.append(entry)
    cfg = {
        "hostname": g.element,
        "users": users,
        "ssh_pwauth": True,
        "packages": list(g.packages),
        "write_files": [
            {"path": f"{_UNIT_DIR}/{g.hold_unit}", "content": hold_unit_text()},
            {"path": _HOLD_SCRIPT, "permissions": "0755", "content": hold_script_text()},
            {
                "path": f"{_UNIT_DIR}/reboot.target.d/otto.conf",
                "content": reboot_target_override_text(),
            },
        ],
        "runcmd": ["systemctl daemon-reload", f"systemctl enable --now {g.hold_unit}"],
    }
    return "#cloud-config\n" + json.dumps(cfg, indent=2) + "\n"


def render_meta_data() -> str:
    """Render cloud-init meta-data, pinning the instance id."""
    g = CHAOS_GUEST
    return f"instance-id: {g.instance_id}\nlocal-hostname: {g.element}\n"


def render_network_config() -> str:
    """Render the netplan v2 network-config (JSON)."""
    g = CHAOS_GUEST
    ethernets: dict = {
        g.nat.netdev: {
            "match": {"macaddress": g.nat.mac},
            "set-name": g.nat.netdev,
            "dhcp4": True,
        }
    }
    for nic in (g.mgmt, g.data):
        ethernets[nic.netdev] = {
            "match": {"macaddress": nic.mac},
            "set-name": nic.netdev,
            "addresses": [f"{nic.guest_ip}/30"],
        }
    return json.dumps({"version": 2, "ethernets": ethernets}, indent=2) + "\n"


def qemu_argv(arch: str) -> list[str]:
    """Build the QEMU command line; the arch picks only binary, machine, cpu and pflash."""
    g = CHAOS_GUEST
    binary, machine, cpu = _MACHINES[arch]
    argv = [binary, "-M", machine, "-cpu", cpu, "-accel", "tcg,thread=multi"]
    argv += ["-smp", str(g.cpus), "-m", str(g.memory_mb), "-display", "none"]
    argv += ["-serial", f"file:{g.home}/console.log"]
    argv += ["-monitor", f"unix:{g.home}/monitor.sock,server,nowait"]
    if arch == "arm64":
        argv += ["-drive", f"if=pflash,format=raw,readonly=on,file={_AAVMF_CODE}"]
    argv += ["-drive", f"if=virtio,format=qcow2,file={g.home}/{g.element}.qcow2"]
    argv += ["-drive", f"if=virtio,format=raw,readonly=on,file={g.home}/seed.iso"]
    argv += ["-nic", f"user,model=virtio-net-pci,mac={g.nat.mac}"]
    for nic in (g.mgmt, g.data):
        argv += [
            "-nic",
            f"tap,model=virtio-net-pci,script=no,downscript=no,ifname={nic.tap},mac={nic.mac}",
        ]
    return argv


def render_wrapper(arch: str) -> str:
    """Render the bash wrapper that execs the QEMU command for ``arch``."""
    return (
        "#!/usr/bin/env bash\n"
        "# There is no -no-reboot: a kernel reset is the reboot under test, and QEMU\n"
        "# resets the machine in place. The firmware has no variable store because\n"
        "# there is nothing to lose: the removable-media path boots the overlay every\n"
        "# time. Restart=always in the unit still covers a QEMU process that dies.\n"
        "set -euo pipefail\n"
        f"exec {shlex.join(qemu_argv(arch))}\n"
    )


def render_unit() -> str:
    """Render the systemd unit that runs the wrapper and owns the TAPs."""
    g = CHAOS_GUEST
    taps = (g.mgmt, g.data)
    lines = [
        "[Unit]",
        (
            f"Description=otto chaos guest {g.element} under QEMU "
            f"(ssh {g.mgmt.guest_ip} via {g.mgmt.tap}, data {g.data.guest_ip} via {g.data.tap})"
        ),
        "After=network.target",
        "",
        "[Service]",
        "Type=simple",
        "User=vagrant",
    ]
    lines.extend(
        f"ExecStartPre=+/bin/sh -c 'ip link del {nic.tap} 2>/dev/null; "
        f"ip tuntap add {nic.tap} mode tap user vagrant && ip link set {nic.tap} up "
        f"&& ip addr add {nic.host_ip}/30 dev {nic.tap}'"
        for nic in taps
    )
    lines.append(f"ExecStart={g.wrapper}")
    lines.extend(
        f"ExecStopPost=+/bin/sh -c 'ip link set {nic.tap} down 2>/dev/null; "
        f"ip tuntap del {nic.tap} mode tap 2>/dev/null; true'"
        for nic in taps
    )
    lines += ["Restart=always", "RestartSec=2", "", "[Install]", "WantedBy=multi-user.target", ""]
    return "\n".join(lines)


def _write_atomic(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text)
    tmp.replace(path)


def main(argv: list[str] | None = None) -> int:
    """Run exactly one render mode; argparse exits 2 on any other call."""
    parser = argparse.ArgumentParser(description="Render the chaos guest's seed, wrapper, unit.")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dest", type=Path, help="write user-data, meta-data, network-config here")
    mode.add_argument("--image-pin", metavar="ARCH", help="print '<url> <sha256> <filename>'")
    mode.add_argument("--wrapper", metavar="ARCH", help="print the QEMU wrapper script")
    mode.add_argument("--unit", action="store_true", help="print the systemd unit")
    args = parser.parse_args(argv)
    if args.dest is not None:
        args.dest.mkdir(parents=True, exist_ok=True)
        _write_atomic(args.dest / "user-data", render_user_data())
        _write_atomic(args.dest / "meta-data", render_meta_data())
        _write_atomic(args.dest / "network-config", render_network_config())
    elif args.image_pin is not None:
        pin = image_pin(args.image_pin)
        print(pin.url, pin.sha256, pin.filename)
    elif args.wrapper is not None:
        image_pin(args.wrapper)  # validates the arch, exit 2 on unknown
        sys.stdout.write(render_wrapper(args.wrapper))
    else:
        sys.stdout.write(render_unit())
    return 0


if __name__ == "__main__":
    sys.exit(main())
