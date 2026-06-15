from otto.host.command_frame import BashFrame
from otto.host.unix_host import UnixHost


def _unix(**kw):
    return UnixHost(ip="10.0.0.1", creds={"u": "p"}, element="e", **kw)


def test_unix_command_frame_defaults_none_preserves_bash_behavior():
    # None means "let SessionManager apply its built-in BashFrame" — the exact
    # historical behavior (UnixHost never passed a frame before).
    h = _unix()
    assert h.command_frame is None
    assert h._session_mgr._command_frame is None


def test_unix_accepts_command_frame_instance_and_threads_it():
    f = BashFrame()
    h = _unix(command_frame=f)
    assert h.command_frame is f
    assert h._session_mgr._command_frame is f


def test_unix_coerces_command_frame_string():
    h = _unix(command_frame="bash")
    assert isinstance(h.command_frame, BashFrame)
    assert isinstance(h._session_mgr._command_frame, BashFrame)
