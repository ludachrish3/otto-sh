"""StaticShellHarness — the built dashboard with no otto server behind it.

A plain file server, as any static host serves the built ``dist/``: ``/``
answers the built ``index.html`` and ``/static/...`` the directory
:class:`~otto.monitor.server.MonitorServer` mounts there, both resolved by
the server module itself, and every other path (every ``/api/*``) answers
404. The dashboard then boots as it does from any static host: its stream
cannot open, no ``/api/mode`` answers, and Import loads a file into the tab
(``web/src/data/bootstrap.ts``). An otto server that answers ``/api/mode``,
even one with nothing recording yet, is not that: its source decides Import.
"""

from http.server import SimpleHTTPRequestHandler
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from otto.monitor import server as monitor_server
from tests._fixtures._static_server import ThreadedStaticServer

if TYPE_CHECKING:
    from playwright.sync_api import Page


class _ShellHandler(SimpleHTTPRequestHandler):
    """``/`` is the built index, ``/static/...`` the server's static mount, anything else 404."""

    def translate_path(self, path: str) -> str:
        route = urlsplit(path).path
        if route == "/":
            return str(monitor_server._dist_index_path())
        if route.startswith("/static/"):
            return super().translate_path(route.removeprefix("/static"))
        return str(Path(self.directory) / "__no_such_route__")

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 — the stdlib's name
        """Keep pytest's output clean."""


class StaticShellHarness(ThreadedStaticServer):
    """Serve the built dashboard on ``127.0.0.1:<ephemeral>`` with no ``/api/*`` at all."""

    def __init__(self) -> None:
        super().__init__(_ShellHandler, monitor_server._STATIC_DIR, name="static-shell")

    @property
    def url(self) -> str:
        """The page URL: the server's root."""
        return f"{self.origin}/"


def import_into_shell(page: "Page", path: Path) -> None:
    """Load *path* into the no-server shell *page* shows, through its Import input.

    The ONE first Import of a page served with no otto server. The boot
    withholds Import until it has concluded that no server serves the page,
    and a file handed to the input before then is ignored; the empty
    state's Import button shows once it is offered, so this waits for it,
    then for the review bar the loaded file brings.
    """
    page.locator('[data-testid="empty-import-btn"]').wait_for()
    page.locator('[data-testid="import-input"]').set_input_files(path)
    page.locator('[data-testid="review-bar"]').wait_for()
