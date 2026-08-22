"""What a BusyBox bed guest IS, checked once per pinned version.

Three claims, and every one of them is a claim about the GUEST rather than
about otto: otto reaching a shell at all is the precondition, not the
finding. The bed exists because otto's whole BusyBox story was, until it,
told by contrived local harnesses — a restricted-PATH rootfs, a dropbear
grafted onto a chroot — where the binary was real and everything around it
was ours. These three say the far end of the telnet session is the pinned
BusyBox milestone, alone on its own rootfs, with the login path a real
device would make you take.

* **The version is the one this row claims.** ``busybox`` with no applet
  name prints its own banner, so the guest identifies itself in its own
  words. The row's ``sw_version`` comes from the lab entry, which
  ``tests/unit/host/test_busybox_bed_lab_entries.py`` pins to the image
  builder's ``GUEST_TABLE``; asserting the banner here closes the loop
  from the pinned tarball through the initramfs to the running shell. A
  mixed-up hostfwd port would land otto on the neighbouring guest, and
  every hostless gate would still be green — this is the assertion that
  sees it.
* **Nothing GNU is hiding behind it.** The whole point of the tier is a
  userland with only what BusyBox ships. A stray ``/usr/bin/sed`` or
  ``/bin/bash`` would silently satisfy probes that must fall back, and the
  suite would certify capabilities the device does not have.
* **ssh is dead by construction.** The guests run telnetd and nothing
  else, which makes them otto's only standing true negative for the
  ssh-shaped assumptions — see the probe-survey design. If an sshd ever
  appears here, that negative is gone and the tests that rely on it are
  quietly lying.

Cheap by design: three commands per guest, no transfers, no files. They
are the module a bed problem should surface in first, so the parametrized
backends in the generic suites are debugging otto instead of the bed.
"""

import pytest

pytestmark = [pytest.mark.asyncio]


async def test_telnet_login_reaches_this_versions_own_ash(guest):
    """The shell behind the login prompt is the BusyBox milestone this row names."""
    host, version = guest
    result = (await host.run("busybox 2>&1 | head -n 1")).only
    assert result.retcode == 0, f"`busybox` failed on {host.element}: {result.value!r}"
    assert f"BusyBox v{version} " in result.value, (
        f"{host.element} answered {result.value!r}, which is not the "
        f"BusyBox v{version} this lab entry claims"
    )


async def test_no_gnu_userland_hides_behind_the_guest(guest):
    """No GNU binaries on the rootfs, and /bin/sh is BusyBox's own applet link."""
    host, _version = guest
    result = (await host.run("[ -e /usr/bin/sed ] || [ -e /bin/bash ] || echo clean")).only
    assert result.retcode == 0
    assert "clean" in result.value, (
        f"{host.element} carries a GNU userland alongside BusyBox "
        f"(probe said {result.value!r}) — the tier's premise is gone"
    )
    link = (await host.run("readlink /bin/sh")).only
    assert link.retcode == 0, f"/bin/sh is not a symlink on {host.element}"
    assert link.value.strip().endswith("busybox"), (
        f"/bin/sh on {host.element} points at {link.value.strip()!r}, not busybox"
    )


async def test_ssh_is_dead_by_construction(guest):
    """No sshd on the guest: the bed's standing ssh-shaped true negative."""
    host, _version = guest
    result = (await host.run("command -v sshd || echo no-sshd")).only
    assert result.retcode == 0
    assert "no-sshd" in result.value, (
        f"{host.element} has an sshd at {result.value.strip()!r}; the bed's "
        "only ssh-absent guests just stopped being ssh-absent"
    )
