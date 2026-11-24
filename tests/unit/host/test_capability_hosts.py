"""Runtime host menu fields + active resolution via to_host."""

from dataclasses import dataclass, field

import pytest

from otto.host.capability import is_default_selection
from otto.host.element import Element
from otto.host.embedded_host import ZephyrHost
from otto.host.login_proxy import Cred
from otto.host.options import ConsoleOptions
from otto.host.unix_host import UnixHost
from otto.logger.mode import LogMode
from tests._fixtures.host_build import build_host


def _unix_host(**kw):
    return build_host(
        {"ip": "10.0.0.1", "creds": [{"login": "u", "password": "p"}], **kw},
        element=Element(name="x"),
    )


def test_unix_defaults_active_is_menu_first():
    h = _unix_host()
    assert h.term == "ssh"
    assert h.transfer == "scp"
    assert h.valid_terms == ["ssh", "telnet"]
    assert h.valid_transfers == ["scp", "sftp", "ftp", "nc"]


def test_unix_single_element_menu_sets_active():
    h = _unix_host(valid_transfers="nc")
    assert h.valid_transfers == ["nc"]
    assert h.transfer == "nc"


def test_unix_pin_selects_within_menu():
    h = _unix_host(valid_transfers=["scp", "nc"], transfer="nc")
    assert h.transfer == "nc"


def test_unix_pin_outside_menu_fails_loud():
    with pytest.raises(ValueError, match="transfer 'sftp' is not in"):
        _unix_host(valid_transfers=["scp", "nc"], transfer="sftp")


def test_directly_built_unix_host_validates_active_against_menu():
    with pytest.raises(ValueError, match="transfer 'sftp' is not in"):
        UnixHost(
            ip="1.1.1.1",
            element=Element(name="x"),
            creds=[Cred(login="u", password="p")],
            transfer="sftp",
            valid_transfers=["scp"],
            log=LogMode.QUIET,
        )


def test_embedded_defaults_active():
    h = build_host(
        {"ip": "192.0.2.1", "os_type": "embedded", "command_frame": "zephyr"},
        element=Element(name="x"),
    )
    assert h.term == "telnet"
    assert h.valid_terms == ["telnet"]
    assert h.transfer == "console"
    assert h.valid_transfers == ["console"]


def test_embedded_connection_uses_self_term_not_hardcoded():
    # The ConnectionManager is built with the host's own term, defaulting to telnet.
    # ZephyrHost (not a bare EmbeddedHost): EmbeddedHost requires a command_frame,
    # and ZephyrHost supplies the built-in "zephyr" frame, so it constructs without
    # an explicit frame argument.
    h = ZephyrHost(ip="192.0.2.1", element=Element(name="d"), log=LogMode.QUIET)
    assert h.term == "telnet"
    assert h._connections.term == "telnet"


def test_host_id_and_name_leave_the_element_id_out():
    from otto.host.unix_host import UnixHost

    h = UnixHost(
        ip="1.1.1.1",
        creds=[Cred(login="root", password="x")],
        element=Element(name="Test", id=5),
        board="BoardX",
        slot=2,
    )
    # The element's ``id`` is data: it reaches neither the id nor the name
    # (spec 2026-09-05 §2.1). The id is slugged, the name is as written.
    assert h.id == "test-boardx-2"
    assert h.element.id == 5
    # name is the element name plus board/slot, exactly as written, no number
    # (spec 2026-09-05 §2.4); board/slot are space-separated, original case.
    assert h.name == "Test BoardX 2"

    h2 = UnixHost(
        ip="1.1.1.1", creds=[Cred(login="root", password="x")], element=Element(name="solo")
    )
    assert h2.id == "solo"
    assert h2.name == "solo"


def test_make_host_id_matches_built_host_id():
    from otto.host.remote_host import make_host_id
    from otto.host.unix_host import UnixHost

    assert make_host_id("Test", "BoardX", 2) == "test-boardx-2"
    assert make_host_id("solo", None, None) == "solo"

    h = UnixHost(
        ip="1.1.1.1",
        creds=[Cred(login="root", password="x")],
        element=Element(name="Test", id=5),
        board="BoardX",
        slot=2,
    )
    assert make_host_id("Test", "BoardX", 2) == h.id


def _direct(cls=UnixHost, **kw):
    return cls(
        ip="1.1.1.1",
        element=Element(name="x"),
        creds=[Cred(login="u", password="p")],
        log=LogMode.QUIET,
        **kw,
    )


def test_a_list_passed_to_the_constructor_selects_its_first_entry():
    h = _direct(valid_transfers=["nc", "sftp"])  # #1295, direct construction
    assert h.transfer == "nc"
    assert h.valid_transfers == ["nc", "sftp"]
    assert is_default_selection(h.transfer)


def test_an_explicit_pin_equal_to_the_class_default_is_a_pin():
    h = _direct(transfer="scp", valid_transfers=["nc", "scp"])
    assert h.transfer == "scp"
    assert not is_default_selection(h.transfer)
    with pytest.raises(ValueError, match="transfer 'scp' is not in this host's transfer menu"):
        _direct(transfer="scp", valid_transfers=["nc", "sftp"])


def test_a_subclass_plain_default_is_a_pin_and_a_redeclared_list_yields():
    @dataclass(slots=True, kw_only=True)
    class Pinned(UnixHost):
        transfer: str = "ftp"

    @dataclass(slots=True, kw_only=True)
    class Yielding(UnixHost):
        valid_transfers: list[str] = field(default_factory=lambda: ["ftp", "scp"])

    with pytest.raises(ValueError, match="transfer 'ftp' is not in this host's transfer menu"):
        _direct(Pinned, valid_transfers=["nc", "scp"])
    assert _direct(Yielding).transfer == "ftp"
    assert is_default_selection(_direct(Yielding).transfer)
    assert _direct(Yielding, valid_transfers=["nc", "scp"]).transfer == "nc"


def test_the_console_rule_holds_in_the_constructor():
    console = {
        "valid_terms": ["console"],
        "console_options": ConsoleOptions(server="hub", port=4001),
    }
    h = _direct(valid_transfers=["nc", "shell"], **console)
    assert (h.term, h.transfer, h.valid_transfers) == ("console", "shell", ["nc", "shell"])
    with pytest.raises(ValueError, match="transfer 'nc' cannot run on a console term"):
        _direct(valid_transfers=["nc", "shell"], transfer="nc", **console)
    with pytest.raises(ValueError, match="leaves a console term no transfer"):
        _direct(valid_transfers=["nc"], **console)
