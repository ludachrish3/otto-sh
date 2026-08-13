"""The Tier 3 harness works, and each of its four levers is pinned by its own failure.

Tier 2 proves what a BusyBox userland does with otto's payloads through a
local `chroot`. This tier puts a real ssh channel in between. What the tests
here measure is the HARNESS — that the daemon comes up, that a command runs
inside the BusyBox root, that a pty can be allocated, and that the thing gets
reaped. The session's CONTRACTS (mixed-arch proof, sftp/scp refusal,
hermeticity) belong to `test_tier3_session.py`.

WHY EACH LEVER NEEDS ITS OWN TEST, rather than one test that "uses the
harness": the four levers fail in four different ways, and three of them are
invisible to a green exec channel.

* `--keep-caps` — startup. Everything reds, at fixture setup, with a message
  rather than an exit code.
* the RSA host key — the PRODUCT path only. `ssh(1)` stays green, so every
  test here connects with asyncssh, the client otto itself uses. A harness
  smoke-tested by hand looks perfect in the configuration where otto cannot
  connect at all.
* the `tty`-less `/etc/group` — pty only. Exec channels are untouched, so only
  a test that allocates a pty notices its loss, and exactly two in this repo
  do: `test_a_pty_session_survives_the_chown_that_kills_it_without_the_group_bind`
  here, and `test_an_interactive_login_shell_reaches_the_root_and_exits_cleanly`
  in `test_tier3_session.py`. Both would red; every other test in the tier
  stays green.
* the sftp mask — nothing here fails; it widens what the tier serves. Pinned
  in `test_tier3_session.py`, where the refusal is the contract.

Mutation-verified 2026-08-13, each lever removed in turn and the message
recorded — see the plan's Task 2 step 7 and this module's per-test notes.
"""

import os
import socket
from pathlib import Path

import pytest

from tests._fixtures import busybox_dropbear as bbd
from tests._fixtures.busybox_dropbear import run_over_ssh
from tests.busybox.conftest import TIER3_GROUP

# `busybox` is LOAD-BEARING despite this tree's directory stamp — the stamp
# depends on collection-hook order and dies with its own file (see the
# conftest). `xdist_group` is load-bearing for a second reason: it is the
# ANSWER to dropbear's MAX_UNAUTH_PER_IP=5, keeping the whole tier sequential
# on one worker with one daemon and one root. It is declared here, not
# stamped, because only a declaration is independent of invocation shape; the
# conftest's `_unhonored_tier3_group` fails any Tier 3 test where xdist did
# not act on it.
pytestmark = [pytest.mark.busybox, pytest.mark.xdist_group(TIER3_GROUP)]


def test_an_exec_channel_runs_inside_the_busybox_root_not_on_the_dev_vm(tier3_dropbear):
    """The tier's central claim: ssh in, and the userland on the far side is BusyBox.

    The chain has four links that all have to be right — unshare, dropbear,
    the forced command, the chroot into the Tier 2 root — and this is the
    harness's smoke test for all of them.

    TWO ANSWERS, BECAUSE ONE OF THEM DOES NOT DISCRIMINATE EVERYWHERE. `x86_64`
    is the artifact's arch against this VM's aarch64, so on the dev VM it
    cannot come from the host by accident; on an x86_64 CI runner the host
    would answer the same thing, and that term proves nothing there. `PATH` is
    the term that holds on both: the login wrapper injects `/bin` before the
    chroot, and no shell outside it has a one-entry PATH. The mixed-arch
    contract proper — `/usr/bin` absent, `/bin/sh` resolving to
    `/bin/busybox` — is `test_tier3_session.py`'s.

    Reds if `--keep-caps` is dropped (at fixture setup, `mount: /home/vagrant:
    must be superuser to use mount.` rc=32) and if the RSA host key is dropped
    (here, `ConnectionLost`). Both measured.
    """
    result = run_over_ssh(tier3_dropbear, "uname -m; echo $PATH")

    assert result.exit_status == 0, result.stderr
    arch, path = result.stdout.split()
    assert arch == "x86_64", (
        f"expected the x86_64 artifact's answer from inside the rootfs, got {arch!r} — "
        f"an aarch64 answer means the session never entered the chroot and is running "
        f"on the dev VM's own userland"
    )
    assert path == "/bin", (
        f"the login wrapper sets PATH=/bin before chrooting, so a session that reached "
        f"the root reports exactly that; {path!r} is the caller's own environment "
        f"passed straight through, i.e. no chroot happened"
    )


