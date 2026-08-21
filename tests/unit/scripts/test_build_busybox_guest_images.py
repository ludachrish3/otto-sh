"""Unit guards for the BusyBox guest image builder.

Everything here runs unprivileged on the dev VM against a FAKE busybox
binary and a FAKE kernel module — the builder's file-shaping logic is
architecture-independent. The real artifacts are only ever consumed on
test1 at provision time, and the per-guest bed smoke (Phase B) is the
live verification.
"""

import gzip
import hashlib
import re
from pathlib import Path

import pytest

from scripts.build_busybox_guest_images import (
    GUEST_TABLE,
    NC_WINDOW,
    ROOT_SHADOW_HASH,
    build_initramfs_bytes,
    main,
)
from tests._fixtures.paths import PROJECT_ROOT


def _parse_newc(blob: bytes) -> dict[str, dict]:
    """Minimal independent newc reader: name -> {mode, rdev, data}."""
    out = {}
    off = 0
    while True:
        assert blob[off : off + 6] == b"070701"
        hdr = blob[off + 6 : off + 110].decode("ascii")
        fields = [int(hdr[i : i + 8], 16) for i in range(0, 104, 8)]
        namesize, filesize = fields[11], fields[6]
        mode, rdevmaj, rdevmin = fields[1], fields[9], fields[10]
        name_off = off + 110
        name = blob[name_off : name_off + namesize - 1].decode("ascii")
        data_off = name_off + namesize
        data_off += (4 - (data_off % 4)) % 4
        data = blob[data_off : data_off + filesize]
        off = data_off + filesize
        off += (4 - (off % 4)) % 4
        if name == "TRAILER!!!":
            return out
        out[name] = {"mode": mode, "rdev": (rdevmaj, rdevmin), "data": data}


@pytest.fixture
def image(tmp_path: Path) -> dict[str, dict]:
    fake_busybox = tmp_path / "busybox"
    fake_busybox.write_bytes(b"\x7fELF-fake-busybox")
    fake_ko = tmp_path / "e1000.ko"
    fake_ko.write_bytes(b"fake-module")
    blob = build_initramfs_bytes(fake_busybox, fake_ko, hostname="bb1161")
    return _parse_newc(gzip.decompress(blob))


def test_the_archive_carries_console_and_null_device_nodes(image):
    console = image["dev/console"]
    assert console["mode"] & 0o170000 == 0o020000  # S_IFCHR
    assert console["rdev"] == (5, 1)
    null = image["dev/null"]
    assert null["mode"] & 0o170000 == 0o020000
    assert null["rdev"] == (1, 3)


def test_init_is_a_symlink_dispatching_the_init_applet(image):
    init = image["init"]
    assert init["mode"] & 0o170000 == 0o120000  # S_IFLNK
    assert init["data"] == b"bin/busybox"


def test_rcs_mounts_proc_before_running_install(image):
    rcs = image["etc/init.d/rcS"]["data"].decode()
    assert rcs.index("mount -t proc") < rcs.index("--install -s /bin")
    assert image["etc/init.d/rcS"]["mode"] & 0o111  # executable


def test_rcs_brings_up_usernet_addressing_and_the_nic_module(image):
    rcs = image["etc/init.d/rcS"]["data"].decode()
    assert "insmod /lib/modules/e1000.ko" in rcs
    assert "ifconfig eth0 10.0.2.15 netmask 255.255.255.0 up" in rcs
    assert "route add default gw 10.0.2.2" in rcs
    assert "hostname bb1161" in rcs
    assert image["lib/modules/e1000.ko"]["data"] == b"fake-module"
    # insmod BEFORE the eth0 config, not merely somewhere in the file:
    # user-net's NIC does not exist as an interface until the module is
    # loaded, so the reversed order configures nothing and the guest comes
    # up unreachable — with an rcS that still contains every line above.
    assert rcs.index("insmod /lib/modules/e1000.ko") < rcs.index("ifconfig eth0")


def test_inittab_respawns_telnetd_with_an_explicit_login(image):
    inittab = image["etc/inittab"]["data"].decode()
    assert "::sysinit:/bin/busybox sh /etc/init.d/rcS" in inittab
    assert "::respawn:/bin/busybox telnetd -F -l /bin/login" in inittab


def test_root_login_is_md5_crypt_and_shell_is_ash(image):
    assert image["etc/passwd"]["data"] == b"root:x:0:0:root:/root:/bin/sh\n"
    shadow = image["etc/shadow"]["data"].decode()
    assert shadow.startswith(f"root:{ROOT_SHADOW_HASH}:")
    # Full MD5-crypt shape: $1$bb$ + exactly 22 crypt-base64 chars. This
    # REDS on the builder's REPLACE_WITH placeholder — a prefix check
    # alone would let the placeholder ship.
    assert re.fullmatch(r"\$1\$bb\$[./0-9A-Za-z]{22}", ROOT_SHADOW_HASH)
    # /etc/securetty must NOT exist: BusyBox login treats a missing file
    # as "root allowed on any tty", which is what telnet login needs.
    assert "etc/securetty" not in image


