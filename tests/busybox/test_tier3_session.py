"""What Tier 3 PROVES about BusyBox over real ssh — not what it proves about its harness.

`test_tier3_harness.py` measures the machinery: the daemon starts, an exec
channel runs, a pty is allocated, the thing is reaped. Every one of those
would still pass against a daemon that logged the caller straight into the dev
VM's own GNU userland. This module measures the CONTRACTS that make the tier
worth having, and each one is a claim the spike made and the spec then relied
on:

* the far side is the PINNED BusyBox artifact, with no GNU userland behind it;
* a NATIVE daemon execs a FOREIGN artifact through binfmt, with no interpreter
  inside the chroot — the spec's named early risk, "expected to work
  transparently ... but proven before it is relied upon";
* sftp and scp are BOTH unreachable, so the tier stands in for a device that
  has neither;
* the mount namespace is what authenticates the session, so the developer's
  real `~/.ssh/authorized_keys` is not merely untouched but UNUSED.

TWO OF THESE NEED A SECOND DAEMON, and that is the difference between a guard
and an observation. A test that removes a lever and watches the tier keep
working proves nothing about the lever; a test that removes it and watches the
outcome CHANGE proves the lever is load-bearing. So the sftp/scp contract is
paired with a daemon whose chroot wrapper is gone (scp then reaches the host's
own binary, and only the mask still refuses sftp), and the hermeticity
contract is paired with a daemon whose namespace is gone (the tier's key then
does not authenticate at all). Each second daemon is built from the session
daemon's own keys and root, started on its own port, and reaped by captured
pid in a `finally`.

COST, IN THE WINDOW THESE TESTS SPEND FROM. Everything here runs in the CALL
phase, against pyproject's `timeout = 180` with `timeout_func_only = true`
(the session daemon's setup is charged to `faulthandler_timeout` instead — see
the coupled-budget block at the top of `busybox_rootfs.py`, amended for the
two daemons this module starts). The most expensive body is the sftp/scp pair
below: `_READY_TIMEOUT_S` (15s) for its own daemon plus two
`_CLIENT_TIMEOUT_S` (30s each), i.e. 75s of the 180 in the worst case.
Measured, warm: the whole module is under a second of work.

NOTHING HERE IS TIMING-BASED. The two bounds this module declares are runaway
guards that turn a wedge into a named failure; no assertion reads a clock, and
none may (see `docs/architecture/quality-gates.md`, and issue #229 for the
most recent time this was got wrong).
"""

import asyncio
import os
import shlex
import shutil
import subprocess
from pathlib import Path

import asyncssh
import pytest

from tests._fixtures import busybox_dropbear as bbd
from tests._fixtures.busybox import _BINFMT_ROOT, QEMU_HANDLER

# One asyncssh helper for the whole tier, imported rather than copied. It
# carries the reason every Tier 3 connection is made with asyncssh and never
# with `ssh(1)` — the client that fails against an ed25519-only host key is
# otto's own — and a second copy here would be a second place for that to rot.
# It lived in `test_tier3_harness.py` until the third consumer arrived; a test
# module that its siblings import is a fixture module wearing the wrong name,
# so it moved to the fixture package beside the daemon it drives.
from tests._fixtures.busybox_dropbear import run_over_ssh
from tests.busybox.conftest import TIER3_GROUP, TIER3_RELEASE

# `busybox` is LOAD-BEARING despite the directory stamp (the stamp depends on
# collection-hook order and dies with its own file — see the conftest), and
# `xdist_group` is the ANSWER to dropbear's measured MAX_UNAUTH_PER_IP=5: it
# keeps the whole tier sequential on one worker, sharing one daemon. Declared
# rather than stamped because only a declaration is independent of the
# invocation shape; `_unhonored_tier3_group` fails any Tier 3 test where xdist
# did not act on it.
pytestmark = [pytest.mark.busybox, pytest.mark.xdist_group(TIER3_GROUP)]

_SFTPSERVER_PATH = "/usr/lib/sftp-server"
"""The path packaged dropbear was built to exec for the `sftp` subsystem.

Not a guess and not a policy: `dropbear-bin 2022.83-4` is compiled with
`SFTPSERVER_PATH=/usr/lib/sftp-server`, and Ubuntu ships an OpenSSH
`sftp-server` at exactly that name (a symlink to `openssh/sftp-server`, here
and on ubuntu-latest). It appears in two places below — the mask the harness
binds over it, and the refusal message the rootfs's ash produces when the
forced command tries to run it — and
:func:`test_the_mask_alone_still_refuses_sftp_while_scp_reaches_the_host`
asserts the harness really binds THIS path, so the string the assertions match
cannot drift away from the string the harness masks.
"""

