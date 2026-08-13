"""Tier 3's prerequisite: a real ssh daemon to log into the BusyBox root with.

Tier 2 (:mod:`tests._fixtures.busybox_rootfs`) proves what a BusyBox userland
does with otto's payloads. It cannot prove what happens to them ON THE WAY:
everything it measures runs through a local `chroot`, so the "transport" is a
pipe this process owns and no byte ever crosses an ssh channel. Tier 3 closes
that by logging INTO the Tier 2 root over real ssh, against a throwaway
rootless daemon on loopback.

DROPBEAR, NOT OpenSSH, for two separate reasons — one about fidelity and one
measured about what is even possible here.

Fidelity: a BusyBox device runs dropbear, not OpenSSH, and the two disagree in
exactly the places this tier exists to measure — channel behaviour and the
legacy crypto an old device offers. A tier that proves otto works against
OpenSSH-plus-a-BusyBox-shell proves the half nobody was worried about.

Possibility: the OpenSSH fallback was measured, and rootless it dies with
`chroot: Operation not permitted` — no CAP_SYS_CHROOT. Making it work needs
the same unprivileged namespace the dropbear harness already builds, so the
"safer, more familiar" fallback buys nothing at all. Dropbear is not the
compromise here; it is the only one of the two that pays its way.

The daemon harness builds on the two constants below. This module's other job
is the one that has to exist BEFORE any of it: when dropbear is not installed,
say so BY NAME. Nothing here skips, for the reason recorded across this whole
tree — a skipped BusyBox tier and a passing one are the same line in a pytest
summary, which is how the coverage evaporates without anyone noticing.

FOUR LEVERS IN THE HARNESS ARE LOAD-BEARING AND EASY TO LOSE. Each was
measured on 2026-08-13 against `dropbear-bin 2022.83-4` on this aarch64 dev
VM, each is spelled out at its own site in :meth:`LoopbackDropbear.argv`, and
each fails in a DIFFERENT way — which is why "I smoke-tested it and it worked"
is not evidence that any of them is still there:

* `--keep-caps` — a STARTUP failure. Without it the first bind dies with
  `mount: /home/<user>: must be superuser to use mount.` and rc 32, so the
  daemon never binds a port. Verify this lever by its MESSAGE; a startup
  failure and a wedge look the same in a summary line.
* the RSA host key — a failure ONLY ON THE PRODUCT PATH. With an
  ed25519-only host key, `ssh(1)` connects perfectly while asyncssh 2.24 (the
  client otto itself uses) gets `ConnectionLost` and the dropbear child dies
  on `Failed assertion (rsa.c:164): key != NULL`. Hand-smoke-testing this
  tier with `ssh` therefore certifies a configuration in which every otto
  test would fail.
* the `tty`-less `/etc/group` — a PTY-ONLY failure. Exec channels stay green
  while every pty allocation kills its session with
  `chown(/dev/pts/N, 1000, 5) failed: Invalid argument`. Fatal, not a
  warning: dropbear calls `dropbear_exit` there.
* the sftp mask — a SILENT WIDENING. Packaged dropbear serves sftp AND scp
  (`SFTPSERVER_PATH=/usr/lib/sftp-server`, a symlink present here and on
  ubuntu-latest), so the spec's "dropbear buys no sftp" is FALSE as packaged
  and the tier would quietly measure a target richer than the device it
  stands in for.

`--map-root-user` is the trap next door: it makes the bind work without
`--keep-caps` and is therefore the obvious fix for the rc 32, but it also
makes `id -u` report 0, which moves dropbear onto its privileged code path —
the one a BusyBox device's unprivileged login never takes. Do not reach for
it.
"""

import asyncio
import contextlib
import ctypes
import os
import pwd
import shlex
import shutil
import signal
import socket
import subprocess
from dataclasses import dataclass
from pathlib import Path

import asyncssh

from otto.utils import WaitTimeoutError, wait_for

DROPBEAR = "/usr/sbin/dropbear"
"""The daemon, by ABSOLUTE path rather than through :func:`shutil.which`.

/usr/sbin is off a non-root login PATH on plenty of machines, and this tier
already has that scar: `chroot` lives there too, and inheriting the caller's
PATH killed the rootfs fixture with `unshare: failed to execute chroot: No
such file or directory` (rc=127) the moment a caller's PATH lacked it — see
:data:`tests._fixtures.busybox_rootfs._HOST_PATH`. A `which` lookup would
also make the gate ask a DIFFERENT question from the one the harness answers:
the harness execs this string, so this string is what must be checked.
"""

