"""A host hands out a netcat backend whatever its own transfer is.

The survey measures netcat's listener checks through the host's netcat backend,
even on a host that moves files with scp or shell. ``_nc_backend_for`` is the one
way to get it: the cached backend when the host's transfer is nc, one built for
the call otherwise.
"""

from unittest.mock import patch

import pytest

import otto.host.unix_host as unix_host_mod
from otto.host import UnixHost
from otto.host.element import Element
from otto.host.errors import ConsoleError
from otto.host.login_proxy import Cred
from otto.host.options import ConsoleOptions, NcOptions
from otto.host.transfer import NcFileTransfer
from otto.logger.mode import LogMode


def _host(transfer: str) -> UnixHost:
    return UnixHost(
        ip="10.0.0.1",
        element=Element(name="box"),
        creds=[Cred(login="user", password="pass")],
        transfer=transfer,
        valid_transfers=["scp", "sftp", "shell", "nc"],
        log=LogMode.QUIET,
    )


def test_a_host_whose_transfer_is_nc_hands_out_its_cached_backend() -> None:
    host = _host("nc")
    assert host._nc_backend_for(None) is host._transfer_for(None)
    assert host._nc_backend_for("postgres") is host._transfer_for("postgres")
    assert host._nc_backend_for("postgres") is host._nc_backend_for("postgres")


def test_a_host_whose_transfer_is_scp_builds_a_netcat_backend_on_its_own_connections() -> None:
    host = _host("scp")
    own = host._file_transfer
    cached = dict(host._user_transfers)

    for user in (None, "postgres"):
        backend = host._nc_backend_for(user)
        assert isinstance(backend, NcFileTransfer)
        assert backend is not own

    assert host._file_transfer is own
    assert not isinstance(own, NcFileTransfer)
    assert host._user_transfers == cached


def test_a_console_host_refuses_the_netcat_backend_by_name() -> None:
    host = UnixHost(
        ip="10.0.0.2",
        element=Element(name="test2"),
        creds=[Cred(login="test", password="pw")],
        term="console",
        valid_terms=["ssh", "console"],
        transfer="shell",
        valid_transfers=["shell", "nc"],
        console_options=ConsoleOptions(server="test1", port=4001),
        log=LogMode.QUIET,
    )
    assert host.transfer == "shell"
    with pytest.raises(ConsoleError, match=r"^test2: .*single-client.*nc.*second"):
        host._nc_backend_for(None)


def test_invalid_nc_options_surface_from_the_build() -> None:
    # An scp host never builds the netcat backend, so its nc_options are
    # accepted as written and the first build is the survey's.
    host = UnixHost(
        ip="10.0.0.1",
        element=Element(name="box"),
        creds=[Cred(login="user", password="pass")],
        transfer="scp",
        valid_transfers=["scp", "nc"],
        nc_options=NcOptions(max_concurrent_transfers=0),
        log=LogMode.QUIET,
    )
    with pytest.raises(ValueError, match="max_concurrent_transfers must be at least 1"):
        host._nc_backend_for(None)


def test_the_registry_is_asked_for_nc_and_the_context_says_nc() -> None:
    host = _host("scp")
    with patch.object(unix_host_mod, "build_transfer_backend") as build:
        host._build_file_transfer("postgres", name="nc")
    assert build.call_args.kwargs["name"] == "nc"
    assert build.call_args.kwargs["ctx"].transfer == "nc"
