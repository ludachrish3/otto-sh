"""The SSH algorithm matrix: everything otto's SSH stack can negotiate.

One row per algorithm the installed asyncssh registers, in registry order.
``stock`` marks the rows asyncssh offers by DEFAULT: a host record with no
``ssh_options`` negotiates those; the rest need ``kex_algs`` /
``server_host_key_algs`` / ``encryption_algs`` / ``mac_algs`` /
``compression_algs`` on the host (docs/configuration/settings.md). ``note``
names what a row needs beyond otto itself, and is empty for the rows that
are always there.

The matrix is pinned four ways, and every pin imports THIS module:

* registry, both directions — every registered name has a row, and every
  row is registered unless its note says which optional library gates it
  (tests/unit/host/test_legacy_ssh_algorithms.py);
* stock, both directions — a row's ``stock`` equals its presence in the
  default list (same module);
* handshake — an in-process asyncssh server and client negotiate every
  exercisable row through the installed cryptography
  (tests/unit/host/test_ssh_algorithm_handshake.py), so a library that
  drops an implementation while keeping its name registered reds here;
* docs — the tables on docs/architecture/ssh-algorithms.md equal this
  module (tests/unit/docs/test_ssh_algorithms_page.py).

``DROPBEAR_2012`` is the subset a 2012.55 dropbear (the bb1350 bed guest)
offers, confirmed on the wire on 2026-08-22 and 2026-09-23; the wire pin is
tests/integration/busybox_bed/test_legacy_dropbear.py.
"""

from dataclasses import dataclass

from asyncssh.compression import get_compression_algs
from asyncssh.encryption import get_encryption_algs
from asyncssh.kex import get_kex_algs
from asyncssh.mac import get_mac_algs
from asyncssh.public_key import get_public_key_algs

KEX = "kex"
HOST_KEY = "host-key"
CIPHER = "cipher"
MAC = "mac"
COMPRESSION = "compression"
FAMILIES = [KEX, HOST_KEY, CIPHER, MAC, COMPRESSION]

# Notes. The first two gate the HANDSHAKE (the row is registered, but no
# hostless test holds the credential); the last two gate REGISTRATION (the
# row exists only when the library was importable).
NEEDS_KERBEROS = "Kerberos credentials on the client"
NEEDS_FIDO = "a FIDO security key"
NEEDS_NETTLE = "libnettle importable when asyncssh loads"
NEEDS_LIBOQS = "liboqs importable when asyncssh loads"
IMPORT_GATED = frozenset({NEEDS_NETTLE, NEEDS_LIBOQS})

# Registries a row's family looks itself up in, to answer "is this name
# registered in THIS interpreter" for the import-gated rows (sntrup needs
# liboqs; umac needs libnettle) without duplicating asyncssh's own
# availability flags. mlkem, curve25519-*, curve448-sha512, ssh-ed25519,
# ssh-ed448 and chacha20-poly1305@openssh.com are also conditionally
# registered, but on cryptography's own backend flags rather than an
# optional library — the lock pins cryptography, so their disappearance
# SHOULD red, and they deliberately carry no note and are not in
# IMPORT_GATED.
_REGISTRY = {
    KEX: get_kex_algs,
    HOST_KEY: get_public_key_algs,
    CIPHER: get_encryption_algs,
    MAC: get_mac_algs,
    COMPRESSION: get_compression_algs,
}


@dataclass(frozen=True)
class SshAlgorithm:
    family: str
    name: str
    stock: bool
    note: str = ""


