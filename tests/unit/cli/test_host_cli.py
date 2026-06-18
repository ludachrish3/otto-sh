"""Dynamic registry-driven tab completion for `otto host --term/--transfer` (WS#4)."""

import click

from otto.cli.host import _term_completer, _transfer_completer


def _ctx():
    return click.Context(click.Command("host"))


def test_term_completer_includes_builtins():
    names = _term_completer(_ctx(), "")
    assert "ssh" in names and "telnet" in names


def test_term_completer_filters_by_prefix():
    assert _term_completer(_ctx(), "te") == ["telnet"]


def test_transfer_completer_offers_unix_protocols_only():
    names = _transfer_completer(_ctx(), "")
    assert {"scp", "sftp", "ftp", "nc"} <= set(names)
    assert "console" not in names  # embedded-only, not offered for the unix override


def test_transfer_completer_surfaces_custom_unix_backend():
    from otto.host import transfer as xfer_mod
    from otto.host.transfer import FileTransfer

    class XmodemTransfer(FileTransfer):
        host_families = frozenset({"unix"})

    saved = dict(xfer_mod._TRANSFER_BACKENDS)
    xfer_mod._TRANSFER_BACKENDS["xmodem"] = XmodemTransfer
    try:
        assert "xmodem" in _transfer_completer(_ctx(), "")
    finally:
        xfer_mod._TRANSFER_BACKENDS.clear()
        xfer_mod._TRANSFER_BACKENDS.update(saved)
