# Options classes

Say your product supports a debug level. You want `--debug-level 3` on the
command line to reach the function that installs the product on a host.
This page shows how, in two steps.

## A project-wide option

Declare the option once, as a field of an **options class**:

```{literalinclude} ../examples/options/acme_options.py
:language: python
:start-after: "# doc: begin project-wide"
:end-before: "# doc: end project-wide"
```

The field becomes the flag `--debug-level`: underscores turn into dashes.
`verbs=["run", "test"]` adds it to every `otto run` command and to
`otto test`.

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
import the class like any Python class, for example
`from my_lab_instructions import ProjectOptions`:

```{literalinclude} ../examples/options/acme_options.py
:language: python
:start-after: "# doc: begin product"
:end-before: "# doc: end product"
:dedent: 4
```

`otto run install --debug-level 3` runs `agent-install --log-level 3`. So
does a test marked `@pytest.mark.ensure("installed")` (the test below): the
marker installs the product before the test runs, with the value from the
same command line:

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
`debug_level` is an `int`.

% Stage 2 of #1331 adds step 3 here: an option for one instruction (verbs=["run install"]).

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

- {doc}`options-advanced`: how a class is registered, where a value comes
  from, setting values from a script or a unit test, and the errors.
- {doc}`writing-tests` and {doc}`writing-instructions`: the test and
  instruction guides.