_OPENSSH_CLIENT_PACKAGE = "openssh-client"
"""Named in the refusal when `scp` or `sftp` is missing. Never skipped.

Same package as the `ssh-keygen` :func:`~tests._fixtures.busybox_dropbear.generate_keys`
needs, and a separate prerequisite from `dropbear-bin`. These two surfaces are
the only place in the tier driven by a command-line client rather than by
asyncssh, for the plain reason that otto has no sftp or scp client of its own
to point at them — the contract is that a DEVICE offers neither, so it is
measured with the tools that would find them if they were there.
"""

_CLIENT_OPTIONS = [
    # The host key is generated per session, so there is nothing to have
    # known. UserKnownHostsFile is not cosmetic here: `scp` and `sftp` run
    # OUTSIDE the mount namespace, so the real `~/.ssh` is the one they would
    # write a new host key into — the very file tree this module's
    # hermeticity contract is about.
    "-o",
    "StrictHostKeyChecking=no",
    "-o",
    "UserKnownHostsFile=/dev/null",
    # No prompt, ever. A client that stops for a passphrase or a password
    # would hang until _CLIENT_TIMEOUT_S and arrive as a timeout, which is the
    # one failure shape indistinguishable from a real one in a summary line.
    "-o",
    "BatchMode=yes",
    # The tier's throwaway key ONLY — never the developer's agent. Without
    # this an agent-loaded key could authenticate instead, and the refusal
    # under test would be measured through a credential the tier never issued.
    "-o",
    "IdentitiesOnly=yes",
    # Keeps the "Permanently added ... to the list of known hosts" notice off
    # stderr so the assertions read the SERVER's message. The relayed remote
    # error survives this level — measured.
    "-o",
    "LogLevel=ERROR",
]
"""Options every OpenSSH client call in this module carries, each load-bearing."""

_CLIENT_TIMEOUT_S = 30.0
"""Runaway guard for ONE `scp`/`sftp` invocation. Measured at ~0.2s.

A bound, never a discriminator: nothing asserts on how long a client took.
Two orders of magnitude of slack, and sized so the most expensive test body
here (a daemon start plus two client calls) stays well inside the 180s
per-test timeout — see this module's docstring.
"""

_INTERACTIVE_TIMEOUT_S = 30.0
"""Runaway guard for the interactive login shell to read `exit` and go.

Same rule: the assertion is that the shell exited cleanly having run the
command, never that it did so quickly. The timeout exists so a shell that
never exits is reported as this named failure instead of as a bare
`Timeout >180.0s` with no thread to pull.
"""


def openssh_client(name: str) -> str:
    """Absolute path to an OpenSSH CLIENT binary, or a NAMED refusal.

    Nothing skips. A missing `scp` would otherwise surface as
    `FileNotFoundError: 'scp'` from inside :mod:`subprocess`, which names the
    binary but neither the package nor why this tier wants it.
    """
    found = shutil.which(name)
    if found is None:
        raise bbd.DropbearUnavailableError(
            f"the BusyBox Tier 3 tier proves that sftp and scp are unreachable inside the "
            f"BusyBox root, which it does by DRIVING them; `{name}` is not on PATH here.\n"
            f"    sudo apt update && sudo apt install {_OPENSSH_CLIENT_PACKAGE}\n"
            f"Same package as the `ssh-keygen` that writes this tier's client key, and a "
            f"separate prerequisite from `dropbear-bin`."
        )
    return found


def run_openssh_client(
    name: str, daemon: bbd.LoopbackDropbear, *args: str, stdin: str = ""
) -> "subprocess.CompletedProcess[str]":
    """Run an OpenSSH client against *daemon* and return the completed process.

    `check=False`: a refusal is the expected outcome of most calls here, so
    the exit status and both streams have to arrive at the assertion as data.
    """
    argv = [
        openssh_client(name),
        *_CLIENT_OPTIONS,
        "-i",
        str(daemon.keys.client),
        # Uppercase for both clients: `scp -P` and `sftp -P` are the port.
        "-P",
        str(daemon.port),
        *args,
    ]
    return subprocess.run(
        argv,
        capture_output=True,
        text=True,
        input=stdin,
        timeout=_CLIENT_TIMEOUT_S,
        check=False,
    )


