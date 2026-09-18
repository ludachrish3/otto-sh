# HTTPS from a backend

A reservation, creds or inventory backend usually talks to a scheduler, a
vault or a CMDB over HTTPS. Inside an organisation that server is signed by
an internal CA, and a plain `requests.post()` fails with an `SSLError`,
because `requests` trusts only its bundled Mozilla roots and never looks at
the certificate store the machine already has.

otto's rule is **one way to trust a certificate: install it in the operating
system** — the CA that lets a browser on the machine open the dashboard also
lets otto's NetBox client, `curl` and `git` reach the same servers, with no
otto-specific trust configuration anywhere (the reasoning is on the
[security page](../architecture/subsystems/security.md#client-side-trust)).
{mod}`otto.tls` gives your backend the same trust in one call.

## A requests session

```python
from typing_extensions import override

from otto.reservations import ReservationBackendBase
from otto.tls import os_trust_session


class MyTeamBackend(ReservationBackendBase):
    def __init__(self, *, url, repo_dir, username):
        super().__init__(url=url, repo_dir=repo_dir, username=username)
        self._http = os_trust_session()

    @override
    def backend_name(self):
        return "my-team"

    @override
    def fetch_reservations(self, username, start=None, end=None):
        response = self._http.get(f"{self.url}/api/bookings", params={"user": username})
        response.raise_for_status()
        ...
```

{func}`otto.tls.os_trust_session` returns a `requests.Session` whose adapter:

- verifies every `https://` connection against the operating system's
  certificate store, and nothing else — there is no `verify=` argument, no
  bundle path and no off switch; a client certificate passed as `cert=` is
  still presented, since only the trust anchors are pinned to the OS store —
  it is loaded into the session's single shared SSL context (urllib3 calls
  `load_cert_chain` on the supplied context) and is presented on every later
  connection of that session, to any host, so use a separate session per
  client identity;
- carries that same trust through an `HTTPS_PROXY`, which stock `requests`
  does not (its proxied pool falls back to urllib3's own defaults). This is
  the destination's certificate: an `https://` proxy's own certificate is
  still verified with urllib3's defaults, since one source of trust is a
  claim about the server you are talking to;
- ignores `REQUESTS_CA_BUNDLE` / `CURL_CA_BUNDLE`, which would otherwise
  union a second trust list into the connection;
- bounds every request that names no `timeout=` of its own at
  {data}`otto.tls.DEFAULT_TIMEOUT_SECONDS` (30 seconds), so a scheduler that
  has gone unreachable fails the run instead of holding it for the kernel's
  TCP connect timeout. Pass `timeout=` to the factory to change the default,
  or to an individual call to override it there.

A TLS failure surfaces as `requests.exceptions.SSLError`; wrap it in your
backend's error like any other scheduler failure.

## Any other client

{func}`otto.tls.os_trust_context` returns the bare `ssl.SSLContext` for a
client that takes one directly:

```python
import httpx
from otto.tls import DEFAULT_TIMEOUT_SECONDS, os_trust_context

client = httpx.Client(verify=os_trust_context(), timeout=DEFAULT_TIMEOUT_SECONDS)
```

## When the CA cannot be installed

See [the NetBox backend](../configuration/inventory.md#the-netbox-backend)
for the `SSL_CERT_FILE` / `SSL_CERT_DIR` escape hatch — it applies to every
`otto.tls` client, not only NetBox.
