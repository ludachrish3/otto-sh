"""The one seam otto meets asyncssh's process-wide state through.

Every ``asyncssh.connect`` otto makes goes through
``otto.host.connections.ssh_connect``; these pin what that seam does around
the call — the deprecation filter, the opt-in asyncssh debug level, and the
two DEBUG descriptions the call sites log — against fakes. The filter's
match itself is proved by injection in ``test_legacy_ssh_algorithms.py``.
"""

import logging
import warnings
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from cryptography.utils import CryptographyDeprecationWarning

from otto.host.connections import (
    FFDH_DEPRECATION_MESSAGE,
    apply_ssh_debug_level,
    describe_connect_kwargs,
    describe_negotiated,
    install_asyncssh_warning_filters,
    ssh_connect,
)

FFDH_TEXT = (
    "Diffie-Hellman over finite fields (FFDH) is deprecated and support will be removed "
    "in a future release. Use a more modern key exchange algorithm."
)


def _ffdh_filters() -> list:
    return [
        f for f in warnings.filters if f[1] is not None and f[1].pattern == FFDH_DEPRECATION_MESSAGE
    ]


def test_the_ffdh_filter_is_installed_once_however_often_it_is_asked():
    """Every connect installs; a filter list that grows per connect is a leak."""
    with warnings.catch_warnings():
        warnings.resetwarnings()
        warnings.simplefilter("error")
        assert _ffdh_filters() == []
        install_asyncssh_warning_filters()
        install_asyncssh_warning_filters()
        install_asyncssh_warning_filters()
        assert len(_ffdh_filters()) == 1
        assert warnings.filters[0] == _ffdh_filters()[0]


def test_the_ffdh_filter_is_moved_back_to_the_front_when_a_later_error_entry_shadows_it():
    """pytest resets the filter list per test and re-applies its ini entries, and
    any caller can ``simplefilter("error")`` after otto installed. A copy that
    sits behind a newer ``error`` entry is not installed in any sense that
    matters, so install keeps exactly one copy and keeps it first."""
    with warnings.catch_warnings():
        warnings.resetwarnings()
        install_asyncssh_warning_filters()
        warnings.simplefilter("error")
        assert warnings.filters[0][0] == "error"
        install_asyncssh_warning_filters()
        assert len(_ffdh_filters()) == 1
        assert warnings.filters[0] == _ffdh_filters()[0]
        warnings.warn_explicit(
            FFDH_TEXT,
            CryptographyDeprecationWarning,
            filename="dh.py",
            lineno=30,
            module="asyncssh.crypto.dh",
        )  # must not raise


def test_ssh_debug_env_sets_asyncssh_s_own_level(monkeypatch):
    import asyncssh
    from asyncssh.logging import SSHLogger

    monkeypatch.setenv("OTTO_SSH_DEBUG", "2")
    try:
        apply_ssh_debug_level()
        assert SSHLogger._debug_level == 2
    finally:
        asyncssh.set_debug_level(1)


def test_ssh_debug_env_unset_leaves_asyncssh_alone(monkeypatch):
    import asyncssh
    from asyncssh.logging import SSHLogger

    monkeypatch.delenv("OTTO_SSH_DEBUG", raising=False)
    asyncssh.set_debug_level(3)
    try:
        apply_ssh_debug_level()
        assert SSHLogger._debug_level == 3
    finally:
        asyncssh.set_debug_level(1)


@pytest.mark.parametrize("value", ["0", "4", "loud", ""])
def test_ssh_debug_env_outside_asyncssh_s_range_warns_and_changes_nothing(
    monkeypatch, caplog, value
):
    """asyncssh accepts 1..3 and raises on anything else; a typo in an env var
    must not turn every connect into a ValueError, and must not pass silently."""
    import asyncssh
    from asyncssh.logging import SSHLogger

    monkeypatch.setenv("OTTO_SSH_DEBUG", value)
    asyncssh.set_debug_level(1)
    with caplog.at_level(logging.WARNING, logger="otto.host.connections"):
        apply_ssh_debug_level()
    assert SSHLogger._debug_level == 1
    assert "OTTO_SSH_DEBUG" in caplog.text
    assert "1..3" in caplog.text


def test_ssh_debug_env_lifts_the_asyncssh_logger_floor_so_level_2_prints(monkeypatch):
    """C1: raising asyncssh's internal debug level is not enough — otto's own
    ``asyncssh`` logger floor (WARNING by default) must also lift, or the CLI
    user who set OTTO_SSH_DEBUG=2 still sees nothing under --log-level DEBUG."""
    import asyncssh
    from asyncssh.logging import SSHLogger

    from otto.logger import management

    monkeypatch.setenv("OTTO_SSH_DEBUG", "2")
    saved_debug_level = SSHLogger._debug_level
    try:
        management.install_console("DEBUG")
        apply_ssh_debug_level()
        assert logging.getLogger("asyncssh").isEnabledFor(logging.DEBUG)
    finally:
        management.reset()
        asyncssh.set_debug_level(saved_debug_level)


