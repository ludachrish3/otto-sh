# Extending otto for new embedded targets

otto's embedded-host support is built to be extended from your own project —
the same way you register instructions and suites. Two seams matter when you
bring up a target otto doesn't ship support for:

- a **command frame** — the shell *dialect*: how a command is wrapped to send
  and how its echoed output is parsed back into `(output, retcode)`;
- an **embedded filesystem** — the on-device file API: where files live and
  what shell commands read, write, and stat them.

The two stories often pair (a new RTOS usually has both a new shell and a new
filesystem), but they are independent — you can add one without the other.
Both register from an `init` module listed in `.otto/settings.toml`, so the
registration runs before any lab data loads. For the lab-data fields that
select them (`command_frame`, `filesystem`), see {doc}`lab-config`; for the
host class that carries them, see {doc}`os-profiles`.

## Adding a shell dialect (a `CommandFrame`)

otto drives a remote shell by wrapping each command in unique sentinels — a
BEGIN marker, the command, a way to recover the exit code, and an END marker —
then parsing the echoed byte stream back into `(output, retcode)`. *How* that
wrapping and parsing happen is the shell's dialect, and it differs per target:
a POSIX **bash** shell bakes `$?` into the END marker, while the **Zephyr** RTOS
shell has no `$?` and no generic `echo`, so it appends a stock `retval` builtin
and parses output positionally.

A {class}`~otto.host.command_frame.CommandFrame` makes that dialect a
first-class, **stateless value object** that a session *holds* rather than *is*.
The per-session sentinels (unique per connection so two sessions can't
cross-talk) are passed to the frame as a
{class}`~otto.host.command_frame.SessionMarkers` value, keeping the frame pure
and unit-testable without a live session.

### The seven methods

Subclass `CommandFrame` and implement seven methods — a *render half* (command
→ bytes to write) and a *parse half* (bytes read → structured result). They
live together because they co-vary through *where the retcode lives*; splitting
them would let mismatched halves combine.

| Method | Half | Responsibility |
|--------|------|----------------|
| `handshake(m)` | render | Readiness-probe payload echoing `m.ready`. |
| `frame(cmd, m)` | render | Full payload that runs `cmd` bracketed by the sentinels. |
| `recover(m)` | render | Post-timeout re-sync payload echoing `m.recover`. |
| `end_pattern(m)` | parse | Regex marking the end of a command's output (the session compiles it once and uses it to detect completion and bound parsing). |
| `marks_begin(data, m)` | parse | `True` if `data` is the chunk carrying the BEGIN sentinel. |
| `parse_output(buffer, cmd, m)` | parse | Extract the command's output from the accumulated `buffer`. |
| `extract_retcode(buffer, m)` | parse | Recover the exit code; return `-1` when none can be read. |

Set a unique `type_name` class attribute — the string lab data uses to select
the frame.

The two in-tree implementations are the reference to read side by side:
{class}`~otto.host.command_frame.BashFrame` (`"bash"`) and
{class}`~otto.host.command_frame.ZephyrFrame` (`"zephyr"`), both in
`otto.host.command_frame`. `BashFrame` brackets with `echo` and embeds `$?` in
the END marker; `ZephyrFrame` sends four CR-separated lines
(`BEGIN` / `cmd` / `retval` / `END`) and parses positionally.

### Registering and selecting it

Register the frame from an `init` module, then select it by `type_name` in lab
data:

```python
# myproject/otto_frames.py
import re
from otto.host.command_frame import CommandFrame, SessionMarkers, register_command_frame

class MyShellFrame(CommandFrame):
    type_name = "myshell"

    def handshake(self, m: SessionMarkers) -> str:
        return f"{m.ready}\n"

    def frame(self, cmd: str, m: SessionMarkers) -> str:
        return f'echo {m.begin}; {cmd}; echo {m.end_prefix}$?__\n'

    def recover(self, m: SessionMarkers) -> str:
        return f"echo {m.recover}\n"

    def end_pattern(self, m: SessionMarkers) -> re.Pattern[str]:
        return re.compile(re.escape(m.end_prefix) + r"(\d+)__")

    def marks_begin(self, data: str, m: SessionMarkers) -> bool:
        return data.rstrip("\r\n").endswith(m.begin)

    def parse_output(self, buffer: str, cmd: str, m: SessionMarkers) -> str:
        ...  # slice between the BEGIN marker and end_pattern

    def extract_retcode(self, buffer: str, m: SessionMarkers) -> int:
        match = self.end_pattern(m).search(buffer)
        return int(match.group(1)) if match and match.groups() else -1

register_command_frame("myshell", MyShellFrame)
```

```json
{ "element": "mote", "os_type": "embedded", "command_frame": "myshell" }
```

For a target that should carry the frame as its default (the way `ZephyrHost`
defaults to `ZephyrFrame`), ship a host subclass instead — see *Custom host
classes* in {doc}`os-profiles`.

### Bringing a new shell up

When a new shell doesn't quite cooperate — different unknown-command wording,
different `retval`-equivalent output, different ANSI noise — the loop is **not**
"read the source," it's "watch the bytes":