def test_the_guest_table_matches_the_pinned_bed_identities():
    # `element` is pinned alongside the ports because main() feeds it in as
    # the guest's hostname, and the lab data (Task 3) addresses the guests by
    # exactly these names — a typo here ships a bed whose hostnames and whose
    # host records disagree, which no port assertion can see.
    assert [(g.version, g.element, g.telnet_port, g.nc_base) for g in GUEST_TABLE] == [
        ("1.16.1", "bb1161", 2316, 9160),
        ("1.21.1", "bb1211", 2321, 9210),
        ("1.28.1", "bb1281", 2328, 9280),
        ("1.31.0", "bb1310", 2331, 9310),
        ("1.35.0", "bb1350", 2335, 9350),
    ]


# --- the Vagrantfile's hand-written copy of the same table -----------------
#
# Each pattern below demands that EVERY item in the shell list match the
# expected shape, so a malformed, extra or missing-quote entry fails the whole
# match rather than being quietly skipped by a findall over the file. The
# separator class allows the line continuations the table is wrapped with.
_PROVISIONER_TRIPLETS = re.compile(r'for entry in\s+((?:"\d+\.\d+\.\d+:\d+:\d+"[\s\\]*)+);\s*do')
_PROVISIONER_ENABLE_LIST = re.compile(r'for entry in\s+((?:"\d+\.\d+\.\d+"[\s\\]*)+);\s*do')
_PROVISIONER_NC_WINDOW = re.compile(
    r"p=\$ncbase[\s\\]+while\s+\[\s*\$p\s+-lt\s+\$\(\(\s*ncbase\s*\+\s*(\d+)\s*\)\)\s*\]"
)


def _busybox_provisioner_text() -> str:
    """The body of the Vagrantfile's ``provision_busybox_bed``, as committed."""
    text = (PROJECT_ROOT / "Vagrantfile").read_text()
    start = text.index("def provision_busybox_bed(vm)")
    return text[start : text.index("\n    end\n", start)]


def test_the_vagrantfile_provisioner_mirrors_the_guest_table():
    """The provisioner's identity table is a THIRD hand-written copy — pin it.

    ``GUEST_TABLE`` is mirrored by the lab data (guarded by
    ``tests/unit/host/test_busybox_bed_lab_entries.py``) and again, in shell,
    by the ``busybox-qemu`` provisioner: the ``version:telnet_port:nc_base``
    triplets it builds each guest's QEMU hostfwds from, and the version list
    the ``systemctl enable`` loop walks. That third copy is the one Chris
    provisions the bed FROM. A transposed digit there ships a bed whose ports
    disagree with lab.json and with the images' own hostnames while every
    hostless gate stays green — only a live bed would ever say so, and the
    live bed smoke arrives a phase later.

    The window width is pinned in the same breath: the wrapper's hostfwd loop
    forwards ``nc_base .. nc_base + N``, and otto's netstat port scan starts
    at ``nc_options.port`` and walks upward, so a window narrower than
    ``NC_WINDOW`` silently caps concurrent transfers instead of failing.
    """
    body = _busybox_provisioner_text()

    triplet_lists = _PROVISIONER_TRIPLETS.findall(body)
    assert len(triplet_lists) == 1, (
        "expected exactly one version:telnet:nc_base list in the busybox-qemu "
        f"provisioner, found {len(triplet_lists)} — the table moved, changed "
        "shape, or grew an entry that is not a well-formed triplet"
    )
    forwarded = [
        (version, int(telnet), int(nc_base))
        for version, telnet, nc_base in re.findall(
            r'"(\d+\.\d+\.\d+):(\d+):(\d+)"', triplet_lists[0]
        )
    ]
    assert forwarded == [(g.version, g.telnet_port, g.nc_base) for g in GUEST_TABLE], (
        "Vagrantfile busybox-qemu hostfwd table drifted from GUEST_TABLE in "
        "scripts/build_busybox_guest_images.py — the bed would forward ports "
        "the lab data does not address"
    )

    enable_lists = _PROVISIONER_ENABLE_LIST.findall(body)
    assert len(enable_lists) == 1, (
        f"expected exactly one systemctl-enable version list, found {len(enable_lists)}"
    )
    enabled = re.findall(r'"(\d+\.\d+\.\d+)"', enable_lists[0])
    assert enabled == [g.version for g in GUEST_TABLE], (
        "the enable/restart loop's version list drifted from GUEST_TABLE — a "
        "guest would be built and wrapped but never enabled (or vice versa)"
    )
    # Stated separately from the two comparisons above so the failure names
    # the real accident: the two shell lists disagreeing with each other.
    assert enabled == [version for version, _telnet, _nc_base in forwarded], (
        "the provisioner's own two copies disagree: enable list "
        f"{enabled} vs hostfwd table {[v for v, _t, _n in forwarded]}"
    )

    window = _PROVISIONER_NC_WINDOW.search(body)
    assert window is not None, (
        "could not find the nc hostfwd window loop (p=$ncbase; while [ $p -lt "
        "$((ncbase + N)) ]) in the busybox-qemu provisioner"
    )
    assert int(window.group(1)) == NC_WINDOW, (
        f"the provisioner forwards a {window.group(1)}-port nc window but the "
        f"builder documents NC_WINDOW={NC_WINDOW}; otto's port scan walks "
        "upward from nc_base, so the two must agree"
    )


