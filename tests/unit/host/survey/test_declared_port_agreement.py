"""The port the survey declares for a protocol is the port the transport dials.

Two readers of one option: ``_declared_port_unix`` for the survey's declared
rows and the options' own connect kwargs for the dial. They must agree on the
same host, which is only guaranteed because an ``extra`` key cannot shadow
the ``port`` field (``tests/unit/host/test_options_extra_shadowing.py``).
"""

from otto.host.element import Element
from otto.host.login_proxy import Cred
from otto.host.options import FtpOptions, SshOptions, TelnetOptions
from otto.host.survey import engine
from otto.host.unix_host import UnixHost
from otto.logger.mode import LogMode


def _unix(**fields) -> UnixHost:
    fields.setdefault("valid_terms", ["ssh", "telnet"])
    fields.setdefault("valid_transfers", ["scp", "sftp", "ftp"])
    fields.setdefault("transfer", "scp")
    fields.setdefault("creds", [Cred(login="admin", password="pw")])
    return UnixHost(element=Element("u1"), name="u1", ip="127.0.0.1", log=LogMode.QUIET, **fields)


def test_the_declared_ssh_port_is_the_port_the_connect_dials() -> None:
    host = _unix(ssh_options=SshOptions(port=2222, extra={"proxy_command": "pc"}))
    dialled = host.ssh_options._kwargs(alias=host.id)["port"]
    assert engine._declared_port_unix(host, "ssh", "term") == dialled
    assert engine._declared_port_unix(host, "scp", "transfer") == dialled
    assert engine._declared_port_unix(host, "sftp", "transfer") == dialled


def test_the_declared_telnet_and_ftp_ports_are_the_ports_their_opens_use() -> None:
    host = _unix(
        term="telnet",
        telnet_options=TelnetOptions(port=2323, extra={"term": "vt100"}),
        ftp_options=FtpOptions(port=2121),
    )
    assert (
        engine._declared_port_unix(host, "telnet", "term")
        == host.telnet_options._open_kwargs()["port"]
    )
    assert engine._declared_port_unix(host, "ftp", "transfer") == host.ftp_options.port
