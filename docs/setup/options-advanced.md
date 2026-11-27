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
`verbs=[...]` names every command whose flags the class joins
([Naming commands](#naming-commands)):

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
- **Entries** name commands, such as `"run"`, `"test"` or `"run install"`
  ([Naming commands](#naming-commands)). An empty list, an entry that is not
  one or two command names, an entry named twice, or one covered by another
  raises {class}`~otto.params.OptionsRegistrationError` when the class
  registers. Whether an entry names a command that exists is checked once
  every project has loaded ([Errors](#errors)).
- **One registration per class.** A class that serves several commands names
  them all in one registration. Registering it a second time, by either form, raises
  {class}`~otto.registry.DuplicateRegistration`; a deliberate change of its
  verbs re-registers it with `overwrite=True`
  (`register_options(Cls, verbs=["run", "test"], overwrite=True)`, or
  `@otto.options(verbs=[...], overwrite=True)`), which replaces the list.

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
until a command it covers runs, so every other command stays fast.
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
`otto test` run. So register for a whole verb only the flags that every
command of it should take.

A flag that belongs to one command goes on a class registered for that
command alone, such as `verbs=["run install"]`
([An option for one instruction](options-classes.md#an-option-for-one-instruction)).

### Which flags reach an install body under `otto test`

A test marked `@pytest.mark.ensure("installed")` runs each project's `install`
body before it ([the `ensure` marker](writing-tests.md#declaring-lab-state-the-ensure-marker)).
Under `otto test` that body's options class is built from `otto test`'s own
flags by name, as `otto run install` builds it from its flags. A flag the user
typed arrives by name; a flag left unset leaves the class its own default. A
class that does not cover `test`, such as one registered for
`"run install"` alone, reads its own default for every name nobody gave a
value. So a flag that an install body reads must be registered for `test`
as well (`verbs=["run install", "test"]`) if you want to set it from
`otto test`.
{doc}`customizing-project-instructions` works through the example.

## Naming commands

Each entry of `verbs=` names a command or a group of commands, written as
you type it after `otto`, with the dashes of the command's name:

| Entry | The class's flags are offered on |
| --- | --- |
| `"test"` | `otto test` |
| `"run"` | every `otto run` command: each of your instructions, and each project instruction (`install`, `status`, ...) |
| `"run <name>"` | that one `otto run` command, for example `"run install"` or `"run get-logs"` |
| `"<command>"` | another top-level command that accepts registered options (below): that command, or every command under it, for example `"cov"` or a plugin's `"flash"` |
| `"<command> <name>"` | that one command beneath it, for example `"cov get"` or `"flash erase"` |

An entry is one or two words, and it covers every command whose words start
with it: `"run"` covers `otto run install`, and a two-word entry that names a
sub-group covers every command beneath that sub-group. An entry covered by
another entry of the same list is refused (`["run", "run install"]`): drop
the narrower one.

**Which commands accept registered options.** The rule is where a command
is registered ([A project's options on your
command](../extend/extending-cli.md#a-projects-options-on-your-command)).
Among otto's commands it gives:

- `otto test`, every `otto run` command, `otto docker`, and `otto cov`
  except its commands that need no lab (`check-stamp`, `kmodcov export` and
  `check`, `embedded-gcov export`, `check` and `finish`);
- not `otto link`, `otto tunnel`, `otto monitor`, or the commands that need
  no lab (`otto schema`, `otto env`, ...).

A plugin's command says for itself. An entry that names a command that does
not accept is an error, and an entry such as `"cov"` passes over the
commands beneath it that do not accept, without an error. An entry under
`host` (`"host"`, `"host install"`) is accepted and offers nothing yet:
`otto host` commands offer no registered class's flags.

**A command of a plugin that may not be installed.** An entry that names a
command otto does not have is an error, and otto cannot tell a plugin that is
not installed from a typing mistake. A project that works with and without
the plugin registers the class only when the plugin is there:

```python
from importlib.util import find_spec

from otto import register_options

if find_spec("flash_tools") is not None:  # the plugin that adds `otto flash`
    register_options("acme_flash_options:AcmeFlash", verbs=["flash"])
```

## Which commands run which hook

There are three ways to put a flag on a command:

| The flag belongs to | Declare it as | Read it |
| --- | --- | --- |
| A standalone instruction you wrote | a plain parameter, or `@instruction(options=Cls)` | the parameter |
| A command you did not write (one of otto's seven project instructions, a plugin's command), or several commands | a class with `verbs=["run install", ...]` | `otto.get_options(Cls)` |
| A project instruction's body you override because it must do different work | `@instruction(options=Cls)` on the method, `Cls` inheriting otto's class ({doc}`customizing-project-instructions`) | the parameter |

All three put the class's flags on the command and validate them the same
way. A plain parameter is not a class, so nothing the instruction calls can
read it: a value that a product hook or a host method needs belongs in a
class.

A product's hooks run under more than one command:

| Hook | Runs under |
| --- | --- |
| `stage`, `install` | `otto run install`; `otto test`, for a test marked `@pytest.mark.ensure("installed")`; `otto host <id> install` |
| `is_installed` | `otto run install` and `otto run status`; the `ensure` marker under `otto test`; `otto host <id> install`, `is-installed` and `is-uninstalled` |
| `uninstall` | `otto run uninstall` and `otto run cleanup`; `otto run install --ensure`, to recover a partial install; `otto host <id> uninstall` |
| `get_logs`, `get_debug_logs` | `otto run get-logs`; `otto run uninstall` and `otto run cleanup`, which fetch the product's logs first; `otto host <id> get-logs` and `get-product-logs` |
| `prepare_coverage`, `reset_coverage` | `otto test --cov`, `otto cov get` and `otto cov clean` |

A hook reads a class the same way under each of them. Under a command that
does not offer the class's flags, it gets the class's defaults (and any name
someone gave a value, [The name is the key](#the-name-is-the-key)), and it
cannot tell. That is often what you want: an `install` that reads
`AcmeInstall` installs the `"field"` variant under `otto test`. When it is
not, the rule is: **offer a flag on every command where a user must be able
to set what the hook reads, and say what the hook does with the default
elsewhere.** For the `install` above, that is
`verbs=["run install", "test"]`.

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
  for that instruction's command (`"run"`, or `"run <name>"`), and otto
  passes the instance in. See
  [Using registered options in an instruction](writing-instructions.md#using-registered-options-in-an-instruction).

- **Anywhere else** (a product's `install`, a host-class method, a fixture, a
  helper), call `otto.get_options(Cls)`. It is the same read as
  `ctx.options(Cls)` on the active context, and it works with no context at
  all, where it returns the class's defaults unless a
  [block](#setting-values-in-a-script-or-a-unit-test) sets them.

The read answers for any options class: one registered for a command, an
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

## When a project is out of scope

A class's flags are offered only when the project that registered it takes
part in the command. Which projects take part is decided by the lab you
select and each project's `lab_patterns`, and by `-I` and `-E`
({ref}`project-scope-active`). A project's instruction bodies follow the
same rule: the flags of its `install` body's class are not on
`otto run install` while the project is out of scope. otto's own classes,
and a class registered outside any project, are always offered.

When a project is out of scope:

- Its flags are not offered: they are not in `--help`, and typing one is the
  parser's `No such option`, which names no project. TAB can still offer
  them, since it completes every project's flags whatever is selected.
- A field declared with `typer.Option(envvar=...)` does not read its
  environment variable, since the flag that reads it is not there. Nothing
  reports it: a read returns the class's default.
- A read with `otto.get_options(Cls)` still answers, with the class's
  defaults and any name someone gave a value, so a hook that another project
  shares does not fail.

The flags come back when the project takes part again, in one of three ways:

- select a lab its `lab_patterns` match (`otto --lab bench1 ...`), the
  usual way;
- name it with `-I` (`otto -I acme run install --debug-level 3`);
- stop excluding it with `-E`.

`-E` wins over a selected lab: `otto --lab bench1 -E acme ...` leaves `acme`
out even when its patterns match `bench1`. A name given to `-I` or `-E` that
no project carries is reported before the command's flags are built, so a
typo in `-I` is named rather than turning into `No such option`.
Likewise, when `--lab` names a lab that no lab source declares and the
command line fails with `No such option`, otto reports the unknown lab
instead.

With no lab selected and no `-E`, every project takes part.

## One flag per name on a command

A command's flags are its own (an instruction's `options=` class and its
parameters, the classes of a project instruction's bodies, `otto test`'s own
flags) followed by those of every class that covers it, in registration
order: otto's own first, then each project in dependency order. When one run
spans several projects, each project's tests can read any class registered
for `test`, whichever project registered it, as long as they can import it.
Each field is one flag, so these rules decide what happens when two fields
meet on one command:

- **Inherited from one base, it is one flag.** A field that two classes both
  inherit from the same base class appears once, and both classes read the
  same value.
- **One name declared by two unrelated classes is an error.** otto raises
  {class}`~otto.params.OptionsCollisionError` naming both classes and the
  projects that registered them. Rename one field, or move it into a base
  class both inherit. When the classes come from two projects, that base
  belongs in a project both require, or in a library package.
- **Two fields spelled alike are an error too.** Each word that selects a flag
  counts: what the field's `typer.Option(...)` declares (`"--debug"`, `"-d"`,
  both halves of `"--cov/--no-cov"`), or, when it declares none, the
  `--field-name` Typer derives from the field, plus `--no-field-name` for a
  `bool` field. So `debug: bool` and a `quiet` field declared as
  `typer.Option("--no-debug")` collide, with the same
  {class}`~otto.params.OptionsCollisionError` naming both fields; change one
  field's spelling.

The command's own flags take part too, by name and by spelling, and so do
`-h` and `--help`. For `otto test` those are its own flags (`--iterations`,
`--cov`, `-m`, ...), so a registered field named `iterations`, or one spelled
`--random`, is the same error. For an `otto run` command they are the
instruction's own `options=` class and its parameters. For a plugin's command
they are the command's parameters. The error names both owners.

The error is found when that command's flags are built: when it runs, or
when its `--help` is asked for. It stops that command before any test or
instruction body runs, and no other: the group's `--help` still lists the
command, and its other commands run. Only the classes of projects that take
part are compared, so two projects that never take part in the same command
do not collide; with no lab selected every project takes part, and they do.
Names your project shares with others are safest with a prefix
(`acme_debug_level`).

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

## Errors

**When a class registers**, the form of its entries is checked, and a bad
list raises {class}`~otto.params.OptionsRegistrationError` at the
`@otto.options(verbs=...)` or `register_options(...)` call:

```text
verbs entry 'run  install' is not one or two command names joined by one space, such as "run" or "run install"
verbs names 'run' twice
verbs entry 'run install' is covered by 'run'; drop 'run install'
```

**Once every project has loaded**, otto checks that each entry names a
command that exists and accepts registered options. A bad entry is reported
as a load error of the project that registered the class:

```text
warning: project root /path/to/acme: failed to load options: options class 'acme_instructions:Acme', registered for 'tset' by project 'acme': otto has no command 'tset'; did you mean 'test'?
```

The part after `registered for ... by project 'acme':` says what is wrong:

| Message | The entry |
| --- | --- |
| `otto has no command 'tset'; did you mean 'test'?` | names a top-level command that does not exist |
| `otto link does not accept registered options; the commands that do: ...` | names a command that does not accept them ([Naming commands](#naming-commands)) |
| `otto run has no command 'instal'; did you mean 'install'?` | names no instruction or command under `otto run` |
| `otto run cool does not accept registered options` | names an instruction whose author said it does not accept them |
| `otto flash has no command 'nope'` | names no command under another group |
| `otto flash is a single command, with no command 'nope' beneath it` | names a command beneath a top-level command that has none |
| `otto cov kmodcov does not accept registered options` | names a command, or a sub-group, under which nothing accepts them |

Such an error is judged like any other load error of the project: when the
project takes part in the command (or is named with `-I`, or is required by
a project that takes part), the command stops before its body runs with

```text
Cannot run commands while a project fails to load (see warnings above).
```

and exit status 1; otherwise the warning is all you see. The first word of
every entry, and every `"run <name>"` entry, is checked at startup. Any
other two-word entry is checked when its group is first used, by a command
beneath it or by its `--help`, so a mistake in `"flash erase"` is seen only
under `otto flash`.

**A registration made after startup**, from a module that a command imports
when it runs, is checked when it is made: a bad entry raises
{class}`~otto.params.OptionsRegistrationError` there, with the same message,
and the class is not registered.

**When a command's flags are built** (it runs, or its `--help` is asked
for):

- two fields that meet on the command with one name or one spelling raise
  {class}`~otto.params.OptionsCollisionError`
  ([One flag per name on a command](#one-flag-per-name-on-a-command));
- a class registered by string that cannot be imported stops the command,
  naming the class, its entries and its project:
  `options class 'acme_device_options:DeviceTestOptions', registered for 'test' by project 'acme', cannot be imported: ...`.

Either error stops that command only. A bad value is a usage error, exit 2
([Reading the values](#reading-the-values)).

See {doc}`writing-tests` and {doc}`writing-instructions` for the test and
instruction guides.
