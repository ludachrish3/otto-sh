"""The known-hosts store: SSH host keys recorded and checked by otto host ID.

otto keeps host keys in the user's standard OpenSSH ``known_hosts`` file
(:attr:`~otto.host.options.SshOptions.known_hosts`, default
``~/.ssh/known_hosts``), keyed by the host ID alone: ``router1 ssh-ed25519
AAAA...``, or ``[router1]:2222 ...`` for a non-22 port. Addresses repeat
between labs and a tunnelled host's peer address is its hop's, so an entry
keyed by address (one the user's own ``ssh`` wrote) never matches here.

Parsing and matching are asyncssh's (:func:`asyncssh.import_known_hosts`):
hashed entries, wildcards, ``@cert-authority`` and ``@revoked`` behave as
asyncssh defines them. What this module adds is the lookup by name only,
OpenSSH's choice of the deciding entry (bare on port 22, ``[alias]:port``
only on any other port), revocations counted under every form that can name
the port (``[alias]:22`` included), the cross-process lock around every
write, and the forget filter.

This module imports asyncssh, so a host class reaches it lazily, inside the
method that needs it.
"""

import contextlib
import enum
import errno
import fcntl
import os
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

from asyncssh import SSHKey, SSHKnownHosts, import_known_hosts
from asyncssh.public_key import KeyImportError, import_certificate_subject

from ..utils import atomic_write_bytes
from .errors import (
    ForgetResult,
    HostKeyChangedError,
    HostKeyNotCertifiedError,
    HostKeyRevokedError,
    KnownHostsError,
    KnownHostsUnwritableError,
)
from .options import SshOptions

__all__ = [
    "ForgetResult",
    "HostKeyMatch",
    "HostKeyVerdict",
    "KnownHosts",
    "entry_name",
    "known_hosts_path",
]

_DEFAULT_PORT = 22

_PROBE_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIBZoRdbnUrqpjyrl/3DmwDRTB4NwEAtnZOY2Ffy8e5p7"
"""A valid public key that stands in for an X.509 subject when asking asyncssh
whether a subject line's host patterns apply (:meth:`KnownHosts._refuse_subject_lines`).
It is never written to a file and never trusted."""


def known_hosts_path(options: SshOptions) -> Path:
    """Return the known-hosts file *options* names, with ``~`` expanded.

    The one place the file is resolved: the connection's host-key check,
    the record after a first use, and ``forget-key`` all call this, so they
    can never disagree about which file they mean. Nothing from
    ``~/.ssh/config`` (``UserKnownHostsFile``, ``GlobalKnownHostsFile``) is
    consulted.
    """
    return Path(options.known_hosts).expanduser()


def entry_name(alias: str, port: int) -> str:
    """Return the entry that decides *alias*'s key on *port*: ``alias`` or ``[alias]:port``.

    The one place that decision is made, following OpenSSH: port 22 is the
    bare host ID, any other port only its ``[alias]:port`` form. It is the
    name :meth:`KnownHosts.record` writes and the entry every check reads.
    """
    return alias if port == _DEFAULT_PORT else _bracketed_name(alias, port)


def _bracketed_name(alias: str, port: "int | str") -> str:
    """Return OpenSSH's bracketed form of *alias* on *port*: ``[alias]:port``.

    The one place the form is spelled. It decides a non-22 port
    (:func:`entry_name`), revokes on any port (``[alias]:22`` included, which
    OpenSSH never reads as the deciding entry), and is what ``forget-key``
    removes on every port.
    """
    return f"[{alias}]:{port}"


class HostKeyMatch(NamedTuple):
    """The keys a known-hosts file holds for one host, in asyncssh's match-result order.

    A ``NamedTuple`` because asyncssh's ``known_hosts`` callable must return
    this seven-part sequence; the names are asyncssh's own.
    """

    host_keys: Sequence[SSHKey]
    ca_keys: Sequence[SSHKey]
    revoked_keys: Sequence[SSHKey]
    x509_certs: Sequence[object]
    revoked_certs: Sequence[object]
    x509_subjects: Sequence[object]
    revoked_subjects: Sequence[object]


