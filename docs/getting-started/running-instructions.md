# Running instructions

This page works in the project `otto init --all` scaffolded in
[Project setup](index.md#project-setup), not the bed project: it writes a
**standalone instruction**, a command of your own under `otto run` (a project
instruction such as `install` is customized instead — see
{doc}`customizing-project-instructions`).

`otto init --instructions` (or `--all`) scaffolds `pylib/<name>_instructions/`
with one `smoke` instruction, so `otto --lab example_lab run smoke` works as
soon as `OTTO_SUT_DIRS` points at the repo. Running an instruction needs a
lab (`--lab` or `OTTO_LAB`); `otto run --list-instructions` does not. This
page hand-writes a more realistic one.

An instruction is an async function that becomes a subcommand of `otto run`:
the function name is the subcommand, with underscores turned into hyphens
(`async def check_disk` becomes `otto run check-disk`). Create
`pylib/my_instructions.py` and add `"my_instructions"` to the `init` list in
`.otto/settings.toml`, so it reads
`init = ["acme_instructions", "my_instructions"]`:

```python
import logging
from typing import Annotated

import typer

from otto.cli.run import instruction
from otto.config import get_host

logger = logging.getLogger(__name__)


@instruction()
async def hello(
    message: Annotated[str, typer.Option(help="Message to echo.")] = "hello from otto",
):
    """Run a simple echo command on the local host."""
    host = get_host("local")
    result = (await host.run(f"echo {message}")).only
    logger.info(f"{host.name}: {result.value.strip()}")
```

`get_host("local")` is the built-in `local` host, so the instruction runs
without any lab edit. To run on every host in the lab instead, iterate
{func}`~otto.config.fleet.all_hosts`: it yields only the lab's configured hosts
(`include_local=True` adds `local`), and the scaffolded `example-device`
cannot be reached until its placeholders are filled in (see
[Project setup](index.md#project-setup)).

Run it:

```bash
otto --lab example_lab run hello --help   # the generated flags, no host contacted
otto --lab example_lab run hello
otto --lab example_lab run hello --message "hi there"
otto run --list-instructions              # see all available instructions
```
