"""session_setup and landing_frame on HostSpec and the runtime hosts."""

import pytest
from pydantic import ValidationError

from otto.host.command_frame import BashFrame, RawFrame, ZephyrFrame
from otto.host.element import Element
from otto.host.embedded_host import EmbeddedHost
from otto.host.session_setup import SESSION_SETUPS, SessionSetup, register_session_setup
from otto.host.unix_host import UnixHost
from otto.models.host import EmbeddedHostSpec, UnixHostSpec

_CREDS = [{"login": "admin", "password": "pw"}]
_PROXIED = [*_CREDS, {"login": "mysql", "proxy": "su", "via": "admin"}]
_E = Element("e")


async def _hook(session, ctx) -> None:
    return None


@pytest.fixture(autouse=True)
def _registered():
    register_session_setup("t6-hook", _hook, overwrite=True)
    yield
    SESSION_SETUPS.unregister("t6-hook")


def test_name_form_reaches_the_unix_host_and_its_manager():
    spec = UnixHostSpec(ip="10.0.0.1", creds=_CREDS, session_setup="t6-hook")
    host = spec.to_host(element=_E)
    assert host.session_setup == SessionSetup(name="t6-hook", params={})
    assert host._session_mgr._session_setup == host.session_setup
    assert host.landing_frame is None


def test_table_form_keeps_params():
    spec = UnixHostSpec(ip="10.0.0.1", creds=_CREDS, session_setup={"type": "t6-hook", "db": "x"})
    assert spec.to_host(element=_E).session_setup == SessionSetup(
        name="t6-hook", params={"db": "x"}
    )


def test_unknown_name_is_refused_at_validation_with_the_registered_names():
    with pytest.raises(ValidationError, match=r"t6-nope.*not a registered session setup.*t6-hook"):
        UnixHostSpec(ip="10.0.0.1", creds=_CREDS, session_setup="t6-nope")


def test_table_without_type_is_refused():
    with pytest.raises(ValidationError, match=r"must carry a string 'type'"):
        UnixHostSpec(ip="10.0.0.1", creds=_CREDS, session_setup={"db": "x"})


def test_landing_frame_requires_session_setup():
    with pytest.raises(ValidationError, match="landing_frame 'bash' requires session_setup"):
        UnixHostSpec(ip="10.0.0.1", creds=_CREDS, landing_frame="bash")


def test_landing_frame_unknown_name_is_refused():
    with pytest.raises(ValidationError, match="not a registered frame"):
        UnixHostSpec(ip="10.0.0.1", creds=_CREDS, session_setup="t6-hook", landing_frame="nope")


def test_command_frame_raw_is_refused():
    with pytest.raises(ValidationError, match=r"raw.*landing"):
        UnixHostSpec(ip="10.0.0.1", creds=_CREDS, session_setup="t6-hook", command_frame="raw")


def test_non_bash_landing_with_a_proxied_cred_is_refused():
    with pytest.raises(ValidationError, match="bash-family"):
        UnixHostSpec(ip="10.0.0.1", creds=_PROXIED, session_setup="t6-hook", landing_frame="raw")


def test_bash_landing_with_a_proxied_cred_is_allowed():
    spec = UnixHostSpec(
        ip="10.0.0.1", creds=_PROXIED, session_setup="t6-hook", landing_frame="bash"
    )
    host = spec.to_host(element=_E)
    assert isinstance(host.landing_frame, BashFrame)
    assert isinstance(host._session_mgr._landing_frame, BashFrame)


def test_embedded_accepts_a_raw_landing():
    spec = EmbeddedHostSpec(
        ip="192.0.2.1", command_frame="zephyr", session_setup="t6-hook", landing_frame="raw"
    )
    host = spec.to_host(element=Element("dut"))
    assert isinstance(host, EmbeddedHost)
    assert isinstance(host.landing_frame, RawFrame)
    assert isinstance(host._session_mgr._landing_frame, RawFrame)
    assert isinstance(host._session_mgr._command_frame, ZephyrFrame)


def test_direct_construction_coerces_strings():
    from otto.host.login_proxy import Cred

    host = UnixHost(
        ip="10.0.0.1",
        element=_E,
        creds=[Cred(login="a", password="p")],
        session_setup="t6-hook",
        landing_frame="bash",
    )
    assert host.session_setup == SessionSetup(name="t6-hook")
    assert isinstance(host.landing_frame, BashFrame)


def test_rebuild_connections_forwards_both_keywords_too():
    """``rebuild_connections`` builds a second ``SessionManager`` from scratch;
    both keywords must repeat there, not just in ``__post_init__``."""
    from otto.host.login_proxy import Cred

    host = UnixHost(
        ip="10.0.0.1",
        element=_E,
        creds=[Cred(login="a", password="p")],
        session_setup="t6-hook",
        landing_frame="bash",
    )
    host.rebuild_connections()
    assert host._session_mgr._session_setup == SessionSetup(name="t6-hook")
    assert isinstance(host._session_mgr._landing_frame, BashFrame)


def test_a_profile_can_default_both_fields_for_a_fleet():
    """A shared landing + manoeuvre rides an os_profile; each host selects it with one key."""
    from otto.host.factory import create_host_from_dict
    from otto.host.os_profile import OS_PROFILES, register_os_profile

    register_os_profile(
        "t6-fleet",
        "unix",
        defaults={
            "landing_frame": "bash",
            "session_setup": {"type": "t6-hook", "db": "x"},
        },
    )
    try:
        host = create_host_from_dict(
            {"ip": "10.0.0.1", "os_type": "t6-fleet", "creds": _CREDS}, element=_E
        )
        assert host.session_setup == SessionSetup(name="t6-hook", params={"db": "x"})
        assert isinstance(host.landing_frame, BashFrame)
        # A host's own key wins over the profile, field by field.
        own = create_host_from_dict(
            {"ip": "10.0.0.1", "os_type": "t6-fleet", "creds": _CREDS, "session_setup": "t6-hook"},
            element=_E,
        )
        assert own.session_setup == SessionSetup(name="t6-hook")
        assert isinstance(own.landing_frame, BashFrame)
    finally:
        OS_PROFILES.unregister("t6-fleet")
