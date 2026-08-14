# BusyBox and other non-GNU userlands

otto's unix path assumes a GNU/coreutils userland with bash. A BusyBox device
is not that, and this page is the standing answer to what happens when otto
meets one: which surfaces are known broken, which are merely unmeasured, what
proved each verdict, and who would close it.

It is the readable rendering of one table in the source —
{data}`~otto.host.userland.GAPS` in `src/otto/host/userland.py`. That table is
the single source of truth for three audiences: the runtime error a user hits,
this page, and the parity queue under `todo/`. `tests/unit/test_docs_gap_sync.py`
pins this page to it in both directions, so a gap cannot be closed in code while
this page still claims it is open, or the reverse.

```{note}
**Division of labour with the {doc}`BusyBox matrix page <../../guide/hosts/busybox>`,
so the two do not drift.** That page is about *running otto's BusyBox matrix*: the
prerequisites (`qemu-user-static`, `dropbear-bin`, `openssh-client`), which upstream artifacts are
pinned and why, the lanes, and the trust note for executing an unsigned
third-party binary. This page is about *otto's behaviour against such a device*.
Facts about the test harness live there; facts about what otto can and cannot do
live here. Neither restates the other.
```

## Most differences are adaptations, not gaps

A gap is what is left over after otto has adapted, and otto adapts to most of
what BusyBox does differently. {class}`~otto.host.userland.Userland` probes the
device for the handful of spellings that actually vary — `elevation`,
`timeout_style`, `stat_size`, `base64_flag`, `checksum`, `shell_dialect` — and
the answers are the device's, not a guess derived from a version string. A
BusyBox host with no `sudo` is not a gap: `su` is present on every matrix row,
`elevation` picks it, and otto refuses only when both answers are no.

Three candidates from the design survey were dropped for exactly this reason
once they were measured, and the reasons are recorded beside the table in
`userland.py` rather than dropped silently: `pgrep`/`pkill` were predicted
absent and measured present on all five matrix artifacts; `sudo` is absent but
adapted to, as above; and `reboot` — which the survey paired with `shutdown` —
is an applet everywhere, so only the `shutdown` half is a gap.

## The rule: measured-broken refuses up front, unmeasured runs

Every record carries one of two statuses, and the status decides whether otto
would block the call. Read the whole of this section together with the note
below it: the rule is implemented in
{func}`~otto.host.userland.refuse_if_gapped`, and **exactly one product call
site consults that function** — so for one surface below the refusal is what
otto does, and for the other seven it is still what a wired call site would do.
Each section's **Status:** line says which.

`measured-broken`
: otto has run it, watched it fail, and written down what it said. A call site
  that consults the registry refuses **before anything is sent** with
  {class}`~otto.host.errors.UnsupportedOnUserlandError`, whose message is
  rendered from the record — the surface, the reason, the measurement, the
  queue entry and a link back to the section on this page. Nothing is attempted,
  which is the one thing this exception means that otto's other host errors do
  not: nothing was learned about the device.

`untested`
: nobody has run it against this class of userland. otto does **not** block it.
  It runs, and the outcome is the measurement the entry is waiting for. Blocking
  an untested surface would convert "we do not know" into "does not work" — a
  lie in the expensive direction, because it makes otto decline things that
  work.

```{note}
**One product call site consults the registry; seven surfaces are still
unwired.** The one is
[`run-command-line-length`](#run-command-line-length): `Host.run()` reads that
record through {func}`~otto.host.userland.refuse_if_gapped` and refuses before
it types anything. For the other seven `measured-broken` rows, wiring a call
site is a behaviour change that belongs with the call site being changed, and
it has not happened — read those rows as "what otto knows", and each `reason`
and **Status:** line as what such a refusal *would* say and do.

Two refusals that fire in otto today are *not* this table at all:
`PosixPrivilege._elevate` and `ShellFileTransfer._run_put`/`_run_get` are
*probe-driven*, refusing on what the host in front of them answered — a
different trigger from "otto measured this on the matrix". That is why
[`shell-transfer-base64`](#shell-transfer-base64) refuses before sending
despite being unwired here: it gets there by probing `base64_flag`.
```

