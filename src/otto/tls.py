"""The operating system's trust store for every HTTPS client otto or a backend opens.

One way to trust a certificate: install it in the operating system. This module
offers no bundle path, no ``verify=`` and no off switch (spec 2026-09-13
system-trust-store §2). Two shapes of the same trust:

- :func:`os_trust_context` — the bare :class:`ssl.SSLContext`, for clients
  that take one directly (``httpx``, ``aiohttp``, ``urllib``).
- :func:`os_trust_session` — a :class:`requests.Session` whose adapter
  carries that context AND a per-request timeout bound, for the common case
  of a backend calling ``requests.post`` against a scheduler.

``requests`` and ``truststore`` are imported inside the functions: a verb that
never opens a connection never pays for an HTTP stack, and the import-budget
guard measures exactly those verbs.

Why an adapter-local context and not ``truststore.inject_into_ssl()``: the
injection swaps :class:`ssl.SSLContext` process-wide and is client-only, and
``otto monitor`` serves TLS in this same process.
"""

import os
import ssl
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # the annotation only; requests stays a function-local import
    import requests

__all__ = ["DEFAULT_TIMEOUT_SECONDS", "os_trust_context", "os_trust_session"]

DEFAULT_TIMEOUT_SECONDS = 30.0
"""Seconds one HTTP request may take before it is given up on.

Generous rather than snappy: a filtered fetch over a large instance is a real
query, and the point of the bound is not speed. It is that an UNREACHABLE host
otherwise blocks a lab-bound command for the kernel's TCP connect timeout
(~2 minutes on Linux). requests' own default is no timeout at all.
"""


def _checked_timeout(timeout: float) -> float:
    """Return *timeout* as a positive float, refusing anything else.

    A plain ``ValueError``; a settings-driven caller wraps it naming the
    settings file and the backend.

    ``bool`` is refused explicitly because it is an ``int`` subclass:
    ``timeout = true`` in a TOML table would otherwise quietly mean one
    second.
    """
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
        raise ValueError(f"timeout must be a positive number of seconds, got {timeout!r}")
    return float(timeout)


def _apply_client_cert(conn: Any, cert: Any) -> None:
    """Mirror stock ``HTTPAdapter.cert_verify``'s client-certificate handling.

    The OS-store override skips the rest of that method (it never assigns
    ``ca_certs`` / ``ca_cert_dir``), but a caller's own client certificate —
    ``session.cert`` or a per-request ``cert=`` — is presented exactly as
    stock requests would present it: the pyproject floor is
    ``requests>=2.20.0``, and only requests>=2.32's ``_urllib3_request_context``
    keys ``cert_file`` / ``key_file`` into the pool on its own, so this
    override still assigns them and still checks the paths exist, mTLS being
    out of scope only for the server side of the handshake.
    """
    if not cert:
        return
    if isinstance(cert, str):
        conn.cert_file = cert
        conn.key_file = None
    else:
        conn.cert_file = cert[0]
        conn.key_file = cert[1]
    # os.path, not Path: pathlib's own import closure (fnmatch, ipaddress,
    # ntpath, urllib.parse) is exactly what a bare `import otto.tls` must not pay for.
    if conn.cert_file and not os.path.exists(conn.cert_file):  # noqa: PTH110
        raise OSError(f"Could not find the TLS certificate file, invalid path: {conn.cert_file}")
    if conn.key_file and not os.path.exists(conn.key_file):  # noqa: PTH110
        raise OSError(f"Could not find the TLS key file, invalid path: {conn.key_file}")


def os_trust_context() -> ssl.SSLContext:
    """Build a fresh TLS client context that verifies against the OS certificate store.

    The trust a browser on this machine already has: an internal CA installed
    once with ``update-ca-certificates`` (Linux), Keychain (macOS) or
    ``certutil -addstore Root`` (Windows) is trusted here too. On Linux,
    ``truststore`` reads OpenSSL's default verify paths, so ``SSL_CERT_FILE`` /
    ``SSL_CERT_DIR`` are the escape hatch for a CA that cannot be installed.
    """
    from truststore import SSLContext as _OsTrustContext

    return _OsTrustContext(ssl.PROTOCOL_TLS_CLIENT)