class HostKeyVerdict(enum.Enum):
    """What the file says of one key presented for one host ID: :meth:`KnownHosts.judge`."""

    TRUSTED = "trusted"
    """The deciding entry holds the key."""

    FIRST_USE = "first use"
    """Trusted on first use and recorded, since the deciding entry holds no key."""

    CHANGED = "changed"
    """The deciding entry holds other plain keys, not this one."""

    NOT_CERTIFIED = "not certified"
    """The deciding entry holds no plain key, and an ``@cert-authority`` line
    covers the host: it is known by its CA, so only that CA's certificates are
    trusted, never a plain key on first use."""

    REVOKED = "revoked"
    """The file revokes the key, under the bare or the bracketed name; this wins
    over an entry that also holds it."""


@dataclass(frozen=True)
class _Line:
    """One known-hosts line split into its marker, host patterns and key data.

    The split is the one asyncssh's own loader applies (an optional
    ``@marker``, then the comma-separated host field, then the key); what
    the key data means is still left to asyncssh.
    """

    marker: str | None
    patterns: list[str]
    data: str

    @classmethod
    def split(cls, line: str) -> "_Line | None":
        """Split *line*, or return ``None`` for a blank line, a comment or a malformed line."""
        text = line.strip()
        if not text or text.startswith("#"):
            return None
        marker: str | None = None
        try:
            if text.startswith("@"):
                marker, pattern, data = text[1:].split(None, 2)
            else:
                pattern, data = text.split(None, 1)
        except ValueError:
            return None
        return cls(marker=marker, patterns=pattern.split(","), data=data)


def _unwritable(path: Path, err: OSError) -> KnownHostsUnwritableError:
    return KnownHostsUnwritableError(
        f"cannot write the known-hosts file {path} ({err.strerror or err}); set "
        'ssh_options known_hosts = "<writable path>", or turn host-key checking off '
        "with verify_host_key = false"
    )


def _matches(parsed: SSHKnownHosts, name: str) -> HostKeyMatch:
    """Return asyncssh's match for *name* alone: no address, no port fallback."""
    return HostKeyMatch(*parsed.match(name, "", None))


def _verdict(found: HostKeyMatch, key: SSHKey) -> HostKeyVerdict:
    """Judge *key* against what the file holds for one host (:meth:`KnownHosts.judge`)."""
    if key in found.revoked_keys:
        return HostKeyVerdict.REVOKED
    if key in found.host_keys:
        return HostKeyVerdict.TRUSTED
    if found.host_keys:
        return HostKeyVerdict.CHANGED
    if found.ca_keys:
        return HostKeyVerdict.NOT_CERTIFIED
    return HostKeyVerdict.FIRST_USE


