# Per-protocol cred scope

**Status:** approved by Chris 2026-09-13 (brainstorm in session; decisions recorded here).
Amended 2026-09-14 (approved in session): the host-level `user` pin is removed and the
pick rule is list order alone — see §2 "No host-level `user`" and §3.2.
**Extends:** `2026-07-04-login-proxy-and-app-shell-design.md` (the cred entry and the
via chain) and `2026-09-06-creds-store-and-three-file-scaffold-design.md` §6.1 (the
by-login merge). Both stay in force; this spec adds one field and refines what
"the default login" means.
**Consumed by:** the probe protocol survey (the spec that supersedes
`2026-08-20-host-probe-protocol-survey-design.md`), whose login tier picks a cred per
protocol through the function this spec introduces.

## The design, one sentence

A cred may name the protocols it is for; a protocol logs in as the first cred in
`creds` that applies to it — unscoped, or scoped to that protocol — so a handmade FTP
server with private accounts is one scoped entry placed ahead of the general ones in
the same list, and no flag and no separate pin decides whether host-wide creds "also
apply".

## Vocabulary

- **Protocol** — a registered term or transfer backend name (`ssh`, `telnet`, `ftp`,
  `scp`, …).
- **Self-authenticating protocol** — one that performs its own login with a cred:
  `ssh`, `telnet`, `ftp` today. Transfers that ride a term session (`scp`, `sftp`,
  `shell`, `nc`, `console`) inherit the carrier's identity and are not
  self-authenticating.
- **Scope** — a cred's `protocols` list. Empty means **unscoped**: the cred applies to
  every protocol. Non-empty means the cred applies to those protocols only.
- **Applies to P** — a cred applies to P when it is unscoped or P is in its scope.
  The creds that apply to P are P's **candidates**.
- **Default login for P** — the first candidate for P in list order (§3). It
  replaces the single "default login" of today, and the term's default login is the
  session identity.
- **Identity** — a cred's `login` plus its scope (sorted). Two entries with the same
  login and different scopes are two creds.

## 1. Problem

Every cred in a host's `creds` list applies to every protocol, and one pick
(the host-level `user`, else the first entry) authenticates ssh, telnet and ftp alike. That is right
for a unix box whose FTP daemon consults PAM. It is wrong for the FTP servers Chris
meets on real devices: small handmade daemons with their own account table, where the
unix `admin` is refused and the FTP `admin` (or `ftpuser`) has a password the unix
side has never heard of.

The obvious fix — a per-protocol cred list plus a boolean saying whether the host-wide
creds still apply — adds two knobs a reader must cross-check to know what FTP will
try. The brainstorm looked for the shape that composes without the boolean.

## 2. Decisions locked during brainstorm

- **Scope lives on the cred, not on the protocol options.** One optional field on
  `CredSpec`; the pool stays one ordered list; the three-layer merge and the creds
  store keep their shape.
- **List order decides; scope only filters.** A protocol's login is the first entry
  that applies to it. Unscoped creds remain candidates for every protocol, so an
  unscoped `admin` is still usable over ftp by name. Chris confirmed there is no
  case where host-wide creds must be *unusable* over a protocol, only cases where
  they must not be the *default* — so "additive versus exclusive" dissolves and no
  flag exists.
- **No host-level `user`.** (Amendment, 2026-09-14.) The `user` field predates the
  ordered list: when `creds` was a `{login: password}` map it chose the default;
  once the list carried order it became a pin for a non-first entry, and nothing
  else read it — no inventory backend supplies it, no CLI flag sets it. Kept beside
  scope it needed a specificity tier to let a scoped cred win a protocol over the
  pin, and that tier let a store-supplied cred scoped to the host's term quietly
  replace the session identity the lab file named. With one ordered list and one
  rule there is nothing to reconcile: the layered merge (§5.2) sequences the lab
  file's entries first, so a lower layer can never move ahead of an entry the lab
  author wrote. A lab that wants a store-supplied cred as its default restates
  `{"login": "…"}` in its own list — `password` is optional on an entry, and the
  restated entry composes with the store's fields while taking the lab's position.
  A lab file that still sets `user` fails to load with a message saying to order
  `creds` instead (§5.1). The Docker container-user field of the same name (spec
  2026-08-31) is a different field on a different class and is untouched.
- **Scope the rest when you scope one.** Because order decides, a cred scoped to
  `ftp` placed after an unscoped entry is never the default for anything. The guide
  says so and shows the full shape — once one entry is scoped, the others are scoped
  too, so every protocol's default is stated rather than implied by position. This
  is guidance, not a load rule: mixed lists stay legal.
