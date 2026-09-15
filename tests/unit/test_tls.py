"""``otto.tls`` — the OS trust store and the request-timeout bound, for any HTTPS client.

The adapter under test used to be private to the NetBox backend; these are its
tests, re-pointed at the public factory (spec 2026-09-14 os-trust-http-helper
§7). The TLS stub is the NetBox one because it already speaks HTTPS with a
self-signed certificate — nothing here reads a NetBox response, only whether
the handshake was accepted.
"""

import subprocess
import sys
from types import SimpleNamespace

import pytest

from otto.tls import DEFAULT_TIMEOUT_SECONDS, os_trust_context, os_trust_session

# Absolute, like tests/unit/test_tier_marker_invariants.py's consumers: tests/unit
# has no __init__.py, so a relative import from a top-level unit module does
# not resolve.
from tests.unit.inventory.netbox_stub import NetBoxStub, device, self_signed_cert


def test_the_public_names_are_exactly_the_three_documented_ones():
    import otto.tls

    assert otto.tls.__all__ == ["DEFAULT_TIMEOUT_SECONDS", "os_trust_context", "os_trust_session"]


def test_the_default_timeout_is_thirty_seconds():
    """Generous, because the bound exists for an unreachable host, not for speed."""
    assert DEFAULT_TIMEOUT_SECONDS == 30.0


def test_os_trust_context_is_a_truststore_client_context():
    import ssl

    import truststore

    ctx = os_trust_context()
    assert isinstance(ctx, truststore.SSLContext)
    assert ctx.protocol == ssl.PROTOCOL_TLS_CLIENT
    assert os_trust_context() is not ctx, "a fresh context per call"


def test_the_session_mounts_one_adapter_on_both_schemes_with_the_os_store():
    """Spec §3: the truststore context decides; requests must not pour certifi into it.

    Stock ``HTTPAdapter.cert_verify`` with ``verify=True`` assigns certifi's
    bundle to ``conn.ca_certs``, which urllib3 then loads INTO the supplied
    context — a silent union of OS store + certifi that hides a missing root.
    The override leaves ``ca_certs`` / ``ca_cert_dir`` alone and only requires
    a certificate. Plain ``http`` keeps the stock behaviour (nothing to verify).
    """
    import truststore

    with os_trust_session() as session:
        adapter = session.get_adapter("https://scheduler.example/")
        assert session.get_adapter("http://scheduler.example/") is adapter
        assert isinstance(
            adapter.poolmanager.connection_pool_kw["ssl_context"], truststore.SSLContext
        )

        # A proxied connection gets its own pool manager (HTTPAdapter.proxy_manager_for
        # does not inherit init_poolmanager's connection_pool_kw) — it must carry the
        # SAME OS-store context, not fall back to urllib3's own default.
        proxy_manager = adapter.proxy_manager_for("http://proxy.example:3128")
        assert (
            proxy_manager.connection_pool_kw["ssl_context"]
            is adapter.poolmanager.connection_pool_kw["ssl_context"]
        )

        conn = SimpleNamespace(cert_reqs=None, ca_certs=None, ca_cert_dir=None)
        adapter.cert_verify(conn, "https://scheduler.example/api/", verify=True, cert=None)
        assert conn.cert_reqs == "CERT_REQUIRED"
        assert conn.ca_certs is None
        assert conn.ca_cert_dir is None

        plain = SimpleNamespace(cert_reqs=None, ca_certs=None, ca_cert_dir=None)
        adapter.cert_verify(plain, "http://scheduler.example/api/", verify=True, cert=None)
        assert plain.cert_reqs == "CERT_NONE"


def test_cert_verify_still_presents_a_callers_client_certificate(tmp_path):
    """Spec: mTLS is out of scope, but nothing here precludes it — the OS-store
    override must not silently drop a client cert a caller supplies."""
    certfile, keyfile = self_signed_cert(tmp_path)
    with os_trust_session() as session:
        adapter = session.get_adapter("https://scheduler.example/")

        conn = SimpleNamespace(
            cert_reqs=None, ca_certs=None, ca_cert_dir=None, cert_file=None, key_file=None
        )
        adapter.cert_verify(
            conn,
            "https://scheduler.example/api/",
            verify=True,
            cert=(str(certfile), str(keyfile)),
        )
        assert conn.cert_file == str(certfile)
        assert conn.key_file == str(keyfile)
        assert conn.cert_reqs == "CERT_REQUIRED"
        assert conn.ca_certs is None
        assert conn.ca_cert_dir is None

        missing = SimpleNamespace(
            cert_reqs=None, ca_certs=None, ca_cert_dir=None, cert_file=None, key_file=None
        )
        with pytest.raises(OSError, match="Could not find the TLS certificate file"):
            adapter.cert_verify(
                missing,
                "https://scheduler.example/api/",
                verify=True,
                cert=str(tmp_path / "does-not-exist.pem"),
            )


