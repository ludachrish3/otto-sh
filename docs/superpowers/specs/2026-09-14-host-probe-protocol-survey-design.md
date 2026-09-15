# Host Probe Protocol Survey — Design (amended)

**Date:** 2026-09-14
**Status:** Approved by Chris 2026-09-14 (brainstormed section by section in
session; the written spec, including the two corrections found while writing —
tftp and snmp, marked below — approved on review). Awaiting implementation
plan.
**Supersedes:** `2026-08-20-host-probe-protocol-survey-design.md` (approved,
never implemented). This is a complete document: every 2026-08-20 decision
that still stands is carried forward here; each amendment is marked
**Amendment**. The old spec keeps a one-line status note pointing here.
**Consumes:** `2026-09-13-per-protocol-cred-scope-design.md` — the authority
for which cred the login tier tries for a protocol (`default_login(creds,
protocol)`, §3.2 there), and `2026-08-20-busybox-bed-and-tier-migration-design.md`
(shipped) — the five BusyBox guests behind test1 are this spec's live
unix-behind-a-hop targets.
**Amended 2026-09-15** (Rulings 20-22, during implementation): §3's `closed`
and `not-checkable` reasons, §7's declared-port dial, §11's bed expectations.
Each amendment is marked in place.
**Surface:** the `otto host <id> probe` verb (`@cli_exposed`, today the
userland recon on posix-shell hosts, `UserlandHost.probe`). The root
`--dry-run --probe` flag and its connection-only contract
(`2026-08-15-dry-run-contract-design.md` §3) are untouched.

## 1. Problem

`otto host <id> probe` answers one question today: what userland does this
host have. Operators also need to know which **term** protocols (ssh,
telnet, console) and which **file-transfer** protocols (scp, sftp, ftp, nc,
shell, console) a host actually supports — the complete, verified list —
both to catch lab-data rot (a declared protocol that no longer works) and to
discover capability the record undersells (a working protocol nobody
declared). Port scans alone cannot answer this: scp/sftp/shell all sit
behind ssh's port, nc has no standing listener, and console rides the term
session. Most transfer verdicts are only knowable from inside a session.

**Amendment.** Two asks from 2026-09-13 extend the question:

- A service often listens on a **non-default port** (sshd on 2222, a
  hand-made ftp server on 2121, snmpd on 1161). The 2026-08-20 survey dials
  only each protocol's *declared* port, so it reports such a host as
  `closed` and never finds the working port. The probe must discover
  listening ports **from inside the session** — listening sockets and the
  programs that own them — and print pasteable `*_options` fragments for
  what it finds.
- Since the cred-scope rework, a host's creds are an ordered list where a
  protocol logs in as the first entry that applies to it. The login tier
  must pick per protocol through that rule and **name the cred it tried**,
  so a `login-failed` row says which entry was refused.

## 2. Decisions carried forward from 2026-08-20

Unchanged, and normative here:

- **Surface:** the `probe` verb. It already costs a real connection and may
  run commands, so it can verify properly. `--dry-run --probe` stays narrow.
- **Candidates: all known protocols** — every registered term/transfer
  backend applicable to the host's family, declared or not. The probe is
  discovery, and reports drift in both directions.
- **Depth: tiered, authenticating where the verb already pays for it.**
  Port and banner checks are evidence; logins and in-session checks are the
  authoritative tiers. nc support is determined by logging in through a
  term method, checking for nc or ncat, and verifying its userland options.
- **Vantage: route dial checks through the hop.** A host reached via a hop
  or login proxy gets its TCP/banner checks executed from the final hop
  over that hop's existing session, so the survey observes from where the
  real connect path observes.
- **Presentation:** every table renders as a Rich `Table` with
  `box.ROUNDED`; pasteable payload blocks stay plain text.
- **Exit semantics:** every answered outcome — dead protocols included — is
  `Success`; dry run short-circuits exactly as the verb does today.
- **No verdict is fabricated from an absence**, and the probe never
  retries a refused login (credential-lockout hygiene).
- **Ports come from the host's own options** (`ssh_options.port`,
  `telnet_options.port`, `ftp_options.port`, `snmp.port`), never literal
  well-known numbers.

