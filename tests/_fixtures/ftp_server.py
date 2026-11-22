"""A real in-process ftp server for unit tests of the ftp transfer backend.

``aioftp.Server`` over a temporary directory on loopback, with an ephemeral
port: the replies and the sockets are the library's own, so a test that drives
otto's backend through it observes what a real server does (a refused STOR, a
final reply, a data connection) and not what a fake was written to do.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import aioftp


@asynccontextmanager
async def served_ftp(root: Path) -> AsyncIterator[aioftp.Client]:
    """Serve *root* over ftp and yield a logged-in client; both are closed on exit."""
    server = aioftp.Server(users=[aioftp.User(base_path=root)])
    await server.start("127.0.0.1", 0)
    port = server.server.sockets[0].getsockname()[1]
    client = aioftp.Client()
    try:
        await client.connect("127.0.0.1", port)
        await client.login()
        yield client
    finally:
        client.close()
        await server.close()
