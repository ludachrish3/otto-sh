# Customizing project instructions

The seven commands the previous pages gave you for free — `install`,
`uninstall`, `cleanup`, `get-logs`, `install-tools`, `uninstall-tools` and
`status` — are **project instructions**: each is one name with one fixed walk
across the lab's projects, and **one body per project**. otto declares all
seven and writes the
default body for each; a project replaces its own body, and adds flags of its own
to the command.

That is the difference from a **standalone instruction** (the `@instruction`
on a plain async function that {doc}`writing-instructions` opens
with): a standalone instruction has one body, belongs to one project, and is its
own command. A project instruction is shared, and every project that has a body
for it runs in dependency order under the one command.

## One override point

A project customizes a project instruction by declaring a method on a
`ProjectActions` subclass and registering that subclass — never by defining a
standalone instruction named `install`. otto refuses that one at startup,
naming the project, the instruction and the guide page that spells the override
out ({doc}`../cli/run/defaults`); the message and the two
ways to migrate an existing `install` instruction are in
[The collision error](writing-instructions.md#the-collision-error).

`otto run install`, a script calling `await otto.project.install()`, and a
test marked `@pytest.mark.ensure("installed")` all run the same bodies.

## A flag of your own

The worked example's project ({doc}`../developing/bed-case-study/index`) installs an agent, and wants to choose which build
variant goes on. It declares an options class that **inherits the first-party
one** for that instruction, then overrides the body:

```{literalinclude} ../examples/getting-started/libs/gs_example/actions.py
:language: python
:start-after: "# doc: begin actions"
:end-before: "# doc: end actions"
```

Three things that code relies on:

- **The options class must inherit `InstallOptions`** — the first-party class
  for this instruction, exported from `otto.project` along with one for each
  of the other five. otto refuses an override whose class does not; the rule,
  and what each body then receives, is in [Your project's flags on a
  default](../cli/run/defaults.md#your-projects-flags-on-a-default).
- **`super()` keeps the default behavior.** The body above does its own work
  and then calls up; otto's body reads only the fields otto declared.
- **`self.ctx` is this project's view of the lab** — `self.ctx.all_hosts()` is
  already narrowed to the hosts this project declared an interest in, and already
  scoped to this project's products.

`otto run install --help` now shows the inherited flags and the new one side
by side:

```{literalinclude} ../examples/captures/run-install-help.txt
:language: text
```

## Sharing a flag across projects

`otto run install` shows the **union** of every configured project's flags, so two
projects that both override `install` both add to one command. Two projects wanting
the *same* flag must inherit it from one shared base class — otherwise otto
refuses the pair at startup — and that base belongs in a project they **require**
or in a library package, never in an optional one. The rule and the error are in
[One command, every project's flags](../cli/run/defaults.md#one-command-every-projects-flags).

## A new project instruction

A project is not limited to otto's seven. Declaring a method otto has no name for
adds a project instruction of its own — and because it is the first
declaration of that name, it sets the walk shape every later declaration
inherits:

```python
from typing import Annotated

import typer

from otto import Status, options
from otto.instructions import instruction
from otto.project import ProjectActions, register_project_actions
from otto.result import Result


@options
class DeployOpts:
    build: Annotated[str, typer.Option(help="Build to deploy.")] = "latest"


@register_project_actions
class BedActions(ProjectActions):  # one registered class per project -- the same one as above
    @instruction(
        options=DeployOpts,
        walk="forward",
        continue_on_failure=False,
        require_dependencies=True,
        help="Deploy the build to every project's fleet, dependencies first.",
    )
    async def deploy(self, opts: DeployOpts) -> Result:
        """Deploy this project's build."""
        ...  # your work, opts.build in hand
        return Result(Status.Success)
```

`otto run deploy` now exists, walks every project that has a `deploy` body in
dependency order, and stops at the first failure. A second project declaring
`deploy` supplies its own body and its own options class, and **may not restate
the walk keywords** — the first declaration fixed them, so a project can never
reshape otto's own seven, which otto declared first. A project's own new instruction has no
first-party class to inherit, so `DeployOpts` above inherits nothing.

## Tests follow the flags

A test marked `@pytest.mark.ensure("installed")` converges the lab through the
same `install` bodies before it runs. Under `otto test`, a body's `--variant`
comes from `otto test`'s own flags, matched by field name
([Which flags reach an install body under `otto test`](options-classes.md#which-flags-reach-an-install-body-under-otto-test)).
So the flag has to be registered for `test` too, which is what the decorator
on `BedVariant` in the code above does: `@options(verbs=["run", "test"])`.
The module is one of the example project's init modules, so the registration
exists from startup.

`BedVariant` is the small class that declares `variant`, and `BedInstall`
inherits it, so `--variant` is one flag on `otto run install`. Registering
`BedVariant` for `run` and `test` puts `--variant`, and only `--variant`, on
every `otto run` command and on `otto test`. `BedInstall` itself stays `install`'s own
options class, unregistered, because a registration reaches every command of
the verb: registering it would put `--ensure` and `--recover-partial` on
`otto run status`, `otto run uninstall` and `otto test` as well. The rule is
in [A registration reaches every command of the
verb](options-classes.md#a-registration-reaches-every-command-of-the-verb).

A test then asks for the state, and `otto test --variant` steers the install
that reaches it:

```python
import pytest


@pytest.mark.ensure("installed")
class TestAgent:
    async def test_agent_answers(self) -> None: ...
```

```bash
otto --lab unix test TestAgent --variant debug
```

`BedInstall`'s other fields, such as `ensure`, have no `otto test` flag, so
the body gets their defaults.

The test side of the marker is in
[Declaring lab state](writing-tests.md#declaring-lab-state-the-ensure-marker);
the declaration rules on this page have their home in
{doc}`writing-instructions` under *Project instructions*,
and the composition rules in {doc}`../cli/run/defaults`.
