# Security cluster before 0.18.0 (#546–#552, with #751 and #487)

**Status:** draft r4, 2026-10-28, after three Astra/Fable rounds; awaiting Chris. Points marked *(Chris)* are
the maintainer's decisions; *(my call)* points are the author's and open to correction.
Line references are to origin/main fbaaf8882 and asyncssh 2.24.1.

## Why

The 2026-09-30 whole-repo security review filed seven issues, #546–#552. None is a
regression, but together they mean otto hands lab passwords to anything that answers at a
host's address, writes them to logs, tracebacks and `junit.xml`, can type them into the
wrong program, and lets a NetBox editor run commands on a hop. Chris wants all seven
fixed before 0.18.0.

## Goals

- Every finding in #546–#552 is closed, each with a regression test that injects the
  hostile condition (a planted password, a quote in an address, a changed host key, a
  prompt string in command output) and proves the leak or injection no longer happens.
  Acceptance cases are listed per section.
- Fixing them costs users as little as possible. The floating-IP labs Chris describes (a
  host's address is reassigned to a new machine once or twice a month, sometimes several
  hops away) stay a one-command recovery or a one-line setting.

## Non-goals

- A mass key-purge command. *(Chris: few hosts are like this.)*
- A general secrets framework beyond `SecretStr` on credentials.
- Deleting docker staging on `down` (§5; follow-up issue for staging lifetime).
- New CI security scanning and Dependabot changes: a separate branch after this one.

## Anti-goals

- A warning on every run for a host whose key checking is off. *(Chris: almost every
  project has at least one such host.)*
- An otto-private key store. *(Chris: keys live in the user's standard SSH file, and a
  user can always fix them with plain `ssh-keygen -R`.)*
- Changing what sudoers authorizes. otto elevates the user's command, never a wrapper
  shell (§3).

## 1. SSH host keys (#552)

### Behavior

- **Default: accept new, refuse changed** *(Chris)*. A host with no entry is accepted and
  its key recorded, as `ssh -o StrictHostKeyChecking=accept-new` does. A host whose entry
  holds a different key is refused, before any password is sent.
