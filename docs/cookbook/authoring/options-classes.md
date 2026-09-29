# Options classes

An **options class** declares a set of CLI flags once. Registering it for a
**verb**, one of the otto subcommands that take registered flags (`otto run`
and `otto test`), adds its flags to that verb: to `otto test`, to every
`otto run` command, or to both. Tests and instructions then read the values
as one typed object.

## Declaring and registering a class

The easiest route is one decorator in the repo's **init module**, a module
named in the `init` list of `.otto/settings.toml`
({doc}`../../configuration/settings`), which otto imports at startup for
every command. The examples on this page use one repo, `acme`, whose init
module is `pylib/acme_instructions/__init__.py` (the layout is in
[Writing tests](writing-tests.md#an-example-repo)):

```python
# pylib/acme_instructions/__init__.py, the init module
from typing import Annotated

import typer
from pydantic import Field

import otto


@otto.options(verbs=["run", "test"])
class RepoOptions:
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

- `RepoOptions` gives `--device-type`, `--lab-env` and `--retries` to
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

## Validating fields

Add pydantic constraints with `Field(...)`. An out-of-range value is rejected
at construction, before any test or instruction runs, and otto turns the
error into a clean CLI failure (exit code 2, naming the offending field)
instead of silently accepting it:

```bash
otto test TestDevice --retries -1
# error: Invalid value: retries: Input should be greater than or equal to 0
```

`RepoOptions` above puts the constraint in the default,
`= Field(default=3, ge=0)`. Putting it in the annotation works the same way:
`retries: Annotated[int, typer.Option(help=...), Field(ge=0)] = 3`.

A dry run (`otto -n test ...`, `otto -n run ...`) builds and validates the
options too, so it fails the same way; see {doc}`../../cli/dry-run`.

The fields on this page ship in otto as `otto.examples.options`
(`src/otto/examples/options.py`), declared without `verbs=` so that
importing them registers nothing, ready to copy:

```{doctest}
>>> from otto.examples.options import RepoOptions
>>> RepoOptions().retries
3
>>> from pydantic import ValidationError
>>> try:
...     RepoOptions(retries=-1)
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
  in one registration; registering it a second time raises.

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
{doc}`../../getting-started/customizing-project-instructions` shows the
pattern: `--variant` goes on a small class registered for `run` and `test`,
while `install`'s own class inherits it and keeps `--ensure` to itself.

### Which flags reach an install body under `otto test`

A test marked `@pytest.mark.ensure("installed")` runs each repo's `install`
body before it ([the `ensure` marker](writing-tests.md#declaring-lab-state-the-ensure-marker)).
Under `otto test` that body builds its options class from `otto test`'s own
parsed flags, matched by field name, exactly as `otto run install` builds it
from its flags. So a body sees only the flags registered for `test`; every
other field of its class takes its default. A flag that an install body reads
must be registered for `test` as well as `run` if you want to set it from
`otto test`. {doc}`../../getting-started/customizing-project-instructions`
works through the example.

## Reading the values

- **In a test**, import the class from the module that defines it (the init
  module, for a class declared there) and call `ctx.options(Cls)` on the
  `ctx` fixture. It returns this run's instance of the registered class:

  ```python
  from acme_instructions import DeviceTestOptions


  class TestDevice:
      async def test_version(self, ctx) -> None:
          assert ctx.options(DeviceTestOptions).firmware
  ```

  A repo that wants a shorter name writes a one-line fixture in its
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

`ctx.options(Cls)` raises {class}`~otto.params.OptionsNotAvailableError`,
with a message that says which case applies, when:

- `Cls` is not registered at all: `DeviceTestOptions is not registered;
  declare it with @otto.options(verbs=[...]) in an init module`;
- `Cls` is registered for another verb only: `RunOnly is registered for run,
  not test`;
- `Cls` was registered only after the verb's options were bound, for
  example by a script that registers a class after binding them: `Late was
  registered after otto test bound its options`. A test or fixture that
  tries to register one fails earlier, with `RegistrationRefused`
  ([Registering a class for a verb](#registering-a-class-for-a-verb));
- no verb's options are bound in this context, for example in a script that
  opened a context without running `otto test` or `otto run`.

## One flag set per verb

A verb's flags are the union of every class registered for it, in
registration order: otto's own first, then each repo in dependency order.
When one run spans several repos, each repo's tests can read any class
registered for `test`, whichever repo registered it, as long as they can
import it. Each field is one flag, so two rules decide what happens when two
classes declare the same field name:

- **Inherited from one base, it is one flag.** A field that two classes both
  inherit from the same base class appears once, and both classes read the
  same value.
- **Declared by two unrelated classes, it is an error.** otto raises
  {class}`~otto.params.OptionsCollisionError` naming both classes and the
  repos that registered them, before any test or instruction runs. Rename one
  field, or move it into a base class both inherit. When the classes come
  from two repos, that base belongs in a repo both require, or in a library
  package.

otto's own flags take part too. For `test` those are `otto test`'s own flags
(`--iterations`, `--cov`, `-m`, ...), so a registered field named
`iterations` is the same error. For `run` they are each instruction's own
`options=` class and its inline parameters.

## Sharing fields by inheritance

Inheritance is optional. Use it when one command's own options class should
carry the same fields as a registered class. The class below is an
instruction's own `options=` class: it inherits `RepoOptions`, which is
registered for `run`, so each shared field stays one flag and the instruction
adds `--debug/--no-debug` of its own.

```python
from typing import Annotated

import typer
import otto
from otto.instructions import instruction

from acme_instructions import RepoOptions  # registered for ["run", "test"]


@otto.options
class _DeployOpts(RepoOptions):  # --device-type, --lab-env, --retries: one flag each
    debug: Annotated[
        bool, typer.Option(help="Deploy debug products instead of field products.")
    ] = False


@instruction(options=_DeployOpts)
async def deploy(opts: _DeployOpts): ...
```

`otto run deploy --help` shows the repo-wide flags once, plus
`--debug/--no-debug`. Without the inheritance, `_DeployOpts` could not declare
a field of the same name as `RepoOptions`: the two would be unrelated classes
declaring one flag.

`otto.examples.options` bundles all three shapes: a `RepoOptions` for both
verbs, a `DeviceTestOptions` for `test`, and a `DeployInstructionOptions`
that inherits `RepoOptions`.

See {doc}`writing-tests` and {doc}`writing-instructions` for the test and
instruction guides.
