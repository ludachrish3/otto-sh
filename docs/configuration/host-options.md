# Host options

Persistent per-host connection tuning, declared in `lab.json`. (For
per-invocation overrides, see {doc}`../cli/host/connections`; for the custom netcat backend,
see {doc}`../cli/host/netcat`.)

(connection-options)=

## Connection options

Every host can be configured with a dedicated options object per network
protocol.  Every field has a default, so a host entry needs none of these
objects.  To tune a protocol, add the matching ``*_options`` object to the host
entry — one entry of its element's ``hosts`` array, which is what every JSON
fragment on this page shows:

| Object            | Protocol                       |
|-------------------|--------------------------------|
| ``ssh_options``   | SSH sessions                   |
| ``telnet_options``| Telnet sessions                |
| ``console_options``| Serial-console sessions ({ref}`console-term`); ``lab.json`` only |
| ``sftp_options``  | SFTP transfers                 |
| ``scp_options``   | SCP transfers                  |
| ``ftp_options``   | FTP transfers (aioftp)         |
| ``nc_options``    | see {doc}`../cli/host/netcat`     |

One further table, ``userland_options``, sits alongside these but names no
protocol: it declares facts about the *device* (which elevation mechanism it
has, which ``timeout`` convention its applet speaks) that otto otherwise
probes for once per host.  It layers exactly like the protocol tables,
per-call override included.  `otto host <id> probe` prints the table ready to paste —
see {ref}`userland-capabilities`.  See also {doc}`lab-config`.

The same tables — all but ``console_options``, which is read from the
host's ``lab.json`` entry alone — are recognized in four places, layered
from least to most specific:

1. **Hardcoded defaults** in `otto.host.options` — what you get when no
   `*_options` is supplied anywhere.
2. **Per-host `*_options`** in `lab.json` — the lab's own values for
   a single host.
3. **Product `[host_preferences]`** in `.otto/settings.toml` — applied
   to every host whose id matches the selector regex (a full match over
   the id, whose seams are `-` — `test1-.*` matches every host of
   `test1` **and** of any element whose name begins `test1-`, so anchor on
   the board, `test1-bb-.*`, or spell the full id when that matters).
   Product values
   **win over** `lab.json`.  See {ref}`host-preferences`.
4. **CLI `--term` / `--transfer`** — final word, applied at invocation
   time.

Merging is **per key** between layers (1)–(3).  A host that sets only
`port` in `lab.json` still inherits `connect_timeout` from the
product preference, and so on down to the dataclass default.  The
fully resolved options are baked into the `UnixHost` at construction
time.

For one-off tuning at the call site (e.g. a single test wants a
different port), pass an `*_options=` keyword to `get_host()` /
`all_hosts()`.  See {ref}`per-call-overrides` under Extend.

### SSH

Set a non-standard port, keep this host's keys in a file of its own, and
tune the connect timeout:

```json
{
    "ip": "10.10.200.12",
    "creds": [{ "login": "admin", "password": "secret" }],
    "ssh_options": {
        "port": 2222,
        "known_hosts": "/srv/lab/known_hosts",
        "connect_timeout": 5.0,
        "keepalive_interval": 30
    }
}
```

An encrypted private key in `client_keys` is decrypted with
`"passphrase": "..."`, which otto keeps wrapped and never logs or shows; it
cannot be given through `extra`.

`otto host <id> probe` reports whether this port answers and, when the
service listens elsewhere, prints the fragment to paste here — see
{doc}`../cli/host/capabilities/protocols`.

Anything supported by ``asyncssh.connect()`` but not surfaced as a
curated field is reachable via ``extra``, which is forwarded verbatim. A key
in ``extra`` that names a curated field (``port``, ``known_hosts``, ...) is
refused when the lab is read: a value has one source, the field, so what the
survey declares and what the connect dials cannot differ. The same holds for
every ``*_options`` table that carries an ``extra``. ``extra`` also refuses
``password`` and ``passphrase``, and the host-key controls ``host_key_alias``
and ``client_factory`` (see {ref}`host-key-settings`).

```json
{
    "ssh_options": {
        "extra": {
            "config": ["/etc/ssh/otto_ssh_config"],
            "proxy_command": "corkscrew proxy 8080 %h %p"
        }
    }
}
```

The algorithm lists (`kex_algs`, `server_host_key_algs`, `encryption_algs`,
`mac_algs`, `compression_algs`) accept the names on the
{doc}`SSH algorithm matrix <../architecture/ssh-algorithms>`; a name not on
it is refused by asyncssh at connect time.

(host-key-settings)=

#### Host-key settings

Two `ssh_options` fields control how otto checks a host's SSH key:

`known_hosts`
: Path of the OpenSSH-format file otto checks and records keys in. Default
  `"~/.ssh/known_hosts"`; `~` is expanded against your home directory.
  Entries are keyed by the otto host ID, never by address:
  `router1 ssh-ed25519 AAAA…`, or `[router1]:2222 …` for a non-22 port.
  Entries your own `ssh` wrote under an address are ignored. otto reads
  only this file: `UserKnownHostsFile`, `GlobalKnownHostsFile` and
  `HostKeyAlias` in `~/.ssh/config` are not consulted, so if your SSH
  config points elsewhere, set this field to match. `null` is refused; use
  `verify_host_key` to turn checking off. Writes are serialized across otto
  processes by a lock file, `known_hosts.lock`, beside the file.

