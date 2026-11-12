"""ThreadedStaticServer — a directory served over HTTP on ``127.0.0.1:<ephemeral>``.

The ONE lifecycle of the suite's plain static file servers (the coverage
report's served-CSP lane, the dashboard's no-server shell): built, started
and stopped the same way, so each differs only in its request handler and
the page URL it hands a browser. The server runs in a daemon thread, the
standard library's own model for it: this is test infrastructure a sync
Playwright page drives, outside any event loop.
"""

import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from typing_extensions import Self


class ThreadedStaticServer:
    """Serve *directory* through *handler* on ``127.0.0.1:<ephemeral>``, in a daemon thread."""

    def __init__(
        self,
        handler: type[SimpleHTTPRequestHandler],
        directory: "str | Path",
        *,
        name: "str | None" = None,
    ) -> None:
        bound = partial(handler, directory=str(directory))
        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), bound)
        self._thread = threading.Thread(target=self._httpd.serve_forever, name=name, daemon=True)

    @property
    def origin(self) -> str:
        """``http://127.0.0.1:<port>``, the server's origin with no path."""
        return f"http://127.0.0.1:{self._httpd.server_port}"

    def start(self) -> Self:
        """Start serving; returns the server, for ``Server(...).start()``."""
        self._thread.start()
        return self

    def stop(self) -> None:
        """Stop serving, close the socket and join the thread."""
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=5)