def interactive_login(
    daemon: bbd.LoopbackDropbear, keystrokes: str
) -> "asyncssh.SSHCompletedProcess":
    """Open an interactive login shell on a pty, type *keystrokes*, wait for exit.

    NO COMMAND IS REQUESTED, and that is the whole point of this helper.
    `run_over_ssh` (and every other Tier 3 test) asks for a remote command,
    which dropbear hands to the forced wrapper in `SSH_ORIGINAL_COMMAND` — so
    they all take the wrapper's FIRST branch. The interactive branch, the bare
    `exec /usr/sbin/chroot <root> /bin/sh` on the wrapper's last line, is
    reached only by a client that asks for a shell and nothing else.
    """

    async def go() -> "asyncssh.SSHCompletedProcess":
        async with asyncssh.connect(
            "127.0.0.1",
            port=daemon.port,
            username=daemon.username,
            client_keys=[str(daemon.keys.client)],
            known_hosts=None,
        ) as conn:
            proc = await conn.create_process(term_type="xterm", encoding="utf-8")
            proc.stdin.write(keystrokes)
            await proc.stdin.drain()
            try:
                return await asyncio.wait_for(proc.wait(), _INTERACTIVE_TIMEOUT_S)
            except asyncio.TimeoutError:
                proc.terminate()
                raise AssertionError(
                    f"the interactive login shell never exited within "
                    f"{_INTERACTIVE_TIMEOUT_S}s of being sent {keystrokes!r}. That is the "
                    f"wrapper's fall-through branch (`exec /usr/sbin/chroot <root> "
                    f"/bin/sh`), not the SSH_ORIGINAL_COMMAND branch every other test "
                    f"here takes — a shell that comes up but never reads its pty looks "
                    f"exactly like one that was never started."
                ) from None

    return asyncio.run(go())


class _DropbearWithoutTheChrootWrapper(bbd.LoopbackDropbear):
    """The tier's daemon with EXACTLY ONE clause removed: the `-c` forced command.

    Every other lever — the namespace, all three binds, both host keys — is
    the real one, because the experiment is only worth running if a single
    variable moved. Without `-c`, dropbear runs the user's own login shell on
    the DEV VM's userland, which is the configuration the sftp/scp contract
    has to be discriminated against.
    """

    def argv(self, port: int) -> "list[str]":
        """The real argv minus `-c <wrapper>`, with the surgery self-verified."""
        argv = super().argv(port)
        head, sep, tail = argv[-1].partition(" -c ")
        # THE MUTATION MUST MUTATE. If the flag ever moves off the end of the
        # exec line, this partition would silently truncate other flags — or
        # find nothing and leave the wrapper in place, which is worse: the
        # test would then measure the real config and pass while claiming to
        # have removed a lever.
        assert sep, f"no ` -c ` clause to remove; the argv changed shape: {argv[-1]}"
        assert tail == shlex.quote(str(self._login_wrapper)), (
            f"` -c ` is no longer the last clause of the exec line, so removing everything "
            f"after it would remove more than the wrapper: {tail!r}"
        )
        return [*argv[:-1], head]


class _DropbearWithoutTheMountNamespace(bbd.LoopbackDropbear):
    """The tier's daemon with the namespace and all three binds removed.

    What is left is the same dropbear, the same host keys, the same forced
    command and the same port — but reading the REAL `~/.ssh/authorized_keys`
    through its hardcoded `getpwnam` lookup, because there is no longer a fake
    home bound over the passwd row's directory.

    It is a real ssh daemon accepting the developer's real authorized keys on
    loopback for the length of one test, which is worth naming rather than
    glossing: it is unprivileged, it listens on 127.0.0.1 at an ephemeral
    port, password auth is off (`-s`), and its forced command chroots — which
    without the namespace's ambient capabilities fails with `Operation not
    permitted`. A session that did authenticate would therefore get nothing.
    """

    def argv(self, port: int) -> "list[str]":
        """The exec clause alone, run by a plain shell instead of by `unshare`."""
        clauses = super().argv(port)[-1].split(" && ")
        exec_clause = clauses[-1]
        # Self-verifying for the same reason as the class above: if this
        # surgery silently kept the namespace, the test below would assert
        # that a fully hermetic daemon refuses the tier's own key — and red
        # for a reason that has nothing to do with hermeticity.
        assert exec_clause.startswith(f"exec {bbd.DROPBEAR} "), (
            f"the daemon is no longer the last clause of the script: {exec_clause}"
        )
        assert all("mount --bind" in clause for clause in clauses[:-1]), (
            f"a clause other than the three binds is being discarded with them: {clauses}"
        )
        return ["/bin/sh", "-c", exec_clause]


