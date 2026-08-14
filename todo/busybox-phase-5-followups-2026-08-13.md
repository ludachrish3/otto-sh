# BusyBox phase 5 follow-ups

Open items left by phase 5 (`4f93b756` — Tier 3 over real ssh, the gap registry,
its docs page and the docs-sync test). Everything here was found by measurement
while building or reviewing that branch, and each entry carries its evidence so
the work starts from a fact rather than a fresh survey.

Two queues already exist and are **not** duplicated here:

- `todo/busybox-tier3-fidelity-2026-08-13.md` — legacy dropbear on loopback, a
  parametrizable Tier 3 endpoint, and the two untested gaps. Agreed for after
  phase 5.
- `todo/busybox-parity-sweep-2026-08-11.md` — the `uuencode` codec for 1.16.1
  and a BusyBox `nc` backend. The separate full-parity workstream.

---

## 1. The registry renders messages that almost nothing invokes

**Status: three of eight surfaces wired. Five open.**

`Gap`, `GAPS`, `gap_for()`, `refuse_if_gapped()` and
`UnsupportedOnUserlandError.for_gap()` all exist and are tested. Phase 5 left
them with **no product call site at all**; three now consult the table:

1. `run-command-line-length` —
   `otto.host.session.refuse_if_line_editor_would_truncate`, called from
   `SessionManager.run_cmd` (see §2 below).
2. `daemon-launch` — `otto.host.daemon.refuse_if_launch_wrapper_needs_bash`,
   called from `otto.link.manage._launch_daemon`, the single path in `otto.link`
   that reaches `launch_command`. Keyed on a declared `has_bash=False`. See §2
   below for what it replaced, which was a **silent** failure rather than a loud
   one.
3. `file-ops-base64` — `otto.host.file_ops.refuse_if_base64_is_absent`, called
   from both `PosixFileOps.read_file` and `PosixFileOps.write_file`. Keyed on a
   **probed** fact, which is what makes it different from the first two: see §2
   below for the cost that predicate carries and why it was accepted here.

The other five measured-broken surfaces are unchanged, and
`shell-transfer-base64` still refuses only incidentally, because `_run_put`
probes `base64_flag` rather than reading this table. **Since the `uuencode`
codec landed, that site now DEGRADES before it refuses** — a settled `absent`
selects uu instead of declining — but the standing is the same: the verdict and
the message are the call site's, not this table's.

The shape all three settled, and the one to copy: the **caller** decides that
this host belongs to the measured class (a declared shell dialect of `ash`; a
declared `has_bash=False`; a settled `base64_flag == "absent"`), and the
**table** decides whether that class is refused at all — so downgrading the
record to `untested` stops the refusal, which is asserted in all three cases.
Neither half is enough alone.

**A probed predicate does not make the refusal probe-driven.** The third
surface's verdict and message are still the record's; the probe only decides
membership of the measured class. Worth stating because the distinction the
docs page draws between this table and the two probe-driven refusals
(`_elevate`, `ShellFileTransfer`) would otherwise read as broken.

**Reachability is the thing to establish before writing the guard**, not after.
`daemon-launch` had three `launch_command` call sites and only two of them were
reachable with a `has_bash=False` host: `otto.tunnel.manage.add_tunnel`'s socat
launch is already unreachable because `_resolve_chain` rejects such a host as a
tunnel path member first, so it got **no guard**. A guard at a site nothing
reaches cannot fail, which is this repo's most common defect; the reachable two
are each pinned end to end by a test that arrives at the site with a bash-less
host.

Wiring a raise site is still a **behaviour change** that belongs with each call
site, one at a time, with its own test. Per-surface, the call sites to wire are
named in each record's `measured_on`.

## 2. Three product bugs recorded as gaps — all three now refuse

Each is a registry entry with a measurement. Recording the first two was phase
5's job; the third was recorded then too, but the fact that its failure was
SILENT was found later, while wiring it. All three have since been converted
from a misleading (or silent, or destructive) failure into a refusal. What is
still open under each is what a fix would be, and none of the three has one.

### `run-command-line-length` — NO LONGER SILENT on `Host.run()`

`UnixHost.run()` passes `term_type="dumb"`, so it allocates a pty; `exec()`
does not. BusyBox `ash` **silently truncates a typed line at 1023 characters**
(`CONFIG_FEATURE_EDITING_MAX_LEN`) — measured identical against OpenSSH and
against a bare local pty, so it is the shell's line editor, not the transport.
Re-measured 2026-08-13 across **all five** matrix artifacts through a local
pty: every row answers 1022 intact / 1023 truncated, so the bound is a
constant (`otto.host.userland.ASH_TYPED_LINE_MAX`) and not a per-row table.

