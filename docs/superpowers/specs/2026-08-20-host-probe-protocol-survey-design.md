# Host Probe Protocol Survey — Design

**Date:** 2026-08-20
**Status:** Approved design, pre-implementation
**Surface:** the `otto host <id> probe` verb (`@cli_exposed`, today BusyBox
userland recon on posix-shell hosts). The root `--dry-run --probe` flag and its
connection-only contract (`docs/superpowers/specs/2026-08-15-dry-run-contract-design.md`
§3) are untouched by this work.

## 1. Problem

`otto host <id> probe` answers one question today: what userland does this
host have. Operators also need to know which **term** protocols (ssh, telnet,
console) and which **file-transfer** protocols (scp, sftp, ftp, nc, shell,
console, tftp) a host actually supports — the complete, verified list — both
to catch lab-data rot (a declared protocol that no longer works) and to
discover capability the lab record undersells (a working protocol nobody
declared). Port scans alone cannot answer this: scp/sftp/shell all sit behind
ssh's port, nc has no standing listener at all, and console rides the term
session — most transfer verdicts are only knowable from inside a logged-in
session.

## 2. Decisions locked during brainstorm

- **Surface:** the `otto host <id> probe` verb. It already costs a real
  connection and may run commands, so it can verify properly. `--dry-run
  --probe` stays narrow and untouched.
- **Candidates: all known protocols** — every registered term/transfer
  backend applicable to the host's family, declared or not. The probe is
  discovery, and reports drift in both directions.
- **Depth: tiered, authenticating where the verb already pays for it.** Port
  and banner checks are evidence; logins and in-session checks are the
  authoritative tiers. Chris's framing is normative: nc support is
  determined by logging in through a term method, checking for nc or ncat,
  and verifying its userland options — and other aspects likewise require a
  term login.
- **Vantage: route dial checks through the hop.** A host reached via a hop
  or login proxy gets its TCP/banner checks executed from the final hop over
  that hop's existing session, so the survey observes from where the real
  connect path observes.
- **Presentation:** every table in the probe output — the new protocol
  table, the drift table, and the existing userland table — renders as a
  Rich `Table` with rounded corners (`box.ROUNDED`). Pasteable payload
  blocks (the userland pin, the new menu pin) stay plain text: copy-paste
  integrity beats prettiness.
- **Live validation rides the BusyBox bed capability, which ships first** —
  five per-milestone-version QEMU guests behind test1 (lab element
  `carrot`) as hop, codified in the `Vagrantfile` with a
  `qemu-restart`-style recovery target. That
  capability (and the migration of the BusyBox artifact tier onto it) is
  its own spec, sequenced before this one:
  `docs/superpowers/specs/2026-08-20-busybox-bed-and-tier-migration-design.md`.

## 3. Verdict model

One `ProtocolVerdict` per candidate protocol:

- `protocol` (registered backend name), `kind` (`term` / `transfer`);
- `state` — one of:
  - **supported** — verified at an authoritative tier;
  - **login-failed** — dialed and the service matched, but authentication
    was refused (a definitive "not usable as configured");
  - **service-mismatch** — the port answered, but not with the expected
    service (the "wrong service squatting the port" false-positive guard);
  - **closed** — the dial was refused;
  - **timeout** — the check ran out of budget; never folded into closed;
  - **no-session** — the verdict needs a session this probe could not get
    (names the carrier it needed and the terms it tried);
  - **not-checkable** — no honest check exists (reason stated, e.g. tftp:
    UDP, no reliable client-side check);
- `tier` — which tier produced the verdict (login / session / userland /
  dial);
- `vantage` — where the observation was made from (`controller` or
  `hop:<host-id>`); session/userland verdicts carry the session's own path;
- `detail` — one line (banner text, binary found, subsystem reply, reason).

### Tiers, strongest first

