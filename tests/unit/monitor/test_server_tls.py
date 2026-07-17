"""TLS-mode MonitorServer: https URLs, Secure cookie, real TLS handshake.

Certificates are throwaway self-signed PEMs minted into tmp_path with the
openssl CLI (universally present on the Linux targets this repo supports;
no trustme/cryptography dev-dep needed).
"""

import asyncio
import ssl
import subprocess
import urllib.request
from pathlib import Path

import pytest

from otto.monitor.collector import MetricCollector
from otto.monitor.server import MonitorServer

_STARTUP_TIMEOUT = 5.0  # bounds the hang test; a real failure raises well under this


def _make_cert(tmp_path: Path) -> tuple[Path, Path]:
    cert, key = tmp_path / "cert.pem", tmp_path / "key.pem"
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-sha256",
            "-days",
            "2",
            "-keyout",
            str(key),
            "-out",
            str(cert),
            "-subj",
            "/CN=127.0.0.1",
            "-addext",
            "subjectAltName=IP:127.0.0.1",
        ],
        check=True,
        capture_output=True,
    )
    return cert, key


def _collector() -> MetricCollector:
    return MetricCollector(hosts=[], parsers=[])


class TestTlsServer:
    def test_origin_and_url_use_https(self, tmp_path):
        cert, key = _make_cert(tmp_path)
        server = MonitorServer(
            _collector(), host="127.0.0.1", port=7777, tls_cert=cert, tls_key=key
        )
        assert server.origin == "https://127.0.0.1:7777"
        assert server.url.startswith("https://")

    def test_plain_server_stays_http(self):
        server = MonitorServer(_collector(), host="127.0.0.1", port=7777)
        assert server.origin.startswith("http://")

    @pytest.mark.asyncio
    async def test_serves_real_tls_with_secure_cookie(self, tmp_path):
        cert, key = _make_cert(tmp_path)
        server = MonitorServer(_collector(), host="127.0.0.1", port=0, tls_cert=cert, tls_key=key)
        task = asyncio.create_task(server.serve())
        while not server.started:  # noqa: ASYNC110 — polling external uvicorn state; no event source available
            await asyncio.sleep(0.05)
        try:
            ctx = ssl.create_default_context(cafile=str(cert))

            def _fetch():
                req = urllib.request.urlopen(  # https URL under test
                    f"{server.origin}/api/mode?key={server.key}", context=ctx, timeout=10
                )
                return req.status, req.headers.get("set-cookie", "")

            status, set_cookie = await asyncio.to_thread(_fetch)
            assert status == 200
            assert "Secure" in set_cookie
        finally:
            server.force_stop()
            await asyncio.gather(task, return_exceptions=True)

    @pytest.mark.asyncio
    async def test_serve_raises_instead_of_hanging_on_bad_cert(self, tmp_path):
        """A garbage PEM must fail loud, not hang the ``while not server.started``

        startup poll forever. ``uvicorn.Server.serve()`` dies inside its own
        background task (``Config.load()`` -> ``ssl.SSLContext.load_cert_chain``
        raises ``ssl.SSLError``) — the poll loop never observes that unless it
        checks the task itself. Bounded with ``asyncio.wait_for`` so a
        regression here times out loudly rather than wedging the test run.
        """
        cert = tmp_path / "cert.pem"
        cert.write_text("this is not a certificate")
        server = MonitorServer(_collector(), host="127.0.0.1", port=0, tls_cert=cert, tls_key=cert)
        with pytest.raises(ssl.SSLError):
            await asyncio.wait_for(server.serve(), timeout=_STARTUP_TIMEOUT)