- **Scope values are restricted to self-authenticating protocols.** Scoping a cred to
  `scp` would be a statement with no effect; it is refused at load rather than
  documented as a no-op. A backend declares whether it authenticates (§2.1) so a
  custom FTP-like backend opts in at registration.
- **No ban on `proxy`/`via` for a scoped cred.** A telnet-only `root` reached by `su`
  from `admin` is a good scoped cred. Each protocol does with a proxied pick what it
  does today: terms replay the hops after transport auth; ftp authenticates as the
  chain's direct end and replays nothing.
- **Identity is login plus scope.** The same login with a different FTP password is
  real on cheap devices. The duplicate rule and the merge key on the pair.
- **Lists, never tuples**, for the new field on the runtime dataclass (Chris's
  standing API preference).
- **Two specs, creds first.** This spec is independent of the probe amendment and
  ships first, so the probe's login tier can consume `default_login` and name which
  cred it tried.

### 2.1 The `authenticates` flag

Term and transfer backend classes declare `authenticates: ClassVar[bool]`. It is
`True` on the built-in `ssh` and `telnet` term backends and on the `ftp` transfer;
`False` on every other built-in transfer. `register_term_backend` and
`register_transfer_backend` refuse a class whose flag is missing or not a `bool`, the
way `register_transfer_backend` refuses a missing `host_families` today. A helper,
`authenticating_protocols()`, returns the sorted union of registered names with the
flag set; it is the only place otto computes the valid scope vocabulary, and the
validator, the error message and the docs all read it.

## 3. The model and the pick rule

### 3.1 The field

`CredSpec` gains `protocols: list[str] = []` (pydantic default-factory). The runtime
`Cred` dataclass gains `protocols: list[str]` with an empty-list default, the same
shape `params` already uses on that frozen dataclass. `CredSpec.to_cred()` copies it.
Every existing lab file, inventory record and creds-store entry is unscoped and
behaves exactly as today.

### 3.2 The pick rule

`default_login(creds, protocol) -> str` in `otto.host.login_proxy` is the one
implementation: the login of the first cred in list order that applies to
*protocol*, and `""` when none does (today's loginless value — an empty list, or
every entry scoped elsewhere). There is no second tier and no pin. The term's
default login is the session identity; ftp's is what ftp authenticates as, ignoring
any via chain as it does today.

Order is therefore the author's whole statement. `[ftpuser [ftp], admin]` logs ssh
in as `admin` and ftp in as `ftpuser`; `[admin, ftpuser [ftp]]` logs ftp in as
`admin` and never picks `ftpuser` by default. A `--term` override that selects a term
no entry applies to gets the loginless value, exactly as an empty list does, and the
transport fails at the far end naming the host.

A cred's display identity — `login` when unscoped, `login [p1, p2]` when scoped —
comes from `cred_identity(login, protocols)` beside it, and is the key every
duplicate check and the merge use, so an error names the same string the key was
built from.

### 3.3 Scope-aware lookup

`cred_for(creds, login, protocol=None)` prefers the cred scoped to *protocol* over the
unscoped cred with the same login, and never returns one scoped elsewhere; with no
protocol it returns the unscoped entry, else the first match by login — on a list with
no scoped entries that is exactly today's first-match behaviour, and it is used only
where no protocol is in play. `resolve_chain(creds, target,
protocol=None)` threads the protocol into every lookup it makes, including
`_default_direct`.

`via` names a login. It resolves to the cred with that login **and the same scope** as
the referencing cred, else to the **unscoped** cred with that login. A `via` that
resolves to nothing fails at load as it does now.

## 4. Runtime plumbing

- **`ConnectionManager`** gains `login_target_for(protocol) -> str` and
  `transport_cred_for(protocol) -> Cred | None` (the pick's directly-loginable end as
  a `Cred`, never a tuple; `None` when no cred applies, and callers substitute the
  module constant `LOGINLESS = Cred(login="", password="")`). The existing
  `login_target` and `credentials` properties become the pick for the active term
  (`self._term`), which keeps every session-identity consumer (`session.py`,
  `login()`, `switch_user`) unchanged. The manager and `TermContext` no longer carry
  a `user`; a custom term backend that read `ctx.user` reads
  `login_target_for(ctx.term)` instead.
- **Transport openers ask for their own protocol:** `ssh()` uses the ssh pick even when
  the term is telnet and ssh is opened only for an scp/sftp transfer; `telnet()` the
  telnet pick; `ftp()` the ftp pick — the line that makes the handmade-server case
  work. Per-user transports (`ssh_as`/`sftp_as`, via `_direct_cred_for`) and the hop
  tunnel builder in `remote_host.py` resolve against `ssh`, since a hop is always an
  ssh tunnel.
