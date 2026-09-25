"""Registry, runtime value, and lab-data coercion for session setup hooks."""

import pytest

from otto.host.command_frame import BashFrame
from otto.host.errors import ConsoleError, RawLandingError
from otto.host.session_setup import (
    SESSION_SETUPS,
    SessionSetup,
    SessionSetupError,
    SetupContext,
    apply_session_setup,
    register_session_setup,
    session_setup_from_spec,
)


async def _noop(session, ctx) -> None:  # signature is the contract
    return None


@pytest.fixture
def registered():
    register_session_setup("t1-noop", _noop)
    try:
        yield "t1-noop"
    finally:
        SESSION_SETUPS.unregister("t1-noop")


def test_register_then_get_returns_the_callable(registered):
    assert SESSION_SETUPS.get(registered) is _noop


def test_duplicate_name_is_refused_without_overwrite(registered):
    with pytest.raises(ValueError, match="t1-noop"):
        register_session_setup(registered, _noop)


def test_overwrite_replaces(registered):
    async def other(session, ctx) -> None:
        return None

    register_session_setup(registered, other, overwrite=True)
    assert SESSION_SETUPS.get(registered) is other


def test_unknown_name_lists_registered_names(registered):
    with pytest.raises(ValueError, match=r"Registered: .*t1-noop"):
        SESSION_SETUPS.get("t1-nope")


def test_from_spec_none_passes_through():
    assert session_setup_from_spec(None) is None


def test_from_spec_instance_passes_through(registered):
    v = SessionSetup(name=registered, params={"a": 1})
    assert session_setup_from_spec(v) is v


def test_from_spec_string_is_a_name_with_no_params(registered):
    v = session_setup_from_spec(registered)
    assert v == SessionSetup(name=registered, params={})


def test_from_spec_table_pops_type_and_keeps_the_rest_as_params(registered):
    v = session_setup_from_spec({"type": registered, "db": "x", "n": 2})
    assert v == SessionSetup(name=registered, params={"db": "x", "n": 2})


def test_from_spec_table_without_type_is_refused():
    with pytest.raises(ValueError, match="type"):
        session_setup_from_spec({"db": "x"})


def test_from_spec_table_unknown_name_is_refused_at_coercion():
    with pytest.raises(ValueError, match="t1-nope"):
        session_setup_from_spec({"type": "t1-nope"})


def test_from_spec_unknown_name_is_refused_at_coercion():
    with pytest.raises(ValueError, match="t1-nope"):
        session_setup_from_spec("t1-nope")


def test_from_spec_other_types_are_refused():
    with pytest.raises(ValueError, match="cannot build"):
        session_setup_from_spec(42)


def test_context_is_frozen():
    ctx = SetupContext(host_id="h", host_name="h", user="u", params={}, kind="default")
    with pytest.raises(AttributeError):
        ctx.user = "x"  # type: ignore[misc]


def test_session_setup_is_frozen():
    setup = SessionSetup(name="t1-noop")
    with pytest.raises(AttributeError):
        setup.name = "x"  # type: ignore[misc]


def test_error_types():
    assert issubclass(SessionSetupError, ConnectionError)
    assert issubclass(RawLandingError, RuntimeError)


@pytest.mark.asyncio
async def test_a_console_refusal_at_frame_entry_reaches_the_caller_unchanged():
    """I2: a bad password typed by console_login() surfaces only once the
    post-hook frame-entry handshake times out — as a ConsoleError raised by
    session.enter_frame() (TelnetSession._fail_init -> _refused_login ->
    ConsoleClient.refused_after_login, in the real path). That message
    already names the console and the exact refusal; apply_session_setup
    must let it through unchanged rather than relabelling it as a generic
    "left no shell that answers frame" failure.
    """

    class _RefusedAtFrameEntry:
        async def enter_frame(self, frame) -> None:
            raise ConsoleError("test2: console test1:4001 (dial=ssh) login refused for 'root'")

    async def hook(session, ctx) -> None:
        return None

    register_session_setup("t1-console-refused", hook, overwrite=True)
    try:
        ctx = SetupContext(
            host_id="test2", host_name="test2", user="root", params={}, kind="default"
        )
        with pytest.raises(ConsoleError, match="login refused for 'root'") as exc_info:
            await apply_session_setup(
                _RefusedAtFrameEntry(),
                object(),
                ctx,
                SessionSetup(name="t1-console-refused"),
                BashFrame(),
            )
        # Not relabelled — the operator reads the actual refusal, not a
        # generic "left no shell" diagnosis.
        assert "left no shell" not in str(exc_info.value)
    finally:
        SESSION_SETUPS.unregister("t1-console-refused")