def test_the_builder_imports_under_bare_python3(tmp_path):
    """The builder runs on test1's bare python3 — no uv env, no pytest.

    Differential: import the builder in a subprocess whose ``sys.path``
    holds ONLY the repo root and the stdlib — what
    ``python3 /vagrant/scripts/build_busybox_guest_images.py`` sees at
    provision time. Runs from ``tmp_path`` so the implicit cwd entry
    cannot stand in for the repo root. If the builder — or the fetch/pin
    layer it reaches through — ever grows a third-party import, this reds.

    Aimed at the BUILDER rather than at ``tests._fixtures.busybox``
    directly, and that is the finding, not a convenience: ``tests/__init__``
    imports pytest, so the pin layer is NOT importable by its own dotted
    name off a bare interpreter. The builder's stdlib-only package shim is
    what closes that gap, and pointing the differential here is what makes
    dropping the shim red.
    """
    import subprocess
    import sys

    repo_root = PROJECT_ROOT
    code = (
        "import sys; sys.path = [p for p in sys.path if 'site-packages' not in p]; "
        f"sys.path.insert(0, {str(repo_root)!r}); "
        "from scripts.build_busybox_guest_images import BUSYBOX_MATRIX; "
        "print(len(BUSYBOX_MATRIX))"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
        cwd=tmp_path,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "5"


def test_main_rebuilds_and_announces_only_what_the_stamps_say_changed(tmp_path, monkeypatch):
    """``--emit-changed`` is the provisioner's restart decision, so pin it.

    The Vagrantfile's ``busybox-qemu`` provisioner restarts a guest when — and
    only when — its version appears in this file (or its unit is already
    down). A builder that always announces "changed" therefore bounces all
    five guests on every ``vagrant provision``; one that never announces it
    leaves the OLD image running behind a freshly written file, which is the
    worse failure because the bed then disagrees with its own artifacts and
    nothing on the VM says so. Neither shape is visible to the per-image
    shaping tests above — they never call ``main``.

    Four runs against a fake artifact, one per branch of the skip predicate:
    cold, warm, stamp-corrupted, image-deleted.
    """
    dest = tmp_path / "bed"
    changed = tmp_path / "changed.txt"
    kernel_module = tmp_path / "e1000.ko"
    kernel_module.write_bytes(b"fake-module")

    def fake_busybox_binary(release):
        # Version-distinct bytes: a builder that fed one artifact to every
        # guest would still satisfy a same-bytes fake.
        artifact = tmp_path / f"busybox-{release.version}"
        artifact.write_bytes(f"\x7fELF-fake-{release.version}".encode())
        return artifact

    monkeypatch.setattr("scripts.build_busybox_guest_images.busybox_binary", fake_busybox_binary)

    def run() -> "list[str]":
        argv = [
            "--dest",
            str(dest),
            "--kernel-module",
            str(kernel_module),
            "--emit-changed",
            str(changed),
        ]
        assert main(argv) == 0
        return changed.read_text().splitlines()

    versions = [guest.version for guest in GUEST_TABLE]

    # Cold: every image built, every version announced.
    assert run() == versions
    built = {}
    for guest in GUEST_TABLE:
        image = dest / f"initramfs-{guest.version}.cpio.gz"
        stamp = dest / f"initramfs-{guest.version}.sha256"
        built[guest.version] = image.read_bytes()
        assert stamp.read_text().strip() == hashlib.sha256(image.read_bytes()).hexdigest()

    # Warm: nothing rebuilt, and the changed file is TRUNCATED rather than
    # left holding the previous run's list. The provisioner greps this file
    # on every run, so a stale one restarts five healthy guests.
    assert run() == []
    assert changed.read_text() == ""

    # A stamp that disagrees with its image rebuilds THAT version alone —
    # this is what makes the skip a content decision rather than a
    # "the file is there" decision.
    (dest / "initramfs-1.28.1.sha256").write_text("dead" * 16 + "\n")
    assert run() == ["1.28.1"]
    assert (dest / "initramfs-1.28.1.sha256").read_text().strip() == hashlib.sha256(
        built["1.28.1"]
    ).hexdigest()

    # A matching stamp is not sufficient on its own: a missing image must
    # rebuild too, or a half-wiped $BED leaves the unit pointed at an
    # initramfs that no longer exists and only the next guest boot finds out.
    (dest / "initramfs-1.31.0.cpio.gz").unlink()
    assert run() == ["1.31.0"]
    assert (dest / "initramfs-1.31.0.cpio.gz").read_bytes() == built["1.31.0"]
