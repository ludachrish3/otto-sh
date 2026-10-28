"""Who serves what: owner basenames per protocol, and the curated sweep ports.

Owners CORROBORATE; a banner DECIDES (TCP). For UDP the owner is the only
evidence beyond the port number. The sweep alternates below are the one place
the spec allows port literals; the defaults come from the options classes.
"""

from ..options import (
    FtpOptions,
    SnmpOptions,
    SshOptions,
    TelnetOptions,
    TftpOptions,
    field_default,
)

OWNER_BASENAMES: dict[str, list[str]] = {
    "ssh": ["sshd", "dropbear"],
    "telnet": ["telnetd", "in.telnetd"],
    "ftp": ["vsftpd", "proftpd", "pure-ftpd", "ftpd", "in.ftpd"],
    "tftp": ["in.tftpd", "tftpd", "atftpd", "dnsmasq"],
    "snmp": ["snmpd"],
}

SWEEP_ALTERNATES: dict[str, list[int]] = {"telnet": [2323, 8023], "snmp": [1161]}
"""Curated non-default ports the embedded sweep dials besides the declared ones."""

SSH_SWEEP_PORTS: list[int] = [22, 2222]
"""Dialed on embedded hosts for the footnote only: ssh is not a family backend there."""


DEFAULT_PORTS: dict[str, int] = {
    "ssh": field_default(SshOptions, "port"),
    "telnet": field_default(TelnetOptions, "port"),
    "ftp": field_default(FtpOptions, "port"),
    "snmp": field_default(SnmpOptions, "port"),
    "tftp": field_default(TftpOptions, "port"),
}
"""Each backend's default port, read from its options class."""

_BUSYBOX = "busybox"
_BUSYBOX_MIN_WORDS = 2


def protocol_for_owner(owner: str | None) -> str | None:
    """Return the protocol *owner* serves, by basename, or ``None``."""
    if not owner:
        return None
    words = owner.replace(":", " ").split()
    if not words:
        return None
    name = words[0].rsplit("/", 1)[-1]
    if name == _BUSYBOX:
        if len(words) < _BUSYBOX_MIN_WORDS:
            return None
        name = words[1]
    for protocol, names in OWNER_BASENAMES.items():
        if name in names:
            return protocol
    return None
