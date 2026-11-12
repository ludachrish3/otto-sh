"""No prompt is called directly inside an ``async def`` (#1003).

A prompt called on the command loop's thread blocks it in ``input()``, and
:func:`otto.lifecycle.run_command`'s Ctrl+C handling is a loop callback: the
interrupt then waits for the person to answer, and lands after the command has
done what the answer authorised (``otto init`` scaffolded everything). Inside
an async command a prompt goes through :func:`otto.lifecycle.interruptible_prompt`,
which runs it on a thread while the command awaits the answer::

    name = await interruptible_prompt(typer.prompt, "Product name", default=...)

This reads every module under ``src/otto`` and fails on a prompt CALLED
lexically inside an ``async def``. Passing ``typer.prompt`` to the helper is
not a call, so it does not count. A prompt in a plain function the async
command calls is out of the guard's reach; that is the reason the questions
of ``otto init`` live in an ``async def`` of their own.
"""

import ast

import pytest

from tests._fixtures.paths import CHECKOUT_ROOT
from tests.unit.test_raw_terminal_writers_drain_first import PROMPTS

pytestmark = pytest.mark.interpreter_agnostic

OTTO = CHECKOUT_ROOT / "src" / "otto"


def blocking_prompts(text: str) -> "list[str]":
    """``<async function>:<line> <spelling>`` for each prompt called inside an async def."""
    found: "list[str]" = []

    def visit(node: ast.AST, owner: "str | None") -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.AsyncFunctionDef):
                visit(child, child.name)
            elif isinstance(child, (ast.FunctionDef, ast.Lambda)):
                visit(child, None)  # a nested sync function is not the loop's thread
            else:
                if owner is not None and isinstance(child, ast.Call):
                    spelling = ast.unparse(child.func)
                    if spelling in PROMPTS:
                        found.append(f"{owner}:{child.lineno} {spelling}")
                visit(child, owner)

    visit(ast.parse(text), None)
    return found


@pytest.mark.parametrize(
    "text",
    [
        "import typer\nasync def f():\n    typer.prompt('x')\n",
        "async def f():\n    if True:\n        input('x')\n",
        "import typer\nclass C:\n    async def m(self):\n        typer.confirm('x')\n",
        "import typer\nasync def f():\n    with direct_terminal():\n        typer.prompt('x')\n",
    ],
)
def test_a_prompt_called_in_an_async_def_is_flagged(text: str) -> None:
    assert blocking_prompts(text), text


@pytest.mark.parametrize(
    "text",
    [
        "import typer\nasync def f():\n    await interruptible_prompt(typer.prompt, 'x')\n",
        "import typer\ndef f():\n    typer.prompt('x')\n",
        "import typer\nasync def f():\n    def g():\n        typer.prompt('x')\n",
        "import typer\nasync def f():\n    return lambda: typer.confirm('x')\n",
    ],
)
def test_a_prompt_through_the_helper_or_off_the_loop_is_not_flagged(text: str) -> None:
    assert blocking_prompts(text) == [], text


def test_no_async_command_calls_a_prompt_directly() -> None:
    found = [
        f"{path.relative_to(OTTO.parent)}::{hit}"
        for path in sorted(OTTO.rglob("*.py"))
        for hit in blocking_prompts(path.read_text(encoding="utf-8"))
    ]
    assert not found, (
        "these async functions call a prompt on the command loop's thread, where Ctrl+C "
        "cannot reach it until the person answers (#1003). Await "
        "otto.lifecycle.interruptible_prompt(<prompt>, ...) instead:\n  " + "\n  ".join(found)
    )


def test_the_prompt_sites_are_seen() -> None:
    """The guard reads the commands that prompt, so a vacuous pass cannot hide."""
    for rel in ("cli/init.py", "cli/tunnel.py"):
        text = (OTTO / rel).read_text(encoding="utf-8")
        assert "interruptible_prompt(typer." in text, rel
