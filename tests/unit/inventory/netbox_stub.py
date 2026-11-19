"""A tiny NetBox REST stub for the real pynetbox client (spec §14: no mocking of pynetbox itself).

Serves paginated ``/api/dcim/devices/`` and
``/api/virtualization/virtual-machines/`` and nothing else — every other path
404s on purpose, so a pynetbox version that started calling ``/api/`` or
``/api/status/`` would fail loudly here rather than be quietly humoured.
Records every request it answered so a test can assert construction made ZERO
of them (and that a device-only configuration made zero virtual machine
calls), and every query string so a test can assert each filter was forwarded
to its own endpoint. One page holds ``page_size`` objects.

Every object is served with the ``url`` a real NetBox gives it (its detail
page, ``<endpoint><id>/``), because pynetbox behaves differently with one: a
record that has a ``url`` answers an attribute the list response lacked by
GETting that page. The stub serves the detail page too (the whole object, as
a NetBox detail response is), and records the GET, so a test can assert both
that a full list response costs no detail request and that a trimmed one
costs exactly one per object.

The list endpoints trim their output as NetBox does, so a payload can lack a
field the way a real one does: ``fields=a,b`` keeps only those fields,
``omit=a,b`` drops those, and ``brief=true`` keeps only NetBox's brief set
(``id``, ``url``, ``display``, ``name``, ``description``). A trimmed field is
ABSENT from the payload, not null. Pagination's ``next`` link carries the
request's own query string, as NetBox's does.

Binds ``127.0.0.1`` on port 0 (the kernel picks a free port) and serves from a
daemon thread the context manager shuts down — no real network, no port
collision between xdist workers, no thread left running past the test. Pass
``tls=(certfile, keyfile)`` (see :func:`self_signed_cert`) to serve HTTPS with
a certificate no operating-system trust store knows, which is what gives the
client's OS-store verification something real to be checked against.
"""

import datetime as dt
import ipaddress
import json
import ssl
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

TOKEN = "stub-token"


def self_signed_cert(directory):
    """Write a self-signed cert/key for 127.0.0.1 into *directory*; return both paths.

    Self-signed and valid for the loopback IP, so it is simultaneously a
    server certificate no default trust store accepts (what the client must
    reject) and a usable one-certificate trust file (what ``SSL_CERT_FILE``
    pointing at it must accept). EC rather than RSA purely for keygen speed —
    this runs per test.
    """
    directory = Path(directory)
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
    now = dt.datetime.now(dt.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + dt.timedelta(days=1))
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


def virtual_machine(
    id_,
    name,
    *,
    ip="10.1.0.1/24",
    site="lab-a",
    cluster="vmc-1",
    platform="Ubuntu",
    custom_fields=None,
    status="active",
    tags=("lab",),
    drop=(),
):
    """One virtual machine payload in NetBox's own shape (virtualization.virtual_machines).

    No ``oob_ip``, ``rack``, ``position`` or ``device_type``: NetBox's virtual
    machine serializer has none of them. *drop* works as for :func:`device`.
    """
    payload = {
        "id": id_,
        "name": name,
        "primary_ip4": None if ip is None else {"id": id_ * 10, "address": ip},
        "site": None if site is None else {"id": 1, "name": site},
        "cluster": None if cluster is None else {"id": 5, "name": cluster},
        "platform": None if platform is None else {"id": 4, "name": platform},
        "status": {"value": status, "label": status.title()},
        "tags": [{"id": i, "name": t} for i, t in enumerate(tags, start=1)],
        "custom_fields": dict(custom_fields or {}),
    }
    for key in drop:
        payload.pop(key, None)
    return payload


_BRIEF_FIELDS = ("id", "url", "display", "name", "description")
"""What NetBox's brief serializer keeps of a device or a virtual machine."""

_DEVICES_PATH = "/api/dcim/devices/"
_VMS_PATH = "/api/virtualization/virtual-machines/"


