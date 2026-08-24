# Serving the dashboard

`otto monitor` binds `0.0.0.0` on purpose — LAN viewing is the point — so
every run is protected by a per-run access key, with optional TLS layered on
top when a lab needs it.

## Access key

Every `otto monitor` run generates a fresh access key and folds it into the
printed URLs as `?key=…`, so the console output you copy-paste is already
self-authenticating. Opening one of those keyed URLs once sets a browser
cookie for the rest of the run — every later request from that browser,
including the SSE stream and every `/api/*` call, rides the cookie and never
needs the query parameter again. Opening the bare address, with no key and
no cookie, renders a small 403 hint page pointing back at the full URL
`otto monitor` printed.

There are no flags to disable or pin the key — no `--key`, no `--no-key`.
The key is always freshly generated and always required.

## Enabling TLS

TLS is optional and config-driven, never a CLI flag. Add a `[monitor]`
table to `.otto/settings.toml`:

```toml
[monitor]
tls_cert = "~/.otto/tls/monitor-cert.pem"
tls_key  = "~/.otto/tls/monitor-key.pem"   # omit if the cert PEM bundles the key
```

`settings.toml` is committed and shared by the whole team, so `tls_cert` /
`tls_key` point at a conventional per-user path (`~` is expanded) rather
than a path that only exists on one machine — see [Who creates which
certificate](#who-creates-which-certificate) below for why the certificate
itself still lives per-machine, never in the repo.

TLS configured but broken — a missing or unreadable cert/key file — exits 1
naming the path and the settings key; it never falls back to plain HTTP
silently, since a security downgrade must not be quiet. With more than one
repo listed in `OTTO_SUT_DIRS`, disagreeing `[monitor]` tables across those
repos are a hard error naming both; identical or single declarations just
apply.

## Who creates which certificate

TLS needs three artifacts, and each one has a different owner and scope:

| Artifact | Scope | Lives where | Committed? |
| --- | --- | --- | --- |
| **CA certificate + CA key** | Team-wide, created once by a team owner | CA key: restricted (owner's machine or secrets store). CA cert: distributed freely | CA cert may be committed (it's public); CA key **never** |
| **Server (leaf) cert + key** | Per-machine — one per machine that runs `otto monitor`, because the SANs bind it to that machine's addresses | `~/.otto/tls/` on the server machine, key `chmod 600` | **Never** |
| **`[monitor]` settings entry** | Per-repo, committed, shared by the team | `.otto/settings.toml` | Yes — which is why it points at the conventional `~/.otto/tls/` path, identical for every user |

Why not the other scopes:

- **Per-repo cert.** A repo is cloned onto many machines with different
  IPs; one leaf cert cannot cover them all, and committing a private key is
  disqualifying on its own.
- **Per-user self-signed (no CA).** Every viewer gets a browser
  interstitial per origin, and the port is ephemeral, so the warning
  returns on every run. Adding a new monitor machine means re-distributing
  trust to every viewer. With a CA, viewers trust once and every future
  leaf cert is covered.

## Creating the certificates

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

**Step 3 — each monitor machine gets a leaf cert (per machine, by its
user).** The SAN list must cover every address the server prints — i.e.
every non-loopback interface IP (`otto monitor` prints one URL per
interface) plus any DNS name teammates use.

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
mkdir -p ~/.otto/tls
mv monitor-cert.pem monitor-key.pem ~/.otto/tls/
chmod 600 ~/.otto/tls/monitor-key.pem
```

```{warning}
`~/.otto/tls/` is the one thing under `~/.otto` that otto cannot rebuild.  The
workspace directories beside it hold derived state and are safe to delete (see
[The workspace home](../index.md#the-workspace-home)); your leaf key is not.
Deleting it means regenerating the cert, not a slower next run.
```

A machine whose interface IPs change (DHCP without reservation) needs its
leaf cert regenerated with the new SANs — the error surfaces as a browser
trust warning naming the SAN mismatch. Static lab addressing avoids this.

