# Container cells for the command contracts (#935, item 4) — design

Status: **paused** (Chris, 2026-11-20), at revision 4. Of this design only
section 8 (the log) has landed, as #1206. The rest waits:

- The stop fix of section 7 is deferred: #1200 stays open, and the behaviour
  is documented with a workaround (`docs/architecture/subsystems/docker-hosts.md`,
  "A timed-out or cancelled exec leaves its command running").
- The identity redesign of section 6 is in the backlog
  (`2026-11-19-container-identity-design.md`).
- The cells themselves wait for the design that makes `exec` a short-lived
  `run` on every host family, and are then designed once against that model.
  Sections 5, 6, 7 and 10 below describe the plan as it stood on 2026-11-19
  and are superseded where they order a stop fix and an identity fix before
  the cells. A container's parent is never a container, so the text about
  nested containers no longer applies.

Revision 4 was reviewed twice each by Astra and Fable and once by Astra 6.2,
and decided by Chris on 2026-11-19 (see Decisions).
Parent design: `2026-11-13-command-execution-conformance-design.md`, landing
order item 4. Its rules 1 to 5 apply unchanged and are not restated here.

## Why

A `DockerContainerHost` answers `run` and `exec` like any other host, and its
`exec` reports the route `container/<the parent's route>`. No conformance cell
is a container, so no command contract has ever run against one, and the
generated contract pages say so (`docs/command-contracts/index.md:199`).
This item makes a container a cell, runs every command contract over it, and
resolves each red cell by fixing the product or declaring the difference.

## What was read and measured

Read (surveys in `.superpowers/sdd/pr4-survey-suite.md` and
`pr4-survey-product.md`, with `path:line` for each fact):

- A container host is only ever built by code (`compose_up`, or a lab-load
  placeholder). It is in no `lab.json`, so every lab-keyed step of the suite
  refuses it (listed in section 2).
- `exec` wraps the command as `docker exec -i [-u U] <cid> sh -c '<cmd>'` and
  hands it to the parent's `exec`. Value, exit status, stdin, timeout, stop,
  cancel and the log lines are whatever the parent does with that wrapper.
- `run`, `send` and `expect` share one persistent `docker exec -it [-u U]
  <cid> sh` channel on the parent's ssh connection. Its result reports
  `default-session`. It always uses `BashFrame`, whatever the image's `sh` is.
- Identity is decided in six ways today:

  | Call | On a container today |
  | --- | --- |
  | `exec(user=X)` | `docker exec -u X` |
  | `exec(sudo=True)` | `docker exec -u root` |
  | `exec(sudo=True, user=X)` | bare `NotImplementedError` |
  | `run(sudo=True)` | types `sudo -S <cmd>` in the channel |
  | `run(user=X)` | `RuntimeError` once the channel is open as another user |
  | `as_user(X)`, `switch_user(X)` | types `su - X` in the channel |

- What a unix host does, which is the behaviour to conform to
  (`host.py:2356`, `login_proxy.py:192-254`, `privilege.py:486-560`, `:770`):
  `as_user(Y)` types `su - Y`, a fresh login shell in `Y`'s home with the
  environment reset; `run(user=Y)` is that scope around one call; inside one
  scope successive `run`s share `Y`'s shell; at exit the login's shell is back
  as it was. `run(sudo=True)` types `sudo -S <cmd>` into the session, so the
  command runs as root in the session's directory. `sudo=True` with `user=X`
  becomes `X` first and elevates from there, so it needs `X` to be a sudoer.

Measured on test3, 2026-11-19 (Docker 29.1.3), in a throwaway container per
image. test1 holds the same two images.