def test_a_pty_session_survives_the_chown_that_kills_it_without_the_group_bind(tier3_dropbear):
    """A pty is allocated and used. ONE OF TWO TESTS THAT NOTICE THE `/etc/group` BIND.

    Dropbear chowns each new pty to `(uid, getgrnam("tty")->gr_gid)`. gid 5 is
    unmapped inside `--map-current-user`, the chown returns EINVAL, and
    dropbear calls `dropbear_exit` — fatal, mid-session. Exec channels never
    allocate a pty, so every test in this tier that does not allocate one stays
    green through it.

    THE OTHER WITNESS is
    `test_an_interactive_login_shell_reaches_the_root_and_exits_cleanly` in
    `test_tier3_session.py`, the only other pty in the tree (both are found by
    grepping `term_type`). It covers the bind by accident rather than by
    design — its subject is the login wrapper's fall-through branch — which is
    why this test exists separately and should not be judged redundant with it:
    delete this one and the lever keeps a witness, but no test is *aimed* at it.

    Mutation-verified by removing the `/etc/group` clause from the argv:
    this test reds with `ConnectionLost` while
    `test_an_exec_channel_runs_inside_the_busybox_root_not_on_the_dev_vm`
    still passes, and the daemon log carries
    `chown(/dev/pts/1, 1000, 5) failed: Invalid argument`.

    A pty also changes the transport, not just the allocation: the reply
    arrives CRLF-terminated, which is why the comparison strips rather than
    matching the exec channel's exact bytes.
    """
    result = run_over_ssh(tier3_dropbear, "uname -m", term_type="xterm")

    assert result.exit_status == 0, result.stderr
    assert result.stdout.strip() == "x86_64", (
        f"a pty session reached the rootfs but answered {result.stdout!r}"
    )


def test_the_group_file_the_harness_binds_declares_no_tty_group(tmp_path):
    """The lever's content, pinned where a live pty cannot say WHY it broke.

    The test above proves a pty works; this one proves the reason. Without it
    a reader who reds the pty test has to rediscover that a `tty` row is what
    matters — and someone "restoring" a realistic `/etc/group` would take the
    pty down with a change no comment forbids.

    Pure, so it costs nothing and runs on a machine with no dropbear at all.
    """
    group = bbd.write_group_without_tty(tmp_path / "group")

    rows = group.read_text().splitlines()
    assert rows, "an empty group file is not the lever; getgrnam would fail for a second reason"
    assert not any(row.startswith("tty:") for row in rows), (
        f'the whole point of this file is that `getgrnam("tty")` returns NULL, so '
        f"dropbear chowns each pty to the user's own mapped gid instead of the "
        f"unmapped gid 5. Rows: {rows}"
    )


def test_the_argv_carries_both_host_keys_and_keeps_its_capabilities(tmp_path):
    """The two levers a green run cannot distinguish from luck, read off the argv.

    Deliberately an INTENT check, and deliberately not the only one. Both
    levers already have outcome guards above — drop `--keep-caps` and the
    fixture dies at setup; drop `-r rsa` and every asyncssh connection reds.
    What those cannot do is SAY which lever went, and both failures arrive as
    a message about something else (a mount error, a lost connection). This
    test names them.

    NO FIXTURE, ON PURPOSE, and the first draft of this test got that wrong.
    It took `tier3_dropbear`, which made the claim above false: when
    `--keep-caps` is removed the daemon never starts, so this test ERRORS in
    setup alongside every other one and names nothing. Measured — the
    `--keep-caps` mutation produced five setup errors, this test among them.
    Built by hand from paths that need not exist, it is the one guard that
    still speaks on a machine where the namespace, dropbear or the artifact
    cache is unavailable.
    """
    keys = bbd.DropbearKeys(
        rsa=tmp_path / "host_rsa",
        ed25519=tmp_path / "host_ed25519",
        client=tmp_path / "client",
        authorized_keys=tmp_path / "client.pub",
    )
    argv = bbd.LoopbackDropbear(
        rootfs=tmp_path / "root",
        keys=keys,
        fake_home=tmp_path / "home",
        group_file=tmp_path / "group",
        sftp_mask=tmp_path / "mask",
        login_wrapper=tmp_path / "login.sh",
        log_path=tmp_path / "dropbear.log",
    ).argv(12345)

    assert "--keep-caps" in argv, (
        f"without --keep-caps the first bind fails `must be superuser to use mount` "
        f"(rc 32) and the daemon never starts. --map-root-user also fixes that and "
        f"must NOT be used: it makes id -u 0 and moves dropbear onto its privileged "
        f"branch. argv={argv}"
    )
    assert "--map-root-user" not in argv, f"see above — the wrong fix for the same rc 32: {argv}"

    command = argv[-1]
    assert f"-r {keys.rsa}" in command, (
        f"the RSA host key is MANDATORY: asyncssh 2.24 gets ConnectionLost against an "
        f"ed25519-only host key and the dropbear child dies on `Failed assertion "
        f"(rsa.c:164): key != NULL`. ssh(1) stays green, so this cannot be checked by "
        f"hand. command={command}"
    )
    assert f"-r {keys.ed25519}" in command, (
        f"both host keys, always — the tier stands in for a device that offers both. "
        f"command={command}"
    )


