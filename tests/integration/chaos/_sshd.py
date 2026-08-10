"""Throwaway non-root sshd on 127.0.0.1 for the tier-2 chaos suite.

Everything (host key, client key, authorized_keys, config, logs) lives in a
tmp directory owned by the test session. The daemon runs foreground
(``sshd -D -e``) as the current user, pubkey-auth only — hermetic on the
dev VM and on ubuntu-latest runners alike, no sudo, no system state.
"""

import ctypes
import os
import shutil
import signal
import socket
import subprocess
from pathlib import Path

from otto.utils import WaitTimeoutError, wait_for

_SSHD = shutil.which("sshd") or "/usr/sbin/sshd"
_READY_TIMEOUT = 15.0

# prctl(2) PR_SET_PDEATHSIG — ask the kernel to signal this process when its
# parent dies.
_PR_SET_PDEATHSIG = 1


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


def generate_keypairs(keys_dir: Path) -> "tuple[Path, Path]":
    """ssh-keygen an ed25519 host key and client key; build authorized_keys.

    Returns (host_key, client_key) private-key paths.
    """
    keys_dir.mkdir(parents=True, exist_ok=True)
    host_key = keys_dir / "host_key"
    client_key = keys_dir / "client_key"
    for key in (host_key, client_key):
        subprocess.run(
            ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)],
            check=True,
            capture_output=True,
        )
    authorized = keys_dir / "authorized_keys"
    authorized.write_bytes((client_key.with_suffix(".pub")).read_bytes())
    authorized.chmod(0o600)
    return host_key, client_key


def write_sshd_config(
    cfg_path: Path, *, port: int, host_key: Path, authorized_keys: Path, user: str
) -> Path:
    """Write a minimal non-root sshd config (pubkey only, loopback only)."""
    cfg_path.write_text(
        f"""\
ListenAddress 127.0.0.1:{port}
HostKey {host_key}
PidFile none
UsePAM no
StrictModes no
PasswordAuthentication no
KbdInteractiveAuthentication no
PubkeyAuthentication yes
AuthorizedKeysFile {authorized_keys}
AllowUsers {user}
Subsystem sftp internal-sftp
LogLevel VERBOSE
"""
    )
    return cfg_path


class LoopbackSshd:
    """Foreground sshd child; ``start()`` blocks until the port accepts."""

    def __init__(self, config: Path, log_path: Path) -> None:
        self._config = config
        self._log_path = log_path
        self._proc: "subprocess.Popen[bytes] | None" = None

    def start(self, port: int) -> None:
        log = self._log_path.open("wb")
        try:
            self._proc = subprocess.Popen(
                [_SSHD, "-D", "-e", "-f", str(self._config)],
                stdout=log,
                stderr=log,
                preexec_fn=_die_with_parent,  # noqa: PLW1509 — see _die_with_parent: the point is that it runs in the CHILD, and the fork-safety caveat does not apply to a bare prctl+getppid
            )
        finally:
            log.close()  # sshd holds its own fd now

        def accepting() -> bool:
            if self._proc.poll() is not None:
                raise RuntimeError(
                    f"loopback sshd died at startup (rc={self._proc.returncode}); "
                    f"log:\n{self._log_path.read_text(errors='replace')}"
                )
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                    return True
            except OSError:
                return False

        never_bound = f"loopback sshd not accepting on 127.0.0.1:{port} after {_READY_TIMEOUT}s"
        try:
            wait_for(accepting, _READY_TIMEOUT, interval=0.05, on_timeout=never_bound)
        except WaitTimeoutError:
            # Still alive but never bound: tear it down before raising, or a hung child
            # outlives the fixture (conftest's stop() only runs once start() *returns*).
            self._terminate()
            raise RuntimeError(never_bound) from None

    def stop(self) -> None:
        self._terminate()
        self._proc = None

    def _terminate(self) -> None:
        """Two-stage escalation shared by ``stop()`` and the start() bind-timeout path."""
        proc = self._proc
        if proc is None:
            return
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)