DROPBEARKEY = "/usr/bin/dropbearkey"
"""The host/client key generator, from the same package as :data:`DROPBEAR`.

Checked by the same gate even though it ships alongside the daemon: "the
daemon is here" and "the tier can run" are not the same claim, and a partial
install that satisfied only the first would fail later inside key generation,
with dropbearkey's own diagnostic instead of a named refusal.
"""

_APT_PACKAGE = "dropbear-bin"
"""The package that supplies both, named in the refusal so the remedy is typed.

Deliberately `-bin` and not `dropbear` or `dropbear-run` — the rationale lives
in the refusal message, where a reader who needs it will actually be standing.
"""

SSH_KEYGEN = "ssh-keygen"
"""OpenSSH's generator, for the CLIENT key only — and it is not interchangeable.

`dropbearkey` writes dropbear's own private-key format, which asyncssh cannot
read; `ssh-keygen` writes the OpenSSH format, which dropbear cannot load as a
host key. So the split is forced by the two consumers, not chosen: host keys
come from :data:`DROPBEARKEY` because the daemon reads them, and the client
key comes from here because asyncssh (and `ssh(1)`, and `scp`) read it.

`dropbearconvert` would collapse this to one package, and was rejected:
:mod:`tests.busybox.test_tier3_session`'s scp/sftp refusals drive the OpenSSH
CLIENT binaries anyway, so `openssh-client` is a prerequisite of this tier no
matter which generator writes the key — and a conversion step is one more
thing to be silently wrong about.

By NAME rather than absolute path, unlike :data:`DROPBEAR`: /usr/bin is on
every PATH worth the name, and the reason the daemon is spelled absolutely
(``/usr/sbin`` is off a non-root login PATH) does not apply here.
"""

_SSH_KEYGEN_APT_PACKAGE = "openssh-client"
"""Named in :func:`generate_keys`'s refusal, which is where its absence bites.

Deliberately NOT folded into :func:`require_dropbear`. That gate answers one
question — "did `dropbear-bin` arrive on this machine" — and it is asserted
against the Vagrantfile and the CI job that install exactly that package. A
second package in the same message would make a green gate mean two things
and a red one ambiguous about which line to go fix.
"""

MAX_UNAUTH_PER_IP = 5
"""Simultaneous PRE-AUTH connections dropbear serves from one IP. Measured.

Loopback makes the whole tier one IP, so this is a budget for the tier, not
per test. Measured 2026-08-13: connections 1-5 receive the
`SSH-2.0-dropbear_2022.83` banner and the 6th onward are reset having received
nothing — with NO server log line, which is the part that costs an afternoon.
A fan-out test that opens six sessions at once therefore fails as an
unexplained `ConnectionLost`, indistinguishable from the ed25519-only-host-key
failure above.

THE RESET IS EFFECTIVELY IMMEDIATE: ~0.0003s (re-measured 2026-08-13, 12
trials holding five pre-auth connections open and timing the sixth from
`connect()` to the empty read; median 0.00034s, range 0.00013-0.00053s). An
earlier note here said ~0.003s and the Tier 3 fidelity queue said 0.02-0.04s;
both were an order or two high, and the ~0.003s is very close to the time the
five SERVED connections take to produce a banner (median 0.0039s), which is
the likelier thing that was being timed.

DO NOT RE-DERIVE THIS NUMBER FROM A CONCURRENT FAN-OUT. Measured at fanout 8,
refused connections took a median 0.0088s and served ones 0.0074s — the two
are indistinguishable, because what dominates there is the fan-out's own
thread scheduling, not the daemon's decision. The sub-millisecond figure is
what the daemon costs; anything larger is the measurement's own overhead.

Not enforced here, because the harness cannot see how many connections its
callers hold. It is enforced by SCHEDULING instead: every Tier 3 module
carries ``pytest.mark.xdist_group("busybox_tier3")`` so the tier runs
sequentially on one worker, and `tests/busybox/conftest.py` fails any test
that reaches the daemon fixture without that pin having been honoured. A test
that deliberately fans out has to read this number.

CHANNELS are a different question with a different answer: 120 concurrent
`conn.run()` calls over ONE connection were measured fine (~2s wall). There
is no `MaxSessions` analogue in dropbear — the ceiling is on connections.
"""


class DropbearUnavailableError(RuntimeError):
    """Tier 3 has no ssh daemon to run here, and the message says which file.

    Raised rather than skipped, the binding rule for every refusal in this
    tree (see :mod:`tests._fixtures.busybox_rootfs`'s module docstring and
    :func:`tests._fixtures.busybox.require_interpreter`).
    """