| Fact | `alpine:3.20` | `centos:7` |
| --- | --- | --- |
| Held by test1 and test3; neither reaches a registry | yes | yes |
| Userland | BusyBox 1.36.1, `sh` is ash | GNU, `sh` is bash 4.2 |
| Default user | root | root |
| `sudo` in the image | no | no |
| `su`, `stty`, `kill`, `/proc/<pid>/stat` | yes | yes |
| `su - daemon` from root | refused (`nologin`) | refused (`nologin`) |
| Client of `docker exec -i` gets SIGTERM or SIGKILL: the command inside | **keeps running** | **keeps running** |
| Client of `docker exec -it` gets SIGHUP: the command inside | keeps running | stops |
| The `sh` of a `docker exec -i` is a process-group and session leader | yes | yes |
| A token passed with `docker exec -e` is in `/proc/<pid>/environ` of the command and its children, and survives the shell's `exec` | yes | yes |
| A second `docker exec` as the command's own user reads that `environ` | yes | yes |
| A second `docker exec -u root` reads it | **no** (no `CAP_SYS_PTRACE`) | **no** |
| `kill -TERM -<pgid>` from a second exec stops the whole group | yes as the command's user; **no** in one earlier run as root | yes |
| `kill … -- -<pgid>` | refused by BusyBox `kill` | accepted |
| Signalling each member of the group by pid | stops all | stops all |
| `sh -c 'kill -TERM $$'` through `docker exec -i` | status 143 | status 143 |
| stdin at end of file through `docker exec -i` | reads EOF | reads EOF |
| `docker exec -u nobody`: `id -un`, writes `/tmp`, reads another user's `/proc/<pid>/stat` | yes | yes |
| `nobody`: `HOME` and start directory | `/`, `/` | `/`, `/` |
| Accounts in both images besides root | `nobody`, `daemon`, `bin` | same |

Not measured yet (first plan task): `stty -a </dev/tty` as `nobody` on the
`run` channel; the frame handshake with an unwritable `HOME`.

## 1. What a container cell is

- **Kind:** `bed-container`. **Family:** `container` (today `family_of` would
  call it `unix`, which would give it a unix host's routes and entries).
- **Elements:** two, `alpine320-on-test3` and `centos7-on-test1` (no `:`, a
  cell label separates its parts with it). Two images cover both userlands the
  bed holds; two parents cover both docker daemons the tests may use
  (`docker_parents`). Image and parent are confounded on purpose, for cost: a
  red cannot be pinned on one or the other without a second run, and the
  record says so.
- **Term and transfer:** the parent's. A container has neither of its own:
  its `run` channel rides the parent's ssh connection and its transfers stage
  through the parent. One cell per element: the parent over ssh with its
  default transfer. The page's "reached over `ssh`" line is reworded for the
  kind ("through its parent, which is reached over ssh").
- **Entries:** `run`, `exec`, `exec-shell`. `exec-shell` builds the *parent*
  with the marking `session_setup` hook, so the parent's `exec` takes its
  pooled shell and the container reports `container/pooled-shell`. It is worth
  a cell: the pooled shell stops, feeds stdin and frames output by a different
  mechanism, and every `exec(expects=…)` on a container takes it.
- **Expected route.** `run`: `default-session`. `exec`: `container/` plus the
  parent's route for the options that actually reach the parent. On a
  container `user=`, `sudo=` and an enclosing `as_user` are an argument of the
  wrapper and do not move the parent; only `expects=` does. `issue()` folds
  all four into one `shell_bound` flag today, so the reasons are kept apart
  and `expected_route` is called for the parent with the parent's reasons (one
  function, no second table).
- **The hook mark.** `exec-isolated` asserts that an `exec-shell` call starts
  with the `session_setup` hook's mark. On a container cell the hook is the
  parent's and its export never reaches the container, and a container host
  has no `session_setup` of its own. The assertion is keyed on "the host under
  test carries the hook", and the parent taking its pooled route is asserted
  by the route alone.
- **Vocabulary:** `POSIX`, as for every GNU and BusyBox cell.
- **Cell facts** (lab hosts read these from `lab.json`; a container states
  them): access user `nobody`, second user `daemon`, not a direct login, no
  password sudo, not a stand-in, `switch_user_may_sudo` true (so the
  `sudo+user` arm runs).
- **The access user is otto's declared user** (`users = {service = "nobody"}`,
  which sets `host.user`), not compose's `user:`. Both images default to root,
  where the `sudo` arm cannot tell an elevation from none. With `nobody`
  declared and the image's own default left at root, a plain `whoami`
  answering `nobody` also proves the declared tier wins over the image's.
- The terminal arm of `exec-isolated` runs for family `unix` only. It does not
  run for a container `exec` (`docker exec -i` has no terminal), which is
  correct and stated in the cell's notes.

Not a cell in this item, each with a tracking issue: a container under a
telnet or local parent (`exec` works there, `run` is refused as a verb); a
container two hops away; containers in the transfer contracts (the parent
design defers transfers; a container has no `_file_transfer` for the batch
and progress contracts to read). The transfer pages get their own sentence,
"not certified for a container yet", which is different from the domain's
"not about this element".

## 2. Publication: the image's userland is the profile