`Host.run()` now REFUSES up front on a host whose declared shell dialect is
`ash`, rendering the record's own message. The bound is applied to the line
otto **types** — the BEGIN/END framing costs 74 characters, leaving 948 for the
longest line of the command — and a multi-line script is judged line by line.

**What is still open here**, and why the record stays `measured-broken`:

1. No fix, only a refusal. The fix is a pty-free `run()` path; the buffer
   belongs to the device and cannot be raised from otto's side.
2. `HostSession.run()` on a named session is **still silently truncated**.
3. `exec()` on a `term: telnet` or proxied-login host is **still silently
   truncated**: neither has a stateless exec primitive, so the call routes
   through a pooled shell session. This is the case the `_SHELL_CHUNK_BYTES`
   docstring already flags, and it is exactly why the guard sits in
   `SessionManager.run_cmd` and not in `ShellSession.run_cmd` — one layer down
   it would refuse `ShellFileTransfer`'s own 5534-character chunk lines instead
   of transferring them. Pinned by `TestTheRefusalIsScoped` in
   `tests/unit/host/test_run_line_length.py`; do not "tidy" the guard downward
   without reading it.
4. A device whose BusyBox raised `CONFIG_FEATURE_EDITING_MAX_LEN`, or compiled
   the line editor out, is now refused a command it could have run. Accepted
   trade, stated on the constant; there is no per-host override today, and
   adding one (a `userland_options` field, or a probe) is the natural next
   item if a real device disagrees.

### `daemon-launch` — NO LONGER a silent SUCCESS on `link impair --expire`

`otto link impair <link> --expire N` launches a detached timer to clear the
impairment later, through `otto.host.daemon.launch_command`'s
`bash -c 'exec -a …'` wrapper. On a host declaring `has_bash=False` that line
came back `bash: not found` — and nothing looked. `otto.link.manage._root_run`
deliberately does not raise on a non-ok result (its docstring's reason: a qdisc
mutation's failure is caught by the caller's own re-read), and **nothing
re-reads after a timer launch**. So `impair_link` appended the placement,
returned an `ImpairReport`, and the CLI printed success for an impairment whose
timer did not exist and which therefore never expired.

It now REFUSES, at `_launch_daemon`, rendering the record's message. The refusal
lands after this call's own qdisc mutation has been applied and verified, so it
takes `impair_link`'s no-half-impairments path and the link is left as it was
found. An impair with no `--expire` on the same host is untouched — `tc` needs no
bash — and so is `repair`, which cancels timers with a `ps` scan and `kill`.

**What is still open here:**

1. **The un-watched launch is only fixed for THIS cause.** A launch that fails
   for any other reason on a host that does have bash is still discarded
   silently, and `impair` still reports success. Pinned deliberately, not fixed,
   by `test_any_other_failed_launch_is_still_unnoticed` in
   `tests/unit/link/test_manage_impair.py`. The fix is a post-launch verify (a
   `ps` scan for the sentinel, the way `add_tunnel` verifies its own chain), which
   is a change to `otto.link`'s contract rather than to the gap registry.
2. **No fix for the gap itself**, only a refusal: `--expire` remains unavailable
   on a bash-less host. A fix is a portable `argv[0]` mechanism — a design
   question, not a spelling change — and the parity sweep does not carry it yet.
3. The refusal is welded to `launch_command` via `_launch_daemon` rather than
   hoisted to the top of `impair_link`, which would refuse before touching the
   device at all. That would be cheaper by one apply/rollback round trip and is
   a reasonable later move; it was not taken because a guard at the API entry
   stops proving that the launch sites downstream of it are reachable.

### `file-ops-base64` — NO LONGER blames the file, or empties it

`read_file`/`write_file` emit `base64` / `base64 -d` whatever
`Userland.base64_flag` says, so both break on BusyBox 1.16.1, which ships no
`base64` applet at all. They still cannot adapt; they now REFUSE, at
`otto.host.file_ops.refuse_if_base64_is_absent`, rendering the record's message.

(This paragraph used to cite `src/otto/host/file_ops.py:266,317`. Both numbers
had drifted two lines, in a field that renders into the operator's error
message, so they were dropped rather than renumbered — the record's `paths` name
the same two call sites as dotted names a test resolves. `Gap.measured_on`'s
docstring now says so, and names the two records that still carry a line number.)