def _unusable_reason(path: str) -> "str | None":
    """Why *path* cannot be executed, or ``None`` when it can.

    Two states, not one, because they have DIFFERENT REMEDIES and a gate that
    reports "missing" for both sends half its readers to apt for a problem apt
    cannot fix. `os.access(X_OK)` alone cannot tell them apart: a binary on a
    `noexec` mount and a binary that was never installed both answer False.
    That distinction has already cost this tree a diagnosis once, which is why
    :func:`tests._fixtures.busybox_rootfs._require_exec_mount` exists at all.

    A dangling symlink reports "absent" — `Path.exists` follows the link, so
    it answers about the target. That is the right answer for the reader too:
    the remedy for a link pointing at nothing is to install the package that
    puts a file there.

    Executability stays on :func:`os.access` rather than reading `st_mode`,
    because the question is "can THIS process exec it" — which depends on the
    caller's uid and gid and on the mount, not on the bits alone.
    """
    if not Path(path).exists():
        return "absent"
    if not os.access(path, os.X_OK):
        return "present but not executable"
    return None


def require_dropbear() -> None:
    """Raise :class:`DropbearUnavailableError` unless Tier 3's binaries can run.

    BOTH paths are examined before anything is raised, and every unusable one
    is named in the single message. The alternative — return on the first
    failure — turns one broken install into a sequence of runs that each
    reveal one more missing file, which is the shape
    :func:`tests._fixtures.busybox.require_interpreter` was written to avoid
    (it reports every missing binfmt handler at once rather than five
    identical ENOEXECs).

    The constants are read from the MODULE at call time, not captured as
    default arguments. That is what makes the guard testable: a default
    argument binds at import, so a test that rebinds
    :data:`DROPBEAR` to a nonexistent path would exercise the real, present
    binary instead and pass while proving nothing.
    """
    unusable = [(path, _unusable_reason(path)) for path in (DROPBEAR, DROPBEARKEY)]
    broken = [f"{path} ({reason})" for path, reason in unusable if reason is not None]
    if not broken:
        return

    raise DropbearUnavailableError(
        f"the BusyBox Tier 3 tier logs into the Tier 2 root over real ssh and needs "
        f"dropbear on this host; unusable here: {', '.join(broken)}.\n"
        f"    sudo apt update && sudo apt install {_APT_PACKAGE}\n"
        f"`{_APT_PACKAGE}` DELIBERATELY, not `dropbear` and not `dropbear-run`: the "
        f"`-bin` package ships only the binaries (dropbear, dbclient, dropbearkey, "
        f"dropbearconvert) and registers NO service — measured, `systemctl is-enabled "
        f"dropbear` reports `not-found` after installing it — so installing it starts "
        f"nothing listening on this machine. The tier brings up its own foreground "
        f"daemon on 127.0.0.1 at an ephemeral port and reaps it. The package is in "
        f"Ubuntu `universe`, and the index refresh is part of the instruction rather "
        f"than decoration: against a stale apt list the download 404s on the .deb and "
        f"reads exactly like 'no such package'. A path reported `present but not "
        f"executable` is NOT an apt problem — look at the mount options on that "
        f"filesystem first. On the otto dev VM the Vagrantfile's `dev-root` "
        f"provisioner installs this package; in CI the busybox job installs it "
        f"beside qemu-user-static."
    )


# ── Timeouts: RUNAWAY GUARDS, never discriminators ─────────────────────────
#
# Nothing in this tier asserts on elapsed time, so every bound below is as
# generous as it can be while still turning a wedge into a named failure
# instead of a session-wide hang. Sized against the measured cost of the call
# they bound (2026-08-13, this VM): key generation 0.01s per key with RSA the
# outlier at 0.20s, `start()` 0.05s to first accept, `stop()` 0.00s.
#
# They are spent in SESSION SETUP, not in a test body — see the Tier 3 note in
# the coupled-budget block at the top of `busybox_rootfs.py` for why that puts
# them outside the per-test SIGALRM window and inside `faulthandler_timeout`.

_KEYGEN_TIMEOUT_S = 30.0
"""Bound for ONE key generation. Three are run; RSA is the slow one at 0.20s.

Generous by two orders of magnitude on purpose: `dropbearkey -t rsa` draws
from the kernel CSPRNG, and a freshly booted CI runner with a cold entropy
pool is the one machine where this call is slow rather than instant."""

_READY_TIMEOUT_S = 15.0
"""Bound for the daemon to accept on its port. Measured at 0.05s.

Same value as the chaos suite's sshd for the same reason: it is long enough
that a loaded runner never trips it, and short enough that a daemon which
died at startup is reported with its log rather than hanging the session."""