- **Lookups by login name the protocol they act for.** The sudo-password lookup and
  `UnixHost.cred()` pass the active term, because they answer for a shell identity.
- **Inert until scoped.** With no scoped creds every pick is the first entry and
  every transport authenticates exactly as a `user`-less lab did before.

## 5. Validation, merge, store

### 5.1 Load-time (`HostSpec._validate_cred_entries`, extended)

- Every `protocols` entry must be in `authenticating_protocols()`; the error lists
  the registered authenticating names (same shape as the unregistered-proxy error).
- Duplicate rule: same login **and** same scope. `admin` unscoped beside `admin`
  scoped to `ftp` is two creds.
- `via` and the chain-termination check run through the scope-aware lookup (§3.3).
- At least one cred must apply to the host's effective term when `creds` is
  non-empty, so the term never connects as nobody; the error lists what is there.
- A `user` key on a host entry is refused with a migration message — "`user` was
  removed: order `creds` instead; a protocol logs in as the first entry that
  applies to it" — the same shape as the `creds`-was-a-dict refusal, so an old lab
  file fails naming the fix rather than with a bare unknown-field error.

### 5.2 The merge keys on identity

`merge_creds` matches entries on login plus scope instead of login alone. `protocols`
is therefore part of the key, never a field a higher layer composes or overrides: a
store `admin`/`ftp` overlays only a lab `admin`/`ftp`. The duplicate-within-one-layer
error keys on the pair too. `CredsOverlay`, `_revalidate_merged_cred`, fingerprints,
stat paths and the JSON store format need no structural change — a store entry
carries `protocols` like any other field, and an absent one is unscoped.

## 6. Testing

Unit, each guard mutation-proven, hostile conditions injected:

- The pick rule as a table: first applicable entry wins, for a scoped entry ahead of
  and behind an unscoped one; a term nothing applies to yields `""`; no scoped creds
  reproduces the first-entry pick exactly.
- Scope-aware `cred_for`/`resolve_chain`: same-scope `via` wins over unscoped; a
  `via` existing only in a foreign scope fails loud; a proxied cred scoped to ftp
  resolves to its direct end.
- Every new `HostSpec` error by name: unregistered protocol; non-authenticating
  protocol (`scp`); duplicate on login plus scope; every entry scoped away from the
  term; a `user` key (the migration message).
- `merge_creds` keyed on identity: store `admin`/`ftp` beside lab `admin`/unscoped
  survive as two entries; a matching pair composes field by field, higher wins.
- `ConnectionManager` openers with scripted transports: `ftp()` authenticates as the
  ftp pick; `ssh()` under a telnet term uses the ssh pick; `ssh_as` and the hop
  builder resolve against ssh. Each asserts the login **and password** that reached
  the fake.
- Registration refuses a backend whose `authenticates` is missing or non-bool.

Repo-wide invariants the plan names in the gate (they escape a targeted run): the
public API snapshot (new function, new fields, new manager methods) and the
error-taxonomy test (new `ValueError`/`LoginProxyError` messages).

Live, on the bed (no bed host has private FTP accounts, so the pair is): the existing
FTP login scoped to `ftp` transfers a file as before; the same login scoped to `ftp`
with a wrong password fails with an error naming the scoped pick while an ssh transfer
on the same host still works. The second case proves scope is per protocol on a real
server.

## 7. Documentation (one home per topic; link, never restate)

- `docs/guide/configuration/host-sources.md` — the cred field table gains the
  `protocols` row; a short paragraph states the one pick rule where the first-entry
  and `user` rules used to be, with the scope-the-rest guidance and its example.
  The `user` row and every mention of the pin go; the lab-config field table drops
  its `user` row.
- `docs/guide/configuration/inventory.md` (§`credentials-layered`) — identity is
  login plus scope; scope never merges.
- `docs/library/creds-backends.md` — a store entry carries `protocols` unchanged.
- `docs/library/extending-backends.md` — the `authenticates` flag, documented next
  to `host_families`.
- The lab-config `creds` row and the CLI reference regenerate. No new page.

## 8. Out of scope

- Excluding host-wide creds from a protocol (no real case; §2).
- Scope on SNMP's `community` (not a cred entry) and on `tftp` (no auth).
- Any probe change — the survey spec consumes `default_login`; nothing here reports.
- Auto-scoping a cred from a probe verdict.
