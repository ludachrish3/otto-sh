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


_ANCHOR = GUEST_TABLE[0]
"""The guest the per-image shaping tests are read off — bb1161, by identity.

Taken from the table rather than spelled out so the shaping assertions below
cannot drift from the bed's own first row; the table itself is pinned by
``test_the_guest_table_matches_the_pinned_bed_identities``.
"""


def _build(tmp_path: Path, guest) -> dict[str, dict]:
    fake_busybox = tmp_path / f"busybox-{guest.version}"
    fake_busybox.write_bytes(b"\x7fELF-fake-busybox")
    fake_ko = tmp_path / "e1000.ko"
    fake_ko.write_bytes(b"fake-module")
    blob = build_initramfs_bytes(fake_busybox, fake_ko, guest.element, guest.ip)
    return _parse_newc(gzip.decompress(blob))


@pytest.fixture
def image(tmp_path: Path) -> dict[str, dict]:
    return _build(tmp_path, _ANCHOR)


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


def test_rcs_addresses_the_guests_own_slash_30_and_loads_the_nic_module(image):
    rcs = image["etc/init.d/rcS"]["data"].decode()
    assert "insmod /lib/modules/e1000.ko" in rcs
    assert f"ifconfig eth0 {_ANCHOR.ip} netmask 255.255.255.252 up" in rcs
    assert f"hostname {_ANCHOR.element}" in rcs
    assert image["lib/modules/e1000.ko"]["data"] == b"fake-module"
    # insmod BEFORE the eth0 config, not merely somewhere in the file: eth0
    # does not exist as an interface until the module is loaded, so the
    # reversed order configures nothing and the guest comes up unreachable —
    # with an rcS that still contains every line above.
    assert rcs.index("insmod /lib/modules/e1000.ko") < rcs.index("ifconfig eth0")


def test_rcs_configures_no_default_route(image):
    """No gateway, and that is the guests' egress story — assert it.

    The /30 peer is on-link, so a default route would have nothing to do; the
    reason to pin its ABSENCE is what the previous arrangement did. Under QEMU
    user-mode networking the guests took a default route to slirp's
    ``10.0.2.2``, which NATs through test1, which has internet — so every guest
    could reach the outside world. ``docs/architecture/subsystems/busybox-bed.md`` now tells
    operators they cannot. Re-adding a gateway line here would make that
    sentence false again silently: nothing else in the suite looks at egress,
    and a guest with a route still passes every reachability test there is.
    """
    rcs = image["etc/init.d/rcS"]["data"].decode()
    assert "route add default" not in rcs, (
        f"the guest rcS configures a default route — the bed's isolation claim "
        f"in docs/architecture/subsystems/busybox-bed.md is that it does not:\n{rcs}"
    )
    assert "10.0.2." not in rcs, (
        f"a QEMU user-mode (slirp) address survives in the guest rcS; these "
        f"guests are on real TAPs now:\n{rcs}"
    )


def test_every_guest_image_carries_its_own_address(tmp_path: Path):
    """Five images, five distinct addresses — the per-guest fact is per-guest.

    The bug this exists for shipped once already, in the arrangement this
    replaced: ``_rcs`` hardcoded ONE address (``10.0.2.15``) for all five,
    which was harmless only because slirp gave every guest its own private
    10.0.2.0/24 and the host never saw those addresses. On real TAPs the same
    shape is fatal and silent in the same way — the five images build, the five
    units start, and four guests answer ARP for an address that is not theirs.
    A single-guest assertion cannot see it: the anchor row above passes against
    a builder that ignores its ``ip`` argument entirely as long as bb1161's
    address is the constant. This one compares across the table.
    """
    seen = {}
    for guest in GUEST_TABLE:
        rcs = _build(tmp_path, guest)["etc/init.d/rcS"]["data"].decode()
        assert f"ifconfig eth0 {guest.ip} netmask 255.255.255.252 up" in rcs, (
            f"{guest.element}'s rcS does not configure {guest.ip}:\n{rcs}"
        )
        assert f"hostname {guest.element}" in rcs
        seen[guest.element] = guest.ip
    assert len(set(seen.values())) == len(GUEST_TABLE), f"two bed guests share an address: {seen}"


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
    # `element` is pinned alongside the addresses because main() feeds it in as
    # the guest's hostname, and the lab data addresses the guests by exactly
    # these names — a typo here ships a bed whose hostnames and whose host
    # records disagree, which no address assertion can see.
    #
    # The /30 arithmetic (guest = 4n+1, tap = 4n+2) is spelled out literally
    # rather than computed: a generated expectation would agree with a
    # generator that had the same off-by-one as the table.
    assert [(g.version, g.element, g.ip, g.host_ip, g.tap) for g in GUEST_TABLE] == [
        ("1.16.1", "bb1161", "198.51.100.1", "198.51.100.2", "bbeth-1161"),
        ("1.21.1", "bb1211", "198.51.100.5", "198.51.100.6", "bbeth-1211"),
        ("1.28.1", "bb1281", "198.51.100.9", "198.51.100.10", "bbeth-1281"),
        ("1.31.0", "bb1310", "198.51.100.13", "198.51.100.14", "bbeth-1310"),
        ("1.35.0", "bb1350", "198.51.100.17", "198.51.100.18", "bbeth-1350"),
    ]


