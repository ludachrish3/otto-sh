"""Runtime host menu fields + active resolution via to_host."""

import pytest

from otto.host.element import Element
from otto.host.embedded_host import ZephyrHost
from otto.host.login_proxy import Cred
from otto.host.unix_host import UnixHost
from otto.logger.mode import LogMode
from otto.models.host import EmbeddedHostSpec, UnixHostSpec


def _unix_spec(**kw):
    return UnixHostSpec(ip="10.0.0.1", creds=[{"login": "u", "password": "p"}], **kw)


def test_unix_defaults_active_is_menu_first():
    h = _unix_spec().to_host(element=Element("x"))
    assert h.term == "ssh"
    assert h.transfer == "scp"
    assert h.valid_terms == ["ssh", "telnet"]
    assert h.valid_transfers == ["scp", "sftp", "ftp", "nc"]


def test_unix_single_element_menu_sets_active():
    h = _unix_spec(valid_transfers="nc").to_host(element=Element("x"))
    assert h.valid_transfers == ["nc"]
    assert h.transfer == "nc"


def test_unix_pin_selects_within_menu():
    h = _unix_spec(valid_transfers=["scp", "nc"], transfer="nc").to_host(element=Element("x"))
    assert h.transfer == "nc"


def test_unix_pin_outside_menu_fails_loud():
    with pytest.raises(ValueError, match="transfer 'sftp' is not in"):
        _unix_spec(valid_transfers=["scp", "nc"], transfer="sftp").to_host(element=Element("x"))


def test_directly_built_unix_host_validates_active_against_menu():
    with pytest.raises(ValueError, match="transfer 'sftp' is not in"):
        UnixHost(
            ip="1.1.1.1",
            element=Element("x"),
            creds=[Cred(login="u", password="p")],
            transfer="sftp",
            valid_transfers=["scp"],
            log=LogMode.QUIET,
        )


def test_embedded_defaults_active():
    h = EmbeddedHostSpec(ip="192.0.2.1", command_frame="zephyr").to_host(element=Element("x"))
    assert h.term == "telnet"
    assert h.valid_terms == ["telnet"]
    assert h.transfer == "console"
    assert h.valid_transfers == ["console"]


def test_embedded_connection_uses_self_term_not_hardcoded():
    # The ConnectionManager is built with the host's own term, defaulting to telnet.
    # ZephyrHost (not a bare EmbeddedHost): EmbeddedHost requires a command_frame,
    # and ZephyrHost supplies the built-in "zephyr" frame, so it constructs without
    # an explicit frame argument.
    h = ZephyrHost(ip="192.0.2.1", element=Element("d"), log=LogMode.QUIET)
    assert h.term == "telnet"
    assert h._connections.term == "telnet"


def test_host_id_and_name_leave_the_element_id_out():
    from otto.host.unix_host import UnixHost

    h = UnixHost(
        ip="1.1.1.1",
        creds=[Cred(login="root", password="x")],
        element=Element("Test", id=5),
        board="BoardX",
        slot=2,
    )
    # The element's ``id`` is data: it reaches neither the id nor the name
    # (spec 2026-09-05 §2.1). The id is slugged, the name is as written.
    assert h.id == "test_boardx2"
    assert h.element.id == 5
    # name is the element name plus board/slot, exactly as written, no number
    # (spec 2026-09-05 §2.4); board/slot are space-separated, original case.
    assert h.name == "Test BoardX 2"

    h2 = UnixHost(ip="1.1.1.1", creds=[Cred(login="root", password="x")], element=Element("solo"))
    assert h2.id == "solo"
    assert h2.name == "solo"


def test_make_host_id_matches_built_host_id():
    from otto.host.remote_host import make_host_id
    from otto.host.unix_host import UnixHost

    assert make_host_id("Test", "BoardX", 2) == "test_boardx2"
    assert make_host_id("solo", None, None) == "solo"

    h = UnixHost(
        ip="1.1.1.1",
        creds=[Cred(login="root", password="x")],
        element=Element("Test", id=5),
        board="BoardX",
        slot=2,
    )
    assert make_host_id("Test", "BoardX", 2) == h.id