def test_stopping_the_daemon_reaps_it_by_pid_and_frees_the_port(tier3_dropbear, tmp_path):
    """`stop()` leaves nothing behind: no process, no listener.

    Its own daemon, not the session's, because proving the reap means killing
    the thing — and the session fixture is what every other test here needs.
    The Tier 2 root is reused, so this costs one `start()` (0.05s measured).

    THE PID IS CAPTURED AND CHECKED. An orphaned sshd from this same shape of
    harness was found on this VM two days after the run that spawned it,
    which is why `_die_with_parent` exists; a reap that is never verified is
    how the next one gets missed. `os.kill(pid, 0)` asks the kernel rather
    than asking `ps` — no name matching anywhere in this tier, since
    `pkill -f dropbear` matches the shell that invoked the run.

    Mutation-verified by making `_terminate` return early: this reds with
    `Failed: DID NOT RAISE ProcessLookupError` — on the still-live PID, not on
    the port, which is the discrimination that matters (a port can be freed by
    a wedged child closing its listener while the group survives).

    That mutation also surfaced a SECOND, independent detector worth knowing
    about, because it means an unreaped daemon reds twice from two mechanisms:
    pyproject's `filterwarnings = ["error"]` turns CPython's
    `ResourceWarning: subprocess <pid> is still running` (raised from
    `Popen.__del__`) into `PytestUnraisableExceptionWarning` at teardown. It
    is not a substitute for this test — it fires only once the Popen is
    garbage-collected, so it names a pid and no cause, and it cannot say
    whether `stop()` was called at all.
    """
    daemon = bbd.LoopbackDropbear.build(tmp_path / "second", tier3_dropbear.rootfs)
    port = bbd.free_port()
    daemon.start(port)
    pid = daemon.pid

    assert run_over_ssh(daemon, "echo up").stdout.strip() == "up", (
        "the second daemon must be genuinely serving before its reap proves anything"
    )

    daemon.stop()

    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
    # ConnectionRefusedError specifically: a listener that is GONE refuses,
    # while one that is merely wedged would time out — and `OSError` covers
    # both, so it would pass on a daemon this method failed to kill.
    with (
        pytest.raises(ConnectionRefusedError),
        socket.create_connection(("127.0.0.1", port), timeout=1.0),
    ):
        pass


def test_the_harness_never_touches_the_developers_real_authorized_keys(tier3_dropbear):
    """The namespace's whole purpose, asserted against the real file on this machine.

    Not the full hermeticity contract — `test_tier3_session.py` owns the
    guard that INJECTS the hostile condition by removing the namespace and
    proving auth is then refused. This one is the cheap standing check that
    the harness, as it runs, leaves the real file alone: a bind over the
    wrong target (`$HOME` where it differs from the passwd row's `pw_dir`, or
    over `~/.ssh` on a runner that has none) is a real mistake with a silent
    signature.

    Content and mtime both, because the two failure directions differ: an
    appended key changes content, and a rewrite that happens to restore the
    same bytes changes only the timestamp.
    """
    real = Path(bbd.login_passwd_entry().pw_dir) / ".ssh" / "authorized_keys"
    if not real.exists():
        # A fresh CI runner has no `~/.ssh` at all. That is the case the bind
        # covers by targeting the HOME DIRECTORY rather than `~/.ssh`, and
        # the property still has to hold: nothing may CREATE it either.
        run_over_ssh(tier3_dropbear, "true")
        assert not real.exists(), f"the tier created {real}, which it must never write"
        return

    before, before_mtime = real.read_bytes(), real.stat().st_mtime_ns
    run_over_ssh(tier3_dropbear, "true")

    assert real.read_bytes() == before, f"the tier modified {real}"
    assert real.stat().st_mtime_ns == before_mtime, f"the tier rewrote {real}"
    # The base64 body, not the whole line: ssh-keygen appends `user@host`, and
    # a comparison that included it would pass on a match that differed only
    # by comment — the substring that actually authenticates is the blob.
    blob = tier3_dropbear.keys.authorized_keys.read_text().split()[1]
    assert blob.encode() not in before, (
        f"the tier's throwaway client key is present in {real} — the fake home was "
        f"not bound over the real one, so this session authenticated against the "
        f"developer's own file"
    )