class NetBoxStub:
    """A local NetBox API a real pynetbox client can talk to.

    ``requests`` is every path+query the stub answered, in order (recorded
    before the token is checked, so a rejected call still shows up);
    ``queries`` is the parsed query string of each ``/api/dcim/devices/`` call
    and ``vm_queries`` the same for each
    ``/api/virtualization/virtual-machines/`` call.

    ``delay`` (seconds) makes every handler sleep before it answers: a NetBox
    that accepts the connection and then goes quiet, which is what a request
    timeout has to bound. Keep it small — the sleep is real.

    ``forbidden`` is a set of endpoint paths the token may not read: each
    answers 403, as NetBox does for a token whose permissions stop short of
    that app (a dcim-only token asking the virtualization app).
    """

    def __init__(
        self,
        devices,
        *,
        virtual_machines=(),
        page_size=2,
        token=TOKEN,
        tls=None,
        delay=0.0,
        forbidden=frozenset(),
    ):
        self.devices = list(devices)
        self.forbidden = frozenset(forbidden)
        # Ids whose detail page 404s although the list serves them: a test
        # sets it to see what one failed detail load does.
        self.detail_missing: set[int] = set()
        # Detail path -> fields its page serves over the listed object's: a
        # test sets it to replay an object that changed between list and detail.
        self.detail_overrides: dict[str, dict] = {}
        # Detail path -> fields its page leaves out although the list serves
        # them: a detail serializer narrower than the list's.
        self.detail_drops: dict[str, set[str]] = {}
        self.virtual_machines = list(virtual_machines)
        self.page_size = page_size
        self.token = token
        self.delay = delay
        self.requests: list[str] = []
        self.queries: list[dict[str, list[str]]] = []
        self.vm_queries: list[dict[str, list[str]]] = []
        stub = self
        # path -> (the objects it serves, where its query strings are recorded)
        endpoints = {
            _DEVICES_PATH: (self.devices, self.queries),
            _VMS_PATH: (self.virtual_machines, self.vm_queries),
        }

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
                if any(url.path.startswith(path) for path in stub.forbidden):
                    return self._send(
                        403, {"detail": "You do not have permission to perform this action."}
                    )
                head, _, tail = url.path.rstrip("/").rpartition("/")
                if f"{head}/" in endpoints and tail.isdigit():
                    # A detail page: the whole object, never trimmed.
                    objects, _queries = endpoints[f"{head}/"]
                    for o in objects:
                        if o["id"] == int(tail) and o["id"] not in stub.detail_missing:
                            served = {"url": f"{stub.base}{url.path}", **o}
                            served.update(stub.detail_overrides.get(url.path, {}))
                            for key in stub.detail_drops.get(url.path, set()):
                                served.pop(key, None)
                            return self._send(200, served)
                    return self._send(404, {"detail": "No Device matches the given query."})
                if url.path in endpoints:
                    objects, queries = endpoints[url.path]
                    q = parse_qs(url.query)
                    queries.append(q)
                    offset = int(q.get("offset", ["0"])[0])
                    limit = int(q.get("limit", [str(stub.page_size)])[0]) or stub.page_size
                    limit = min(limit, stub.page_size)
                    page = [
                        {"url": f"{stub.base}{url.path}{o['id']}/", **o}
                        for o in objects[offset : offset + limit]
                    ]
                    if "fields" in q:
                        wanted = q["fields"][0].split(",")
                        page = [{k: v for k, v in o.items() if k in wanted} for o in page]
                    if "omit" in q:
                        dropped = q["omit"][0].split(",")
                        page = [{k: v for k, v in o.items() if k not in dropped} for o in page]
                    if q.get("brief", [""])[0].lower() in ("true", "1"):
                        page = [{k: v for k, v in o.items() if k in _BRIEF_FIELDS} for o in page]
                    nxt = None
                    if offset + limit < len(objects):
                        following = {**q, "limit": [str(limit)], "offset": [str(offset + limit)]}
                        nxt = f"{stub.base}{url.path}?{urlencode(following, doseq=True)}"
                    return self._send(
                        200,
                        {
                            "count": len(objects),
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