- **Login tier (authoritative for terms and ftp).** A term is *supported*
  only when a real login succeeds over it — port 23 answering proves nothing
  about credentials, and opening includes authenticating (the dry-run probe
  module's own doctrine). The probe attempts a login over **each** candidate
  term, declared or discovered-open, using the host's configured creds;
  candidate terms are independent (one failure never aborts the others).
  ftp's transfer verdict is likewise a real control-channel login.
- **Session tier (authoritative for most transfers).** Over one established
  session (the resolved term first; else the first term that logged in):
  **nc** = nc/ncat binary present + flavor + the userland-options recon the
  verb already runs — never a port check, there is no standing listener;
  **scp** = server-side scp binary presence via the same userland recon;
  **sftp** = the subsystem actually opens over the live ssh connection
  (requires the ssh session specifically — with telnet-only access sftp
  reports *no-session* naming ssh as the missing carrier);
  **shell / console** = the carrying session itself working.
- **Dial tier (evidence, and negative verdicts).** TCP connect + banner
  classification — ssh `SSH-2.0-…`, ftp `220 …`, telnet IAC negotiation —
  against each protocol's **resolved** port from the host's own options
  (`ssh_options.port`, `telnet_options.port`, `ftp_options.port`; never
  literal well-known numbers). It produces *closed* / *service-mismatch* /
  *timeout* verdicts and discovery triggers (an open, service-matching port
  for an undeclared protocol promotes that protocol into the login/session
  tiers), but on its own upgrades nothing to *supported*.

Protocols outside the host's family (`host_families`) are not candidates and
take no table row; a single footnote line under the table names them
(`not applicable to this family: …`) so "all known protocols" stays
transparent without noise rows.

## 4. Survey flow and vantage

New module `src/otto/host/protocol_survey.py` owns the engine:

1. **Candidates** — registered term/transfer backends filtered by the host's
   family.
2. **Dial tier**, concurrent across candidates. Directly-routable hosts are
   dialed from the controller (`asyncio.open_connection` + bounded banner
   read). A host whose connect path rides a hop or login proxy has the dial
   executed on the final hop through that hop's existing session, via a
   BusyBox-aware fallback chain (`nc -z`, `/dev/tcp`, with the userland gap
   machinery deciding what the hop offers). Every dial verdict records its
   vantage.
3. **Login tier** — one login attempt per candidate term (and ftp), bounded
   by the family's existing connect timeout, failures isolated per protocol.
4. **Session tier** — over the first successful term session, run the
   userland recon (shared with the verb's existing userland section — one
   recon, two consumers) and the sftp subsystem check.

If no term logs in at all, every session-dependent candidate reports
**no-session** with the terms tried. Nothing is guessed.

## 5. Verb surface and report

`otto host <id> probe` output becomes, in order:

1. **Userland section** — content unchanged, table converted to Rich
   `box.ROUNDED`; its pasteable pin stays plain text.
2. **Protocol table** (Rich, `box.ROUNDED`): protocol / kind / state / tier /
   vantage / detail, one row per candidate, followed by the
   not-applicable footnote line.
3. **Drift table** (Rich, `box.ROUNDED`), only when non-empty:
   *declared-but-dead* rows (in `valid_terms`/`valid_transfers` but verified
   login-failed / service-mismatch / closed) and *working-but-undeclared*
   rows (verified supported but absent from the menus). `no-session` and
   `timeout` states are unknowns, not drift — they never appear here.
4. **Pasteable menu pin** (plain text): `valid_terms = [...]` /
   `valid_transfers = [...]` built from *supported* entries only — RECON
   ONCE, THEN PIN, exactly like the userland pin. Never auto-applied.

Exit semantics unchanged from the verb today: every answered outcome — dead
protocols included — is `Success`; dry-run short-circuits exactly as now
(skips with the dry-run report, contacts nothing). The CLI result renderer
currently prints `Result.value: list[str]`; it learns to accept Rich
renderables interleaved with strings (a general improvement any future
tabular verb inherits), and the probe verb returns the mixed sequence.

## 6. Host families

The verb extends from posix-shell hosts to every family:

- **Unix:** the full survey as above.
- **Embedded / Zephyr:** console term verdict = the console session opening;
  the telnet console port dial runs hop-side; the candidate set is the
  embedded family's (console transfer; unix-only protocols appear only in
  the footnote). No userland recon where the family has none — the existing
  no-resolver report line stays.
- **Local:** session-tier only (the machine otto runs on; no sockets dialed,
  states are honest — `not-checkable: no transport` where nothing applies).