`verify_host_key`
: Default `true`: a host with no entry is accepted and its key recorded,
  and a host whose entry holds a different key is refused. `false` turns
  checking off without a warning; connections then neither read nor write
  the file (`forget-key`, an explicit action, still edits it). Set it for
  the whole project with the `".*"` selector, or for one host with its
  ID, in {ref}`host-preferences`:

  ```toml
  [host_preferences."router1".ssh_options]
  verify_host_key = false
  ```

  or for one host in its own `lab.json` entry:

  ```json
  {
      "ssh_options": { "verify_host_key": false }
  }
  ```

A first-use key is recorded after the key exchange has proven the host
holds it and before any credential is sent. Connections that reach a
never-seen host at the same moment (a fan-out, two otto processes, the
first connect after `forget-key`) all succeed and record one line: each
judges the presented key against the file as it is at the key exchange, so a
key another connection has just recorded is trusted, not a changed key. A
hop is checked under its own ID with its own `ssh_options`, so a target
reached through a hop records two entries, the hop's and its own. A refused connection fails
before authentication, and the error names the host whose key was refused
(the hop's ID when the hop leg failed), its address, the file and the fix:

```text
SSH host key for router1 (10.10.200.12) refused: Host key is not trusted for host 10.10.200.12 with alias router1. /home/me/.ssh/known_hosts holds a different key for router1; if the host's key really changed, run `otto host router1 forget-key`
```

`otto run`, `otto test`, the host verbs and the survey all show this
message. Unless `server_host_key_algs` is set, otto chooses the host-key
algorithms it offers, as OpenSSH does: the recorded key's type first, then
every default plain-key type. A host that changed its key type therefore
still completes the handshake with its new key and is refused as a changed
key. A revoked key (an `@revoked` line under the host ID or under
`[<id>]:<port>`, `[<id>]:22` included) is refused with a message saying so
and no forget-key advice, since forget-key leaves `@revoked` lines in place;
a key revoked while the connection is being made is refused the same way,
before any credential is sent, and is not recorded. Host certificates are covered in
{ref}`host-certificates`. If a key must be
recorded and the file cannot be written, the connection is refused with
both fixes named: a writable `known_hosts` path, or
`verify_host_key = false`. A read-only file whose entries all match keeps
working.

(forget-key)=

#### When a host's key changes

A rebuilt or reinstalled host comes back with a new key, and otto refuses
it until you remove the old entry. `otto host <id> forget-key` removes that
host's entries from its `known_hosts` file and does nothing else; the next
connection records the new key:

```console
$ otto host router1 forget-key
removed 2 entries for router1 from /home/me/.ssh/known_hosts
```

It handles one host per call, reads the same `known_hosts` setting the
connection does, and contacts nothing. It exits 0 whether or not there
was an entry, so a script can run it unconditionally; with nothing to
remove it says `no entry for router1 in /home/me/.ssh/known_hosts` and
writes nothing, not even its lock file, so it works on a read-only file in
a read-only directory. It fails when the file cannot be read, or when there
is an entry to remove and the file (or its directory, or the
`known_hosts.lock` beside it) cannot be written. From Python, the same
method is `await host.forget_key()`, and `str()` of what it returns is the
same text.

An entry is the host ID on port 22 and `[<id>]:<port>` on any other port,
as OpenSSH writes them, so a host whose `ssh_options.port` is `2222` has
the entry `[router1]:2222`. forget-key removes the bare form and the
bracketed form for any port, and a hashed entry for the host's configured
port or port 22. A line that also names another host or a wildcard, an
`@cert-authority` line and an `@revoked` line are left in place and listed
after the count as `kept: <line>`. With plain OpenSSH, the same removal is:

```bash
ssh-keygen -R router1                # the port-22 entry
ssh-keygen -R '[router1]:2222'       # the entry for a non-22 port
```

**A floating IP or a host rebuilt every run.** When the machine behind a
host ID changes on purpose (a floating IP moved to a new VM, a lab host
reimaged by each pipeline), run `otto host <id> forget-key` before
connecting, or set `verify_host_key = false` for that host alone, with its
ID as the selector (see `verify_host_key` above).

(host-certificates)=

#### Host certificates

A host that presents an OpenSSH host certificate is trusted only through
an `@cert-authority` line naming its host ID, in the bare form for port 22
or the `[<id>]:<port>` form for any other port, with the certificate
authority's public key, in the same `known_hosts` file:

```text
@cert-authority router1,[router1]:2222,switch* ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAA… lab CA
```

The certificate's principals must include the host ID. With such a line,
otto accepts only that CA's certificates for the host: a host that offers a
plain key alone is refused before any credential is sent, nothing is
recorded, and the error says so and gives the fix:

```text
SSH host key for router1 (10.10.200.12) refused: Host key is not certified by the host's CA for host 10.10.200.12 with alias router1. An @cert-authority line in /home/me/.ssh/known_hosts covers router1, but the host presented a plain key, not a certificate signed by that CA; have the host present a certificate signed by that CA, or remove the @cert-authority line to accept a new key on first use again
```

`forget-key` is no fix here: it keeps `@cert-authority` lines.
Without such a line, otto offers no certificate algorithms, so a host that
also holds a certificate is checked, and recorded at first use, by its plain
key; otto never records a certificate.

#### Host keys in CI

Where the runner keeps its home directory decides the problem. A runner
whose home persists between jobs keeps `~/.ssh/known_hosts` too, so after
a lab is rebuilt its old entries are still there and every rebuilt host is
refused as a changed key. A runner whose home is fresh each job has the
opposite gap: every key is accepted at first use, so nothing is ever
checked against a known key. Choose one:

- **A workspace file**, for the persistent home. Point `known_hosts` at a
  path inside the job's workspace, for every host. The file starts empty
  with each fresh workspace, so a rebuilt lab is simply recorded again. A
  relative path is resolved against the current working directory, not
  `--xdir`:

  ```toml
  [host_preferences.".*".ssh_options]
  known_hosts = "ci/known_hosts"
  ```

- **A pre-seeded file**, to check every key against a known one. Commit a
  `known_hosts` file holding each host's key under its host ID and point
  `known_hosts` at it. The simplest way to produce it is to run otto once
  against a writable copy (any command that connects to each host, such as
  `otto host <id> exec true`) and commit what it recorded. From
  `ssh-keyscan`, rewrite the address at the start of each line to the
  host ID: `router1` for port 22, `[router1]:2222` for another port. The
  file may be read-only: otto writes only to record a host that has no
  entry, so a key that differs from the file fails the job instead of
  being accepted.
- **No checking**, for a throwaway lab rebuilt for each job:
  `verify_host_key = false` under `".*"` turns checking off for the whole
  project.

#### Port forwarding

Structured forwards are declarative and applied right after the
connection opens.  Each list element maps straight to an
``asyncssh.SSHClientConnection.forward_*_port`` call:

```json
{
    "ssh_options": {
        "local_forwards": [
            {"listen_host": "localhost", "listen_port": 8080,
             "dest_host": "web.internal", "dest_port": 80}
        ],
        "remote_forwards": [
            {"listen_host": "", "listen_port": 9000,
             "dest_host": "localhost", "dest_port": 22}
        ],
        "socks_forwards": [
            {"listen_host": "localhost", "listen_port": 1080}
        ]
    }
}
```

For forwards that aren't expressible in JSON (UNIX-socket forwards,
X11, custom subsystems), build the ``SshOptions`` in Python and supply
a ``post_connect`` async hook — see the
[connection options recipe](../extend/connection-options.md).

