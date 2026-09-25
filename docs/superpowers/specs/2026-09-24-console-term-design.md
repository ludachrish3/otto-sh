# Console term: serial consoles behind a telnet server — design

**Date:** 2026-09-24
**Source:** Chris. Add console access for unix and embedded hosts where the
console may present a login and password, reached through a telnet server
named by lab host ID and port; fail loudly when the console is already logged
in; plan the bed's serial wiring and prove it with e2e tests.

## Problem

otto has two terms, `ssh` and `telnet`. A device whose only shell is a serial
console is reachable today only when the UART is bridged by QEMU's
`-serial telnet:` and the device has no login, which is the three ARM Zephyr
bed guests. Nothing serves the common real-world shape: a serial console
server (ser2net, a Lantronix/Digi box, a QEMU bridge) fronting a UART that a
getty answers with `login:`.

The telnet term cannot be stretched to it:

1. Its login reads up to a single delimiter byte (`b":"`) with no failure
   detection. On a line that is already logged in there is no `login:`; the
   read blocks until the marker handshake times out, and the error says the
   shell never became ready — true, useless.
2. Its target is the host's own `ip` and `telnet_options.port`. A console
   port belongs to the *server*, not to the device; the device may have no
   network at all.
3. A serial line is single-client and stateful. Whoever was there last is
   still there. otto must never proceed on a session it did not open.

## Decisions (adjudicated with Chris, 2026-09-24)

- **A third term, `console`.** Not an extension of telnet and not a hop
  variant. Serves `unix` and `embedded`; `authenticates=True`.
- **Addressing: a lab host ID plus a port.** `console_options.server` names
  the host running the telnet server, `console_options.port` its port.
