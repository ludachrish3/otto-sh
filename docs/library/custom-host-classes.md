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


@dataclass(slots=True, kw_only=True)
class MyRtosHost(EmbeddedHost):
    """Custom RTOS host with project-specific defaults."""

    os_type: str = "my-rtos"
    os_name: str | None = "MyRTOS"
    command_frame: ZephyrFrame = field(default_factory=ZephyrFrame)


register_host_class("my-rtos", MyRtosHost)
```

`kw_only=True` is what keeps the constructor the shape every in-tree family
has: `MyRtosHost(ip)` positionally, everything else by keyword.  Without it
your three overrides join the positional signature behind `ip` —
`MyRtosHost(ip, os_type, os_name, command_frame)` — which no otto host has.
If your class genuinely needs a positional parameter of its own, keep the
class `kw_only=True` and mark that one field `field(kw_only=False)`, the way
{class}`~otto.host.remote_host.RemoteHost` marks `ip`.

Your class must also declare a `capabilities`
({class}`~otto.host.capability_grid.HostCapabilities`) saying what its verbs
promise for `user=`, progress and session identity — `register_host_class`
refuses a class that does not, so no family reaches a reader as a blank row in
{doc}`../guide/hosts/families`.  Subclassing `EmbeddedHost` or `UnixHost`
inherits theirs; redeclare only where yours differs.

`ZephyrHost` in `otto.host.embedded_host` is the in-tree worked example — it
re-declares `os_type`, `os_name`, and `command_frame` as class-level field
defaults and is registered under `"zephyr"` at module load.

### What you inherit, and what you may re-declare

{class}`~otto.host.host.BaseHost` and {class}`~otto.host.remote_host.RemoteHost`
are `@dataclass(kw_only=True)` bases, and each shared field — its type, its
docstring, its default — is declared there exactly once.  `BaseHost` holds what
all five families answer; `RemoteHost` holds what the networked families add.
A subclass inherits the lot with its defaults already in place, whether it
subclasses `EmbeddedHost`, `UnixHost`, or `RemoteHost`/`BaseHost` directly:
there is nothing to copy, and no field you must re-declare to make it exist.
The field-by-field reference is generated from the classes themselves — see
{class}`~otto.host.host.BaseHost` and {class}`~otto.host.remote_host.RemoteHost`
in the API pages.

Re-declare a base field only to change its *value policy*: a different default,
`init=False`, or "required here".  Do it keyword-only — either under a
`kw_only=True` class as above, or as `field(kw_only=True, ...)` on that one
line — so the override cannot shift the positional signature.  An override
carries **no docstring**: the docstring lives with the field's one home, and
`tests/unit/host/test_field_homes.py` holds otto's own leaves to that shape.

### Migrating a subclass written before the bases became dataclasses

Three things moved when the bases became dataclasses:

- **Hosts are value-compared and unhashable.** The bases generate `__eq__`, so
  `__hash__` is `None` on every host class, decorated or not.  A host can no
  longer be a dict key or a set member — key on `host.id` instead.
- **`element` is keyword-only on the embedded family.** It used to be the
  second positional argument of `EmbeddedHost`; pass `element=` now.
- **The `Host` protocol names four more members** — `app_shell`, `as_user`,
  `switch_user` and the `current_user` property.  They were always there on
  `BaseHost`; now the contract says so, and
  {func}`~otto.testing.assert_host_conforms` asks your class for them.
  `BaseHost.as_user`'s refusal is an async context manager that raises
  `NotImplementedError` on `__aenter__`, so a family that cannot switch
  identity refuses at `async with host.as_user(...)`, not at the call.

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