class KnownHosts:
    """One known-hosts file, read and written by otto host ID.

    Reading never takes the lock and never writes: a read-only file whose
    entries all match keeps working. Every write (:meth:`record`,
    :meth:`forget`) holds an exclusive ``flock`` on ``known_hosts.lock``
    beside the file (beside its target, when the file is a symlink), so two
    otto processes cannot both record a first-use key.

    Which entry decides follows OpenSSH: on port 22 the bare ``alias``
    entry, on any other port the ``[alias]:port`` entry only, with no
    fallback to the bare one (:func:`entry_name` names it, the same name
    :meth:`record` writes). A revocation counts under either form.
    """

    def __init__(self, path: Path) -> None:
        self.path = path

    # -- reading ------------------------------------------------------------

    def _read(self) -> str:
        try:
            return self.path.read_text()
        except FileNotFoundError:
            return ""
        except OSError as err:
            raise KnownHostsError(
                f"cannot read the known-hosts file {self.path} ({err.strerror or err})"
            ) from err

    def _lookup(self, text: str, alias: str, port: int) -> HostKeyMatch:
        """Return what *text* holds for *alias* on *port*: the one lookup every check uses.

        Trusted keys, CA keys and certificates come from the deciding entry
        (:func:`entry_name`); revocations are the union of the deciding, the
        bare and the bracketed ``[alias]:port`` form, so a bare
        ``@revoked`` line also revokes on another port and an
        ``@revoked [alias]:22`` line on port 22. asyncssh's own lookup instead
        lets one form's result replace the other's, revocations included,
        hence a lookup per form here.

        Raises:
            KnownHostsError: *text* holds a line asyncssh cannot parse, or an
                X.509 subject line that applies to *alias* (see
                :meth:`_refuse_subject_lines`).
        """
        names = list(dict.fromkeys([entry_name(alias, port), alias, _bracketed_name(alias, port)]))
        self._refuse_subject_lines(text, names)
        try:
            parsed = import_known_hosts(text)
        except ValueError as err:
            raise KnownHostsError(f"{self.path}: {err}") from err
        found = [_matches(parsed, name) for name in names]
        deciding = found[0]
        return HostKeyMatch(
            host_keys=deciding.host_keys,
            ca_keys=deciding.ca_keys,
            revoked_keys=[key for r in found for key in r.revoked_keys],
            x509_certs=deciding.x509_certs,
            revoked_certs=[cert for r in found for cert in r.revoked_certs],
            x509_subjects=[],
            revoked_subjects=[],
        )

    def _refuse_subject_lines(self, text: str, names: list[str]) -> None:
        """Refuse an X.509 subject line that would decide or revoke for *names*, by line number.

        asyncssh reads such a line only when its X.509 support is installed
        (and skips it silently otherwise), and a result handed back to it
        through a ``known_hosts`` callable cannot carry one either way, so
        the line would drop out of the check. Whether a line applies is
        asked of asyncssh with the subject swapped for a plain key, so its
        host patterns and marker are judged exactly as for any other line:
        a revocation under any of *names*, a trusted line under the deciding
        name (``names[0]``). Another host's subject line is no concern here.
        """
        for number, line in enumerate(text.splitlines(), start=1):
            entry = _Line.split(line)
            if entry is None:
                continue
            try:
                import_certificate_subject(entry.data)
            except KeyImportError:
                continue
            marker = f"@{entry.marker} " if entry.marker else ""
            try:
                probe = import_known_hosts(f"{marker}{','.join(entry.patterns)} {_PROBE_KEY}")
            except ValueError:
                continue
            revokes = any(_matches(probe, name).revoked_keys for name in names)
            decides = _matches(probe, names[0])
            if revokes or decides.host_keys or decides.ca_keys:
                raise KnownHostsError(
                    f"{self.path}:{number}: X.509 subject lines are not supported in "
                    f"otto's known-hosts file: {line.strip()}"
                )

    def match(self, alias: str, port: int | None) -> HostKeyMatch:
        """Return the keys the file holds for *alias* on *port*, matched by name only.

        *port* ``None`` is asyncssh's value for the default port and means 22.
        On port 22 the bare ``alias`` entry decides; on any other port only
        ``[alias]:port`` does, as in OpenSSH. Revoked keys come from the bare
        and the bracketed form alike, so a revocation under either is
        honoured.

        Raises:
            KnownHostsError: the file cannot be read, a line cannot be parsed,
                or an X.509 subject line applies to *alias*.
        """
        return self._lookup(self._read(), alias, _DEFAULT_PORT if port is None else port)

    def judge(self, alias: str, port: int, key: SSHKey) -> HostKeyVerdict:
        """Judge *key*, presented by *alias* on *port*, against the file as it is now.

        The one decision both the handshake
        (:meth:`~otto.host.host_key_client.HostKeyClient.validate_host_public_key`)
        and :meth:`record` make. The handshake reads the file again here
        rather than trusting what asyncssh read when the connection opened: a
        concurrent first contact may have recorded this same key since, which
        is trusted, not a changed key.

        Raises:
            KnownHostsError: the file cannot be read or parsed.
        """
        return _verdict(self._lookup(self._read(), alias, port), key)

    # -- writing ------------------------------------------------------------

    def _target(self) -> Path:
        """Return the real file behind :attr:`path`: a symlink is edited at its target."""
        return Path(os.path.realpath(self.path))

    @contextlib.contextmanager
    def _locked(self) -> Iterator[None]:
        """Hold the exclusive cross-process lock beside the file, creating the directory."""
        target = self._target()
        try:
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            fd = os.open(target.with_name(target.name + ".lock"), os.O_RDWR | os.O_CREAT, 0o600)
        except OSError as err:
            raise _unwritable(self.path, err) from err
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            try:
                yield
            finally:
                # Explicit: a child forked while the lock was held shares this
                # open file description, so closing our descriptor alone would
                # leave the lock held until the child exits.
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def record(self, alias: str, port: int, key: SSHKey) -> None:
        """Record *key* as *alias*'s key on *port*, unless the file already holds it.

        The line written is the deciding entry's name (:func:`entry_name`):
        ``alias <key>`` for port 22 and ``[alias]:port <key>`` otherwise. Each
        read is judged through the one lookup the handshake uses
        (:meth:`match`), revocations included. Under the lock the file is read
        again, so a key recorded or revoked meanwhile (by another process, or
        by hand) is seen: a revoked key is refused, the same key means nothing
        to write, a different key is refused. A missing directory is created
        0700 and a missing file 0600.

        Raises:
            HostKeyRevokedError: the file revokes *key* for *alias*.
            HostKeyChangedError: the deciding entry now holds a different key.
            HostKeyNotCertifiedError: an ``@cert-authority`` line now covers the host.
            KnownHostsUnwritableError: the key must be written and the file
                (or its directory, or the lock beside it) cannot be, or the
                line could not be written whole.
            KnownHostsError: the file cannot be read or parsed.
        """
        if self._refuse_or_held(self.judge(alias, port, key), alias, port):
            return
        with self._locked():
            text = self._read()
            verdict = _verdict(self._lookup(text, alias, port), key)
            if self._refuse_or_held(verdict, alias, port):
                return
            name = entry_name(alias, port)
            public = key.export_public_key("openssh").decode().strip()
            line = f"{name} {public}\n"
            if text and not text.endswith("\n"):
                line = "\n" + line
            self._append(line)

    def _refuse_or_held(self, verdict: HostKeyVerdict, alias: str, port: int) -> bool:
        """Return whether *verdict* means the key is already held; raise for a refusal.

        Raises:
            HostKeyRevokedError: the file revokes the key.
            HostKeyChangedError: the deciding entry holds a different key.
            HostKeyNotCertifiedError: an ``@cert-authority`` line covers the host.
        """
        name = entry_name(alias, port)
        if verdict is HostKeyVerdict.REVOKED:
            raise HostKeyRevokedError(f"{self.path} revokes the key offered for {name}")
        if verdict is HostKeyVerdict.CHANGED:
            raise HostKeyChangedError(f"{self.path} holds a different key for {name}")
        if verdict is HostKeyVerdict.NOT_CERTIFIED:
            raise HostKeyNotCertifiedError(
                f"{self.path} has an @cert-authority line for {name}; the key offered is plain"
            )
        return verdict is HostKeyVerdict.TRUSTED

    def _append(self, line: str) -> None:
        """Append *line* whole (the lock is held); a new file is created 0600.

        ``os.write`` may write less than it was given, so it is called until
        every byte is written; a call that writes nothing is a failure, raised
        as unwritable, so a first-use key that was not recorded never lets the
        connection authenticate. No ``fsync``: a line lost to a crash is
        recorded again at the next first use, which is what OpenSSH's own
        ``accept-new`` append does too.
        """
        data = line.encode()
        try:
            fd = os.open(self._target(), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            try:
                while data:
                    written = os.write(fd, data)
                    if written <= 0:
                        raise OSError(errno.EIO, "the line could not be written whole")
                    data = data[written:]
            finally:
                os.close(fd)
        except OSError as err:
            raise _unwritable(self.path, err) from err

    def forget(self, alias: str, ports: list[int]) -> ForgetResult:
        """Remove *alias*'s entries and report what was removed and what was kept.

        A plain line is the alias's when its host field is ``alias`` or
        ``[alias]:<any port>``. A hashed line names no host in clear text,
        so it is tested against the names otto would have written for
        *ports* (the host's configured SSH port) and for port 22, which is
        always added here, so a caller passes only its own port. A line is removed
        only when every host pattern on it is the alias's and it carries no
        marker; a line that also names another host or a wildcard, and a
        ``@cert-authority`` or ``@revoked`` line, is left in place and
        listed in :attr:`ForgetResult.kept_shared`. A missing file means
        nothing to remove and is not created.

        The file is read first without the lock, so a forget with nothing to
        remove writes nothing, not even the lock file (it works on a
        read-only directory). Only when a line would go is the lock taken and
        the file read again under it. The file is rewritten as
        ``ssh-keygen -R`` does, write-then-rename
        (:func:`otto.utils.atomic_write_bytes`), keeping its mode and, for
        root, its owner: a concurrent reader (the user's own ``ssh``) sees
        the old file or the new one, never a truncated one.

        Raises:
            KnownHostsUnwritableError: there is something to remove and the
                file cannot be rewritten.
        """
        names = list(dict.fromkeys(entry_name(alias, p) for p in [_DEFAULT_PORT, *ports]))
        if not self.path.exists():
            return ForgetResult(alias=alias, removed=0, kept_shared=[], path=self.path)
        plan = _ForgetPlan.of(self._read(), alias, names)
        if plan.removed:
            with self._locked():
                # Read again under the lock: another writer may have changed it.
                plan = _ForgetPlan.of(self._read(), alias, names)
                if plan.removed:
                    try:
                        original = self._target().stat()
                        atomic_write_bytes(
                            self.path, "".join(plan.kept).encode(), preserve=original
                        )
                    except OSError as err:
                        raise _unwritable(self.path, err) from err
        return ForgetResult(
            alias=alias, removed=plan.removed, kept_shared=plan.shared, path=self.path
        )


@dataclass(frozen=True)
class _ForgetPlan:
    """What :meth:`KnownHosts.forget` would leave of one reading of the file."""

    kept: list[str]
    """The lines that stay, line endings included."""

    shared: list[str]
    """The kept lines that name the alias among other hosts, stripped."""

    removed: int
    """How many lines go."""

    @classmethod
    def of(cls, text: str, alias: str, names: list[str]) -> "_ForgetPlan":
        """Judge every line of *text* for *alias* (:func:`_forget_verdict`)."""
        lines = text.splitlines(keepends=True)
        kept: list[str] = []
        shared: list[str] = []
        for line in lines:
            verdict = _forget_verdict(line, alias, names)
            if verdict is _Verdict.REMOVE:
                continue
            if verdict is _Verdict.SHARED:
                shared.append(line.strip())
            kept.append(line)
        return cls(kept=kept, shared=shared, removed=len(lines) - len(kept))


class _Verdict(enum.Enum):
    """What :meth:`KnownHosts.forget` does with one line."""

    KEEP = enum.auto()
    REMOVE = enum.auto()
    SHARED = enum.auto()


def _is_alias_pattern(pattern: str, data: str, alias: str, names: list[str]) -> bool:
    """Return whether one host pattern names *alias* and nothing else."""
    if pattern.startswith("|"):
        # A hashed name can only be tested against candidates, through asyncssh.
        try:
            parsed = import_known_hosts(f"{pattern} {data}")
        except ValueError:
            return False
        return any(any(_matches(parsed, name)) for name in names)
    if pattern == alias:
        return True
    # Any port, 22 included: forget-key removes the alias on every port it was
    # recorded under, not only on the one the host is configured for.
    port = pattern.rpartition("]:")[2]
    return port.isdigit() and pattern == _bracketed_name(alias, port)


def _forget_verdict(line: str, alias: str, names: list[str]) -> "_Verdict":
    entry = _Line.split(line)
    if entry is None:
        return _Verdict.KEEP
    ours = [_is_alias_pattern(p, entry.data, alias, names) for p in entry.patterns]
    if not any(ours):
        # Not named outright; a wildcard or negation is asyncssh's to judge.
        try:
            parsed = import_known_hosts(line)
        except ValueError:
            return _Verdict.KEEP
        if not any(any(_matches(parsed, name)) for name in names):
            return _Verdict.KEEP
        return _Verdict.SHARED
    if entry.marker is None and all(ours):
        return _Verdict.REMOVE
    return _Verdict.SHARED
