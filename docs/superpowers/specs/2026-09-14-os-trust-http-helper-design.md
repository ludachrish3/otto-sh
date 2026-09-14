# `otto.tls` — the OS trust store for every HTTPS client, not only NetBox

**Date:** 2026-09-14
**Status:** Designed (this session); awaiting implementation plan
**Touches:** `2026-09-13-system-trust-store-design.md` §3 (the adapter it
describes moves out of the NetBox module; the principle and mechanism are
unchanged) and `2026-09-07-reservation-object-api-design.md` (a backend
talking HTTPS to a scheduler is this spec's first external consumer)

## 1. Goal

The system-trust-store spec made the operating system's certificate store the
only source of TLS trust for otto's HTTPS clients, but it wired that trust into
exactly one client: the `HTTPAdapter` the NetBox backend mounts on pynetbox's
session. A user writing a reservation, creds or inventory backend that calls
`requests.post()` against an internal scheduler gets requests' bundled
`certifi` roots and an `SSLError` for an internal CA, on every otto version.
The principle ("trust the CA once, at the OS level") is otto's; the
implementation that honours it is private to one module.

This spec lifts that adapter into a small public module, `otto.tls`, so a
backend author gets the same trust, the same proxy handling and the same
request-timeout bound with one call, and NetBox becomes the first consumer of
the shared code rather than the owner of a private copy.

### In scope

- A new public module `otto.tls` with three names (§3).
- The NetBox backend consumes it; its private adapter is deleted (§4).
- Docs: one new library page, one new API reference page, and one-sentence
  links from the three backend guides and the security page (§6).
- The adapter's unit tests move with the code (§7).

### Out of scope

- The monitor **server**. A server needs its own leaf certificate and key; a
  trust store holds only anchors. `[monitor] tls_cert` / `tls_key` are
  untouched. The store reaches the dashboard through the certificate an
  organisation CA issues it, which viewers and otto's clients then trust
  because that CA is installed at the OS level.
- Client certificates (mTLS), `truststore.inject_into_ssl()`, any new
  settings key, any support for otto releases before this one.
- Wiring HTTP into the reservation, creds or inventory protocols. otto never
  makes a backend's HTTP calls; this is an opt-in helper the backend imports.

## 2. Principle

Unchanged from the system-trust-store spec §2: **one way to trust a
certificate — install it in the operating system**. The helper offers no
`verify=` override, no CA-bundle argument and no off switch; the escape hatch
for a CA that cannot be installed is `SSL_CERT_FILE` / `SSL_CERT_DIR` on Linux,
as before.

One addition of the same kind for timeouts: **a session leaves the helper with
a bound.** requests' own default is no timeout at all, and the NetBox adapter
was written partly to close that hole for a client (pynetbox) that offers no
seam. The helper keeps the bound and names its default once.

## 3. The module

`src/otto/tls.py`, public as `otto.tls`. Three names, all in `__all__`:

```python
DEFAULT_TIMEOUT_SECONDS: float = 30.0

def os_trust_context() -> ssl.SSLContext: ...

def os_trust_session(*, timeout: float = DEFAULT_TIMEOUT_SECONDS) -> requests.Session: ...
```

- `DEFAULT_TIMEOUT_SECONDS` **moves** here from `otto.inventory.netbox`, with
  its rationale ("generous rather than snappy; the point is an unreachable
  host, not speed"). It is the one named home of the number; the NetBox
  module imports it back so its constructor default does not change.
- `os_trust_context()` returns `truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)`.
  A fresh context per call. It exists for clients other than requests
  (`httpx`, `aiohttp`, `urllib`), which take an `ssl.SSLContext` directly.
- `os_trust_session(*, timeout)` returns a new `requests.Session` with one
  adapter mounted on **both** `http://` and `https://` (a redirect from one
  scheme to the other must not escape the bound). The adapter is the one the
  system-trust-store spec §3 describes, moved verbatim:
  1. `init_poolmanager` and `proxy_manager_for` both pass the OS-store
     context, so a proxied fetch (requests honours `HTTPS_PROXY`) verifies
     the destination against the same store as a direct one. The proxy's own
     certificate on an `https://` proxy URL is still urllib3's default
     context: "one source of trust" is a claim about the destination.
  2. `cert_verify` requires a certificate for `https` and assigns no
     `ca_certs` / `ca_cert_dir`, so urllib3 never pours certifi into the
     shared context.
  3. `send` pins `verify=True` for every `https` request, so requests'
     `trust_env` rewrite of `verify` into `REQUESTS_CA_BUNDLE` /
     `CURL_CA_BUNDLE` never reaches urllib3 as a second trust source; and it
     applies `timeout` when the caller named none (a per-request `timeout=`
     still wins).
- The positive-number check on `timeout` (`_checked_timeout`, today in the
  NetBox module) moves with the constant and runs in `os_trust_session`; a
  `bool`, a non-number or a non-positive number raises `ValueError` with the
  existing wording. NetBox keeps calling it at construction so a bad
  `[inventory] timeout` still fails before any fetch and still names the
  settings file.

`requests`, `requests.adapters` and `truststore` are imported **inside** the
two functions. `import otto.tls` costs `ssl` and nothing else, so the
import-budget guard's surface is unchanged and a verb that never opens a
connection never pays for an HTTP stack.

The adapter class stays module-private (`_OsTrustAdapter`). Its long
docstring, the record of why each override exists, moves with it.

## 4. NetBox as the first consumer

`otto.inventory.netbox._mount_timeout` and its nested adapter are deleted.
Where the backend today calls `pynetbox.api(...)` and then mounts its adapter
on `api.http_session`, it now assigns `api.http_session =
os_trust_session(timeout=self.timeout)` — pynetbox documents that attribute
as the supported way to supply a session. The backend's own docstring keeps
one paragraph saying trust and timeout come from `otto.tls` and links there;
the mechanism prose leaves the module.

Observable behaviour is identical: same context, same pin, same bound, same
`InventoryError` wrapping on an untrusted server. The differential is the
existing NetBox trust tests, which keep passing against the shared session.

## 5. Errors

None new. `os_trust_session` raises `ValueError` for a bad timeout, as the
NetBox constructor does today. A TLS failure surfaces as the client library's
own exception (`requests.exceptions.SSLError`) for the backend author to wrap
in their backend's error, exactly as the reservation guide already asks of
any scheduler failure.

## 6. Documentation

One home for the topic, linked from wherever a reader meets an `https://`
URL:

- **New** `docs/library/https-clients.md`, in the "Extending otto" toctree
  after `creds-backends`: when a backend needs it (any HTTPS call to a
  scheduler, a ticket system, an inventory), the two functions with a
  six-line `os_trust_session` example, the timeout default and how to override
  it per request, the proxy caveat, and the `SSL_CERT_FILE` escape hatch. It
  states the principle in one sentence and links the security page for the
  rest rather than restating it.
- **New** `docs/api/tls.rst`, automodule of `otto.tls`, listed in the API
  index beside `inventory`.
- `docs/library/reservation-backends.md`, `creds-backends.md` and
  `inventory-backends.md`: one sentence each, at the first `https://` example,
  pointing at the new page.
- `docs/getting-started/reservations.md`, "A backend of your own": the
  paragraph ends on *replace the file read with your scheduler's API*; one
  sentence follows it saying that when that API is HTTPS, `otto.tls.os_trust_session()`
  gives a client that trusts the CA your organisation installed on the
  machine, linking the new page. This is the first place a backend author
  reads, before the library guide, so the convenience must be visible here
  and not only where the protocol is documented in full.
- `docs/architecture/subsystems/security.md` "Client-side trust": the
  sentence that names the NetBox adapter as the carrier now names `otto.tls`
  and links the library page.
- `docs/superpowers/specs/2026-09-13-system-trust-store-design.md` is left as
  written; this spec supersedes its §3 location and says so above.

## 7. Testing

**New** `tests/unit/test_tls.py`, receiving the adapter tests that live in
`tests/unit/inventory/test_netbox.py` today, re-pointed at `os_trust_session`
and unchanged in substance:

- The pool manager's context is a `truststore.SSLContext`, and `os_trust_context()`
  returns one.
- The adapter is mounted on both schemes.
- A request with no timeout carries `DEFAULT_TIMEOUT_SECONDS`; an explicit
  per-request timeout is not overridden; the default is thirty seconds.
- A bad timeout is refused with the existing `ValueError` wording
  (parametrised over `True`, a string, zero, a negative number).
- `cert_verify` on an `https` connection requires a certificate and sets no
  `ca_certs` / `ca_cert_dir`.
- `REQUESTS_CA_BUNDLE` in the environment is not a second trust source: with
  it set to a bundle that trusts the TLS stub, a fetch still fails until
  `SSL_CERT_FILE` names the same file.
- `proxy_manager_for` hands the OS-store context to the proxied pool.

Kept in `test_netbox.py`: the OS-store miss and hit tests against the TLS stub
(they exercise the backend's error wrapping, which stays there), the
settings-file wording on a refused timeout, and one new test that the
backend's `api.http_session` is the session `os_trust_session` returns.

Repo-wide invariants a targeted gate would miss, named so the task gate runs
them: the public API snapshot (`tests/unit/api_snapshot`, gains
`otto.tls:DEFAULT_TIMEOUT_SECONDS`, `otto.tls:os_trust_context`,
`otto.tls:os_trust_session`), the import-budget guard, and the docs build
with `-W`.

## 8. Acceptance

- A reservation backend that does `session = otto.tls.os_trust_session()` and
  `session.post(url, ...)` reaches a scheduler signed by a CA in the OS store
  with no otto settings and no `verify=` anywhere, and gets `SSLError` when
  the CA is absent.
- `otto.tls.os_trust_context()` plugs into an `httpx.Client(verify=...)`
  unchanged.
- The NetBox backend behaves exactly as before the change, including under
  `HTTPS_PROXY` and with `REQUESTS_CA_BUNDLE` set.
- `import otto.tls` adds no module beyond `ssl` to the import budget.
- Minor version; nothing removed from the public API.
