# The bed lease (#794) and the bed reap (#759) — design

## Why

The lab bed is single-tenant, and nothing enforces it. A session that wants
the bed today runs `pgrep` and asks its peers; a lane started in ignorance of
another dirties both runs, and the failure that follows is read as a flake. The
lab-server plan (#801) multiplies the askers: humans, agent sessions in
worktrees and one CI runner per bed cell, and a GitHub `concurrency` group
cannot see a run started from a shell.

One lease, in one place, acquired by every bed lane. otto's own
`reservations` JSON backend is the read side, so `otto reservation check` on
the cell reports who holds it: otto stays a strict consumer, as
`src/otto/reservations/protocol.py` says ("otto never creates, modifies, or
releases reservations"); the lease script is the scheduler.

An interrupted run leaves the bed dirty (#759: tunnels on test1/test2,
logged-in consoles, docker stacks and labelled images on the docker hosts,
impair timers and netem qdiscs, `/tmp/otto*` staging, a half-built embedded
coverage product), and the next run fails for that, not for its change.
Chris (2026-11-01): `make bed-reap` is the mechanism to clean up after an
interrupted run, and it must be documented in docs/contributing.md so every
user knows it. The reap runs under the lease: a reap must never destroy a
live run's resources, and the lease is what proves none is live. Both land
on this branch, lease first.

## Goals

1. **Two bed lanes from two shells on one cell cannot overlap.** The second
   fails fast, naming the holder, its purpose and its expiry, or waits when
   `BED_LEASE_WAIT=<seconds>` is set.
2. **A dead run never blocks the bed, and a live one is never overlapped or
   reaped.** The lease is held by the run's process groups, not a pid: a
   lane killed with SIGKILL whose pytest workers still run keeps the bed
   until they are gone, and the next acquirer then takes it over with a
   logged notice. Expiry reports an overrun; it never licenses an overlap
   (a deviation from the issue's wording, below).
3. **otto reports the lease.** The lease is rendered in the JSON backend's
   exact file format, so `otto reservation check` against that file reports
   the bed's resources as held by the holder, and refuses anyone else naming
   the holder.
4. **Every bed lane reaps at start and at a green exit, and `make bed-reap`
   reaps by hand**, under the lease, reusing the bed-hygiene oracle and
   otto's own removal verbs, never a `pkill` of its own.
5. **The census pins the prologue.** `tests/unit/test_lane_invariants.py`
   fails when a bed target's recipe does not carry the lease and reap lines,
   and when a hostless target carries them.
6. **The docs name the lease and the reap as the mechanism**, in
   docs/contributing.md and AGENTS.md, where the coordination rule is stated.

## Non-goals

- The bed cells and runners (#801), capability pools (#804). The lease is
  the primitive they build on; their prologues call it.
- A network lease. The lease root lives on the machine the lanes run from,
  which is the cell's lab host (the dev VM today). #801 moves the root with
  `BED_LEASE_ROOT`; the file format does not change.
- Writing the lease through otto. otto reads reservations; it does not write
  them, and nothing under `src/otto/reservations/` changes.
- A test-level host lease. `tests/_fixtures/_host_pool.py` leases single
  hosts between tests of one or more sessions on the same bed; the bed lease
  is per lane, above it. Both stay.
- Evicting a live run. No renewal, no fencing: a lease held by a live group
  stays held however old it is. Eviction is a human's `make bed-release`,
  or the second Ctrl-C of the run's own operator (below).
- A reset-from-snapshot of the bed (#801/#802 make that the mechanism and
  this reap the fallback; the reap's definition of "clean" stays).

## Decisions: the lease

### The script

`scripts/bed_lease.py`, run as `uv run python scripts/bed_lease.py <verb>`.
It imports otto (the lab loader, the resource resolver, the identity
resolver, `otto.utils.wait_for`), as `scripts/lab_health.py` and
`scripts/refresh_docs_captures.py` already do under `uv run`.

| Verb | Does | Exit |
| --- | --- | --- |
| `acquire --pid PID [--purpose TEXT] [--unless-all-under DIR -- PATHS...]` | Take the cell's lease for *PID*'s run, or report the holder. | 0 taken, or already held by *PID*'s run, or no bed path; 1 held by another run; 2 usage, a *PID* that does not exist, or a lab that cannot be loaded |
| `release --pid PID [--force]` | Release the lease *PID*'s run holds. Not the holder: a no-op, exit 0, one line saying so. `--force` releases whoever holds it and names them. | 0; 2 usage |
| `status` | Print the cell's lease (owner, purpose, pid and group states, since, expires, checkout), or `free`. | 0 always |

Options common to every verb, never spelled twice: `--lab FILE` (required:
the cell's lab file; the cell is the file's directory name, `tech1`; the
file declares the cell's labs in its `labs` table, `unix`, `busybox` and
`embedded` for tech1, and the script loads every declared lab through
`otto.load_lab` with the file's directory as the one search path), `--root
DIR` (default `$BED_LEASE_ROOT`, then `/tmp/otto-bed-lease`). Environment:
`BED_LEASE_WAIT` (seconds to poll before giving up; default 0, fail fast; a
value that is not a non-negative integer is exit 2), `BED_LEASE_TTL`
(seconds of budget the lease declares; default 14400, four hours; a soak
expected to run longer sets it in its environment; validated the same way).
The Makefile passes `--lab $(LAB_FILE)`; the script has no lab default.

`--unless-all-under DIR -- PATHS...` is `make quick`'s classifier, one
function `paths_need_bed(checkout, dir, paths)` in `bed_lease.py` that the
reap imports too: every path is normalised against the checkout
(`os.path.normpath` of the absolute path, so `tests/unit/../integration`,
`./tests/unit/x`, `.` and `tests` classify correctly); when every path is
under *DIR* the verb prints `no bed path in PATHS` and exits 0 without
touching the lease.

### The record

Under the root, per cell:

- `<cell>.json`, the structured record, present only while held: `owner`
  (`user@host`), `pid` (the holder), `pid_started` (the kernel start time of
  *pid* from `/proc/<pid>/stat`, so a reused pid is not mistaken for the
  holder), `groups` (the process groups of the run: the holder's at acquire,
  plus the group of every re-entrant acquirer whose group differs; each
  `{pgid, pid, owner}`, *owner* `holder` when the group's leader is the
  registering pid, `ancestor` when the leader is one of its ancestors,
  `foreign` otherwise, for the status report), `user` (the holder's login as
  otto resolves it, recorded once at acquire: the rendering below reads it,
  so another user's `status` never rewrites the lease as theirs),
  `checkout` (the acquiring repository's top level), `since`, `expires`
  (UTC ISO 8601, `since` plus the TTL), `purpose`, `resources`. A reader
  ignores keys it does not know (a newer branch's record stays readable to
  an older one sharing the root); a record it cannot parse jams every verb
  with exit 2 except `release --force`, which removes it.
- `<cell>.reservations.json`, otto's JSON backend format, version 1. Held:
  one entry, `user` = the record's `user` (the holder's login, `resolve_username(None)` from
  `otto.reservations.identity`, which is how otto resolves the invoking
  identity on the same machine), `resources` = every resource
  `otto.reservations.check.required_resources(lab)` returns with no host
  selection, over every lab the cell declares (lab, element and host
  levels: for tech1 that includes `test1-chassis` and `test2-console`, which
  a `labs.*.resources` union misses), and **no `expires`**: the backend drops
  an expired entry (`json_backend._all_reservations`), and the lease is held
  while the structured file exists, however overdue. The budget lives in the
  structured record only. Free: `{"version": 1, "reservations": []}`. The
  schema forbids extra keys, so `owner`, `since` and `purpose` live only in
  the structured record.

The rendered file is a pure function of the structured one and every verb
that takes the lock ends by re-rendering it (held or free), so a SIGKILL
between the two writes is repaired by the next verb, and otto, which reads
without the lock, sees either the previous or the next rename, never a torn
file. The structured file is written first on acquire and removed last on
release.

Modes are explicit, never left to the umask, and set only on an object this
process created (another user's object cannot be chmodded, and need not be):
the root is created and then chmod `0o777`, the lock file `0o666`, each
record `0o666`. A runner and a human on one cell are different users and
either must take over the other's dead lease.
`gate_stamp_common.write_atomically` gains an optional `mode` (chmod of the
temp file before the rename); `store_lock` gains the same for its directory
and lock file, moves from `scripts/gate_tip.py` to
`scripts/gate_stamp_common.py`, and `gate_tip` imports it from there (one
lock implementation, as the issue asks; `gate_tip`'s callers keep their
modes).

### Holder, liveness, re-entrancy

The holder is a process id passed explicitly: a recipe passes `$$PPID`, the
`make` running the lane, the same for every line of the recipe. The run is
the holder plus everything it starts, and what survives a SIGKILLed holder is
found by process group, because a group survives the reparenting the
holder's children undergo. The record keeps every group the run is known to
occupy: the holder's own at acquire, and the group of every re-entrant
acquirer (a nested `make` started by `scripts/gate_tip.py` in a session of
its own, a lane a human runs from a shell that holds `make bed-lease`) when
it differs from those recorded. A group may be wider than the run (a
launcher without job control leaves its `make` in the launcher's group); the
record notes it (`owner: foreign`) and the lease counts it all the same.

The lease is **live** while the holder *pid* is alive with its recorded
start time, or any recorded group has a member (`group_alive`). Erring
toward "held" is the safe direction: a takeover licenses a reap. `status`
reports which: `holder running`, `holder pid gone, N group(s) still
running (one is the launcher's: foreign)`, or `dead`. A dead holder whose
`foreign` group is a long-lived launcher's is held until `make bed-release`;
the docs say to launch unattended lanes under `setsid` so the group is the
run's own. `process_group` gains `process_start(pid)` beside
`process_state` for the pid-reuse check and `ancestors(pid)` (the ppid chain
from `/proc/<pid>/stat`, the field after state) for re-entrancy and the
owner note.

`acquire` decides, under the lock, in this order:

1. No record: take it.
2. The record's `pid` with its `pid_started` is *PID* itself or one of its
   ancestors: re-entrant, print `held by this run`, exit 0; the acquirer's
   group is appended to `groups` if new, then the re-render. This is how
   `make gate-tip`, `make release`, `make stability` and `make all` hold the
   lease across the lanes they run with `$(MAKE)`: the nested recipes'
   acquires register their groups, their releases (not the holder) are
   no-ops, and the outer `make`'s release frees it. An expired ancestor
   lease is still re-entrant; the overrun is printed.
3. The lease is dead (holder pid gone, every recorded group empty): take it
   over, printing a notice naming what was taken (`owner`, `purpose`,
   `since`, `expired` or not).
4. Otherwise held. Print the holder, purpose, expiry, and `overran its
   budget by <d>` when expired; with `BED_LEASE_WAIT` poll (`wait_for`,
   5 s interval) until the lease is dead or released, then take it as in 3;
   past the wait, exit 1.

`release` changes the record only when its `pid`/`pid_started` is the
caller's (or `--force`); any other release re-renders and keeps it;
`--pid` decides ownership and nothing else. The
holder's release first drains, outside the lock (the lock is one per root
and for short steps only): it waits up to 30 s for every recorded group
to hold nothing but the script's own process and its ancestors (the
`make` and recipe shell running it are in the group too; the lingering
xdist worker `gate_fresh` waits for is what remains). If survivors remain
past the wait the lease is **kept**, with a notice naming them and exit 0
(a green lane is not reddened for a lingering worker): the next acquirer
takes it over once they are gone, and only `--force` abandons live
survivors. A clean drain, or `--force`, releases: the structured file's
removal after the empty render, so the rendered file stays a valid empty
reservation file either way.

**Deviation from the issue.** The issue's acceptance reads "a lane killed
with SIGKILL leaves an expired lease the next acquirer takes over after
expiry". Here a SIGKILLed lane whose groups are empty is taken over at once,
and one whose workers still run is not taken over at all, expiry or no
expiry: the goal is no overlap, and expiry cannot prove the bed idle (a
`LANE_TIMEOUT=7200s` coverage run alone exceeds four hours; the stability
recipes have no cap). The human who knows the run is dead evicts it with
`make bed-release`, which names what it evicted.

### The Makefile

```make
LAB_FILE := tests/_fixtures/lab_data/tech1/lab.json
BED_LEASE := uv run python scripts/bed_lease.py --lab $(LAB_FILE)
BED_REAP := uv run python scripts/bed_reap.py --lab $(LAB_FILE)
bed_acquire = @$(BED_LEASE) acquire --pid $$PPID --purpose "$(1)"
bed_reap = @$(BED_REAP) --pid $$PPID
bed_release = @$(BED_LEASE) release --pid $$PPID
```

`LAB_FILE` replaces the literal path in `lab-reachability`, and `vm-health`,
`qemu-restart` and `console-logout` pass `--hosts $(LAB_FILE)` to
`scripts/lab_health.py` (its own default stays, for hand runs: the Makefile
is make's one place for the path).

Every target whose recipe runs a bed suite begins with
`$(call bed_acquire,<target>)` then `$(call bed_reap)`, and ends with
`$(call bed_reap)` then `$(call bed_release)`; a target that runs such
targets with `$(MAKE)` carries the acquire and release lines only (its
sub-makes reap):

- Direct: `coverage-python`, `coverage-integration`, `coverage-unix`,
  `coverage-embedded`, `conformance-bed`, `kmodcov`, `stability-unix`,
  `stability-tunnel`, `stability-embedded`, `chaos`, `chaos-embedded`,
  `nox-integration`, `nox-unix`, `nox-embedded`, `nox`, `nox-release`,
  `nox-full`, `repeat`, `docs-captures`, `docs-captures-check`.
- Lease only, no reap lines: the console and guest tools `vm-health`,
  `qemu-restart`, `console-logout` (a console probe or a guest restart under
  a live lane is the overlap the lease exists to prevent; the reap's own
  console step runs two of them, so they cannot reap in turn).
- Aggregators: `gate-tip`, `release`, `release-campaign`, `release-matrix`,
  `release-kmodcov-matrix`, `stability`, `all`.
- Conditional: `quick`'s prologue is `$(call bed_acquire,quick)
  --unless-all-under tests/unit -- $(PATHS)` then `$(call bed_reap)
  --unless-all-under tests/unit -- $(PATHS)`, and its epilogue the same
  pair in reverse: one classifier, four callers. A unit-only `quick`
  therefore never waits behind a peer's gate and never probes the bed.

Not leased, and pinned as such: `coverage` (prerequisites only;
`coverage-python` is the bed user, `coverage-ts` is not), `validate-python`
(its `$(COVERAGE_TARGET)` is hostless under `make ci`; its sub-makes lease
themselves), `coverage-unit`, `coverage-hostless`, `nox-unit`,
`nox-hostless`, `nox-unit-repeat`, `quick-guards`, `conformance` (hermetic
venue), `busybox` (artifact tier, no lab), `dashboard`, `dashboard-all`,
`dashboard-soak` (browser lanes), `stability-unit`, `ci`, and every other
target: the census classifies every Makefile target as leased, holding,
a lease verb or hostless, and fails on one it does not know, so a new bed
target cannot escape the lease by not being listed.

**Failure.** Make stops at the first failed recipe line, so a red lane runs
neither its exit reap nor its release: its `make` exits, its groups empty
once its children do, and the next acquirer takes the lease over with a
notice and reaps at its start. The exit reap and the release are the green
path's courtesy; the start reap is the correctness guarantee (#759's
contract: "start-reap is the correctness guarantee, exit-reap is the
courtesy"). The docs say so.

Three convenience targets: `bed-status` (the status report; exit 0),
`bed-lease` (a hold from an interactive shell: `--pid $$(ps -o ppid= -p $$PPID)`,
the shell that ran `make`, so the hold lives as long as that shell, by the
pid leg of the liveness rule; the lanes that shell then runs re-enter and
register their groups, so they are tracked, while a foreground job that
takes no lease runs in a group of its own and is not; `PURPOSE="..."` names
it),
`bed-release` (`--force`: frees whoever holds it and says who that was),
and `bed-reap` (below).

### otto as the reader

docs/contributing.md shows the two reads. `make bed-status` for the full
record. otto's own view, from a project whose settings name the rendered
file:

```toml
libs = ["/home/vagrant/otto-sh/tests/custom_hosts"]
init = ["custom_hosts"]

[[lab.sources]]
backend = "json"
paths = ["/home/vagrant/otto-sh/tests/_fixtures/lab_data/tech1"]

[reservations]
backend = "json"

[reservations.json]
path = "/tmp/otto-bed-lease/tech1.reservations.json"
```

```bash
uv run otto --lab unix+busybox+embedded reservation check
```

(a scratch project's `.otto/settings.toml`; a directory entry under
`[[lab.sources]]` contributes its own `lab.json` only, and the cell's labs
are the three the file declares). The holder sees every bed resource
`held: yes`; anyone else is refused with the missing resources and their
current holder named (the `owner` column is the declaring level, not the
holder; the holder appears in the refusal). A free cell reads as a valid
empty file, so `check` on it says nothing is held. The test suite runs this
invocation for real (below). The issue's "`otto reservation` lists the
active lease" is met by `check`, the verb that exists; no `list` verb is
added.

## Decisions: the reap

`scripts/bed_reap.py`, run as `uv run python scripts/bed_reap.py --lab
$(LAB_FILE) --pid PID [--unless-all-under DIR -- PATHS...] [--full]
[--restart-qemu]`; `make bed-reap [RESTART_QEMU=1]` for the hand run. It
imports otto and the test fixtures the way `scripts/refresh_docs_captures.py`
does, and never spells a `pkill`, a `docker rm` filter or a lab path of its
own. The cell is the loaded cell labs (the lease's loader, shared): its
unix hosts are the loaded hosts with `os_type` unix and no hop, its docker
hosts the ones `tests._fixtures.docker_reap.docker_parents(hosts)` selects
(below), its consoles whatever `scripts/lab_health.py` reads from the same
`--hosts` file. Nothing in the
reap names `tech1`, `test3` or an IP: a different `--lab` reaps a different
cell. (`tests._fixtures.tunnel_bed.build_bed_host` and
`tests/integration/conftest.py`'s `_DOCKER_HOST_IP` stay as the tests'
spellings; the reap uses the loaded `Host` objects, which carry the same
credentials.)

What it does, in order, printing one line per thing removed and
`bed-reap: clean` when there was nothing:

0. **Under the lease, after a drain.** It acquires the lease for *PID*
   (re-entrant inside a lane's `make`; `make bed-reap`'s own make
   otherwise, released at the end). A lease held by another live run is a
   refusal, exit 1, before any probe: a reap of a live run's resources is
   exactly what the lease exists to prevent. Then it drains its own run: up
   to 30 s for every recorded group to hold nothing but the reap's own
   process and its ancestors (the release's drain, one function; `--pid` is
   ownership only). Survivors past the wait are a refusal too, naming them:
   the run's own lingering workers are live resources.
1. **The unix bed hosts**, each over a fresh connection, snapshotted by
   `bed_hygiene.snapshot_host` before and after. The hygiene module gains
   `residual_dirt(snapshot)`, the **absolute** predicate the chaos lane's
   new-only `diff_snapshots` is not: otto-tunnel daemons, otto-impair timers,
   nc listeners the probe lists, `/tmp/otto-*` staging entries, netem
   qdiscs otto placed (the classifier `src/otto/link/netem.py` already has
   for foreign-vs-otto qdiscs, over every netdev `tc qdisc show` lists, not
   only `eth1`/`eth2`: the link e2e creates VLAN links), docker containers
   and networks whose names carry an orphan fragment (`-e2e-`,
   `-noexist-`), and labelled images. The snapshot's qdisc probe widens
   accordingly; `diff_snapshots` keeps its new-only semantics for the chaos
   bracket. The reap removes what `residual_dirt` names:
   - otto-tunnel daemons: `otto.tunnel.remove_tunnel(lab, id)` per observed
     id over the loaded cell labs (what `tunnel_bed._reap_recipe` prints
     today, now run instead of printed).
   - otto-impair timers: every timer the sentinel scan lists, whatever its
     link id (a test-created VLAN link's timer is otto's by its sentinel).
     `otto.link.manage._cancel_timers` keeps its link-and-netdev filter
     (`everything=True` only drops the selector filter) and is refactored
     over one core, `_cancel_timers_matching(host, predicate)`, that the
     existing callers pass their filter to and a public
     `cancel_all_timers(host)` passes "every timer" to: one scan-and-kill
     sequence, two predicates. Then `repair_all(lab)` for the declared
     links' placements, then otto-classified qdiscs left on any netdev
     deleted with the `tc qdisc del` line `netem.py` builds, as root. The
     snapshot's qdisc inventory becomes, per netdev `tc qdisc show` lists,
     the pair of outputs `NetEmImpairer.scoped_read_commands` reads (qdisc
     and filter: a scoped otto tree classifies as foreign without its
     filters), classified by `parse_scoped_outputs`; `residual_dirt` names a
     netdev whose state is `whole` or `scoped` (otto's), reports a `foreign`
     one, and ignores `clean`. `diff_snapshots` compares the raw pair as it
     compared the raw text.
   - nc listeners: `otto.host.daemon.kill_command` over the pids the probe
     listed, as the user that owns them (the bed fixtures spawn them as the
     login user).
   - staging entries: `rm -rf` of each listed entry, by name. The bed hosts
     are otto's own fixtures; the `/tmp/otto-*` namespace and nc listeners
     on them are the test suite's by the hygiene oracle's definition.
   - every command's `Result` is checked (`check_probe_result`), so a
     removal that failed is reported as such, not read as clean.
   `HygieneSnapshot` gains `docker_images` (the ids `docker image ls`
   lists under the e2e label, empty on a docker-less host, the probe shaped
   like the container probe), so labelled images are in the inventory
   `residual_dirt` reads; `diff_snapshots` reports a new one like a new
   container. The absolute report is `format_residual_report(host,
   leftovers)`, a sibling of `format_hygiene_report` whose header says what
   is still on the host, not that pre-existing dirt was excluded.
2. **Docker**, on the cell's container parents: one rule, one function,
   `docker_parents(hosts) -> list[str]` in `tests/_fixtures/docker_reap.py`,
   the element names of the loaded hosts that are `docker_capable` and
   whose resolved `host.term` is `ssh` (a container's parent session must
   be ssh; the resolution is otto's own, pins and preferences included,
   never rebuilt here: test2 is `docker_capable` with ssh among its terms
   but resolves to telnet, so it is not a parent). The e2e pool
   (`tests/e2e/docker/conftest.py`'s `_DOCKER_POOL`, today the literal
   `("test1", "test3")`) becomes that function over the fixture lab data,
   pinned equal to the literal it replaces, so the pool and the reap cannot
   drift apart. Each host under `lease_unix_host([element_name])`, the
   element's name string as the pool's own locks are keyed
   (`docker_host_lease` is test3's alone and stays the fixtures'):
   `tests/integration/conftest.py`'s sweep becomes
   `tests/_fixtures/docker_reap.py: reap_docker(host) -> list[str]` (the
   orphan fragments, containers then networks, each command's `Result`
   checked, returning what it removed; the conftest fixture keeps its
   best-effort wrapper around it), plus `remove_labelled_images(host,
   label)` extracted from `test_docker_image_honesty.py`'s finalizer
   (list ids by label, remove by id; the test calls it with its token
   label, the reap with the bare `otto-e2e-699` label that every token
   carries). One cleanup decision, two callers each.
3. **The embedded coverage product**, local to the dev VM: the version to
   build-dir to artifact mapping is the one `tests/project3/tests/
   test_embedded_coverage.py` already holds, extracted to
   `tests/project3/product/builds.py` (the test module imports it). For
   each mapping whose artifact is missing, the build dir is removed if
   present (a cut `west build` leaves a dir `build.sh`'s incremental path
   cannot finish) and `build.sh` is run for it, so the next lane's e2e
   guard (`test_embedded_coverage_e2e.py:225`, which requires every
   artifact before otto starts) finds it. A whole product is left alone.
4. **Consoles, with `--full` only**: `scripts/lab_health.py --hosts <lab>
   --logout-consoles`, as `make console-logout` runs it (it probes every
   console, resets only one that has credentials, and prints the whole-bed
   health report; a Zephyr console is probed, never reset). With
   `--restart-qemu`, `--restart-qemu` first: a hand-run choice, never a
   lane's, because a restart reboots every guest.
5. **After:** the hosts are snapshotted again and `residual_dirt` of the
   after-snapshot is the verdict: printed as the residual report
   (`format_residual_report`) and exit 1 if anything remains or any host was
   unreachable (a host down is reported and the others are still reaped).

**Scope and cost.** Steps 0 to 3 are a few SSH round trips and run in
seconds (a product rebuild, when needed, takes what `build.sh` takes);
step 4's console probe waits out 10 s per Zephyr console (about 35 s). A
lane's prologue runs the quick reap after its acquire, and its epilogue runs
it again before the release; `make bed-reap` runs everything (`--full`) and
is what the docs name for a run killed or interrupted. A lane's prologue
reap failing is the lane failing, before any test runs: a bed that cannot be
cleaned is not a bed to test on.

**The gate driver on interrupt.** One process-lifecycle core,
`process_group.run_awaiting_descendants(args, *, cwd, env, shell, capture)
-> (status, output)`: a session of its own, a return only when the group
is empty, the interrupt relay. `gate_fresh._run_awaiting_descendants`
becomes a thin wrapper over it (argv, cwd, int) and keeps its contract,
with one change of policy it gains from the core: a second interrupt
SIGKILLs the lanes' group and waits for it to empty instead of escaping
the drain, and a group that does not empty raises the core's `Undrained`
(naming the pids), on which `gate_fresh` keeps the worktree (its existing
`kept` outcome, with the reason) instead of removing it over survivors;
`gate_tip`'s `_default_runner` calls it with its shell line or argv, its
sanitised environment and its capture choice, so `RunOutcome` is unchanged.
In `gate_tip`, SIGTERM is handled as SIGINT is (a handler raising
`KeyboardInterrupt`). On the first interrupt the core forwards SIGINT to
the lane's group and waits for it to empty, unbounded, as `gate_fresh`
does (pytest's single-Ctrl-C unwind runs the fixtures' teardown), then
re-raises; `gate_tip` catches it, runs the quick reap (the run is confirmed
empty), releases the lease and exits 130. A second interrupt during that
wait means the operator has decided the run is dead: the core SIGKILLs the
group and waits for it to empty again, up to a post-kill budget shared by
both gates (`process_group.POST_KILL_DRAIN_S`, 30 s: after SIGKILL only a
zombie under a non-reaping init or a process the user cannot signal
survives, and waiting longer does not change that); `gate_tip` then reaps,
releases and exits 130 the same way. A group still non-empty at the budget
is `Undrained`: reported with its pids, the reap does not run, the lease is
kept, and the exit is 130: no reap until the bed is confirmed idle, `make bed-reap` once
it is. The nested lane's own acquire registered its session group, so the
lease tracks it either way. The `make coverage` legs get no trap: the reap
at the next start covers a killed make.

`bed-reap` is `$(call bed_acquire,bed-reap)`, `$(BED_REAP) --pid $$PPID
--full $(if $(RESTART_QEMU),--restart-qemu)`, `$(call bed_release)`.

## The census

In `tests/unit/test_lane_invariants.py`:

- A literal `BED_TARGETS` list (the leased targets above, direct and
  aggregator) and `HOSTLESS_TARGETS` (the not-leased list). The scanner sees
  the raw recipe text (`makefile_live_recipe` does no make expansion), so
  it matches the literal `$(call bed_acquire,<target>)` as the first live
  line of every bed target and `$(call bed_release)` as the last, a
  `$(call bed_reap)` second and second-to-last on every direct target and
  on none of the aggregators, and no `bed_` call anywhere in a hostless
  target's recipe. `$(SAY)` and commented lines are already dropped, so a
  removed prologue cannot hide behind its banner.
- Every target `gate_scope.BED_LANES` names, and every target of the oracle's
  full-gate bed lines, is in `BED_TARGETS`: the oracle cannot call a lane a
  bed lane that the Makefile does not lease.
- The macro definitions themselves are pinned: `bed_acquire` names
  `$(BED_LEASE) acquire --pid $$PPID`, `bed_reap` names `$(BED_REAP) --pid
  $$PPID`, `bed_release` names `release --pid $$PPID`, and both `BED_LEASE`
  and `BED_REAP` pass `--lab $(LAB_FILE)`; a macro emptied to nothing would
  otherwise satisfy the literal scan.
- `quick`'s first two live lines are the acquire and the reap, each with
  `--unless-all-under tests/unit -- $(PATHS)`, and its last two the reverse.

## Tests

### The lease

`tests/unit/scripts/test_bed_lease.py`, `interpreter_agnostic`, under a
`tmp_path` root with the real `tests/_fixtures/lab_data/tech1/lab.json`:

- acquire on a free cell writes both files with the declared modes; the
  rendered file loads through `JsonReservationBackend` and `holders(r)` names
  the user for every resource `required_resources` returns over the three
  labs, including the element- and host-level ones; the structured record
  has every field, `groups` holding the holder's group.
- a second acquire for another live pid exits 1 and prints the holder,
  purpose and expiry. With a wait budget it polls: the holder is a `sleep`
  child the test starts in its own session (`start_new_session=True`, a real
  pid and group that die on their own), and the acquire with `BED_LEASE_WAIT`
  longer than the sleep takes the lease over and reports the takeover; with a
  wait shorter than the sleep it exits 1.
- re-entrancy: the holder is the test process; an acquire for a child whose
  ancestor is the holder exits 0 and, when the child is in a new session,
  appends its group to `groups` (and leaves the structured file otherwise
  unchanged); a record whose pid matches an ancestor but whose
  `pid_started` does not is not re-entrant.
- group liveness: a record whose pid does not exist, and one whose pid is
  live with a different `pid_started`, each with empty recorded groups, are
  taken over with the notice; the same records with a surviving `sleep` in
  a recorded group (the holder's, or a nested one) are refused as `holder
  pid gone, 1 group(s) still running`; the `owner` note is `holder` for a
  `setsid` child, `ancestor` for a grandchild of one, `foreign` for a child
  of the test process started without a new session, and a dead-pid
  `foreign` record whose group is alive is held, `status` saying the group
  is the launcher's.
- expired with a live holder is refused with the overrun printed; the
  rendered file carries no `expires`, so `JsonReservationBackend` still
  reports the holder.
- release by a non-holder is a no-op; by the holder removes the structured
  file and renders the empty file after a clean drain; with a `sleep`
  surviving in a recorded group past the drain budget (shortened for the
  test through an environment knob) the lease is kept, the notice names
  the survivor and the exit is 0; `--force` removes the structured file,
  renders the empty file and names the evicted holder; a rendered file
  missing or stale is repaired by any verb.
- `paths_need_bed`: `tests/unit/x`, `./tests/unit/x` and
  `tests/unit/../unit/y` take no lease; `tests`, `.`, `tests/unit/../e2e`
  and an absolute bed path do.
- the otto read, for real: a scratch project under `tmp_path` with the
  settings above (the lab source pointing at the checkout's `tech1`
  directory, the reservation path at the rendered file), `uv run otto --lab
  unix+busybox+embedded reservation check` run as a subprocess from that
  project (`OTTOPATH` naming it): every row `held: yes` and the `OK` line
  while the test process holds the lease (the holder's login is the test's);
  every row `held: no` and the refusal naming `someone-else` when the test
  writes the rendered file by hand with that `user`; every row `held: no`
  and the refusal naming nobody on a free cell (the empty rendered file).
- `store_lock` is imported from `gate_stamp_common` by both scripts;
  `write_atomically(mode=...)` sets the mode (`test_gate_stamp_common.py`).

The make side, in `tests/unit/test_make_release_lanes.py`'s pattern (real
`make` against the checkout's Makefile, `BED_LEASE_ROOT` in `tmp_path`,
`BED_REAP` overridden on the command line to a recording stub where a
recipe would reap, as `MAKE=` is stubbed there):

- `make bed-lease PURPOSE=x` from a shell, then `make bed-status` names that
  shell's pid; `make bed-release` frees it and names it.
- a scratch makefile included with `-f Makefile -f extra.mk` defines `outer`
  (acquire, `$(MAKE) ... inner`, release) and `inner` (acquire, `bed-status`
  to a file, release): the status inside `inner` names the outer `make` as
  holder with two groups when `inner` runs under `setsid`, and the cell is
  free after `outer` returns. This is the nested gate-tip shape through real
  make, with real `$$PPID`.
- two competing lanes: `make -f ... hold` (acquire, sleep, release) started
  in its own session, then a second `make -f ... hold` fails (make exits 2
  on a failed recipe line, the acquire's own exit 1 is in its output)
  naming the first's holder, and succeeds once the first returns.
- a SIGKILLed lane with survivors: `make -f ... hold` in its own session,
  SIGKILL the `make`; the next acquire is refused naming the surviving
  group; `signal_group` it; the next acquire takes over.
- `test_make_release_lanes.py`'s `_run_make` gains `BED_LEASE_ROOT` under
  `tmp_path`, the `BED_REAP` stub and its own session, so the leased
  `stability` and `release-campaign` recipes it drives never touch the real
  lease or bed and never leave a lease whose group is the test process's.

### The reap

`tests/unit/scripts/test_bed_reap.py`, `interpreter_agnostic`. The cell
loader, the lease and the hosts are injected: a fake `Host` whose `exec`
answers the hygiene probes from a scripted state (before, and after each
removal command it records) and returns `Result`s with the status the test
chooses; `remove_tunnel`, `cancel_all_timers`, `repair_all`, `reap_docker`,
`remove_labelled_images`, the console runner and the product build
monkeypatched to record their calls. Pins:

- a refused lease (held by another live run) is exit 1 with no probe made;
  a re-entrant lease with a surviving `sleep` in a recorded group past the
  drain budget is exit 1 naming it, no probe made.
- the tunnel ids seen are the ids removed; every impair timer seen is
  cancelled; an otto netem qdisc is deleted with the `netem.py` line and a
  foreign one is reported and kept; the nc pids seen are the pids killed
  through `kill_command`; each staging entry is removed by name; a removal
  whose `Result` is not ok is reported, not read as clean.
- docker runs on `docker_parents(hosts)` (test1 and test3 for tech1, not
  test2, pinned against the real lab data, and the e2e pool is the same
  call) under each host's own `lease_unix_host` by element name, and on no
  other; `--full` adds the console step with `--hosts <lab>` and only
  `--restart-qemu` the restart; a mapping with a missing artifact has its
  build dir removed and its build run, a whole one is untouched.
- the after-snapshot's `residual_dirt` is the verdict: leftovers printed
  and exit 1; an unreachable host is reported, the others reaped, exit 1.
- `residual_dirt` itself (in `test_bed_hygiene.py`): each class of dirt
  from a scripted snapshot, the orphan fragments for docker, labelled
  images, the qdisc classifier on a whole otto netem, a scoped otto tree
  with its filters, the same tree without them (foreign, reported), and a
  kernel-default root (clean); an empty snapshot is clean;
  `format_residual_report`'s header; `snapshot_host` reads the qdisc and
  filter pair per listed netdev and the image probe.
- `cancel_all_timers` and `_cancel_timers` share the matching core (in the
  link tests: the same scripted `ps` output, the existing callers' filter
  keeps its exact pids, "all" takes every pid).
- `paths_need_bed` is the lease's (one import, pinned).
- `gate_tip`'s interrupt path, in `test_gate_tip.py`, with a real local
  lane (a `sleep` whose child survives it, run through the real core) and
  the reap and release stubbed to record their calls: SIGINT to the driver
  reaches the lane's group, the driver waits until the group is empty and
  only then reaps, releases and exits 130; a second SIGINT during the wait
  kills the group first; a group that stays non-empty (a stubbed
  `group_alive` that keeps answering true) yields the report, no reap, the
  lease kept, exit 130; SIGTERM behaves as the first SIGINT.
  `run_awaiting_descendants` is imported from `process_group` by both gates;
  `test_gate_fresh.py` keeps its wrapper's pins and adds the second
  interrupt's kill-and-drain and the `Undrained` outcome that keeps the
  worktree.

The live reap is exercised by the bed lanes themselves, whose prologue runs
it.

## Docs

- docs/contributing.md, under "Running tests", a subsection "The bed lease"
  (anchor `bed-lease`): what it is, that every bed lane takes it, the held
  message and `BED_LEASE_WAIT`, group liveness and takeover, the overrun
  notice and `make bed-release`, `make bed-status` / `bed-lease` /
  `bed-release`, otto's read, `BED_LEASE_ROOT` and `BED_LEASE_TTL`, the
  `setsid` advice for unattended launches; then "The bed reap" beside it
  (anchor `bed-reap`): what an interrupted run leaves, that every bed lane
  reaps at start and at a green exit and a red lane is reaped by the next
  start, `make bed-reap` as the command after a killed or interrupted run
  (`RESTART_QEMU=1` for a wedged guest), what it removes and what it only
  reports, that it refuses a bed another live run holds, and what Ctrl-C
  (once, twice) does to a running `make gate-tip`. The "wedged embedded
  bed" paragraph links to it. The "Gating a branch" section gains one
  sentence: the bed lanes and `make gate-tip` take the lease, so a second
  landing's gate on the same cell waits or fails fast, and an interrupted
  gate is followed by `make bed-reap`.
- AGENTS.md: one sentence after the gate bullet: every bed lane takes the bed
  lease; a held lease is the answer to "is the bed busy", `make bed-status`
  shows it, `BED_LEASE_WAIT` queues behind it, and `make bed-reap` is what
  to run after an interrupted bed run.
- docs/architecture/quality-gates.md: no new row; the lease is not a gate.

## Out of scope, recorded

- The issue's `owner`/`since`/`purpose` keys in the otto-format file: the
  schema forbids extras, hence the second file.
- "docs/contributing.md replaces 'ask your peers' with the lease": no such
  text exists in the docs; the lease subsection is added where the rule
  would have been.
- `scripts/lab_health.py`'s own lab default stays; the Makefile stops
  spelling the path twice.
- #759's "inject the interrupt against a bed double" acceptance is met at
  the unit level (the gate driver's interrupt path with a real local lane
  and stubbed bed operations, the reap with fake hosts); a live interrupt
  test would need a bed lane of its own and is not added.

## Commits

1. `feat(scripts): lease the bed per lane, readable by otto reservation`
   (script, `store_lock` move and modes, `process_start`/`ancestors`,
   Makefile lease lines, census, tests, lease docs). Refs #794.
2. `feat(scripts): reap the bed at every lane's start and green exit, and by hand`
   (`bed_reap.py`, `residual_dirt`, the qdisc-and-filter and image probes,
   the docker reap module, the product builds module, the timer-cancel
   core and `cancel_all_timers`, the shared process-lifecycle core and
   gate_tip's interrupt path, Makefile reap lines, tests, reap docs).
   Refs #759.

The Makefile is a build file, so the oracle's tip column is the full gate
plus the bed lanes; one gate-tip for the two-commit tip.