MATRIX: list[SshAlgorithm] = [
    # kex, registry order (asyncssh/kex.py, asyncssh/kex_dh.py). The gss-*
    # rows need a Kerberos credential to exercise; nothing here gates their
    # REGISTRATION.
    SshAlgorithm(KEX, "gss-curve25519-sha256", stock=True, note=NEEDS_KERBEROS),
    SshAlgorithm(KEX, "gss-curve448-sha512", stock=True, note=NEEDS_KERBEROS),
    SshAlgorithm(KEX, "gss-nistp521-sha512", stock=True, note=NEEDS_KERBEROS),
    SshAlgorithm(KEX, "gss-nistp384-sha384", stock=True, note=NEEDS_KERBEROS),
    SshAlgorithm(KEX, "gss-nistp256-sha256", stock=True, note=NEEDS_KERBEROS),
    SshAlgorithm(KEX, "gss-1.3.132.0.10-sha256", stock=True, note=NEEDS_KERBEROS),
    SshAlgorithm(KEX, "gss-gex-sha256", stock=True, note=NEEDS_KERBEROS),
    SshAlgorithm(KEX, "gss-gex-sha1", stock=False, note=NEEDS_KERBEROS),
    SshAlgorithm(KEX, "gss-group14-sha256", stock=True, note=NEEDS_KERBEROS),
    SshAlgorithm(KEX, "gss-group15-sha512", stock=True, note=NEEDS_KERBEROS),
    SshAlgorithm(KEX, "gss-group16-sha512", stock=True, note=NEEDS_KERBEROS),
    SshAlgorithm(KEX, "gss-group17-sha512", stock=True, note=NEEDS_KERBEROS),
    SshAlgorithm(KEX, "gss-group18-sha512", stock=True, note=NEEDS_KERBEROS),
    SshAlgorithm(KEX, "gss-group14-sha1", stock=True, note=NEEDS_KERBEROS),
    SshAlgorithm(KEX, "gss-group1-sha1", stock=False, note=NEEDS_KERBEROS),
    SshAlgorithm(KEX, "mlkem768x25519-sha256", stock=True),
    SshAlgorithm(KEX, "mlkem768nistp256-sha256", stock=True),
    SshAlgorithm(KEX, "mlkem1024nistp384-sha384", stock=True),
    # sntrup761x25519-sha512{,@openssh.com}: kex_dh.py registers these right
    # here, after the mlkem rows, only when liboqs is importable
    # (sntrup_available); absent in this interpreter. `stock=True` comes
    # from the `register_kex_alg(..., True)` call at the source, not from a
    # default-list lookup this interpreter can make.
    SshAlgorithm(KEX, "sntrup761x25519-sha512", stock=True, note=NEEDS_LIBOQS),
    SshAlgorithm(KEX, "sntrup761x25519-sha512@openssh.com", stock=True, note=NEEDS_LIBOQS),
    SshAlgorithm(KEX, "curve25519-sha256", stock=True),
    SshAlgorithm(KEX, "curve25519-sha256@libssh.org", stock=True),
    SshAlgorithm(KEX, "curve448-sha512", stock=True),
    SshAlgorithm(KEX, "ecdh-sha2-nistp521", stock=True),
    SshAlgorithm(KEX, "ecdh-sha2-nistp384", stock=True),
    SshAlgorithm(KEX, "ecdh-sha2-nistp256", stock=True),
    SshAlgorithm(KEX, "ecdh-sha2-1.3.132.0.10", stock=True),
    SshAlgorithm(KEX, "diffie-hellman-group-exchange-sha256", stock=True),
    SshAlgorithm(KEX, "diffie-hellman-group-exchange-sha224@ssh.com", stock=False),
    SshAlgorithm(KEX, "diffie-hellman-group-exchange-sha384@ssh.com", stock=False),
    SshAlgorithm(KEX, "diffie-hellman-group-exchange-sha512@ssh.com", stock=False),
    SshAlgorithm(KEX, "diffie-hellman-group-exchange-sha1", stock=False),
    SshAlgorithm(KEX, "diffie-hellman-group14-sha256", stock=True),
    SshAlgorithm(KEX, "diffie-hellman-group15-sha512", stock=True),
    SshAlgorithm(KEX, "diffie-hellman-group16-sha512", stock=True),
    SshAlgorithm(KEX, "diffie-hellman-group17-sha512", stock=True),
    SshAlgorithm(KEX, "diffie-hellman-group18-sha512", stock=True),
    SshAlgorithm(KEX, "diffie-hellman-group14-sha256@ssh.com", stock=True),
    SshAlgorithm(KEX, "diffie-hellman-group14-sha224@ssh.com", stock=False),
    SshAlgorithm(KEX, "diffie-hellman-group15-sha256@ssh.com", stock=False),
    SshAlgorithm(KEX, "diffie-hellman-group15-sha384@ssh.com", stock=False),
    SshAlgorithm(KEX, "diffie-hellman-group16-sha384@ssh.com", stock=False),
    SshAlgorithm(KEX, "diffie-hellman-group16-sha512@ssh.com", stock=False),
    SshAlgorithm(KEX, "diffie-hellman-group18-sha512@ssh.com", stock=False),
    SshAlgorithm(KEX, "diffie-hellman-group14-sha1", stock=True),
    SshAlgorithm(KEX, "diffie-hellman-group1-sha1", stock=False),
    SshAlgorithm(KEX, "rsa2048-sha256", stock=True),
    SshAlgorithm(KEX, "rsa1024-sha1", stock=False),
    # host-key, registry order (asyncssh/public_key.py). The sk-*/webauthn-*
    # rows need a FIDO security key to exercise; nothing here gates their
    # REGISTRATION.
    SshAlgorithm(HOST_KEY, "rsa-sha2-256", stock=True),
    SshAlgorithm(HOST_KEY, "rsa-sha2-512", stock=True),
    SshAlgorithm(HOST_KEY, "ssh-rsa-sha224@ssh.com", stock=True),
    SshAlgorithm(HOST_KEY, "ssh-rsa-sha256@ssh.com", stock=True),
    SshAlgorithm(HOST_KEY, "ssh-rsa-sha384@ssh.com", stock=True),
    SshAlgorithm(HOST_KEY, "ssh-rsa-sha512@ssh.com", stock=True),
    SshAlgorithm(HOST_KEY, "ssh-rsa", stock=True),
    SshAlgorithm(HOST_KEY, "sk-ssh-ed25519@openssh.com", stock=True, note=NEEDS_FIDO),
    SshAlgorithm(HOST_KEY, "sk-ecdsa-sha2-nistp256@openssh.com", stock=True, note=NEEDS_FIDO),
    SshAlgorithm(
        HOST_KEY, "webauthn-sk-ecdsa-sha2-nistp256@openssh.com", stock=True, note=NEEDS_FIDO
    ),
    SshAlgorithm(HOST_KEY, "ssh-ed25519", stock=True),
    SshAlgorithm(HOST_KEY, "ssh-ed448", stock=True),
    SshAlgorithm(HOST_KEY, "ecdsa-sha2-nistp521", stock=True),
    SshAlgorithm(HOST_KEY, "ecdsa-sha2-nistp384", stock=True),
    SshAlgorithm(HOST_KEY, "ecdsa-sha2-nistp256", stock=True),
    SshAlgorithm(HOST_KEY, "ecdsa-sha2-1.3.132.0.10", stock=True),
    SshAlgorithm(HOST_KEY, "ssh-dss", stock=False),
    # cipher, registry order (asyncssh/encryption.py).
    SshAlgorithm(CIPHER, "chacha20-poly1305@openssh.com", stock=True),
    SshAlgorithm(CIPHER, "aes256-gcm@openssh.com", stock=True),
    SshAlgorithm(CIPHER, "aes128-gcm@openssh.com", stock=True),
    SshAlgorithm(CIPHER, "aes256-ctr", stock=True),
    SshAlgorithm(CIPHER, "aes192-ctr", stock=True),
    SshAlgorithm(CIPHER, "aes128-ctr", stock=True),
    SshAlgorithm(CIPHER, "aes256-cbc", stock=False),
    SshAlgorithm(CIPHER, "aes192-cbc", stock=False),
    SshAlgorithm(CIPHER, "aes128-cbc", stock=False),
    SshAlgorithm(CIPHER, "3des-cbc", stock=False),
    SshAlgorithm(CIPHER, "blowfish-cbc", stock=False),
    SshAlgorithm(CIPHER, "cast128-cbc", stock=False),
    SshAlgorithm(CIPHER, "seed-cbc@ssh.com", stock=False),
    SshAlgorithm(CIPHER, "arcfour256", stock=False),
    SshAlgorithm(CIPHER, "arcfour128", stock=False),
    SshAlgorithm(CIPHER, "arcfour", stock=False),
    # mac, registry order (asyncssh/mac.py). The umac-* rows are libnettle
    # (ctypes) gated at import.
    SshAlgorithm(MAC, "umac-64-etm@openssh.com", stock=True, note=NEEDS_NETTLE),
    SshAlgorithm(MAC, "umac-128-etm@openssh.com", stock=True, note=NEEDS_NETTLE),
    SshAlgorithm(MAC, "hmac-sha2-256-etm@openssh.com", stock=True),
    SshAlgorithm(MAC, "hmac-sha2-512-etm@openssh.com", stock=True),
    SshAlgorithm(MAC, "hmac-sha1-etm@openssh.com", stock=True),
    SshAlgorithm(MAC, "hmac-md5-etm@openssh.com", stock=False),
    SshAlgorithm(MAC, "hmac-sha2-256-96-etm@openssh.com", stock=False),
    SshAlgorithm(MAC, "hmac-sha2-512-96-etm@openssh.com", stock=False),
    SshAlgorithm(MAC, "hmac-sha1-96-etm@openssh.com", stock=False),
    SshAlgorithm(MAC, "hmac-md5-96-etm@openssh.com", stock=False),
    SshAlgorithm(MAC, "umac-64@openssh.com", stock=True, note=NEEDS_NETTLE),
    SshAlgorithm(MAC, "umac-128@openssh.com", stock=True, note=NEEDS_NETTLE),
    SshAlgorithm(MAC, "hmac-sha2-256", stock=True),
    SshAlgorithm(MAC, "hmac-sha2-512", stock=True),
    SshAlgorithm(MAC, "hmac-sha1", stock=True),
    SshAlgorithm(MAC, "hmac-sha256-2@ssh.com", stock=True),
    SshAlgorithm(MAC, "hmac-sha224@ssh.com", stock=True),
    SshAlgorithm(MAC, "hmac-sha256@ssh.com", stock=True),
    SshAlgorithm(MAC, "hmac-sha384@ssh.com", stock=True),
    SshAlgorithm(MAC, "hmac-sha512@ssh.com", stock=True),
    SshAlgorithm(MAC, "hmac-md5", stock=False),
    SshAlgorithm(MAC, "hmac-sha2-256-96", stock=False),
    SshAlgorithm(MAC, "hmac-sha2-512-96", stock=False),
    SshAlgorithm(MAC, "hmac-sha1-96", stock=False),
    SshAlgorithm(MAC, "hmac-md5-96", stock=False),
    # compression, registry order (asyncssh/compression.py).
    SshAlgorithm(COMPRESSION, "none", stock=True),
    SshAlgorithm(COMPRESSION, "zlib@openssh.com", stock=True),
    SshAlgorithm(COMPRESSION, "zlib", stock=False),
]

DROPBEAR_2012 = frozenset(
    {
        "diffie-hellman-group14-sha1",
        "diffie-hellman-group1-sha1",
        "ssh-rsa",
        "ssh-dss",
        "aes128-ctr",
        "aes256-ctr",
        "aes128-cbc",
        "aes256-cbc",
        "3des-cbc",
        "hmac-sha1",
        "hmac-sha1-96",
        "hmac-md5",
    }
)


def rows(family: str) -> list[SshAlgorithm]:
    return [row for row in MATRIX if row.family == family]


def names(family: str) -> list[str]:
    return [row.name for row in rows(family)]


def stock_names(family: str) -> list[str]:
    return [row.name for row in rows(family) if row.stock]


def _registered_names(family: str) -> list[str]:
    return [n.decode("ascii") for n in _REGISTRY[family]()]


def exercisable(family: str) -> list[str]:
    """Rows the in-process handshake can negotiate: no credential or hardware,
    and registered in this interpreter (an import-gated row that is absent is
    not a failure — the registry pin already checks its note explains it)."""
    return [
        row.name
        for row in rows(family)
        if row.note == "" or (row.note in IMPORT_GATED and row.name in _registered_names(family))
    ]
