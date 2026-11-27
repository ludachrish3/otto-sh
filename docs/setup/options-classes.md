# Options classes

Say your product supports a debug level. You want `--debug-level 3` on the
command line to reach the function that installs the product on a host.
This page shows how, in three steps.

## A project-wide option

Declare the option once, as a field of an **options class**:

```{literalinclude} ../examples/options/acme_options.py
:language: python
:start-after: "# doc: begin project-wide"
:end-before: "# doc: end project-wide"
```

The field becomes the flag `--debug-level`: underscores turn into dashes.
`verbs=["run", "test"]` adds it to every `otto run` command
(`otto run install`, `otto run status`, your own instructions, ...) and to
`otto test`, and to no other command. A class can name other commands too
([Naming commands](options-advanced.md#naming-commands)).

Put the class in your project's **init module**, which otto imports at
startup for every command. In a project made by `otto init`, that is
`pylib/<name>_instructions/__init__.py`, where `<name>` is the project's name
with dashes turned into underscores (`my-lab` gives `my_lab_instructions`).
That file already has a class `ProjectOptions` registered with
`verbs=["run", "test"]`. Adding the `debug_level` field to it is all this
step needs. The class name is yours to choose: this page says `Acme`, and in
a generated project you would write `otto.get_options(ProjectOptions)`. In a
project you made by hand, the init module is the one named in the `init`
list of `.otto/settings.toml` ({doc}`../configuration/settings`).

Now the flag works:

```bash
otto run install --debug-level 3
```

## Using it on the host

Read the value with `otto.get_options(Acme)`. Here the product's `install`
passes that level to the installer (products are on
{doc}`defining-products-and-tools`). In the module that holds your product,
import the class from the init module like any Python class:

```python
from my_lab_instructions import Acme  # in a generated project: ProjectOptions
```

```{literalinclude} ../examples/options/acme_options.py
:language: python
:start-after: "# doc: begin product"
:end-before: "# doc: end product"
:lines: 1-4,6
:dedent: 4
```

otto runs a product's `install` hook under `otto run install`, and under
`otto test` for a test marked `@pytest.mark.ensure("installed")`; the full
list is in
[Which commands run which hook](options-advanced.md#which-commands-run-which-hook).

`otto run install --debug-level 3` runs
`agent-install --log-level 3`. So does a test marked
`@pytest.mark.ensure("installed")` (the test below): the marker installs the
product before the test runs, with the value from the same command line:

```bash
otto test TestAgent --debug-level 3
```

With no flag, `debug_level` is `0`, the field's default.

The same read works anywhere in your project. In a method of your own host
class ({doc}`../extend/custom-host-classes`):

```{literalinclude} ../examples/options/acme_options.py
:language: python
:start-after: "# doc: begin host-method"
:end-before: "# doc: end host-method"
```

In a test ({doc}`writing-tests`):

```{literalinclude} ../examples/options/acme_options.py
:language: python
:start-after: "# doc: begin test"
:end-before: "# doc: end test"
```

In an instruction ({doc}`writing-instructions`):

```{literalinclude} ../examples/options/acme_options.py
:language: python
:start-after: "# doc: begin instruction"
:end-before: "# doc: end instruction"
```

`otto.get_options` returns an `Acme`, so your editor knows that
`debug_level` is an `int`. Under a command the class does not cover, such as
`otto host`, it returns the class's defaults.

## An option for one instruction

A flag that only one command should take goes on a second class, registered
for that command alone. `verbs=["run install"]` gives `AcmeInstall`'s
`--variant` to `otto run install`, and to no other command:

```{literalinclude} ../examples/options/acme_options.py
:language: python
:start-after: "# doc: begin one-instruction"
:end-before: "# doc: end one-instruction"
```

Put it in the init module beside `Acme`, and import it where you read it:

```python
from my_lab_instructions import Acme, AcmeInstall
```

Then read it in the product's `install` the same way. The highlighted line
is the one this step adds to the hook of step 2:

```{literalinclude} ../examples/options/acme_options.py
:language: python
:start-after: "# doc: begin product"
:end-before: "# doc: end product"
:emphasize-lines: 5
:dedent: 4
```

Both flags reach the installer:

```bash
otto run install --debug-level 3 --variant debug
# runs: agent-install --log-level 3 --variant debug
```

`install` also runs under other commands, such as `otto test` with the
`ensure` marker, which have no `--variant` flag; there it reads the
class's default, `"field"`. Why a hook should read classes that cover every command
it runs under is on the advanced page, in
[Which commands run which hook](options-advanced.md#which-commands-run-which-hook).

## Validating fields

`@otto.options` makes the class a pydantic dataclass, so you can add
pydantic constraints with `Field(...)`. Here is the same `Acme` class with the
field's default `= 0` replaced, so `debug_level` must be 0 or more:

```python
from typing import Annotated

import typer
from pydantic import Field

import otto


@otto.options(verbs=["run", "test"])
class Acme:
    debug_level: Annotated[int, typer.Option(help="Product debug log level.")] = Field(
        default=0, ge=0
    )
```

otto rejects a bad value before any test or instruction runs. It exits with
code 2 and names the field:

```bash
otto run install --debug-level -1
# error: Invalid value: debug_level: Input should be greater than or equal to 0
```

A dry run (`otto -n run ...`) checks the values the same way; see
{doc}`../cli/dry-run`.

## Going further

- {doc}`options-advanced`: how a class is registered, which commands an
  entry of `verbs=` names, where a value comes from, what happens to a
  project's flags when it is out of scope, setting values from a script or a
  unit test, and the errors.
- {doc}`writing-tests` and {doc}`writing-instructions`: the test and
  instruction guides.