### Telnet

```json
{
    "telnet_options": {
        "port": 2323,
        "cols": 200,
        "rows": 50,
        "echo_negotiation_timeout": 1.0
    }
}
```

Set ``auto_window_resize`` to ``true`` for interactive telnet sessions to have
otto install a SIGWINCH handler that sends NAWS updates on every local terminal
resize — remote TUIs (``vi``, ``top``, ``less``) then reflow like they do under
SSH.  It defaults to off; leave it off for automated runs, whose output would
otherwise vary with terminal resizes.

`otto host <id> probe` reports whether this port answers and, when the
service listens elsewhere, prints the fragment to paste here — see
{doc}`../cli/host/capabilities/protocols`.

**Embedded / UART-backed consoles** — four extra fields matter when the
telnet endpoint is a QEMU ``-serial telnet:`` bridge rather than a Unix
telnetd ({class}`~otto.host.options.TelnetOptions`). A serial console behind
a telnet server is usually better reached with the `console` term
({ref}`console-term`), which names the server rather than the device and
checks what state the line is in; these fields keep such a device working on
`telnet`:

- ``write_chunk_size`` (default ``0``) — split each command write into
  chunks of at most this many bytes.  ``0`` sends the whole payload in
  one call (correct for a host-terminated shell).  Set a positive value
  (e.g. ``64``) for a UART-backed RTOS shell that overruns its console
  RX FIFO on a multi-KB ``llext load_hex`` line.
- ``write_chunk_delay`` (default ``0.0``) — seconds to pause between
  chunks; ignored when ``write_chunk_size`` is ``0``.
- ``login`` (default ``true``) — set ``false`` for a bare-metal / RTOS
  shell with no login step; otherwise otto waits for a ``login:`` prompt
  that never arrives and hangs the connection.
- ``login_prompt`` (default ``":"`` byte) — byte delimiter that ends the
  login / password prompts.  The default matches ``login:``,
  ``Username:``, ``Password:``, etc.
- ``single_client_console`` (default ``false``) — set ``true`` when the
  endpoint is a single-client console (e.g. Zephyr ``shell_telnet``).
  Otto registers the transport so the embedded teardown can force-release
  the slot if a timed-out test left it half-open.  Leave ``false`` for
  ordinary multi-session telnetd.

(console-term)=

### Console

A **serial console** is a device's serial port (its UART): the line a
Linux box runs a `login:` prompt on (a *getty*), and the only shell many
routers, boards and RTOS targets have. Labs rarely cable those ports to a
test machine directly; a **console server** sits in front of them and
turns each serial port into a telnet port — `ser2net` on a Linux box, a
Lantronix or Digi appliance, or QEMU's `-serial telnet:` for a virtual
board. The `console` term is otto's client for that shape.

