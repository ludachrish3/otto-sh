"""Replay a NetBox through ``tests/unit/inventory/netbox_stub.py`` under a logical URL.

A snapshot's filename is a digest of the inventory's configuration, URL
included (:func:`otto.inventory.cache.snapshot_slug_material`). The stub binds
a kernel-chosen port, so a snapshot written against ``stub.base`` would be
named differently on every run. :func:`route_requests_to` lets the inventory
be configured with a fixed LOGICAL URL (:data:`LOGICAL_URL`, under the
reserved ``.invalid`` TLD, so nothing real can ever answer it) while its
requests reach the stub: the filename is then stable across runs and
checkouts.

:class:`RequestTrap` is the opposite instrument: it replaces the transport so
that every request is counted and refused, which is how a test proves a
snapshot was served without one.

Both patch :meth:`requests.Session.send`, the one method every request of the
NetBox backend passes through (it hands pynetbox a plain
:class:`requests.Session` from :func:`otto.tls.os_trust_session`).
"""

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

LOGICAL_URL = "http://netbox.invalid"
"""The URL the replayed inventory is configured with; never resolvable."""


@contextmanager
def route_requests_to(*, stub: Any, logical: str = LOGICAL_URL) -> Iterator[None]:
    """Send every request for *logical* to *stub* instead, for the ``with`` block.

    A request already addressed to ``stub.base`` passes unchanged. Any other
    URL is refused with an ``AssertionError``, never passed through: the stub
    writes ``stub.base`` into every object's ``url`` and every ``next`` link,
    so a silent pass-through would hide a request that escaped the routing.
    Environment proxies are dropped for every request that reaches the stub,
    rewritten or already addressed to ``stub.base``, since the stub is on the
    loopback.
    """
    import requests

    original = requests.Session.send
    prefix = logical.rstrip("/")

    def send(session: Any, request: Any, **kwargs: Any) -> Any:
        url = request.url or ""
        if url == prefix or url.startswith(f"{prefix}/"):
            request.url = stub.base + url[len(prefix) :]
        elif not url.startswith(f"{stub.base}/"):
            raise AssertionError(f"route_requests_to: refusing unrouted URL {url!r}")
        kwargs["proxies"] = {}
        return original(session, request, **kwargs)

    requests.Session.send = send  # ty: ignore[invalid-assignment]
    try:
        yield
    finally:
        requests.Session.send = original


class RequestTrap:
    """Replace :meth:`requests.Session.send` for one test: count each attempt, then refuse it.

    ``attempts`` is how many requests were tried and ``urls`` their URLs, in
    order. Each attempt raises :class:`requests.ConnectionError`, so the code
    under test sees exactly what an unreachable server would give it.
    """

    def __init__(self, monkeypatch: Any) -> None:
        import requests

        self.attempts = 0
        self.urls: list[str] = []

        def send(session: Any, request: Any, **kwargs: Any) -> Any:
            self.attempts += 1
            self.urls.append(str(request.url))
            raise requests.ConnectionError(f"RequestTrap: refused {request.url}")

        monkeypatch.setattr(requests.Session, "send", send)