**Amendments in this document**, each marked where it lands: the
`inventory` tier (§3, §4); the `listening` state and the `port` field on
every verdict (§3); the owner vocabulary (§3.3); elevation and the
owners-incomplete notice, with `--user` on the verb (§4.2); the snmp active
check (§5); discovery on undeclared ports, per-protocol resolution, and the
`*_options` pin fragments (§6); the embedded external sweep and
`--scan-ports` (§8); the login tier's per-protocol cred pick (§3.2).

## 3. Verdict model

One `ProtocolVerdict` per **(protocol, port)** checked:

- `protocol` — registered backend name, or `snmp` (§5);
- `kind` — `term` / `transfer` / `monitor`;
- `port` — **Amendment.** The port this verdict was reached on. An ssh
  `supported` at 2222 and a `closed` at the declared 22 are two rows. Making
  the port a field is what lets the pin say `ssh_options: {"port": 2222}`.
- `state` — one of:
  - **supported** — verified at an authoritative tier;
  - **login-failed** — dialed and the service matched, but authentication
    was refused (a definitive "not usable as configured");
  - **service-mismatch** — the port answered, but not with the expected
    service (the wrong-service-squatting guard);
  - **closed** — the dial was refused, or the inventory shows nothing bound
    on that port. **Amendment (Ruling 20).** Behind a hop nothing of otto's
    touches the socket — the connect is a direct-tcpip forward the hop
    performs — so a refusal arrives as a channel otto's client could not open
    (`OPEN_CONNECT_FAILED`) and reads `connection refused (via hop)`. It is
    `closed`, never `login-failed`: no login was refused, and `login-failed`
    is an answer that drifts and pins;
  - **listening** — **Amendment.** A socket is bound on port N for this
    protocol's transport, and no authoritative check exists or ran. It is an
    *unknown* like `timeout` and `no-session`: it never feeds the drift
    table and never feeds the pin. Confidence rides in `detail` (§3.3).
  - **timeout** — the check ran out of budget; never folded into closed;
  - **no-session** — the verdict needs a session this probe could not get
    (names the carrier it needed and the terms it tried);
  - **not-checkable** — no honest check exists; reason stated. Shrinks to:
    tftp (§5.2), Docker containers (§9), a protocol no cred applies to
    (§3.2), snmp without pysnmp (§5.1), and a hop with no dial tool (§10).
    **Amendment (Ruling 20).** Two more, both the hop declining to open the
    channel rather than a condition at the target: a hop that forbids port
    forwarding (`OPEN_ADMINISTRATIVELY_PROHIBITED`), stated as such, and any
    other channel-open code, stated on the code's own reason text.
- `tier` — which tier produced the verdict: `login` / `session` /
  `userland` / `inventory` / `dial`;
