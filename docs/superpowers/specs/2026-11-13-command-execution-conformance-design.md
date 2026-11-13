# Behaviour promises conformed on every route (#935) — design

Status: approved by the maintainer, 2026-11-13, with the three decisions
recorded at the end. Reviewed twice by two independent reviewers before
approval.

Scope: the mechanism, and its first tenant, **command execution** (`run` and
`exec`). Transfers, product lifecycle, entry-point parity and extension seams
follow as their own designs, in that order, on the same mechanism.

## Why

The 2026-11-12 review found otto's most severe breaks were silent behaviour
changes, and that nothing records behaviour. The cause is one level down: a
promise is written once in prose, then **decided once per route**, and each
copy is tested by its own tests against its own idea of the promise.

Command execution, as it stands:

- **Which route an `exec` takes is decided in five places.**
  `SessionManager._exec_route` (`session.py:3726`) is documented as the one
  decider, but `EmbeddedHost._exec_one` calls `run_cmd` directly
  (`embedded_host.py:411`), `LocalHost._exec_one` calls its subprocess
  directly (`local_host.py:256`), a container sends the call to its parent
  (`docker_host.py:1121`), and `UnixHost` picks the console and the
  direct-credential connection before the manager is asked
  (`unix_host.py:1064-1077`).
- **What `value` holds is decided three times**: `output_line` (ssh channel
  and local exec), `BashFrame.parse_output`, and `ZephyrFrame.parse_output`
  (`"\n".join(output).strip()`, a different rule, written into the docs as an
  exception with no device reason). The custom frame in
  `tests/custom_hosts/custom_hosts/zephyr_inline.py:127` is a fourth.
- **What a timed-out result holds is built three times** (`session.py:1017`,
  `session.py:3526`, `local_host.py:353`) and the `value` differs: two always
  append a newline, one does not.
- **The exact-output table exists three times**:
  `tests/unit/host/test_exact_shell_output.py` (`TABLE`),
  `tests/unit/host/test_session.py:2335` (`_EXEC_LINE_ROWS`) and
  `tests/e2e/console/test_exact_output_e2e.py`.
- **The one suite parametrised over hosts never calls `exec`.**
  `tests/conformance/` has no `exec` call in any test; its framing contract
  compares one multi-line value. A second, older suite
  (`tests/integration/host/test_host_contract.py`) asserts an overlapping
  subset with per-backend kits.

So `exec` on a pooled shell returns its session to the pool carrying the
command's `cd` and `export`, while `exec` on the ssh channel does not, and no
test can see it.

## The rules (Chris, 2026-11-12 and 11-13)

1. **One rule everywhere.** A promise holds on every host, term, frame and
   route unless the device makes it impossible.
2. **Code otto implements conforms 100%.** The only difference otto's own
   code may declare is "the device cannot do it".
3. **Library- or protocol-backed code may differ only where the difference is
   intrinsic**, and then otto states how and why, in data, shown in the docs.
4. **An option otto exposes has the same effect everywhere.** Where the
   device cannot do it, the call is refused by name. A missing library
   feature is not an exemption, and neither is otto's own wrapper around a
   library.
5. **A red conformance cell is resolved one of two ways:** fix the product,
   or declare the difference. Never soften the assertion.

## The approach: consolidate, then conform

Two steps per promise:

1. **Consolidate the decision.** Each decision named above gets one function
   that every route calls: one exec-route resolver, one "output to `value`"
   rule, one timed-out-result builder, one log-line rule, one signal-exit
   mapping (`_bridged_exit_status`, `interact.py:866`, already exists and is
   reused). Where the copies disagree today, the consolidation takes the
   ideal and the disagreement is a fix. Parity then holds by construction:
   `AGENTS.md`'s ONE DECISION, ONE PLACE applied to the cause.
2. **Conform the wiring.** One parametrised contract per promise, over every
   cell and entry, proves each route reaches the shared decision. The
   contract is written first and seen red where a route diverges.

## What already exists and is extended