Choose it when the serial console is the way in: the device runs no ssh or
telnet daemon, its network is down or not yet configured, or what you are
testing is the console itself. Where the device has a working ssh, prefer
ssh — a serial line serves one client at a time, and that shapes what otto
can do over it (see *One client at a time* below).

Unlike `telnet`, which dials the device's own address, the console term
dials **a port on another lab host**: the host that runs the telnet server.
The device's own `ip` and `hop` play no part in reaching the console (they
still address the device for anything that reaches it over its network,
such as an `scp` transfer). This is `test2` from otto's test bed, whose
console is served by `test1` on port 4001:

```json
{
    "ip": "10.10.200.12",
    "creds": [{ "login": "vagrant", "password": "vagrant" }],
    "valid_terms": ["telnet", "ssh", "console"],
    "console_options": { "server": "test1", "port": 4001 }
}
```

`test1` must be a host in the same lab. Put `console` first in
`valid_terms` (or pin `"term": "console"`) to use it by default, or select
it for one invocation with `--term console` ({doc}`../cli/host/connections`).

`dial` picks how otto reaches the server's port:

- **`"ssh"`** (the default) — otto opens an SSH connection *into* the
  server, with the server's own cred and through the server's own `hop`
  chain if it has one, and forwards `localhost:<port>` from inside it. A
  listener bound only to the server's loopback — QEMU's usual bridge — is
  reachable this way. The server needs a cred otto can SSH with.
