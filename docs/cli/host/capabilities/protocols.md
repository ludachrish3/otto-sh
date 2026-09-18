(protocol-survey)=

# Protocol survey

`otto host <id> probe` also answers a second question: which term,
transfer, and monitoring protocols this host really supports, and on
which ports. It runs after the {doc}`userland section <userland>` and
prints one table, a few footnotes, a drift table when something disagrees
with the lab record, and a pasteable pin.

```bash
otto host <id> probe
```

## The tiers

The survey checks from the strongest evidence down, and a row names the
tier that produced it:

- **login** — a real login over the protocol on that port. The only tier
  that can say a term or ftp is *supported*. One attempt per protocol and
  port, with the first cred in the host's `creds` that applies to the
  protocol; the row names it (`login 'admin'`, or `login 'admin [ftp]'`
  when the cred is scoped, or `login 'admin' (--user)` when `--user` forced
  the pick). A refused login is never retried.
- **session** / **userland** — checks made from inside the session that
  logged in: `shell` is the session itself, `scp` and `nc` are the
  binaries the userland recon found, `sftp` is the subsystem opening.
- **inventory** — the listening sockets and their owning programs, read
  from inside the session (`ss`, else `netstat`, else `/proc`). On an
  embedded host an external sweep replaces it — from the hop when the host
  is hopped, else from the controller.
- **dial** — a TCP connect and a banner read from the real connect path:
  through the hop when the host is hopped, else from the controller. The
  snmp check files under this tier too — an unauthenticated probe against a
  candidate port, over UDP with a `sysUpTime` GET instead of a banner read.

## States

| state | meaning |
|---|---|
| `supported` | verified at an authoritative tier |
| `login-failed` | the service answered, the login was refused |
| `service-mismatch` | the port answered with a different service, or a binary is absent |
| `closed` | the connect was refused, or the inventory shows nothing bound |
| `listening` | a socket is bound; no authoritative check ran (the owner is in `detail`) |
| `timeout` | no answer within the check's own timeout, or the survey's whole budget ran out (the detail says which) — never folded into `closed` |
| `no-session` | needs a session the survey could not get |
| `not-checkable` | no honest check exists; the reason is stated |

`listening`, `timeout`, `no-session` and `not-checkable` are unknowns: they
never appear in the drift table and never feed the pin.

## Port and vantage

Every row names the port it was reached on, so ssh can show `22 closed`
beside `2222 supported`. `vantage` is where the observation was made from:
`controller`, `hop:<id>`, `session:<term>` for in-session tiers, or `local`
for the machine otto itself runs on. snmp is the one exception: pysnmp
issues the GET itself, a local UDP call never routed through a hop the way
a dial or a login is, so its row always reads `controller` — even when
every other row on that same hopped host reads `hop:<id>`.

## Owners, and running as root

Owner names for root-owned daemons need root. The inventory runs elevated
only when the userland resolved `sudo` or `su` **and** the cred can answer
the prompt; otherwise it runs plain and the report says so:

```text
owners incomplete: inventory ran as 'admin' without root; run otto host <id> probe --user root for owner names
```

`--user root` opens the session as that login through the cred's proxy
chain, exactly as `otto host <id> login --user root` does — which may
itself run through `su`, when the cred's proxy is (or defaults to) `su` —
and the inventory then runs there **unwrapped**: no per-command `sudo` or
`su` wraps the inventory script itself.

## Embedded hosts: the sweep and `--scan-ports`

A host with no shell has no in-session inventory, so the survey dials a
bounded set of TCP ports from the hop: the family defaults, a short list of
common alternates, and ssh's 22 and 2222. Never a range by default; the
sweep itself runs two dials at a time, half a second each, because an RTOS
target has a small socket pool. The declared console port is not among them
— its verdict is the host's own console connection, and a second client on
a single-client console buys nothing — and neither is snmp, whose check is
the controller-issued GET described above.

`--scan-ports 2000-2010,8080` adds ports or ranges to that sweep. It is not
embedded-only: a unix host accepts it too, adding those ports to its
ordinary dial tier — one at a time, at the same 2 s timeout every other
dial on that host uses, not the sweep's tighter budget. A bad value — a
reversed range, a port outside 1..65535, or a range wider than 1024 ports —
is refused before the survey makes any contact with the device, so a typo
costs nothing on a slow link.

A telnet banner on an unexpected port is a discovered console port and
gets a real console open; an ssh banner lands in the *other listeners*
footnote — the hint that a device may be misfiled as embedded. On a
Zephyr 3.7 guest a dead port reads `timeout`, not `closed`: its stack
answers a SYN to a closed port badly, and the survey does not pretend to
know better.

## The pin

Recon once, then pin. When something differs from the lab record the
report ends with a plain-text block to paste into the host's entry:

```text
valid_terms = ["ssh"]
"ssh_options": {"port": 2222}
```

Menus list supported protocols only; a `*_options` fragment appears only
for a protocol whose working port differs from the declared one (`"snmp":
{"port": N}` for the monitoring block). Two or more supported ports that
are all undeclared produce no fragment at all — just a footnote naming the
choice: `ssh: supported on 2222 and 2223; choose one in ssh_options.port`.
Nothing is applied for you.