def test_ssh_debug_env_unset_leaves_the_asyncssh_logger_floor_at_warning(monkeypatch):
    """Without the env var, otto's WARNING floor on ``asyncssh`` holds."""
    import asyncssh
    from asyncssh.logging import SSHLogger

    from otto.logger import management

    monkeypatch.delenv("OTTO_SSH_DEBUG", raising=False)
    saved_debug_level = SSHLogger._debug_level
    try:
        management.install_console("DEBUG")
        apply_ssh_debug_level()
        assert not logging.getLogger("asyncssh").isEnabledFor(logging.DEBUG)
    finally:
        management.reset()
        asyncssh.set_debug_level(saved_debug_level)


def test_ssh_debug_env_malformed_leaves_the_asyncssh_logger_floor_at_warning(monkeypatch):
    """A malformed value is reported and ignored — it lifts nothing."""
    import asyncssh
    from asyncssh.logging import SSHLogger

    from otto.logger import management

    monkeypatch.setenv("OTTO_SSH_DEBUG", "loud")
    saved_debug_level = SSHLogger._debug_level
    try:
        management.install_console("DEBUG")
        apply_ssh_debug_level()
        assert not logging.getLogger("asyncssh").isEnabledFor(logging.DEBUG)
    finally:
        management.reset()
        asyncssh.set_debug_level(saved_debug_level)


def test_ssh_debug_env_overrides_a_repos_own_asyncssh_logging_level(monkeypatch):
    """N1: the CLI applies a repo's [logging.levels] once at startup, BEFORE
    any connect; apply_ssh_debug_level runs on every connect, afterwards, and
    apply_library_levels remembers its override on the state — so a valid
    OTTO_SSH_DEBUG overrides a repo's own asyncssh entry from the first
    connect on, not the other way around. This is the ruled behaviour (an
    explicit per-invocation opt-in beats a standing noise-floor entry), not a
    bug: connections.py's docstring, settings.md and the spec all say so."""
    import asyncssh
    from asyncssh.logging import SSHLogger

    from otto.logger import management

    monkeypatch.setenv("OTTO_SSH_DEBUG", "2")
    saved_debug_level = SSHLogger._debug_level
    try:
        management.install_console("DEBUG")
        management.apply_library_levels({"asyncssh": "ERROR"})  # a repo's [logging.levels]
        assert not logging.getLogger("asyncssh").isEnabledFor(logging.DEBUG)
        apply_ssh_debug_level()
        assert logging.getLogger("asyncssh").isEnabledFor(logging.DEBUG)
    finally:
        management.reset()
        asyncssh.set_debug_level(saved_debug_level)


def test_describe_connect_kwargs_redacts_secrets_and_keeps_the_algorithm_lists():
    text = describe_connect_kwargs(
        "bb1350",
        "198.51.100.17",
        {
            "username": "root",
            "password": "s3cret",
            "tunnel": None,
            "port": 22,
            "kex_algs": ["diffie-hellman-group1-sha1"],
        },
    )
    assert text.startswith("bb1350: asyncssh.connect('198.51.100.17') kwargs=")
    assert "s3cret" not in text
    assert "'password': '***'" in text
    assert "'kex_algs': ['diffie-hellman-group1-sha1']" in text
    assert "'username': 'root'" in text


def test_describe_connect_kwargs_redacts_passphrase_too():
    text = describe_connect_kwargs(
        "bb1350",
        "198.51.100.17",
        {
            "username": "root",
            "passphrase": "k3y-pw",
        },
    )
    assert "k3y-pw" not in text
    assert "'passphrase': '***'" in text


def test_describe_connect_kwargs_shows_an_absent_password_as_none():
    """LOGINLESS hosts connect with password=None; that is worth seeing, not hiding."""
    text = describe_connect_kwargs("dev", "127.0.0.1", {"username": "", "password": None})
    assert "'password': None" in text


def test_describe_negotiated_reads_the_extra_info_asyncssh_exposes():
    info = {
        "server_version": "SSH-2.0-dropbear_2012.55",
        "send_cipher": "aes256-ctr",
        "recv_cipher": "aes256-ctr",
        "send_mac": "hmac-sha1",
        "recv_mac": "hmac-sha1",
        "send_compression": "none",
        "recv_compression": "none",
    }
    conn = MagicMock()
    conn.get_extra_info.side_effect = lambda key, default=None: info.get(key, default)
    assert describe_negotiated("bb1350", conn) == (
        "bb1350: negotiated server='SSH-2.0-dropbear_2012.55' "
        "cipher=aes256-ctr/aes256-ctr mac=hmac-sha1/hmac-sha1 compression=none/none"
    )


@pytest.mark.asyncio
async def test_ssh_connect_prepares_the_seam_before_connecting(monkeypatch):
    """Filter and debug level are applied on the way in, every time."""
    import asyncssh
    from asyncssh.logging import SSHLogger

    monkeypatch.setenv("OTTO_SSH_DEBUG", "3")
    fake = MagicMock()
    with (
        warnings.catch_warnings(),
        patch("asyncssh.connect", AsyncMock(return_value=fake)) as connect,
    ):
        warnings.simplefilter("error")
        try:
            result = await ssh_connect("10.0.0.1", username="u", password="p")
            assert result is fake
            connect.assert_awaited_once_with("10.0.0.1", username="u", password="p")
            assert len(_ffdh_filters()) == 1
            assert SSHLogger._debug_level == 3
        finally:
            asyncssh.set_debug_level(1)
