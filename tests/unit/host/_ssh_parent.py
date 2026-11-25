"""An ssh ``UnixHost`` for a test that drives a host as a container's parent."""

from unittest.mock import AsyncMock, MagicMock

from otto.host import UnixHost
from otto.host.element import Element
from otto.host.login_proxy import Cred
from otto.logger.mode import LogMode


def ssh_parent_host(*, element: str = "box") -> UnixHost:
    """An ssh ``UnixHost`` whose connection manager is a mock that opens nothing."""
    host = UnixHost(
        ip="10.0.0.1",
        element=Element(name=element),
        creds=[Cred(login="u", password="p")],
        term="ssh",
        log=LogMode.QUIET,
    )
    connections = MagicMock()
    connections.term = "ssh"
    connections.proxy_hops = []
    connections.ssh = AsyncMock(return_value=None)
    host._session_mgr._connections = connections
    return host