- **Entries are keyed by otto host ID, and only by host ID** *(Chris: IDs are globally
  unique; addresses repeat between labs)*. The entry reads `<host id> ssh-ed25519 AAAA…`
  (`[<host id>]:<port> …` for a non-22 port, OpenSSH's form). Address-keyed entries the
  user's own `ssh` wrote are ignored by otto.
- **`otto host <id> forget-key`** removes that host's entries and does nothing else; the
  next connection records the new key. One host per call *(Chris)*. It always exits 0,
  reports how many entries it removed and from which file, including "no entry for
  `<id>` in `<file>`", so a script can run it unconditionally. Docs give the plain-SSH
  equivalent: `ssh-keygen -R <id>`, plus `ssh-keygen -R '[<id>]:<port>'` for a non-22 port.
- **Refusal message** names the host whose entry differs, its address, the file, and the
  exact `otto host <that id> forget-key`. When a hop leg fails, that is the **hop's** ID.
- **Opt-out** *(Chris)*: a setting turns checking off, project-wide or per host, silently
  (no warning), documented on the host-configuration page with the floating-IP case as
  its example. With checking off, otto neither reads nor writes the file.

### Design

- **The file** *(my call)*: `SshOptions.known_hosts` becomes a path, default
  `~/.ssh/known_hosts`. otto does not read `UserKnownHostsFile`/`GlobalKnownHostsFile` or
  `HostKeyAlias` from `~/.ssh/config`: one file, resolved by one function, used by
  connect, record and forget alike. The host-options page says this next to the setting,
  since a user with `UserKnownHostsFile` in their config will hit it first.
- **`known_hosts = None` is refused** at `SshOptions` construction, naming
  `verify_host_key = false`. *(my call)*
- **Opt-out setting** *(my call on the name)*: `SshOptions.verify_host_key: bool = True`,
  mirrored in `SshOptionsSpec`. Project-wide and per-host use the existing
  `[host_preferences."<regex>".ssh_options]` cascade: `".*"` for the project, the host ID
  for one host. No new layer.
- **Matching, alias only**: asyncssh by default matches an entry by alias *and* peer
  address (`connection.py:3570`, `known_hosts.py:214-217`), and a tunnelled target's peer
  address is the hop's. So otto passes asyncssh a **callable** `known_hosts`
  (synchronous, `(host, addr, port)`, `known_hosts.py:376-387`) that parses the file with
  `asyncssh.read_known_hosts` and returns `parsed.match(alias, "", port)`. Hashed entries,
  `@cert-authority` and `@revoked` are asyncssh's logic, not re-implemented. A revoked key
  for the alias, under the bare or the `[alias]:port` form (`[alias]:22` included, which
  never decides port 22 but names it), is refused. X.509 subject
  lines are not supported in this file and are refused with a message naming the line
  (asyncssh's callable path cannot round-trip them, `known_hosts.py:65-71`).
- **otto's SSH client object** (passed as `client_factory`; otto passes none today)
  carries the decision through the connection:
  1. `validate_host_public_key` (synchronous): if the alias has **any** plain-key entry,
     return False (a changed key). If it has none, accept and remember "first use, this
     key". Certificates are never accepted here; they are trusted only through
     `@cert-authority` lines.
  2. `begin_auth` (synchronous, after key exchange has proven the server holds the key,
     before any credential is sent, `connection.py:2328-2348`): if this is a first use,
     record the key under the flock (below), judging each read through the same lookup
     as the handshake, revocations included. If, under the lock, the file now revokes
     the key or holds a different key for the alias, or the line cannot be written
     whole (`os.write` is looped until every byte lands), raise; the connection
     is torn down through the existing `teardown_ssh_connection`
     (`host/transport.py:29`) and no password is ever sent.
  3. GSS key exchange (no host key, `connection.py:3674`) records nothing.
- **Key-type rotation**: when the alias has an entry, asyncssh restricts the offered
  algorithms to that entry's (`connection.py:3575-3580`), so a host that switched key
  type fails as `KeyExchangeFailed`. With an alias entry present, that maps to the same
  "changed key, forget-key" message.
- **Serialized across processes**: `fcntl.flock` on a lock file beside the known-hosts
  file, held for record (re-read, check, append) and for a forget that has something to
  remove (scan, then lock and re-scan; a forget with nothing to remove writes nothing,
  not even the lock file, so it works in a read-only directory). The synchronous hooks
  run on the single event loop, so in-process connections cannot interleave inside them.
- **Missing or unwritable file**: checking is read-only until a key must be recorded. A
  missing file reads as empty; a missing `~/.ssh` is created 0700 and the file 0600 at
  the first record. If a record is needed and the file cannot be written, the
  connection is refused (step 2) naming both fixes: `known_hosts = "<writable path>"` or
  `verify_host_key = false`. A read-only pre-seeded file whose entries all match keeps
  working.
- **The alias is the host ID, passed explicitly**: connection labels are display names
  (`TermContext.name`, `"name as 'user'"`, `"target via hop"`), so the host passes its ID
  into the connect seam (`connect_and_describe`, `connections.py:394-419`); the hop tunnel
  passes the hop's ID; `ssh_as` the host's. `host_copy_on_port` (survey) keeps the ID, so
  a probe on another port records `[id]:port`. `SshOptions.extra` may not name
  `known_hosts`, `host_key_alias` or `client_factory` (the existing `_refuse_shadowing`).
- **One error**: `connect_and_describe` translates every host-key refusal into one otto
  error carrying the host ID, address, file and the forget-key hint. The survey's
  "login-failed: host key" classification (`survey/login.py:122`) and `cli/expose.py`'s
  unreachable-host handling recognize it, so run, test, host verbs and the survey show
  the same message.
- **forget-key** is a `@cli_exposed` verb on the SSH-capable host class. It edits the file
  through the same helper that records (same resolution, same lock), not by shelling out
  to `ssh-keygen`. It removes a line only when every host pattern on it is the alias or
  `[alias]:<port>` (plain or hashed); a line that also names other hosts, a wildcard, an
  `@cert-authority` or an `@revoked` line is left alone and reported.

### Acceptance cases

A changed key is refused before authentication (the password never reaches the server)
and names the host; forget-key then lets it through and records the new key; a hop with
a changed key is named, not its target; a pre-existing address-keyed entry with a
different key does not affect otto; two processes recording the same new host write one
entry; `verify_host_key = false` per host connects silently and touches no file;
`known_hosts = None` is refused; an unwritable file refuses only when a record is
needed; a non-22 port records and forgets `[id]:port`; a key-type change gets the
changed-key message.

## 2. Passwords out of logs, tracebacks and reports (#549, #548, #487)

Traceback locals **stay on** in both the CLI and the logs, with no switch to turn them
off *(Chris)*. So the password must never be a plain string in any frame a traceback
renders. All of #487 joins this branch.

- **`SecretStr` end to end (#487 §1)** *(Chris: every password travels as a redacting
  object)*. `Cred.password` (`models/base.py`) and `CredSpec.password` become pydantic
  `SecretStr`; the store and lab file formats are unchanged (pydantic coerces the
  string). `Cred` stays frozen and equal by value (it is not hashable: `params` is a
  dict). The raw value is unwrapped with
  `get_secret_value()` only **inside the expression that hands it to a library** (for
  example `asyncssh.connect(..., password=cred.password.get_secret_value())`), never
  bound to an otto local, parameter, kwargs dict or attribute. The type checker lists
  every read site once the type changes (12 in `src/` today, plus `.credentials`
  tuples and `privilege.py`'s sudo/su answers).
- **Merge trap (#487)**: JSON-mode dumps mask `SecretStr` as `**********`, so
  `inventory/creds.py:150-152` would silently replace every merged store password, and a
  dump that revealed the password would leave it in a plain dict in the merge frame's
  locals, which a crash traceback shows. Both merge sites (`inventory/creds.py` and
  `inventory/resolve.py:96`) dump in python mode, which keeps the `SecretStr` objects
  themselves; `CredSpec` validates them back unchanged, and no serialization context
  exists to reveal a password. A test merges two creds that differ only by password, and
  a merge error raised with a planted password renders, locals on, without it.
- **`OTTO_SSH_DEBUG=3`** is asyncssh's raw packet dump, which logs the password-auth
  request and channel bytes carrying sudo/su answers. It stays as an explicit debug
  tool: every place `OTTO_SSH_DEBUG` is documented says so, and otto logs one WARNING
  per process when it applies level 3.
- **Expect responses carry secrets wrapped**: the sudo/su answer is an expect response
  holding a `SecretStr`; the session unwraps it only in the write call. A caller's own
  pair may do the same; a plain-`str` response stays supported.
- **`repr=False` for secrets that cannot be `SecretStr`** (#487 §2), and as a second
  layer on those that can: `TelnetClient.password` and `ConsoleClient.password` hold
  `SecretStr` (the stay-wrapped rule below needs it) and keep `repr=False`; any other
  secret field the guard finds gets one or the other. Hosts' reprs therefore never
  show passwords: their `creds` hold `SecretStr`. An SSH client-key passphrase is the
  `SshOptions.passphrase` field (`SecretStr`), never an `extra` key.
- **The password stays wrapped through otto's own helpers**, not only in `Cred`: the SSH
  connect seam (`ssh_connect`/`connect_and_describe`, `connections.py:378-419`, today a
  `**kwargs` passthrough that binds the password in two otto frames), the telnet,
  console and login-proxy constructors (`connections.py:979,1084`,
  `login_proxy.py:636,723`), the FTP login (`connections.py:879`), and every write
  helper a password passes through (`session.py:1377,1455,1461,1606`,
  `ConsoleClient._write` `console.py:340`, `telnet.py:310`, `interact.py:883,1012`).
  Each carries `SecretStr` and unwraps in the final library or transport call. Verified
  by Astra and Fable (2026-10-28): a `SecretStr` local renders as `SecretStr('**********')`,
  an argument unwrapped in the call expression is not in the caller's frame, and
  library objects holding the password (asyncssh `SSHClientConnectionOptions`) render
  as an opaque `<… object at …>`.
- **Library frames hidden** *(Chris; verified by Astra and Fable with planted-password
  experiments, both with controls)*: Rich's `suppress=` renders a suppressed module's
  frames as file and line only, without code or locals (`rich/traceback.py:808-833`).
  The one list is otto's existing `_suppressed_packages()` (`logger/formatters.py:261`,
  today typer and click), extended with asyncssh, telnetlib3, aioftp and asyncio (a
  failed telnet write leaves the raw bytes in asyncio's transport frame,
  `asyncio/selector_events.py:904`). Its consumers:
  - the log files: `render_traceback` already passes it (`formatters.py:321`); covered by
    the one edit, including `record_command_failure` and `logger.exception`;
  - the console log handler: passes it as `tracebacks_suppress`
    (`logger/management.py:280-292`); it shows no locals today, so this only hides
    library code;
  - the CLI crash: typer builds its own suppress list (`typer/main.py:77`), has no
    setting to extend it, and re-installs its excepthook on every call
    (`typer/main.py:1134`). So otto renders the crash itself in `entry()`'s existing
    `except BaseException` leg (`cli/main.py:1323`) with `show_locals=True` and the same
    list, and exits 1. `pretty_exceptions_enable` is turned off so the crash prints
    once.
- **Exception messages are not covered by hiding frames** (both reviewers): Rich prints
  an exception's message regardless, and asyncio `Task` reprs embed it. Rule: otto never
  puts a password in an exception message or log message, and an acceptance test drives
  each library's authentication failure (asyncssh, telnetlib3, aioftp) and the sudo/su
  refusal with a planted password and asserts it is absent from the rendered CLI crash
  and log file. asyncssh's auth-failure text is fixed and password-free; the test pins
  the other two.
- **Guard**: a unit guard scans otto's dataclasses and pydantic models for fields named
  like `password`/`passphrase`/`secret`/`token` and fails unless each is `SecretStr` or
  repr-hidden.

- **#549, expect responses never logged** *(my call)*. The DEBUG "expect matched" line
  (`session.py:1116-1119`) logs the pattern and `response=<N bytes>` for every pair, not
  only otto's elevation pair: a caller's `("Password:", secret)` is as secret as otto's.
  Under `LogMode.NEVER` no response is logged at all, and the elevation answer is sent
  with `LogMode.NEVER`.
- **#548, terse expect reports** *(Chris approved the shape)*. A failing `expect()`
  reports the whole call's source (multi-line calls included, found by parsing the
  source with `ast`), the message, and `Values:` with **only the plain variable names the
  call's arguments read** (`ast.Name` loads; no attribute chains, so no property or
  descriptor runs and `cred.password` is never fetched). Each value is rendered by:
  - a compact summary for otto's domain objects, through one protocol method on the
    base host class, `Lab` and `OttoContext` (e.g. `<UnixHost router1>`), as Chris
    approved, and on `CommandResult` (status, retcode and the first ~60 characters of
    `value`), since nearly every failing expect is about one;
  - a `reprlib`-bounded repr capped at about 100 characters for everything else;
  - `'***'` for a name that looks like a credential (`password`, `passwd`, `secret`,
    `token`, `passphrase`), whatever its value.
  When the call cannot be found unambiguously, `Values:` is omitted, never replaced by
  all locals. The `Locals:` section goes away.

### Acceptance cases

A planted password appears in none of: console, `console.log`, `verbose.log`, `junit.xml`,
an `expect()` report (including lines naming `ctx`, `cred` and `password`), a CLI
traceback, a logged traceback, a DEBUG run of an elevated command, a DEBUG run with a
caller's secret expect pair, rich-rendered `CredSpec`.

## 3. Elevation password sent only to the elevation prompt (#551)

Constraints found in review: the bash frame prints BEGIN before sudo runs, so "only before
BEGIN" breaks sudo; matching "the end of received text" is a no-op because each read ends
at its match; and wrapping the command in `sh -c` would change what sudoers authorizes and
break compound commands. So the command line stays exactly as today (sudo takes the
command as argv, `privilege.py:223-227`; su quotes it as one word) and the fix is in the
prompt and the answer.

- **Unpredictable sudo prompt, not in argv** *(my call)*: each elevated command sets a
  random prompt through the `SUDO_PROMPT` environment variable on the sudo invocation
  (`SUDO_PROMPT='otto-sudo-<random>:' sudo -S <cmd>`), replacing `-p`. It never appears
  in any process's argv, so `ps`, `/proc/*/cmdline` or a planted process name cannot
  produce it, and sudo's `env_reset` keeps it out of the command's environment. The
  expect pair matches that exact string (`re.escape`d). sudo expands `%u %h %p %U %H %%`
  in prompts, so the random token is `%`-free. The command line otto types is echoed
  before BEGIN, so only matches after BEGIN count, and that gate holds even on a chunk
  where a pair matched (today `session.py:1126-1128` skips BEGIN detection then).
- **The assignment is prepended in `_elevate`** itself (`SUDO_PROMPT=… sudo -S <cmd>`).
  Password expects already force the shell route (`session.py:3252`), so no SSH env
  forwarding or login-shell env clearing is involved. `-S` still reads the password
  from stdin.
- **Once**: the credential pair answers at most once per invocation. A second elevation
  prompt means the password was refused: otto interrupts the command through the
  existing interrupt-and-probe recovery (`session.py:1191`, `:1311`), discards the
  session if the probe fails, and raises "password refused for <user> on <host>"
  instead of waiting for the timeout. Tests prove the next command on the same session
  is accepted, and cover cancellation during recovery. *(my call)*
- **su** has no settable prompt. Under the supported policy below su prompts a non-root
  caller before running the command, so its first `[Pp]assword:` after BEGIN is su's
  own. Once-only applies. The pattern stays the shared `_SU_PROMPT`
  (`login_proxy.py:140`). The credential pair is not armed at all when the session is
  already the target user (root), reusing the identity check `login_proxy.py:149`
  already makes, so a root session running `su -c` never answers a `Password:` in
  output. *(Astra r3-2)*
- **Supported elevation policy, documented** *(Astra r3-1, r3-4)*: (a) sudoers keeps its
  default `env_reset` and does not list `SUDO_PROMPT` in `env_keep`, so the command
  never sees the prompt; (b) sudo's prompt is otto's: `SUDO_PROMPT` overrides sudoers
  `passprompt`, but a custom PAM prompt wins unless `passprompt_override` is set (the
  same constraint `-p` has today, so no regression); a non-matching prompt stalls until
  the command timeout as it does today; (c) su prompts for a password (no
  passwordless PAM rule such as `pam_wheel trust` for otto's user). Outside this
  policy the once-only rule still caps exposure at one send. The elevation docs state
  the policy.
- Callers' own expect pairs are unaffected: repeatable, not disarmed.
- **Residual, documented**: an elevated command that itself dumps other processes'
  environments (`ps e` under sudo, while sudo is still running) could print the prompt;
  with cached credentials and once-only, that is the one remaining path, and it needs
  the operator's own command to do it. The prompt also appears in otto's own DEBUG
  session log; that is harmless, since it is per invocation and worthless once the
  command ends.

### Acceptance cases

sudo with a wrong password fails fast with "password refused"; sudo with cached
credentials running a command whose output contains `otto-sudo:`, the old fixed prompt,
and a `Password:` line sends no password; a planted process named like the prompt sends
no password; su sends the password once; a correct password still works on sudo and su
hosts; the exact command line for sudo and su is pinned by `tests/unit/host/test_privilege.py`.

## 4. NetBox address and hop commands (#547)

- **Validate at the model boundary**: `InventoryRecord.ip` and `HostSpec.ip` accept an IP
  address or a strict DNS hostname (RFC 1123 labels). The docs say `ip` may be a DNS
  name, so IP-only would break documented use. IPv6 scope IDs, which `ip_address`
  accepts loosely, are limited to an interface-name pattern. One validator, shared with
  `InterfaceSpec.ip` (`host/specs.py:205-212`), with a parameter for whether hostnames are
  allowed.
- **Every interpolation sink quoted for its own syntax** (defense in depth):
  - shell: `HOP_DIAL_BASH`, `HOP_DIAL_NC` (`host/survey/dial.py`). `HOP_DIAL_BASH`
    already wraps its body in single quotes, so values are passed as positional
    arguments to `bash -c` rather than spliced in. Reuse `bash_script`
    (`check/clock.py:26`) and the existing argv builders.
  - socat addresses: `tunnel/check_probes.py:178,180,328` (TCP and UDP) and
    `link/probes.py:242`: validated against socat's address syntax as well as
    shell-quoted.
  - embedded Python: `link/probes.py:258,285` interpolate into Python source; values go
    in as `repr()` literals.
  - power templates (`host/power.py`): a template such as `ssh hv virsh start {name}`
    re-parses a value on the remote side, where one layer of shell quoting no longer
    holds. Each placeholder value must match the shell-inert charset
    `[A-Za-z0-9_.:@%+=,/-]` (empty allowed, rendering nothing) and is inserted
    unquoted, inert at any nesting depth; any other character is refused naming the
    placeholder, the host and the character.
  - one quoter per kind of shell word: `otto.host.shell_quote.shell_operand` (refuses a
    leading `-`) and `path_operand` over it (a remote path; a leading `-` is spelled
    `./-…`), used by every host command that splices a value or path as an operand.

### Acceptance cases

A NetBox custom field holding `1.2.3.4'; touch /tmp/pwned; '` is refused at the model with
the field named; each sink, fed a hostile value directly (bypassing the model), produces a
command that treats it as one inert word or literal.

## 5. Docker staging on a shared parent (#550, #751)

State on origin/main: env values reach compose through a 0600 launcher (#748). Still open:
the root `/tmp/otto-docker/<project>` is shared, predictable and made 0755; sidecars,
compose files and build contexts keep default modes; per-transfer staging
`/tmp/otto-docker-stage/<cid>-<uuid>` likewise; the launcher's `exec env 'K=V' …` shows
values in `env`'s argv to every account on the parent; dry-run previews print the
launcher with its values.

- **Per-account private root** *(my call)*: `/tmp/otto-docker-<login>/`, where `<login>` is
  the parent's transport login, the account SFTP uploads as. The path builders stay pure
  (`staging.py:303-323`): the login comes from the host's creds, so dry-run can print the
  path without running anything. Use-case, build and per-transfer staging all live
  under it. One function owns the root, used by `compose_staging_root`,
  `use_case_compose_paths`, `use_case_launcher` and the transfer path.
- **One staging identity**: if the parent's command session runs as a different account
  from the transport login (a login proxy), uploads and commands would disagree about a
  0700 root, so otto refuses docker staging on that parent with a message naming the two
  accounts; the docs give the fix (declare the account that runs docker as a direct
  cred on the parent). *(my call; this setup is rare and was already fragile)* With that
  refusal, commands and uploads run as one account by construction, so staging stays
  backend-agnostic (`parent.exec`/`parent.put`), and scp-only parents (dropbear) keep
  working.
- **Race-safe creation**: `mkdir -m 0700` through the command session (atomic, mode set
  exactly regardless of umask; fails if the path exists). If it exists, one `stat` that
  does not follow links reads type, owner UID and mode together (`stat -c '%F %u %a'`,
  BusyBox-compatible, with `ls -ldn` as the fallback); otto proceeds only for a
  directory owned by the session's UID with mode 0700, and otherwise refuses, naming the
  path. It never repairs or adopts the path. `/tmp`'s sticky bit stops another account
  removing or renaming it. The check runs before the existing
  `rm -rf base && mkdir -p base` (`staging.py:716-718`).
- **No deletion on down**: f6ae8a377's `down` reads the staged files back to scope
  teardown to what the deploy started; deleting them would make a later `down` fall back
  to the label teardown with `--remove-orphans`, removing containers the first `down`
  deliberately kept. With the root private, staged files are readable only by that
  account and root, which closes #550's disclosure and tampering. otto does not remove
  `/tmp/otto-docker-<login>/<project>`; the docs tell the user to delete it by hand once
  the deployment is down.
- **Legacy staging read back, never widened, never trusted blindly** *(Astra r3-3)*: when a
  deployment has no staging under the new root but has it under 0.17's
  `/tmp/otto-docker/<project>`, `down` uses it only if the same one-`stat` check (the
  root's check, with "not writable by group or others" instead of "exactly 0700")
  passes for `/tmp/otto-docker`, the project directory, the compose directory, the
  numbered `compose/<n>/` directories and compose files `down` passes as `-f`, and the
  launcher: owned by the session's UID, no symlinks, each directory searchable by its
  owner. A directory that reads as absent (the probe answers absent only where it
  could look) means there is no legacy staging, and nothing is judged; trust is
  judged only when the staging is there. A path behind a directory the session cannot
  search reads as unreadable, and an unreadable directory refuses rather than send the
  teardown to the label. Teardown executes the staged launcher, so a tree another
  account could have written is refused, naming the path and telling the user to tear
  that deployment down by hand; otto never falls back to the broader label teardown in
  that case. Nothing new is written there. Docs tell users to delete the old directory
  once their 0.17 deployments are down: it is world-readable and holds their old env
  file.
- **Trust assumption, stated**: the root's safety rests on `/tmp` being sticky and owned
  by root (checked once, refused otherwise); interference by the same UID or by root is
  out of scope.
- **Dry-run previews redact launcher values** (`deployment.py:836`): `KEY=<redacted>`.
- **No secrets on any command line**: question 1.
- Docs state that the private root is per account, so two engineers sharing one login and
  deploying the same project still collide (unchanged, unsupported).

### Acceptance cases

Another account cannot read or replace staged files (permissions asserted on the parent);
a pre-created root owned by another UID, or a symlink, is refused; a dry-run shows no env
value; the second `down` after a successful `down` removes nothing the first kept; a
deployment staged by 0.17 tears down with its 0.17 scope.

## 6. Test fixture name (#546)

- The fixture package gets a PEP 440 local version: `otto_fixture_beetroot-0.1.0+otto.fixture`,
  and `tests/project4` requires `otto-fixture-beetroot == 0.1.0+otto.fixture`. PyPI rejects
  uploads with local versions, so PyPI can never satisfy it.
- The installs the suite drives see no other index: through the existing test-environment
  seam (`tests/e2e/_otto_subprocess.py`), the e2e environment sets
  `PIP_CONFIG_FILE=/dev/null` and `UV_NO_CONFIG=1` and clears
  `PIP_INDEX_URL`/`PIP_EXTRA_INDEX_URL`/`UV_INDEX_URL`/`UV_EXTRA_INDEX_URL`, so user,
  global and site installer config cannot add a private index that could serve the
  local version.
- Filename, metadata, the embedded `__version__`, wheel assertions, the fixture builder
  (`tests/_fixtures/wheels/build_fixture_beetroot.py`), its README and the examples in
  `docs/cli/env/index.md` change together.
- Focused checks prove both uv and pip resolve the pinned local version from find-links,
  and the "must fail" arm fails because the version is unsatisfiable.
- Covers `tests/e2e/cli/test_preflight_e2e.py:121` as well as `test_env_e2e.py`.
- Chris may also register the name on PyPI as extra protection; that needs his account,
  so it is optional and outside this branch.

## Breaking changes

- SSH host keys are checked by default (accept new, refuse changed); entries are keyed by
  host ID and never by address; `known_hosts` is a file path and `None` is refused;
  `~/.ssh/config` known-hosts settings and `HostKeyAlias` are not read; X.509 subject
  lines are refused; `extra` may not name `host_key_alias`/`client_factory`.
- `Cred.password`/`CredSpec.password` are `SecretStr` (read with `get_secret_value()`); tracebacks hide the frames of the libraries that receive passwords.
- A failing `expect()` reports `Values:` instead of `Locals:`.
- `TelnetClient`'s repr omits the password.
- DEBUG logs no longer show any expect response.
- sudo's prompt comes from `SUDO_PROMPT`, not `-p`; a refused elevation password fails
  fast instead of timing out.
- `HostSpec.ip`/`InventoryRecord.ip` refuse values that are neither an IP nor a valid
  hostname.
- Docker staging moves to `/tmp/otto-docker-<login>/` (0700); a login-proxied docker parent
  is refused; dry-run previews redact env values; plus question 1's outcome.

## Docs

- `docs/configuration/host-options.md`: host-key checking, the floating-IP recipe
  (`forget-key`, or `verify_host_key = false` per host), `known_hosts` as a path and the
  `~/.ssh/config` note, port forms and the plain-`ssh-keygen` equivalent, certificate
  trust through `@cert-authority`, the CI recipe (workspace `known_hosts` path, a
  pre-seeded read-only file, or a `".*"` opt-out).
- Stale statements: `docs/installation.md:710` ("hop hosts are always unverified"),
  `docs/extend/connection-options.md` ("Otto's default already disables host-key
  checking"), `docs/configuration/settings.md` legacy SSH section; regenerate
  `docs/examples/captures/zephyr37-debug-run.txt`.
- `docs/cli/host/` lists `forget-key`.
- Elevation docs: the `SUDO_PROMPT` prompt and the fail-fast refusal.
- `docs/setup/test-recipes.md`, `docs/architecture/subsystems/execution.md` on `expect()`.
- `docs/cli/docker/use-cases.md`: staging location, dry-run redaction, repeated-down,
  legacy staging cleanup, per-account root, login-proxied parents.
- `tests/_fixtures/wheels/README.md`, `docs/cli/env/index.md`.

## Delivery *(my call)*

One branch, one persisting commit per item, landed together, each with an independent Opus
review; Astra and Fable review the whole branch before the gate. Order: §2 (repr and
traceback guard, which §1's tests rely on), §1, §3, §4, §5, §6.

## Left to the builder

Names of new helpers, modules and errors; exact error, help and doc wording; the hostname,
interface-name and credential-name patterns; the summary protocol's name; the
randomness source; lock-file name; test placement.

## Questions for Chris

1. ~~Docker env keys~~: settled by Chris: backlog, P2 - Low, filed as #755. Original framing: The launcher accepts env keys that are not shell identifiers
   (`deployment.py:429`), which forces `exec env 'K=V' …`, putting values in `env`'s argv
   where every account on the parent can read them with `ps`. Compose interpolation
   (`${VAR}`) only accepts identifier names anyway, so a non-identifier key can only reach
   a container through `environment:` passthrough, and no doc or fixture uses one.
   Restricting keys to identifiers lets the launcher `export` them and `exec "$@"`, with
   no argv exposure. Recommendation: restrict. Alternative: keep arbitrary keys and
   accept the exposure.
2. ~~Traceback locals~~: settled by Chris: stay on, no switch; passwords travel as `SecretStr`; library frames hidden (§2).

## Review resolutions

Round 1 and round 2 findings from Astra (A) and Fable (F), with where each is resolved.
Matching by alias and address (A1, F-A1, F-B1) → §1 Matching. IDs at connect sites (A2,
F-A9) → §1 alias. File resolution and `None` (A3, F-A2) → §1 The file. Certificates,
revocation, hashed and port forms (A4, F-A3, A-r2-4, A-r2-24) → §1 Matching, client
object, forget-key. Persisting before proof or after authentication (A5, A-r2-5) → §1
client object step 2 (`begin_auth`). Unknown vs changed in the hook and key-type rotation
(F-r2-N1) → §1 step 1, Key-type rotation. su anchoring, cached-sudo leak, wrapper shell
(A6, F-A6, A-r2-20/21/22, F-r2-N2) → §3 redesigned: `SUDO_PROMPT`, no wrapper, no marker.
Once-only scope and retry (A7, F-A5) → §3 Once. Traceback locals (A8) → §2, question 2
(F-r2-N4). Rich repr (A9) → §2. expect identity and dotted-chain leak (A10, A-r2-10, F-B5,
F-r2-N6) → §2 plain names, summary protocol, credential-name masking. Caller secret
pairs (A-r2-23) → §2 #549. Sinks and IPv6 (A11) → §4. Race-safe root and staging
identity (A12, A-r2-12, F-A10, F-r2-N5) → §5. Teardown widening and legacy staging (A13,
A14, A-r2-13) → §5 No deletion, Legacy. Launcher keys and previews (A15, A-r2-15/19,
F-r2 (4)) → §5, question 1. Fixture isolation (A16, A-r2-16, F-r2 (2)) → §6. Breaking
and docs inventory (A17, A-r2-17, F-r2-N7) → those sections. Unwritable file too broad
(F-A7, F-B3, F-r2-N3) → §1 Missing or unwritable. Error naming and hops (F-A4, F-B2) → §1
One error. Lock (F-A8) → §1. forget-key exit status (F-r2-N6) → §1 Behavior. Shared
login (F-B4) → §5 docs. Not verified by running: PyPI's local-version refusal and actual
uv/pip resolution (A18, A-r2-18) → §6 focused checks prove them in the branch.
Round 3: begin_auth abort, plain-name capture, no wrapper all confirmed settled by both.
Astra r3-1/r3-4 (`SUDO_PROMPT` reaching the command, PAM prompts) and r3-2 (root
session, passwordless su) → §3 supported policy and root disarm. A-r3-3 (legacy launcher
trust) → §5 Legacy. A-r3-6 (recovery) → §3 Once. A-r3-7, F-r3-1 (staging mechanism, scp
parents, sticky `/tmp`) → §5 identity, creation, trust assumption. F-r3-3 (`CommandResult`
summary) → §2. F-r3 sudo `%` escapes and BEGIN gate → §3. F-r3-4 (prompt in DEBUG log)
→ §3 residual.
