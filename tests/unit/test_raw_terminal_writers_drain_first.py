"""Every write or read that goes past the stream proxies waits for the log queue, or says why not.

On the CLI path the log listener's thread is the terminal's only writer:
``sys.stdout`` and ``sys.stderr`` put their text on the log queue (see
``otto.logger.management._QueuedStream``), so ``print``, ``rich.print``,
``typer.echo`` and tracebacks need nothing at the call site. What this test
reads is what goes around those two streams, where text otto queued earlier
could still be waiting:

* a raw descriptor write, ``os.write(1, ...)`` or ``os.write(2, ...)``;
* a subprocess that inherits stdout (``subprocess.run``/``Popen``/``call``/
  ``check_call``, ``asyncio.create_subprocess_exec``/``_shell`` given no
  ``stdout=`` or ``capture_output=``, and ``os.system``);
* a prompt: ``input``, ``getpass.getpass``, ``typer.prompt``/``confirm``,
  ``click.prompt``/``confirm``.

A raw write or a subprocess must call ``drain_log_queue()`` on an earlier line
of its own function, or sit inside ``with direct_terminal():``. A prompt must
sit inside ``with direct_terminal():``, because a drain alone is not enough
there: the prompt's question goes through ``sys.stdout`` and would be queued
behind the drain, while ``input()`` writes its own part straight to the
descriptor. Anything else is listed in ``ALLOWED`` with the number of such
calls its function holds and the reason. A drain or hand-over in an outer
function does not cover a function nested inside it.

It reads every module under ``src/otto``. A new call in a function not listed
fails, and so does one more call in a function that is listed: the count is
part of the entry. An entry whose function no longer makes such a call fails
too, so the list cannot go stale.
"""

import ast
from typing import NamedTuple

import pytest

from tests._fixtures.paths import CHECKOUT_ROOT

pytestmark = pytest.mark.interpreter_agnostic

SRC = CHECKOUT_ROOT / "src"
OTTO = SRC / "otto"

PROMPTS = {
    "input",
    "getpass",
    "getpass.getpass",
    "typer.prompt",
    "typer.confirm",
    "click.prompt",
    "click.confirm",
}
SUBPROCESSES = {
    "subprocess.run",
    "subprocess.Popen",
    "subprocess.call",
    "subprocess.check_call",
    "asyncio.create_subprocess_exec",
    "asyncio.create_subprocess_shell",
}
DRAIN = "drain_log_queue"
HAND_OVER = "direct_terminal"


class _Allowance(NamedTuple):
    """How many uncovered calls a function may hold, and why."""

    count: int
    reason: str


class _Uncovered(NamedTuple):
    """One call that goes past the proxies with nothing in its function covering it."""

    qualname: str
    line: int
    spelling: str


class _Call(NamedTuple):
    line: int
    spelling: str
    kind: str
    held: bool


ALLOWED: dict[str, _Allowance] = {
    "otto/lifecycle.py::SyncPhaseGuard._on_signal": _Allowance(
        1,
        (
            "the Ctrl-C notice is written from inside the signal handler, which may have "
            "interrupted the main thread inside Queue.put: a drain there could deadlock on "
            "the queue's lock, and would hold the interrupt back while a stuck sink is "
            "drained. It stays an async-signal-safe raw write and may precede text still queued"
        ),
    ),
}


def _spelling(call: ast.Call) -> str:
    return ast.unparse(call.func)


def _is_named(call: ast.Call, name: str) -> bool:
    spelling = _spelling(call)
    return spelling == name or spelling.endswith(f".{name}")


def _kind(call: ast.Call) -> "str | None":
    """``prompt``, ``raw`` (a descriptor write or a subprocess), or None."""
    spelling = _spelling(call)
    if spelling in PROMPTS:
        return "prompt"
    if spelling == "os.write":
        first = call.args[0] if call.args else None
        if isinstance(first, ast.Constant) and first.value in (1, 2):
            return "raw"
        return None
    if spelling == "os.system":
        return "raw"
    if spelling in SUBPROCESSES:
        # An explicit ``stdout=None`` or ``capture_output=False`` inherits
        # descriptor 1 like no keyword at all.
        keywords = {
            kw.arg
            for kw in call.keywords
            if not (
                isinstance(kw.value, ast.Constant)
                and (kw.value.value is None or kw.value.value is False)
            )
        }
        if not keywords & {"stdout", "capture_output"}:
            return "raw"
    return None