def test_the_session_lands_in_the_pinned_busybox_release_with_no_gnu_userland_behind_it(
    tier3_dropbear,
):
    """The far side is the ARTIFACT, not a shell that happens to answer.

    Three terms, because the cheap one does not discriminate everywhere and
    the tier's whole value rests on this being right.

    The BANNER is the strongest: it names the exact release the fixture
    fetched, and the dev VM's own `/bin/busybox` is a different version
    (Ubuntu 24.04 ships 1.36.1), so a session that never entered the chroot
    reports a version this assertion rejects — on any architecture, unlike
    `uname -m`. `/usr/bin` ABSENT is the property Tier 1 structurally could
    not provide: it scopes PATH, leaving a caller that reaches for
    `/usr/bin/<gnu tool>` by absolute path to find the real one, while here
    there is no such directory at all. `/bin/sh -> /bin/busybox` is what makes
    every payload otto sends a measurement of ash rather than of dash.

    ONE ROUND TRIP for the three, because each `run_over_ssh` is a fresh
    connection and the tier's budget is connections, not assertions (dropbear
    serves five simultaneous pre-auth connections per IP; see
    `MAX_UNAUTH_PER_IP`).

    Mutation-verified by dropping `-c <wrapper>` from the harness argv, which
    logs the session into the dev VM instead: this reds with
    `session answered 'BusyBox v1.36.1 (Ubuntu 1:1.36.1-6ubuntu3.1)
    multi-call binary.'` against the pinned 1.35.0 — the banner term earning
    its place, since the host's own BusyBox is a real and plausible thing to
    be talking to by mistake.
    """
    result = run_over_ssh(
        tier3_dropbear,
        "busybox 2>&1 | head -1; readlink /bin/sh; "
        "[ -d /usr/bin ] && echo /usr/bin-PRESENT || echo /usr/bin-ABSENT",
    )

    assert result.exit_status == 0, result.stderr
    lines = result.stdout.splitlines()
    # Checked before the unpack, which would otherwise fail as a bare
    # `ValueError: not enough values to unpack` — a message about this test's
    # own tuple, naming nothing about the session that produced it.
    assert len(lines) == 3, f"expected three answers from the session, got {result.stdout!r}"
    banner, sh_target, usr_bin = lines

    assert banner.startswith("BusyBox "), (
        f"the login shell's `busybox` announced {banner!r}, which is not a BusyBox banner "
        f"at all — the session is not in the root"
    )
    assert f"v{TIER3_RELEASE.version} " in banner, (
        f"Tier 3 serves the {TIER3_RELEASE.version} artifact (see `TIER3_RELEASE`) and the "
        f"session answered {banner!r}. A DIFFERENT version means the chroot did not happen "
        f"and this is the host's own busybox — the dev VM's is 1.36.1 — so every payload "
        f"this tier measures would be measured against the wrong userland."
    )
    assert sh_target == "/bin/busybox", (
        f"/bin/sh resolves to {sh_target!r} inside the session. The tier exists to measure "
        f"what BusyBox ash does with otto's payloads; anything else here (dash, bash) means "
        f"it is measuring the host's shell."
    )
    assert usr_bin == "/usr/bin-ABSENT", (
        f"the root has a /usr/bin ({usr_bin}), so a caller that reaches for a GNU tool by "
        f"ABSOLUTE path finds one — which is precisely the hole Tier 1's PATH scoping left "
        f"and this tier was built to close."
    )


