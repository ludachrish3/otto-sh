#!/usr/bin/env python3
"""Build the BusyBox bed guest initramfs images (spec 2026-08-20 §3).

Runs on test1 at `vagrant provision` time under bare python3 (stdlib
only), consuming the artifact tier's own fetch/pin/verify layer
(tests/_fixtures/busybox.py + busybox_pins.json) so the guest images are
built from exactly the bytes the retained CI lane certifies. The cpio
archive is written in pure Python (newc format) so /dev/console and
/dev/null can be encoded as character devices without root or mknod —
the kernel opens /dev/console from the initramfs BEFORE /init runs, and
CONFIG_DEVTMPFS_MOUNT does not apply to initramfs boots.

Usage (the Vagrantfile's busybox-qemu provisioner is the caller):

    python3 scripts/build_busybox_guest_images.py \
        --dest /home/vagrant/busybox-bed \
        --kernel-module /home/vagrant/busybox-bed/e1000.ko \
        --emit-changed /home/vagrant/busybox-bed/changed.txt
"""

import argparse
import gzip
import hashlib
import sys
import types
from dataclasses import dataclass
from importlib.machinery import ModuleSpec
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# `tests/__init__.py` imports pytest (it sets a package-level asyncio
# pytestmark), so `import tests._fixtures.busybox` executes a third-party
# import that test1's bare python3 does not have — the fetch/pin layer is
# not reachable by its own dotted name off a plain interpreter. Standing a
# stdlib-only stub for the PACKAGE in sys.modules keeps `tests/__init__.py`
# from running while `tests._fixtures.busybox` and the `tests._ambient_env`
# it imports load normally from their own files.
#
# `setdefault`, never an unconditional assignment: inside a pytest session
# the real package is already imported, and this must not displace it.
# Pinned by `test_the_builder_imports_under_bare_python3`, which imports
# this module on a sys.path stripped of site-packages — drop the stub and
# that test reds with the same ModuleNotFoundError test1 would print.
_tests_pkg = types.ModuleType("tests")
_tests_pkg.__path__ = [str(REPO_ROOT / "tests")]
_tests_pkg.__spec__ = ModuleSpec("tests", loader=None, is_package=True)
_tests_pkg.__spec__.submodule_search_locations = _tests_pkg.__path__
sys.modules.setdefault("tests", _tests_pkg)

from tests._fixtures.busybox import BUSYBOX_MATRIX, busybox_binary  # noqa: E402

# Generated once with `openssl passwd -1 -salt bb otto` and pinned —
# deterministic, and BusyBox login supports $1$ (MD5-crypt) on every
# version in the matrix. The password is `otto`, recorded in the guests'
# lab-data creds.
ROOT_SHADOW_HASH = "$1$bb$atR8Vd4A2Wtv6xqEg8BFw1"


@dataclass(frozen=True)
class Guest:
    """One bed guest: a pinned BusyBox version and its fixed ports."""

    version: str
    element: str
    telnet_port: int
    nc_base: int


# The bed identity table (spec §2/§4). Ports are version-encoded:
# telnet 23xx and nc 9xx0 windows derive from the version digits.
GUEST_TABLE = [
    Guest("1.16.1", "bb1161", 2316, 9160),
    Guest("1.21.1", "bb1211", 2321, 9210),
    Guest("1.28.1", "bb1281", 2328, 9280),
    Guest("1.31.0", "bb1310", 2331, 9310),
    Guest("1.35.0", "bb1350", 2335, 9350),
]

NC_WINDOW = 10  # forwarded data ports per guest: nc_base .. nc_base+9


@dataclass(frozen=True)
class CpioEntry:
    """One newc archive member. rdev only matters for device nodes."""

    name: str
    mode: int
    data: bytes = b""
    rdev: "tuple[int, int]" = (0, 0)


def _newc_header(e: CpioEntry, ino: int) -> bytes:
    """Render *e*'s 110-byte newc header plus its NUL-terminated name."""
    name = e.name.encode("ascii") + b"\0"
    fields = [
        ino,
        e.mode,
        0,
        0,
        1,
        0,
        len(e.data),
        0,
        0,
        e.rdev[0],
        e.rdev[1],
        len(name),
        0,
    ]
    return b"070701" + b"".join(b"%08X" % f for f in fields) + name


def _pad4(buf: bytearray) -> None:
    """Pad *buf* to the next 4-byte boundary — newc aligns every field."""
    buf.extend(b"\0" * ((4 - len(buf) % 4) % 4))


def cpio_newc(entries: "list[CpioEntry]") -> bytes:
    """Serialize *entries* as a newc cpio archive (what `-H newc` writes)."""
    buf = bytearray()
    for ino, e in enumerate(entries, start=1):
        buf.extend(_newc_header(e, ino))
        _pad4(buf)
        buf.extend(e.data)
        _pad4(buf)
    buf.extend(_newc_header(CpioEntry("TRAILER!!!", 0), 0))
    _pad4(buf)
    return bytes(buf)