A profile is a userland used as a column of the support matrix. Lab hosts get
theirs from `lab.json`. **One offline descriptor table** in the test tree
states, per container element: image, userland, parent element, access and
second user. Everything below reads it and nothing reads a container through
a lab-only constructor:

| Lab-keyed today | For a container |
| --- | --- |
| `axis_space`, `axes_for`, `_userland`, `discover_profiles` | profile membership from the table |
| `bed_space`: `bed_scratch_dir`, `_unpinned_bed_host`, `host_data` facts | scratch `/tmp`; facts from the table |
| `build_bed_host`'s term and transfer pin; `_require_an_answer` (`RemoteHost.verify_connection`) | the parent is built and checked that way; the container answers one `exec` |
| `vocabulary_for`, `_kind_for_userland` (raise on a new userland by design) | called with the image's userland (`gnu`, `busybox-1.36.1`); the kind comes from the table |
| `profile_for` (observation records) | answers from the table |
| `expected_route`, `entries_of`, `family_of`, `ENTRY_REASON` | the container case, section 1 |
| `HOST_KIND_VOICE`, `HOST_KIND_GLOSS`, `FAMILY_BLURB`/`profile_blurb` | one entry each |
| Unit pins: 55 bed cells, the bed kind set, `_BED_KINDS`, the timeout kinds, the container sentence and its `ValueError` | updated; the last is replaced by a pin of the container route |
| The cancel contract's stimulus table, keyed by unprefixed route | the container rows, section 7 |

The table is static because `discover_profiles()` and `render --check` run
with no bed. The opener checks it against the image when it opens (the
BusyBox banner or its absence), so a table that drifts fails the cell by name.

My call: **container columns are separate**, `container-busybox-1.36.1` and
`container-gnu`. The column id is not the userland string here: the userland
picks the vocabulary, the column says where the evidence came from. A merged
`gnu` column would still keep per-cell evidence, so this is about what a
reader sees at a glance, not about correctness: "is a container certified" is
the question this item exists to answer.

## 3. Lifecycle

- **Started through the product:** `otto.docker.compose_up(build=False)` on a
  small project fixture the conformance tree owns, whose compose file names
  the held image. That is how a user gets a live container host.
- **One stack per element for the whole run, with one owner.** The lane runs
  under xdist, and the conformance tree stamps no `xdist_group` on purpose (a
  distribution hint fails silently; it uses locks). So the owner is the
  process that owns the run: the xdist controller, or the single process when
  the run is not distributed. It starts an element's stack when a test of that
  element is first about to run (not at collection: a run can be deselected
  or `--collect-only`), hands the container id to the workers, and removes
  the stacks at session end. It runs its own event loop for those two steps,
  so no test's loop owns a stack. Cell labels stay free of worker and run ids;
  only the docker resource name carries the unique suffix.
- **One lease, held by that owner for the stack's life**: the parent's daemon
  lease (`lease_unix_host`), the existing rule for an `-e2e-` stack. The
  orphan reaper takes the same lease, so it cannot remove the stack between
  two cells. Openers borrow the stack and take no lease, so the witness's
  second open of the same container is not a second lease.
- **The parent is not reserved.** The lab-host cells for test1 and test3 take
  no pool lease and run beside the container cells on the same hosts. Nothing
  here claims otherwise; the added contention is measured with the lane time.
- **Each open builds a new host object** for the running container (the
  cancel and timeout witnesses read `/proc` through a second one).
- **Teardown is owned and checked.** The owner registers removal before the
  first start can leak, runs `compose_down`, and asserts the stack, its
  network and its staging directory are gone. The `-e2e-` name is for the
  interrupted case only: `docker_reap` and `make bed-reap` sweep it with no
  change to either.
- **A missing image fails the cell by name**, and **the bed provisions both
  images in this item**: the lane is the tip gate for every host diff, so a
  rebuilt bed must not turn every landing red. The parents reach no registry,
  so a make target loads the two images on the docker parents from a pinned
  source (platform and image digest), and the Vagrant provisioning calls it.
  The digest is in the descriptor table the opener checks, so a local tag that
  points at another image fails the cell by name too.
  (`centos:7` is on the bed today only because an unrelated test builds from
  it.)

## 4. Venue: the bed only

`make conformance-bed`, under the lease. No hermetic container cell in this
item: no pull-request check runs docker, the hermetic lane would need an image
pulled on the runner, and the dev VM is short of memory. Follow-up issue. The
cost is that a container regression is caught at the tip gate and not by a
pull request's CI.

