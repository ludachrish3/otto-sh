# Custom host classes
A **code profile** registers a host *class*: Python that changes how otto
talks to a host, not just what the lab entry says about it. Data profiles —
named bundles of lab-data defaults — are configuration and live in
{doc}`../../configuration/os-profiles`.
## Code profiles

Call `register_os_profile()` from an init module listed in `settings.toml`:

```python
from otto.host import register_os_profile

register_os_profile(
    "vendor-linux",
    base="unix",
    defaults={"os_version": "5.4"},
    login_prompt=r"(?i)username: ?$",
    password_prompt=r"(?i)password: ?$",
)
```

`login_prompt` and `password_prompt` are what the `console` term's login
waits for on this OS's serial console
({ref}`console-term`); both are compiled at registration, so a bad regex
fails the init module rather than a later connect. A profile carries none
unless you pass them, even one based on `unix`; the built-in values are
listed in {doc}`../../configuration/os-profiles`.

A code profile wins over a data table of the same name, whichever comes
first (see {doc}`../../configuration/os-profiles`), so an `[os_profiles]`
table in `settings.toml` cannot patch a profile a library registers in code.
Registering a name that is already taken raises
{class}`~otto.registry.DuplicateRegistration`; to replace a library's
profile, import the library in your own init module and call
`register_os_profile(..., overwrite=True)` for that name afterwards, or
register the variant under a new name.

## Custom host classes

To ship a host subclass from an external repo:

1. Subclass `EmbeddedHost` or `UnixHost` (whichever family fits).
2. Call `register_host_class(name, cls)` from an init module.  `os_type:
   <name>` then resolves to the class's own profile with no extra config. It
   registers no `os_type` profile: the class's profile is the lowest layer
   ({doc}`../../configuration/os-profiles`), so a code profile or a repo's
   data table of the same name replaces it. Pass
   `profile=ProfileFields(...)` (from `otto.host`) to give it defaults or
   console prompt patterns; without it the profile carries none — even over
   `unix` — so a `UnixHost` subclass whose hosts log in over the `console`
   term passes
   `profile=ProfileFields(login_prompt=..., password_prompt=...)`.
   Every argument after the class is keyword-only, and registering a name
   that is already taken raises {class}`~otto.registry.DuplicateRegistration`
   unless you pass `overwrite=True`.

```python
from dataclasses import dataclass, field
from otto.host import EmbeddedHost, register_host_class
from otto.host.command_frame import ZephyrFrame


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
refuses a class that does not.  Subclassing `EmbeddedHost` or `UnixHost`
inherits theirs; redeclare only where yours differs.

`ZephyrHost` in `otto.host.embedded_host` is the in-tree worked example — it
re-declares `os_type`, `os_name`, and `command_frame` as class-level field
defaults and is registered under `"zephyr"` at module load.

### Fields of your own need a `HostSpec`

A `lab.json` entry reaches your class through a boundary spec, a
{class}`~otto.models.host.HostSpec` subclass that validates the entry and
refuses any key it does not declare.  `register_host_class` takes it as
`spec=`.  Left out, it is the spec registered for the nearest registered
ancestor — {class}`~otto.host.UnixHostSpec` under `UnixHost`,
{class}`~otto.models.host.EmbeddedHostSpec` under `EmbeddedHost` and
`ZephyrHost` — so a class that only re-declares inherited fields, like
`MyRtosHost` above, needs none.

A class that **adds** a field needs its own spec: subclass the inherited one,
declare the field, and hand it on in `to_host`.  Without that, registration
succeeds but every lab entry that sets the field is refused at load:

```python
from dataclasses import dataclass

from otto.host import UnixHost, UnixHostSpec, register_host_class


@dataclass(slots=True, kw_only=True)
class GadgetHost(UnixHost):
    widget: str = "sprocket"


class GadgetHostSpec(UnixHostSpec):
    widget: str = "sprocket"

    def to_host(self, cls=GadgetHost, *, element, preferences=None):
        host = super().to_host(cls, element=element, preferences=preferences)
        host.widget = self.widget
        return host