_STOP_TIMEOUT_S = 10.0
"""Bound for each stage of the two-stage reap (SIGTERM, then SIGKILL).

Charged twice in the worst case. `stop()` measured at 0.00s: dropbear exits
the moment its process group is signalled."""

# prctl(2) PR_SET_PDEATHSIG — ask the kernel to signal this process when its
# parent dies.
_PR_SET_PDEATHSIG = 1


# `_die_with_parent` and `free_port` below are LIFTED VERBATIM from
# `tests/integration/chaos/_sshd.py`, docstrings included, rather than
# imported from it. Both hazards are identical here — this tier also spawns a
# foreground ssh daemon that holds a port and outlives a SIGKILLed worker —
# and the orphan story in the docstring is this same VM's.
#
# Copied rather than shared because the alternative is worse in a specific
# way: `tests/integration/chaos/` is the bed lane, whose conftest tree exists
# to drive real Vagrant/QEMU hosts, and importing from it would put a
# hostless, docker-free tier one import away from that tree's fixtures — the
# exact coupling `tests/busybox/conftest.py`'s docstring records paying for
# when this tier briefly lived under `tests/integration/` and inherited a
# docker sweep that SSHed to the lab. Fourteen duplicated lines is the
# cheaper of the two prices. If a third daemon harness appears, promote them
# to a shared module then; two is not yet a pattern.


def _die_with_parent() -> None:
    """Between fork and exec: arrange for the kernel to kill us with our parent.

    ``stop()`` runs in a ``finally`` and handles every exit the worker is alive
    for. It cannot handle the one where the worker is SIGKILLed — a stopped
    gate run, an OOM kill, ``kill -9`` on a wedged suite — because no Python
    finalizer runs at all. The daemon then reparents to init and keeps its
    port; pytest's numbered-dir rotation later deletes the directory holding
    its config, leaving an orphan with no visible owner. One was found on this
    VM two days after the run that spawned it, and the run that spawned it was
    a gate stopped by hand.

    So the kernel holds the other end. This is the same shape as the netcat
    listener's remote ``timeout`` cap: a bound that survives the death of the
    thing that would otherwise have done the cleaning up.

    Linux-only (the chaos lane already is — it shells out to ``sshd`` and
    reads ``/proc``). Best-effort by design: if ``prctl`` is unavailable this
    returns quietly and behaviour is exactly what it was before, because the
    ``finally`` remains the primary mechanism.

    The ``getppid`` re-check closes the race where the parent dies *between*
    the fork and the ``prctl`` call — the signal would already have been
    delivered and missed, so the child has to notice for itself.
    """
    parent = os.getppid()
    try:
        ctypes.CDLL("libc.so.6", use_errno=True).prctl(_PR_SET_PDEATHSIG, signal.SIGKILL)
    except (OSError, AttributeError):  # pragma: no cover — non-glibc / no prctl
        return
    if os.getppid() != parent:
        os._exit(1)