- **`"direct"`** — otto connects to `<server ip>:<port>` over TCP (through
  the server's `hop` chain, when it has one). For a console appliance that
  has no SSH of its own.

A missing `server`, or a `port` outside `1`–`65535`, is checked for the
term the host will use: under no preferences (`otto init`'s doctor, host
listings) an entry passes when some selection its menus and pins can produce
passes; at load, the selected one must. So a host whose menu offers `console`
beside `ssh` lists and passes the doctor with no `server`, and fails to load
only when `console` is the term it selects. A `server` that names the host
itself, or a host not in the lab, is an error naming both, raised on the
first connect, as a bad `hop` is, not at lab load.

| Field | Default | Meaning |
|---|---|---|
| `server` | — | Lab host ID of the telnet server. Required when the selected term is `console`; missing, the lab fails to load. |
| `port` | — | The console's telnet port on the server, `1`–`65535`. Required when the selected term is `console`; missing or out of range, the lab fails to load. |
| `dial` | `"ssh"` | `"ssh"` or `"direct"`, as above. |
| `login` | `true` | Log in from the `login:` prompt. Set `false` for a line with no login step (an RTOS shell); embedded hosts always force `false`. |
| `login_prompt`, `password_prompt` | from the OS profile | Regexes matched against the *end* of what the line shows. Unset means the host's OS profile's patterns ({doc}`os-profiles`); set them for a device whose prompts read differently. |
| `login_timeout` | `10.0` | Seconds to wait for each prompt after otto presses Enter or types a name. Also bounds the dial. Raise it when a boot log precedes `login:`. |
| `settle` | `0.5` | Seconds of output read and thrown away right after connecting, so stale bytes on the line are never mistaken for an answer. |
| `logout` | `true` | On close, send Ctrl-D (end of input) and wait up to `login_timeout` for `login:`, so the next client finds the line logged out. |
| `write_chunk_size`, `write_chunk_delay` | `0`, `0.0` | Paced writes for a UART that overruns on a long line, as for telnet (above). |
| `cols`, `rows` | `400`, `24` | Initial terminal size reported to the server. |
| `encoding` | `false` | Text encoding; `false` is bytes mode. |
| `echo_negotiation_timeout` | `3.0` | Seconds to wait for the server to stop echoing. |
| `extra` | `{}` | Passed verbatim to `telnetlib3.open_connection()`. |

**What otto does on connect.** It dials, waits `settle`, then presses
Enter and reads until the output *ends* in a prompt or `login_timeout`
passes. Only a `login:` prompt it has just seen lets it continue: it types
the username, waits for the password prompt, and types the password. The
login happens before anything else runs on the line — see
{ref}`What runs, in order <session-run-order>`. With `login: false`
there is no login step; the readiness handshake that follows is the check
that a shell answers.

Anything but a `login:` prompt is an error. otto never resets a console on
its own and never proceeds on a session it did not open — **a console that
is already logged in always fails**, even if the session left on it is
otto's own from an earlier run. Resetting the line is an operator's
decision: {doc}`../cli/host/login` covers `otto host <id> logout` and
`login --force`.

(console-failures)=

**The failures.** Every console error starts with the words
`<host>: console <server>:<port> (dial=<mode>)` — the lab's names, never a
forwarded local port — then says which state the line was in, because each
has a different fix:

| The message says | What the line was doing | What to do |
|---|---|---|
| `is stuck at a password prompt` | Someone typed a username and left. | `otto host <id> logout`. |
| `is busy: the server closed the connection before any prompt — another client holds the port, or the server has no serial device behind it right now (the line is down)` | The console server allows one client and another has it (a colleague's telnet, another otto run, a stale tunnel), or the server accepted and closed at once because its serial device is gone (a USB adapter unplugged; on the bed, the socat link while its far end reboots). During `reboot --wait` this is the down phase and expected. | Find and close the other client, or wait for the device to return. |
| `is silent: no login prompt within <N>s and no bytes received` | Nothing answered Enter. | Check the port, that a getty runs on the device's serial line, and the cable. |
| `did not show a login prompt; it is likely already logged in. Seen: '…'` | A shell, or a program in it, was left running. The quote is the last 200 characters the line showed, escape sequences stripped and the password replaced by `***`. | Read the quote; then `otto host <id> logout` (or `login --force`). |
| `login refused for '<user>'` | After the username the line showed `login:` again, or after the password it showed a login or password prompt. | Check the host's cred. |

Rarer ones name their cause the same way: `showed no password prompt after
the username`, `closed the connection after the username` (the device or
server hung up mid-login), `lost the connection: …` (the link dropped while
otto was reading), `could not be dialled: …`, `could not tunnel to console
server …`, and `no login prompt pattern` (or `no password prompt pattern`)
when neither the host nor its OS profile supplies that prompt regex. Passwords never appear in any of them.

**One client at a time.** A serial line has one shell, so otto uses one
session on it:

- `run()` works, on that session. So does `exec()` (and
  `otto host <id> exec`): on a console it is a special case of `run()`, one
  command on the same session with the same exit-code handling. It shares
  that session's shell state (a `cd` stays). Concurrent `run()`, `exec()`
  and `shell`-transfer commands take turns on the one line. A user switch
  (`run(user=...)`, `as_user`) happens outside that turn-taking, so keep
  switched calls sequential rather than gathering them with other commands.
  `exec(user=...)` switches the session to that user for the one command
  and back, inside its turn.
- The `shell` transfer works (its commands run on that session, as the
  logged-in user), and so do `scp`, `sftp` and `ftp`, which reach the device
  over its network rather than the console.
- Named sessions are refused on a Unix host, with `console is single-client
  (<server>:<port>, dial=<mode>); named sessions are not available on this
  term`: each one would be a second connection. On an embedded host `exec`
  already shares the one session ({doc}`../cli/host/embedded`).
- The `nc` transfer is refused: its remote listener would hold the session
  while its control commands need it too. How a menu that lists `nc` resolves
  is on {doc}`lab/host-fields` (the `transfer` row); a transfer asked to run as a
  user other than the one logged in is refused too.
- Every command otto issues for itself — the userland probes, a transfer's
  helper commands, the `sudo` probe — is an `exec`, so it runs on the same
  session, one at a time. A `session_setup` hook that calls the host's
  `run()`, `exec()` or file operations (`put`, `exists`, `read_file`, …) on
  a console host therefore waits for the session its own hook is still
  setting up, forever. Use the session handle the hook is given instead.
- Calling `login()` or `logout()` on a host in the same process ends that
  host's `run()` session: both close otto's own session first, since it
  holds the line. The next `run()` connects and logs in again. A
  reachability probe (`otto host <id> probe`, `is_reachable()`, and each
  poll of `reboot(wait=True)`) does the same and then dials fresh: the link
  to the server outlives a device reboot, so only a new login shows the
  device is up.

When otto closes the session it logs out (per `logout`), so the next client
finds `login:`.

### SFTP, SCP, FTP, Netcat

```json
{
    "sftp_options": { "env": { "LANG": "C" } },
    "scp_options":  { "block_size": 65536, "preserve": true },
    "ftp_options":  { "port": 2121, "ssl": true, "socket_timeout": 30 }
}
```

``SftpOptions``, ``ScpOptions``, and ``FtpOptions`` each carry an ``extra``
dict that is forwarded verbatim to the underlying library (``asyncssh``,
``aioftp``) for any option not surfaced as a curated field; a key that names
a curated field is refused, as for ``ssh_options`` above.  ``NcOptions``
has no ``extra`` — all netcat knobs are curated fields.

``scp_options`` and ``sftp_options`` also carry ``max_concurrent_transfers``
(default ``null``): how many files of one transfer are in flight at once, one
SSH session each. ``null`` derives a bound that fits a default sshd — see
{ref}`transfer-channel-budget` below.

Each of these backends reports transfer progress at a fixed stride — the most
the bar may advance between two ticks: 16 KiB for ``sftp`` and ``scp``, 8 KiB
for ``ftp`` and ``nc``.  Only ``scp``'s follows your configuration: whatever
``block_size`` reaches ``asyncssh`` becomes the stride, and the field above
is the only way to set it.
{ref}`The support-matrix page <matrix-progress-promises>` lists every backend's
promise and how a run measures it.

**Notable SFTP fields** ({class}`~otto.host.options.SftpOptions`): ``env``
sets remote environment variables; ``send_env`` forwards named local
variables to the remote SFTP process.

**Notable SCP fields** ({class}`~otto.host.options.ScpOptions`): ``recurse``
(default ``true``) controls directory recursion — set it ``false`` to
transfer a single file without descending; ``preserve`` carries mtime/atime/mode.

**Notable FTP fields** ({class}`~otto.host.options.FtpOptions`): beyond
``port``, ``ssl``, and ``socket_timeout``, the impactful knobs are
``connection_timeout`` (handshake), ``path_timeout`` (list/stat),
``read_speed_limit`` / ``write_speed_limit`` (bytes/sec caps, ``null`` =
unlimited), and ``passive_commands`` (default ``["epsv", "pasv"]``).

`otto host <id> probe` reports whether this port answers and, when the
service listens elsewhere, prints the fragment to paste here — see
{doc}`../cli/host/capabilities/protocols`.

Netcat has additional options and auto-detection strategies — see {doc}`../cli/host/netcat`.

(per-host-init-timeout)=

## Shell readiness timeout

Every shell otto opens on a Unix host must answer a readiness probe before
otto sends it a command: the session behind `run`, the named sessions of
`open_session`, the shells `exec` uses, and the landing shell of an
`otto login` on a host with a `session_setup` hook. Over ssh, `exec` uses a
shell only under a proxied login, on a host with a `session_setup` hook, or
for a call that needs one (an elevation, a user switch, a prompt to answer);
otherwise it runs on a bare channel and is probed by nothing. If no answer
comes back in time, the open fails with `shell never became ready after open`.
The session behind `run` then retries once, on a fresh connection, after a 2 s
pause (not on a console whose login has already typed the password); the
others fail at the first deadline.

`init_timeout` sets that deadline in seconds, a number greater than 0; leave it
out for the default, 10. The clock starts once the connection is up: over ssh
when the channel opens, over telnet when the password has been sent, so on a
telnet host it has to cover the login itself, the MOTD and the shell's startup
files. Raise it for a host whose login is slow, or for a lab where the machine
running otto is heavily loaded. When it is the machine running otto that is
slow, so every host needs the same room, set `OTTO_INIT_TIMEOUT` (seconds) in
the environment instead: it replaces the default, 10 s here and 15 s on an
embedded host, for every host without its own `init_timeout`, which still
wins. An empty value counts as unset; any other value must be a finite number
greater than 0, and otto refuses anything else before it connects, naming the
variable. For the test suite on a slow machine, see
{ref}`the contributing guide <slower-machines>`. To pick a value,
time a manual telnet login on that host, from the password to the prompt, and
allow a few times that:

```json
{
    "ip": "10.10.200.12",
    "creds": [{ "login": "admin", "password": "secret" }],
    "init_timeout": 30
}
```

A higher value costs nothing when the shell answers quickly. It changes how
long a telnet login that never yields a shell (a wrong password, for example)
takes to fail: for the `run` session, two connection attempts, each up to the
deadline plus the time to connect, and the 2 s pause between them.
`otto host survey` sizes each login check from this, so it still reports such
a host as `login-failed`. The survey has 90 s for the whole host, though, so a
large value leaves its later checks less of that time, and those can report
`survey budget exhausted`. A command's own timeout starts once the shell is
ready, so it never cuts an open short; the exception is tunnel discovery's
30 s check that a container host is running, which runs a command on the
parent host and so includes opening the parent's shell when it needs one.

Like `shell_history`, `init_timeout` can be defaulted for a class of hosts from
an `[os_profiles.<name>]` block.

(per-host-shell-history)=

## Shell history

otto drives a Unix host through a persistent interactive shell, so without
intervention every command it runs is appended to that host's shell history —
burying a human's own history under automation traffic on a shared lab box.

By default otto suppresses this: on each shell it opens it neutralizes
``HISTFILE`` before running anything, so nothing it does reaches
``~/.bash_history``. Set ``shell_history`` to ``true`` on a host where otto's
commands *should* stay visible in the shell's own history (e.g. an audited
box where the history file is the record of what touched it):

```json
{
    "ip": "10.10.200.12",
    "creds": [{ "login": "admin", "password": "secret" }],
    "shell_history": true
}
```

Being a plain host field, it can also be defaulted for a whole class of hosts
from an ``[os_profiles.<name>]`` block in `.otto/settings.toml`, with a
per-host ``lab.json`` value overriding the profile. It is not accepted in
``[host_preferences]``, which takes only the menu-style capabilities
(``term`` / ``transfer`` / ``impairer``).

What suppression covers, and what it doesn't:

| Path | Suppressed? |
|------|-------------|
| SSH and telnet sessions (`host.run`, named sessions, app shells) | yes |
| Shells entered via a login proxy — `switch_user`, `as_user` | yes; `su` starts a fresh shell that re-reads rc files, so it is re-applied there |
| `host.exec(...)` | not needed — an exec channel has no PTY, and a non-interactive shell keeps no history at all |
| Local host commands | not needed — non-interactive |
| `otto login` | **no** — see the note below |
| Embedded / Zephyr targets | not applicable — their shell history is a RAM ring buffer, never a file |

```{note}
`otto login` hands *you* a real shell, with its history left on. If that
login goes through a login proxy (`--user`), otto's own
`__OTTO_…_RECOVER__` resync probe is written into the elevated shell and so
appears in *its* history; on a host with a `session_setup`, the hook's own
commands land in yours the same way. There is no way to suppress the probe
alone.
```

Suppression is best-effort and silent: a shell that refuses it keeps working,
merely unsuppressed. To suppress history yourself (say, in a `session_setup`
hook), unset ``HISTFILE``; never set ``HISTSIZE=0``, which makes bash overwrite
the history file at exit.

(per-host-session-setup)=

## Session setup

otto runs two commands of its own on every shell it opens — the readiness
handshake (`stty -echo; echo; printf 'X\r\nY\nZ\rW\n'; echo <marker>` on
bash) and, on Unix hosts, history suppression — and then, by default, nothing
else. The `printf` is the handshake's calibration line: how its known bytes
come back shows how the session's terminal translates line endings, which is
what lets otto return a command's output exactly
({ref}`command-output-contract`). It costs no extra round trip, and a target
without `printf` still completes the handshake. A host that needs
more before its shell is fit to use names a **session setup**: a registered
async callable that runs once per shell session, after the handshake and
after every login-proxy hop, with a real session handle.

```json
{
    "ip": "10.10.200.11",
    "creds": [{ "login": "vagrant", "password": "vagrant" }],
    "session_setup": { "type": "provision-app", "env": "lab" }
}
```

A string selects the hook by name; an object names it in `type` and hands
every other key to the hook as `ctx.params`. The hook itself is project
code, registered from an `init` module — {doc}`../extend/extending-backends`
has the contract and a copyable registration, and the bed case study's
{doc}`../developing/bed-case-study/customizations` page walks through three.

(session-run-order)=

### What runs, in order

1. Transport hops (`hop`), then the connection lands.
2. The console login (term `console` only): from a `login:` prompt otto
   observed, never otherwise — see {ref}`console-term`.
3. The readiness handshake, in the **landing** dialect.
4. Every login-proxy hop (`creds` with `proxy`), identity-proved.
5. The hook, over a session framed in the landing dialect.
6. **Frame entry**: the `command_frame`'s own handshake, in whatever shell
   the hook left. This runs after every hook, unconditionally, and is the
   confirmation that the shell is fit to use.

The hook runs on the persistent default session, on every named session,
on the pooled sessions `exec` uses on a hooked Unix host, and on the
interactive `otto host <id> login` bridge before you get the prompt. It
does not run on `as_user`/`switch_user` (identity changes inside a session
that is already set up), on `exec(user=...)`, or under a dry run (no
session opens).

### Two dialects: `landing_frame`

When the shell otto lands in is not the shell `command_frame` describes —
a Linux login shell in front of a vendor CLI, a `python3` REPL, a boot
menu in front of a Zephyr shell — declare the landing dialect too:

```json
{
    "ip": "10.10.200.11",
    "creds": [{ "login": "vagrant", "password": "vagrant" }],
    "landing_frame": "bash",
    "command_frame": "pyrepl",
    "session_setup": "enter-python"
}
```

The handshake and the hops run in the landing dialect; the hook's `run()`
is framed in it too, until the hook calls `enter_frame()` or returns; then
frame entry runs the `command_frame`'s handshake. `landing_frame` is only
valid with `session_setup`, and a host whose `creds` include a proxied
entry must land in a bash-family dialect (the hop identity probe is bash).

`"raw"` is the landing for a console that answers no frame at all: no
handshake is sent, the hook works with `send`/`expect` alone (`run()`
refuses until `enter_frame()`), and frame entry still confirms the target.
`command_frame: "raw"` is refused — raw is a landing, never a destination.

A console whose line must be driven through a boot menu before Linux
reaches `login:` declares `console_options.login: false`, so the transport
does not try to log in, and lands raw; its hook calls
`await session.console_login()` once the prompt is in sight. That runs the
same login and raises the same {ref}`failures <console-failures>` as the
transport would, once per session: a second call, or a call on a console
whose transport already logged in (`login: true`), fails with `is already
logged in by this connection`. On any other term it is refused with an
error naming the host.

### What a two-dialect host can do

| Verb | On a two-dialect host |
|------|-----------------------|
| `run`, `send`/`expect`, named sessions, `app_shell`, `login` | work; every session ends in the application and speaks its dialect |
| `exec` | works — each pooled session runs the hook and enters the application on its own |
| `put`/`get` over `sftp`/`scp` | work — transport-level, they land on the host the application runs on — whether the application sees the files is the project's business |
| `run(sudo=True)`, `read_file`/`write_file`, `lsmod`, `shutdown`, `reboot(wait=True)`, the `shell` and `nc` transfer backends | unavailable — they assume a POSIX shell at the far end of `exec`, which is how otto resolves the host's userland |

`SshOptions.post_connect` ({doc}`../extend/connection-options`) is a
different thing: it runs against the SSH *connection* (port forwards, X11)
and never touches a shell.

(per-host-snmp)=

## SNMP monitoring block

A host that exposes metrics over SNMP rather than a shell carries an ``snmp``
block ({class}`~otto.host.options.SnmpOptions`) instead of (or alongside) the
``*_options`` transport objects.  The full field reference and a worked example
are under [SNMP monitoring](lab/snmp.md#snmp-monitoring) in the lab schema;
see {doc}`../cli/monitor/metrics` for what ``otto monitor`` does with the
readings.

`otto host <id> probe` reports whether this port answers and, when the
service listens elsewhere, prints the fragment to paste here — see
{doc}`../cli/host/capabilities/protocols`.

(per-host-toolchain)=

## Per-host toolchain

Each host can specify a **toolchain** that tells otto which ``gcov`` and
``lcov`` binaries to use for coverage report generation.  This is
essential when hosts run products built with different cross-compilers.

Add an optional ``toolchain`` object to the host entry in ``lab.json``:

```json
{
    "ip": "10.10.200.12",
    "board": "arm-board",
    "creds": [{ "login": "admin", "password": "secret" }],
    "toolchain": {
        "sysroot": "/opt/arm-toolchain"
    }
}
```

Tool paths (``gcov``, ``lcov``) are resolved **relative to the sysroot**.
The defaults are ``usr/bin/gcov`` and ``usr/bin/lcov``, so setting just
``sysroot`` is sufficient when the toolchain follows the standard layout.

For non-standard layouts, override individual paths.  A host whose
product is built with ``clang --coverage`` points ``gcov`` at an
``llvm-cov`` binary — otto substitutes the one-word ``llvm-cov gcov``
wrapper that ``lcov`` requires at capture time:

```json
{
    "toolchain": {
        "sysroot": "/usr/lib/llvm-18",
        "gcov": "bin/llvm-cov",
        "lcov": "/usr/bin/lcov"
    }
}
```

### Resolution order

See {ref}`coverage-gcov-resolution` on the coverage page for the order
otto uses to pick a host's `gcov` when this object does not name one, and
for what happens when the resolved tool cannot actually read the build's
counters.


## `nc_options` reference

The `nc_options` object accepts all eight fields of {class}`~otto.host.options.NcOptions`:

| Field                        | Default    | Purpose                                                                   |
|------------------------------|------------|---------------------------------------------------------------------------|
| ``exec_name``                | ``"nc"``   | Netcat binary on both sides (e.g. ``ncat``, ``netcat``).                  |
| ``port``                     | ``9000``   | Base port; the ss/netstat/proc scans start at a random offset above it.   |
| ``port_strategy``            | ``"auto"`` | Strategy for finding a free remote port (see {doc}`../cli/host/netcat`).                |
| ``port_cmd``                 | ``null``   | Shell command printing a free port; used when ``port_strategy="custom"``. |
| ``listener_check``           | ``"auto"`` | Strategy for verifying the remote listener is ready (see {doc}`../cli/host/netcat`).    |
| ``listener_cmd``             | ``null``   | Shell command (``{port}``); exits 0 when ``listener_check="custom"``.     |
| ``listener_timeout``         | ``30.0``   | Seconds otto waits for the remote listener to exit after a transfer ends. |
| ``max_concurrent_transfers`` | ``null``   | Files in flight at once; ``null`` fits a default sshd (see below).        |

``listener_timeout`` is otto's own bound and is not passed to the remote
netcat.  It caps the wait for the listener process to exit once a transfer has
ended, so a port-collision race that leaves a listener servicing someone else
surfaces as a named error instead of a hang.  What ends the remote listener
itself is otto reaping it on every error path, plus the ``timeout`` prefix the
spawn carries; otto never passes ``nc -w``.

### What a GET can fetch

The nc backend transfers **regular files**, and transfers exactly the number
of bytes the remote ``stat`` reports — that size is what ends otto's read,
because the spawn carries no netcat option that ends itself.  A symlink is
followed and delivers its target.  A directory or a device file is refused by
name: what ``stat`` reports for those is not what reading them delivers.
procfs and sysfs pseudo-files report a size that is not their content either
(``/proc/version`` reports 0), and nothing in the remote ``stat`` tells them
apart from a genuinely empty file — so they are not refused, they simply
arrive empty.  Use the ``shell`` backend for those.

`nc_options` participates in the same layered merge as the other transport
option objects, described at the top of this page.

```json
{
    "nc_options": { "exec_name": "ncat", "port": 9500 }
}
```

(transfer-channel-budget)=

## Concurrency and the remote channel budget

An nc transfer is not free of SSH channels just because it moves its bytes over
its own TCP connection: otto holds one exec channel for the whole life of the
remote ``nc -l``, and the listener-readiness poll opens another while it is
held. A default OpenSSH server allows ``MaxSessions 10`` channels per
*connection* and **refuses** the excess rather than queueing it, so an
unbounded bulk transfer turns "many files" into ``open failed`` — or into
``Remote nc listener on port N not ready`` when it is the readiness poll that
loses its channel.

otto therefore caps the files in flight per host connection, for ``nc``,
``scp`` and ``sftp`` alike. The default is derived from the OpenSSH default
(``MaxSessions 10``) less two channels of headroom for otto's own control
commands — 10 − 2 = **8 usable**. An nc transfer holds two channels per file,
so its default is 8 ÷ 2 = 4. An scp or sftp transfer holds one session per
file, and the 8 is halved to 4 rather than spent: a full fan-out then takes
only half the usable budget and the other half is still there for whatever
else you run on that connection while the batch is in flight — your own
``run`` calls, a second transfer on another backend, an interactive session.
(A ``user=`` transfer authenticates separately and rides its own SSH
connection, so it does not share this budget; it has one of its own.) Set
``max_concurrent_transfers`` on the protocol's own options table when the
remote sshd is *not* default — raise it for a host with a raised
``MaxSessions`` to transfer wider, and lower it for a host with a lowered
one, which would otherwise lose files. otto cannot read the server's
setting, so this is the only way to tell it.

```json
{
    "nc_options": { "max_concurrent_transfers": 12 },
    "scp_options": { "max_concurrent_transfers": 8 }
}
```