def test_the_native_daemon_execs_the_foreign_artifact_with_no_interpreter_in_the_root(
    tier3_dropbear,
):
    """The spec's named early risk: mixed-arch, PROVEN rather than expected.

    An aarch64 dropbear forks a login wrapper that chroots into a root holding
    nothing but an x86_64 static binary, and the exec has to cross an
    architecture boundary with no interpreter on the far side. The spec
    expected that to "work transparently" through binfmt and required it to be
    proven before anything relied on it.

    THE PROOF IS THE ABSENCE, not the answer. `uname -m` returning the
    artifact's arch is necessary but weak — on an x86_64 runner the host would
    answer the same thing without a boundary having been crossed. What makes
    it a proof on THIS machine is that the root contains no interpreter for
    the kernel to find: `qemu-x86_64` is registered with binfmt flag `F`,
    which loads the interpreter at REGISTRATION time and keeps it open, so it
    survives the chroot. Without `F` the kernel would open the interpreter
    path at exec time, inside a root where it does not exist, and every
    session would die with ENOENT.

    The second half runs only where the arch actually differs. On an x86_64
    runner there is no boundary and nothing to prove; the branch says so
    rather than pretending otherwise, and the dev VM is where the risk lives.

    Mutation-verified in both halves. Planting a `qemu-x86_64` inside the root
    from `busybox_rootfs()` reds with
    `the root at ... contains ['qemu-x86_64']`; pointing the handler read at a
    fabricated registration carrying `flags: PO` reds with
    `carries flags 'PO'` and quotes the registration back.
    """
    result = run_over_ssh(tier3_dropbear, "uname -m")

    assert result.exit_status == 0, result.stderr
    session_arch = result.stdout.strip()
    assert session_arch == TIER3_RELEASE.arch, (
        f"the session answered {session_arch!r} where the Tier 3 artifact is "
        f"{TIER3_RELEASE.arch} — the login never reached the root, or the root holds a "
        f"different artifact than the fixture believes"
    )

    daemon_arch = os.uname().machine
    if daemon_arch == TIER3_RELEASE.arch:
        # Native. The exec crossed no boundary, so there is no binfmt claim to
        # make here and asserting one would be a guard that cannot fail. This
        # is the busybox job's `ubuntu-latest` (x86_64) row. The OTHER row,
        # `ubuntu-24.04-arm`, is foreign-arch against this x86_64 artifact and
        # takes the branch below — the same one the dev VM takes, and the place
        # a missing binfmt `F` flag would bite first.
        return

    strays = sorted(p.name for p in tier3_dropbear.rootfs.rglob("*qemu*"))
    assert not strays, (
        f"the root at {tier3_dropbear.rootfs} contains {strays}, so the {daemon_arch} daemon "
        f"could have run the {session_arch} artifact through an interpreter copied INTO the "
        f"chroot. The tier's claim is that it does not need one — with a copy present, this "
        f"test can no longer tell the two apart"
    )

    handler = _BINFMT_ROOT / QEMU_HANDLER[TIER3_RELEASE.arch]
    registration = handler.read_text() if handler.exists() else ""
    flags = next(
        (line.partition(":")[2].strip() for line in registration.splitlines() if "flags:" in line),
        "",
    )
    assert "F" in flags, (
        f"this is a {daemon_arch} host running {TIER3_RELEASE.arch} artifacts, and "
        f"{handler} carries flags {flags!r}. Without `F` the kernel opens the interpreter "
        f"at exec time rather than holding it open from registration, so it is looked for "
        f"INSIDE the chroot — which contains one static BusyBox and nothing else — and "
        f"every Tier 3 session dies with ENOENT. Registration:\n{registration}"
    )


def test_an_interactive_login_shell_reaches_the_root_and_exits_cleanly(tier3_dropbear):
    """The wrapper's OTHER branch: a shell was asked for, and no command.

    Not a duplicate of the harness's pty test, which is the only other test in
    the tier to allocate one. That test sends a COMMAND over a pty, so
    dropbear fills `SSH_ORIGINAL_COMMAND` and the wrapper takes its first
    branch. Nothing anywhere took the second — the bare
    `exec /usr/sbin/chroot <root> /bin/sh` the wrapper falls through to when
    the test is false — and that branch is the one a person logging into a
    BusyBox device actually gets, as well as the only path on which ash's line
    editor (and its measured 1022-character limit) exists at all.

    The reply arrives through a tty, so it carries the echo of what was typed
    and ash's own prompt: `x86_64` is asserted as PRESENT rather than as the
    whole of stdout, and it can only have come from the command running,
    because the echo shows the request (`uname -m`) and not an answer.

    Mutation-verified by deleting the wrapper's last line — the fall-through
    `exec` — from :func:`~tests._fixtures.busybox_dropbear.write_login_wrapper`.
    This reds with `the interactive login shell exited 1;
    stdout='uname -m\\r\\nexit\\r\\n'` (the tty's echo of the keystrokes and
    nothing else), and it is the ONLY test that notices — not as a counted
    result but by construction: reaching the deleted line takes a client that
    asks for a shell and NO command, which is only :func:`interactive_login`,
    which only this test calls. Every other test in the tier's four Tier 3
    modules either sends a command (so dropbear fills `SSH_ORIGINAL_COMMAND`
    and the wrapper takes its first branch, the harness's pty test included)
    or never reaches the daemon at all. An earlier version of this note put a
    number on that — "the other eleven across both Tier 3 modules" — which was
    right for the two modules existing when it was written and went stale at
    four; the reason above does not.
    """
    result = interactive_login(tier3_dropbear, "uname -m\nexit\n")

    assert result.exit_status == 0, (
        f"the interactive login shell exited {result.exit_status}; stdout={result.stdout!r} "
        f"stderr={result.stderr!r}"
    )
    assert TIER3_RELEASE.arch in result.stdout, (
        f"an interactive shell came up and `uname -m` did not answer "
        f"{TIER3_RELEASE.arch!r} in it. Transcript: {result.stdout!r}"
    )


