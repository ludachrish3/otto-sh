# Monitor dashboard access key + optional TLS

**Status:** approved design, pre-implementation
**Date:** 2026-07-16

## Problem

`otto monitor` binds `0.0.0.0` on purpose (LAN viewing is the point) and serves
an unauthenticated HTTP API. Today that API already mutates state
(`POST/PATCH/DELETE /api/event`); the roadmap adds actions that alter lab state
(impairments, tunnels, test runs). Anyone who can reach the port owns the
dashboard.

**Threat model:** stop drive-by access on a trusted lab network — a port scan,
a wandering browser, an unrelated script. Explicitly *not* defended against: a
hostile actor inside the trusted network, or (without the TLS option) an
on-path sniffer. No accounts, no login UI.

## Decisions (settled during brainstorming)

- **Mechanism:** Jupyter-style token → cookie handoff. Not key-on-every-request
  (touches every frontend call site, sprays the key into logs), not
  bind-host-only (kills LAN viewing).
- **No CLI knobs:** the key is always freshly generated per run. No `--key`,
  no `--no-key`. Knobs can be added later if a need appears.
- **No localhost exemption:** one uniform rule; the printed URL carries the key.
- **TLS is optional, config-driven** via `[monitor]` in `.otto/settings.toml`.
  When configured, it is mandatory for that run — a broken TLS config is a
  hard error, never a silent fall-back to HTTP.

## Design — access key

### Token lifecycle

- `MonitorServer.__init__` generates `secrets.token_urlsafe(16)` (~128 bits)
  and stores it; a read-only `key` property exposes it for tests/harnesses.
- Both `mode="live"` and `mode="review"` get a key — no mode branching.
- `MonitorServer.url` / `.urls` append `?key=<token>`, so the console output
  users copy-paste is self-authenticating. The CLI output is otherwise
  unchanged.
- The key appears exactly once in output (the printed URL) and is never logged
  per-request.

### Enforcement — one middleware, everything gated

A single Starlette HTTP middleware registered on the FastAPI app, ahead of all
routes (`/`, `/static/*`, every `/api/*` including the SSE stream):

1. `?key=` present and equal (via `secrets.compare_digest`) → **allow**, and
   set the auth cookie on the response. Setting it on *any* keyed request, not
   just `/`, makes keyed deep links work.
2. Else auth cookie present and equal → **allow**.
3. Else → **403**:
   - `/api/*`: JSON `{"error": "missing or invalid access key"}`. The SPA's
     soft-fail boot contract (`bootstrap.ts`) already treats any non-200 as
     "nothing here", so an unkeyed API probe degrades exactly like an empty
     server.
   - everything else: a small plain-HTML page telling the user to open the
     full URL printed by `otto monitor` (including the `?key=` part).

### Cookie

- **Value:** the token itself.
- **Name:** `otto_monitor_<port>`, port taken from `request.url.port`. Cookies
  are not port-scoped, so the suffix is what keeps two concurrent monitor
  servers on one machine from clobbering each other (same trick Jupyter uses).
- **Flags:** `HttpOnly` (the SPA never reads it), `SameSite=Lax` (free CSRF
  coverage for the mutating endpoints — cross-site subresource requests don't
  carry the cookie), `Secure` **iff** TLS is active. No expiry beyond the
  browser session; the token dies with the process anyway.

### What does not change

- The `0.0.0.0` bind and ephemeral-port default.
- Anything under `web/` — `fetch()` and `EventSource` send same-origin cookies
  automatically. Zero frontend diff.
- The review/live endpoint contract, payload shapes, SSE framing.

## Design — optional TLS

### settings.toml surface

New typed sub-table in `SettingsModel` (extra='forbid' like its siblings):

```toml
[monitor]
tls_cert = "~/.config/otto/tls/monitor-cert.pem"
tls_key  = "~/.config/otto/tls/monitor-key.pem"   # optional if cert PEM bundles the key
```

- `MonitorSettingsSpec`: `tls_cert: Path | None = None`,
  `tls_key: Path | None = None`. Validator: `tls_key` set without `tls_cert`
  is an error.
- Paths get `Path.expanduser()` applied (settings expansion only handles
  `${sut_dir}`; `~` is what lets one committed value work for every team
  member — see the certificate model below). `${sut_dir}` also works but is
  discouraged: keys must never live in the repo.
- Plumbed like `docker_settings`: `Repo.parse_settings` →
  `monitor_settings` runtime value → CLI passes it to `MonitorServer`.
- **Multiple repos** (`OTTO_SUT_DIRS` lists several): if more than one repo
  declares `[monitor]` and the values disagree, fail loud at startup naming
  both repos. Identical or single declarations just apply.
- Applies to **both** live and review modes.

### Runtime behavior

- TLS configured → uvicorn gets `ssl_certfile`/`ssl_keyfile`, printed URLs
  become `https://`, cookie gains `Secure`. HTTPS only — no HTTP listener, no
  redirect.
- TLS configured but cert/key file missing or unreadable → `otto monitor`
  exits 1 with a message naming the path and the settings key. Never a silent
  HTTP fall-back (a security downgrade must not be quiet).