## 5. Dispositions

| Today | Disposition |
| --- | --- |
| The six identity mechanisms above. | **Fix**, by the identity design (section 6). |
| Stopping the `docker exec` client leaves the command running in the container (measured). A timed-out or cancelled `exec` does not stop its command, on either parent route: the channel route stops the client, the pooled shell sends Ctrl-C to the client. | **Fix**, section 7. Not a `library` limitation: docker does not forward the stop, and otto can deliver it. |
| `exec`'s log lines go to the parent's log, with the wrapped `docker exec …` command. | **Fix**, section 8. |
| A dropped `docker exec -it` client leaves its shell running on alpine (measured), so `close()` and `rebuild_connections()` leave a shell behind per channel. | **Fix** with section 7's delivery, as its own step: an interactive shell's foreground jobs are in other groups, which the plan measures. |
| The container builds an `ExecInput` (stdin at EOF) and drops it; the parent builds its own. | **Measure** with `exec-stdin`. EOF arrives today; the dropped value is removed or forwarded, whichever the contract shows. |
| The `run` channel uses `BashFrame` on an ash image. | **Measure** with `exec-output-exact` and `exec-framing` on the alpine cell; fix by choosing the frame from the image's shell if red. |
| The container id is not quoted in the `exec` wrapper; it is in two other builders. | **Fix**: one builder for the `docker exec` command line, used by `exec`, the channels and `login`. |

Reds that only the cells will show go in the ledger with raw evidence and get
the parent design's rule: fix what otto's wrapper decides; declare (`library`,
on the owner that really has the behaviour) only what docker makes impossible.
A proposed declaration goes to Chris before it is written.

## 6. Identity and elevation: their own design

Split out by Chris's decision of 2026-11-19, to
`2026-11-19-container-identity-design.md`. It covers the six identity
mechanisms above, `sudo=True` on `run` (one fixed rule), `sudo=True` with
`user=X` (an absent `X` is refused), and named sessions, as one decision. Its
fix lands third (section 10). Until it lands, `exec-options` is red on a
container cell, which is why the cells are published last.

Item 2 of #1172 ("a library-kind refusal cannot be declared") is closed when
that design's option behaviour is implemented and demonstrated, not before.
`HostCapabilities` keeps its device-only check either way.

## 7. Stop: a timed-out or cancelled command is stopped inside the container

The container delivers the stop itself, as a compensating step around
`parent.exec`: on a timed-out result, on a returned session-death error, and
on any exception unwinding the call, on both parent routes.

Measured so far, and what it settles:

- **Identity of the command.** A fresh token passed with `docker exec -e`. It
  is in the environment of the command and everything it starts, and it
  survives the shell's `exec`. (A token as the shell's `$0`, revision 2, is
  lost when the shell execs, which the suite's own stimulus does.)
- **Who delivers.** A second `docker exec` as the command's own user: root in
  a container cannot read another user's `environ`.
- **Group.** The command's shell is a process-group leader, and signalling the
  group stopped all of it on both images as the command's user. One earlier
  run as root left the group running on alpine, so the plan measures this
  again and keeps "signal each member" (measured on both) as the fallback.

Requirements for the plan:

- The sequence is the one rule's (`stop_process`: TERM on a timeout, HUP on a
  cancel, a bounded wait for the *leader* to exit, then KILL), reached through
  an adapter and not restated in shell. `StoppableProcess.send` becomes
  awaitable; `wait` already is.
- A started command is acknowledged, or the stop owns the cancellation until
  it is, as `started_or_stopped` does for the existing routes. A bounded
  search for the token is not enough: a launch that completes after the
  search would escape.
- The command is identified by pid and start time through the wait and the
  KILL, not by pid alone.
- Delivery is bounded as well as the wait. When the stop cannot be delivered
  (the parent connection is what failed), the outcome says the command may
  still be running: in the result where there is one, in the log where the
  call raises.
- Cost: several extra `docker exec` calls on a stop, none on the sunny path.
- **One finder.** Coverage already cleans up inside a container by a token in
  the environment (`OTTO_SCAN_ID`, `src/otto/coverage/objects.py`). The
  `docker exec` invocation and the search for a token's processes are shared
  with it. The two policies stay separate and named: coverage kills every
  process of a scan; a stop follows `stop_process`.