- **Docker containers:** *not-checkable* rows with the stated reason (the
  family has no generic probe today — same honesty rule as the dry-run
  probe's NOT_PROBED).

### Prerequisite bed capability: the BusyBox guests

The five per-milestone-version BusyBox QEMU guests (behind test1/`carrot`
as hop, `term = telnet`, transfers `shell`/`nc`, ssh dead by construction) are
built by the bed spec that ships before this one —
`2026-08-20-busybox-bed-and-tier-migration-design.md`. This spec *consumes*
them: they are the live unix-behind-a-hop probe targets, and their
permanently dead, undeclared ssh gives the drift table a standing
true-negative.

## 7. Error handling and budgets

- Per-dial timeout: 2.0 s (module constant). Per-login: the family's
  existing connect timeout. Whole-survey budget: 90 s (module constant) so a
  wedged host cannot hang the verb; exhausting it marks the remaining
  checks *timeout*, runs nothing further, and still prints the report.
- A hop that cannot execute dial commands degrades those verdicts to
  *not-checkable* ("hop <id> offers no dial tool"), never to closed.
- No verdict is fabricated from an absence: timeouts say timeout, missing
  sessions say no-session, and the probe never retries a refused login
  (credential-lockout hygiene).

## 8. Testing

- **Unit (engine):** scripted per-tier fakes drive every state — including
  service-mismatch via a wrong-banner fake, login-failed via a
  credential-refusing fake, hop-vantage dial via a scripted hop session, and
  the discovery promotion (undeclared open port → login attempted). Every
  guard mutation-proven; hostile conditions injected, never inherited.
  Verdict tier/vantage labels asserted, not just states.
- **Unit (report):** table construction from a fixed verdict set — Rich
  tables with `box.ROUNDED` asserted structurally (not by screen-scraping
  ANSI), pin built from supported-only, drift excludes unknowns.
- **BusyBox — verified against the five live bed guests, in both roles.**
  The bed spec retires the contrived local harness, so BusyBox verification
  here targets real remote hosts (one guest per milestone version,
  1.16→1.35); scripted fakes cover engine *states*, never BusyBox
  *behavior*.
  - *As the probed target:* the session tier's nc/ncat flavor and gap
    verdicts run live against each guest, parametrized over all five
    versions — asserting the verdict (and its detail) tracks what that
    version's applet set really offers, and that a missing applet yields
    the documented negative, never a false *supported*.
  - *As the hop:* the dial fallback chain must ASK THE TOOL, not predict it
    — the chain's command selection consults the hop's resolved userland.
    The bed guests' own hop is a full test VM, so the ash/BusyBox-hop arm
    of the chain (no `/dev/tcp` bashisms, applet-gap degradation to
    *not-checkable* — "hop offers no dial tool" — rather than a fabricated
    *closed*) is pinned by unit tests with scripted hop userlands, each
    state mutation-proven.
- **e2e (bed):** a live unix host reports its real menu; a fixture host
  whose declared menu deliberately overstates produces the drift table; the
  Zephyr bed exercises the hop-side dial and console verdicts on the
  embedded family; and the BusyBox guests (§6) exercise the full
  unix-behind-a-hop path live — telnet login tier through the hop, hop-side
  dials, nc/userland session tier per version, and the permanent ssh
  true-negative in the drift table. The docker budget stays reserved for
  the 1-2 old-OS e2e tests (house rule) — nothing here uses it.
- The existing userland-probe tests keep passing with the table conversion
  (assertions on content, not on box-drawing characters).

## 9. Documentation

The host guide page covering the probe verb documents the tiers, the states,
the vantage column, and the pin workflow; the CLI reference regenerates.
The BusyBox/userland guide section gains one line pointing at the merged
report (one recon, two consumers).

## 10. Out of scope / deferred

- Fleet-wide sweep aggregation — the verb stays per-host; existing walk
  mechanisms can drive it across hosts.
- Auto-applying the pin to lab data.
- UDP service checks (tftp stays not-checkable).
- A custom-backend `probe_strategy` hook on the registries — added the day a
  custom backend needs a bespoke check; the engine consults built-in
  strategies until then.
- Any change to `--dry-run --probe`.