def test_sftp_and_scp_are_both_refused_inside_the_root(tier3_dropbear, tmp_path):
    """Neither transfer surface exists on the far side — the DEVICE's contract.

    A BusyBox device offers no sftp subsystem and no scp binary, which is why
    otto has a `shell` transfer backend at all. Packaged dropbear does not
    agree by default: it serves BOTH (see
    :func:`~tests._fixtures.busybox_dropbear.write_sftp_mask`), so a tier that
    left them reachable would stand in for a target strictly richer than the
    one it claims to model, and the backend that exists for this constraint
    would be exercised against a host that never needed it.

    Both are asserted through the ROOTFS's own diagnostic, not merely through
    a non-zero exit. `/bin/sh: ...: not found` is ash inside the chroot; the
    dev VM's login shell would have said `bash: line 1: ...: command not
    found`. That distinction is the whole discrimination — a refusal for some
    unrelated reason (a dead daemon, a rejected key) produces neither string.

    The two calls take DIFFERENT routes on purpose. `scp -O` forces the legacy
    protocol, so it reaches for a remote `scp` binary; plain `sftp` opens the
    subsystem, so it reaches for dropbear's compiled-in
    :data:`_SFTPSERVER_PATH`. A test of one leaves the other surface
    unmeasured.

    THIS TEST ALONE DOES NOT PROVE BOTH LEVERS ARE THERE, and it is the pair
    below that closes that — with the wrapper gone and the mask still in
    place, `scp -O` succeeds. Read them together, and note the measurement
    that makes the pair necessary rather than tidy: with the MASK bind removed
    from the argv and the wrapper left alone, THIS TEST STAYS GREEN. A tier
    guarded only from here would have gone on serving a real sftp server to
    any client that asked for the subsystem without the forced command.

    Mutation-verified by dropping `-c <wrapper>` from the argv: this reds with
    `scp -O SUCCEEDED against the tier` before it ever reaches the sftp half.
    """
    payload = tmp_path / "payload.txt"
    payload.write_text("tier 3 must not be able to move this\n")
    landed = tmp_path / "landed"

    scp = run_openssh_client(
        "scp",
        tier3_dropbear,
        "-O",
        str(payload),
        f"{tier3_dropbear.username}@127.0.0.1:{landed}",
    )

    assert scp.returncode != 0, f"scp -O SUCCEEDED against the tier: {scp.stdout!r}"
    assert "/bin/sh: scp: not found" in scp.stderr, (
        f"scp -O failed, but not by finding no `scp` inside the BusyBox root. "
        f"rc={scp.returncode} stderr={scp.stderr!r} — a `bash:` prefix means the session "
        f"never chrooted and the failure is the host's, not the root's"
    )
    assert not landed.exists(), f"scp -O reported failure and still wrote {landed}"

    sftp = run_openssh_client(
        "sftp",
        tier3_dropbear,
        "-b",
        "/dev/stdin",
        f"{tier3_dropbear.username}@127.0.0.1",
        stdin="ls\n",
    )

    assert sftp.returncode != 0, f"the sftp subsystem SERVED the tier: {sftp.stdout!r}"
    assert f"/bin/sh: {_SFTPSERVER_PATH}: not found" in sftp.stderr, (
        f"sftp failed, but not by finding no {_SFTPSERVER_PATH} inside the BusyBox root. "
        f"rc={sftp.returncode} stderr={sftp.stderr!r}"
    )