def test_the_guest_slash_30s_do_not_collide_with_the_zephyr_beds():
    """TEST-NET-2 here, because TEST-NET-1 is already spoken for on a hop.

    The Zephyr instances take 192.0.2.x/30s on their own TAPs (see the
    ``zeth-*`` wrappers in the Vagrantfile). Both beds are QEMU-on-a-lab-VM
    with host-side addresses in the hop's routing table, so an overlapping
    block would be a routing accident that only shows up live. Documentation
    blocks are the cheap way to make the collision impossible, and picking a
    DIFFERENT one is the whole point — so pin the block, not just the offsets.
    """
    for guest in GUEST_TABLE:
        for address in (guest.ip, guest.host_ip):
            assert address.startswith("198.51.100."), (
                f"{guest.element} is addressed outside TEST-NET-2 at {address} — "
                "192.0.2.0/24 belongs to the Zephyr beds"
            )
        octet = int(guest.ip.rsplit(".", 1)[1])
        assert octet % 4 == 1, f"{guest.element}'s address {guest.ip} is not a /30 host"
        assert guest.host_ip == f"198.51.100.{octet + 1}", (
            f"{guest.element}'s tap address {guest.host_ip} is not the peer of {guest.ip}"
        )


# --- the Vagrantfile's hand-written copy of the same table -----------------
#
# Each pattern below demands that EVERY item in the shell list match the
# expected shape, so a malformed, extra or missing-quote entry fails the whole
# match rather than being quietly skipped by a findall over the file. The
# separator class allows the line continuations the table is wrapped with.
_QUAD = r'"\d+\.\d+\.\d+:\d+\.\d+\.\d+\.\d+:\d+\.\d+\.\d+\.\d+:[a-z0-9-]+"'
_PROVISIONER_QUADS = re.compile(rf"for entry in\s+((?:{_QUAD}[\s\\]*)+);\s*do")
_PROVISIONER_ENABLE_LIST = re.compile(r'for entry in\s+((?:"\d+\.\d+\.\d+"[\s\\]*)+);\s*do')

# The three templates that consume the table. Spelled against the loop
# VARIABLES (``${tap}``, ``${hip}``) rather than against any address, because
# that is the property under test: the table is the one authority and the
# wrapper/unit must read the row rather than carry a hardcoded copy of it.
_PROVISIONER_NIC = re.compile(r"-nic tap,model=e1000,script=no,downscript=no,ifname=\$\{tap\}")
_PROVISIONER_TAP_UP = re.compile(
    r"ExecStartPre=\+/bin/sh -c 'ip link del \$\{tap\} 2>/dev/null; "
    r"ip tuntap add \$\{tap\} mode tap user vagrant && "
    r"ip link set \$\{tap\} up && ip addr add \$\{hip\}/30 dev \$\{tap\}'"
)
_PROVISIONER_TAP_DOWN = re.compile(
    r"ExecStopPost=\+/bin/sh -c 'ip link set \$\{tap\} down 2>/dev/null; "
    r"ip tuntap del \$\{tap\} mode tap 2>/dev/null; true'"
)

