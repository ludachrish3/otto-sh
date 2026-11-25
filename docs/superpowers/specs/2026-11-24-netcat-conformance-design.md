# Netcat conformance: every implementation and strategy is verified on the bed and documented

Issues: #1315 (this design), #1299 (the probe opens its own listener), #1317 (a netcat that ends early), #1304 (closed by this work). #998 is not done here.

This work lands after #1316 (the listener count per address family, and the one function for "no exit status"). #1316 is open as this is written; the design assumes it.

## Why

Netcat is a supported file transfer protocol. Its conformance is uneven:

- Two netcat implementations run on the bed: Ubuntu's `netcat-openbsd` on `test1` to `test4` and the BusyBox applet on the five BusyBox guests. `nc_options.exec_name` documents `ncat` and traditional netcat as usable, and neither runs anywhere. The comments in `src/otto/host/transfer/nc.py` say so: the listener form is measured on six userlands "plus traditional netcat and ncat, which document it".
- #1304 is what that cost. `ncat` binds one IPv4 and one IPv6 socket for its port, every built-in listener check counted two listeners, and every transfer through it was refused as a shared port. The fix (#1316) is tested against that socket shape, not against `ncat`.
- The strategy cells added by #1309 verify every port strategy and listener check on nine hosts, and nothing renders the result. No page tells a user which strategies work on which userland.
- `otto host <id> probe` measures the listener checks against a port the host already listens on. It cannot see a check that miscounts a listener bound the way netcat binds it, which is #1304's fault.

## Rulings (maintainer, 2026-11-24)