What that replaced was worse than the record originally said, in both
directions. `read_file` re-attributed the device's `base64: not found` to the
caller's path as a `FileNotFoundError`, sending them after a file that is
present. `write_file` was destructive: measured on the 1.16.1 artifact's own ash
with `PATH` blocked, `echo … | base64 -d > <17-byte file>` left that file at **0
bytes** before answering `not found`, because the shell opens the redirect before
it resolves the command. `>>` (an `append=True` write) did not truncate.

**The predicate is PROBED, and that is a real cost this change accepted.** There
is no declared base64 fact — `has_bash` is unrelated and the `busybox`
os_profile deliberately declares no `userland_options`, because a declaration
skips the probe and a wrong guess is unfixable from the device. So the first
`read_file`/`write_file` on a host now pays one `Userland.resolve()`, cached on
the host object thereafter, and up to `_RESOLVE_BUDGET_S` (30s) on a host that
answers nothing — where before it paid none. Accepted because these two are
coarse-grained, user-facing and called by nothing under `src/otto/`, so there is
no loop for that cost to multiply through, and because `_RETRY_COOLDOWN_S`
bounds the repeat to one attempt per 60s window however many calls arrive. This
is the OPPOSITE call from the first two surfaces, whose guards refuse to read a
probe; the difference is the path, not a change of policy.

**What is still open here:**

1. **No fix, only a refusal**, and closing it properly overlaps the uu-codec
   item in the parity sweep — a device with no `base64` needs a second codec,
   not just a better error. The two records are one change.
2. **A host whose probe round never arrived is not refused**, deliberately:
   `base64_flag` reads `absent` for it too, but that is an assumption, not a
   measurement (`Userland.is_settled`). Such a host still attempts the
   operation and still gets the old misleading error. Refusing on an assumed
   value would turn a refused ssh channel into a verdict about the device.
3. **`LocalHost` and `DockerContainerHost` build no `Userland` at all**, so the
   guard is structurally incapable of firing on either — a guard that cannot
   fire, which is this repo's most-repeated defect. Recorded as two `PATH_OPEN`
   entries on the record. **Measured 2026-08-14 and deliberately NOT closed**;
   the measurement and the decomposition are below, because "give them a
   resolver" is three changes wearing one sentence.

   *Severity first, so the work is not over-scoped.* This is a coverage hole,
   not ongoing data loss. `base64` is absent only on BusyBox 1.16.1
   (`tests/busybox/test_applet_resolution.py::_EXPECTED_BASE64`, every later row
   `True`) and `alpine:3.20` ships BusyBox 1.36.1 with `/bin/base64` — measured,
   round trip returns its input — so `read_file`/`write_file` work on an
   ordinary container. What is exposed is a minimal, ancient or custom image
   with the applet compiled out, where a `write_file` does not merely fail but
   **zeroes the destination** (the shell opens `>` before resolving the codec;
   `>>` is intact).

   *Why it is not one change.* `_userland()` is one hook with two consumers, and
   `Userland.resolve()` has no scoped form, so settling `base64_flag` also
   produces an `elevation` verdict on a class that has none today:

   - **the mechanism moves.** A resolver over `LocalHost.exec` answered
     `elevation=sudo` on this machine (7 probes, 16 ms — cost is not the
     problem), but scripted against alpine's measured shape (`su`, no `sudo`) the
     same wiring builds `su -c <cmd>` with **no password expect** — neither class
     has a `creds` field — and with neither applet it **raises**
     `UnsupportedOnUserlandError` where the caller gets a non-ok `CommandResult`
     today. Two of three arms are a behaviour change on the two families that
     reach otto's own machine. Pinned by `TestWhyTheseTwoPathsAreStillOpen` in
     `tests/unit/host/test_file_ops_base64_refusal.py`, which is also the
     discriminator: if elevation ever stops reading this hook, those tests go
     red and this entry should be re-decided rather than re-worded.
   - **on `LocalHost` the probes measure the wrong shell.** `exec` runs them
     under `loop.subprocess_shell` (`/bin/sh`); `run` runs commands in a
     persistent `bash` (`LocalSession`). Measured: `$0` is `/bin/sh` and `bash`
     respectively, and a round answering the way a non-bash `/bin/sh` does
     resolves `shell_dialect` to `ash` on a class declaring `has_bash=True`.
     Latent only because nothing consumes `shell_dialect` yet (the module
     docstring's own hole) — and `resolve()` already prints that value as a
     pasteable `lab.json` pin.
   - **neither class can be pinned out of the cost.** `userland_options` is a
     `UnixHost` field, so the escape hatch the guard's docstring offers an
     operator does not exist here, and adding one is an init-field change that
     needs a spec field to reach a host from lab data. For the container it is
     not a small cost either: each probe is a `docker exec` dispatched as **one
     exec channel on the parent**, so 7–11 of them land on the first elevated
     command or `read_file` against a server that refuses excess channels
     rather than queueing them.

   *The decomposition, in order.* Each step is landable and testable alone:

   1. **A scoped resolution** — let a consumer settle one capability without
      producing verdicts it did not ask for. This is the step that decouples the
      file-ops guard from elevation, and it has to argue with
      `Userland.resolve`'s own case for a whole round (channel fan-out, the
      partial pin line, capabilities stranded at their cannot-ask defaults).
      Everything else is blocked on it or made safe by it.
   2. **A `userland_options` field on both classes**, plus the spec field that
      lets it arrive from lab data. Independent of step 1 and useful alone: it
      is also the per-host override item 4 under `run-command-line-length`
      wants.
   3. **A probe runner that targets the shell the host's commands run in** —
      `LocalHost` only. Either bind the probes to the session shell, or make the
      resolver state that it describes `exec`'s `/bin/sh`. Until one of the two,
      a `LocalHost` resolver records a false `shell_dialect`.
   4. **Then wire `DockerContainerHost`**, which is the higher-exposure of the
      two (an image *can* be a 1.16.1-shaped userland; the machine running otto
      realistically cannot), and `LocalHost` only after step 3.

   *One gate finding, worth keeping.* Wiring `LocalHost` by hand and leaving its
   path record at `OPEN` was caught by **three** tests — the two "the hook is
   declared once" pins and `test_a_host_with_no_userland_builds_todays_exact_sudo_command`
   — but by **nothing** in `tests/unit/host/test_gap_registry.py` or
   `tests/unit/test_docs_gap_sync.py`, which stayed green at 194 passed. The
   registry checks an `OPEN` path's SHAPE (its site resolves, it names no
   checker, it has a docs bullet), never that the site is still unguarded, and
   for these two paths it could not: the wiring is not a guard call at the site,
   it is the host acquiring a resolver. The record's own `pinned_by` test is what
   catches it — which is the mechanism working as designed, but it means the gate
   command in the brief is not where that drift shows up.
