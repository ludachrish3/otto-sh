"""otto's SSH client object: host keys checked and recorded by host ID during the handshake.

asyncssh checks a server's host key against what its ``known_hosts`` argument
returns, and consults the connection's client object for a key that is not
in it. otto hands asyncssh both, built per connection by :class:`HostKeyCheck`
for one host ID (the *alias*):

- ``known_hosts`` is the check itself, a callable that asks
  :class:`~otto.host.known_hosts.KnownHosts` for the alias's entries by name
  only. asyncssh's own lookup would also match the peer address, and a
  tunnelled host's peer address is its hop's.
- ``client_factory`` builds a :class:`HostKeyClient`, which decides the key
  the entries do not hold, judged against the file as it is then (another
  connection may have recorded it meanwhile): accepted when the alias's entry
  now holds it, refused when the entry holds other keys (a changed key) or
  when it holds none and an ``@cert-authority`` line covers the alias (a host
  known by its CA takes only its certificates), accepted and remembered
  otherwise (first use). The first-use
  key is recorded in :meth:`HostKeyClient.begin_auth`, after the key
  exchange has proven the server holds it and before any credential is sent.
- ``host_key_alias`` is the host ID, so asyncssh judges a host certificate's
  principals against it too.
- ``server_host_key_algs``, unless the options set their own list, is otto's
  choice, as OpenSSH orders its host-key algorithms: certificate algorithms
  first when an ``@cert-authority`` line applies (none otherwise), then the
  recorded key's algorithms, then asyncssh's default plain-key algorithms. A
  host that rotated its key type therefore still negotiates, reaches the
  check with its new key, and is refused as a changed key; a host known by
  its CA that offers only a plain key reaches the check and is refused as not
  certified; and a host
  that also holds a certificate no CA line trusts is recorded by its plain
  key.

Every refusal, whichever layer raised it, becomes one
:class:`~otto.host.errors.HostKeyRefusedError` through
:func:`host_key_refusal`, which
:func:`~otto.host.connections.connect_and_describe` calls.

This module imports asyncssh, so it is imported only where a connection is
being made (``SshOptions._kwargs`` and the connect seam's error path).
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from asyncssh import HostKeyNotVerifiable, SSHClient, SSHKey
from asyncssh.public_key import get_default_certificate_algs, get_default_public_key_algs
from typing_extensions import override

# The module, not its names: the test suite redirects the default file by
# patching ``known_hosts.known_hosts_path`` in that one module
# (``_hermetic_known_hosts`` in tests/conftest.py), which a name imported
# here would not see.
from . import known_hosts
from .errors import (
    HostKeyChangedError,
    HostKeyNotCertifiedError,
    HostKeyRefusal,
    HostKeyRefusedError,
    HostKeyRevokedError,
    KnownHostsError,
    KnownHostsUnwritableError,
)
from .options import SshOptions

__all__ = ["HostKeyCheck", "HostKeyClient", "host_key_refusal"]

_REVOKED_REASONS = ("Host key is revoked", "Host CA key is revoked")
"""The reasons asyncssh gives a revoked plain key and a revoked certificate
authority, verbatim (asyncssh 2.24 ``connection.py:1309``, ``:1312`` and
``:1379``). asyncssh refuses a key revoked in what it read when the
connection opened before the client object sees it; a revocation written
after that read is caught by :meth:`HostKeyClient.validate_host_public_key`,
which raises the first reason itself, so both reach :func:`host_key_refusal`
the same way. asyncssh's ``HostKeyNotVerifiable`` message (``"<reason> for
host <host>"``) is the only place that verdict is reported. Matched as a
prefix, never as a word anywhere in the message, which also carries the host
ID."""

_CHANGED_REASON = "Host key is not trusted"
"""The reason asyncssh gives when :meth:`HostKeyClient.validate_host_public_key`
returns ``False`` (asyncssh 2.24 ``connection.py:1387``), which it does only
for a changed key. A refused certificate says ``Host CA key ...`` instead.
Matched as a prefix, like ``_REVOKED_REASONS``, so the label is the
hook's own verdict, never a second read of a file that may have changed
since."""

_NOT_CERTIFIED_REASON = "Host key is not certified by the host's CA"
"""The reason :meth:`HostKeyClient.validate_host_public_key` raises for a
plain key presented by a host an ``@cert-authority`` line covers. asyncssh
wraps it as it wraps ``_REVOKED_REASONS``; matched as a prefix. Neither it
nor asyncssh's own reasons (``Host key is not trusted``, ``Host key is
revoked``, ``Host CA key ...``) begins with another."""


class HostKeyClient(SSHClient):
    """The client object of one connection that checks host keys by host ID.

    asyncssh calls :meth:`validate_host_public_key` only for a plain key the
    ``known_hosts`` result does not trust. A certificate is never accepted
    here: it is trusted only through an ``@cert-authority`` line (asyncssh's
    own check), and ``validate_host_ca_key`` keeps asyncssh's refusal. A
    GSS key exchange presents no host key, so nothing is remembered or
    recorded for it.
    """

    def __init__(self, store: known_hosts.KnownHosts, alias: str, port: int) -> None:
        self._store = store
        self._alias = alias
        self._port = port
        self._first_use: SSHKey | None = None

    @override
    def validate_host_public_key(self, host: str, addr: str, port: int, key: SSHKey) -> bool:
        """Judge the key against the file as it is now; accept and remember a first use.

        asyncssh asks this for a key absent from what its ``known_hosts``
        callable returned when the connection opened, which may be stale: a
        concurrent first contact may have recorded this very key since. So the
        file is read again and judged by :meth:`KnownHosts.judge
        <otto.host.known_hosts.KnownHosts.judge>`, the decision
        :meth:`~otto.host.known_hosts.KnownHosts.record` makes too:

        - held: accepted;
        - no entry: accepted for this connection, and recorded at
          :meth:`begin_auth`;
        - another plain key held: refused, as a changed key (asyncssh reports
          it as ``HostKeyNotVerifiable`` with ``_CHANGED_REASON``);
        - no plain key held but an ``@cert-authority`` line for the alias (the
          host is known by its CA): refused with ``_NOT_CERTIFIED_REASON``;
        - revoked: refused with asyncssh's own reason for a revoked key, so the
          refusal is labelled revoked (:func:`host_key_refusal`).
        """
        verdict = self._store.judge(self._alias, self._port, key)
        if verdict is known_hosts.HostKeyVerdict.REVOKED:
            # asyncssh turns a ValueError here into HostKeyNotVerifiable with
            # this reason, the one it gives a key it knew was revoked.
            raise ValueError(_REVOKED_REASONS[0])
        if verdict is known_hosts.HostKeyVerdict.NOT_CERTIFIED:
            raise ValueError(_NOT_CERTIFIED_REASON)
        if verdict is known_hosts.HostKeyVerdict.FIRST_USE:
            self._first_use = key
        return verdict in (known_hosts.HostKeyVerdict.TRUSTED, known_hosts.HostKeyVerdict.FIRST_USE)

    @override
    def begin_auth(self, username: str) -> None:
        """Record a first-use key before authentication begins.

        asyncssh calls this once the key exchange has proven the server holds
        the key and before the first credential leaves. A record that fails
        (the file meanwhile revokes the key or holds a different key for the
        alias, or cannot be written) raises here, and asyncssh aborts the
        connection with that error: no credential is sent.
        """
        if self._first_use is not None:
            key, self._first_use = self._first_use, None
            self._store.record(self._alias, self._port, key)


@dataclass(frozen=True, repr=False)
class HostKeyCheck:
    """The host-key check for one host ID on one port, in the shape asyncssh takes.

    Called by asyncssh as its ``known_hosts`` callable (synchronously, with
    the host, peer address and port, all ignored: the answer is the alias's
    entries, by name only). A value: two checks for the same host, port,
    file and algorithm choice are equal, and so are the kwargs they build.
    """

    alias: str
    """The otto host ID the key is checked and recorded under."""

    port: int
    """The SSH port. Port 22 is decided by the bare ``alias`` entry, any
    other by ``[alias]:port`` (:func:`~otto.host.known_hosts.entry_name`)."""

    path: Path
    """The known-hosts file, as :func:`~otto.host.known_hosts.known_hosts_path` resolved it."""

    chooses_algs: bool
    """Whether otto chooses the host-key algorithms; ``False`` when the
    options name their own ``server_host_key_algs``, which then win."""

    @classmethod
    def for_options(cls, options: SshOptions, alias: str) -> "HostKeyCheck":
        """Build the check *options* configure for host *alias*."""
        return cls(
            alias=alias,
            port=options.port,
            path=known_hosts.known_hosts_path(options),
            chooses_algs=options.server_host_key_algs is None,
        )

    @property
    def store(self) -> known_hosts.KnownHosts:
        """The known-hosts store this check reads and records in."""
        return known_hosts.KnownHosts(self.path)

    def __call__(self, _host: str, _addr: str, _port: int | None) -> known_hosts.HostKeyMatch:
        """Return the alias's entries: asyncssh's ``known_hosts`` callable.

        asyncssh passes the host (the alias), the peer address and the port;
        none is read, because the alias and port are this check's own and an
        address never decides.
        """
        return self.store.match(self.alias, self.port)

    @override
    def __repr__(self) -> str:
        """One short phrase for the DEBUG line that shows the connect kwargs."""
        return f"<host-key check: {self.alias} port {self.port} in {self.path}>"

    def host_key_algs(self) -> list[str] | None:
        """Return the host-key algorithms otto offers, or ``None`` for asyncssh's default.

        The recorded key's signature algorithms first, then asyncssh's
        default plain-key algorithms. When an ``@cert-authority`` line applies
        to the alias, asyncssh's default certificate algorithms come before
        both, so a host holding a certificate is checked by its CA; a host
        offering only a plain key still negotiates and reaches the check,
        which refuses it as not certified (a host known by its CA). With no
        CA line no certificate algorithm is offered: a certificate no CA line
        trusts would only be refused, where the host's plain key can be
        checked or recorded. ``None`` when the file cannot be read (the
        handshake's own read reports that, as a refusal).
        """
        try:
            found = self.store.match(self.alias, self.port)
        except KnownHostsError:
            return None
        certificates = get_default_certificate_algs() if found.ca_keys else []
        recorded = [alg for key in found.host_keys for alg in key.sig_algorithms]
        ordered = dict.fromkeys([*certificates, *recorded, *get_default_public_key_algs()])
        return [alg.decode("ascii") for alg in ordered]

    def connect_kwargs(self) -> dict[str, Any]:
        """Return the ``asyncssh.connect`` keyword arguments that apply this check."""
        kwargs: dict[str, Any] = {
            "known_hosts": self,
            "client_factory": _ClientFactory(self),
            "host_key_alias": self.alias,
        }
        if self.chooses_algs:
            algs = self.host_key_algs()
            if algs is not None:
                kwargs["server_host_key_algs"] = algs
        return kwargs


@dataclass(frozen=True)
class _ClientFactory:
    """asyncssh's ``client_factory`` for one check: a value, equal for equal checks."""

    check: HostKeyCheck

    def __call__(self) -> HostKeyClient:
        """Build the connection's client object."""
        return HostKeyClient(self.check.store, self.check.alias, self.check.port)


def host_key_refusal(
    exc: BaseException, check: HostKeyCheck, *, address: str
) -> HostKeyRefusedError | None:
    """Return the :class:`~otto.host.errors.HostKeyRefusedError` *exc* is, or ``None``.

    The one place a failed connect is judged a host-key refusal, for the
    connection *check* configured:

    - asyncssh's ``HostKeyNotVerifiable``, labelled by its reason alone: a
      revoked key (``_REVOKED_REASONS``), a changed key
      (``_CHANGED_REASON``, the client object's refusal), a plain key
      from a host known by its CA (``_NOT_CERTIFIED_REASON``), or anything
      else, an untrusted certificate among them. The file is never read
      again to label it: a ``forget-key`` meanwhile would relabel a changed
      key.
    - the known-hosts store's own errors, raised during the handshake: the
      key revoked meanwhile, a different key recorded meanwhile, an
      unwritable or unreadable file.

    Anything else, a failed key exchange included, is not a key matter: otto
    offers every default key type (:meth:`HostKeyCheck.host_key_algs`), so a
    host that changed key type reaches the check and is refused there.
    """
    store = check.store

    def refused(reason: str, kind: HostKeyRefusal) -> HostKeyRefusedError:
        return HostKeyRefusedError(
            host_id=check.alias, address=address, path=str(store.path), reason=reason, kind=kind
        )

    if isinstance(exc, HostKeyNotVerifiable):
        if str(exc).startswith(_REVOKED_REASONS):
            return refused(str(exc), HostKeyRefusal.REVOKED)
        if str(exc).startswith(_CHANGED_REASON):
            return refused(str(exc), HostKeyRefusal.CHANGED)
        if str(exc).startswith(_NOT_CERTIFIED_REASON):
            return refused(str(exc), HostKeyRefusal.NOT_CERTIFIED)
        return refused(str(exc), HostKeyRefusal.UNTRUSTED)
    if isinstance(exc, HostKeyRevokedError):
        return refused("the key was revoked during this connection", HostKeyRefusal.REVOKED)
    if isinstance(exc, HostKeyNotCertifiedError):
        return refused(
            "an @cert-authority line for the host appeared during this connection",
            HostKeyRefusal.NOT_CERTIFIED,
        )
    if isinstance(exc, HostKeyChangedError):
        return refused(
            "a different key was recorded during this connection", HostKeyRefusal.CHANGED
        )
    if isinstance(exc, (KnownHostsUnwritableError, KnownHostsError)):
        return refused(str(exc), HostKeyRefusal.FILE)
    return None
