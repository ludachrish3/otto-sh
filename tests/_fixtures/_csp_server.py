"""Serve a directory over HTTP from a subpath, with the documented minimal CSP header.

Backs the ``report_browser`` suite's served-CSP lane (test_spa_csp.py): the
coverage-report SPA is also delivered by Jenkins' ``DirectoryBrowserSupport``
CSP header (spec §2), a considerably stricter policy than a browser's default
``file://`` origin. ``file://`` tests alone can't prove the bundle survives it
(no inline scripts, no eval, no unexpected fetches) — this fixture actually
serves the report over HTTP with that exact header attached to every
response, from a non-root subpath (``SUBPATH``), so the suite exercises the
real constraint instead of assuming it.
"""

import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; font-src 'self'; base-uri 'none'; form-action 'none'"
)
SUBPATH = "/job/artifacts"  # exercised depth — report must be path-agnostic


class _Handler(SimpleHTTPRequestHandler):
    def translate_path(self, path):
        if path.startswith(SUBPATH):
            path = path[len(SUBPATH) :] or "/"
        return super().translate_path(path)

    def end_headers(self):
        self.send_header("Content-Security-Policy", CSP)
        super().end_headers()

    def log_message(self, *args):  # keep pytest output clean
        pass


class CspReportServer:
    def __init__(self, directory):
        handler = partial(_Handler, directory=str(directory))
        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)

    @property
    def url(self):
        return f"http://127.0.0.1:{self._httpd.server_port}{SUBPATH}/index.html"

    def start(self):
        self._thread.start()
        return self

    def stop(self):
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=5)
