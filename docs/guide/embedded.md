# Embedded Hosts

An embedded host is a firmware or RTOS target reached over a serial console —
typically via a telnet connection, often through an SSH hop.  Shell I/O is
wrapped in a *command frame* that encodes each command and parses the
output/return-code back from the plain telnet byte stream.

Embedded hosts expose the same `Host` API as Unix hosts (`run` / `exec` /
`send` / `expect` / `put` / `get`), so test code does not branch on host type.
The key differences are:

- **One console.** An embedded target exposes a single shell.  There is no
  second channel to run commands out-of-band, so `exec` shares the
  persistent session with `run` and is **not** concurrency-safe.
- **No bash.** No `$?`, no command substitution, no `scp`/`ftp`/`nc`.  Command
  framing and file transfer use a device-shell protocol, not Unix tools.
- **Telnet only.** The shell is reached over telnet (optionally through an SSH
  hop), never SSH directly.

## Host class taxonomy

```text
Host (Protocol) / BaseHost (ABC)
├── LocalHost(BaseHost)            local machine, no network
├── RemoteHost(BaseHost)          abstract base for networked hosts
│   ├── UnixHost(RemoteHost)      SSH/Telnet shell; SCP/SFTP/FTP/nc transfer
│   └── EmbeddedHost(RemoteHost)  console-framed RTOS/firmware target;
│       │                         OS-agnostic — fails loud with no command_frame
│       └── ZephyrHost(EmbeddedHost)   concrete Zephyr defaults
└── DockerContainerHost(BaseHost) container host
```

## Selecting an embedded host

Set `os_type` in the host's `lab.json` entry to select the host class:

- `os_type: "zephyr"` builds a `ZephyrHost` with Zephyr-specific defaults
  (the `zephyr` command frame, `os_name: "Zephyr"`).  Use `os_version` to record
  the exact kernel version — `"2.7"`, `"3.7"`, and `"4.4"` appear in the
  in-tree test fixture.
- `os_type: "embedded"` builds a bare `EmbeddedHost` with no OS-specific
  defaults.  Because `EmbeddedHost` carries no default `command_frame`, it
  **fails loud** at construction if none is supplied:

```text
EmbeddedHost '<name>' has no command_frame. A bare 'embedded' host carries no
shell-framing dialect. Set os_type to a profile that supplies one (e.g.
"zephyr"), or pass an explicit command_frame.
```

Supply a frame via a profile (see {doc}`os-profiles`), or by setting
`command_frame` directly in the host entry.

## Command frames

A command frame is a small stateless strategy object that:

1. wraps each command in unique sentinels (BEGIN/END markers, a return-code
   probe), and
2. parses the echoed byte stream back into `(output, retcode)`.

How the wrapping and parsing work differs per target OS — that variation is the
frame's dialect.

### Built-in frames

| Name | Class | Notes |
|------|-------|-------|
| `zephyr` | `ZephyrFrame` | Stock Zephyr `retval` shell (3.7 / 4.4 LTS).  Default for `ZephyrHost`. |
| `zephyr-serial` | `ZephyrSerialFrame` | Same framing as `zephyr`; differs only in handshake.  For a UART shell bridged via QEMU `-serial telnet:` (raw byte bridge, not the in-guest `SHELL_BACKEND_TELNET`). |
| `bash` | `BashFrame` | POSIX bash; used internally by SSH/telnet Unix sessions. |

Declare a frame by name in lab data:

```json
{
    "element": "sprout_no_fs",
    "os_type": "zephyr",
    "command_frame": "zephyr-serial"
}
```

### Custom frames

Projects can register additional dialects from an init module:

```python
from otto.host.command_frame import register_command_frame, ZephyrFrame

class ZephyrInlineRetcodeFrame(ZephyrFrame):
    type_name = "zephyr-inline"
    # ... override parse_retcode / parse_output for 2.7 inline retcode

register_command_frame(ZephyrInlineRetcodeFrame.type_name, ZephyrInlineRetcodeFrame)
```