def free_port() -> int:
    """Reserve an ephemeral loopback port (bind/close; sequential suite makes the race moot)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@dataclass(frozen=True, slots=True)
class DropbearKeys:
    """The four key files one Tier 3 daemon needs, as paths.

    BOTH host keys, always. :data:`rsa` is not a fallback for old clients —
    it is the one asyncssh 2.24 needs in order to complete a handshake at all
    (see this module's docstring), so a caller that drops it gets a tier that
    `ssh(1)` can log into and otto cannot.
    """

    rsa: Path
    ed25519: Path
    client: Path
    authorized_keys: Path


def generate_keys(keys_dir: Path) -> DropbearKeys:
    """Write both host keys and a client keypair into *keys_dir*.

    Host keys are dropbear-format (:data:`DROPBEARKEY`), the client key is
    OpenSSH-format (:data:`SSH_KEYGEN`); see :data:`SSH_KEYGEN` for why that
    split is forced rather than chosen.

    The client key is ed25519 while the host keys include RSA, and that is not
    an inconsistency: the measured `rsa.c:164` assertion is about the key the
    SERVER offers, and pubkey auth with an ed25519 CLIENT key was measured
    working against the same daemon on the same day.
    """
    keygen = shutil.which(SSH_KEYGEN)
    if keygen is None:
        raise DropbearUnavailableError(
            f"the BusyBox Tier 3 tier needs `{SSH_KEYGEN}` to write the CLIENT key in the "
            f"OpenSSH format asyncssh reads — dropbearkey writes dropbear's own format, "
            f"which asyncssh cannot load — and it is not on PATH here.\n"
            f"    sudo apt update && sudo apt install {_SSH_KEYGEN_APT_PACKAGE}\n"
            f"This is a SEPARATE prerequisite from `{_APT_PACKAGE}`, which "
            f"`require_dropbear()` checks; the same package supplies the `ssh`, `scp` and "
            f"`sftp` clients the tier drives to prove those surfaces are refused."
        )

    keys_dir.mkdir(parents=True, exist_ok=True)
    rsa = keys_dir / "host_rsa"
    ed25519 = keys_dir / "host_ed25519"
    for path, kind in ((rsa, "rsa"), (ed25519, "ed25519")):
        subprocess.run(
            [DROPBEARKEY, "-t", kind, "-f", str(path)],
            check=True,
            capture_output=True,
            timeout=_KEYGEN_TIMEOUT_S,
        )

    client = keys_dir / "client"
    subprocess.run(
        [keygen, "-q", "-t", "ed25519", "-N", "", "-f", str(client)],
        check=True,
        capture_output=True,
        timeout=_KEYGEN_TIMEOUT_S,
    )
    return DropbearKeys(
        rsa=rsa, ed25519=ed25519, client=client, authorized_keys=client.with_suffix(".pub")
    )


def login_passwd_entry() -> pwd.struct_passwd:
    """This user's passwd row — THE thing dropbear consults, so the thing to bind.

    Not `$HOME`, and the difference is the whole hermeticity argument. Dropbear
    has no `AuthorizedKeysFile` option (checked against 2022.83: no such flag
    exists); it resolves the authenticating user with `getpwnam` and appends
    `/.ssh/authorized_keys` to that row's `pw_dir`. An environment variable it
    never reads is therefore the wrong bind target: on any machine where
    `$HOME` and `pw_dir` disagree, a bind over `$HOME` leaves the daemon
    reading the developer's REAL `~/.ssh/authorized_keys` while the harness
    reports success.

    The plan this tier was built from spells the bind `$HOME`, measured
    working — which it is, on every machine where the two agree. Binding
    `pw_dir` is the same mount on those machines and the correct one on the
    rest.
    """
    return pwd.getpwuid(os.getuid())


def write_fake_home(home: Path, authorized_keys: Path) -> Path:
    """Build a throwaway `$HOME` holding *authorized_keys*, and return it.

    Bound over the real home directory so the daemon's hardcoded
    `<pw_dir>/.ssh/authorized_keys` lands here instead. THE REAL FILE IS NEVER
    READ AND NEVER WRITTEN — that property is the reason the mount namespace
    exists, and it is asserted by
    `tests/busybox/test_tier3_session.py`'s hermeticity guard rather than
    assumed here.

    Over the HOME DIRECTORY, not over `~/.ssh`: `mount --bind` needs its
    target to already exist, and a fresh CI runner has no `~/.ssh` at all. The
    home directory always exists, and covering it hides any other ssh state
    the developer happens to have as well, so it is the more hermetic of the
    two as well as the one that works.

    Modes are set because dropbear checks them: a group- or world-writable
    home, `.ssh` or `authorized_keys` is refused with the key never tried,
    which surfaces as an ordinary auth failure.
    """
    ssh_dir = home / ".ssh"
    ssh_dir.mkdir(parents=True, exist_ok=True)
    installed = ssh_dir / "authorized_keys"
    installed.write_bytes(authorized_keys.read_bytes())
    installed.chmod(0o600)
    ssh_dir.chmod(0o700)
    home.chmod(0o700)
    return home


def write_group_without_tty(path: Path) -> Path:
    """Write an `/etc/group` with NO `tty` entry, and return *path*.

    This file exists to make one `getgrnam("tty")` fail. Dropbear's
    `pty_setowner` looks the group up and, when it finds it, chowns the
    freshly allocated pty to `(uid, tty_gid)`. Inside
    `unshare --map-current-user` only the invoking uid and gid are mapped, so
    gid 5 is unmapped, reads back as 65534, and the chown returns EINVAL —
    which dropbear treats as FATAL (`dropbear_exit`), not as a warning.
    Measured: `chown(/dev/pts/1, 1000, 5) failed: Invalid argument`, session
    killed, `ConnectionLost` at the client.

    With no `tty` row the lookup returns NULL and dropbear falls back to the
    user's own `pw_gid` — mapped, so the chown succeeds and the pty comes up.

    THE FAILURE IS PTY-ONLY. Exec channels never allocate a pty and stay
    perfectly green without this bind, so a tier whose tests all use
    `conn.run()` measures nothing about it. That asymmetry is why
    `test_tier3_harness.py` spends a pty round trip.

    The two rows kept are inert padding for readability, not requirements:
    nothing in this tier resolves either name.
    """
    path.write_text("root:x:0:\nnogroup:x:65534:\n")
    return path


def write_sftp_mask(path: Path) -> Path:
    """Write an empty, non-executable file to bind over `sftp-server`.

    PACKAGED DROPBEAR SERVES SFTP AND SCP. The spec's claim that choosing
    dropbear "buys no sftp" is true of the upstream default and false of
    `dropbear-bin 2022.83-4`, which is built with
    `SFTPSERVER_PATH=/usr/lib/sftp-server` and ships beside an OpenSSH
    `sftp-server` at exactly that path (a symlink to
    `/usr/lib/openssh/sftp-server`, present here and on ubuntu-latest). Left
    alone, the tier would stand in for a BusyBox device while offering two
    transfer surfaces no such device has.

    Empty and mode 0644 rather than a stub that exits non-zero: the point is
    that there is nothing executable behind the name, so any route that
    reaches it fails at exec rather than at a return code a caller might
    mistake for a real server's refusal.

    Note what the bind actually covers. `mount --bind` resolves the symlink,
    so the mount lands on `/usr/lib/openssh/sftp-server`; the effect through
    the name dropbear was built with is the same, and nothing else in the
    namespace wants that binary.

    THE MASK IS ONLY HALF THE GUARD. With the login wrapper removed, `scp -O`
    still finds the HOST's `scp` and succeeds — the wrapper's chroot is what
    makes both surfaces unreachable. A test that mutates one lever at a time
    passes green for the wrong reason; see `test_tier3_session.py`.
    """
    path.write_text("")
    path.chmod(0o644)
    return path


def write_login_wrapper(path: Path, rootfs: Path) -> Path:
    """Write the forced-command login shell that chroots into *rootfs*.

    Handed to dropbear as `-c`, so EVERY session runs it whatever the client
    asked for: an interactive login gets `/bin/sh` inside the root, and a
    remote command arrives in `SSH_ORIGINAL_COMMAND` and is run by that same
    ash. This is what makes the tier measure a BusyBox userland instead of the
    dev VM's GNU one, and — with the sftp mask — what puts sftp and scp out of
    reach.

    PATH IS INJECTED BEFORE THE CHROOT, NOT AFTER, because `chroot` passes the
    environment straight through and there is no applet to correct it with on
    the other side. Same reasoning, and the same scar, as
    :func:`tests._fixtures.busybox_rootfs.run_in_rootfs`: `HOME=/tmp` because
    the real home does not exist inside the root, and `/usr/sbin/chroot` is
    spelled absolutely because the PATH this line just set does not contain
    it.

    The `[ -n ... ] && exec` line is a deliberate one-liner rather than an
    `if`: if the test is false the `&&` short-circuits, the line's status is
    1, and execution falls through to the interactive `exec` on the next line
    — which is the intended behaviour and not, as an `if`-less reading might
    suggest, an error path.
    """
    root = shlex.quote(str(rootfs))
    path.write_text(
        "#!/bin/sh\n"
        "PATH=/bin; HOME=/tmp; export PATH HOME\n"
        f'[ -n "${{SSH_ORIGINAL_COMMAND:-}}" ] && exec /usr/sbin/chroot {root} '
        '/bin/sh -c "$SSH_ORIGINAL_COMMAND"\n'
        f"exec /usr/sbin/chroot {root} /bin/sh\n"
    )
    path.chmod(0o755)
    return path


class LoopbackDropbear:
    """A throwaway rootless dropbear on 127.0.0.1, logging into a BusyBox root.

    ``start()`` blocks until the port accepts or the child is reported dead
    with its log. ``stop()`` reaps the whole process GROUP, because dropbear
    forks per connection and terminating the leader alone leaves the sessions.
    """

    def __init__(
        self,
        *,
        rootfs: Path,
        keys: DropbearKeys,
        fake_home: Path,
        group_file: Path,
        sftp_mask: Path,
        login_wrapper: Path,
        log_path: Path,
    ) -> None:
        self.rootfs = rootfs
        self.keys = keys
        self.username = login_passwd_entry().pw_name
        self.port: "int | None" = None
        self._fake_home = fake_home
        self._group_file = group_file
        self._sftp_mask = sftp_mask
        self._login_wrapper = login_wrapper
        self._log_path = log_path
        self._proc: "subprocess.Popen[bytes] | None" = None

    @classmethod
    def build(cls, workdir: Path, rootfs: Path) -> "LoopbackDropbear":
        """Assemble every file the daemon needs under *workdir*, unstarted.

        The pieces stay separately callable — a test that wants to prove one
        lever writes only that file — and this is the one call a fixture
        needs.
        """
        workdir.mkdir(parents=True, exist_ok=True)
        keys = generate_keys(workdir / "keys")
        return cls(
            rootfs=rootfs,
            keys=keys,
            fake_home=write_fake_home(workdir / "home", keys.authorized_keys),
            group_file=write_group_without_tty(workdir / "group"),
            sftp_mask=write_sftp_mask(workdir / "sftp-mask"),
            login_wrapper=write_login_wrapper(workdir / "login.sh", rootfs),
            log_path=workdir / "dropbear.log",
        )

    def argv(self, port: int) -> "list[str]":
        """The measured command line, with every clause's failure named.

        Reproduced from the phase-5 spike's measurement of 2026-08-13 rather
        than assembled from the manpages, and every element below has a
        failure attached. Re-deriving it "more cleanly" is how the four
        levers in this module's docstring get lost one at a time.
        """
        home = shlex.quote(str(login_passwd_entry().pw_dir))
        script = " && ".join(
            [
                # Hermeticity. Over the passwd row's home, since that is what
                # dropbear's getpwnam-based lookup reads — see
                # `login_passwd_entry`. The whole namespace exists for this
                # one line.
                f"mount --bind {shlex.quote(str(self._fake_home))} {home}",
                # Fatal pty chown EINVAL on unmapped gid 5 without this.
                f"mount --bind {shlex.quote(str(self._group_file))} /etc/group",
                # Packaged dropbear otherwise serves sftp AND scp.
                f"mount --bind {shlex.quote(str(self._sftp_mask))} /usr/lib/sftp-server",
                # -F: foreground, so this process IS the daemon and the pid we
                #     hold is the pid we reap.
                # -E: log to stderr, which is the file `start()` captures and
                #     quotes back on a startup failure.
                # -s: no password auth. Belt and braces with the forced
                #     command: this user has a password on some machines.
                # -r rsa FIRST and ALWAYS: asyncssh gets `ConnectionLost` and
                #     the child dies on `Failed assertion (rsa.c:164)` against
                #     an ed25519-only host key, while `ssh(1)` stays green.
                # -c: the forced command, i.e. the chroot wrapper.
                # No -P: the default pidfile is unwritable here and is never
                #     created under -F anyway, so asking for one only adds a
                #     way to fail.
                (
                    f"exec {DROPBEAR} -F -E -s -p 127.0.0.1:{port} "
                    f"-r {shlex.quote(str(self.keys.rsa))} "
                    f"-r {shlex.quote(str(self.keys.ed25519))} "
                    f"-c {shlex.quote(str(self._login_wrapper))}"
                ),
            ]
        )
        return [
            # --mount: the namespace the three binds live in.
            # --map-current-user: uid stays 1000 inside. NOT --map-root-user,
            #     which also makes the binds work and moves dropbear onto its
            #     privileged code path — the branch a device's login never
            #     takes.
            # --keep-caps: without it the FIRST bind fails with
            #     `mount: <home>: must be superuser to use mount.` and rc 32,
            #     because execve drops a non-root process's permitted caps and
            #     this flag is what raises them into the ambient set instead.
            #     The same ambient caps are what let the login wrapper chroot.
            "unshare",
            "--mount",
            "--map-current-user",
            "--keep-caps",
            "--",
            "/bin/sh",
            "-c",
            script,
        ]

    @property
    def pid(self) -> "int | None":
        """The daemon's pid, which is also its process-GROUP id; None if unstarted.

        Exposed so a caller can verify the reap the way this tier insists on
        verifying it — `os.kill(pid, 0)` against a CAPTURED pid, never a name
        match. `pkill -f dropbear` matches the shell that invoked the run.

        Group id as well as pid because `start()` passes
        `start_new_session=True`: the leader's pid IS the pgid, which is what
        makes `os.killpg(self.pid, ...)` reach the per-connection forks.
        """
        return None if self._proc is None else self._proc.pid

    def log_text(self) -> str:
        """Whatever the daemon has written so far; `''` before it starts."""
        if not self._log_path.exists():
            return ""
        return self._log_path.read_text(errors="replace")

    def start(self, port: int) -> None:
        """Launch the daemon and block until 127.0.0.1:*port* accepts."""
        log = self._log_path.open("wb")
        try:
            self._proc = subprocess.Popen(
                self.argv(port),
                stdout=log,
                stderr=log,
                # dropbear forks per connection; the group is what `stop()`
                # signals, so the child has to lead one.
                start_new_session=True,
                preexec_fn=_die_with_parent,  # noqa: PLW1509 — see _die_with_parent: the point is that it runs in the CHILD, and the fork-safety caveat does not apply to a bare prctl+getppid
            )
        finally:
            log.close()  # the child holds its own fd now
        self.port = port

        proc = self._proc

        def accepting() -> bool:
            if proc.poll() is not None:
                # A STARTUP failure, and this is the message that names it.
                # `--keep-caps` and a bad key path both land here, and neither
                # says anything at all through the port — so the log is quoted
                # rather than summarised, and rc travels with it.
                raise DropbearUnavailableError(
                    f"the Tier 3 dropbear died at startup (rc={proc.returncode}); "
                    f"log:\n{self.log_text()}"
                )
            try:
                # One pre-auth connection for the length of this probe. Counts
                # against MAX_UNAUTH_PER_IP while it is open, which is why it
                # is closed immediately rather than held.
                with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                    return True
            except OSError:
                return False

        never_bound = (
            f"the Tier 3 dropbear is alive but never accepted on 127.0.0.1:{port} "
            f"after {_READY_TIMEOUT_S}s; log:\n{self.log_text()}"
        )
        try:
            wait_for(accepting, _READY_TIMEOUT_S, interval=0.05, on_timeout=never_bound)
        except WaitTimeoutError:
            # Alive but never bound: reap before raising, or a wedged daemon
            # outlives the fixture — the caller's `finally` only runs once
            # `start()` has RETURNED.
            self._terminate()
            raise DropbearUnavailableError(never_bound) from None

    def stop(self) -> None:
        """Reap the daemon and every session it forked."""
        self._terminate()
        self._proc = None
        self.port = None

    def _terminate(self) -> None:
        """Two-stage escalation, shared by `stop()` and `start()`'s bind timeout.

        BY PROCESS GROUP, BY CAPTURED PID. Never by name: `pkill -f dropbear`
        matches the shell that invoked the run as readily as the daemon. The
        group is what carries dropbear's per-connection forks, and the leader
        alone leaves them holding the port.
        """
        proc = self._proc
        if proc is None:
            return
        # Suppressed, not handled: a group that is already gone is the
        # outcome this method exists to produce.
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, signal.SIGTERM)
        try:
            proc.wait(timeout=_STOP_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(proc.pid, signal.SIGKILL)
            proc.wait(timeout=_STOP_TIMEOUT_S)


async def run_over_ssh_async(
    daemon: LoopbackDropbear, command: str, **kwargs
) -> asyncssh.SSHCompletedProcess:
    """Run *command* over one asyncssh connection to *daemon* and return the result.

    ASYNCSSH, NOT `ssh(1)`, EVERYWHERE IN THIS TIER. This is otto's own
    client, and it is the one that fails against an ed25519-only host key
    while `ssh(1)` connects happily — so a harness exercised through the
    command-line client certifies a configuration in which the product cannot
    talk to the target at all. Measured: `ConnectionLost`, with the daemon's
    child dying on `Failed assertion (rsa.c:164): key != NULL`.

    One connection per call, closed on the way out. That keeps every test's
    pre-auth demand at exactly one against the measured
    :data:`MAX_UNAUTH_PER_IP` of 5.

    `known_hosts=None` because the host key is generated per session and there
    is nothing to have known. `check=False` so a failing command reaches the
    assertion as data rather than as an exception with the output buried.

    Lives HERE, beside the daemon it drives, rather than in the test module
    that first needed it. It was written in `tests/busybox/test_tier3_harness.py`
    and imported out of it by the second consumer, which made a test file a
    fixture module for its siblings: `pytest -k` on the harness would drag the
    import in, and deleting a test would break the tier. Two consumers is where
    that shape is still cheap to undo, so it was undone.

    THE COROUTINE IS THE PRIMITIVE and :func:`run_over_ssh` is the wrapper,
    not the other way round. Tier 3's first two modules are synchronous and
    reach for the wrapper; `test_tier3_shell_transfer.py` drives otto's own
    async API, and inside a running loop `asyncio.run` raises
    `RuntimeError: asyncio.run() cannot be called from a running event loop`
    — so an async caller needs the coroutine, and a sync caller cannot be
    given one. Both spellings, one implementation.
    """
    async with asyncssh.connect(
        "127.0.0.1",
        port=daemon.port,
        username=daemon.username,
        client_keys=[str(daemon.keys.client)],
        known_hosts=None,
    ) as conn:
        return await conn.run(command, check=False, **kwargs)


def run_over_ssh(daemon: LoopbackDropbear, command: str, **kwargs) -> asyncssh.SSHCompletedProcess:
    """:func:`run_over_ssh_async` for a caller with no event loop of its own."""
    return asyncio.run(run_over_ssh_async(daemon, command, **kwargs))