register_host_class("gadget", GadgetHost, spec=GadgetHostSpec)
```

If the field you add is a runtime object rather than a JSON scalar, convert it
in `to_host` the way `UnixHostSpec.to_host` converts its option tables
(`getattr(self, n).to_runtime()`, in `otto.models.host`).

A class with no registered ancestor — a direct
{class}`~otto.host.remote_host.RemoteHost` subclass — has no spec to inherit,
so `register_host_class` refuses it unless you pass `spec=`, whatever its
fields.

### What you inherit, and what you may re-declare

{class}`~otto.host.host.BaseHost` and {class}`~otto.host.remote_host.RemoteHost`
are `@dataclass(kw_only=True)` bases, and each shared field — its type, its
docstring, its default — is declared there exactly once.  `BaseHost` holds what
all five families answer; `RemoteHost` holds what the networked families add.
A subclass inherits the lot with its defaults already in place, whether it
subclasses `EmbeddedHost`, `UnixHost`, or `RemoteHost`/`BaseHost` directly
(a direct `BaseHost` subclass conforms but cannot be registered):
there is nothing to copy, and no field you must re-declare to make it exist.
The field-by-field reference is on {class}`~otto.host.host.BaseHost` and
{class}`~otto.host.remote_host.RemoteHost` in the API pages.

Re-declare a base field only to change its *value policy*: a different default,
`init=False`, or "required here".  Do it keyword-only — either under a
`kw_only=True` class as above, or as `field(kw_only=True, ...)` on that one
line — so the override cannot shift the positional signature.  An override
carries **no docstring**: the docstring lives with the field's one home.

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
  {func}`~otto.testing.assert_host_conforms` checks each by its shape:
  `switch_user` must be an `async def`; `as_user` and `app_shell` may not be
  a coroutine function or an undecorated async generator, the two shapes
  `async with` can never enter (an `@asynccontextmanager` method is the usual
  form); all three must accept the protocol's keywords; and `current_user`
  must be a property.
  `BaseHost.as_user`'s refusal is an async context manager that raises
  `NotImplementedError` on `__aenter__`, so a family that cannot switch
  identity refuses at `async with host.as_user(...)`, not at the call.

### Proving it

```python
from otto.host import Element
from otto.testing import assert_host_conforms, assert_host_registrable


def test_my_rtos_host_conforms():
    assert_host_conforms(MyRtosHost, instance=MyRtosHost(ip="192.0.2.1", element=Element("dev")))


def test_my_rtos_host_registers():
    assert_host_registrable(MyRtosHost)
```

{func}`~otto.testing.assert_host_conforms` checks the call shapes production
depends on — read off the `Host` protocol itself, so a parameter added there is
asked of your class rather than silently skipped — and, given an `instance`,
probes each verb against what your `capabilities` promise for it: a verb
declared `refused` must raise `NotImplementedError`, and one declared anything
else must not.  The probes run inside a dry-run context, so nothing connects and
no bytes move; what each value means is in {doc}`../../cli/host/families`.  Call
it from a synchronous test — the probes drive their own event loop — and
close the `instance` afterwards (`asyncio.run(instance.close())`): the helper
leaves that to the caller.

Your `capabilities` are read per **class**, while behaviour can depend on the
**instance**: there is no dimension in the declaration for a host's own
configuration.  A class whose answer varies that way conforms on one instance
and reports a violation on another, both truthfully.  Probe the configuration
your declaration speaks for, and put the conditions in your row's `note` so a
reader of {doc}`../../cli/host/families` sees them too.

The second test asks a different question.  **Conformance** is whether a class
keeps the `Host` contract, and any {class}`~otto.host.host.BaseHost` can:
`LocalHost` and `DockerContainerHost` conform, yet neither can be registered.
**Registrability** is whether `register_host_class` would accept the class: a
`RemoteHost` subclass declaring `capabilities`, with a spec it can resolve.
{func}`~otto.testing.assert_host_registrable` runs `register_host_class`'s own
checks and registers nothing; pass it the `spec=` you pass to
`register_host_class`.  It cannot tell whether that spec declares your added
fields — only a lab entry that sets one can.

## Composition

Layer a defaults bundle over a custom class to create per-build profiles
without writing a new subclass:

```python
from otto.host import register_os_profile

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

- {doc}`../../configuration/lab-config` — `lab.json` schema and repo-level host defaults
- {doc}`../../cli/host/embedded` — embedded host classes, command frames, and filesystems
- {doc}`extending-embedded` — writing a custom command frame or filesystem
- {doc}`../../configuration/settings` — `init` modules and `settings.toml` field reference
