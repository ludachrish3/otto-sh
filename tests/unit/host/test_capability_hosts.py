"""Runtime host menu fields + active resolution via to_host."""

import pytest

from otto.host.embedded_host import ZephyrHost
from otto.host.login_proxy import Cred
from otto.host.unix_host import UnixHost
from otto.logger.mode import LogMode
from otto.models.host import EmbeddedHostSpec, UnixHostSpec


def _unix_spec(**kw):
    return UnixHostSpec(ip="10.0.0.1", element="x", creds={"u": "p"}, **kw)


def test_unix_defaults_active_is_menu_first():
    h = _unix_spec().to_host()
    assert h.term == "ssh"
    assert h.transfer == "scp"
    assert h.valid_terms == ["ssh", "telnet"]
    assert h.valid_transfers == ["scp", "sftp", "ftp", "nc"]


def test_unix_single_element_menu_sets_active():
    h = _unix_spec(valid_transfers="nc").to_host()
    assert h.valid_transfers == ["nc"]
    assert h.transfer == "nc"


def test_unix_pin_selects_within_menu():
    h = _unix_spec(valid_transfers=["scp", "nc"], transfer="nc").to_host()
    assert h.transfer == "nc"


def test_unix_pin_outside_menu_fails_loud():
    with pytest.raises(ValueError, match="transfer 'sftp' is not in"):
        _unix_spec(valid_transfers=["scp", "nc"], transfer="sftp").to_host()


def test_directly_built_unix_host_validates_active_against_menu():
    with pytest.raises(ValueError, match="transfer 'sftp' is not in"):
        UnixHost(
            ip="1.1.1.1",
            element="x",
            creds=[Cred(login="u", password="p")],
            transfer="sftp",
            valid_transfers=["scp"],
            log=LogMode.QUIET,
        )


def test_embedded_defaults_active():
    h = EmbeddedHostSpec(ip="192.0.2.1", element="d", command_frame="zephyr").to_host()
    assert h.term == "telnet"
    assert h.valid_terms == ["telnet"]
    assert h.transfer == "console"
    assert h.valid_transfers == ["console"]


def test_embedded_connection_uses_self_term_not_hardcoded():
    # The ConnectionManager is built with the host's own term, defaulting to telnet.
    # ZephyrHost (not a bare EmbeddedHost): EmbeddedHost requires a command_frame,
    # and ZephyrHost supplies the built-in "zephyr" frame, so it constructs without
    # an explicit frame argument.
    h = ZephyrHost(ip="192.0.2.1", element="d", log=LogMode.QUIET)
    assert h.term == "telnet"
    assert h._connections.term == "telnet"


def test_host_id_and_name_render_element_id():
    from otto.host.unix_host import UnixHost

    h = UnixHost(
        ip="1.1.1.1",
        creds=[Cred(login="root", password="x")],
        element="Test",
        element_id=5,
        board="BoardX",
        slot=2,
    )
    assert h.id == "test5_boardx2"  # element_id is the NUMBER; id is lower-cased
    assert h.name == "Test5 BoardX2"  # original case, space-joined name

    h2 = UnixHost(ip="1.1.1.1", creds=[Cred(login="root", password="x")], element="solo")
    assert h2.id == "solo"
    assert h2.name == "solo"


def test_make_host_id_matches_built_host_id():
    from otto.host.remote_host import make_host_id
    from otto.host.unix_host import UnixHost

    assert make_host_id("Test", 5, "BoardX", 2) == "test5_boardx2"
    assert make_host_id("solo", None, None, None) == "solo"

    h = UnixHost(
        ip="1.1.1.1",
        creds=[Cred(login="root", password="x")],
        element="Test",
        element_id=5,
        board="BoardX",
        slot=2,
    )
    assert make_host_id("Test", 5, "BoardX", 2) == h.id