1. Raise the `otto.host` logger to DEBUG (standard Python logging — no env var,
   no custom dial). From the CLI verbose flag, from
   `logging.getLogger("otto.host").setLevel(logging.DEBUG)`, or from a pytest
   run with `log_level = "DEBUG"`.
2. Run a single command against the target (`await host.run("...")`).
3. Read the log. The session logs the framed write and the marker matching, so
   you see the exact bytes that came back and the buffer each parse method was
   handed — without writing any per-frame logging.
4. If a method's slice is wrong, the logged buffer shows exactly what it had to
   chew on. Fix that one method; the other six are unaffected.

Things you should **not** do, by design:

- **Don't write a read loop, expect handler, or recovery code.** The session
  owns the engine; the frame only supplies dialect. A correct frame inherits
  all of it.
- **Don't add per-frame logging or an env-var verbosity dial.** The session
  already logs at the call site of every framing method; DEBUG is the contract.
- **Don't modify the target's firmware.** otto meets the target as-is. Any
  sentinel behavior the framing relies on must come from what the shell
  *already* does (e.g. Zephyr's unknown-command echo, or a builtin like
  `retval`). If the target can't frame a command, the right answer is a clear
  capability error — not a custom command added to the firmware.

## Adding a filesystem (an `EmbeddedFileSystem`)

An embedded host's on-device filesystem is a typed object on the host —
{class}`~otto.host.embedded_filesystem.EmbeddedFileSystem`. It is the source of
truth for:

- the **mount path** (`/RAM:`, `/lfs`, …) — used as the default destination
  directory and the target of `fs statvfs` in the disk metric;
- the optional **`mount_cmd`** that must run once before the first transfer
  (for filesystems Zephyr can't auto-mount via `zephyr,fstab`);
- the **command-formation hooks** (`read_command`, `write_command`,
  `rm_command`, `trunc_command`, `ls_command`, `statvfs_command`) that
  {class}`~otto.host.transfer.EmbeddedFileTransfer` and the embedded
  monitor's disk parser drive when they talk to the device.

The three built-ins — `NoFileSystem` (`none`), `FatRamFileSystem` (`fat-ram`),
`LittleFsFileSystem` (`littlefs`) — assume the stock Zephyr `fs` shell. The
transfer code and disk parser never hardcode the literal `fs …` strings, so a
subclass that overrides one hook composes cleanly with the inherited defaults.

### Shallow path — new mount, same shell syntax

The common case: a custom build with a filesystem otto doesn't ship a class for
(a non-default FAT mount, NFFS, …) but the same Zephyr `fs` shell. Subclass,
set the class constants, register:

```python
# myproject/otto_filesystems.py
from otto.host.embedded_filesystem import EmbeddedFileSystem, register_filesystem

class NffsFileSystem(EmbeddedFileSystem):
    """NewtNFFS on simulated flash, mounted at /nffs."""
    type_name = "nffs"
    mount = "/nffs"
    # NFFS auto-mounts via zephyr,fstab — no mount_cmd needed.

register_filesystem("nffs", NffsFileSystem)
```

```json
{ "element": "mote_nffs", "os_type": "embedded", "ip": "192.0.2.7", "filesystem": "nffs" }
```

That's the whole change. The host factory resolves `"nffs"` through the
registry to an `NffsFileSystem` on `host.filesystem`; transfers go through
`fs read`/`fs write` at the new mount, and the disk metric reports
`fs statvfs /nffs`.

### Deep path — different on-device command syntax

When the device-side tool is not the stock `fs` shell (a vendor build using
`myfs read` instead of `fs read`), override only the hooks that differ:

```python
class MyFsFileSystem(EmbeddedFileSystem):
    type_name = "myfs"
    mount = "/data"

    def read_command(self, path):
        return f"myfs read {path}"

    def write_command(self, path, offset, hexbytes):
        return f"myfs write {path} {offset} {hexbytes}"

    def rm_command(self, path):
        return f"myfs rm {path}"
    # trunc_command, ls_command, statvfs_command inherit the stock
    # `fs <verb> ...` defaults — override only if the vendor shell differs.

register_filesystem("myfs", MyFsFileSystem)
```

`supports_transfer` and `supports_disk_metric` derive from `mount`
(`supports_disk_metric` defaults to `supports_transfer`); override either if
your filesystem can transfer but lacks `statvfs`, or vice versa.

### Validation

The host factory rejects an unknown `filesystem` before the host is
constructed, listing every registered type so a typo (`"fatram"` vs
`"fat-ram"`) is diagnosable from the message alone. A host whose `filesystem`
resolves to `NoFileSystem` short-circuits transfers with a clear, FS-aware
error before sending any shell command — no hang, no garbled response — and the
disk parser yields nothing for it. Both behaviors are static, declared in lab
data, not runtime-detected.

## See also

- {doc}`embedded` — the embedded-host user guide (selecting frames/filesystems)
- {doc}`os-profiles` — registering a custom host class that bundles these
- {doc}`lab-config` — the `command_frame` / `filesystem` lab-data fields
