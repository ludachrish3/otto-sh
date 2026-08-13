"""Tier 3's prerequisite is installed here, and its absence is NAMED not skipped.

Two claims, and they fail for opposite reasons.

The first is about PLUMBING: `dropbear-bin` is a package a machine has to have
before Tier 3 can run at all, and it is delivered by two files that no test
executes — the `dev-root` provisioner in the repository's `Vagrantfile`, and
the busybox job in `.github/workflows/ci.yml`. Neither is a Python import, so
the only way either one is checked is a test that asks the running machine
whether the package arrived. Without that, an apt line that was mistyped, or
dropped in a merge, or landed on the wrong CI leg, shows up as a Tier 3
failure some later commit has to diagnose.

The second is about the REFUSAL, and it is the one this repo keeps paying for
when it is missing: `require_dropbear` must raise, by name, naming the apt
line. A `pytest.skip` there and a passing tier are the same line in a summary.

These two are also load-bearing for each other. A refusal test that patches a
path to something nonexistent proves nothing on a machine where the real path
is nonexistent too — the hostile condition has to be INJECTED, not inherited —
so each refusal test below asserts the satisfied pre-state first, and the
plumbing test is what tells you which of the two went wrong.
"""

import pytest

from tests._fixtures import busybox_dropbear as bbd

# `busybox` is LOAD-BEARING here even though tests/busybox/conftest.py stamps it
# by directory: that stamp depends on collection-hook order and dies with its own
# file, so the declaration is what keeps this tier out of the lanes that would
# fetch from busybox.net. Pinned by G9b in tests/unit/test_tier_marker_invariants.py.
pytestmark = [pytest.mark.busybox]


def test_the_provisioned_host_has_the_dropbear_tier3_needs():
    """The plumbing guard: the Vagrantfile and CI apt lines actually delivered.

    This is the only check on either of them. It fails with
    :class:`~tests._fixtures.busybox_dropbear.DropbearUnavailableError`'s full
    remedy text, so a developer on a machine provisioned before `dropbear-bin`
    joined the Vagrantfile gets the apt line rather than a puzzle.

    It also underwrites every refusal test in this module: those inject a
    nonexistent path, and an injection is only an injection while the real
    path is present. If this test reds, treat the ones below as vacuous rather
    than as evidence.
    """
    bbd.require_dropbear()


def test_missing_dropbear_refuses_by_name_rather_than_skipping(monkeypatch):
    """A missing daemon raises and names the package, never skips.

    The rebind is what makes this a test of the guard and not of the machine.
    It also pins that :func:`require_dropbear` reads the module constant at
    call time: were it captured as a default argument, this rebind would be
    inert and the real, present binary would be checked instead.
    """
    # INJECTED, not inherited. On a host with no dropbear at all, everything
    # below passes without the rebind doing anything — a green row proving
    # something about the machine and nothing about the refusal. So the
    # satisfied pre-state is asserted first. Yes, that repeats the plumbing
    # test above, on purpose: its failure says the provisioning is broken,
    # this line's says THIS test was measuring nothing.
    bbd.require_dropbear()

    monkeypatch.setattr(bbd, "DROPBEAR", "/nonexistent/dropbear")
    with pytest.raises(bbd.DropbearUnavailableError, match=r"dropbear-bin") as excinfo:
        bbd.require_dropbear()

    message = str(excinfo.value)
    # The package alone is not a diagnosis. A reader whose dropbear lives
    # somewhere else entirely needs to see WHICH path was consulted, or the
    # apt line sends them to reinstall a package they already have.
    assert "/nonexistent/dropbear" in message, message
    assert "absent" in message, message


def test_missing_dropbearkey_is_refused_too(monkeypatch):
    """The gate covers the KEY GENERATOR, not just the daemon.

    Tier 3 generates a host key and a client key before the daemon is worth
    starting, so `dropbearkey` is as much a prerequisite as `dropbear` is.
    Nothing else would notice its removal from the check: the two ship in one
    package, so on every machine that has one it has the other, and the gate
    would keep passing right up until a partial install turned a named refusal
    into dropbearkey's own error from inside key generation.
    """
    bbd.require_dropbear()

    monkeypatch.setattr(bbd, "DROPBEARKEY", "/nonexistent/dropbearkey")
    with pytest.raises(bbd.DropbearUnavailableError, match=r"dropbear-bin") as excinfo:
        bbd.require_dropbear()

    assert "/nonexistent/dropbearkey" in str(excinfo.value), str(excinfo.value)


def test_both_missing_binaries_are_named_in_one_message(monkeypatch):
    """Every unusable path is reported at once, not one per run.

    The precedent is :func:`tests._fixtures.busybox.require_interpreter`,
    which reports every missing binfmt handler in one message rather than
    letting the reader discover them one ENOEXEC at a time. A check that
    returned on the first failure would satisfy both tests above and still
    make a fresh machine reveal its missing files in sequence.
    """
    monkeypatch.setattr(bbd, "DROPBEAR", "/nonexistent/dropbear")
    monkeypatch.setattr(bbd, "DROPBEARKEY", "/nonexistent/dropbearkey")

    with pytest.raises(bbd.DropbearUnavailableError) as excinfo:
        bbd.require_dropbear()

    message = str(excinfo.value)
    assert "/nonexistent/dropbear" in message, message
    assert "/nonexistent/dropbearkey" in message, message


def test_a_present_but_unexecutable_binary_is_diagnosed_apart_from_absent(monkeypatch, tmp_path):
    """`noexec` is not `not installed`, and the message must not conflate them.

    `os.access(X_OK)` answers False for both, so a gate built on that alone
    tells someone whose filesystem is mounted `noexec` to install a package
    they already have. The rootfs tier learned this the expensive way — see
    :func:`tests._fixtures.busybox_rootfs._require_exec_mount`, which exists
    because a bare `Permission denied` named neither the mount option nor the
    fixture responsible.

    Driven through the public refusal with a real non-executable file, so the
    branch is selected by the filesystem the way it would be in the wild
    rather than by rebinding a reason string.
    """
    bbd.require_dropbear()

    unexecutable = tmp_path / "dropbear"
    unexecutable.write_text("#!/bin/sh\nexit 0\n")
    unexecutable.chmod(0o644)
    monkeypatch.setattr(bbd, "DROPBEAR", str(unexecutable))

    with pytest.raises(bbd.DropbearUnavailableError) as excinfo:
        bbd.require_dropbear()

    message = str(excinfo.value)
    assert f"{unexecutable} (present but not executable)" in message, message
    assert "absent" not in message, message
