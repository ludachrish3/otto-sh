"""Owner vocabulary: basenames corroborate, BusyBox applet forms resolve, ports from options."""

import pytest

from otto.host.options import FtpOptions, SnmpOptions, SshOptions, TelnetOptions, TftpOptions
from otto.host.survey.owners import (
    DEFAULT_PORTS,
    OWNER_BASENAMES,
    SSH_SWEEP_PORTS,
    SWEEP_ALTERNATES,
    protocol_for_owner,
)


@pytest.mark.parametrize(
    ("owner", "protocol"),
    [
        ("sshd", "ssh"),
        ("/usr/sbin/sshd", "ssh"),
        ("dropbear", "ssh"),
        ("telnetd", "telnet"),
        ("in.telnetd", "telnet"),
        ("busybox: telnetd", "telnet"),
        ("busybox telnetd", "telnet"),
        ("/bin/busybox telnetd", "telnet"),
        ("vsftpd", "ftp"),
        ("proftpd", "ftp"),
        ("pure-ftpd", "ftp"),
        ("ftpd", "ftp"),
        ("in.ftpd", "ftp"),
        ("in.tftpd", "tftp"),
        ("dnsmasq", "tftp"),
        ("snmpd", "snmp"),
        ("nginx", None),
        ("", None),
        (None, None),
        ("busybox", None),
    ],
)
def test_owner_basenames_map_to_protocols(owner, protocol):
    """Mutation: match on the full path and `/usr/sbin/sshd` becomes unknown."""
    assert protocol_for_owner(owner) == protocol


def test_the_vocabulary_is_exactly_the_spec_table():
    assert OWNER_BASENAMES == {
        "ssh": ["sshd", "dropbear"],
        "telnet": ["telnetd", "in.telnetd"],
        "ftp": ["vsftpd", "proftpd", "pure-ftpd", "ftpd", "in.ftpd"],
        "tftp": ["in.tftpd", "tftpd", "atftpd", "dnsmasq"],
        "snmp": ["snmpd"],
    }


def test_default_ports_come_from_the_options_dataclasses_not_literals():
    """Mutation: type 22 into the engine and a changed SshOptions default goes unnoticed."""
    assert {
        "ssh": SshOptions().port,
        "telnet": TelnetOptions().port,
        "ftp": FtpOptions().port,
        "snmp": SnmpOptions().port,
        "tftp": TftpOptions().port,
    } == DEFAULT_PORTS


def test_sweep_alternates_are_the_curated_list_and_lists_not_tuples():
    assert SWEEP_ALTERNATES == {"telnet": [2323, 8023], "snmp": [1161]}
    assert SSH_SWEEP_PORTS == [22, 2222]
    assert all(isinstance(v, list) for v in SWEEP_ALTERNATES.values())