| Piece | Where | Role here |
| --- | --- | --- |
| Surfaces | `tests/_fixtures/support_matrix.py`, `SURFACES` | **A promise is a surface.** A new promise is one row, one `VOICE` entry, one test, one positive control. No new catalog. |
| Promise wording and arms | `scripts/render_support_matrix.py`, `VOICE`, `Branch` | `Branch` already publishes "the contract watched something else on this device", chosen by the observable the test records. It is the publication for a declared difference. |
| Cells | `tests/conformance/` | `(element, term, transfer)` cells in a hermetic and a bed venue; per-userland `Vocabulary`. |
| Domain / known failure | `applicable_cell`, `expected_failure` | "Not about this element" and "broken here, strict xfail". |
| Declared capability | `HostCapabilities` (read by the product to refuse `user=`), `CommandFrame.single_client_console`, `TransferMetadata` | Typed declarations on the thing that owns the behaviour. |
| Evidence | observation records → `schemas/support-matrix.schema.json` → `docs/architecture/support-matrix.md` | Per profile and surface. |

## What the mechanism adds

### 1. The entry is part of the cell

A command contract runs over every way a caller reaches the promise. The
entry is a field of `ResolvedCell` and of `cell_label`, resolved into the
cell's opener. It is **not** a second pytest axis: one observation record and
one positive-control citation per cell stay true, and an entry that needs a
differently built host can have one.

| Entry | Call | Where |
| --- | --- | --- |
| `run` | `host.run(cmd)` | every cell |
| `exec` | `host.exec(cmd)` | every cell except hermetic BusyBox-artifact cells, whose applets are on the session's `PATH` only (`_cells.py:333`); an `exec` there would measure GNU under a BusyBox label |
| `exec-shell` | `host.exec(cmd)` on a host built with a no-op `session_setup` hook | ssh cells; forces the pooled shell, as `test_exact_output_e2e.py:209` does today |

**A contract states the axes and entries it reads.** For command contracts
the space is reduced to one representative full cell per `(element, term)`
*before* sampling (so the schema's required `transfer` stays satisfied and
the draw is not biased toward hosts with more transfers), then crossed with
the entries the contract reads. A `run`-only contract says so there, not in
its domain: a domain that excludes one entry of an element makes the collator
refuse the element (`collate_support_matrix.py:547`). Domains stay for whole
elements.

**The route is recorded by the call, not predicted.** The consolidated
resolver returns a plan for one call: the route, the connection it uses, and
for a container its parent's plan. The dispatch consumes that plan and
nothing else reaches a primitive. The route that ran is readable from the
result of that call, so a contract asserts it for the call it made instead of
asking again. Each cell states the route it expects; the positive control
fails if they differ. My call: a read-only `route` on `CommandResult`. It is
additive and also answers a user's "which route ran my `exec`".

**The evidence format changes with it**, all in scope of the first pull
request:

- `entry` in the observation identity and filename (`_observation.py:329`,
  `:360`, `:481`), required on new command evidence;
- the draw cache keyed by projection (`conftest.py:85`, `:104-135`);
- label and control enumeration per contract (`support_matrix.py:445-499`,
  `:532`), exclusion provenance, and migration of stale records;
- the schema, the collator and the renderer's split.

### 2. A declared limitation: typed, owned, with provenance

The existing declarations are typed fields on the owner (`UserSupport` on a
host family, a `ClassVar` on a frame), and the product reads them. This
design keeps that shape and adds what it lacks: **why**, and **on whose
authority**.

```python
@dataclass(frozen=True)
class Limitation:
    kind: str               # "device" | "library"
    reason: str             # why the ideal cannot hold; never empty
    measured_on: list[str]  # what it was measured against; never empty
```