4. **`ShellFileTransfer` still refuses on an ASSUMED `absent`**
   (`_run_put`/`_run_get` read the value without asking whether it was
   settled), so a transfer to a host whose probes were refused is declined with
   a message about the device's applets. That is a pre-existing, probe-driven
   refusal and was left alone; `shell-transfer-base64` is the next surface in
   the queue and is where it should be reconsidered.
   **RESOLVED with the `uuencode` codec.** `_select_codec` now separates the
   two: a SETTLED absence selects uu, an unsettled one refuses with a message
   that says the probe could not be asked rather than claiming the device has
   no base64.

## 3. Coverage the exit criteria do not actually have

### Criterion 3 — Tier 3 drives one matrix row, not the matrix

`tests/busybox/test_tier3_shell_transfer.py` is the only place under
`tests/busybox/` that drives the `shell` backend at all, and it runs
`BUSYBOX_MATRIX[-1]` (1.35.0). `TIER3_RELEASE`'s docstring argues for one row on
cost grounds and the argument is sound, but the criterion says "on the matrix".

Matrix-wide evidence today is Tier 2's codec contracts (`base64 -d`,
chunk-append, `dd` ranges on the four rows that have `base64`) and Tier 1's
applet table — the **primitives**, not the backend. Deciding whether to widen
Tier 3 or to reword the criterion is the open question; do not silently treat
the primitives as backend coverage.

### Criterion 7 — CLOSED: the CI half is now demonstrated

**Resolved by CI run `31759194001` on `901326a4`: success, every job green,
including `busybox-artifacts (arm64)` and `busybox-artifacts (x86_64)`.** Tier 3
cannot reach a passing state with any of the three preconditions below unmet, so
all three are now demonstrated rather than asserted. Criterion 7 is met.

Kept for the record, because each is a live dependency that a future change to
the runner image or the tier could break, and the failure modes are worth
recognising:

1. **`/usr/lib/sftp-server` exists on both runner images.** This is the one that
   fails hardest: `mount --bind` onto a missing target aborts the daemon script
   and takes the whole tier down at session setup, reported as "the Tier 3
   dropbear died at startup", which slightly misnames the cause.
2. **`openssh-client` is present.** Nothing installs it — not the Vagrantfile,
   not CI. The tier needs `ssh-keygen` (`dropbearkey` writes a format asyncssh
   cannot read), plus `scp` and `sftp` for the refusal contract. Documented in
   the guide page as of phase 5, but there is **no plumbing test and no install
   step**, unlike `dropbear-bin`. Failures are named, so it is a completeness
   gap rather than a silent one.
