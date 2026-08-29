"""A tiny NetBox REST stub for the real pynetbox client (spec §14: no mocking of pynetbox itself).

Serves paginated ``/api/dcim/devices/`` and nothing else — every other path
404s on purpose, so a pynetbox version that started calling ``/api/`` or
``/api/status/`` would fail loudly here rather than be quietly humoured.
Records every request it answered so a test can assert construction made ZERO
of them, and every query string so a test can assert the filter was forwarded.
One page holds ``page_size`` devices.

Binds ``127.0.0.1`` on port 0 (the kernel picks a free port) and serves from a
daemon thread the context manager shuts down — no real network, no port
collision between xdist workers, no thread left running past the test. Pass
``tls=(certfile, keyfile)`` (see :func:`self_signed_cert`) to serve HTTPS with
a certificate no system trust store knows, which is what gives ``verify=``
something real to be checked against.
"""

import datetime
import ipaddress
import json
import ssl
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

TOKEN = "stub-token"


def self_signed_cert(directory):
    """Write a self-signed cert/key for 127.0.0.1 into *directory*; return both paths.

    Self-signed and valid for the loopback IP, so it is simultaneously a
    server certificate no default trust store accepts (what ``verify=True``
    must reject) and a usable one-certificate CA bundle (what
    ``verify="<path>"`` must accept). EC rather than RSA purely for keygen
    speed — this runs per test.
    """
    directory = Path(directory)
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName([x509.IPAddress(ipaddress.IPv4Address("127.0.0.1"))]),
            critical=False,
        )
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    certfile = directory / "stub-cert.pem"
    keyfile = directory / "stub-key.pem"
    certfile.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    keyfile.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    return certfile, keyfile


def device(  # noqa: PLR0913 — a NetBox device payload has this many independently varied fields
    id_,
    name,
    *,
    ip="10.0.0.1/24",
    site="lab-a",
    rack="1",
    position=3.0,
    model="cx-4",
    platform="Ubuntu",
    custom_fields=None,
    oob_ip=None,
    status="active",
    tags=("lab",),
    drop=(),
):
    """One device payload in NetBox's own shape (dcim.devices serializer).

    *drop* removes keys from the payload, which is how an older or
    plugin-trimmed serializer is reproduced: NetBox does not promise every
    field this backend reads is present on every instance.
    """
    payload = {
        "id": id_,
        "name": name,
        "primary_ip4": None if ip is None else {"id": id_ * 10, "address": ip},
        "oob_ip": None if oob_ip is None else {"id": id_ * 10 + 1, "address": oob_ip},
        "site": None if site is None else {"id": 1, "name": site},
        "rack": None if rack is None else {"id": 2, "name": rack},
        "position": position,
        "device_type": {"id": 3, "model": model},
        "platform": None if platform is None else {"id": 4, "name": platform},
        "status": {"value": status, "label": status.title()},
        "serial": f"S{id_}",
        "asset_tag": f"A-{id_}",
        "tags": [{"id": i, "name": t} for i, t in enumerate(tags, start=1)],
        "custom_fields": dict(custom_fields or {}),
    }
    for key in drop:
        payload.pop(key, None)
    return payload


class NetBoxStub:
    """A local NetBox API a real pynetbox client can talk to.

    ``requests`` is every path+query the stub answered, in order (recorded
    before the token is checked, so a rejected call still shows up);
    ``queries`` is the parsed query string of each ``/api/dcim/devices/`` call.

    ``delay`` (seconds) makes every handler sleep before it answers: a NetBox
    that accepts the connection and then goes quiet, which is what a request
    timeout has to bound. Keep it small — the sleep is real.
    """

    def __init__(self, devices, *, page_size=2, token=TOKEN, tls=None, delay=0.0):
        self.devices = list(devices)
        self.page_size = page_size
        self.token = token
        self.delay = delay
        self.requests: list[str] = []
        self.queries: list[dict[str, list[str]]] = []
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):  # silence: the stub's log IS `stub.requests`
                pass

            def _send(self, code, body, headers=None):
                data = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("API-Version", "4.1")
                self.send_header("Content-Length", str(len(data)))
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(data)

            # do_GET: BaseHTTPRequestHandler dispatches on this exact name.
            def do_GET(self):
                stub.requests.append(self.path)
                if stub.delay:
                    # A NetBox that ACCEPTS the connection and then does not
                    # answer — the shape a request timeout is for. Slept after
                    # the request is recorded and before any byte of the
                    # response, so the client blocks on the read.
                    time.sleep(stub.delay)
                url = urlsplit(self.path)
                if self.headers.get("Authorization") != f"Token {stub.token}":
                    return self._send(403, {"detail": "Invalid token"})
                if url.path == "/api/dcim/devices/":
                    q = parse_qs(url.query)
                    stub.queries.append(q)
                    offset = int(q.get("offset", ["0"])[0])
                    limit = int(q.get("limit", [str(stub.page_size)])[0]) or stub.page_size
                    limit = min(limit, stub.page_size)
                    page = stub.devices[offset : offset + limit]
                    nxt = None
                    if offset + limit < len(stub.devices):
                        nxt = f"{stub.base}/api/dcim/devices/?limit={limit}&offset={offset + limit}"
                    return self._send(
                        200,
                        {
                            "count": len(stub.devices),
                            "next": nxt,
                            "previous": None,
                            "results": page,
                        },
                    )
                return self._send(404, {"detail": "Not found."})

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        scheme = "http"
        if tls is not None:
            certfile, keyfile = tls
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(certfile, keyfile)
            # Wrapping the LISTENING socket: the handshake then happens inside
            # accept(), and a client that rejects the certificate fails there
            # as an OSError subclass socketserver already swallows quietly.
            self._server.socket = context.wrap_socket(self._server.socket, server_side=True)
            scheme = "https"
        self.base = f"{scheme}://127.0.0.1:{self._server.server_address[1]}"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        try:
            self._server.shutdown()
            self._thread.join(timeout=5)
        finally:
            self._server.server_close()
