# System trust store — one source of TLS trust for otto's HTTPS clients

**Date:** 2026-09-13
**Status:** Designed (this session); awaiting implementation plan
**Touches:** `2026-08-28-host-inventory-layer-design.md` §9 (the NetBox backend's
`verify` argument, removed here) and `2026-07-16-monitor-access-key-tls-design.md`
(docs reframed; no code change)

## 1. Goal

Make the operating system's certificate store the **only** source of TLS trust
for every HTTPS connection otto opens as a client, so that a lab on an
air-gapped or split-horizon network with an internal CA needs no otto-specific
TLS configuration at all: trust the CA once at the OS level, and NetBox, the
browser viewing `otto monitor`, `curl`, `git` and otto all agree.

Today `requests` (under `pynetbox`) trusts only its bundled `certifi` roots and
ignores the OS store. That is why the `[inventory]` table grew a `verify` key
with three shapes — `true` (certifi), a CA-bundle path, `false` — and why an
internal-CA NetBox forces every user to carry a path override in a committed
settings file. The analogue elsewhere is `uv --native-tls` and pip's default
since 24.2: verify against the OS store via the `truststore` library.

### In scope

- The NetBox backend verifies with the OS trust store, unconditionally (§3).
- The `[inventory]` `verify` key and the `NetBoxInventory(verify=)` argument
  are **removed** — a breaking change taken on purpose (§4).
- `truststore` becomes a direct runtime dependency (§5).
- Docs: the inventory guide's `verify` row is replaced; the monitor serving
  guide leads with an organisation CA rather than a do-it-yourself one; the
  security architecture page gains a client-trust paragraph (§7).

### Out of scope

- The monitor **server**: `[monitor] tls_cert` / `tls_key` stay exactly as they
  are. A server needs its own leaf certificate and key; a trust store holds
  only anchors. The only change on that side is documentation.
- Client certificates (mTLS) — no user has asked; nothing here precludes it.
- Any new settings key. This spec adds none and removes one.

## 2. Principle

**One way to trust a certificate: install it in the operating system.** otto
does not offer a second trust list, a per-backend bundle, or an off switch.
Every former override has a standard, non-otto replacement:

| Former `verify` value | Replacement |
| --- | --- |
| `true` | Unchanged meaning ("verify"), now against the OS store. |
| `"/abs/path/ca.pem"` | Install the CA in the OS store (`update-ca-certificates`, Keychain, `certutil -addstore Root`). On Linux without root, set `SSL_CERT_FILE` or `SSL_CERT_DIR` — the OpenSSL variables `truststore` honours there, and so do `curl`, `git` and OpenSSL itself; on macOS and Windows `truststore` reads the platform store and ignores them, so the CA must be installed there. requests' own `REQUESTS_CA_BUNDLE` / `CURL_CA_BUNDLE` are deliberately neutralised: they would union a second list into the context. |
| `false` | Trust the instance's certificate the same way (a self-signed leaf is its own CA bundle). Disabling verification from a committed settings file is gone. |

A union of OS store plus `certifi` is deliberately **not** done: it would hide
a missing root and diverge from what the browser on the same machine trusts.
Distribution bundles carry the Mozilla roots already.

## 3. Mechanism

All inside `otto/inventory/netbox.py`, on the `HTTPAdapter` subclass the
backend already mounts on pynetbox's session for the per-request timeout
bound (`_mount_timeout`):

1. `init_poolmanager` passes `ssl_context=truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)`
   to urllib3's pool manager. One context per adapter, built at mount time.
2. `cert_verify` sets `conn.cert_reqs = "CERT_REQUIRED"` for an `https` URL and
   assigns **no** `ca_certs` / `ca_cert_dir`, so urllib3 leaves the truststore
   context untouched rather than loading `certifi` into it. For `http` it
   defers to the stock implementation.
3. `verify` is no longer read or forwarded anywhere; `api.http_session.verify`
   is left at requests' default (`True`), which with the overrides above means
   "the context decides".

No process-wide `truststore.inject_into_ssl()`: that swaps `ssl.SSLContext`
for the whole process, and `otto monitor` builds a **server**-side context in
the same process. The adapter-local context is how pip integrates truststore
with requests for the same reason.

