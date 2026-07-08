"""Monitor attributes series/log rows by host.id, not the display name."""

from pathlib import Path

from otto.host.login_proxy import Cred
from otto.host.unix_host import UnixHost


def _host(**over):
    base = {
        "ip": "10.0.0.1",
        "element": "server",
        "name": "Friendly Label",
        "creds": [Cred(login="user", password="pass")],
    }
    base.update(over)
    return UnixHost(**base)


def test_id_and_name_diverge_for_this_host():
    # A construction-time name override makes name != id, so attribution source matters.
    h = _host()
    assert h.id == "server"
    assert h.name == "Friendly Label"


def test_collector_attributes_by_id():
    # Both attribution SOURCES (shell + SNMP) feed target.host.id; diagnostic
    # log messages may still use the display name.
    import re

    from otto.monitor import collector as collector_mod

    text = Path(collector_mod.__file__).read_text()
    # Shell path: _process_host_results(...) is called with the id (tolerate newline/indent).
    assert re.search(r"_process_host_results\(\s*target\.host\.id", text)
    # SNMP path: the host_name source is the id, not the name.
    assert "host_name = target.host.id" in text
    assert "host_name = target.host.name" not in text