Not measured yet, and each needs its own evidence before the mechanism is
called complete: a command that replaces its environment; a child that keeps
the token after the leader has exited; a cancel that lands before the launch
is acknowledged; both parent routes; a lost parent connection; an interactive
channel's foreground jobs. A token that cannot be read, or is absent, never
means "stopped". The original timeout value or cancellation is preserved, and
an undelivered stop is reported beside it.

The cancel contract's stimulus table and its positive control name routes
without the `container/` prefix today. They gain the container rows, and the
control disables *the container's* stop: the command must survive with it
disabled and die with it enabled, on both parent routes.

## 8. Log: the container's log gets the lines, live

`exec-log` reads the log records of the host under test, and the cancel
contract waits for a line in that log while the command is still running. So
logging `value` after the call is not enough. The parent's `exec` takes a
per-call log context, immutable, carrying who the lines belong to, the command
text to show, the sinks and the already-resolved mode. All three parent routes
use it, and the pooled shell does not fold a mode over it a second time. The
one log-line rule, mode stamps and `NEVER` redaction apply unchanged. Nothing
shared is rebound, so concurrent commands do not mix.

- **The mode is the more restrictive of the two hosts'.** Today the container
  folds its mode, then the parent folds its own over the result. The context
  carries one mode computed once from both, so output a user suppressed on the
  parent does not start appearing because the lines moved.
- **The wrapped `docker exec …` line** stays on the parent at debug level,
  under that same mode (so nothing under `NEVER`).
- **This is a break** for anyone reading the parent's log for container
  commands: the lines move to the container's log.
- **A project's host class may override `_run_plan`** with today's signature.
  The context must not be passed to an override that does not take it: either
  it travels outside the signature, or the dispatcher passes it only where the
  host delegates. The plan picks one and tests it against an override with the
  old signature.
- The probe, `docker cp` and `chown` calls a container makes on its parent are
  not changed here.

## 9. Tests that move

`tests/integration/test_docker_run_get_put.py` asserts exec output, a nonzero
status, run state, timeout recovery and exec concurrency on a container. Each
is deleted when a surface over the container cells makes the same assertion;
its put/get half stays. Its concurrency test stays as well: it asserts that
parallel `exec`s save time, and `exec-concurrent` allows them to be
serialised. The unit tests of the bind (`test_docker_host.py`) are
rewritten with the identity design.

## 10. Landing: publish last

Decided by Chris, 2026-11-19. Each pull request is green alone and "Part of
#935".

1. **Log** (section 8). First, because the cancel contract waits on the
   container's live log.
2. **Stop** (section 7). Before identity, which adds interactive shells to
   clean up.
3. **Identity and elevation** (its own design).
4. **Cells and publication** (sections 1 to 4), with image provisioning: every
   in-scope command surface enabled together, each with its real assertion and
   control from the first day.

The cell harness (opener, descriptor table, route assertions, controls) is
built first on a branch and not landed. It is what shows each fix red before
and green after. Each of 1 to 3 also lands a focused regression that runs
against a real container on the bed.

Considered and rejected: cells first with the unfinished surfaces withheld
("not certified for a container yet"). A surface withheld because it failed
has a known defect and no assertion, so it can get worse unseen; that is a
skip under another name. The parent design already says the container route
is not certified until this item lands. Strict expected failures do not fit
either: the hook marks every test of a module for a cell and entry, so a
passing control becomes an unexpected pass.

The lane's wall time before and after is reported with the cells; the budget
is the lane's 1500 s timeout, which is a watchdog and not a measure.

## Non-goals

- Transfers on a container, a hermetic container cell, non-ssh and multi-hop
  parents: follow-up issues.
- `send`, `expect`, `open_session` and `login` contracts (the parent design's
  non-goals).
- Container lifecycle ownership (`compose_up`/`down` promises): the later
  product-lifecycle design of #935.

## Decisions

Chris, 2026-11-19:

1. Landing order as section 10: harness on a branch, then log, stop,
   identity, and the cells published last.
2. Identity and elevation are their own design, and log and stop go ahead.
3. `run(sudo=True)` on a container gets one fixed rule.

My calls, agreed by all three reviewers and open to reversal:

- Separate `container-…` columns; the bed venue only (sections 2 and 4).
- Image provisioning lands with the cells, from a pinned source (section 3).
- Container commands log to the container, under the more restrictive of the
  two hosts' modes, with the wrapped line on the parent at debug level
  (section 8).
