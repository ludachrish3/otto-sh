# Options classes: the details

{doc}`options-classes` shows the common case: one class, one flag, read with
`otto.get_options` where a product is installed. This page holds the rest.

## Declaring and registering a class

The easiest route is one decorator in the project's **init module**, a module
named in the `init` list of `.otto/settings.toml`
({doc}`../configuration/settings`), which otto imports at startup for
every command. The examples on this page use one project, `acme`, whose init
module is `pylib/acme_instructions/__init__.py` (the layout is in
[Writing tests](writing-tests.md#an-example-project)):

```python
# pylib/acme_instructions/__init__.py, the init module
from typing import Annotated

import typer
from pydantic import Field

import otto


@otto.options(verbs=["run", "test"])
class ProjectOptions:
    device_type: Annotated[
        str, typer.Option(help="Type of device under test (e.g. 'router', 'switch').")
    ] = "router"
    lab_env: Annotated[str, typer.Option(help="Lab environment to target.")] = "staging"
    retries: Annotated[
        int,
        typer.Option(help="Connection retries (must be >= 0)."),
    ] = Field(default=3, ge=0)


@otto.options(verbs=["test"])
class DeviceTestOptions:
    firmware: Annotated[str, typer.Option(help="Firmware version to validate.")] = "latest"
    check_interfaces: Annotated[
        bool, typer.Option(help="Also check every interface's link state.")
    ] = True
```

Each field is annotated with `Annotated[T, typer.Option(...)]` and becomes a
CLI flag named after it, underscores turned into dashes; the
`typer.Option(...)` carries the help text and, when you want one, a
different flag spelling. A `bool` field becomes an on/off pair of flags.
`verbs=[...]` names every verb whose flags the class joins:

- `ProjectOptions` gives `--device-type`, `--lab-env` and `--retries` to
  `otto test` and to every `otto run` command.
- `DeviceTestOptions` gives `--firmware` and the pair
  `--check-interfaces/--no-check-interfaces` to `otto test` only.

`otto.options` (also `from otto import options`) is otto's name for
**pydantic's** dataclass decorator, `pydantic.dataclasses.dataclass`, and
passes pydantic's own keyword arguments through. It is not the standard
library's `@dataclass`: the fields of an options class are validated when
the class is constructed. Use it for every options class. Without `verbs=`,
`@otto.options` declares a class and registers nothing: that is how you
declare an instruction's own `options=` class ({doc}`writing-instructions`)
or a base class that others inherit.

`ProjectOptions` above puts a
[field constraint](options-classes.md#validating-fields) in the default,
`= Field(default=3, ge=0)`. Putting it in the annotation works the same way:
`retries: Annotated[int, typer.Option(help=...), Field(ge=0)] = 3`.

The fields on this page ship in otto as `otto.examples.options`
(`src/otto/examples/options.py`), declared without `verbs=` so that
importing them registers nothing, ready to copy:

```{doctest}
>>> from otto.examples.options import ProjectOptions
>>> ProjectOptions().retries
3
>>> from pydantic import ValidationError
>>> try:
...     ProjectOptions(retries=-1)
... except ValidationError:
...     print("rejected")
rejected
```

## Registering a class for a verb

A registration exists from the moment its module is imported, so it must run
at startup, before otto builds any command's flags:

- **Register from an init module, or a module an init module imports.** A
  test module or `conftest.py` that registers a class, by either form below,
  fails with {class}`~otto.registry.RegistrationRefused`. pytest imports test
  modules and conftests only inside `otto test`'s pytest session, after the
  flags are built, so a registration there would exist for some commands and
  not others. The same holds for anything else a test module might register
  (an `@instruction()`, a backend, a CLI command). A test module may still
  *import* a class that an init module registers, which is how a test reads
  it.
- **Verbs** come from `run` and `test`, the verbs that take registered
  options. An unknown verb, an empty list or a verb named twice raises
  {class}`~otto.params.OptionsRegistrationError`.
- **One registration per class.** A class that serves both verbs names both
  in one registration. Registering it a second time, by either form, raises
  {class}`~otto.registry.DuplicateRegistration`; a deliberate change of its
  verbs re-registers it with `overwrite=True`
  (`register_options(Cls, verbs=["run", "test"], overwrite=True)`, or
  `@otto.options(verbs=[...], overwrite=True)`), which replaces the verb list.

### The lazy form: `register_options`

With `@otto.options(verbs=[...])`, the class's module is loaded at startup
with the init module, for every command, `otto host` included. When that
module is slow to import, declare the class with plain `@otto.options` in a
module of its own,
`pylib/acme_device_options.py` say, and register it from the init module by
name:

```python
# pylib/acme_instructions/__init__.py, the init module
from otto import register_options

register_options("acme_device_options:DeviceTestOptions", verbs=["test"])
```

The string form, `"package.module:Attr"`, doesn't import the class's module
until a verb it is registered for runs, so every other command stays fast.
Pass the string the class's own module defines it under, not a re-export:
otto refuses a string that resolves to a class defined somewhere else. A
test then imports the class from that module,
`from acme_device_options import DeviceTestOptions`.
`register_options` also takes the class itself,
`register_options(DeviceTestOptions, verbs=["test"])`, which is exactly what
`@otto.options(verbs=["test"])` does after the class.

### A registration reaches every command of the verb

Registering a class for `run` puts its flags on **every** `otto run` command:
each of your instructions, and each project instruction (`install`,
`status`, `uninstall`, ...). Registering it for `test` puts them on every
`otto test` run. So register only the flags that every command of the verb
should take.

A flag that belongs to one command stays on that command's own options class.
The worked example in
{doc}`customizing-project-instructions` shows the
pattern: `--variant` goes on a small class registered for `run` and `test`,
while `install`'s own class inherits it and keeps `--ensure` to itself.

### Which flags reach an install body under `otto test`

A test marked `@pytest.mark.ensure("installed")` runs each project's `install`
body before it ([the `ensure` marker](writing-tests.md#declaring-lab-state-the-ensure-marker)).
Under `otto test` that body's options class is built from `otto test`'s own
flags by name, as `otto run install` builds it from its flags. A flag the user
typed arrives by name; a flag left unset leaves the class its own default. A
class that does not cover `test` reads its own default for every name nobody
gave a value. So a flag that an install body reads must be registered for
`test` as well as `run` if you want to set it from `otto test`.
{doc}`customizing-project-instructions` works through the example.

## Reading the values

- **In a test**, import the class from the module that defines it (the init
  module, for a class declared there) and call `ctx.options(Cls)` on the
  `ctx` fixture. It returns this run's instance of the registered class:

  ```python
  from acme_instructions import DeviceTestOptions

  from otto import OttoContext


  class TestDevice:
      async def test_version(self, ctx: OttoContext) -> None:
          assert ctx.options(DeviceTestOptions).firmware
  ```

  A project that wants a shorter name writes a one-line fixture in its
  `conftest.py`:

  ```python
  import pytest

  from acme_instructions import DeviceTestOptions


  @pytest.fixture(scope="session")
  def opts(ctx) -> DeviceTestOptions:
      return ctx.options(DeviceTestOptions)
  ```

- **In an instruction**, declare a parameter annotated with a class registered
  for `run`, and otto passes the instance in. See
  [Using registered options in an instruction](writing-instructions.md#using-registered-options-in-an-instruction).

- **Anywhere else** (a product's `install`, a host-class method, a fixture, a
  helper), call `otto.get_options(Cls)`. It is the same read as
  `ctx.options(Cls)` on the active context, and it works with no context at
  all, where it returns the class's defaults unless a
  [block](#setting-values-in-a-script-or-a-unit-test) sets them.

The read answers for any options class: one registered for a verb, an
instruction's own `options=` class, or a plain `@otto.options` class
registered for nothing. Under a command that does not offer a class's flags,
each field reads its own default unless someone gave that name a value
([The name is the key](#the-name-is-the-key)).

The read fails in these cases only:

- `Cls` is not an options class (a dataclass type):
  {class}`~otto.params.OptionsNotAvailableError`;
- the command has not read its flags yet, for example a host class's
  constructor that reads an option while the command line is still being
  parsed: {class}`~otto.params.OptionsNotAvailableError`. Read option values
  in a body or a hook, never in a constructor;
- a field has no default and no value, or the class's own validation rejects
  a value: {class}`~otto.params.OptionsValidationError`, naming the class and
  the field. For a class the command offers, otto checks this before any body
  runs, as a usage error (exit 2).

## The name is the key

An invocation has one set of option values: for each field name, the value
someone gave it. The name is the Python field name (`debug_level`), however
its flag is spelled on the command line. Every class that has a field of that
name reads that value; a name nobody gave leaves each class its own default.
Only a value someone gave is shared: a flag the user typed (or set through an
environment variable), and the fields of an instance handed to a library call
or to `otto.use_options` ([below](#setting-values-in-a-script-or-a-unit-test)).
A flag left unset is given to nobody.

This holds on every path. `otto run install --ensure` runs a `status` probe
and, to recover a partial install, an `uninstall`; their classes receive a
same-named flag only when it was typed. An `ensure` marker under `otto test`
works the same way, from `otto test`'s own flags. So give a field the same
name on two of your classes only when it means the same thing.

An instance shares every one of its fields, the ones left at their defaults
included: a dataclass cannot tell which were set. Take a hook that reads its
own class's `dev: bool = False`, while otto's `InstallToolsOptions` has a
`dev` field whose default is `True`:

- under `otto run install-tools`, and under `otto.project.install_tools()`
  with no `opts`, nothing gave `dev` a value, so the hook reads its own
  `False`;
- under `otto.project.install_tools(opts=InstallToolsOptions())`, the instance
  gives `dev=True`, so the hook reads `True`.

The same rule, in a block you can run yourself:

```python
import otto
from otto.project import InstallToolsOptions


@otto.options
class HookOptions:
    dev: bool = False


assert otto.get_options(HookOptions).dev is False  # nothing gave `dev` a value
with otto.use_options(InstallToolsOptions()):
    assert otto.get_options(HookOptions).dev is True  # the instance gives its default
```

A typed flag is shared as the raw input, and an instance's field as that
class's validated value. Each class validates its own instance when it is
built. So a class whose validator rewrites a value can hand a same-named
reader a different value on the command line than through an instance.

## Setting values in a script or a unit test

Code that is not a command sets values for a block with `otto.use_options`.
Here `Acme` is the class from {doc}`options-classes`, imported from the
project's init module:

```python
import dataclasses

import otto
from my_lab_instructions import Acme

with otto.use_options(Acme(debug_level=3)):
    assert otto.get_options(Acme).debug_level == 3
    changed = dataclasses.replace(otto.get_options(Acme), debug_level=4)
    with otto.use_options(changed):
        assert otto.get_options(Acme).debug_level == 4
    assert otto.get_options(Acme).debug_level == 3
assert otto.get_options(Acme).debug_level == 0
```

Inside the block, `otto.get_options(Acme)` returns that instance, and every
other class reads its fields by name. Wrap `await host.install()` the same
way to give a product's `install` the value. It needs no context, so a unit
test of a product hook with a fake host can use it. Blocks nest: an inner
block overrides only the names it sets, and the outer values are back when it
ends. The library calls (`run_instruction(opts=[...])`,
`run_tests(options=[...])`, `otto.project.install(opts=...)`) do the same for
the instances they are handed. For every class they are not handed, they keep
the values of the command they are called from.

A block lays whole instances: `Acme(debug_level=3)` sets every field of
`Acme`, the ones you did not name at their defaults. To change one field of
the current values and keep the rest, use `dataclasses.replace`, as above.

Values are read-only: otto does not copy what it is given, and changing a
returned instance changes nothing for other readers.

Under `otto test`, a block in a test body or around a fixture's `yield` is
seen by the test. The `ensure` marker's converge is an autouse fixture and
runs before a test's own function-scoped fixtures, so a block opened in one of
those does not steer it. A block in a class-, module- or session-scoped
fixture does.

Two limits:

- A field's secrecy is per class. A class that declares a name as plain `str`
  reads the raw value that another class declares `SecretStr`.
- Names are shared across projects without a warning. Give the names that
  are your project's own a prefix (`acme_variant`).

## One flag set per verb

A verb's flags are the union of every class registered for it, in
registration order: otto's own first, then each project in dependency order.
When one run spans several projects, each project's tests can read any class
registered for `test`, whichever project registered it, as long as they can
import it. Each field is one flag, so two rules decide what happens when two
classes declare the same field name:

- **Inherited from one base, it is one flag.** A field that two classes both
  inherit from the same base class appears once, and both classes read the
  same value.
- **Declared by two unrelated classes, it is an error.** otto raises
  {class}`~otto.params.OptionsCollisionError` naming both classes and the
  projects that registered them, before any test or instruction runs. Rename one
  field, or move it into a base class both inherit. When the classes come
  from two projects, that base belongs in a project both require, or in a library
  package.

otto's own flags take part too. For `test` those are `otto test`'s own flags
(`--iterations`, `--cov`, `-m`, ...), so a registered field named
`iterations` is the same error. For `run` they are each instruction's own
`options=` class and its inline parameters.

## Sharing fields by inheritance

Inheritance is optional. Use it when one command's own options class should
carry the same fields as a registered class. The class below is an
instruction's own `options=` class: it inherits `ProjectOptions`, which is
registered for `run`, so each shared field stays one flag and the instruction
adds `--debug/--no-debug` of its own.

```python
from typing import Annotated

import typer
import otto
from otto.instructions import instruction

from acme_instructions import ProjectOptions  # registered for ["run", "test"]


@otto.options
class _DeployOpts(ProjectOptions):  # --device-type, --lab-env, --retries: one flag each
    debug: Annotated[
        bool, typer.Option(help="Deploy debug products instead of field products.")
    ] = False


@instruction(options=_DeployOpts)
async def deploy(opts: _DeployOpts): ...
```

`otto run deploy --help` shows the project-wide flags once, plus
`--debug/--no-debug`. Without the inheritance, `_DeployOpts` could not declare
a field of the same name as `ProjectOptions`: the two would be unrelated classes
declaring one flag.

`otto.examples.options` bundles all three shapes: a `ProjectOptions` for both
verbs, a `DeviceTestOptions` for `test`, and a `DeployInstructionOptions`
that inherits `ProjectOptions`.

See {doc}`writing-tests` and {doc}`writing-instructions` for the test and
instruction guides.