class _Writers(ast.NodeVisitor):
    """Collects each uncovered call as an ``_Uncovered``."""

    def __init__(self) -> None:
        self.scope: list[str] = []
        self.drains: list[list[int]] = [[]]
        self.held: list[int] = [0]
        self.calls: list[list[_Call]] = [[]]
        self.found: list[_Uncovered] = []

    def _function(self, node: "ast.FunctionDef | ast.AsyncFunctionDef") -> None:
        self.scope.append(node.name)
        self.drains.append([])
        self.held.append(0)
        self.calls.append([])
        self.generic_visit(node)
        self._close()
        self.held.pop()
        self.scope.pop()

    def _close(self) -> None:
        drains = self.drains.pop()
        qualname = ".".join(self.scope) or "<module>"
        for call in self.calls.pop():
            if call.held:
                continue
            if call.kind == "raw" and any(drain < call.line for drain in drains):
                continue
            self.found.append(_Uncovered(qualname, call.line, call.spelling))

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._function(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    def _with(self, node: "ast.With | ast.AsyncWith") -> None:
        hands_over = any(
            isinstance(item.context_expr, ast.Call) and _is_named(item.context_expr, HAND_OVER)
            for item in node.items
        )
        for item in node.items:
            self.visit(item)
        self.held[-1] += hands_over
        for statement in node.body:
            self.visit(statement)
        self.held[-1] -= hands_over

    def visit_With(self, node: ast.With) -> None:
        self._with(node)

    def visit_AsyncWith(self, node: ast.AsyncWith) -> None:
        self._with(node)

    def visit_Call(self, node: ast.Call) -> None:
        kind = _kind(node)
        if kind is not None:
            self.calls[-1].append(_Call(node.lineno, _spelling(node), kind, self.held[-1] > 0))
        elif _is_named(node, DRAIN):
            self.drains[-1].append(node.lineno)
        self.generic_visit(node)


def uncovered(text: str) -> "list[_Uncovered]":
    """Every call in *text* that goes past the proxies with nothing in its function covering it."""
    visitor = _Writers()
    visitor.visit(ast.parse(text))
    visitor._close()
    return sorted(visitor.found)


def _found_in_tree() -> "dict[str, list[int]]":
    found: dict[str, list[int]] = {}
    for path in sorted(OTTO.rglob("*.py")):
        rel = path.relative_to(SRC).as_posix()
        for call in uncovered(path.read_text(encoding="utf-8")):
            found.setdefault(f"{rel}::{call.qualname}", []).append(call.line)
    return found


def test_every_write_past_the_proxies_waits_for_the_queue_or_is_allowed() -> None:
    found = _found_in_tree()
    unexpected = [
        f"{key} (lines {', '.join(map(str, lines))})"
        for key, lines in sorted(found.items())
        if key not in ALLOWED or len(lines) != ALLOWED[key].count
    ]
    assert not unexpected, (
        "these functions write to the terminal past sys.stdout/sys.stderr, or read from it, "
        "without waiting for the log queue, so text otto queued just before can land after "
        "them. Call otto.logger.management.drain_log_queue() before a raw write or a "
        "subprocess, put a prompt inside `with direct_terminal():`, or list the function in "
        "ALLOWED with its count and the reason:\n  " + "\n  ".join(unexpected)
    )


def test_no_allowed_entry_is_stale() -> None:
    found = _found_in_tree()
    stale = sorted(key for key in ALLOWED if key not in found)
    assert not stale, f"these ALLOWED functions no longer make such a call; remove them: {stale}"


def test_the_covered_sites_are_seen_and_read_as_covered() -> None:
    """The calls this guard exists for are read, and read as covered."""
    for rel, function in [
        ("cli/init.py", "_ask"),
        ("cli/tunnel.py", "remove"),
        ("host/interact.py", "_pump_remote_to_stdout"),
        ("host/interact.py", "_run_bridge"),
    ]:
        tree = ast.parse((OTTO / rel).read_text(encoding="utf-8"))
        node = next(
            n
            for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == function
        )
        assert any(_kind(c) for c in ast.walk(node) if isinstance(c, ast.Call)), rel
        assert uncovered(ast.unparse(node)) == [], rel


@pytest.mark.parametrize(
    "text",
    [
        "import os\ndef f():\n    os.write(1, b'x')\n",
        "import os\ndef f():\n    os.write(2, b'x')\n",
        "def f():\n    input('x')\n",
        "import getpass\ndef f():\n    getpass.getpass()\n",
        "import typer\ndef f():\n    typer.prompt('x')\n",
        "import typer\ndef f():\n    typer.confirm('x')\n",
        "import click\ndef f():\n    click.prompt('x')\n",
        "import subprocess\ndef f():\n    subprocess.run(['x'])\n",
        "import subprocess\ndef f():\n    subprocess.Popen(['x'], stderr=None)\n",
        # An explicit stdout=None inherits descriptor 1 all the same.
        "import subprocess\ndef f():\n    subprocess.run(['x'], stdout=None)\n",
        # So does capture_output=False, which asks for no pipes.
        "import subprocess\ndef f():\n    subprocess.run(['x'], capture_output=False)\n",
        (
            "import subprocess\ndef f():\n"
            "    subprocess.run(['x'], capture_output=False, stdout=None)\n"
        ),
        "import asyncio\nasync def f():\n    await asyncio.create_subprocess_exec('x')\n",
        "import os\ndef f():\n    os.system('x')\n",
        "class C:\n    def m(self):\n        input('x')\n",
        # A drain after the write is too late.
        "import os\ndef f():\n    os.write(1, b'x')\n    drain_log_queue()\n",
        # A drain is not enough for a prompt: its question would be queued.
        "import typer\ndef f():\n    drain_log_queue()\n    typer.prompt('x')\n",
        # The hand-over ends with its block.
        "def f():\n    with direct_terminal():\n        pass\n    typer.prompt('x')\n",
        # A drain or hand-over in an enclosing function does not cover a nested one.
        "import os\ndef f():\n    drain_log_queue()\n    def g():\n        os.write(1, b'x')\n",
        "def f():\n    with direct_terminal():\n        def g():\n            input('x')\n",
    ],
)
def test_an_uncovered_call_is_flagged(text: str) -> None:
    assert uncovered(text), text


@pytest.mark.parametrize(
    "text",
    [
        "import os\ndef f():\n    drain_log_queue()\n    os.write(1, b'x')\n",
        "import os\ndef f():\n    management.drain_log_queue()\n    os.write(2, b'x')\n",
        "import typer\ndef f():\n    with direct_terminal():\n        typer.prompt('x')\n",
        (
            "import typer\ndef f():\n"
            "    with management.direct_terminal():\n        typer.confirm('x')\n"
        ),
        "import os\ndef f():\n    with direct_terminal():\n        os.write(1, b'x')\n",
        "import os\ndef f(fd):\n    os.write(fd, b'x')\n",
        "import subprocess\ndef f():\n    subprocess.run(['x'], capture_output=True)\n",
        "import subprocess\ndef f():\n    subprocess.Popen(['x'], stdout=subprocess.DEVNULL)\n",
        "import subprocess\ndef f():\n    subprocess.check_output(['x'])\n",
        "import getpass\ndef f():\n    getpass.getuser()\n",
        "def f():\n    print('x')\n    rich.print('x')\n    typer.echo('x', err=True)\n",
    ],
)
def test_a_covered_or_queued_call_is_not_flagged(text: str) -> None:
    assert uncovered(text) == [], text
