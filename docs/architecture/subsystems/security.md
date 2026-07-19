# Security — the dashboard's trust boundary

`otto monitor` binds `0.0.0.0` on purpose — LAN viewing is the point, not an
accident to patch over. That choice sets the threat model: defend against
**drive-by access on a trusted lab network** — a port scan, a wandering
browser, an unrelated script that happens to hit the port. It does **not**
defend against a hostile actor already inside that trust boundary, nor
(without TLS) an on-path sniffer. There are no accounts and no login UI —
both would imply a stronger boundary than a lab network actually has, and
would add state (user databases, sessions) for a threat this design doesn't
claim to stop.

## Access key

Every run gets its own credential: `MonitorServer.__init__` generates
`secrets.token_urlsafe(16)` (~128 bits) once per process and never accepts
one from a flag — there is no `--key`/`--no-key` to pin or disable it. The
console URLs `otto monitor` prints fold the token in as `?key=…`, so the
line a user copy-pastes is already self-authenticating; nothing extra to
type or configure.

**One pure-ASGI middleware gates every route** — page, static assets, every
`/api/*` call, and the `/api/stream` SSE connection — ahead of routing, not
sprinkled through each handler. It is deliberately not
`BaseHTTPMiddleware`: that wrapper buffers/replays the response, which
fights streaming and disconnect detection, and the SSE stream needs to be
gated by the exact same code path as everything else rather than a special
case.

**Why token → cookie, not key-on-every-request.** `EventSource` (which
drives the live SSE feed) cannot set custom headers, and threading `?key=`
onto every fetch call would touch every frontend call site for no benefit.
Instead, a request that arrives with a valid `?key=` gets a cookie minted
on its response; every later request — including `EventSource` and every
`/api/*` fetch — carries the cookie automatically, same-origin, no code
change. The frontend stays entirely auth-unaware: zero diff under `web/`.

The cookie is named `otto_monitor_<port>` rather than a fixed name, because
cookies are scoped by host but not by port — two `otto monitor` processes
running on the same machine at different ports would otherwise clobber each
other's cookie. It is `HttpOnly` (the SPA never needs to read it) and
`SameSite=Lax`, which gives the mutating endpoints (`POST`/`PATCH`/`DELETE
/api/session/{id}/event[/{id}]`, `POST /api/session/{id}/event/{id}/end`,
and whatever the roadmap adds) CSRF coverage for free — a cross-site
request simply doesn't carry the cookie.

An unkeyed, uncookied request gets a 403 shaped for its consumer rather
than one generic error page: `/api/*` gets a small JSON body, because the
dashboard's boot sequence already treats any non-200 from those endpoints
as "nothing here" (its soft-fail contract for a bare static-file deployment
with no backend at all — see [Web dashboard](../../guide/monitor.md#web-dashboard)),
so an unkeyed API probe degrades exactly the same way. Everything else gets
a plain-HTML hint page pointing back at the full keyed URL.

The key never appears in per-request logs. Uvicorn's access logger would
otherwise print the full request line — query string included — on every
hit, which would put the credential in the log on request one; a filter
strips the query string from that logger's records for exactly this
reason.

## TLS

TLS is optional and config-driven — a `[monitor]` table in
`.otto/settings.toml` — never a CLI flag, matching every other
per-repo/team-level decision in this file rather than a per-invocation one.
Once configured, it is **mandatory** for that run: a cert or key path that
doesn't resolve is a hard exit, not a fall back to plain HTTP. A security
feature that silently downgrades on failure is worse than not having it,
because it fails exactly when someone is relying on it.

### Certificate scoping

TLS needs three artifacts, and the design keeps each at the scope it
actually belongs to rather than collapsing them into one:

| Artifact | Scope | Why |
| --- | --- | --- |
| CA certificate + key | Team-wide, created once | A viewer trusts the CA once; every future leaf cert issued under it is covered with no re-distribution. |
| Server (leaf) cert + key | Per-machine | SANs bind a leaf cert to one machine's addresses; a repo is cloned onto many machines with different IPs, so no single leaf cert could cover them all. |
| `[monitor]` settings entry | Per-repo, committed | Shared team-wide, so it points at a conventional per-user path (`~/.config/otto/tls/…`) rather than a machine-specific one — identical text resolves differently per user, and the key itself never enters the repo. |

The alternative of per-user self-signed certificates (no CA) was rejected:
every viewer would hit a browser trust interstitial on every origin, the
warning never goes away because the monitor's port is ephemeral, and adding
one new monitor machine means re-training every viewer to click through
another warning. A shared CA turns that into a one-time trust decision per
viewer, forever.

See [Securing the dashboard](../../guide/monitor.md#securing-the-dashboard)
in the guide for the operational steps — creating the CA, installing viewer
trust, issuing and installing a leaf cert, and the `[monitor]` settings
syntax.

## Where the code lives

- {mod}`otto.monitor.server` — `_AccessKeyMiddleware` (the gate),
  `_cookie_name` (port-scoped naming), `_RedactAccessLogQueryString` (the
  access-log filter), and `MonitorServer`'s `ssl_certfile`/`ssl_keyfile`
  wiring into uvicorn
- {class}`~otto.models.settings.MonitorSettingsSpec` — the `[monitor]`
  boundary spec (`tls_cert`/`tls_key`, `~` expansion, the
  key-without-cert validation error)
- {mod}`otto.config.repo` — `MonitorSettings`, the runtime dataclass
  `MonitorSettingsSpec.to_runtime()` builds
- {mod}`otto.cli.monitor` — `_resolve_monitor_tls`, which resolves the
  declaration across every configured repo and turns a missing cert/key
  file or a multi-repo disagreement into a hard exit