- TLS absent → today's plain-HTTP behavior, plus the access key.

### Certificate model — who creates what (the important part)

Three artifacts, three different scopes:

| Artifact | Scope | Lives where | Committed? |
|---|---|---|---|
| **CA certificate + CA key** | **Team-wide**, created once by a team owner | CA key: restricted (owner's machine or secrets store). CA cert: distributed freely | CA cert may be committed (it's public); CA key **never** |
| **Server (leaf) cert + key** | **Per-machine** — one per machine that runs `otto monitor`, because the SANs bind it to that machine's addresses | `~/.config/otto/tls/` on the server machine, key `chmod 600` | **Never** |
| **`[monitor]` settings entry** | **Per-repo**, committed, shared by the team | `.otto/settings.toml` | Yes — which is why it points at the conventional `~/.config/otto/tls/` path, identical for every user |

Why not the other scopes:

- *Per-repo cert:* a repo is cloned onto many machines with different IPs; one
  leaf cert cannot cover them all, and committing a private key is disqualifying
  on its own.
- *Per-user self-signed (no CA):* every viewer gets a browser interstitial per
  origin, and our port is ephemeral, so the warning returns on every run.
  Adding a new monitor machine means re-distributing trust to every viewer.
  With a CA, viewers trust once and every future leaf cert is covered.

### Certificate creation steps (to be reproduced in the docs)

**Step 1 — team owner creates the CA (once per team).** Keep
`otto-lab-ca.key` restricted; distribute `otto-lab-ca.crt` to everyone.

```sh
openssl req -x509 -newkey rsa:4096 -sha256 -days 1825 -nodes \
  -keyout otto-lab-ca.key -out otto-lab-ca.crt \
  -subj "/CN=Otto Lab CA"
```

**Step 2 — each viewer trusts the CA (once per viewing machine).**

- Linux: `sudo cp otto-lab-ca.crt /usr/local/share/ca-certificates/ && sudo update-ca-certificates`
- macOS: import into Keychain Access → System, set "Always Trust" (or
  `security add-trusted-cert -d -k /Library/Keychains/System.keychain otto-lab-ca.crt`)
- Windows: `certutil -addstore Root otto-lab-ca.crt`
- Firefox keeps its own store: Settings → Certificates → Import, or set
  `security.enterprise_roots.enabled`.

**Step 3 — each monitor machine gets a leaf cert (per machine, by its user).**
The SAN list must cover **every address the server prints** — i.e. every
non-loopback interface IP (`otto monitor` prints one URL per interface) plus
any DNS name teammates use.

```sh
openssl req -newkey rsa:2048 -sha256 -nodes \
  -keyout monitor-key.pem -out monitor.csr -subj "/CN=$(hostname)"

openssl x509 -req -in monitor.csr -sha256 -days 825 \
  -CA otto-lab-ca.crt -CAkey otto-lab-ca.key -CAcreateserial \
  -out monitor-cert.pem \
  -extfile <(printf 'subjectAltName=IP:10.10.200.5,IP:192.168.1.20,DNS:%s' "$(hostname)")
```

(825 days is the maximum validity Apple platforms accept; longer and Safari
rejects the cert outright.)

**Step 4 — install where settings.toml points.**

```sh
mkdir -p ~/.config/otto/tls
mv monitor-cert.pem monitor-key.pem ~/.config/otto/tls/
chmod 600 ~/.config/otto/tls/monitor-key.pem
```

A machine whose interface IPs change (DHCP without reservation) needs its leaf
cert regenerated with the new SANs — the error surfaces as a browser trust
warning naming the SAN mismatch. Static lab addressing avoids this.

## Testing

- **Unit (`tests/unit/monitor/`):** middleware matrix — no key → 403 on `/`
  and `/api/*` (both shapes asserted); wrong key → 403; good query key → 200 +
  `Set-Cookie`; cookie-only follow-up → 200; SSE endpoint gated; port-suffixed
  cookie name. Settings: `[monitor]` validation (key-without-cert error,
  unknown-key rejection via extra='forbid'), missing-cert-file hard exit,
  multi-repo disagreement error. A shared fixture/helper keys the TestClient
  so the diff to existing tests stays mechanical.
- **e2e dashboard:** `DashboardHarness` hands Playwright `server.url`, which
  now carries the key; the browser context keeps the cookie. One new e2e:
  navigating to the bare URL (key stripped) renders the 403 page. Run the full
  `nox -s dashboard` matrix, not bare pytest.
- **TLS runtime:** unit-level with a throwaway self-signed cert generated in
  `tmp_path` (openssl or `trustme`): server serves HTTPS, URLs say `https://`,
  cookie is `Secure`, missing file exits 1. No browser e2e for TLS (Playwright
  would need the CA installed; not worth the harness complexity now).
- Every new guard proven red against the pre-middleware code (house rule).

## Out of scope

- Accounts, sessions with expiry, login UI, role separation.
- Auto-generated self-signed certs (trains users to click through warnings).
- HTTP→HTTPS redirect listeners, Let's Encrypt/public-CA flows.
- CLI flags for key or TLS (`--key`, `--no-key`, `--tls-cert`); settings.toml
  is the only TLS surface, and the key is always random.