def test_the_mask_alone_still_refuses_sftp_while_scp_reaches_the_host(tier3_dropbear, tmp_path):
    """BOTH levers, each shown doing work the other does not.

    The refusal above has two possible authors and a single-lever test cannot
    say which: the sftp MASK (an empty, non-executable file bound over
    dropbear's compiled-in sftp-server) and the chroot WRAPPER (which puts the
    whole host filesystem out of reach). Remove one and the tier can still
    look green while quietly serving a surface the device it models does not
    have.

    So this removes the wrapper and keeps everything else, and the two
    surfaces then part company — measured, and the reason the pair exists:

    * `scp -O` SUCCEEDS, finding the HOST's `/usr/bin/scp` and landing a real
      file on the dev VM's real filesystem. The mask does nothing for scp.
      This is what would silently happen to the test above if the wrapper were
      dropped, so it is written down as an outcome rather than as a warning.
    * `sftp` is STILL refused, now by the mask, and with a different message:
      `Permission denied` from the host's bash (the file exists and is not
      executable) rather than `not found` from the root's ash. That is the
      mask's own signature, and dropping the mask bind turns it green.

    Mutation-verified in both directions. Removing the sftp mask bind from the
    argv reds HERE — first on the argv precondition, and with that suppressed
    on the outcome, `sftp SUCCEEDED: 'sftp> ls\\n'` — while the contract test
    above stays green, which is the entire argument for this test's existence.
    Removing `-c <wrapper>` reds the contract test above and reds this one at
    its own self-check (`no ` -c ` clause to remove`), since there is then no
    lever left for it to take away.
    """
    assert _SFTPSERVER_PATH in tier3_dropbear.argv(0)[-1], (
        f"the harness no longer binds the mask over {_SFTPSERVER_PATH}, so the message this "
        f"test matches is not the one the harness produces: {tier3_dropbear.argv(0)[-1]}"
    )

    work = tmp_path / "unwrapped"
    work.mkdir()
    keys = tier3_dropbear.keys
    daemon = _DropbearWithoutTheChrootWrapper(
        rootfs=tier3_dropbear.rootfs,
        keys=keys,
        # The session daemon's own keys and root, so the ONLY difference
        # between this daemon and the tier's is the removed clause.
        fake_home=bbd.write_fake_home(work / "home", keys.authorized_keys),
        group_file=bbd.write_group_without_tty(work / "group"),
        sftp_mask=bbd.write_sftp_mask(work / "sftp-mask"),
        login_wrapper=bbd.write_login_wrapper(work / "login.sh", tier3_dropbear.rootfs),
        log_path=work / "dropbear.log",
    )
    daemon.start(bbd.free_port())
    try:
        where = run_over_ssh(daemon, "[ -d /usr/bin ] && echo HOST-USERLAND || echo ROOTFS")
        assert where.stdout.strip() == "HOST-USERLAND", (
            f"this daemon is supposed to have NO chroot wrapper, and its session still "
            f"reports {where.stdout.strip()!r}. The injection did not take, so everything "
            f"below measures the real config and proves nothing about either lever"
        )

        payload = tmp_path / "payload.txt"
        payload.write_text("the host's own scp can move this\n")
        landed = work / "landed"
        scp = run_openssh_client(
            "scp", daemon, "-O", str(payload), f"{daemon.username}@127.0.0.1:{landed}"
        )

        assert scp.returncode == 0, (
            f"with the wrapper gone, `scp -O` was expected to find the HOST's scp and "
            f"succeed — that is the measured hazard the wrapper exists to close. It failed "
            f"instead: rc={scp.returncode} stderr={scp.stderr!r}. Either the host has no "
            f"scp on dropbear's default PATH, or something ELSE is now refusing scp, in "
            f"which case the test above is green for a reason nobody has identified"
        )
        assert landed.read_bytes() == payload.read_bytes(), (
            f"scp -O exited 0 without landing {payload} at {landed}"
        )

        sftp = run_openssh_client(
            "sftp", daemon, "-b", "/dev/stdin", f"{daemon.username}@127.0.0.1", stdin="ls\n"
        )

        assert sftp.returncode != 0, (
            f"with no wrapper, the MASK is the only thing left between this daemon and a "
            f"working sftp server — and sftp SUCCEEDED: {sftp.stdout!r}. The mask is not "
            f"bound over {_SFTPSERVER_PATH}, and the tier serves a transfer surface no "
            f"BusyBox device has"
        )
        assert "Permission denied" in sftp.stderr, (
            f"sftp was refused, but not by the mask. The mask is an EMPTY, NON-EXECUTABLE "
            f"file, so the exec fails with `Permission denied`; anything else means the "
            f"refusal came from somewhere this test is not measuring. "
            f"rc={sftp.returncode} stderr={sftp.stderr!r}"
        )
    finally:
        daemon.stop()