- **Two dial modes, default `ssh`.** `dial: "ssh"` tunnels *into* the server
  host (through the server's own hop chain, any depth) and forwards
  `localhost:port` from there, so a listener bound on the server's loopback
  is reachable — today's Zephyr shape. `dial: "direct"` connects to
  `server.ip:port` through the server's hop chain, for a console appliance
  with no SSH. The console host's own `hop` is not consulted by this term.
- **Already logged in: fail loudly, always.** No automatic logout on the
  scripted path. A host-named `ConsoleError` quotes what the line showed.
- **A bounded, observable reset.** `otto host <id> logout` and
  `otto host <id> login --force` run one reset sequence that ends with
  `login:` observed or a loud failure. The operator chooses to reset; otto
  never chooses for them.
- **Login runs in the transport slot**, before the landing handshake, where
  ssh auth and the telnet login run today. A `login:` prompt is not a
  dialect: it answers no handshake, so nothing can land there. For a console
  whose line must be driven through a boot menu before Linux reaches
  `login:`, the raw-landing hook path applies, and a `session.console_login()`
  helper runs the same state machine from inside the hook.
- **Prompt patterns come from the OS profile, overridable per host.**
  `unix` and `busybox` carry `login: ?$` / `[Pp]assword: ?$`; `embedded` and
  `zephyr` carry none and force `login=False`.
- **Bed: three console rows.** A VirtualBox UART link test2→test1 served by
  ser2net (GNU getty), a second QEMU UART on the bb1350 BusyBox guest
  (BusyBox getty), and the existing ARM Zephyr `-serial telnet:` guests
  migrated to the console term (no login).
  **Correction (2026-09-25, proven by the implementation):** the GNU row's link is a socat null-modem over the lab network, not a
  VirtualBox UART (see §7), and the three kinds are five console *hosts*:
  test2, bb1350 and the three ARM Zephyr guests.
- **Not a breaking change.** The telnet term is byte-for-byte unchanged, so
  an existing `-serial telnet:` Zephyr host on `term: telnet` keeps working.
  Only otto's own bed entries migrate. Minor bump.

## Hard constraints

- otto never invents identity. It logs in only with the host's cred and only
  from a `login:` prompt it observed. It never completes someone else's
  half-typed login and never acts on a session it did not open unless the
  operator asked for a reset by name.
- Passwords never appear in any error, log line, or quoted buffer. Usernames
  may.
- A console is single-client. `run` and the `shell` transfer use the default
  session; `exec` and every named session are refused. The transfers that
  reach the device over its own network (`scp`, `sftp`, `ftp`, `nc`) are
  unaffected by the term.
  **Correction (2026-09-25, proven by the implementation):** `nc` is not unaffected: its remote side is a command (`nc -l`) that
  holds the one session while its control commands must run beside it, so
  `nc` is refused on a console term. A `transfer: nc` pin (or a menu of only
  `nc`) is a load-time config error; an `nc` preference falls through to the
  next kind. `scp`, `sftp` and `ftp` stay unaffected (their remote side is a
  daemon).
  **Correction (h) (2026-09-25, Chris's ruling):** `exec` is not refused. It is a
  special case of `run`: on a console term it runs its one command on the
  default session (the one `run` uses), with `run`'s marker and exit-code
  handling, and returns a normal `CommandResult`. Concurrent `exec` calls on
  one console host run one at a time. The exec pool is never built on a
  console, because a pool session is a second connection. Named sessions
  stay refused, and so does `nc`, which needs two sessions at once. With
  `exec` working, otto's own commands (file ops, userland probes, transfer
  steps) call `exec` directly; no separate internal chooser is needed.
- `otto.host.console` stays off the CLI startup import graph (lazy import
  from the session manager, as `session_setup` is). The import budget does
  not move.
- Every failure the state machine can reach is a distinct `ConsoleError`
  message naming host, server, port and dial mode, because each has a
  different fix.

## Design

### 1. Term registration and dispatch (`otto.host.connections`, `otto.host.session`)

```python
register_term_backend(
    "console", ConnectionManager,
    host_families=frozenset({"unix", "embedded"}), authenticates=True,
)
```

`SessionManager` has two hard-coded `match self._connections.term` sites
(`_build_session` and `open_session`). They collapse into one
`_open_transport_session(kind)` helper that knows `ssh`, `telnet` and
`console`. For `console` it calls `ConnectionManager.console()` (default
session) and builds a `TelnetSession` over the client's streams with the
console's `write_chunk_size` / `write_chunk_delay`, exactly as telnet does.
A named-session request on a console term raises `ConsoleError`:

```
test2: console is single-client (test1:4001, dial=ssh); exec and named
sessions are not available on this term — use run(), or select another term
```

**Correction (h) (2026-09-25, Chris's ruling):** only named sessions are refused.
`exec` never reaches this build on a console: it runs on the default session
(see the hard constraints). The message reads `named sessions are not
available on this term — use run() or exec(), or select another term`.

### 2. Options (`otto.host.options.ConsoleOptions`, `otto.models.options.ConsoleOptionsSpec`)

Runtime dataclass and pydantic spec, paired in `OPTION_SPEC_RUNTIME_PAIRS`
so the drift guard holds. `console_options` lives on the shared `HostSpec`
beside `telnet_options`, so both families carry it.

| Field | Default | Meaning |
|---|---|---|
| `server` | `""` | Lab host ID of the telnet server. Required when the active term is `console`. |
| `port` | `0` | Console port on the server. Required, `1..65535`. |
| `dial` | `"ssh"` | `"ssh"`: tunnel into the server, forward `localhost:port`. `"direct"`: connect to `server.ip:port` through the server's hop chain. |
| `login` | `True` | Run the login state machine. Embedded hosts force `False`. |
| `login_prompt` | `None` | Regex; `None` means the OS profile default. |
| `password_prompt` | `None` | Regex; `None` means the OS profile default. |
| `login_timeout` | `10.0` | Seconds to wait for each prompt after a nudge. |
| `settle` | `0.5` | Quiet window read and discarded after connect. |
| `logout` | `True` | Send EOF on close and wait briefly for `login:`. |
| `write_chunk_size` | `0` | As `telnet_options`. |
| `write_chunk_delay` | `0.0` | As `telnet_options`. |
| `cols`, `rows` | `400`, `24` | As `telnet_options`. |
| `encoding` | `False` | As `telnet_options`. |
| `echo_negotiation_timeout` | `3.0` | As `telnet_options`. |
| `extra` | `{}` | Passed to `telnetlib3.open_connection`. |

Validation at lab load (`otto.models.host`, alongside the `hop` checks):

- `term == "console"` (pinned or resolved) requires non-empty `server` and a
  valid `port`.
- `server` must be a host in the lab and must not be the host itself.
- `dial == "ssh"` requires the server to carry a cred usable for `ssh`,
  checked when the lab is built, the way a hop's cred is resolved.
- `login_prompt` / `password_prompt`, when given, must compile.
- `login: true` on an embedded host is accepted and overridden to `False`
  at construction, matching the telnet behaviour, with a debug log line.

**Correction (i) (2026-09-25, planning ruling):** the server checks in bullets 2 and 3 (the
server is a host in the lab, is not the host itself, and carries an ssh cred for `dial: ssh`)
run at connect-time pre-flight, the way a hop resolves, not at lab load: the repo has no
lab-load cross-host validation layer. A bad server surfaces at the first connection, and the
error still names the host and the server.

`TermContext` gains `console_options`. `RemoteHost` gains
`console_options: ConsoleOptions`, the JSON schemas regenerate via
`make schema`, and `test_host_spec_fields_match_runtime_init` pins the pair.

### 3. Addressing (`otto.host.remote_host`, `otto.host.connections`)

`_build_hop_transport()` is parametrised on the hop id
(`_build_hop_transport(hop_id=self.hop)`), so the same recursion, cred
lookup and cycle check can be pointed at the console server. At host
construction, when the active term is `console`:

- `dial == "ssh"`: `console_hop = self._build_hop_transport(hop_id=server)`
  and the console target is `TelnetTarget("localhost", await
  console_hop.forward_port("localhost", port))`. The forward's destination
  resolves on the server side, so a listener on the server's loopback works.
- `dial == "direct"`: the console target is `TelnetTarget(server.ip, port)`
  when the server has no hop, else `("localhost", forward_port(server.ip,
  port))` through `server._build_hop_transport()`.

`ConnectionManager.console_target()` returns that target;
`ConnectionManager.console()` opens and caches the `ConsoleClient` the way
`telnet()` does, tearing down on any exception during `connect()`. `close()`
cascades into the console hop transport as it does for `hop`.

### 4. The client (`otto.host.console.ConsoleClient`)

A dataclass with the same shape as `TelnetClient` (`host`, `user`,
`password`, `options`, `connect_port`, `reader`, `writer`, `alive`,
`close()`) — **Correction (2026-09-25, proven by the implementation):** the dial port field is `port`, not `connect_port` — reusing telnetlib3's open and the DONT ECHO negotiation, and
always registering as a single-client console transport. What differs is
`connect()`:

1. **Open** the target.
2. **Settle**: read for `settle` seconds and discard.
3. **No login** (`login=False`): return. The marker handshake is the
   readiness check.
4. **Nudge**: write `\r`; collect until, bounded by `login_timeout`:
   - buffer ends in `login_prompt` → step 5;
   - buffer ends in `password_prompt` → fail *stuck at a password prompt*;
   - EOF from the server → fail *busy* (the server closed the connection;
     another client holds the port);
   - timeout, zero bytes → fail *silent* (wrong port, no getty, dead cable);
   - timeout, bytes but no prompt → fail *already logged in*, quoting the
     last 200 bytes ANSI-stripped and `repr`-escaped.
5. **Username**: write `<login>\r`; collect until `password_prompt` (step
   6), `login_prompt` again → fail *login refused for '<user>'*, or timeout
   → fail *no password prompt after username*.
6. **Password**: write `<password>\r`; return. The marker handshake follows.
   When it times out with the buffer ending in `login_prompt`, the session
   layer's existing failed-login branch names the console, not telnet.
7. **Close**: when otto logged in and `logout=True`, write EOF (`\x04`) and
   wait up to `login_timeout` for `login_prompt`; log a warning if it does
   not come. Never raises. `abort` paths skip it.

`reset()` is the bounded operator sequence, on an open client at step 4's
starting point:

1. Nudge `\r`. At `login_prompt` → done. At `password_prompt` → `\r`, wait
   for `login_prompt` (getty prints "Login incorrect" and re-prompts) → done.
   **Correction (2026-09-25, proven by the implementation):** at `password_prompt` the reset sends Ctrl-C (`\x03`), not `\r`:
   `login(1)` exits and getty respawns a clean prompt, whereas a CR leaves
   `login(1)`'s own retry prompt, which answers the next client's nudge with
   `Password:` (bed-proven). And a `login_prompt` is confirmed by a *second*
   nudge before it counts as done, because only getty's prompt re-prints on
   CR; `login(1)`'s retry prompt takes it as an empty username and asks for a
   password. In step 2 each round writes Ctrl-C, waits (bounded) for the
   interrupt to show, then Ctrl-D and CR, since a tty's Ctrl-C flushes a
   Ctrl-D sent before it lands.
2. Up to three rounds of `\x03`, `\x04`, `\r`, then classify; each round
   ends as soon as `login_prompt` appears. Three rounds cover a nested shell
   and an `su` level. EOF rather than `exit` because EOF ends bash, sh,
   python and most REPLs, while `exit` is a shell builtin.
3. After three rounds → fail *could not reset console*, quoting the last 200
   bytes. A pager or an editor does not answer either key; that is the
   bounded failure.

A `ConsoleState` enum (`AT_LOGIN`, `AT_PASSWORD`, `LOGGED_IN`, `SILENT`,
`BUSY`) is what `classify()` returns; the health probe reports it by name.

`ConsoleError(OttoError, ConnectionError)` lives in `otto.host.errors`. Its
message prefix is always `<host>: console <server>:<port> (dial=<mode>) `.

### 5. OS profile prompt defaults (`otto.host.os_profile`)

`OsProfile` gains `login_prompt: str | None = None` and
`password_prompt: str | None = None`; `register_os_profile` accepts both as
keywords and compiles them at registration so a bad pattern fails at init
time. Built-ins:

| Profile | `login_prompt` | `password_prompt` |
|---|---|---|
| `unix`, `busybox` | `login: ?$` | `[Pp]assword: ?$` |
| `embedded`, `zephyr` | `None` | `None` |

Resolution at host construction: `console_options.login_prompt` if set,
else the profile's, else `ConsoleError` at connect time when `login=True`
("no login prompt pattern: set console_options.login_prompt or give the OS
profile one").

### 6. Host verbs (`otto.host.host`, `otto.host.unix_host`, `otto.host.embedded_host`)

- `Host.login(user=None, force=False)`. `--force` synthesises through
  `@cli_exposed`. On a console term it opens the client, runs `reset()`,
  then the scripted login, then hands the streams to the interactive bridge
  (`run_console_login`, a sibling of `run_telnet_login` using
  `ConsoleClient.connect(interactive=True)`); on bridge exit it runs step 7.
  Without `force` the scripted login runs as usual and fails loudly. On ssh
  or telnet, `force=True` raises `ValueError("--force applies to console
  hosts only")`.
- `Host.logout()`, `@cli_exposed` as `otto host <id> logout`. Console term
  only; on other terms it raises the same shape of `ValueError`. Opens the
  client, runs `reset()`, prints the outcome (`already at login prompt` /
  `reset: login prompt restored` / the failure), closes. On a `login=False`
  console it reports "this console has no login" and does nothing.
  **Correction (2026-09-25, proven by the implementation):** `logout()` prints nothing: it returns a `Result` whose value is the
  outcome line (a dry run returns `NotRun`), and the CLI renders it; a
  failure is the raised `ConsoleError`. It also closes otto's own session on
  the host first, as `login --force` does.
- `HostSession.console_login()`: runs steps 4–6 with the host's console
  cred on the session's own streams. Refused with `ConsoleError` on a
  non-console term. Intended for a `landing_frame: "raw"` +
  `session_setup` hook that drives a boot menu first; such a host declares
  `login: false` so the transport does not also try.
  **Correction (2026-09-25, proven by the implementation):** the refusal names the host
  (`<host>: console_login() needs a console term; ...`).

`EmbeddedHost._login` stays `NotImplementedError`; `logout` on an embedded
host reports "no login".

### 7. Bed (Vagrantfile, `scripts/build_busybox_guest_images.py`, `scripts/lab_health.py`)

**GNU row — test2's UART cabled to test1's UART.**

**Correction (2026-09-25, proven by the implementation):** the bed spike found no usable UART: VirtualBox's UART is an x86
16550A at I/O `0x3f8` and the arm64 guests have no ISA bus. The shipped
fallback is the socat null-modem below: test2 holds a pty `/dev/ttyV0` with
agetty on it and listens on `10.10.200.12:4102`; test1 dials it into its own
`/dev/ttyV0` and ser2net serves that on port 4001. test1 retries the dial,
so boot order does not matter. The VirtualBox bullets that follow were not
built.

- test1: `vb.customize ["modifyvm", :id, "--uart1", "0x3F8", "4",
  "--uartmode1", "tcpserver", "<host port>"]`; test2: the same UART in
  `tcpclient 127.0.0.1:<host port>` mode. Both sockets live on the
  VirtualBox host. test1 must be running before test2 boots;
  `docs/contributing.md` says so and `vagrant up` order already agrees.
- test2: `systemctl enable --now serial-getty@<tty>.service` (agetty,
  115200). The line shows `test2 login:`.
- test1: `apt install ser2net`; `/etc/ser2net.yaml` serves the tty with a
  `telnet(rfc2217),tcp,4001` accepter bound on all addresses (so `dial: ssh`
  and `dial: direct` hit one listener), `max-connections: 1`.
- **Spike first.** The provider here is VirtualBox on arm64, and this dev VM
  shows no working UART driver. Task 1 of the plan adds the UART to test1
  and test2, reloads them (only after Chris says go — it powers real VMs),
  and checks for a tty with a driver. If the provider exposes none, the
  fallback is a socat pseudo-terminal pair joined over the bed network:
  agetty on test2's pty, ser2net on test1's. otto sees the same thing.

**BusyBox row — bb1350 gains a second UART.** The QEMU line becomes
`-serial mon:stdio -serial telnet:127.0.0.1:2450,server,nowait` so ttyS0 keeps
feeding the journal and ttyS1 is the console. `GUEST_TABLE` gains a
`console_port` column (bb1350 = 2450, others `-`); `_inittab` adds
`ttyS1::respawn:/bin/busybox getty -L 115200 ttyS1 vt100` only when the
guest has one. The Vagrantfile table and `GUEST_TABLE` stay pinned to each
other by the existing unit test. The line shows `bb1350 login:`.

**Zephyr row — rebind.** The three ARM units change
`-serial telnet:${addr}:${port}` to `-serial telnet:127.0.0.1:${port}` so
`dial: ssh` reaches them. The raw protocol ports (`pport`) and the lo
aliases stay for the llext channel, which the host's `ip` + `hop` still
serve — the reason the console term ignores the host's own `hop`.

**Lab data** (`tests/_fixtures/lab_data/tech1/lab.json`):

- `test2`: `valid_terms: ["telnet", "ssh", "console"]`,
  `console_options: {"server": "test1", "port": 4001}`.
- `bb1350`: `valid_terms: ["telnet", "ssh", "console"]`,
  `console_options: {"server": "test1", "port": 2450}`.
- `zephyr37_nofs`, `zephyr37_llext`, `zephyr44_llext`: `valid_terms:
  ["console"]`, `console_options: {"server": "test4", "port": <23xx>,
  "write_chunk_size": 64, "write_chunk_delay": 0.015}`; `telnet_options`
  dropped; `hop` and `ip` kept.

Conformance draws console cells automatically because `_bed.py` enumerates
`valid_terms`; `make conformance-bed` regenerates the support matrix.

**Health.** `scripts/lab_health.py` gains a console probe: for every lab
host with a console term it connects, classifies, and reports `at-login` /
`logged-in` / `at-password` / `silent` / `busy`. `--logout-consoles` runs
`reset()` on the logged-in ones. `make vm-health` shows the probe.
**Correction (2026-09-25, proven by the implementation):** the probe reports five console rows, not three: test2, bb1350 and each
of the three ARM Zephyr guests. A credential-less (Zephyr) row is healthy
`logged-in` and waits out the full `login_timeout` to know it, adding about
35 s to a run.

### 8. Tests

**Unit** (`tests/unit/host/test_console.py`, `tests/unit/models/`,
`tests/unit/host/test_os_profile.py`, `tests/unit/host/test_session.py`):

- Every `connect()` and `reset()` branch against a scripted fake stream:
  at-login success, password-prompt, EOF/busy, silent, logged-in (quoted
  bytes ANSI-stripped, password absent), login refused, no password prompt,
  logout on close, reset rounds 1–3 and the bounded failure.
- Spec/runtime drift for `ConsoleOptions`; `server` required, self-server
  refused, unknown server refused, `dial: ssh` without an ssh cred refused,
  bad regex refused; embedded forces `login=False`.
- OS profile keywords registered and compiled; host override wins.
- Session dispatch builds a `TelnetSession` for `console`; named session
  refused; `exec` refused. **Correction (h) (2026-09-25):** `exec` runs on the
  default session, never builds a pool session, and serialises; a named
  session is still refused.
- `login(force=True)` on ssh/telnet refused; `logout()` on ssh/telnet
  refused; `console_login()` on a non-console session refused.
- Repo invariants named in their tasks: public API snapshot, error
  taxonomy, import budget, JSON schema snapshot.

**e2e** (`tests/e2e/console/`, primary marker per row: `integration`,
`busybox`, `embedded`):

1. `test_bed_serial_link`: no otto console code. Raw telnetlib3 through an
   SSH tunnel to test1 and directly to `10.10.200.11:4001`; nudge; expect
   `test2 login:`. Same for `bb1350` on 2450 and one Zephyr port expecting
   its shell prompt after `\r`. Runs first; it is the wiring proof.
2. `test_console_login_and_run`: `run("id -un")` on test2 over `console`,
   both dial modes; a `shell` transfer roundtrip.
3. `test_console_already_logged_in_fails_loud`: log in over raw telnet,
   drop the TCP connection without logging out (a `local` serial line keeps
   its shell), then otto raises `ConsoleError` whose message contains
   `already logged in` and the quoted shell prompt. Cleanup runs `reset()`.
4. `test_console_logout_restores_login_prompt`: after `host.close()`, a raw
   nudge sees `login:`.
5. `test_console_wrong_password_fails_loud`: `login refused for 'test'`.
6. `test_console_busy`: a raw client holds the port; otto raises `busy`.
7. `test_console_exec_refused`. **Correction (h) (2026-09-25):** renamed
   `test_console_exec_runs_on_the_single_session`: exec returns the
   command's output and exit code, a second exec works, and a named session
   is still refused.
8. `test_console_login_busybox` in the busybox bed group; the migrated ARM
   Zephyr entries are covered by the existing embedded suites.
9. `test_logout_verb` and `test_login_force`: `otto host test2 logout`
   against a deliberately left-open shell; `login --force` reaches a prompt
   through the pty-driven bridge harness used for the telnet login bridge.

Never skip on a host-down: a missing console fails with the host named.

### 9. Docs (one home per topic; link, never restate)

- `docs/configuration/host-options.md`: the `console_options` table, the OS
  profile prompt defaults, and the console login as a new step in "What
  runs, in order". The single home for the semantics.
- `docs/cli/host/connections.md`: `console` in the term list, the two dial
  modes, a link to host-options. `docs/cli/host/embedded.md`: the ARM
  serial example moves to the console term.
- The host login page: `--force`, the `logout` verb, the reset sequence
  described once.
- `docs/configuration/os-profiles.md` and
  `docs/cookbook/extending/extending-backends.md`: the two new
  `register_os_profile` keywords.
- `docs/contributing.md`: the serial link, console ports, boot order.
  `docs/architecture/subsystems/busybox-bed.md`: the ttyS1 getty.
  `docs/architecture/testing.md`: console on the host axis.
- `docs/getting-started/defining-hosts/`: a console-fronted host example.
- CHANGELOG is generated from commit subjects.

## Out of scope

- A Windows host family.
- Evicting another TCP client from the console server (a server setting,
  e.g. ser2net `kickolduser`).
- A configurable reset sequence or nudge string.
- Reaching a console server that is not a lab host (raw IP). Add a host
  entry for it.
- Migrating third-party `-serial telnet:` Zephyr hosts off the telnet term.

## Gates

Squash-aware. The spec commits on its own. Per task: the task's own tests,
`ruff check` on changed files, `make typecheck`; repo-invariant tests are
named in the task that touches them. The bed spike and every `vagrant
reload` wait for Chris's go. Once, before the squash: `make coverage`,
`nox -s tests_hostless-3.14`, `make typecheck`, the console e2e selection
under the unix, busybox and embedded lanes, `make conformance-bed`, then
`make gate-fresh`.