`truststore` is imported lazily beside `requests.adapters` inside
`_mount_timeout`, so no other verb pays for it and the import-budget guard's
surface is unchanged. On Linux, `truststore` uses OpenSSL's default verify
paths (honouring `SSL_CERT_FILE` / `SSL_CERT_DIR`) with a fallback list of
distribution bundle locations; on macOS the Security framework; on Windows
CryptoAPI.

## 4. The breaking change

Removed:

- `[inventory] verify` (settings key).
- `NetBoxInventory(..., verify=)` (constructor keyword) and the `.verify`
  attribute.
- `_checked_verify` and its "relative CA bundle path" error.

Hard cutover, decided 2026-09-13: no user has configured `verify` yet, so
no dead-key pointer is added. A stale key surfaces as the constructor's
`TypeError` wrapped in the usual inventory error naming the settings file.

Third-party inventory backends are unaffected; `verify` was never part of the
`Inventory` protocol or the registry's constructor contract.

The commit that lands the removal carries a `!` breaking marker and a
`BREAKING CHANGE:` footer so the generated changelog says so.

## 5. Dependency

`truststore>=0.10` joins `[project.dependencies]` in `pyproject.toml`, with the
usual comment naming the importing module. It is pure Python with no
dependencies of its own and is already in `uv.lock` as a transitive of the
docs tooling, so the resolution does not move.

## 6. Errors

An untrusted server keeps surfacing exactly as today: pynetbox's or requests'
exception is wrapped into `InventoryError("netbox inventory <url>: SSLError:
…")`. No new wording in code; the inventory guide tells the reader what an
`SSLError` here means and what to do (§7).

## 7. Documentation

- `docs/guide/configuration/inventory.md`: the `verify` row leaves the table.
  A short paragraph after the table says otto verifies NetBox against the
  operating system's certificate store, as a browser on the same machine
  would; an `SSLError` in the inventory error means the OS does not trust the
  instance, and the fix is to install the CA (or, without root, `SSL_CERT_FILE`).
  Cross-link to the monitor guide's "each viewer trusts the CA" steps, which
  are the same steps.
- `docs/guide/cli/monitor/serving.md`: "Creating the certificates" is
  reordered. Step one becomes *obtain a leaf certificate for this machine
  from your organisation's CA* — viewers already trust it, and so does otto's
  NetBox client. The existing otto-lab CA recipe becomes the fallback for a
  team with no PKI. The artifact table, SAN guidance, `~/.otto/tls/` warning
  and Firefox caveat stay.
- `docs/architecture/subsystems/security.md`: under "TLS", a paragraph
  "Client-side trust" stating the principle in §2 and why there is no
  override, linking to the inventory guide.
- `docs/superpowers/specs/2026-08-28-host-inventory-layer-design.md` is left
  as written; this spec supersedes its `verify` clause and says so above.

## 8. Testing

Unit, in `tests/unit/inventory/`:

- **OS-store miss is refused.** The existing TLS stub with its self-signed
  certificate; a fetch raises `InventoryError` naming the URL. (Today's
  `test_verify_true_refuses_a_certificate_no_trust_store_knows`, kept.)
- **OS-store hit is accepted.** Same stub; `monkeypatch.setenv("SSL_CERT_FILE",
  certfile)` before the fetch (truststore reads OpenSSL's default paths at
  socket-wrap time, not at context construction), then the fetch returns the
  device. This is the guard that the OS-store path is real: with certifi still
  in play the same test fails. It replaces the `verify=False` and
  `verify=<path>` acceptance tests.
- **Adapter wiring.** The mounted adapter's pool manager carries a
  `truststore.SSLContext`, and `cert_verify` on an `https` connection leaves
  `ca_certs` / `ca_cert_dir` unset while requiring a certificate. This is the
  guard against a future change quietly loading certifi into the context.
- The two `_checked_verify` tests are deleted with the function.

Repo-wide invariant tests a targeted gate would miss, run in the final gate:
the import-budget guard (`truststore` is lazy, so it must stay green) and the
docs build with `-W`.

## 9. Acceptance

- A NetBox instance signed by a CA present in the OS store is fetched with an
  `[inventory]` table containing only `backend`, `url` and the usual
  non-TLS keys.
- The same instance with the CA absent from the store fails with an inventory
  error naming the URL; adding the CA (or `SSL_CERT_FILE`) fixes it with no
  settings change.
- `otto monitor` with `[monitor] tls_cert` / `tls_key` behaves exactly as
  before.
