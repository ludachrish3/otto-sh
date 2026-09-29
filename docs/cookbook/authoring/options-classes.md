# Options classes

An **options class** declares a set of CLI flags once. Registering it for a
**verb**, one of the otto subcommands that take registered flags (`otto run`
and `otto test`), adds its flags to that verb: to `otto test`, to every
`otto run` command, or to both. Tests and instructions then read the values
as one typed object.

## Anatomy of an options class

An options class has fields annotated with `Annotated[T, typer.Option(...)]`.
Each field becomes a CLI flag named after it, underscores turned into dashes;
the `typer.Option(...)` carries the help text and, when you want one, a
different flag spelling. A `bool` field becomes an on/off pair of flags.

The examples on this page use one repo, `acme`, whose shared options live in
`pylib/acme_options.py` (the layout is in
[Writing tests](writing-tests.md#an-example-repo)):

```python
# pylib/acme_options.py
from typing import Annotated

import typer
from pydantic import Field

from otto import options


@options
class RepoOptions:
    device_type: Annotated[
        str, typer.Option(help="Type of device under test (e.g. 'router', 'switch').")
    ] = "router"
    lab_env: Annotated[str, typer.Option(help="Lab environment to target.")] = "staging"
    retries: Annotated[
        int,
        typer.Option(help="Connection retries (must be >= 0)."),
    ] = Field(default=3, ge=0)


@options
class DeviceTestOptions:
    firmware: Annotated[str, typer.Option(help="Firmware version to validate.")] = "latest"
    check_interfaces: Annotated[
        bool, typer.Option(help="Also check every interface's link state.")
    ] = True
```

Once registered ([below](#registering-a-class-for-a-verb)), `RepoOptions`
gives `--device-type`, `--lab-env` and `--retries`, and `DeviceTestOptions`
gives `--firmware` and the pair `--check-interfaces/--no-check-interfaces`.
Declaring a class adds no flag anywhere; registering it for a verb does, and
so does naming it as an instruction's own `options=` class
({doc}`writing-instructions`).

`@options` (`from otto import options`) is otto's name for **pydantic's**
dataclass decorator, `pydantic.dataclasses.dataclass`, and passes pydantic's
own keyword arguments through. It is not the standard library's
`@dataclass`: the fields of an `@options` class are validated when the class
is constructed. Use it for every options class.

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

The classes on this page ship in otto as `otto.examples.options`
(`src/otto/examples/options.py`), ready to copy:

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

Register an options class from an **init module**: a module named in the
`init` list of `.otto/settings.toml` ({doc}`../../configuration/settings`),
which otto imports at startup for every command. Name every verb whose flags
the class joins:

```python
# pylib/acme_instructions/__init__.py, listed in `init`
from otto import register_options

register_options("acme_options:RepoOptions", verbs=["run", "test"])
register_options("acme_options:DeviceTestOptions", verbs=["test"])
```

`RepoOptions`' flags are now on `otto test` and on every `otto run` command.
`DeviceTestOptions`' flags are on `otto test` only.

The first argument is the class itself or a `"package.module:Attr"` string.
The string form doesn't import the class's module until a verb it is
registered for runs, so `otto host` and every other command stay fast. Pass
the string the class's own module defines it under, not a re-export: otto
refuses a string that resolves to a class defined somewhere else.

The same registration can sit on the class itself:

```python
@options(verbs=["test"])
class DeviceTestOptions: ...
```

`@options(verbs=[...])` is exactly `register_options(Cls, verbs=[...])` right
after the class. It registers when its module is imported, so that module
must be an init module or one an init module imports, and it is imported at
startup for every command. When that cost matters, use the string form of
`register_options` instead. Plain `@options`, without `verbs=`, registers
nothing: that is how you declare an instruction's own options class or a
base class that others inherit.

The rules, all checked when the registration runs:

- **Verbs** come from `run` and `test`, the verbs that take registered
  options. An unknown verb, an empty list or a verb named twice raises
  {class}`~otto.params.OptionsRegistrationError`.
- **One registration per class.** A class that serves both verbs names both
  in one call; registering it a second time raises.
- **Init modules only.** A test file or `conftest.py` that registers a class,
  by either form, fails with {class}`~otto.registry.RegistrationRefused`.
  Test files load only inside `otto test`'s pytest session, so a registration
  there would exist for some commands and not others. The same holds for
  anything else a test file might register (an `@instruction()`, a backend, a
  CLI command). A test file may still *import* a class that an init module
  registers.

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

- **In a test**, call `ctx.options(Cls)` on the `ctx` fixture. It returns this
  run's instance of the registered class:

  ```python
  from acme_options import DeviceTestOptions


  class TestDevice:
      async def test_version(self, ctx) -> None:
          assert ctx.options(DeviceTestOptions).firmware
  ```

  A repo that wants a shorter name writes a one-line fixture in its
  `conftest.py`:

  ```python
  import pytest

  from acme_options import DeviceTestOptions


  @pytest.fixture(scope="session")
  def opts(ctx) -> DeviceTestOptions:
      return ctx.options(DeviceTestOptions)
  ```

- **In an instruction**, declare a parameter annotated with a class registered
  for `run`, and otto passes the instance in. See
  [Using registered options in an instruction](writing-instructions.md#using-registered-options-in-an-instruction).

`ctx.options(Cls)` raises {class}`~otto.params.OptionsNotAvailableError`,
with a message that says which case applies, when:

- `Cls` is not registered at all: `DeviceTestOptions is not registered; call
  register_options(DeviceTestOptions, verbs=[...]) from an init module`;
- `Cls` is registered for another verb only: `RunOnly is registered for run,
  not test`;
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
from otto import options
from otto.cli.run import instruction

from acme_options import RepoOptions  # registered for ["run", "test"]


@options
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