def test_a_request_with_no_timeout_carries_the_session_default(monkeypatch):
    """requests' ``Session.send`` passes ``timeout=None`` EXPLICITLY when the caller
    named none, so the bound must OVERWRITE a ``None`` — a ``setdefault`` never fires."""
    from requests.adapters import HTTPAdapter

    seen: list = []
    real_send = HTTPAdapter.send

    def spy(self, request, **kw):
        seen.append(kw.get("timeout"))
        return real_send(self, request, **kw)

    monkeypatch.setattr(HTTPAdapter, "send", spy)
    with NetBoxStub([device(1, "d1")]) as stub, os_trust_session(timeout=0.2) as session:
        session.get(stub.base + "/api/dcim/devices/", timeout=7.5)
        session.get(stub.base + "/api/dcim/devices/")
    assert seen == [7.5, 0.2]


def test_a_session_with_no_timeout_argument_uses_the_default(monkeypatch):
    from requests.adapters import HTTPAdapter

    seen: list = []
    real_send = HTTPAdapter.send

    def spy(self, request, **kw):
        seen.append(kw.get("timeout"))
        return real_send(self, request, **kw)

    monkeypatch.setattr(HTTPAdapter, "send", spy)
    with NetBoxStub([device(1, "d1")]) as stub, os_trust_session() as session:
        session.get(stub.base + "/api/dcim/devices/")
    assert seen == [DEFAULT_TIMEOUT_SECONDS]


@pytest.mark.parametrize("bad", [0, -1, "30", None, True])
def test_a_timeout_that_is_not_a_positive_number_is_refused(bad):
    """``True`` is in here on purpose: ``bool`` is an ``int``, and ``timeout = true``
    in a TOML table would otherwise quietly mean "one second"."""
    with pytest.raises(ValueError, match="timeout must be a positive number of seconds"):
        os_trust_session(timeout=bad)


def test_a_certificate_the_os_store_does_not_know_is_refused(tmp_path, monkeypatch):
    import requests

    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    monkeypatch.delenv("SSL_CERT_DIR", raising=False)
    # pyproject's filterwarnings = error: an unclosed Session is a ResourceWarning
    # and therefore a red, so every session here is a context manager.
    with (
        NetBoxStub([device(1, "d1")], tls=self_signed_cert(tmp_path)) as stub,
        os_trust_session(timeout=5) as session,
        pytest.raises(requests.exceptions.SSLError),
    ):
        session.get(stub.base + "/api/dcim/devices/")


@pytest.mark.skipif(
    sys.platform != "linux",
    reason="SSL_CERT_FILE is an OpenSSL override; truststore reads the platform store"
    " on macOS/Windows",
)
def test_a_certificate_the_os_store_trusts_is_accepted(tmp_path, monkeypatch):
    """The guard that the OS-store path is REAL: with certifi in play this fails."""
    certfile, keyfile = self_signed_cert(tmp_path)
    monkeypatch.setenv("SSL_CERT_FILE", str(certfile))
    monkeypatch.delenv("SSL_CERT_DIR", raising=False)
    with (
        NetBoxStub([device(1, "d1")], tls=(certfile, keyfile)) as stub,
        os_trust_session(timeout=5) as session,
    ):
        response = session.get(stub.base + "/api/dcim/devices/")
    # Any HTTP status will do: the handshake is what the OS store decided.
    assert response.status_code > 0


def test_a_requests_ca_bundle_from_the_environment_is_not_a_second_trust_source(
    tmp_path, monkeypatch
):
    # requests' Session.merge_environment_settings turns verify=True into the
    # REQUESTS_CA_BUNDLE / CURL_CA_BUNDLE path when trust_env is on (the
    # default), and urllib3 would load that file INTO the shared truststore
    # context — a silent union with a second trust list. The send override
    # pins verify=True for https so the OS store stays the only source.
    import requests

    certfile, keyfile = self_signed_cert(tmp_path)
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    monkeypatch.delenv("SSL_CERT_DIR", raising=False)
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", str(certfile))
    with (
        NetBoxStub([device(1, "d1")], tls=(certfile, keyfile)) as stub,
        os_trust_session(timeout=5) as session,
        pytest.raises(requests.exceptions.SSLError),
    ):
        session.get(stub.base + "/api/dcim/devices/")


def test_importing_the_module_does_not_import_an_http_stack():
    """``import otto.tls`` must cost ``ssl`` and nothing else (spec §3): the
    import-budget guard measures verbs that never open a connection.

    ``otto`` and ``ssl`` are imported FIRST, before the baseline snapshot, so
    neither the otto package's own closure (``otto/__init__.py`` attaches a
    ``NullHandler``, which pulls in ``logging`` / ``threading`` / etc. — not
    ``tls.py``'s business) nor ``ssl``'s own stdlib closure (which differs
    across 3.10-3.14: ``typing.io`` / ``typing.re`` are gone in 3.13,
    ``sre_compile`` / ``sre_parse`` / ``sre_constants`` move under ``re._*``
    in 3.11) counts against the module under test. What's left after
    ``import otto.tls`` must be exactly ``otto.tls`` itself — nothing else,
    on any interpreter. ``pathlib`` and its own closure (``fnmatch``,
    ``ipaddress``, ``ntpath``, ``urllib``) is the regression reviewers have
    actually caught here.
    """
    code = (
        "import sys, otto, ssl; baseline = set(sys.modules); import otto.tls; "
        "print(sorted(set(sys.modules) - baseline))"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    ).stdout.strip()
    assert out == "['otto.tls']", f"import otto.tls pulled in {out}"