def os_trust_session(*, timeout: float = DEFAULT_TIMEOUT_SECONDS) -> "requests.Session":
    """Build a :class:`requests.Session` that trusts the OS store and bounds every request.

    Parameters
    ----------
    timeout : float
        Seconds one request may take when the caller names no ``timeout=``
        of its own, default :data:`DEFAULT_TIMEOUT_SECONDS`. Must be a
        positive number. A per-request ``timeout=`` still wins.

    One adapter is mounted on BOTH ``http://`` and ``https://`` so a redirect
    from one scheme to the other cannot escape the bound. The adapter:

    - hands the OS-store context to urllib3 for direct AND proxied pools
      (``HTTPAdapter.proxy_manager_for`` builds a separate pool manager that
      does not inherit ``init_poolmanager``'s keyword arguments, so a
      ``HTTPS_PROXY`` fetch would otherwise verify with urllib3's own
      default context). This covers the CONNECT hop's destination only; an
      ``https://`` proxy URL's own certificate is still wrapped with
      urllib3's default context, since "one source of trust" is a claim
      about the destination.
    - requires a certificate for ``https`` without assigning ``ca_certs`` /
      ``ca_cert_dir``, which is how stock requests would pour certifi INTO
      the context and hide a missing root behind a silent union; a client
      certificate the caller supplies is still presented as with stock
      requests, since only the trust anchors are pinned to the OS store —
      it is loaded into this session's single shared SSL context and is
      presented on every later connection of this session, to any host, so
      a caller needing more than one client identity needs one session per
      identity.
    - pins ``verify=True`` for every ``https`` request, so requests'
      ``trust_env`` rewrite of ``verify`` into ``REQUESTS_CA_BUNDLE`` /
      ``CURL_CA_BUNDLE`` never reaches urllib3 as a second trust source.
    - REPLACES a ``timeout`` that is already ``None`` rather than setting a
      default, because requests' ``Session.send`` passes ``timeout=None``
      EXPLICITLY when the caller named none — a ``setdefault`` would never
      fire, and the bound would silently not exist.

    """
    default = _checked_timeout(timeout)

    import requests
    from requests.adapters import HTTPAdapter
    from typing_extensions import override

    os_trust = os_trust_context()

    class _OsTrustAdapter(HTTPAdapter):
        @override
        def init_poolmanager(
            self,
            connections: int,
            maxsize: int,
            block: bool = False,
            **pool_kwargs: Any,
        ) -> None:
            pool_kwargs["ssl_context"] = os_trust
            super().init_poolmanager(connections, maxsize, block, **pool_kwargs)

        @override
        def proxy_manager_for(self, proxy: str, **proxy_kwargs: Any) -> Any:
            proxy_kwargs["ssl_context"] = os_trust
            return super().proxy_manager_for(proxy, **proxy_kwargs)

        @override
        def cert_verify(self, conn: Any, url: str, verify: "bool | str", cert: Any) -> None:
            if url.lower().startswith("https"):
                conn.cert_reqs = "CERT_REQUIRED"
                _apply_client_cert(conn, cert)
                return
            super().cert_verify(conn, url, verify, cert)

        # The full signature rather than ``**kwargs``: ``HTTPAdapter.send``
        # declares six parameters, and an override that narrowed them would
        # be a Liskov violation the type checker refuses.
        @override
        def send(
            self,
            request: Any,
            stream: bool = False,
            timeout: Any = None,
            verify: "bool | str" = True,
            cert: Any = None,
            proxies: Any = None,
        ) -> Any:
            if request.url is not None and request.url.lower().startswith("https"):
                verify = True
            return super().send(
                request,
                stream=stream,
                timeout=default if timeout is None else timeout,
                verify=verify,
                cert=cert,
                proxies=proxies,
            )

    session = requests.Session()
    adapter = _OsTrustAdapter()
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session
