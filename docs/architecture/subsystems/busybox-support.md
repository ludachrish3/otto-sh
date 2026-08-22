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
**Division of labour with the {doc}`BusyBox matrix page <busybox-bed>`,
so the two do not drift.** That page is about *running otto's BusyBox harness*: the
prerequisite for executing the artifacts (`qemu-user-static`, off x86_64), which
upstream artifacts are pinned and why, the five live guests and how to provision,
recover and read the logs of them, which lane runs which half, and the trust note for
executing an unsigned third-party binary. This page is about *otto's behaviour against
such a device*. Facts about the test harness live there; facts about what otto can and
cannot do live here. Neither restates the other.
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
{func}`~otto.host.userland.refuse_if_gapped`, and only some of the call sites
that reach these surfaces consult it — so for some surfaces below the refusal is
what otto does, and for the rest it is still what a wired call site would do.
Each section's **Status:** line says which, and its **Paths** list says it per
call site, which is the finer-grained answer: a surface can be guarded where
`Host.run()` reaches it and unguarded where a named session does.

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
**A surface is a fact about your device; a path is a place otto touches it.** One
measurement, several call sites — and otto reaches most of these surfaces from
more than one. Wiring one call site leaves the others exactly as broken, so
"which surfaces refuse" is the wrong question and every section below answers the
right one instead, per call site, in its **Paths** list.

For the surfaces with no wired path, wiring one is a behaviour change that
belongs with the call site being changed, and it has not happened — read those
rows as "what otto knows", and each `reason` and **Status:** line as what such a
refusal *would* say and do.