## The declared gaps

One row per record, in the table's own order. The full text of each record —
the reason, the measurement and the queue entry — is in `GAPS`, and the runtime
error prints it verbatim; the sections below are its readable form.

| Surface | Status | What it means for you |
| --- | --- | --- |
| [`shell-transfer-base64`](#shell-transfer-base64) | `measured-broken` | The `shell` transfer backend cannot move a single file on a device with no `base64` applet. |
| [`file-ops-base64`](#file-ops-base64) | `measured-broken` | `read_file` and `write_file` break on those same devices, and blame the file rather than the missing applet. |
| [`sftp-transfer`](#sftp-transfer) | `measured-broken` | No sftp subsystem on a stock BusyBox device. Use the `shell` backend. |
| [`scp-transfer`](#scp-transfer) | `measured-broken` | No `scp` binary on a stock BusyBox device. Use the `shell` backend. |
| [`nc-transfer`](#nc-transfer) | `measured-broken` | The `nc` backend cannot drive BusyBox's own `nc` applet. A real netcat installed alongside is fine. |
| [`daemon-launch`](#daemon-launch) | `measured-broken` | Launching a daemon needs bash, which a stock BusyBox userland does not have. |
| [`shutdown-command`](#shutdown-command) | `measured-broken` | `Host.shutdown()` emits a command BusyBox spells differently. `Host.reboot()` is unaffected. |
| [`run-command-line-length`](#run-command-line-length) | `measured-broken` | `Host.run()` refuses a command whose typed line would exceed 1022 characters, rather than let ash truncate it. `Host.exec()` is safe and is not refused. |
| [`product-lifecycle`](#product-lifecycle) | `untested` | otto's `stage`/`install`/`uninstall` verbs emit no command of their own. Whether they work on your device is decided by your own product code. |
| [`legacy-dropbear-crypto`](#legacy-dropbear-crypto) | `untested` | An old dropbear may need `ssh_options` to negotiate at all. Nobody has tried. |
| [`busybox-over-a-real-network`](#busybox-over-a-real-network) | `untested` | Every tier is loopback, so nothing has met a real path's MTU, latency or window. |

### shell-transfer-base64

**Status:** `measured-broken` — otto does refuse before it sends anything, and
this is the one surface here that already does. The refusal is probe-driven
(`ShellFileTransfer._run_put` resolves `base64_flag` and raises on `absent`),
not read from this table.

The `shell` transfer backend encodes every chunk with the device's own
`base64`, so a userland without that applet cannot use the backend at all.
Nothing is attempted: on such a device every file in the batch would fail
identically. Use a backend the device supports, or install `base64` on it.

**Measured:** BusyBox 1.16.1 ships no `base64` applet. `tests/busybox/test_applet_resolution.py`
records `False` for that row and `tests/busybox/test_shell_codec_contracts.py`
records a `None` decode flag, while 1.21.1 and every later matrix row decode
with `-d`.

**Queued for:** the full-parity workstream, `todo/busybox-parity-sweep-2026-08-11.md`.
`uuencode`/`uudecode` is measured-feasible on all five matrix rows including
1.16.1, and needs a codec probe plus a second codec path in the backend.

### file-ops-base64

**Status:** `measured-broken` — otto *would* refuse before sending anything,
once a call site consults the registry. These two call sites are furthest from
that: as below, they consult no `Userland` at all, so today they cannot refuse
up front even on a probe.

`Host.read_file` and `Host.write_file` move their payload through the device's
`base64` too, but unlike the `shell` transfer they hard-code it: `file_ops.py`
emits `base64 <path>` and `... | base64 -d` without ever consulting
`Userland.base64_flag`. So on a device with no `base64` they can neither refuse
up front nor adapt — the caller gets the device's own `not found`, attributed to
the file it asked for.

**Measured:** the same 1.16.1 rows as [`shell-transfer-base64`](#shell-transfer-base64),
against two call sites in `src/otto/host/file_ops.py` that read as unconditional
in the source: no `Userland` is consulted on either path.

**Queued for:** the full-parity workstream, with the entry above — the codec
probe queued there is what these two would read, so the two are one change and
not two.

### sftp-transfer

**Status:** `measured-broken` — otto *would* refuse before sending anything,
once a call site consults the registry. None does yet, so today the attempt is
made and the outcome is whatever the device does with it, below.

The `sftp` transfer backend needs a server-side sftp subsystem, and a stock
BusyBox userland ships none. Note what does *not* decide this: the ssh daemon.
Packaged dropbear serves sftp perfectly well when the machine provides an
`sftp-server` binary, so the question is what the **device** has, not which
daemon answered. Use the `shell` backend.

**Measured:** Tier 3, 2026-08-13. An `sftp` session into the pinned BusyBox root
fails with `/bin/sh: /usr/lib/sftp-server: not found` — ash inside the chroot,
not the host's shell.

**Queued for:** nothing, deliberately. The `shell` backend is the answer for
these devices, and it is verified over real ssh in Tier 3.

### scp-transfer

**Status:** `measured-broken` — otto *would* refuse before sending anything,
once a call site consults the registry. None does yet, so today the attempt is
made and the outcome is whatever the device does with it, below.

The legacy `scp` protocol needs an `scp` binary on the far side, and a stock
BusyBox userland has none. Same caveat as [`sftp-transfer`](#sftp-transfer): the
daemon is not the authority, the device's userland is. Use the `shell` backend.

**Measured:** Tier 3, 2026-08-13. `scp -O` into the pinned BusyBox root fails
with `/bin/sh: scp: not found`, and the file does not land. The two surfaces are
measured separately on purpose — `scp -O` reaches for a remote binary while
`sftp` opens a subsystem.

**Queued for:** nothing, deliberately, for the same reason as `sftp-transfer`.

### nc-transfer

**Status:** `measured-broken` — otto *would* refuse before sending anything,
once a call site consults the registry. None does yet, so today the attempt is
made and the outcome is whatever the device does with it, below.

The `nc` transfer backend cannot drive BusyBox's own `nc` **applet**: it sends
with `nc -N <ip> <port>` and listens OpenBSD-style with `nc -l <port>`, and the
applet supports neither spelling. A BusyBox device with a real OpenBSD netcat
installed alongside is fine — point `NcOptions.exec_name` at it — so this is a
gap in the applet, not in every BusyBox host.

**Measured:** the five matrix artifacts, 2026-08-13. `nc -N 127.0.0.1 1` is
rejected on every row (`nc: invalid option -- N` on the two oldest,
`nc: unrecognized option: N` on the rest), and every row's own usage line spells
the listener `nc [OPTIONS] -l -p PORT`.

**Queued for:** the full-parity workstream, `todo/busybox-parity-sweep-2026-08-11.md`,
which queues a BusyBox `nc` variant (`-l -p PORT`, size-terminated reads to
replace the missing `-N`).

### daemon-launch

**Status:** `measured-broken` — otto *would* refuse before sending anything,
once a call site consults the registry. None does yet, so today the attempt is
made and the outcome is whatever the device does with it, below.

`otto.host.daemon.launch_command` wraps every daemon in a `setsid bash -c` that
re-`exec`s it under a findable `argv[0]`, and a stock BusyBox userland has no
bash. The wrapper body is not portable to ash either, so this is not a
`bash`→`sh` substitution: it needs a different `argv[0]` mechanism.

**Measured:** the five matrix artifacts, 2026-08-13, running the wrapper body
under each row's own ash. The two oldest have no `exec -a` and answer
`ash: exec: line 1: -a: not found`. The three newest *do* parse `exec -a` and
then mis-expand `"${@:2}"` into a substring of `$1`, so the launch execs
`NTINEL` instead of `SENTINEL`. The naive fix trades a clean `not found` for a
corrupted program name.

**Queued for:** the full-parity workstream. Not yet written up in the queue
file: this table is the record, and the queue file carries the plan for work
that has one.

### shutdown-command

**Status:** `measured-broken` — otto *would* refuse before sending anything,
once a call site consults the registry. None does yet, so today the attempt is
made and the outcome is whatever the device does with it, below.

`Host.shutdown()` emits `shutdown -h now`, and BusyBox has no `shutdown` applet;
the BusyBox spelling is `poweroff`. **`Host.reboot()` is not affected** and must
not be lumped in with this — `reboot` is present on every matrix row and otto's
soft reboot works as shipped.

**Measured:** the five matrix artifacts, 2026-08-13. `shutdown` is absent from
the applet list on all five, while `reboot` and `poweroff` are present on all
five. BusyBox runs only what its own applet list carries, so the list is the
whole answer here.

**Queued for:** the full-parity workstream. `poweroff` is the measured spelling,
but choosing between the two is a userland probe — the pattern
`Userland.timeout_style` already sets — not a hard-coded swap that would break
every GNU host.

### run-command-line-length

**Status:** `measured-broken` — and this is the one surface on this page that
otto actually refuses *from this table*. `Host.run()` reads the record through
{func}`~otto.host.userland.refuse_if_gapped` and raises before it types
anything, so nothing is attempted and no connection is opened.

BusyBox ash's line editor **silently truncates** a typed line longer than 1022
characters: a different, shorter command runs and its success is reported as the
caller's. That is the failure this refusal replaces, and it was the last place
otto was knowingly wrong in the quiet direction. `run()` drives a persistent
session, which otto opens with a `dumb` terminal type, so the far side allocates
a pty and the command arrives as a *typed line* through that editor.

**What the bound applies to.** The line otto *types*, not the command you passed:
every command is wrapped in BEGIN/END sentinels first, which cost 74 characters,
so **948** is the most any single line of your command may be. A multi-line
script is judged line by line, because the editor's buffer holds one line —
a long here-doc is fine as long as no individual line is over.

**What is not refused.** `Host.exec()` opens a bare exec channel with no pty, is
unaffected, and is the way to send an over-long command. Two further paths stay
unguarded deliberately: a named session's `HostSession.run()`, and `exec()` on a
**telnet or proxied-login** host — neither has a stateless exec primitive, so
the call routes through a pooled shell session and *is* line-edited. Guarding
those would make {class}`~otto.host.transfer.shell.ShellFileTransfer` refuse its
own 5534-character chunk lines, which is the whole reason the `shell` backend
exists. Those two remain measured-but-unfixed; on them, truncation is still
possible.

**Which hosts are refused.** Those whose declared shell dialect is `ash` — the
`busybox` os_profile sets `command_frame: "ash"`, and a lab entry may set it
directly. Not every BusyBox-userland host and not every host without bash: the
buffer belongs to ash's line editor, and dash or ksh hosts have no such limit.

**Measured:** two measurements. First the phase-5 spike, 2026-08-13, dropbear
2022.83 against BusyBox 1.35.0: largest line delivered intact 1022, first
truncated 1023, with no error and no log line — identical against OpenSSH and
against a bare local pty, which is what identifies it as BusyBox ash's
`CONFIG_FEATURE_EDITING_MAX_LEN` rather than any transport, while the exec
channel took 9000 characters intact and broke at 9001. Then, because that
config is set at *build time* and one artifact cannot speak for the matrix, all
five pinned rows (1.16.1, 1.21.1, 1.28.1, 1.31.0, 1.35.0) were driven through a
local pty and answered 1022/1023 identically; the same harness carried 18437
characters into bash and over 20000 into dash, ruling itself out as the thing
being measured.

**The cost of the bound, stated plainly:** a device whose BusyBox was built with
a larger `CONFIG_FEATURE_EDITING_MAX_LEN` — or with the line editor compiled out
— will be refused a command it could actually have run. Twelve years of upstream
prebuilds agreeing is why otto takes that trade; a device that disagrees is a
new measurement, not a bug in your command.

**Queued for:** the refusal has landed; a *fix* has not, and stays unqueued
deliberately. A fix is a pty-free `run()` path, not a larger buffer — the buffer
belongs to the device — and until one exists the record stays `measured-broken`,
because the surface still is: otto declines the command rather than running a
shorter one.

### product-lifecycle

**Status:** `untested` — nothing is blocked, and there is no otto command here
to block.

Nobody has run otto's product verbs — `Host.stage()`, `Host.install()`,
`Host.uninstall()`, `Host.is_installed()` — against a BusyBox device, and no
tier can, because **those four emit no command of their own**. Each iterates
`Host.products` and delegates to a {class}`~otto.host.product.Product`, and
`Product` declares all four of its methods abstract. otto ships exactly one
concrete body — {meth}`~otto.host.product.FileProduct.stage`, a single
`await host.put(...)` — and `put` is a surface this table already covers and
Tier 3 already exercises over real ssh.

Everything else that would reach the device comes from **your** product code.
The documented shape (see {doc}`../../guide/hosts/capabilities`) has `install`
call `host.run("tar xzf …")` and `is_installed` call `host.run("test -d …")`,
so on a BusyBox device the verdict is decided by those commands and by the
`run`/`put` rows above — not by anything in `otto.host.product`.

**Measured:** nothing, and a measurement would not mean what it looked like. A
test that stood up a `Product` and staged it against a BusyBox root would be
measuring the subclass the test itself wrote, plus a `for` loop; the only otto
code under it is `host.put`, which is already measured elsewhere. That is why
this is `untested` rather than cleared — the design survey listed
`install`/`stage`/`uninstall` as a predicted gap and it sat with the *rejected*
candidates on reasoning alone, which is the one thing this table does not
accept.

**Queued for:** nothing, and not for the usual reason — there is no otto code
here to fix. What would close it is a project taking a real `Product` to a real
BusyBox device and reporting what its `install` emitted. Any gap that turns up
then belongs to the command that failed, and gets recorded under *that* surface.

### legacy-dropbear-crypto

**Status:** `untested` — otto attempts it, and the outcome is the measurement.

Real BusyBox devices run dropbear, and an old dropbear negotiates only
SHA-1-era algorithms that modern asyncssh disables by default. otto carries
cipher, host-key and kex lists in `ssh_options`, so the design calls this
configuration rather than code — unverified in either direction. Nothing is
blocked and nothing should be.

**Measured:** nothing yet. That is what `untested` means here, and it is why
this row does not refuse.

**Queued for:** Tier 3 fidelity item C, `todo/busybox-tier3-fidelity-2026-08-13.md`:
run the phase-5 harness against a period-appropriate dropbear instead of
2022.83. Two things it has to measure first — whether an old dropbear even
builds on a modern toolchain, and whether `ssh_options` really suffices.

### busybox-over-a-real-network

**Status:** `untested` — otto attempts it, and the outcome is the measurement.

No BusyBox target is exercised over a real network path. Every tier is local:
Tier 1 runs the artifact as a subprocess, and Tiers 2 and 3 run it inside an
unprivileged namespace on loopback. Loopback has a ~64 KB MTU and no real
latency, so nothing measured so far can surface an interaction between the
transfer's chunking and a real path's MTU, window or timeouts.

**Measured:** nothing yet, by construction — this row exists to say that the
green Tier 1-3 lanes do not cover it.

**Queued for:** Tier 3 fidelity item B, `todo/busybox-tier3-fidelity-2026-08-13.md`:
let the harness aim at a remote host when one is configured, defaulting to
loopback.

## Keeping this page true

`tests/unit/test_docs_gap_sync.py` reads {data}`~otto.host.userland.GAPS` and
this page's table and asserts, in both directions, that they describe the same
set of surfaces in the same order with the same status — plus that every row
links to a section this page actually has, since those anchors are what the
runtime error's `See ...` line points at.

What that test does **not** check is the prose: no assertion can tell whether a
`measured_on` string is true, and pinning paragraphs verbatim would only add a
copying ritual. The structure is compulsory and the wording is review's job. So
when a record changes:

- adding a gap means adding a table row **and** a section, or the sync test
  reddens;
- closing one means deleting both;
- changing a status means changing it in the row and in the section's
  **Status:** line;
- the page's location is not written here twice — it is `GAP_DOCS_PAGE` in
  `src/otto/host/userland.py`, which every rendered error message and this test
  both read, so moving the page is one edit.