3. **`qemu-x86_64` registers with the `F` flag on the arm64 leg.** The busybox
   job is a two-row matrix (`ubuntu-latest` and `ubuntu-24.04-arm`), and since
   `TIER3_RELEASE.arch` is `x86_64` the arm64 leg takes the foreign-arch branch
   — the same one as the dev VM, and the branch that asserts `F`. That is
   precisely where a missing `F` would first bite.

Only criterion 3's "on the matrix" half remains open under this heading.

## 4. Small deferred items

- **`staged_temp_name(dest, 1)` returns `"."`**, a basename resolving to the
  containing directory. Documented as out of contract in both the source
  docstring and the test, because making it visible means raising, which is a
  behaviour change. No matrix target declares such a limit.
- **`pytest.UsageError` from `pytest_runtest_setup` does not abort the
  session** — measured: pytest reports a per-item ERROR *with a conftest
  traceback* and continues, exit 1. So versus `pytest.fail(..., pytrace=False)`
  it is strictly noisier, not more decisive, which is the opposite of what the
  name suggests. `tests/busybox/conftest.py`'s raise site is left alone;
  changing the mechanism is a behaviour change.
- ~~**`install`/`stage` was never measured.**~~ **CLOSED** — moved to the
  untested entries as the `product-lifecycle` record. Measuring it turned out
  to be not merely expensive but *meaningless*: `Host.stage`/`install`/
  `uninstall`/`is_installed` emit no command of their own, `Product` declares
  all four of its methods abstract, and otto ships exactly one concrete body —
  `FileProduct.stage`, a single `await host.put(...)`, a surface already in the
  table and already run over real ssh in Tier 3. Every other byte that reaches
  the device is project-supplied product code, so a Tier 3 test would have
  exercised the `Product` subclass the test itself wrote plus a `for` loop —
  a guard that cannot fail for a BusyBox reason. The record's claim is about
  otto's own source rather than about a device, so it is pinned as structure in
  `tests/unit/host/test_gap_registry.py`
  (`TestProductLifecycleIsUntestedBecauseOttoShipsNoImplementation`): the day
  otto ships a concrete `Product.install`, the surface becomes otto's, becomes
  measurable, and those assertions redden.

## 5. Not BusyBox: the empty-lane-leg gate — **CLOSED**

**Built, as `test_every_lane_leg_selects_at_least_one_test` in
`tests/unit/test_lane_invariants.py` (`f9e895ce`).** It inventories all 37 legs
across the Makefile, `noxfile.py` and `scripts/stability_campaign.py`, and
asserts each selects at least one test. Verified red by restoring the pre-#229
two-leg recipe, which names the leg and the count it saw:

```text
Makefile: `-m serial_timing and concurrency` over <testpaths>
  — selects 0 of the 6515 tests collected there
```

Costs ~13.6 s.

The design finding is the durable part, and it is why the obvious version of
this gate would have been worthless: **membership had to come from real
collection, not from reading the tests.** Markers reach a test by four routes
here — module `pytestmark`, function/class decorators, directory conftest
stamps, and param-level `marks=` built by a helper — and a per-function AST
scan modelling three of them reports phantom empty legs. Measured: that scan
called `stability and embedded and not chaos` empty; it collects 15. The gate
therefore shells out one `--collect-only` per distinct path set and evaluates
expressions with pytest's own `Expression` compiler rather than a hand-rolled
one, which is load-bearing — an `or` expression a conjunction reader would have
silently skipped is caught as an offender.

Retained below: the original record of the hazard and its two sightings.

**A lane leg that selects zero tests exits 5 and aborts `make`.** Unmarking a
test can empty one: issue #229 removed `serial_timing` from the last two members
of `serial_timing and concurrency`, which was the selector for the second leg of
`make stability-unit`, and the nightly would have gone red across a 5-way Python
matrix *after* paying the full ×100 soak.

No gate catches it. `makefile_serial_lane_gaps()` in
`tests/unit/test_lane_invariants.py` audits leg **text** — "does a recipe that
spells `not serial_timing` also carry a paired `-n0` leg?" — never leg
**membership**. `make coverage` stayed green throughout because its serial legs
use different intersections.

First sighting was `dashboard-soak`, whose Makefile comment already records the
identical hazard in prose. The natural template is
`test_no_lane_but_the_busybox_lane_can_select_the_busybox_tier` in
`tests/unit/test_tier_marker_invariants.py`, which states its rule over what a
lane can **select** rather than over the shape of its expression, and re-derives
its premise on every run.

Not built: a new gate needs its own mutation proof, and the house rule is that a
gate which cannot be shown red does not land.