See {doc}`extending-embedded` for a full walkthrough of writing and
registering a command frame.

## Embedded filesystems

The `filesystem` field declares the on-device filesystem variant.  It controls
the mount path, the optional `fs mount` command issued before the first
transfer, and the command-formation hooks the file transfer code drives.

### Built-in filesystems

| Name | Class | Mount path | Notes |
|------|-------|------------|-------|
| `none` | `NoFileSystem` | — | No filesystem.  Transfer and disk metrics short-circuit to a clear no-op / error. |
| `fat-ram` | `FatRamFileSystem` | `/RAM:` | FAT on a RAM disk.  Otto issues `fs mount fat /RAM:` on first transfer (Zephyr 3.7 LTS does not auto-mount FAT). |
| `littlefs` | `LittleFsFileSystem` | `/lfs` | LittleFS on simulated flash.  Auto-mounted at boot via `zephyr,fstab`; no mount command needed. |

The `max_filename_len` field caps the basename length (including extension)
accepted by the target filesystem.  Defaults to `255`.  Override per-host
when the firmware enforces a tighter limit — for example `32` for a build with
`CONFIG_FS_FATFS_MAX_LFN=32` or `CONFIG_FS_LITTLEFS_NAME_MAX=32`.

### Custom filesystems

Projects can register custom variants from an init module:

```python
from otto.host.embedded_filesystem import register_filesystem, EmbeddedFileSystem

class MyFlashFs(EmbeddedFileSystem):
    type_name = "my-flash"
    mount = "/flash"

register_filesystem(MyFlashFs.type_name, MyFlashFs)
```

## File transfer

The `transfer` field selects the file-transfer backend for an embedded host:

| Value | Description |
|-------|-------------|
| `console` | Default.  Drives the device shell's `fs` commands (`fs write`, `fs read`). Requires a real filesystem (not `none`). |
| `tftp` | Reserved; not yet implemented. |

This is distinct from the Unix transfer set (`scp`, `sftp`, `ftp`, `nc`), which
are unavailable on embedded hosts — they require a POSIX shell.

## Example

A `sprout` entry from the test fixture, annotated:

```json
{
    "ip": "192.0.2.1",
    "element": "sprout",
    "os_type": "zephyr",
    "os_version": "3.7",
    "transfer": "console",
    "filesystem": "fat-ram",
    "max_filename_len": 32,
    "is_virtual": true,
    "hop": "basil_seed",
    "snmp": {
        "address": "10.10.200.14",
        "port": 16101,
        "community": "public",
        "oids": [
            "1.3.6.1.2.1.1.3.0",
            "1.3.6.1.4.1.63245.1.1.0",
            "1.3.6.1.4.1.63245.1.2.0",
            "1.3.6.1.4.1.63245.1.3.0",
            "1.3.6.1.4.1.63245.1.4.0"
        ]
    },
    "resources": [
        "sprout"
    ],
    "labs": [
        "embedded"
    ]
}
```

Key fields:

- `os_type: "zephyr"` — builds `ZephyrHost` with the `zephyr` command frame and
  `os_name: "Zephyr"`.
- `os_version: "3.7"` — recorded on the host; selects the correct test paths
  (e.g. coverage SDK version).
- `transfer: "console"` — file I/O over the device's `fs` shell commands.
- `filesystem: "fat-ram"` — FAT on RAM disk, mounted at `/RAM:`.
- `max_filename_len: 32` — firmware `CONFIG_FS_FATFS_MAX_LFN` ceiling.
- `hop: "basil_seed"` — all connections route through the `basil_seed` jump host.
- `snmp` block — enables SNMP-based metric collection alongside the telnet
  console (a separate channel).

## See also

- {doc}`lab-config` — full `lab.json` schema reference
- {doc}`os-profiles` — custom host classes and data profile bundles
- {doc}`extending-embedded` — writing custom command frames and filesystems
- {doc}`coverage` — cross-toolchain configuration for embedded coverage
- {doc}`monitor` — SNMP monitoring configuration