- `vantage` — where the observation was made from (`controller`,
  `hop:<host-id>`, or the session's own path for session/inventory tiers);
- `detail` — one line (banner text, binary found, owner name, cred tried,
  tool used, reason).

### 3.1 Tiers, strongest first

- **Login tier (authoritative for terms and ftp).** A term is *supported*
  only when a real login succeeds over it. The probe attempts one login per
  candidate (term, port) and per (ftp, port) — declared, or discovered by
  §6 — using the cred §3.2 picks. Candidates are independent: one failure
  never aborts the others. ftp's transfer verdict is a real control-channel
  login. Ssh on a discovered port reuses the host's `known_hosts` setting
  unchanged; a host-key refusal is `login-failed` with the reason, never
  bypassed.
- **Session tier (authoritative for most transfers).** Over one established
  session (the resolved term first; else the first term that logged in):
  **nc** = nc/ncat binary present + flavor + the userland-options recon the
  verb already runs; **scp** = server-side scp binary presence via the same
  recon; **sftp** = the subsystem actually opens over the live ssh
  connection (with telnet-only access sftp reports *no-session* naming ssh);
  **shell / console** = the carrying session itself working.
- **Inventory tier (evidence and discovery). Amendment.** Runs once over
  the same session, right after the userland recon. Lists every listening
  socket with its owning program (§4). It never produces `supported`. It
  produces `listening` / `closed` rows for declared ports, discovery
  candidates for undeclared ones (§6), and the "other listeners" footnote.
  On embedded hosts the inventory tier is the external sweep (§8).
- **Dial tier (evidence, and negative verdicts).** TCP connect + banner
  classification — ssh `SSH-2.0-…`, ftp `220 …`, telnet IAC negotiation —
  against each candidate (protocol, port). Produces *closed* /
  *service-mismatch* / *timeout* and discovery triggers; on its own
  upgrades nothing to *supported*.

Strength order: login > session > inventory > dial; `userland` labels the
session-tier rows the recon produced, as before. **One row per (protocol,
port):** when more than one tier reports on the same pair — a refused
connect at the login tier and a `closed` from the inventory, say — the row
carries the strongest tier's verdict and names that tier.

### 3.2 Which cred the login tier tries — Amendment

For each candidate protocol the cred is `default_login(creds, protocol)`
from `otto.host.login_proxy`: the first entry in the host's ordered `creds`
that is unscoped or scoped to that protocol; `""` when none applies, in
which case the row is `not-checkable: no cred applies to <protocol>` and no
login is attempted. One attempt per (protocol, port); the probe never
cycles through creds.

`detail` names the cred as `login 'admin'`, using the cred-scope spec's
display identity — `admin` when unscoped, `admin [ftp]` when scoped — so a
`login-failed` row says exactly which entry was refused.

With `--user <login>` (§4.2) that login replaces the pick for every
protocol it applies to, and the row's `detail` says `login 'root' (--user)`.

### 3.3 Owner vocabulary and evidence levels — Amendment

The survey module carries one small table mapping a protocol to the
program basenames that serve it:

| protocol | owner basenames |
|---|---|
| ssh | `sshd`, `dropbear` |
| telnet | `telnetd`, `in.telnetd`, `busybox` running the `telnetd` applet |
| ftp | `vsftpd`, `proftpd`, `pure-ftpd`, `ftpd`, `in.ftpd` |
| tftp | `in.tftpd`, `tftpd`, `atftpd`, `dnsmasq` |
| snmp | `snmpd` |

Matching is on the basename; BusyBox's `busybox: telnetd` / `busybox
telnetd` forms are recognised. tftp's row exists only to name listeners in
the footnote (§5.2).

**Owners corroborate, banners decide (TCP); owners are the evidence (UDP).**
Three evidence levels, all state `listening`, distinguished in `detail`:

- port and owner agree — `listening on :161, owner snmpd`;
- port agrees, owner unknown or hidden by lack of elevation —
  `listening on :161, owner unknown`;
- owner agrees on an unexpected port — `listening on :1161, owner snmpd`
  — the **discovery** case (§6), on a par with an ssh banner on 2222.

## 4. The inventory tier (unix and local hosts)

### 4.1 Script, tools, parsing

One compound POSIX shell script, in the nc backend's port-finding style
(`ss → netstat → /proc`, first tool present wins, cached per host object):

1. `ss -tulnp`, else
2. `netstat -tulnp`, else
3. a `/proc` walk: `/proc/net/tcp`, `/proc/net/tcp6`, `/proc/net/udp`,
   `/proc/net/udp6`, socket inodes joined to `/proc/*/fd` links for the
   owner's `/proc/<pid>/comm`.

The tool chosen, and whether the script ran elevated, are recorded once in
the inventory rows' `detail` (`inventory: ss, elevated` / `inventory:
netstat, unelevated`).

Parsing turns every LISTEN (TCP) or bound (UDP) line into (transport, bind
address, port, owner-or-none). **Loopback-only binds are dropped** and
counted in one footnote line (`3 loopback-only listeners not shown`). IPv6
and wildcard binds are kept. A blank owner column — BusyBox `netstat`
without root, or GNU `netstat -p` for another user's process — is `owner
unknown`, never a parse failure. Output the parser does not understand
produces one footnote line naming the tool and the survey continues with
what it has (§10).

Local hosts run the same script on the machine otto runs on, with no dials.

### 4.2 Elevation, the owners-incomplete notice, and `--user`

Owner names for root-owned daemons need root. The rule is: **be polite, no
guessing, no second attempt.**

- The script is wrapped through the existing sudo/su elevation path only
  when the userland resolved `elevation` to `sudo` or `su` **and** the
  session's cred has a password to answer it. Otherwise it runs plain.
- A refused elevation is recorded and never retried.
- Whenever the inventory ran unelevated, or elevation was refused, one line
  under the protocol table reads:
  `owners incomplete: inventory ran as 'admin' without root; run otto host <id> probe --user root for owner names`.
  Rows whose owner is unknown say `owner unknown` in `detail`, never a blank.
- **`--user` on the probe verb.** `probe` gains `user: str | None = None`,
  synthesised as `--user` like the per-call `user=` the other verbs carry.
  With `--user root` the term session is opened as that login through the
  cred's proxy chain exactly as `login(user=)` does today, and the
  inventory runs in that session with no sudo/su wrapping at all. The login
  tier then tries that login for every protocol it applies to (§3.2). No
  elevation prompt is ever answered unless the operator asked for root, or
  the userland resolved `elevation` and the cred can answer it.

### 4.3 What the inventory feeds

- Each candidate protocol's **declared** port gets an inventory verdict —
  `listening` or `closed` — before any dial. A `closed` here is
  authoritative for that port on that host (nothing is bound), so the dial
  tier skips it.
- Every other non-loopback **TCP** listener goes to the dial tier for a
  banner (§6).
- **UDP** listeners on the snmp port, or with an `snmpd` owner on any port,
  go to the snmp active check (§5.1).
- Listeners that map to nothing otto speaks — including tftp listeners —
  are listed in one "other listeners" footnote as `port/proto owner` pairs.

## 5. Active checks

### 5.1 snmp — Amendment

snmp is not a term or transfer backend; it is the host's `snmp` block
(`SnmpOptions`: `community`, `port`, `version`, `address`) that the monitor
polls. The survey treats it as a candidate of kind `monitor` on every
family, because a working agent on an unexpected port is exactly the drift
an operator wants told.

The check: one `SnmpClient.get(["1.3.6.1.2.1.1.3.0"])` — `sysUpTime`, a
numeric TimeTicks value the shipped client already coerces — against the
host's `snmp` block when declared (its community, version, port, and
address), else the client's own defaults (`public`, `2c`, 161, the host's
ip). Reached the same way the monitor reaches the agent: from the
controller, or through the hop's UDP relay endpoint when the host declares
one. Verdicts:

- a value comes back → `supported`, detail `sysUpTime <n> via community
  'public'`;
- silence → `timeout`, detail `no reply from :161 (no agent, or the
  community was refused: v2c agents drop a wrong community silently)`.
  **Correction vs brainstorm:** the brainstorm expected `login-failed` on a
  wrong community; SNMP v2c cannot distinguish that from no agent, so the
  survey does not pretend to;
- an SNMP error-status → `service-mismatch` with the status text.

pysnmp is a hard runtime dependency (otto ships as one package with no
extras), so there is no "not installed" arm. The module graph forbids
`otto.host` importing `otto.monitor` (monitor depends on host), so the one
pysnmp GET moves to a new leaf module `otto.snmp` that returns the raw
outcome — answered / silence / error-indication / error-status — and
`otto.monitor.snmp.SnmpClient.get` becomes a caller of it with its numeric
coercion and warning logs unchanged. The survey imports the leaf.

A discovered `snmpd` on an undeclared port (§3.3) gets the same check on
that port; the pin fragment key is `snmp: {"port": N}` because the options
block is named `snmp`, not `snmp_options`.

### 5.2 tftp — Correction vs brainstorm

The brainstorm added a tftp read-request check expecting an ERROR packet
from any real server. Writing this up against the code showed it answers
the wrong question: otto's `tftp` transfer backend is a **stub** (`create`
works, both directions raise `NotImplementedError`), and `TftpOptions`
models otto as the *server* (`server_ip` is where otto binds) with the
**target running the client**. A tftp *listener on the target* is therefore
no evidence that otto's tftp transfer works, and a `supported` row built
from it would be a lie.

So: tftp stays `not-checkable: tftp backend not implemented` on every
family, no active check is built, and a tftp listener found by the
inventory or the sweep appears only in the "other listeners" footnote
(`69/udp in.tftpd`). When the backend lands, its probe is a session-tier
client-presence check (`tftp` applet / shell command), on the nc model,
and belongs to that spec.

## 6. Discovery, resolution, and the pin — Amendment

**Discovery.** A TCP listener whose banner classifies as ssh, telnet, or
ftp on a port other than that protocol's declared one is a *discovered
port*; so is a UDP listener with an `snmpd` owner on an undeclared port.
Each becomes a candidate (protocol, port) and goes to the authoritative
tier: the login tier for ssh, telnet, ftp; the snmp check for snmp. On
unix hosts the source is the inventory; on embedded hosts it is the sweep
(§8). The dial tier runs the banner check from the usual vantage (through
the hop when the host is hopped).

**Resolution per protocol.** After the authoritative tier each protocol
has zero or more `supported` ports:

- the declared port wins when it is among them;
- else, exactly one other supported port is that protocol's *working port*;
- two or more supported undeclared ports produce a row each and **no** pin
  fragment, with a footnote: `ssh: supported on 2222 and 2223; choose one
  in ssh_options.port`.

**The pin.** One plain-text block, only when non-empty, with these keys in
this order and each only when it differs from what the lab declares:

1. `valid_terms = [...]` and `valid_transfers = [...]` — built from
   *supported* entries only (2026-08-20 rule);
2. one `"<protocol>_options": {"port": N}` fragment per protocol whose
   working port differs from the declared one (`"snmp": {"port": N}` for
   snmp).

The existing `"userland_options": {...}` pin stays its own block. RECON
ONCE, THEN PIN, as the userland pin; nothing is ever auto-applied.

**Drift table.** Rules unchanged: *declared-but-dead* (in the menus but
verified login-failed / service-mismatch / closed on the declared port) and
*working-but-undeclared* (supported but absent from the menus). A protocol
dead on its declared port but supported on a discovered one appears as
declared-but-dead for the declared port, and its pin fragment beneath is
the remedy. `listening`, `timeout` and `no-session` are unknowns and never
appear here.

## 7. Survey flow and vantage

New module `src/otto/host/protocol_survey.py` owns the engine:

1. **Candidates** — registered term/transfer backends filtered by the
   host's family, plus snmp.
2. **Login tier, declared ports** — one login per candidate term and ftp
   on its declared port, bounded by the family's connect timeout, failures
   isolated. **Amendment (Ruling 21).** Every declared ftp and non-own-term
   port is dialed from the host's own vantage FIRST: a `closed`, `timeout` or
   `not-checkable` dial is that candidate's row and no login is tried on it.
   The login classifier reads a failure on the premise that something is
   listening, which for a declared port nothing had established — through a
   hop the local end of the forward succeeds and the EOF behind it reads as a
   refused password, fabricating `login-failed` for a port with no service.
   It is also lockout hygiene: a refused login is never retried, and never
   attempted on a dead port. The host's OWN term is not pre-dialed — `host.run`
   proves that session and is stronger evidence than a dial — and snmp is not
   dialed at all, being UDP with the §5.1 GET as its check. If no term logs in at all, every session-dependent candidate
   reports **no-session** with the terms tried, the inventory does not run,
   and step 3 is skipped; the declared-port dials of step 5 still run.
3. **Session tier** — over the first successful term session: the userland
   recon (one recon, two consumers — the verb's userland section reuses
   it) and the sftp subsystem check.
4. **Inventory tier** — the §4 script over the same session (unix, local),
   or the §8 sweep (embedded). Declared-port `listening`/`closed` rows;
   discovery candidates; footnotes.
5. **Dial tier** — concurrent banner checks against every declared port
   the inventory did not settle and every discovered TCP port. Directly
   routable hosts are dialed from the controller (`asyncio.open_connection`
   + bounded banner read). A hopped host has the dial executed on the final
   hop through that hop's existing session via the BusyBox-aware fallback
   chain (`nc -z`, `/dev/tcp`, the userland gap machinery deciding what the
   hop offers). Every dial verdict records its vantage.
6. **Login tier, discovered ports** — one login per discovered (term|ftp,
   port); the snmp check per (snmp, port).
7. **Resolution and report** (§6, §9).

## 8. Embedded hosts: the external sweep — Amendment

A family with no shell has no in-session inventory; "cannot be determined"
is not an answer when a dial can be. On embedded hosts the inventory tier
is an **external sweep** run from the vantage the real connect path uses:
on the final hop through its session when the host is hopped, else from
the controller.

- **TCP:** a connect attempt per port with a **0.5 s** timeout and **at
  most two in flight**, because a Zephyr target has a tiny socket pool and
  its 3.7 stack answers a SYN to a dead port badly.
- **UDP:** no listener is visible from outside; the snmp active check *is*
  the sweep for snmp, one GET per candidate port.
- **Bounded port set, never a range by default:** the declared ports; the
  family's backend defaults (23 for the telnet console, 161 for snmp); a
  short curated alternates list kept in the survey module beside the owner
  vocabulary — telnet `2323`, `8023`; snmp `1161`; and ssh `22`, `2222`.
- **`--scan-ports`** on the verb: `scan_ports: str | None = None`, a
  comma-separated list of ports and `a-b` ranges (`"2000-2010,8080"`) for
  the operator who wants more. The concurrency and timeout caps still apply
  and the whole-survey budget still ends it. (Typer takes `Optional[X]`
  only, hence a string rather than a list.)
- **Verdicts:** an open port with a telnet banner is a discovered console
  port and goes to the console login tier; an open port with no
  classifiable banner is `listening, owner unknown`. ssh is not an
  embedded-family backend so it takes no table row: a classified
  `SSH-2.0…` banner lands in the "other listeners" footnote with its banner
  text — the hint that a device may be misfiled as embedded. Every sweep
  row records its vantage; a hop with no dial tool degrades every sweep row
  to `not-checkable: hop <id> offers no dial tool`, never to closed.

Unix hosts keep the in-session inventory and do not sweep; `--scan-ports`
on a unix host is accepted and adds those ports to the dial tier.

**Known guest defect, reported honestly.** Zephyr 3.7's RST to a SYN is
malformed, so a Linux `connect()` to a *closed* port on a healthy 3.7 guest
times out instead of being refused (`tests/firmware/zephyr/README.md`,
"Known guest defects"). On such a guest a dead port therefore reads
`timeout`, never `closed`, and the survey does not fold one into the other.
4.4 guests refuse correctly.

## 9. Report layout, verb surface, families, budgets

Output order for `otto host <id> probe`:

1. **Userland section** — content unchanged, table as Rich `box.ROUNDED`,
   its `"userland_options"` pin plain text.
2. **Protocol table** (Rich `box.ROUNDED`): protocol / kind / port / state
   / tier / vantage / detail — one row per (protocol, port) checked, so ssh
   can show `22 closed` and `2222 supported`. Then up to four footnote
   lines, each only when it has content: *not applicable to this family*;
   *owners incomplete* (§4.2); *loopback-only listeners not shown* (§4.1);
   *other listeners* as `port/proto owner` pairs (§4.3, §8). Inventory or
   parse failures add their one line here (§10).
3. **Drift table** (Rich `box.ROUNDED`), only when non-empty.
4. **Menu and options pin** (plain text), only when non-empty (§6).

**Verb surface.** `probe(user: str | None = None, scan_ports: str | None =
None)`, synthesised as `--user` and `--scan-ports`. `--user` is honoured on
families with a session identity (unix); on any other family it is refused
before contact with the message `switch_user` already uses. Exit semantics
unchanged. The CLI result renderer (`render_leaf_value`) learns to print
Rich renderables interleaved with strings — a general improvement any
future tabular verb inherits — and the verb returns the mixed sequence.

**Families.**

- **Unix:** everything above.
- **Embedded / Zephyr:** the family gains the verb (today only the posix
  hosts carry it); the console term verdict (the console session
  opening), the sweep as inventory (§8), the snmp check; candidate set is
  the embedded family's (console transfer); unix-only protocols appear only
  in the footnote; no userland recon where the family has none — the
  existing no-resolver line stays.
- **Local:** the inventory runs locally, no dials, session-tier verdicts;
  `not-checkable: no transport` where nothing applies.
- **Docker containers:** `not-checkable` rows with the stated reason (the
  family has no generic probe; the dry-run probe's NOT_PROBED honesty rule).
  Owner detection inside containers is out of scope (§13).

**Budgets** (module constants): inventory script 5 s; snmp check 2 s
(`SnmpClient.timeout`, one retry); per-dial 2 s, sweep dials 0.5 s;
per-login the family's connect timeout; whole survey 90 s — exhausting it
marks the remaining checks `timeout`, runs nothing further, and still
prints the report.

## 10. Error handling

- **No verdict is fabricated from an absence.** Timeouts say timeout,
  missing sessions say no-session, nothing bound says closed only when the
  inventory actually looked.
- A failed inventory script, output the parser does not understand, or a
  refused elevation each produce **one stated-reason footnote line**
  (`inventory failed: netstat exited 1` / `inventory: unparsed output from
  ss, 4 lines ignored` / `elevation refused by sudo`) and the survey
  continues with what it has; declared ports then fall through to the dial
  tier as in 2026-08-20.
- The probe never retries a refused login or a refused elevation.
- A hop that cannot execute dial commands degrades those verdicts to
  `not-checkable`, never to closed.

## 11. Testing

- **Unit, engine.** Scripted per-tier fakes drive every state — including
  `service-mismatch` via a wrong-banner fake, `login-failed` via a
  credential-refusing fake, hop-vantage dial via a scripted hop session,
  discovery promotion (ssh banner on 2222 → login attempted on 2222; `snmpd`
  on 1161 → GET on 1161) — plus, new: the inventory parser over **real
  captured output** from `ss -tulnp`, `netstat -tulnp` (GNU, and BusyBox
  with and without owners) and `/proc/net` files with an fd join; loopback
  exclusion; the owner vocabulary including the BusyBox applet forms;
  elevation chosen, refused, and unavailable each producing the right
  notice; `--user` replacing the cred pick; the resolution rule with zero,
  one, and two supported undeclared ports; the sweep's concurrency cap and
  timeout **injected, never inherited**; `--scan-ports` parsing incl. bad
  input; the snmp check against a scripted UDP responder (answer, silence,
  error-status). Tier and vantage labels asserted, not just states. Every
  guard mutation-proven.
- **Unit, report.** Table rows asserted structurally (Rich tables, never
  screen-scraped ANSI), the port column present, footnotes present only
  when they have content, the pin built from supported-only and
  differing-only, drift excludes unknowns.
- **BusyBox bed (five guests, both roles).** *As target:* the inventory runs
  live per guest version — the `netstat` arm on each, owner blanks handled,
  the unelevated notice when the guest cred cannot elevate; the session
  tier's nc/ncat flavor and gap verdicts per version. *As hop:* the dial
  chain ASKS THE TOOL — command selection consults the hop's resolved
  userland; the ash/BusyBox-hop arm (no `/dev/tcp` bashisms, applet-gap
  degradation to `not-checkable`) pinned by unit tests with scripted hop
  userlands. Their permanently dead, undeclared ssh stays the drift table's
  standing true-negative.
- **e2e (bed).** **Amendment (Ruling 22):** the three expectations below
  replace what this bullet asked for first, each refuted by the live bed for a
  reason the spec's own text gives. A unix bed host reports its real menu with
  exactly one drift row and one pin line — `shell` is the carrying session, so
  the session tier states it `supported` on every host whose own term opens,
  and no lab menu names it: §6's working-but-undeclared rule fires by the book
  (it will do so on every unix host, not just this one). A fixture host whose
  **`ftp_options.port`** deliberately points at a dead port produces the drift
  row and the `ftp_options` pin fragment — not `ssh_options`, because a wrong
  own-term port kills the session the inventory and discovery ride on, so that
  example can produce no pin at all. The Zephyr bed exercises the sweep from
  the hop with telnet found, and the 4.4-`closed`/3.7-`timeout` differential is
  read at the **dial tier on a swept alternate port (2323)**: the sweep keeps
  no verdict and no listener entry for a non-open non-console port, so that
  difference is unobservable in the survey's rows and is asserted on the dial
  the sweep itself uses, with the 4.4 guest as the positive control. The snmp
  check runs against whichever bed host serves an agent and is the first
  thing dropped if it proves flaky (Chris's call, recorded in session).
  Docker budget untouched (house rule).
- The existing userland-probe tests keep passing with the table conversion
  (assertions on content, not box-drawing characters).

## 12. Documentation

- `docs/guide/cli/host/capabilities/userland.md` stays the home for the
  userland pin.
- New page `docs/guide/cli/host/capabilities/protocols.md`, in this order:
  the tiers; the states, including `listening`; the port and vantage
  columns; the owners-incomplete notice and `--user root`; the embedded
  sweep and `--scan-ports`; the pin workflow. The capabilities index gains
  its row.
- `docs/guide/configuration/host-options.md` gains one sentence under the
  `port` fields linking to that page (one home per topic; link, never
  restate).
- The CLI reference regenerates.

## 13. Out of scope / deferred

- Auto-applying the pin to lab data.
- Range scans by default on any family.
- Fleet-wide aggregation — the verb stays per-host.
- A custom-backend probe hook on the registries.
- Any change to `--dry-run --probe`.
- Owner detection on Docker containers.
- A tftp check (§5.2) — belongs to the spec that implements the backend.

## 14. Supersession

`2026-08-20-host-probe-protocol-survey-design.md` gets a one-line status
note pointing at this document and is otherwise left as written. Where this
document is silent, nothing from the old one applies: it is complete on its
own. The cred-scope spec remains the authority for the login tier's pick.