- **Owners.** A host family (`HostCapabilities`), a command frame (its own
  class body), and an exec route. Routes get one record each in a table
  beside the resolver: who implements it (`otto`, or the library it delegates
  to: the ssh channel is asyncssh's, a container exec is `docker exec`) and
  its limitations. Term- and route-level facts (the console's single
  session, a line-disciplined route's line budget) belong to the route
  record.
- **Each new capability field defines its ideal value.** At the ideal it
  carries no `Limitation`; at any other value it must. Checked at
  construction, and for frames in `_check_frame_class`
  (`command_frame.py:911`, shared by eager and lazy registration, so a
  project's frame is checked too). The check covers the fields this design
  adds. The existing `user=` and transfer fields are brought under it by the
  designs that give them cells.
- **A frame's declaration is not inherited.** `AshFrame`,
  `ZephyrSerialFrame` and a project's subclass declare their own: the
  measurement was not taken on them.
- **The product reads the capability where it acts on it.** Example: a frame
  states whether it can run a command isolated from its session, and the
  shell-routed `exec` framing reads that. The contract reads the same field
  to choose its arm.
- **Declared means asserted.** The arm asserts what holds instead, with a
  witness that tells it apart from the ideal, so a limitation that stops
  being true goes red.
- **Declared means published, per cell.** Today one observable is stored per
  surface and profile, and the renderer requires it to match exactly one
  `Branch` (`render_support_matrix.py:1235`). A limitation that applies to
  one term or entry of a device would fail the docs build. So each observed
  cell stores the arm it ran, the renderer selects the branch per cell, and
  the pin that forbids mixed arms (`tests/unit/test_support_matrix.py:2901`,
  `:6306`) is revised. A branch names the declaration it speaks for, and the
  page prints that declaration's reason and measurement. An arm that can only
  be asserted at unit level (an `unknown(...)` terminal mapping) is published
  as declared and unit-asserted, with no bed evidence claimed.

**What the `kind` check can and cannot do.** A test refuses
`kind="library"` on an owner implemented by otto, and no label exempts an
option (rule 4). Whether a `device` claim is true is not testable. It needs
the measurement on the record and a reviewer, and this design treats every
`device` declaration below as **unproven until measured**.

### 3. One table per promise, shipped

The exact-output cases live once, in `otto.testing`: the bytes a program
writes and the `value` the rule gives, including invalid UTF-8 and a long
line with no newline. Consumers:

- the unit tests of the shared value function and of each frame (the ssh
  reader-chunk rows, `test_session.py:2345`, are about the reader and stay
  where they are);
- the conformance contract, over every cell and entry whose vocabulary can
  print chosen bytes;
- a new kit, `assert_command_frame_conforms(cls, transcript=...)`. A frame's
  author supplies `transcript`: given a program's output and exit code, the
  bytes their device sends back. The kit proves the frame recovers the
  table's `value` from it. In tree, each frame's transcript model is checked
  against a recorded bed measurement, and the custom Zephyr frame under
  `tests/custom_hosts` runs the kit like any project's would.

### 4. The contract page names its tests

For command execution the page is `docs/architecture/utilities/results.md`.
It includes a **generated file** (the families page is the precedent: a whole
committed file written and `--check`ed by `render_support_matrix.py`): one
row per surface with the promise, the test that proves it, and each declared
limitation. The hand-written prose states inputs, outputs, exit codes and
what is undefined.

## First tenant: command execution

### Promises

Existing surfaces, now over every entry: `exec-exit-code`, `exec-framing`,
`exec-failure-in-sequence` (`run` only), `timeout`.

New surfaces, with the ideal each one fixes:

| Id | Ideal |
| --- | --- |
| `exec-output-exact` | `value` is the output with exactly one trailing newline removed, for every row of the table. |
| `exec-isolated` | An `exec`'s session state (cwd, an exported variable, a shell option) does not depend on any earlier call and is not visible to any later one: asserted `run`→`exec`, `exec`→`exec`, `exec`→`run`. The effective user and the host's `session_setup` are part of every `exec`'s starting state. Files it writes are not session state. |
| `run-keeps-state` | What one `run` does to its session is visible to the next `run` on it. |
| `exec-concurrent` | Concurrent `exec` calls each return their own command's result. (Safety only. Whether they run at the same time is a declared property of the route.) |
| `exec-signal-exit` | A command killed by signal N reports 128+N on every route that has signals. |
| `exec-stdin` | The command of an `exec` without `expects=` reads end of file on stdin (after any credential prompt otto itself answers). |
| `exec-cancel` | Cancelling the awaiting task stops the command and leaves the host usable. |
| `exec-options` | `expects=`, `sudo=`, `user=`: the effect on every entry, or a refusal by name from a declared limitation. Includes `ShellCommand` precedence, an enclosing `as_user()`, and the combinations. |
| `exec-log` | The host log receives the lines of `value`, the same for the same command and `log=` mode on every entry. |
| `exec-dry-run` | A dry run reaches no device and returns the same result shape on every entry. |

`timeout` gains: the same `value` shape on every entry, the command is no
longer running afterwards, and the call returns within its budget when the
command closes its output and keeps running.

### Known differences today, and the disposition

Read from the code. Each is seen red in its cell, or its unit arm, before it
is fixed. "Measure" means: no declaration is accepted until the measurement
shows the device cannot do it; if it can, the row is a fix.

| Today | Disposition |
| --- | --- |
| Zephyr frames trim both ends, delete every `\r`, and strip ANSI from the program's own output (`command_frame.py:785`, `:798`). | **Measure, then fix.** The frame removes exactly what the Zephyr shell adds. The first plan task records raw transcripts on the three bed releases. `region_before_end` is a documented subclass helper, so the fix is breaking for a project that subclasses it. |
| Pooled-shell `exec` leaks session state to the next `exec` (`session.py:3709`). | **Fix**: a shell-routed `exec` is framed so its state cannot outlive it. |
| A command that changes its terminal on a pooled session changes the next `exec`'s output. | **Fix**: the session's terminal settings are restored when it is reused. (Re-measuring the mapping would only classify the damage.) |
| `exec` on a `console` term runs on `run`'s session (`session.py:3601`): it sees what `run` left. | Outbound: **fix**, as above. Inbound on a POSIX console: **measure** whether a fresh login-shell child on the same line gives the starting state; if so, fix. Zephyr: measure, expected to be a `device` limitation both ways. |
| Zephyr `exec` takes no lock (`embedded_host.py:411`); the unix console does (`unix_host.py:1087`). | **Fix**: one console-lock decision for both. Reproduced at unit level, not on the bed (#260 wedge risk). |
| Timed-out `value` differs by route. After a timeout, local exec terminates only the shell (`local_host.py:340`) and a shell route sends Ctrl-C (`session.py:1014`). | **Fix** by the shared builder, and one "the command is stopped" rule. |
| Cancelling a shell-routed `exec` only marks the session for recovery (`session.py:1031`); a pooled session returns to the pool with the command still running (`:3710`). | **Fix.** |
| Killed by a signal: the channel reports `exit_status or 0` (`session.py:3553`), local reports a negative code where `-1` means "never ran" (`local_host.py:368`), a shell reports 128+N. | **Fix** by the existing mapping. Zephyr has no signals: out of the surface's domain. |
| stdin is `/dev/null` on the channel, otto's own stdin on local exec (`local_host.py:313`), the pty on shell routes. | **Fix** to the ideal. |
| `exec` bounds the output drain but not the wait for exit (`session.py:3552`, `local_host.py:359`). Local exec's line reader has a 64 KiB limit (`local_host.py:308`). | **Fix.** |
| Local `exec` refuses `expects=` and `sudo=` (`local_host.py:267-282`); local `run` honours them. | **Fix**: a local `exec` that needs a shell takes a shell route, as a unix host's does. |
| `LocalHost` refuses `user=` on `run` and `exec` (`local_host.py:226`, `:258`) while `as_user()` switches the same session. | **Fix** both verbs (rule 4). It needs the local host to resolve a credential for the target user. |
| Containers reject `sudo=` with `user=` and bind `run`'s identity when the channel opens (`docker_host.py:1044`, `:1160`). | **Measure** against `docker exec`; fix what otto's wrapper decides, declare (`library`) only what docker does. With the container cells. |
| Embedded hosts refuse `user=` and have no `sudo`. | **Declare** (`device`). `HostCapabilities` gains `sudo` and `expects` declarations beside its `user=` ones. |
| The shell route's log drops blank lines and `\r`; the channel logs every line (`session.py:1146-1183`, `:2712`). Buffered frames log at the end. | **Fix** the content by one log-line rule. End-of-command logging: **measure**; it is an implementation choice today (`session.py:1180`), and is declared only for a frame whose output cannot be delimited before its end marker. |
| Dry-run `exec` passes the call's `log`, not the resolved command's (`host.py:2230`). | **Fix.** |
| Long command lines are refused on line-disciplined routes and not on the channel (`exec_line_budget`). | **Declare** (`device`), on the route record; already measured and refused by name. |
| A bash/ash session whose terminal mapping is `unknown(...)`. | "Measured and not invertible": **declare**, unit-asserted. "No calibration line came back" (`terminal_mapping.py:99`) is not a device fact: **measure** which targets produce it and fix or refuse. |

### Tests that move

A test is deleted only when a surface now makes its assertion.

- The three output tables → the shipped table.
- `test_exact_output_e2e.py`: its cells move to bed conformance. Its log
  check moves with `exec-log`. Its 20 repetitions (the #711 timing defect)
  stay as one focused ssh-channel test.
- `test_host_contract.py`: the run/exec status tests move. Its send/expect
  test and its transfer half stay until a surface covers them.

### Landing order

Each pull request consolidates the decisions its surfaces touch, then
conforms them. Each is green alone.

1. **Route, output, mechanism** (#935 proper): the exec-route resolver, plan
   and dispatch; the shared value function; entry-in-cell, projection, and
   the evidence-format changes; `Limitation` and per-cell arms; the shipped
   table and frame kit; `exec-output-exact` and the existing four surfaces
   over every entry; the Zephyr measurement and fix; the generated contract
   table.
2. **State, concurrency, time:** `exec-isolated`, `run-keeps-state`,
   `exec-concurrent`, `exec-signal-exit`, `exec-stdin`, `exec-cancel`, the
   `timeout` additions.
3. **Options and logging:** `exec-options`, `exec-log`, `exec-dry-run`.
4. **Container cells.** A container is not a lab element, so it has no
   profile column and no lifecycle in this suite today. Its own item: cell
   identity, the image's userland as its profile, lifecycle through the
   existing docker fixtures and `docker_parents`, under the bed lease.
   **Until it lands, the container route is not certified by any surface
   above**, and the generated table says so.

## Non-goals

- Transfers, product lifecycle, CLI entry-point parity, other extension
  seams, `send`/`expect` and `open_session`: later designs.
- The `retcode == -1` sentinel colliding with a real exit code, beyond the
  signal case: an API question, its own issue.
- Folding hermetic observations into the support-matrix page.
- Raising any namespace to `stable` (see Decisions).

## Anti-goals

- A second promise catalog or a second publication path.
- A limitation used to buy a green.
- An assertion weakened to fit a route. The stimulus may vary by userland;
  the asserted property may not.
- A conformance test that passes because the route under test was never
  taken.

## Evidence

- Hermetic: `make conformance` with `OTTO_CONFORMANCE_CELLS=all`.
- Bed: `make conformance-bed`, under the lease, coordinated with peers. The
  lane's wall time before and after is reported in each pull request.
- Zephyr and console measurements recorded as raw `repr()` transcripts.
- Each fix is seen red first.

## Left to the builder

- Where `Limitation` lives and the field names of each capability.
- The plan's shape, given that the dispatch consumes it and the result
  reports it.
- How a shell-routed `exec` isolates state and restores the terminal on
  POSIX, at no extra round trip on the sunny path.
- Stimuli: a signal stimulus must not kill the session's own shell.

## Decisions (Chris, 2026-11-13)

1. **The stable label waits.** #935 says done when the namespace is `stable`
   in `api/public.toml`. The 2026-11-07 keyword-only design says no stable
   label until most interface-stability issues are resolved, and the manifest
   is namespace-granular (per-symbol tiers are #654). The command part of
   #935 is done when the page, the named tests and the declared limitations
   land; the label waits for #654.
2. **A console's `exec` is measured before anything is declared.** A console
   serves one client, so `exec` cannot have its own session. If a fresh login
   shell on the same line reproduces the starting state at no extra round
   trip, that is the fix. If it costs a round trip or a login per `exec`, the
   measured cost goes back to the maintainer before choosing between the cost
   and a declared limitation.
3. **The breaking changes are accepted**, each as `fix!` or `feat!`: Zephyr
   `value` no longer trimmed; shell-routed `exec` no longer leaks state;
   signal exit codes change on the channel and on local exec; local `exec`
   gets end-of-file on stdin; timed-out `value` text changes on one route;
   `LocalHost` accepts `user=`; `CommandResult` gains `route`.