Not every refusal in otto is this table's. `PosixPrivilege._elevate` and
`ShellFileTransfer._run_put`/`_run_get` are *probe-driven*: they refuse on what
the host in front of them answered *and* print their own message, which is a
different thing from "otto measured this on the matrix". That is why
[`shell-transfer-base64`](#shell-transfer-base64) picks a codec, and then
refuses before sending if it has none, while reading nothing from this record —
it gets there by probing `base64_flag` and the applet list — and
it is what the `PROBE_REFUSED` state below records. `read_file`/`write_file` are
the ones that mix the two, and the split is worth keeping straight: their
*predicate* is that same probe, while their *verdict* and their *message* are the
record's, so the probe decides only that this host is in the measured class.
```

## Where otto consults this table

Each `measured-broken` section below lists the call sites otto reaches that
surface from, with one of six states. **This is the answer to "am I protected",
and the surface's status is not** — a surface can refuse on one path and truncate
silently on another.

`ADAPTED`
: otto asks your device and then does the thing it *can* do, so the operation
  **succeeds**. Not a refusal and not a hole: the only state where the
  measurement behind the record no longer costs you anything. The record stays
  on this page because the device limitation is still real and because a device
  that can do neither thing is still refused — from the record — rather than
  told it worked.

`WIRED`
: this call site reads the record and refuses from it. The message you get *is*
  the record — the reason, the measurement, the queue entry and a link back to
  this page. Downgrading the record to `untested` stops the refusal, which is
  what makes this table the authority here.

`PROBE_REFUSED`
: otto refuses before it sends anything, but on its own authority rather than
  this table's: the call site asks the device, raises itself, and writes its own
  message. **You are protected here, and this page is a record of the surface
  rather than the thing deciding it** — downgrading the record would not stop the
  refusal, and the message carries none of the evidence above.

`PROTECTED`
: this call site cannot reach the gapped operation at all, because something
  upstream refuses your host first. Not a hole, and not a place to add a guard:
  a guard there could never fire.

`ATTRIBUTED`
: otto attempts it, your device refuses it, and **the failure you get is this
  record** rather than a library's internal. Nothing is prevented here: the
  operation fails exactly as it did before, in the same time, having moved
  nothing. What changes is that the message names the surface, the measurement
  and what to do instead. This state exists for surfaces where **no pre-check is
  possible without refusing hosts that work** — read
  [`sftp-transfer`](#sftp-transfer), the only one today, for the argument.

`OPEN`
: reachable, unguarded, **still broken as described**. These are the holes, and
  they are listed here so they are visible to you and not only to the table.

Counts, derived from the records themselves rather than maintained by hand
({func}`~otto.host.userland.gap_path_totals`), and pinned to them by
`tests/unit/test_docs_gap_sync.py`:

| Path state | Paths |
| --- | --- |
| `ADAPTED` | 1 |
| `WIRED` | 7 |
| `PROBE_REFUSED` | 2 |
| `PROTECTED` | 2 |
| `ATTRIBUTED` | 2 |
| `OPEN` | 5 |

The `WIRED`, `ADAPTED` and `ATTRIBUTED` paths — the ones whose message is this
table's — reach {func}`~otto.host.userland.refuse_if_gapped`
through **7** guard functions ({func}`~otto.host.userland.table_guards`), which
is fewer than the ten such paths above — `read_file`/`write_file` share one
guard, and the scp and sftp backends each share one across both directions — so
the two numbers are different on purpose and both are derived.

## The declared gaps

One row per record, in the table's own order. The full text of each record —
the reason, the measurement and the queue entry — is in `GAPS`, and the runtime
error prints it verbatim; the sections below are its readable form.

| Surface | Status | What it means for you |
| --- | --- | --- |
| [`shell-transfer-base64`](#shell-transfer-base64) | `measured-broken` | A device with no `base64` applet gets the `shell` backend's `uuencode` codec instead; only a device with neither is refused. |
| [`file-ops-base64`](#file-ops-base64) | `measured-broken` | `read_file` and `write_file` are refused on those same devices, rather than blaming the file for the missing applet — and, for a write, emptying it. |
| [`sftp-transfer`](#sftp-transfer) | `measured-broken` | No sftp subsystem on a stock BusyBox device. Use the `shell` backend. |
| [`scp-transfer`](#scp-transfer) | `measured-broken` | No `scp` binary on a stock BusyBox device, so otto refuses the transfer up front rather than failing one file at a time. A device with a real `scp` installed is unaffected. Use the `shell` backend. |
| [`nc-transfer`](#nc-transfer) | `measured-broken` | The `nc` backend cannot drive BusyBox's own `nc` applet: a GET is refused up front on a device whose `nc` rejects `-N`, a PUT is not yet. A real netcat installed alongside is fine, and is not refused. |
| [`daemon-launch`](#daemon-launch) | `measured-broken` | Launching a tagged daemon needs bash, which a stock BusyBox userland does not have, so `link impair --expire` is refused rather than left with a timer that never runs. Impair without `--expire` and it works. |
| [`shutdown-command`](#shutdown-command) | `measured-broken` | Nothing, on any measured device: `Host.shutdown()` asks which spelling your device has and emits `poweroff` where there is no `shutdown`. Only a device with neither is refused. `Host.reboot()` is unaffected. |
| [`run-command-line-length`](#run-command-line-length) | `measured-broken` | `Host.run()` refuses a command whose typed line would exceed 1022 characters, rather than let ash truncate it. `Host.exec()` is safe and is not refused. |
| [`product-lifecycle`](#product-lifecycle) | `untested` | otto's `stage`/`install`/`uninstall` verbs emit no command of their own. Whether they work on your device is decided by your own product code. |
| [`legacy-dropbear-crypto`](#legacy-dropbear-crypto) | `untested` | An old dropbear may need `ssh_options` to negotiate at all. Nobody has tried. |
| [`busybox-over-a-real-network`](#busybox-over-a-real-network) | `untested` | The last leg to every target is local or emulated, so nothing has met a real path's MTU, latency or window. |

### shell-transfer-base64

**Status:** `measured-broken` — the *applet* really is missing on 1.16.1, but
since the `uuencode` codec landed that no longer stops the backend. otto probes
the device, picks the codec it can actually run, and only refuses when it can
run neither. Both the fallback and the refusal are decided at the call site from
`Userland`, not read from this table, which is what the `PROBE_REFUSED` paths
below mean: you are protected, and this page is the record rather than the
authority.

**Paths otto touches this from:**

- `otto.host.transfer.shell.ShellFileTransfer._run_put` — **PROBE_REFUSED**:
  degrades first, refuses second, both with its own message. On a *settled*
  `base64_flag` of `absent` it switches to `uudecode` and the transfer happens;
  it refuses only when `uudecode` is measured absent too, or when the probe round
  never arrived at all — a probe that could not be asked does not get to choose a
  codec.
- `otto.host.transfer.shell.ShellFileTransfer._run_get` — **PROBE_REFUSED**: the
  same choice in the other direction, after GET's own size-probe check, reading
  `uuencode` rather than `uudecode` — the device only *encodes* for a GET, and the
  two are separate applets.

Wiring either of these means *moving* the verdict onto the record, never adding a
second refusal beside the one already there.

The `shell` transfer backend prefers the device's own `base64`, because that is
the cheaper shape on the wire: one command per chunk, one line. A userland
without that applet gets `uuencode`/`uudecode` instead — one command per chunk
plus a scratch file the same command removes — which is present on every BusyBox
row in this matrix, 1.16.1 included. Only a device with *neither* is refused, and
then nothing is attempted, because every file in the batch would fail
identically.

**Measured:** BusyBox 1.16.1 ships no `base64` applet.
`tests/integration/busybox_bed/test_applet_userland.py` records `False` for that
row and `tests/integration/busybox_bed/test_shell_codec.py` records a `None`
decode flag, while 1.21.1 and every later matrix row decode with `-d`. The same
module puts and gets a multi-chunk payload through each guest's own codec — uu
on 1.16.1, base64 on the rest — and compares the bytes that come back.

**Queued for:** nothing. The *pty* path that used to be queued here is measured:
a `term: telnet` BusyBox host routes this backend through a pooled shell session
whose line editor truncates at 1022 characters (see
[`run-command-line-length`](#run-command-line-length)), so PUT now **sizes its
chunk lines to that budget**, and multi-chunk round trips run over telnet on all
five guests — uuencode on 1.16.1, base64 on the rest.

### file-ops-base64

**Status:** `measured-broken` — and this is a surface otto actually refuses *from
this table*, on the hosts that build a userland. `Host.read_file` and
`Host.write_file` read the record through
{func}`~otto.host.userland.refuse_if_gapped` and decline the operation. Other host
families reach the same two methods and are never refused; the paths say which.

**Paths otto touches this from:**

- `otto.host.file_ops.PosixFileOps.read_file` — **WIRED** by
  {func}`~otto.host.file_ops.refuse_if_base64_is_absent`: declines before it
  emits `base64 <path>`.
- `otto.host.file_ops.PosixFileOps.write_file` — **WIRED** by the same guard, and
  the more valuable of the two: the command it declines to emit is *destructive*
  on exactly the device that cannot run it.
- `otto.host.local_host.LocalHost._userland` — **OPEN**: `LocalHost` never builds
  a `Userland`, so the guard returns on its `None` arm before it reads this
  record. A local shell with no `base64` still gets the failure described below.
- `otto.host.docker_host.DockerContainerHost._userland` — **OPEN**: the same
  `None` arm, and the sharper case — an `alpine` container *is* a BusyBox
  userland, and otto will never refuse it.

Both open paths close by giving the host a resolver, not by widening the guard —
and both stay open because that resolver was **measured** (2026-08-14) to change
more than this surface. `_userland()` is one hook read by two mixins, and
`resolve()` has no scoped form, so a resolver added here also decides how
`run(sudo=True)` elevates on the same host: on the shape `alpine` actually has
(BusyBox 1.36.1, `/bin/su`, no `sudo`) it moves the built command from
`sudo -S -p …` to `su -c …`, and on a host with neither applet it *raises* where
today the caller gets a non-ok result. See
{class}`~otto.host.userland.UserlandHost` for the three findings and
`todo/busybox-phase-5-followups-2026-08-13.md` §2 for the decomposition that
would make it safe.

**What is *not* at risk while they stay open:** an ordinary `alpine` container has
`base64` (measured on `alpine:3.20`; the matrix records the applet missing on
1.16.1 alone), so `read_file`/`write_file` work there. The exposure is an image
or a local machine with the applet compiled out, and on a *write* that is the
destructive case described below.

Both move their payload through the device's `base64`, and unlike the `shell`
transfer they hard-code it: `file_ops.py` emits `base64 <path>` and
`... | base64 -d` whatever `Userland.base64_flag` says. They cannot *adapt*, so
otto refuses them instead.

**What the refusal replaced was a failure that blamed the wrong thing** —
and, on a write, destroyed a file. `read_file` turned the device's own
`base64: not found` into a `FileNotFoundError` naming the caller's path, sending
them to look for a file the device has. `write_file` was worse than misleading:
the shell opens `> <path>` before it resolves `base64`, so an overwriting write
emptied the destination and *then* reported failure. Refusing leaves the file
exactly as it was found. An `append=True` write builds `>>`, which never
truncated, and is refused on the same terms — it would still write nothing.

**Which hosts are refused.** Those whose userland *settled* `base64_flag` on
`absent` — the device answered both decode probes, or the lab entry declared it
in `userland_options`. Unlike the other two refusals this one is **probed**, so
the first `read_file`/`write_file` on a host pays one userland resolution
(cached per host object thereafter, and up to 30s on a host that will not answer
at all — see {meth}`~otto.host.userland.Userland.resolve`). There is no declared
`base64` fact to key on instead, and the `busybox` os_profile deliberately
declares none, because a declaration would skip the probe and a wrong guess
would be unfixable from the device.

A host whose probe round never *arrived* is **not** refused, even though
`base64_flag` reads `absent` for it too: that value is what otto assumes before
it has asked anything, and treating it as a measurement would turn a refused ssh
channel into a verdict about the device's applets. Such a host attempts the
operation exactly as it did before. Neither is a host with no resolver at all —
`LocalHost` and a docker container host never build one.

**What is not refused.** Only these two methods. `put`/`get` choose a transfer
backend and are covered by [`shell-transfer-base64`](#shell-transfer-base64),
and the other file operations — `exists`, `ls`, `mkdir`, `rm`, `cp`, `mv` — need
nothing but a shell and are untouched.

**Measured:** the same 1.16.1 rows as [`shell-transfer-base64`](#shell-transfer-base64),
against two call sites in `src/otto/host/file_ops.py` that still emit one fixed
spelling each. The destructive half was measured directly, 2026-08-14, on the
1.16.1 artifact's own ash with `PATH` blocked: `echo aGk= | base64 -d > <file>`
against a 17-byte file answered `sh: base64: not found` and left that file at
0 bytes, while `>>` left it intact.

**Queued for:** the refusal has landed; a *fix* is still the full-parity
workstream's, with the entry above — the codec SELECTION that landed there
(`_select_codec`, which degrades to `uuencode` where `base64` is absent) is what
these two would need to read, so the two are one change and not two. The record
stays `measured-broken` because the surface still is: otto now declines the
operation instead of emitting one it cannot run.

### sftp-transfer

**Status:** `measured-broken` — and otto **does not refuse this up front, on
purpose**. It attempts the transfer, and when the subsystem does not start it
tells you so in this record's words instead of asyncssh's. Read the paragraph
after the paths for why a pre-check would be worse than none.

**Paths otto touches this from:**

- `otto.host.transfer.sftp.SftpFileTransfer._run_get` — **ATTRIBUTED** by
  {func}`~otto.host.transfer.sftp.open_sftp_or_attribute`: opens the subsystem,
  and if it closes before the SFTP handshake, raises this record with asyncssh's
  own error chained beneath it. The transfer still fails; only the message
  changes.
- `otto.host.transfer.sftp.SftpFileTransfer._run_put` — **ATTRIBUTED** by the
  same guard, charged once per `put()` rather than once per file because it sits
  above the per-file fan-out. That position is also why no file is ever blamed
  for the missing subsystem.

The `sftp` transfer backend needs a server-side sftp subsystem, and a stock
BusyBox userland ships none. Note what does *not* decide this: the ssh daemon.
Packaged dropbear serves sftp perfectly well when the machine provides an
`sftp-server` binary, so the question is what the **device** has, not which
daemon answered. Use the `shell` backend.

**Why there is no pre-emptive refusal here, unlike every other surface on this
page.** Nothing otto can ask before the operation distinguishes a device that
serves sftp from one that does not. `sftp-server` is not on `PATH` even on a
healthy Debian host (it lives at `/usr/lib/openssh/sftp-server`), so a
presence probe answers "absent" where sftp works. The absolute path differs
across distros and dropbear compiles it in rather than reading a config, so a
known-paths list fails on the first unusual device. And the daemon is not the
authority either. Every available pre-check therefore produces false absents —
refusals of hosts that work — which is the one error this table is ordered to
avoid, and the reason the `busybox` profile keeps `sftp` in its
`valid_transfers`. The only definitive test is opening the subsystem, which
*is* the operation. So the operation runs, and the record improves its failure
rather than preventing it.

**Measured:** twice, on the same device. The dropbear rig, 2026-08-13 — an
`sftp(1)`
session into the pinned BusyBox root fails with
`/bin/sh: /usr/lib/sftp-server: not found`, ash inside the chroot rather than
the host's shell. Then otto's own backend against that same dropbear rig, 2026-08-14 —
`UnixHost.put` on a host built with `transfer: sftp` raised
`asyncssh.sftp.SFTPConnectionLost: 0 bytes read on a total of 4 expected bytes`
in 22ms, moved no bytes and left nothing behind on either side. That message is
what this record now replaces: it names no subsystem, no device and no
alternative, and it reads like a dropped connection.

**Queued for:** nothing for a fix, deliberately — the `shell` backend is the
answer for these devices, and it is verified end to end on five live BusyBox
guests (`tests/integration/busybox_bed/test_shell_codec.py`). That verification
originally ran over real ssh against a dropbear rig that has since been retired;
the guests carry it over telnet instead. What landed instead of a fix is the
attribution above.

### scp-transfer

**Status:** `measured-broken` — and otto **refuses before it sends anything**,
on both directions, when your device answered that it has no `scp`. A device
that has one, or one otto could not ask, transfers exactly as it did before.

**Paths otto touches this from:**

- `otto.host.transfer.scp.ScpFileTransfer._run_get` — **WIRED** by
  {func}`~otto.host.transfer.scp.refuse_if_scp_is_absent`: resolves the userland
  and declines before the connection is opened, rather than letting asyncssh
  report the missing binary once per file.
- `otto.host.transfer.scp.ScpFileTransfer._run_put` — **WIRED** by the same
  guard, in the other direction. Two sites, one guard — and it is charged once
  per `put()`/`get()` rather than once per file, because it sits above the
  per-file fan-out.

The legacy `scp` protocol needs an `scp` binary on the far side, and a stock
BusyBox userland has none. Same caveat as [`sftp-transfer`](#sftp-transfer): the
daemon is not the authority, the device's userland is. Use the `shell` backend.

Three things worth knowing if you operate one of these devices. **The refusal is
about your device and never about its profile:** the `busybox` os_profile keeps
`scp` in `valid_transfers` deliberately, so a BusyBox box with a real `scp`
installed alongside can still pin `transfer: scp` and it keeps working — only
the applet answer decides. **A probe round otto could not get an answer to is
not a refusal:** the guard asks `is_settled` first, so an sshd at its
`MaxSessions` ceiling cannot be mistaken for a device with no `scp`. And **this
is the one guard on this page that adds a probe round to a path that had none** —
an scp transfer resolved nothing before, so the first `put`/`get` against a host
now pays one applet batch. It is cached on the host for every later transfer,
and pinning `applet_scp` in that host's `userland_options` (`otto host <id>
probe` prints the payload) removes it entirely.

Unlike [`shutdown-command`](#shutdown-command), there is nothing here to adapt
to: `ScpOptions` carries no binary-name override, and the name the far side runs
is the protocol's rather than otto's — which is also what separates this from
[`nc-transfer`](#nc-transfer), where `NcOptions.exec_name` means the presence of
*an* `nc` is not the question.

**Measured:** two measurements, and the second is what the refusal keys on.
The dropbear rig, 2026-08-13: `scp -O` into the pinned BusyBox root fails with
`/bin/sh: scp: not found`, and the file does not land — measured separately from
`sftp-transfer` on purpose, since `scp -O` reaches for a remote binary while
`sftp` opens a subsystem. Then the five matrix artifacts through the batched
applet probe, 2026-08-14: `scp` is absent from the applet list on all five
(`tests/integration/busybox_bed/test_applet_userland.py` records it per row).

**Queued for:** nothing for a *fix*, deliberately, for the same reason as
`sftp-transfer` — the `shell` backend is the answer for these devices. The
refusal has landed. The record stays `measured-broken` because the surface still
is: otto now declines the transfer instead of attempting one the device cannot
serve.

### nc-transfer

**Status:** `measured-broken` — and the GET direction now refuses *from this
table*, on a device whose own `nc` was measured to reject the option otto sends
with. The PUT direction still does not, deliberately: read on.

**Paths otto touches this from:**

- `otto.host.transfer.nc.NcFileTransfer._get_files_nc` — **WIRED** by
  `otto.host.transfer.nc.refuse_if_nc_rejects_dash_n`: asks the device to
  send with `nc -N`, the option every matrix row rejects outright, and now
  declines before it binds a local server or spawns anything.
- `otto.host.transfer.nc.NcFileTransfer._get_files_nc_tunneled` — **PROTECTED**
  by that same guard: the hop-tunnelled GET spawns `nc -Nl <port>` — both
  rejected spellings in one option string — but `_get_files_nc` is its only
  caller and refuses *above* the tunnel dispatch, so on a refused device this is
  never entered. Not a hole: a guard here could never fire.
- `otto.host.transfer.nc.NcFileTransfer._put_files_nc` — **OPEN**: spawns the
  device-side listener as `nc -l -w <secs> <port>`, which the applet does not
  accept, so nothing binds and otto waits for a peer that cannot arrive — a
  timeout rather than the refusal this record describes.

**Why the PUT is still open now the GET is not.** That command carries no `-N`
at all; it is broken on BusyBox for a *different* reason — the applet spells a
listener `-l -p PORT`. The two facts coincide on every matrix row and are still
two facts, and refusing the PUT on the `-N` answer would decline a transfer this
measurement says nothing about. Settling the right fact would mean asking your
device to *bind a port*, which is a probe with a side effect on the host it is
asking about.

The `nc` transfer backend cannot drive BusyBox's own `nc` **applet**: it sends
with `nc -N <ip> <port>` and listens OpenBSD-style with `nc -l <port>`, and the
applet supports neither spelling. A BusyBox device with a real OpenBSD netcat
installed alongside is fine — point `NcOptions.exec_name` at it — so this is a
gap in the applet, not in every BusyBox host. **The refusal respects that**: it
keys on the binary otto would actually exec, so a host whose `exec_name` is
anything but `nc` is never refused from this record, whatever the device
answered about its own `nc`.

**Measured:** two measurements of the same option, and the second is what the
refusal keys on. The five matrix artifacts, 2026-08-13: `nc -N 127.0.0.1 1` is
rejected on every row (`nc: invalid option -- N` on the two oldest,
`nc: unrecognized option: N` on the rest), and every row's own usage line spells
the listener `nc [OPTIONS] -l -p PORT`. That one *connects*, so no call site can
issue it. Then, 2026-08-14, the same five rows through the probe a call site
*can* issue — {attr}`~otto.host.userland.Userland.nc_dash_n`, which compares a
destination-less `nc` against `nc -N` and touches no socket: all five answer
`rejected`, while a real OpenBSD netcat (1.226) answers `supported`, and answers
`rejected` for a `-Q` control it genuinely lacks. That last row is what makes
this an option test rather than a "does this look like BusyBox" test.

**Pin it and skip the probe:** `otto host <id> probe` prints `nc_dash_n` with
everything else, and a value pinned into `userland_options` is never re-probed.
Re-take it if you change `NcOptions.exec_name`, since the answer is about the
name `nc`.

**Queued for:** the full-parity workstream, `todo/busybox-parity-sweep-2026-08-11.md`,
which queues a BusyBox `nc` variant (`-l -p PORT`, size-terminated reads to
replace the missing `-N`). The refusal has landed for the GET direction; a fix
for either direction has not.

### daemon-launch

**Status:** `measured-broken` — and this is a surface otto actually refuses *from
this table*. `otto link impair --expire` reads the record through
{func}`~otto.host.userland.refuse_if_gapped` and declines to launch the timer.
Every path otto reaches this surface from is accounted for: one refuses from the
record, and the other cannot be reached at all.

**Paths otto touches this from:**

- `otto.link.manage._launch_daemon` — **WIRED** by
  {func}`~otto.host.daemon.refuse_if_launch_wrapper_needs_bash`: the only path in
  `otto.link` that reaches `launch_command`, shared by both expire-timer
  flavours, so the refusal cannot be bypassed by adding a third launch.
- `otto.tunnel.manage.add_tunnel` — **PROTECTED** by
  `otto.tunnel.manage._validate_chain_shape`, which rejects a `has_bash=False`
  host as a tunnel path member before any launch is planned — on the real path
  (via `_resolve_chain`) and under `--dry-run` (via `_planned_chain`, which
  reaches no device and launches nothing) alike. Not a hole: a guard here could
  never fire.

`otto.host.daemon.launch_command` wraps every daemon in a `setsid bash -c` that
re-`exec`s it under a findable `argv[0]`, and a stock BusyBox userland has no
bash. The wrapper body is not portable to ash either, so this is not a
`bash`→`sh` substitution: it needs a different `argv[0]` mechanism.

**What the refusal replaced was silence, not a loud failure.** `otto.link`
launches a detached timer to clear an impairment after `--expire` seconds, and
its `_root_run` helper deliberately does not raise on a non-ok result — a qdisc
mutation's failure is caught by the caller's own re-read instead — and nothing
re-reads after a timer launch. So the device's `bash: not found` came back, was
discarded, and `impair` reported **success** for an impairment whose timer did
not exist and which therefore never expired. Now the impair is refused, and the
link is left as it was found.

**What is not refused.** Only the daemon. `tc` needs no bash, so an impair
*without* `--expire` works normally on the same device — clear it yourself with
`otto link repair` when you are done. `repair` is also unaffected: it cancels
timers with a `ps` scan and `kill`, and never launches one.

**Which hosts are refused.** Those declaring `has_bash=False` — the `busybox`
os_profile sets it, and any `unix` lab entry may set it directly. It is the
*absence of bash* that matters, not BusyBox specifically: a dash-only host
cannot run this wrapper either and is refused for the same reason. Nothing is
probed to decide this, so the refusal costs no connection.

**otto's other tagged-daemon launch, tunnels, is not a second raise site** and
does not need one: `otto.tunnel.manage._validate_chain_shape` already refuses a
`has_bash=False` host as a tunnel path member, before any launch is planned,
because tunnel discovery and removal scan only `has_bash` hosts and would
otherwise leak un-reapable processes. That refusal is loud and predates this
one. It is named at `_validate_chain_shape` rather than at a caller because
`add_tunnel` reaches it two ways — `_resolve_chain` on the real path,
`_planned_chain` under `--dry-run` — and only the shared callee is true of
both.

**Measured:** the five matrix artifacts, 2026-08-13, running the wrapper body
under each row's own ash. The two oldest have no `exec -a` and answer
`ash: exec: line 1: -a: not found`. The three newest *do* parse `exec -a` and
then mis-expand `"${@:2}"` into a substring of `$1`, so the launch execs
`NTINEL` instead of `SENTINEL`. The naive fix trades a clean `not found` for a
corrupted program name.

**Queued for:** the refusal has landed; a *fix* has not, and is not written up
in the queue file — this table is the record, and the queue file carries the plan
for work that has one. A fix is a portable `argv[0]` mechanism, which is a
design question rather than a spelling change, and until one exists the record
stays `measured-broken`, because the surface still is.

### shutdown-command

**Status:** `measured-broken` — about the *device*, not about what otto does.
**otto shuts a BusyBox host down.** It asks the device which spelling it has and
emits that, so no host on any matrix row is refused here; the record stays
because `shutdown` really is absent on all five, and because a device that has
*neither* spelling still has to be told so rather than reported as powered off.

**Paths otto touches this from:**

- `otto.host.unix_host.UnixHost.shutdown` — **ADAPTED** by
  {func}`~otto.host.unix_host.shutdown_command`: resolves the userland, emits
  `shutdown -h now` where the device has that applet and `poweroff` where it does
  not. Every matrix row takes the second arm and powers off. Only a device that
  answered `absent` to both names is refused, from this record.
  `Host.reboot()` is a *different* surface and not a path of this record.

`Host.shutdown()` used to emit `shutdown -h now` unconditionally, and BusyBox has
no `shutdown` applet; the BusyBox spelling is `poweroff`. The choice is a
userland probe — the `applet_shutdown` and `applet_poweroff` capabilities, in the
pattern `Userland.timeout_style` set — rather than a hard-coded swap that would
break every GNU host. **`Host.reboot()` is not affected** and must not be lumped
in with this — `reboot` is present on every matrix row and otto's soft reboot
works as shipped, unchanged.

Two details worth knowing if you operate one of these devices. Picking the
spelling reads the probe's *value* alone, so a host whose probe round never
arrived gets `shutdown -h now` — exactly what otto sent before this existed —
rather than a refusal; the refusal for a device with neither name asks
`is_settled` first, so an sshd that refused otto an exec channel cannot be
mistaken for a device with no way to power off. And `shutdown()` now *reports*
what the device answered: a completed round trip that exits non-zero (sudo
denied, say) comes back `Failed` instead of `Success`. A dropped connection
still counts as success, because issuing a power-off command races the transport
being torn down and that is not evidence the device disobeyed.

**Measured:** the five matrix artifacts, 2026-08-13, re-measured through the
batched applet probe 2026-08-14. `shutdown` is absent from the applet list on all
five, while `reboot` and `poweroff` are present on all five
(`tests/integration/busybox_bed/test_applet_userland.py` records it per row).
BusyBox runs only
what its own applet list carries, so the list is the whole answer here — and it
is also why the choice always has somewhere to go.

**Queued for:** nothing. The fix has landed in
{func}`~otto.host.unix_host.shutdown_command`, the first place this registry
answers a measurement by adapting rather than declining. The record stays
`measured-broken` because the device still is.

### run-command-line-length

**Status:** `measured-broken` — and this is a surface otto actually refuses *from
this table*, on the path `Host.run()` takes. `Host.run()` reads the record through
{func}`~otto.host.userland.refuse_if_gapped` and raises before it types anything,
so nothing is attempted and no connection is opened. **Other paths reach the same
line editor and are still open**, deliberately; the paths list says which, and
"What is not refused" below says why they must stay that way for now.

**Paths otto touches this from:**

- `otto.host.session.SessionManager.run_cmd` — **WIRED** by
  {func}`~otto.host.session.refuse_if_line_editor_would_truncate`: the per-command
  path of `Host.run()` for every host family. Keys on the *declared* dialect, sizes
  the line otto would *type* including its own framing, and refuses before any
  session is opened.
- `otto.host.session.HostSession.run` — **OPEN**: a named session's `run()` calls
  `ShellSession.run_cmd` directly, one layer below the guard, so an over-long
  typed line is still silently truncated there.
- `otto.host.session.SessionManager.exec` — **OPEN**: on a `term: telnet` host,
  and on *any* host whose login is proxied, `exec()` has no stateless primitive
  and routes through a pooled shell session — so it is line-edited like a typed
  command, and the escape hatch below does not hold on those two host shapes.

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

**Measured:** two measurements, both on rigs of their day. First the phase-5
spike, 2026-08-13, over the since-retired dropbear rig — dropbear 2022.83 in
front of a BusyBox 1.35.0 chroot: largest line delivered intact 1022, first
truncated 1023, with no error and no log line — identical against OpenSSH and
against a bare local pty, which is what identifies it as BusyBox ash's
`CONFIG_FEATURE_EDITING_MAX_LEN` rather than any transport, while the exec
channel took 9000 characters intact and broke at 9001. (That rig is gone; the
bound it found is not a property of it, which is the point of the second
measurement.) Then, because that
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
`await host.put(...)` — and `put` is a surface this table already covers and the
five live BusyBox guests already exercise end to end.

Everything else that would reach the device comes from **your** product code.
The documented shape (see {doc}`../../guide/cli/host/capabilities/index`) has `install`
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
measure a period-appropriate dropbear instead of 2022.83. Note what moved under
that item: the phase-5 harness it named has been retired, and the live BusyBox
guests that replaced it run no ssh daemon at all, so closing this now needs a rig
of its own. Two things it has to measure first — whether an old dropbear even
builds on a modern toolchain, and whether `ssh_options` really suffices.

### busybox-over-a-real-network

**Status:** `untested` — otto attempts it, and the outcome is the measurement.

No BusyBox target is exercised over a real network path. The artifact tier runs
the binary as a local subprocess, and the live BusyBox guests answer through
QEMU's user-mode networking behind a hop — so the last leg to the device is
emulated, with no real latency, MTU or loss on it. Nothing measured so far can
surface an interaction between the transfer's chunking and a real path's MTU,
window or timeouts.

**Measured:** nothing yet, by construction — this row exists to say that the
green lanes do not cover it.

**Queued for:** Tier 3 fidelity item B, `todo/busybox-tier3-fidelity-2026-08-13.md`:
aim a BusyBox target across a physical link. The harness that item would have
extended is retired, and the bed that replaced it moved the target only part of
the way — otto reaches the guests over the lab network, but the last hop into
each one is emulated.

## Keeping this page true

`tests/unit/test_docs_gap_sync.py` reads {data}`~otto.host.userland.GAPS` and
this page's table and asserts, in both directions, that they describe the same
set of surfaces in the same order with the same status — plus that every row
links to a section this page actually has, since those anchors are what the
runtime error's `See ...` line points at.

**The paths are pinned in both directions too**, and the `OPEN` ones especially:
every open path in the registry has to appear in its section's **Paths** list, so
a hole cannot be recorded in the source and left off the page a reader is sent
to; and every path on this page has to be a declared one, in the state the record
declares, so this page cannot invent a hole or report a closed one as open. The
counts under "Where otto consults this table" are pinned to
{func}`~otto.host.userland.gap_path_totals` and
{func}`~otto.host.userland.table_guards`, so **no number on this page is
maintained by hand** — that is the whole reason the paths exist as data rather
than prose.

What that test does **not** check is the prose: no assertion can tell whether a
`measured_on` string is true, and pinning paragraphs verbatim would only add a
copying ritual. The structure is compulsory and the wording is review's job. So
when a record changes:

- adding a gap means adding a table row **and** a section, or the sync test
  reddens;
- closing one means deleting both;
- changing a status means changing it in the row and in the section's
  **Status:** line;
- adding or re-stating a path means changing the record's `paths` **and** the
  section's **Paths** bullet, and the count table if the totals moved;
- the page's location is not written here twice — it is `GAP_DOCS_PAGE` in
  `src/otto/host/userland.py`, which every rendered error message and this test
  both read, so moving the page is one edit.
