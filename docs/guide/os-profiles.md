# OS Profiles & Custom Host Classes

The `osType` field in a `hosts.json` entry is a *selector* that resolves to an
`OsProfile`.  A profile names a registered host class (its `base`) and carries
an optional bundle of raw field defaults merged beneath each host's own fields.
This lets many hosts that share a characteristic bundle — a particular Zephyr
build's `command_frame`, `filesystem`, and `max_filename_len` — name that
bundle once instead of copy-pasting it into every entry.

Built-in profiles registered at startup:

| `osType` | Host class | Notes |
|----------|------------|-------|
| `unix` | `UnixHost` | Default when `osType` is absent. |
| `embedded` | `EmbeddedHost` | OS-agnostic bare-metal/RTOS.  Fails loud without a `command_frame`. |
| `zephyr` | `ZephyrHost` | Concrete Zephyr subclass; supplies `ZephyrFrame` and `osName: "Zephyr"`. |

Profiles are authorable two ways, both feeding the same registry:

- **Data** — an `[os_profiles.<name>]` table in `.otto/settings.toml`,
  registered at settings-parse time.
- **Code** — `register_os_profile()` called from an init module listed in
  `settings.toml`, registered after settings parse.  A code registration
  overrides a data table of the same name (last writer wins).

## Data profiles

Add an `[os_profiles.<name>]` sub-table to `.otto/settings.toml`.  The only
required key is `base` — the name of a registered host class.  Every other key
is a raw field default merged beneath each matching host's own fields (with
`${sutDir}` expansion applied).

Example — a profile for a specific Zephyr 3.7 FAT build:

```toml
[os_profiles.zephyr-3.7-fat32]
base            = "zephyr"
osVersion       = "3.7"
filesystem      = "fat-ram"
max_filename_len = 32
```

With this profile in place, a host entry only needs to name the profile:

```json
{
    "ip": "192.0.2.1",
    "ne": "sprout",
    "osType": "zephyr-3.7-fat32",
    "hop": "basil_seed",
    "labs": ["embedded"]
}
```

Unknown `base` values and unknown default field names raise `ValueError` at
startup so typos fail loudly instead of silently no-opping.

## Code profiles

Call `register_os_profile()` from an init module listed in `settings.toml`:

```python
from otto.host.os_profile import register_os_profile

register_os_profile(
    "zephyr-3.7-fat32",
    base="zephyr",
    defaults={
        "osVersion": "3.7",
        "filesystem": "fat-ram",
        "max_filename_len": 32,
    },
)
```

Init modules are imported *after* settings-file parsing, so a code registration
overrides a data table of the same name.  This lets third-party libraries ship
profiles that users can patch from `settings.toml` without editing the library
source.

## Custom host classes

To ship a host subclass from an external repo:

1. Subclass `EmbeddedHost` or `UnixHost` (whichever family fits).
2. Call `register_host_class(name, cls)` from an init module.  This also
   auto-registers a trivial same-named profile so `osType: <name>` resolves
   immediately with no extra config.

```python
from dataclasses import dataclass, field
from otto.host.embeddedHost import EmbeddedHost
from otto.host.command_frame import ZephyrFrame
from otto.host.os_profile import register_host_class

@dataclass(slots=True)
class MyRtosHost(EmbeddedHost):
    """Custom RTOS host with project-specific defaults."""

    osType: str = "my-rtos"
    osName: str | None = "MyRTOS"
    command_frame: ZephyrFrame = field(default_factory=ZephyrFrame)

register_host_class("my-rtos", MyRtosHost)
```

`ZephyrHost` in `otto.host.embeddedHost` is the in-tree worked example — it
re-declares `osType`, `osName`, and `command_frame` as class-level field
defaults and is registered under `"zephyr"` at module load.

## Composition

Layer a defaults bundle over a custom class to create per-build profiles
without writing a new subclass:

```python
from otto.host.os_profile import register_os_profile

# "my-rtos" is already registered as a host class (see above).
register_os_profile(
    "my-rtos-v1",
    base="my-rtos",
    defaults={
        "osVersion": "1.0",
        "filesystem": "fat-ram",
        "max_filename_len": 32,
    },
)
```

Lab-data entries can then use `osType: "my-rtos-v1"` to select this bundle.
The profile's defaults are merged beneath the host's own fields; host fields
always win.

## See also

- {doc}`lab-config` — `hosts.json` schema and repo-level host defaults
- {doc}`embedded` — embedded host classes, command frames, and filesystems
- {doc}`extending-embedded` — writing a custom command frame or filesystem
- {doc}`repo-setup` — `init` modules and `settings.toml` field reference