def _rcs(hostname: str) -> str:
    """Render the guest's /etc/init.d/rcS: mounts, install, networking."""
    # /proc BEFORE --install: BusyBox resolves its own path through
    # /proc/self/exe, and 1.16.1 measured DANGLING links without it
    # (CI issues #227/#228). Everything runs via the /bin/busybox
    # applet-prefix spelling until the install has run.
    return f"""#!/bin/sh
/bin/busybox mount -t proc proc /proc
/bin/busybox mount -t sysfs sysfs /sys
/bin/busybox mount -t devtmpfs devtmpfs /dev 2>/dev/null
/bin/busybox mkdir -p /dev/pts
/bin/busybox mount -t devpts devpts /dev/pts
/bin/busybox --install -s /bin
[ -x /bin/sh ] || for a in sh login getty ifconfig route insmod hostname netstat nc; do
    /bin/busybox ln -sf busybox /bin/$a
done
/bin/busybox insmod /lib/modules/e1000.ko
/bin/busybox ifconfig lo 127.0.0.1 netmask 255.0.0.0 up
/bin/busybox ifconfig eth0 10.0.2.15 netmask 255.255.255.0 up
/bin/busybox route add default gw 10.0.2.2
/bin/busybox hostname {hostname}
"""


_INITTAB = """::sysinit:/bin/busybox sh /etc/init.d/rcS
::respawn:/bin/busybox telnetd -F -l /bin/login
::restart:/bin/busybox init
::ctrlaltdel:/bin/busybox reboot
"""


def cpio_newc_entries(busybox: Path, kernel_module: Path, hostname: str) -> "list[CpioEntry]":
    """Build the full member list for one guest image.

    No /etc/motd or /etc/issue is baked, and scripts/lab_health.py's
    login-probe needle precedence RELIES on that: a pre-login banner could
    carry a needle substring and change a probe verdict. Adding one means
    re-checking that probe's needle handling first.
    """
    dirs = [
        "bin",
        "dev",
        "dev/pts",
        "etc",
        "etc/init.d",
        "lib",
        "lib/modules",
        "proc",
        "root",
        "sys",
        "tmp",
    ]
    entries = [CpioEntry(d, 0o040755) for d in dirs]
    entries += [
        CpioEntry("bin/busybox", 0o100755, busybox.read_bytes()),
        CpioEntry("init", 0o120777, b"bin/busybox"),
        CpioEntry("dev/console", 0o020600, rdev=(5, 1)),
        CpioEntry("dev/null", 0o020666, rdev=(1, 3)),
        CpioEntry("etc/inittab", 0o100644, _INITTAB.encode()),
        CpioEntry("etc/init.d/rcS", 0o100755, _rcs(hostname).encode()),
        CpioEntry("etc/passwd", 0o100644, b"root:x:0:0:root:/root:/bin/sh\n"),
        CpioEntry("etc/shadow", 0o100600, f"root:{ROOT_SHADOW_HASH}:0:0:99999:7:::\n".encode()),
        CpioEntry("etc/group", 0o100644, b"root:x:0:\n"),
        CpioEntry("lib/modules/e1000.ko", 0o100644, kernel_module.read_bytes()),
    ]
    return entries


def build_initramfs_bytes(busybox: Path, kernel_module: Path, hostname: str) -> bytes:
    """One guest's gzipped initramfs, byte-deterministic for stamping."""
    archive = cpio_newc(cpio_newc_entries(busybox, kernel_module, hostname))
    return gzip.compress(archive, mtime=0)


def main(argv: "list[str] | None" = None) -> int:
    """Build every guest image into --dest, rebuilding only what changed."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dest", type=Path, required=True)
    parser.add_argument("--kernel-module", type=Path, required=True)
    parser.add_argument("--emit-changed", type=Path, default=None)
    parser.add_argument("--only", default=None, help="build a single version")
    args = parser.parse_args(argv)

    args.dest.mkdir(parents=True, exist_ok=True)
    by_version = {r.version: r for r in BUSYBOX_MATRIX}
    changed: "list[str]" = []
    for guest in GUEST_TABLE:
        if args.only and guest.version != args.only:
            continue
        release = by_version[guest.version]
        binary = busybox_binary(release)  # fetch + sha-pin verify (cached)
        blob = build_initramfs_bytes(binary, args.kernel_module, guest.element)
        digest = hashlib.sha256(blob).hexdigest()
        image = args.dest / f"initramfs-{guest.version}.cpio.gz"
        stamp = args.dest / f"initramfs-{guest.version}.sha256"
        if stamp.exists() and stamp.read_text().strip() == digest and image.exists():
            print(f"{guest.version}: unchanged")
            continue
        tmp = image.with_suffix(".gz.tmp")
        tmp.write_bytes(blob)
        tmp.replace(image)
        stamp.write_text(digest + "\n")
        changed.append(guest.version)
        print(f"{guest.version}: rebuilt ({digest[:12]})")
    if args.emit_changed is not None:
        args.emit_changed.write_text("".join(v + "\n" for v in changed))
    return 0


if __name__ == "__main__":
    sys.exit(main())