def test_without_the_mount_namespace_the_tiers_own_key_no_longer_authenticates(
    tier3_dropbear, tmp_path
):
    """Hermeticity, INJECTED: take the namespace away and auth must be REFUSED.

    The harness test already checks the standing property — the real
    `~/.ssh/authorized_keys` is not modified while the tier runs. That check
    would pass unchanged on a machine where Tier 3 never ran at all, so it
    cannot be the whole guard. This one makes the hostile condition instead of
    waiting for it: same daemon, same keys, same forced command, no
    `unshare` and no binds.

    THE SATISFIED PRE-STATE IS ASSERTED FIRST, twice, because a refusal is
    easy to obtain by accident. Through the namespace the tier's key
    authenticates; and the real file does not contain that key, so the only
    thing that could have authenticated it is the fake home bound over the
    passwd row's directory. With both established, the refusal below can only
    mean one thing.

    That refusal IS the differential the plan asks for between the
    in-namespace and outside reads of `<pw_dir>/.ssh/authorized_keys` —
    expressed as the outcome that matters rather than as a byte comparison,
    which would also differ on a machine where the tier had never run.

    Reads `PermissionDenied` specifically, not `asyncssh.Error`: a daemon that
    died at startup, or one whose host key asyncssh cannot handle, gives
    `ConnectionLost` — and that is the failure this tier's other levers
    produce, so accepting it here would make this test green for any of them.

    Mutation-verified in both directions, which for an injected guard is two
    different questions. Neutering the injection (the subclass returns the
    real argv) reds with `Failed: DID NOT RAISE PermissionDenied` — the guard
    notices when it stops injecting anything. Neutering the LEVER instead
    (dropping the fake-home bind from the harness argv, so the real file is
    what the tier reads) reds on the pre-state line above with
    `asyncssh.misc.PermissionDenied: Permission denied for user ... on host
    127.0.0.1` — the guard notices when the namespace stops being what
    authenticates.
    """
    assert run_over_ssh(tier3_dropbear, "true").exit_status == 0, (
        "the tier's key does not authenticate through the namespace, so the refusal this "
        "test injects below would prove nothing"
    )

    real = Path(bbd.login_passwd_entry().pw_dir) / ".ssh" / "authorized_keys"
    # The base64 body only: `ssh-keygen` appends `user@host`, and a comparison
    # that included the comment would pass on a file that carried the key
    # under a different one.
    blob = tier3_dropbear.keys.authorized_keys.read_text().split()[1]
    outside = real.read_text(errors="replace") if real.exists() else ""
    assert blob not in outside, (
        f"{real} already carries this tier's throwaway client key, so a daemon reading it "
        f"would authenticate too and the injection below could not fail. The tier must "
        f"never write there"
    )

    work = tmp_path / "unnamespaced"
    work.mkdir()
    keys = tier3_dropbear.keys
    daemon = _DropbearWithoutTheMountNamespace(
        rootfs=tier3_dropbear.rootfs,
        keys=keys,
        # Built and handed over exactly as the real fixture does. Every one of
        # them is INERT without the namespace to mount it in, and that is the
        # injection: the files are perfect and unreachable.
        fake_home=bbd.write_fake_home(work / "home", keys.authorized_keys),
        group_file=bbd.write_group_without_tty(work / "group"),
        sftp_mask=bbd.write_sftp_mask(work / "sftp-mask"),
        login_wrapper=bbd.write_login_wrapper(work / "login.sh", tier3_dropbear.rootfs),
        log_path=work / "dropbear.log",
    )
    daemon.start(bbd.free_port())
    try:
        with pytest.raises(asyncssh.PermissionDenied, match="Permission denied"):
            run_over_ssh(daemon, "true")
    finally:
        daemon.stop()