1. Netcat gets a conformance test on every bed host that can run it, BusyBox and Unix alike. (The Zephyr hosts transfer over the console and have no netcat.)
2. Every port strategy and listener check is verified on the bed, and the compatibility is documented.
3. The extra netcat packages are installed on the Ubuntu 24.04 QEMU guest (`ubu2404`) only. Nothing is installed on `test1` to `test4`.
4. The probe always opens its own listener to check the listener strategies. There is no flag to turn it off (#1299).
5. A commit says `Fixes #N` only when a gate lane runs a test of that fix. #1304 closes in this work, when real `ncat` runs in `make conformance-bed`.
6. `net-tools` goes on the same guest, so that the `netstat` check meets every implementation.
7. `ubu2404` gets netcat cells only. Crossing the `chaos` lab for the other contracts is a later, separate issue (#998).
8. A pinned listener check is not verified by the probe. Conformance is what the test suite proves.
9. The guest is rebuilt by hand from the dev VM, with the provision steps, and the repository's provisioning records the same result, so a later `vagrant provision` builds the same guest.
10. The probe's commit is not breaking and carries no `!`: it breaks nothing about the probe and nothing about data users manage.

## Goals

- Every netcat implementation otto's documentation names runs a real transfer on the bed, in `make conformance-bed`.
- Every listener check is verified against every implementation the bed has, every port strategy on every host, and one generated page in the user documentation shows the result.
- The probe's listener measurement is made against a netcat listener the probe opens, so a check that would refuse a transfer is reported by the probe.
- A netcat that ends before the transfer starts is reported at once, with its exit status (#1317).

## Non-goals

- Supporting a netcat that needs something other than `<exec_name> -l -p PORT` with no further flag. Section 1 measures what the listener sequence relies on; an implementation that fails one of those measurements is a finding to bring back to the maintainer, not something to engineer around here.
- A netcat implementation axis in `schemas/support_matrix.json` (see Approaches).
- Verifying a listener check the user has pinned. It is reported as declared and no listener is opened for it. (A pinned port strategy is also reported as declared, but section 5 runs it to get the probe's port when the listener check is not pinned.)
- An up-front check that `exec_name` is installed. An earlier draft had one; it would be a third place that decides "is the transfer's binary there", it would refuse multi-word values that work today (`busybox nc`), and #1317 gives the same answer from the exit status otto already receives.
- Telnet on `ubu2404`. It stays ssh-only.

## Approaches considered

**Where the implementation axis lives.**

- *A: a new field on the conformance `Cell`* (`element, term, transfer, variant`), so every transfer surface runs once per implementation and the support matrix gains a dimension. Most thorough. It changes the cell identity every contract, the recorded order, the scratch table and the matrix format are keyed on, for one protocol on one host.
- *B (chosen): one committed table of netcat implementations per bed host, beside the strategy table, and the netcat contract module parametrized over it.* Each cell opens the host with `BedCell.open_host_with({"nc_options": {...}})`, the override the strategy cells already use. The same table generates the documentation page. One source, no change to the cell model.
- *C: test `ncat` in the hostless tier only* (install it on the CI runner). It tests a listener on the runner, not a transfer through a host, and leaves traditional netcat out. Rejected: ruling 1 asks for the bed.

**Where the compatibility is documented.** The support matrix has userland profiles as columns and contract surfaces as rows; "implementation by listener check" is a different shape. The result is a whole generated page, committed, written from the committed table by one script and pinned by a unit test, the way `docs/cli/host/families.md` is (`test_the_committed_families_page_is_what_the_tree_would_generate`). The hand-written netcat page links to it. `RENDERED` in `scripts/docs_tree.py` is not used: it is for git-ignored build output, and nothing renders a section into a hand-written page.

**How the probe opens its listener (#1299).**

- *A: one remote shell script that backgrounds `nc -l`, counts and kills it.* No otto-side teardown, but it is a second way to start and end a listener, and a backgrounded listener is what the transfer code deliberately avoids (`_nc_listener_prefix`).
- *B (chosen): the netcat backend's own listener lifetime, extracted into one helper that put, tunnelled get and the probe all use.* Today that lifetime (spawn, cancel handler, reap, unforward, release) is written twice. The probe would be the third copy, so the helper is extracted first.

## Design

### 1. The bed: three netcat implementations on `ubu2404`

`CHAOS_GUEST.packages` in `scripts/render_chaos_guest_seed.py` gains `netcat-openbsd`, `netcat-traditional`, `ncat` and `net-tools` (ruling 6). A seed change makes the provisioner rebuild the guest from its pristine image: about seven minutes, and a new SSH host key (`otto host ubu2404-qemu forget-key`, once on the dev VM).

The provisioner runs from the host checkout (`vagrant provision zephyr --provision-with chaos-guest`), which no dev-VM session can do. By ruling 9 its steps are applied by hand on `test4` from the dev VM: render the seed with the changed `CHAOS_GUEST`, stop the unit, drop the overlay and the seed image, rebuild them, start the unit. The rendered seed is left installed where the provisioner compares it, so the next `vagrant provision` from a checkout that has this change sees no difference and does not rebuild the guest again. Provisioning from a checkout without this change would rebuild the guest without the packages; the netcat contract then fails naming the missing implementation, which is the intended alarm.

**Measured on the rebuilt guest before the table is written**, each as a recorded command and its output:

- which names exist (`nc`, `nc.openbsd`, `nc.traditional`, `ncat`, `netcat`) and which implementation `nc` resolves to;
- whether `readlink -f` answers on every bed host (the oldest BusyBox guest may lack it); where it does not, a row records what `command -v` prints;
- for each implementation: `<name> -l -p PORT < /dev/null` is accepted with no further flag; it stays listening, with stdin at end of file, until a peer connects; it exits after one peer connects and closes; and the time from the peer's connect to the first byte of a served file (traditional netcat resolves the peer's name unless told not to, and the tunnelled get allows five seconds for the first byte);
- for `ncat`: the port is listed on both address families while it listens (#1304's shape);
- on every bed host, not only the guest: the exit status otto receives when the listener command names a program that does not exist, through that host's `timeout` prefix. An old BusyBox `timeout` may not pass 127 through, and section 4's wording and its bed cell follow what is measured.

A failed measurement goes to the maintainer before anything is built around it. If a flag turns out to be needed, it belongs in the one listener-command builder of section 5, never at a call site.

The guest's lab entry gains `nc` in `valid_transfers` and keeps `scp` first (the default) and `valid_terms: ['ssh']`. Checked: lab health routes on credentials, hop and terms; the bed reap takes its hosts from the same rules; the reboot lane reads no transfer menu; the support matrix iterates `BED_LABS` only. The committed lab golden (`tests/_fixtures/goldens/tech1_lab.json`) changes with the entry.

### 2. The netcat contract

`ubu2404` is reached by the netcat contract only. `tests/conformance/_bed.py` gains one explicit declaration of hosts the netcat contract reaches outside `BED_LABS`, and the 55-cell space every other contract runs over is unchanged. Crossing the `chaos` lab for every contract stays #998; the comment beside `LABS_NOT_CROSSED` is updated to say netcat reaches the guest.

One committed table (`tests/conformance/_nc_rungs.py`) holds, per host:

- the port strategies the host can run (as today; a port strategy never runs the netcat binary, so it is not crossed with the implementation);
- the implementations on the host, by the `exec_name` that selects each and the path that name resolves to (`readlink -f "$(command -v <name>)"`): one on `test1` to `test4` and on each BusyBox guest, three on `ubu2404`;
- per implementation, each listener check's state, one of three: **works**; **tool absent**; **does not work**, with the reason and the issue that tracks it. The third state is the one #1304 needed: the tool is there and miscounts this implementation.

The contract module keeps its file name (`test_nc_rung_contract.py`; a rename has four unlisted sites and buys nothing). It asserts:

- **Availability, per host**, as today: the strategies the host can run are exactly the table's.
- **Identity, per implementation**: the `exec_name` still resolves to the path the table records. A `nc` that has become another implementation fails here, asking for the row to be measured again, before any transfer.
- **Each port strategy, per host, pinned**, as today.
- **Round trip, per implementation**: put then get of three files (the transfer contract's binary payload with CRLF, NUL and invalid UTF-8; an empty file; one larger than a pipe buffer), bytes compared, and the mode checked by `put(mode=)` then `stat` on the host (a get carries no mode back). The payload and the comparison move from `test_transfer_contract.py` into one helper both modules import.
- **Each listener check, per implementation, pinned**: a *works* state moves a file with the witness that the pinned check ran; *tool absent* fails as a pinned tool failure, as today; *does not work* fails with a message that opens as the row records, the way `PINNED_TOOL_FAILED` records a tool failure's opening today. A positive control accompanies each, as the module's existing assertions have.
- **The socket shape, for `ncat`**: while its listener is up, the host lists the port on both address families. It is read from inside the module's listener-check recorder, which runs while the listener is up. Without this a passing `ncat` cell would not show #1304's condition was met.
- **A netcat that does not exist** (#1317): `exec_name` set to a name that is not on the host fails put and get with a result that names the binary and the exit status section 1 measured for that host, and with no second attempt where that status is 126 or 127.

`ubu2404` is reached through `test4`, so its put and its get are both tunnelled. It is the one GNU-userland host where get runs both ladders; the four lab VMs' direct get runs neither. The direct-get sender form (`<exec_name> <ip> <port> < file`) therefore never runs with `ncat` or traditional netcat on the bed. The generated page states, per row, the route it was verified on.

**Load.** `ubu2404` runs under emulation on `test4` beside seven Zephyr guests. Its netcat cells hold a named lock for `test4`'s guests, taken the way the BusyBox family lock is (the lock function already takes its file names as parameters), so they never run beside each other. This tree stamps no xdist group, by its own recorded rule, so a group is not the tool. The BusyBox family lock is not the lock either: it is for the five guests on `test1`. Whether the guest's cells also need serializing against the Zephyr cells is measured, not assumed: three passes of `make conformance-bed` in the plan's first bed task, reading each `ubu2404` cell's margin against its bound. If they do, the Zephyr cells take the same lock, before the console lock. Which lock a cell takes is decided by one predicate in `tests/conformance/_console_safety.py`; the netcat module's own copy of that decision is replaced by a call to it. The `test4` lock is keyed on the lab entry's hop (a guest behind `test4`), not on the kind (the guest's kind is `bed-unix`, the same as `test1` to `test4`) and not on the element's name; the pin that keys the BusyBox lock on its kind stays.

**Lane time.** `make conformance-bed` took 1097 s in #1316's gate against a 1500 s cap. This adds about seventeen cells on the emulated guest. The plan's first bed task measures the lane before and after; the cap is raised only if the measurement requires it, by the measured amount.

### 3. The documented compatibility

A new committed page, `docs/cli/host/netcat-compatibility.md`, written whole by one script from the committed table and pinned by a unit test. It holds one row per host userland and implementation (package name, the `exec_name` that selects it, the bed host, the route verified), a column per listener check with its three-state result, and a second table of port strategies per userland. It says what "works" means (a file moved with that check pinned, in the gate lane) and what it does not cover (a route no bed host has). It carries no version column: nothing in the contract asserts one.

`docs/cli/host/netcat.md` links to it, and its stale `nc -l <port>` becomes `-l -p`. `docs/configuration/host-options.md`, the `exec_name` docstring (`src/otto/host/options.py`) and the two "which document it" comments in `nc.py` stop claiming unmeasured support and point at the page. `docs/architecture/subsystems/chaos-guest.md` gains the packages and the guest's second job.

### 4. A netcat that ends before the transfer starts (#1317)

No new probe and no new command: the answer is the exit status otto already receives.

- The readiness wait ends as soon as the listener task has ended. No further poll is sent.
- The direct get's wait is resolved by the sender task ending in any way, not only by its raising. A sender that returns an exit status without having connected fails the get with that status.
- The error carries the exit status and names `nc_options.exec_name`. Status 127 is worded as the binary not being found on the host.
- An exit status a fresh port cannot change (126, 127) is not retried: it leaves the attempt the way a listener check's tool failure does today, so the retry never sees it.
- The other endings keep their present meaning. A listener that ends with status 0 during the readiness wait is the existing "fed by another" case, worded as today and retried on a fresh port. A listener command that raised, or gave no exit status (`gave_no_exit_status`), is reported with its cause and retried.

Unit tests drive the public `put` and `get`, direct and tunnelled.

Known and left: the survey's `nc` row still asks whether an applet called `nc` exists, whatever `exec_name` says (`userland.py` documents this). That is #1318. Section 5 stops the row from gating the probe's measurement when `exec_name` is not the default, so a listener that never started is still reported by `exec_name`. Until #1318 the report can therefore list `nc` as declared and dead while offering measured `nc_options` strategies for the same host.

### 5. The probe opens its own listener (#1299)

**One listener-command builder and one lifetime helper**, in `nc.py`. The builder returns the listener command for a port, a redirect and a lifetime cap (the cap becomes a parameter of `_nc_listener_prefix`). The helper is an async context manager that owns a remote listener, on a port its caller hands it, from spawn to teardown: the spawn, the cancel handler (`compensate` with the held cancellation), the reap unless the listener command returned an exit status otto received (`gave_no_exit_status` is the test; a task that merely finished, by cancellation, a raised error or a result with no status, is still reaped, and the `listen_task.done()` shortcut in `_cancel_and_reap` goes), the unforward and the port release, the last two run whether or not the reap succeeded. Readiness, the data phase and the success checks stay with each caller. Put and tunnelled get reserve their port as today and use the helper in place of their two copies. The five structural pins in `tests/unit/host/test_transfer_nc_listener_reap.py` (the spawn count among them) and that file's reads of the prefix property change with it. The reap's connect-and-close returns whether it connected; today it returns nothing.

**What the survey asks the backend for.** `UnixHost._build_file_transfer` takes the backend name as a parameter, used at all three places it reads `self.transfer` today (the registry lookup, the transfer context, and the console refusal). The survey takes the netcat backend for the survey's user: `_transfer_for(user)` when the host's transfer is `nc` (the per-user cache a transfer uses), and otherwise one built by `_build_file_transfer` with the name `nc` and that user. Either way it shares the host's connections and owns nothing that needs closing. The netcat tier already returns before this on a console session. Today it also returns when the host has no applet called `nc`; that stays the rule only while `exec_name` is the default `nc`. With another `exec_name` the measurement is attempted, and a binary that is not there is reported from its exit status (the table below). If the registered `nc` backend is a replacement without the measurement method, or building it raises (an invalid `nc_options` on a host that never used netcat), the listener ladder is unmeasured with that reason, and the survey's other rows are unaffected.

**The measurement**, one backend method. It takes the survey's runner for control commands (each bounded as today, never logged), the listener checks to ask, and a port. The survey runs the whole method as one step bounded by what is left of its deadline; when that runs out the step is cancelled, the helper's cancel handler runs, and the ladder is unmeasured as budget spent. The method first awaits the userland's `resolve()`, which the listener command's lifetime cap needs and which a transfer's warm-up otherwise provides. It catches only a check's own errors (a tool failure, an answer that is not a count); anything else from the runner, the survey's budget signal included, leaves through the helper's teardown. The survey catches it there and reports the listener ladder unmeasured with the error, as it does for the availability probe; its other rows, the declared settings and the port ladder's result are unaffected. The survey's own budget running out keeps its separate budget-spent wording, and a cancellation from outside the survey propagates after the bounded cleanup.

The four cases of pins:

| Port strategy | Listener check | What runs |
| --- | --- | --- |
| auto | auto | the port ladder, and its port is kept; then the listener measurement |
| pinned | auto | the pinned strategy once, through the survey's runner and the transfer's own reader, to get a port (a custom `port_cmd` included); it is reported as declared; then the listener measurement |
| auto | pinned | the port ladder, as today; no listener is opened |
| pinned | pinned | nothing, as today |

A port that cannot be found leaves the listener ladder unmeasured, and the note quotes the strategy's error. The port scan starts where a transfer's does, at the random offset above `nc_options.port` (`_scan_start`, made callable by both), not at `nc_options.port` exactly as the survey's port ladder does today: two probes of one host would otherwise open their listeners on the same port.

1. *Open*: the listener command a put uses, with all three streams redirected (`< /dev/null > /dev/null 2> /dev/null`), through the helper. It is spawned through the backend's exec, never logged and with no otto-side timeout, not through the survey's five-second runner: it lives until step 3 ends it. Its remote lifetime cap is 90 seconds from its spawn (the survey's whole budget), where a transfer passes one hour; it is a backstop against an orphan, not a budget for the step.
2. *Count*: rounds on the existing poll core (`wait_for_async`, with the readiness wait's timeout and interval schedule taken from the one place that holds them), each asking the available listener checks in ladder order, until one counts at least one, the listener command has ended, or the existing readiness timeout passes. Those rounds only establish that the listener is up. When the command has already ended, the rest of step 2 and step 3 are skipped. Otherwise one more complete round is then asked, and its answers are the result, so a check that answered zero a moment before the listener bound is not called blind.
3. *Close*: connect to the port and close, the act that ends a `nc -l` and the one a transfer's reap already performs, then wait for the listener command to end. The connect and close keep their own bounds; the wait for the command is bounded by the interrupted-reap constant (five seconds). Nothing is cancelled first: this is the normal path, not the error path.

Only the complete round supplies verdicts; a close that succeeds never turns an earlier zero into a failed check. Two facts are established by evidence, never assumed:

- *The listener was up*: a check counted it; or its command ended with status 0 only after otto's connect and close (it served that connection); or, on a direct route, the connect succeeded. Through a hop a successful connect is not evidence: it reaches a local forward before anything remote answers.
- *The listener ended*: its command completed with an exit status otto received (`gave_no_exit_status` of #1316 is the test). A cancelled command, a lost transport or a timeout leaves the end unconfirmed.

The method returns each check's count or error from the complete round, those two facts, whether the spawned command carried a lifetime cap, and the listener's exit status.

The probe runs no transfer, so the measurement holds no transfer permit and does not reserve its port against one.

**The verdict**, in the survey. The walk keeps today's shape: the first check in ladder order that counts exactly one is the measured one, the ones before it are named as failed, the ones after it are not reported.

| What was seen | Reported as |
| --- | --- |
| a check counts 1 | measured |
| a check counts 0 in the complete round, and another check counts it | failed: did not see the listener otto opened on the port |
| a check counts 2 or more | failed: counted N listeners on the port otto opened one on. Either the check miscounts this netcat, so a transfer would refuse the port as shared, or another process holds the same port. Running the probe again may tell them apart; a repeated result is not proof of either |
| no answer, tool failed | failed, as today |
| no check counts it before the readiness timeout | the ladder is unmeasured, whatever the close then shows. The note says which it was: the listener was up when otto closed it, so either every check is blind to this netcat or it took longer than the timeout to start; or otto could not tell whether its listener started |
| the listener command raised, or gave no exit status | the ladder is unmeasured with the cause, and the listener's end is unconfirmed (see below); never worded as an exit status |
| the listener ended before the close | the ladder is unmeasured: `<exec_name>` exited with status N. Status 127 names the binary as not found. Any other status is either this netcat refusing `-l -p PORT` or a port another process took in the same moment: the line says to run `<exec_name> -l -p <port>` on the host to see its message, or to run the probe again |

A count of two or more is not measured again on another port. An earlier draft did; a collision needs another transfer to pick the same port in the same moment, and the message names both causes. The random scan start makes a simultaneous collision unlikely; it does not guarantee a different port, and a custom port command may return the same one every time.

Under `--user` the listener and the checks run as that user, and no pin is offered, as today. The lifetime cap's spelling comes from the host's userland, as a transfer's does; it is the host's, not the user's.

**How the listener ends, and what the probe says about it.**

- Normally: step 3, and the end is confirmed. The report says nothing more.
- The end is not confirmed (the connect failed, as behind a firewall between the controller and the port on a host that never used netcat; or the command did not end in the bound; or the survey's deadline cancelled the step, where the cancel handler cancels the command and then makes its own bounded connect and close): the report adds one line naming the port and what bounds the listener. Where the spawned command carried the cap, that is 90 seconds from its start. Where it did not (no `timeout` on the host, an unresolved userland, a convention otto does not know), the line says no lifetime cap was installed, the end is unconfirmed and the listener may stay indefinitely; the connect and close was a best-effort attempt to end it.
- A step the deadline cancels returns nothing, so the method fills a result object the survey created before the call: a cancelled step still leaves the port, whether the command carried a cap, and what was counted. The teardown has its own five-second allowance beyond the budget, which is the overrun #1300 already lists. A Ctrl-C prints no report; the cancel handler's attempt and the remote cap are what end the listener then.

The sentence "Nothing listens and nothing is left on the host" (the engine docstring and `docs/cli/host/capabilities/protocols.md`) becomes: the probe opens one netcat listener on a free port, counts it and closes it, and says so when it could not confirm the listener ended. The two notes about having no inventory listener to ask about go, since the listener ladder no longer needs the inventory. The committed probe captures (`docs/examples/captures/probe-test1.txt`, `probe-bb1350.txt`, declared in `docs/examples/captures.toml`) are refreshed through their declarations if the output changes. Ruling 13 of the explicit-input guards design ("opens no port for now") is superseded by ruling 4 above.

### 6. Hygiene and reap know the new names

The implementation names live once, in `tests/_fixtures`, imported by the conformance table, by the bed's orphan-listener probe (`tests/_fixtures/bed_hygiene.py`) and by `scripts/find_test_orphans.py`, which has its own list today (it runs as a bare script, so it puts the repository root on its path first, as `scripts/bed_reap.py` does). The probe's pattern is still built by `argv_pattern`, with each name regex-escaped (`nc.openbsd` has a dot, and the reap kills what the pattern matches). The retired flag spellings it recognises stay, kept apart from the current names so that dropping a table row does not stop an old orphan being seen. The unit test that pins `ncat -l` as not matched flips, and the false-positive cases are extended alongside the positive ones.

## Constraints

- **Transfers:** additive. No option is removed or renamed, and a host that works today behaves the same. Error texts change for a netcat that ends early (#1317), and such a failure arrives sooner.
- **The probe changes behaviour for every Unix host that has netcat**, whatever its transfer: it opens a listening port for a moment, runs a few more commands, and can report a failed listener check where it reported a measured one (a check that counts an existing listener but miscounts netcat's). By ruling 10 its commit is not marked breaking; the commit body and the release note say what the probe now does.
- No package is installed on `test1` to `test4`.
- The guest rebuild is announced to the other sessions before it runs and happens under the bed lease.
- The reboot lane keeps working: `make chaos` is run once on the rebuilt guest as proof, not inferred from the package list.

## Testing

| What | Where it runs |
| --- | --- |
| Listener helper: spawn text, teardown on success, error and cancellation; put and tunnelled get unchanged through it | unit |
| #1317: early exit on put and get, direct and tunnelled, statuses 127 and 1 | unit |
| The measurement method against a fake runner: each row of the verdict table, each of the four pin cases, a listener that binds between two checks of one round, a replacement backend, a build that raises | unit |
| The generated page equals what the table generates; hygiene pattern per name, escaped, with false positives | unit |
| The netcat contract over every host and implementation, with the `ncat` socket-shape witness and the missing-binary cell | `make conformance-bed` |
| The probe on a BusyBox guest and on `ubu2404` with each implementation: the ladder is measured, and the host's listeners after the probe are what they were before | `make coverage-bed` (the integration tier), on guests only their own worker touches |
| The probe interrupted while its listener is up: the listener is gone within the cancel handler's bound | `make coverage-bed`, same guests |
| The reboot lane on the rebuilt guest | `make chaos`, once |

The gate is scoped by the diff, so these lanes run for a landing that touches this code, not for every landing.

## Landing

Two pull requests, in this order, both after #1316:

1. **Bed and contract.** One commit per issue: #1317 (the early exit), then #1315 (the guest's packages, the lab entry, the table, the contract, the generated page, hygiene). The #1315 commit says `Fixes #1304`: `ncat` then runs in the gate, with the socket-shape witness.
2. **Probe** (#1299): the helper, the measurement, the survey change and its documentation.

The second can be built in parallel in its own worktree; only its bed tests need the first one's guest.

## Not in this work

- Crossing the `chaos` lab for every conformance contract, and an Ubuntu 24.04 column in the support matrix (#998).
- The survey's `nc` row asking about `exec_name` rather than an applet called `nc` (#1318).
- The open items of #1300.

## Left to the builder

- The `exec_name` values in the table, after the measurement in section 1.
- The generated page's layout.
- The names of the helper, the builder and the measurement method.
