# Custom host classes
A **code profile** registers a host *class*: Python that changes how otto
talks to a host, not just what the lab entry says about it. Data profiles —
named bundles of lab-data defaults — are configuration and live in
{doc}`../guide/configuration/os-profiles`.
## Code profiles

Call `register_os_profile()` from an init module listed in `settings.toml`:

```python
from otto.host.os_profile import register_os_profile

register_os_profile(
    "zephyr-3.7-fat32",
    base="zephyr",
    defaults={
        "os_version": "3.7",
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
   auto-registers a trivial same-named profile so `os_type: <name>` resolves
   immediately with no extra config.

```python
from dataclasses import dataclass, field
from otto.host.embedded_host import EmbeddedHost
from otto.host.command_frame import ZephyrFrame
from otto.host.os_profile import register_host_class


@dataclass(slots=True)
class MyRtosHost(EmbeddedHost):
    """Custom RTOS host with project-specific defaults."""

    os_type: str = "my-rtos"
    os_name: str | None = "MyRTOS"
    command_frame: ZephyrFrame = field(default_factory=ZephyrFrame)


register_host_class("my-rtos", MyRtosHost)
```

`ZephyrHost` in `otto.host.embedded_host` is the in-tree worked example — it
re-declares `os_type`, `os_name`, and `command_frame` as class-level field
defaults and is registered under `"zephyr"` at module load.

Subclassing `EmbeddedHost` or `UnixHost` inherits every field otto's loader
stamps.  A class that subclasses `RemoteHost` (or `BaseHost`) **directly** must
declare them itself — among them `resources`, `element_resources`,
`element_metadata`, `inventory_ref` and `lab_info`, each with a
`field(default_factory=...)`.  Neither `RemoteHost` nor `BaseHost` is a
dataclass, so their annotations are a contract the type checker credits to
every subclass while creating no attribute and no dataclass field: the first
read raises `AttributeError`.  The failure is loud and happens at load rather
than mid-run, but nothing warns you before it.
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
        "os_version": "1.0",
        "filesystem": "fat-ram",
        "max_filename_len": 32,
    },
)
```

Lab-data entries can then use `os_type: "my-rtos-v1"` to select this bundle.
The profile's defaults are merged beneath the host's own fields; host fields
always win.
## See also

- {doc}`../guide/configuration/lab-config` — `lab.json` schema and repo-level host defaults
- {doc}`../guide/cli/host/embedded` — embedded host classes, command frames, and filesystems
- {doc}`extending-embedded` — writing a custom command frame or filesystem
- {doc}`../guide/configuration/settings` — `init` modules and `settings.toml` field reference