# The four ``cut`` bindings, in order and as one contiguous block. This is the
# JOINT between the table and the templates, and pinning the two ends without
# it leaves the whole chain forgeable: ``tap=bbeth-1161`` written straight at
# the binding line satisfies every ``${tap}`` pattern above AND the quad
# comparison, and strands four guests on one TAP -- the exact accident the
# template pins were written to prevent. A swapped field number is the same
# hazard one column over: ``hip=... -f2`` addresses each TAP with its GUEST's
# address, so every /30 has two ends with the same address and nothing routes.
# Anchored on ``$entry`` so a binding that stopped reading the loop variable
# fails too.
_PROVISIONER_BINDINGS = re.compile(
    r'ver=\$\(echo "\$entry" \| cut -d: -f1\)\s*\n'
    r'\s*gip=\$\(echo "\$entry" \| cut -d: -f2\)\s*\n'
    r'\s*hip=\$\(echo "\$entry" \| cut -d: -f3\)\s*\n'
    r'\s*tap=\$\(echo "\$entry" \| cut -d: -f4\)'
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
    by the ``busybox-qemu`` provisioner: the
    ``version:guest_ip:tap_ip:tap_name`` quads it builds each guest's TAP and
    unit from, and the version list the ``systemctl enable`` loop walks. That
    third copy is the one Chris provisions the bed FROM. A transposed digit
    there ships a bed whose addresses disagree with lab.json and with the
    images' own rcS while every hostless gate stays green — only a live bed
    would ever say so.

    The TEMPLATES that consume the quads are pinned in the same breath, and
    against the loop variables rather than against any address. A wrapper that
    hardcoded one ``ifname``, or a unit whose ``ExecStartPre`` addressed a tap
    the row does not name, would satisfy a table-only comparison exactly as
    well as the correct one — and would leave four guests sharing one TAP, or
    a TAP with no address on it, which is a bed that answers nothing.

    And so are the ``cut`` BINDINGS between them, which is the finding that
    added them: pinning the table and pinning the ``${tap}``-shaped templates
    still leaves the joint unguarded, and the joint is where the same accident
    is cheapest to write. ``tap=bbeth-1161`` at the binding line, or ``hip``
    reading field 2 instead of field 3, satisfies BOTH ends and ships the bed
    the template pins exist to prevent.
    """
    body = _busybox_provisioner_text()

    quad_lists = _PROVISIONER_QUADS.findall(body)
    assert len(quad_lists) == 1, (
        "expected exactly one version:guest_ip:tap_ip:tap_name list in the "
        f"busybox-qemu provisioner, found {len(quad_lists)} — the table moved, "
        "changed shape, or grew an entry that is not a well-formed quad"
    )
    provisioned = re.findall(
        r'"(\d+\.\d+\.\d+):(\d+\.\d+\.\d+\.\d+):(\d+\.\d+\.\d+\.\d+):([a-z0-9-]+)"',
        quad_lists[0],
    )
    assert provisioned == [(g.version, g.ip, g.host_ip, g.tap) for g in GUEST_TABLE], (
        "Vagrantfile busybox-qemu identity table drifted from GUEST_TABLE in "
        "scripts/build_busybox_guest_images.py — the bed would stand up TAPs "
        "and addresses the lab data does not address"
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
    assert enabled == [version for version, _gip, _hip, _tap in provisioned], (
        "the provisioner's own two copies disagree: enable list "
        f"{enabled} vs identity table {[v for v, _g, _h, _t in provisioned]}"
    )

    for name, pattern in (
        ("the four cut -d: field bindings, in table order", _PROVISIONER_BINDINGS),
        ("the qemu -nic tap line", _PROVISIONER_NIC),
        ("the unit's ExecStartPre TAP setup", _PROVISIONER_TAP_UP),
        ("the unit's ExecStopPost TAP teardown", _PROVISIONER_TAP_DOWN),
    ):
        found = pattern.findall(body)
        assert len(found) == 1, (
            f"expected exactly one occurrence of {name} in the busybox-qemu "
            f"provisioner, found {len(found)} — it was edited away, hardcoded "
            "against something other than the loop's own row, or duplicated"
        )


def test_the_provisioner_keeps_no_user_mode_networking_behind():
    """A leftover slirp flag would quietly undo the whole change.

    ``-nic`` and ``-netdev``/``hostfwd`` are not mutually exclusive to QEMU: a
    guest given both comes up with two interfaces, and the one its rcS
    configures is whichever the kernel enumerates as ``eth0``. So a half-
    reverted wrapper — or a commented-out copy of the old table left "for
    reference" — is a bed that may answer on loopback ports again while every
    address assertion above still passes. The stronger statement is that the
    slirp spelling does not appear in this provisioner AT ALL, which is also
    what makes a shadow copy fail closed rather than parse as documentation.
    """
    body = _busybox_provisioner_text()
    for spelling in ("hostfwd", "netdev", "-net ", "user,", "10.0.2."):
        assert spelling not in body, (
            f"{spelling!r} survives in the busybox-qemu provisioner — the bed "
            "guests are on real TAPs and must have no user-mode networking left"
        )
    # A spelling ban alone is a blocklist, and ``-nic user`` — no comma, no
    # ``netdev``, no ``hostfwd`` — walks straight through one while adding
    # exactly the second interface the paragraph above is about. So state the
    # allowlist instead: EVERY ``-nic`` in this provisioner must be a tap.
    nics = re.findall(r"-nic\s+(\S+)", body)
    assert nics, (
        "no -nic flag in the busybox-qemu provisioner at all — the guests have "
        "no NIC, or the wrapper stopped being generated here"
    )
    for spec in nics:
        assert spec.startswith("tap,"), (
            f"the provisioner gives a guest `-nic {spec}` — anything but a tap "
            "is user-mode networking under another name, and a guest handed two "
            "NICs configures whichever the kernel enumerates as eth0"
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
