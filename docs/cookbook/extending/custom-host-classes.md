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

To ship a host subclass from an external project:

1. Subclass `EmbeddedHost` or `UnixHost` (whichever family fits).
2. Call `register_host_class(name, cls)` from an init module.  `os_type:
   <name>` then resolves to the class's own profile with no extra config. It
   registers no `os_type` profile: the class's profile is the lowest layer
   ({doc}`../../configuration/os-profiles`), so a code profile or a project's
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
{class}`~otto.host.HostSpec` subclass that validates the entry and
refuses any key it does not declare.  `register_host_class` takes it as
`spec=`.  Left out, it is the spec registered for the nearest registered
ancestor — {class}`~otto.host.UnixHostSpec` under `UnixHost`,
{class}`~otto.host.EmbeddedHostSpec` under `EmbeddedHost` and
`ZephyrHost` — so a class that only re-declares inherited fields, like
`MyRtosHost` above, needs none.

A class that **adds** a field needs its own spec: subclass the inherited one
and declare the field there too.  The class and its spec must cover each
other: `register_host_class` refuses a class field the spec lacks, and a spec
field the class lacks, naming each — at registration (with `spec=` left out,
the inherited spec is checked then too), or at the first lookup for a class
given by `Ref`.  It also refuses a constructor parameter with no default that
nothing supplies: make it a required spec field, or give it a default.  How
each spec field reaches the constructor is its field kind, declared beside it;
{mod}`otto.host.construction` lists the kinds.

```python
from dataclasses import dataclass
from typing import Annotated

from pydantic import PositiveInt

from otto.host import Convert, UnixHost, UnixHostSpec, register_host_class


@dataclass(slots=True, kw_only=True)
class GadgetHost(UnixHost):
    widget: str = "sprocket"
    buffer_bytes: int = 1024


class GadgetSpec(UnixHostSpec):
    widget: str = "sprocket"
    buffer_bytes: Annotated[PositiveInt, Convert(lambda kib, build: kib * 1024)] = 1


register_host_class("gadget", GadgetHost, spec=GadgetSpec)
```

The spec has no `to_host`.  The stock builder,
{meth}`~otto.host.HostSpec.build_kwargs`, walks its fields by kind: `widget`
reaches `GadgetHost` as the entry wrote it, and `buffer_bytes`, written in
KiB, reaches it in bytes.  A field the entry leaves out is not passed, so the
class's own default applies.

- A plain field needs a class field and a spec field of the same name.
- A converted field adds `Convert(fn)`: `fn(value, build)` returns the
  constructor argument, or {data}`~otto.host.Omit` to pass none.  The
  field's type constrains its input — here `PositiveInt` refuses `0` before
  the conversion runs — so the conversion never checks it, and `otto init`'s
  doctor reports a bad value like any other.
- A check that needs the entry's context overrides `check_in` (below).
- Override `to_host` for construction that is not keyword arguments, or to
  act on the built host before it leaves the factory; build through
  `build_kwargs` either way.

Here a bench variant marks each host it builds with the address it was built
for:

```python
class BenchGadgetSpec(GadgetSpec):
    def to_host(self, *, ready):
        kw = self.build_kwargs(ready)
        host = ready.cls(**kw)
        host.metadata["bench"] = kw["ip"]
        return host
```

`ready` is the readiness record {meth}`~otto.host.HostSpec.validate_in`
issued for this spec, and it names the class to build.  `build_kwargs`
consumes it, so an override builds once per record and reads what it needs
from the arguments `build_kwargs` returned, never from `self` afterwards.
Under `create_host_from_dict` the factory scopes the build, so an override
keeps the data profile's console prompts; a host built by hand through
`host_context` gets them only from the stock `to_host`.

A constructor field that no lab entry supplies — your code fills it in after
construction — stays off the spec and says why on the class, with
{class}`~otto.host.construction.RuntimeOnly` (from `otto.host`). Here a
subclass of `GadgetHost` above adds one:

```python
from dataclasses import dataclass, field

from otto.host import RuntimeOnly


@dataclass(slots=True, kw_only=True)
class LoggedGadgetHost(GadgetHost):
    readings: list = field(
        default_factory=list, metadata={"otto": RuntimeOnly(reason="filled by our tests")}
    )
```

A class that sets an inherited field itself, as `field(init=False)` filled in
`__post_init__`, takes no constructor argument for it, so its spec re-declares
that field {class}`~otto.host.construction.Consume`: an entry may still carry
it, and it is validated but never passed on:

```python
from typing import Annotated

from pydantic import Field

from otto.host import Consume, UnixHostSpec
from otto.models import CredSpec


class SelfCredHostSpec(UnixHostSpec):
    creds: Annotated[list[CredSpec], Consume("the class sets its own creds")] = Field(
        default_factory=list
    )
```

A class with no registered ancestor — a direct
{class}`~otto.host.remote_host.RemoteHost` subclass — has no spec to inherit,
so `register_host_class` refuses it unless you pass `spec=`, whatever its
fields.

### Checking an entry in its context: `check_in`

A field validator sees the entry alone. A check that needs the entry's context
— its element, its resolved profile, its host id, its preferences — goes in
`check_in(self, ctx)`, where `ctx` is a {class}`~otto.host.HostContext`.
Here a subclass of `GadgetSpec` above adds one:

```python
class GuardedGadgetSpec(GadgetSpec):
    def check_in(self, ctx):
        allowed = ctx.element.metadata.get("widgets")
        if allowed is not None and self.widget not in allowed:
            raise ValueError(f"widget {self.widget!r} is not one of {allowed}")
```

{meth}`~otto.host.HostSpec.validate_in` calls `check_in` once, after the
spec's structural checks and otto's own contextual checks pass. Raise any
exception to refuse the entry: `validate_in` raises it as a
{class}`~otto.host.HostValidationError` naming the host
(`host 'lab1': check_in failed: ValueError: …`), and a
`HostValidationError` you raise yourself passes through unchanged. `otto
init`'s doctor, host listing and lab loading report that message, naming the
host: the doctor as a problem, host listing as a recorded drop, and lab
loading inside its own error.

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

To build a host in a test without the factory, validate the entry in its
context with {func}`~otto.host.host_context` and build from the record
that issues; `create_host_from_dict` is the same route plus the product
providers:

```python
from otto.host import Element, host_context


def test_a_gadget_converts_its_buffer():
    hc = host_context(
        {"ip": "192.0.2.1", "os_type": "gadget", "buffer_bytes": 2},
        element=Element("dev"),
    )
    host = hc.spec.to_host(ready=hc.spec.validate_in(hc.context))
    assert host.buffer_bytes == 2048
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
`register_host_class`.  Those checks include the coverage rule above, so a
field your class adds and your spec forgets fails here, not at load.

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

- {doc}`../../configuration/lab-config` — `lab.json` schema and project-level host defaults
- {doc}`../../cli/host/embedded` — embedded host classes, command frames, and filesystems
- {doc}`extending-embedded` — writing a custom command frame or filesystem
- {doc}`../../configuration/settings` — `init` modules and `settings.toml` field reference
