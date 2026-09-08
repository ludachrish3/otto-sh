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

Your class must also declare a `capabilities`
({class}`~otto.host.capability_grid.HostCapabilities`) saying what its verbs
promise for `user=`, progress and session identity — `register_host_class`
refuses a class that does not, so no family reaches a reader as a blank row in
{doc}`../guide/hosts/families`.  Subclassing `EmbeddedHost` or `UnixHost`
inherits theirs; redeclare only where yours differs.

`ZephyrHost` in `otto.host.embedded_host` is the in-tree worked example — it
re-declares `os_type`, `os_name`, and `command_frame` as class-level field
defaults and is registered under `"zephyr"` at module load.

Subclassing `EmbeddedHost` or `UnixHost` inherits every field otto's loader
stamps.  A class that subclasses `RemoteHost` (or `BaseHost`) **directly** must
declare them itself — among them `resources`, `element`, `inventory_ref` and
`lab_info`, each with its own `field(...)` default.  Neither `RemoteHost` nor `BaseHost` is a
dataclass, so their annotations are a contract the type checker credits to
every subclass while creating no attribute and no dataclass field: the first
read raises `AttributeError`.  The failure is loud and happens at load rather
than mid-run, but nothing warns you before it.

### Proving it

```python
from otto.host.element import Element
from otto.testing import assert_host_conforms


def test_my_rtos_host_conforms():
    assert_host_conforms(MyRtosHost, instance=MyRtosHost(ip="192.0.2.1", element=Element("dev")))
```

{func}`~otto.testing.assert_host_conforms` checks the call shapes production
depends on — read off the `Host` protocol itself, so a parameter added there is
asked of your class rather than silently skipped — and, given an `instance`,
probes each verb against what your `capabilities` promise for it: a verb
declared `refused` must raise `NotImplementedError`, and one declared anything
else must not.  The probes run inside a dry-run context, so nothing connects and
no bytes move; what each value means is in {doc}`../guide/hosts/families`.  Call
it from a synchronous test — the probes drive their own event loop.

Your `capabilities` are read per **class**, while behaviour can depend on the
**instance**: there is no dimension in the declaration for a host's own
configuration.  A class whose answer varies that way conforms on one instance
and reports a violation on another, both truthfully — otto's own `unix` row
declares `exec_user=authenticate`, which holds over `term="ssh"` while a
`term="telnet"` host refuses.  Probe the configuration your declaration speaks
for, and put the conditions in your row's `note` so a reader of
{doc}`../guide/hosts/families` sees them too.

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
