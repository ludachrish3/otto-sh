"""The POSITIVE CONTROLS: per surface, the test that proves its observable can go red.

Spec 2026-08-22 §5 refuses a ``measured-ok`` cell that does not name one. Its
reason is the whole of this module's job: run an assertion across profiles
without asking whether its observable can even MOVE on those profiles, and
"the matrix publishes ``measured-ok`` for cells that proved nothing -- the
guards-that-cannot-fail defect promoted to a *published* artifact, which is
worse than the silent version because the matrix is what people read instead
of the tests."

This is not a hypothetical failure in this tree. Three consecutive tasks of
this workstream each caught a guard that stayed green under mutation -- a
noxfile scanner blind to one spelling, a guard that de-duplicated a frozen
dataclass, an emitter reading the wrong phase. Every one of them looked
meaningful.

**A CONTROL ASSERTS THE INSTRUMENT, NOT THE PRODUCT**, and that is the line
that decides what belongs here. A contract says *the host answers correctly*;
its control says *this cell's answer would have been REJECTED had it been
wrong*. So a control creates, on the real cell, a reply the contract's
assertion must refuse -- a planted sentinel, a corrupted byte, a second
permission mode -- and asserts the refusal. ``tests/e2e/host/
test_shell_history_e2e.py::test_opting_in_still_records`` is the exemplar the
spec names and the shape every control here follows: pollute deliberately,
assert the instrument DETECTED it, restore, verify the restoration.

**EVERY CONTROL RUNS ON THE CELL IT VOUCHES FOR.** A control that passed on
``gnu`` says nothing about ``busybox-1.16.1``: the two run different shells
behind different transports, and an observable that moves on one can be inert
on the other. So a control lives in its contract's own module and is
parametrized by ``tests/conformance/conftest.py`` over the very same drawn
cells -- inheriting that module's ``applicable_cell`` domain and its
``expected_failure`` declarations, because a control is only as applicable as
the contract it is about.

**A CONTROL IS NOT A CONTRACT, AND THE ARTIFACT MUST NOT CONFUSE THE TWO.**
Both take ``resolved_cell``, so signature alone cannot tell them apart. The
:data:`CONTROL_MARK` marker is what does, in three places that would otherwise
each get it wrong:

* ``tests/_fixtures/support_matrix.py`` would file a control as a seventh
  matrix ROW (it discovers contracts by their ``resolved_cell`` parameter);
* ``tests/conformance/_observation.py`` would emit an observation record for
  it, with ``surface: null``, which the collator must refuse -- a control's
  result is evidence about the instrument and never about the host;
* a reader of the rendered page would have no way to tell which nodeids are
  the evidence and which are the thing being evidenced.

THE MARKER CARRIES THE SURFACE IT VOUCHES FOR, rather than a hand-written
table mapping one to the other. A table is a second copy of a fact the tree
already holds, and the copy that goes stale is the one nothing runs -- the
failure mode ``tests/conformance/_vocabulary.py`` records for its own table:
*a missing entry looks like a passing cell*. Here the declaration sits on the
control itself, and :func:`discover_controls` reads it back.

Not to be confused with :mod:`otto.testing.conformance`, which asserts that
pluggable BACKEND INTERFACES conform. This tree is about HOST CONTRACTS.
"""

import ast
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from otto.host.host import BaseHost
from otto.result import CommandResult
from tests._fixtures.paths import PROJECT_ROOT
from tests._fixtures.profiles import Cell
from tests.conformance._vocabulary import Vocabulary

CONTROL_MARK = "positive_control"
"""The marker a positive control carries, with the surface id it vouches for.

Spelled ``@pytest.mark.positive_control("exec-exit-code")``. Registered in
``pyproject.toml`` like every other marker in this repo -- ``filterwarnings =
["error"]`` turns an unregistered one into a collection error, so the
registration is load-bearing rather than documentation.
"""

CONFORMANCE_ROOT = PROJECT_ROOT / "tests" / "conformance"


@dataclass(frozen=True)
class Control:
    """One positive control, and the matrix row it vouches for."""

    surface: str
    """The ``id`` of the surface (matrix row) whose observable this control moves."""

    nodeid: str
    """The control's pytest nodeid, WITHOUT its ``[cell]`` parametrization.

    Unparametrized because the mapping *surface -> control* is a property of
    the tree, while *which cell* a given verdict was controlled on is a
    property of a RUN. Collation pairs the two: the cell it writes names this
    contract's control parametrized on one of that profile's own cells.
    """


def mark_string_argument(node: ast.expr, mark_name: str) -> "str | None":
    """The single string literal in a ``@pytest.mark.<mark_name>("...")`` decorator.

    Answers ``None`` when *node* is not that marker at all, and the EMPTY
    STRING when it is that marker but its argument cannot be read from source.
    The two are different answers and every caller distinguishes them: "not a
    control" and "a control whose surface this cannot see" must not collapse,
    because the second is a loud failure and the first is the ordinary case.

    Matches the ATTRIBUTE PATH and not merely the final name, so an unrelated
    ``@something.positive_control(...)`` is not read as this marker. A marker
    whose argument is computed puts its value somewhere an AST read cannot
    follow, and the honest answer to that is to fail the tree's cross-check
    rather than to guess.

    Shared with ``tests/conformance/_observable.py``, which reads a second
    marker of exactly this shape off the CONTRACTS. One reader rather than
    two, so the two markers cannot end up disagreeing about what
    ``@pytest.mark.X("y")`` means.
    """
    if not isinstance(node, ast.Call):
        return None
    func = node.func
    if not (isinstance(func, ast.Attribute) and func.attr == mark_name):
        return None
    mark = func.value
    if not (isinstance(mark, ast.Attribute) and mark.attr == "mark"):
        return None
    if not (isinstance(mark.value, ast.Name) and mark.value.id == "pytest"):
        return None
    if len(node.args) != 1 or not isinstance(node.args[0], ast.Constant):
        return ""
    value = node.args[0].value
    return value if isinstance(value, str) else ""


def _mark_surface(node: ast.expr) -> "str | None":
    """The surface id in a ``@pytest.mark.positive_control("...")`` decorator, or None."""
    return mark_string_argument(node, CONTROL_MARK)


def control_surface_of(node: "ast.FunctionDef | ast.AsyncFunctionDef") -> "str | None":
    """The surface *node* declares itself a positive control for, or None.

    ``None`` means "not a control at all", which is what
    ``tests/_fixtures/support_matrix.py`` reads to keep a control out of the
    matrix's surface rows. The empty string means "a control whose marker
    this cannot read", which is a loud failure there rather than a silent
    demotion to contract.
    """
    for decorator in node.decorator_list:
        surface = _mark_surface(decorator)
        if surface is not None:
            return surface
    return None


def walk_test_functions(
    path: Path,
) -> "Iterator[tuple[str, ast.FunctionDef | ast.AsyncFunctionDef]]":
    """Every ``test*`` function *path* defines, as ``(nodeid, node)``, in source order.

    Walks classes too, so a future ``class TestX`` module is not silently
    dropped -- and a dropped module would REMOVE matrix rows rather than fail,
    which is the direction that goes unnoticed.

    Shared with ``tests/_fixtures/support_matrix.py``'s contract discovery
    rather than copied into it: the two walks answer opposite halves of one
    question ("is this a contract or a control?"), and two walkers that
    disagreed about what a test function is would put a test in both sets or
    neither.
    """
    rel = path.relative_to(PROJECT_ROOT).as_posix()

    def visit(
        body: "list[ast.stmt]", prefix: str
    ) -> "Iterator[tuple[str, ast.FunctionDef | ast.AsyncFunctionDef]]":
        for node in body:
            if isinstance(node, ast.ClassDef):
                yield from visit(node.body, f"{prefix}{node.name}::")
            elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name.startswith(
                "test"
            ):
                yield f"{rel}::{prefix}{node.name}", node

    yield from visit(ast.parse(path.read_text(encoding="utf-8"), filename=str(path)).body, "")


def discover_controls() -> "list[Control]":
    """Every positive control the tree declares now, in file then source order.

    Read from the tree rather than tabulated, for the reason the module
    docstring gives. ``tests/unit/test_support_matrix.py`` asserts the set
    covers every surface exactly once, so a control deleted, renamed or
    pointed at a surface that no longer exists fails loudly instead of leaving
    a ``measured-ok`` cell citing a nodeid nothing collects.
    """
    return [
        Control(surface=surface, nodeid=nodeid)
        for path in sorted(CONFORMANCE_ROOT.glob("test_*.py"))
        for nodeid, node in walk_test_functions(path)
        if (surface := control_surface_of(node)) is not None
    ]


def positive_control_for(surface: str) -> str:
    """The nodeid of the control that proves *surface*'s observable can go red.

    The interface collation consumes: a cell it writes ``measured-ok`` cites
    this, parametrized on one of that profile's own cells.

    RAISES on a surface with no control, and never answers a default. A
    default would be a ``measured-ok`` cell citing a plausible-looking nodeid
    that vouches for nothing, which is precisely the artifact §5 exists to
    refuse.
    """
    matches = [control for control in discover_controls() if control.surface == surface]
    if len(matches) != 1:
        raise KeyError(
            f"surface {surface!r} has {len(matches)} positive controls, not 1: "
            f"{[control.nodeid for control in matches]} -- a `measured-ok` cell for it "
            f"could not name the test that proves its observable can go red"
        )
    return matches[0].nodeid


def marks_a_positive_control(item: "pytest.Item") -> bool:
    """Whether *item* is a positive control rather than a contract.

    The RUNTIME half of :func:`control_surface_of`, which reads the same
    marker off the source. Both are needed and neither substitutes for the
    other: the AST read answers for a tree nobody has collected (the matrix's
    surface rows are derived without running anything), and this one answers
    inside a hook, where the source is not in hand.

    Read through ``get_closest_marker`` rather than by scanning
    ``own_markers``, so a control declared on a CLASS is seen the same way as
    one on a function.
    """
    return item.get_closest_marker(CONTROL_MARK) is not None


def control_surface_of_item(item: "pytest.Item") -> "str | None":
    """The surface *item* vouches for, read off its marker at RUN time.

    The runtime twin of :func:`control_surface_of`, and the reason it exists is
    the CONTROL RECORD (``tests/conformance/_observation.py``): a control's
    outcome never becomes a cell's verdict, but the collator must be able to
    ask *did the instrument for this surface actually prove itself on this very
    cell?* before writing ``measured-ok``. A record that could not name its
    surface would be unanswerable, so this reads the marker rather than
    guessing from the module the control happens to live in.

    ``None`` for an item that is not a control, and for a control whose marker
    carries no readable surface -- which
    ``tests/unit/test_support_matrix.py`` refuses in the tree, so the second
    case cannot reach a committed run.
    """
    marker = item.get_closest_marker(CONTROL_MARK)
    if marker is None or len(marker.args) != 1 or not isinstance(marker.args[0], str):
        return None
    return marker.args[0] or None


async def remove_landed(host: BaseHost, words: Vocabulary, path: Path) -> "CommandResult":
    """Delete *path* on *host*, best-effort, and answer what the host said.

    THE HALF THAT MUST NEVER RAISE. It is called from a ``finally``, and an
    exception raised there REPLACES the one already on its way out -- so a
    control whose real failure was "the transfer never happened" would report
    "could not remove the file", blaming the cleanup for the defect. The
    verification is :func:`assert_bed_left_clean`, which the caller runs only
    on the path where nothing else went wrong.

    MEASURED, and this split exists because of it: on the five ``bed-busybox``
    ``nc`` cells otto's registered ``nc-transfer`` gap makes the put FAIL --
    and it still leaves a ZERO-BYTE file behind on the guest. So "the put
    failed" and "there is nothing to clean up" are different statements, and a
    control that skipped its cleanup whenever the put failed left litter on
    exactly the cells that fail every run.
    """
    return (await host.run(words.remove_file_template.format(path=path))).only


def assert_bed_left_clean(removed: "CommandResult", path: Path, cell: Cell) -> None:
    """Require :func:`remove_landed`'s attempt to have SUCCEEDED.

    Run on the success path only. The exemplar
    (``tests/e2e/host/test_shell_history_e2e.py::test_opting_in_still_records``)
    restores byte-for-byte and then VERIFIES the restoration; this is that
    verification, for a control whose "restore" is a delete.

    THE REMOVAL IS ITS OWN VERIFICATION, which is why the spelling is ``rm``
    and not ``rm -f``. MEASURED on the bed 2026-08-24: ``rm -f`` answers 0
    whether or not the file was there, so asserting on it could never fail --
    the guards-that-cannot-fail defect, in the cleanup. Plain ``rm`` and
    Zephyr's ``fs rm`` both answer non-zero for a file that is not there
    (``fs rm`` gives -8, ``Failed to remove <path> (-2)``) and 0 for one that
    is. So a success says two things at once: there WAS a file, and there is
    not one now.

    Its own message rather than pytest's introspected diff: this module is not
    one pytest rewrites assertions in, the same rule
    ``tests/conformance/_framing.py`` carries.
    """
    assert removed.is_ok, (
        f"{cell}: the control could not remove {path} it had put there -- "
        f"`{removed.command}` answered {removed.retcode} {removed.value!r}. "
        f"A control that leaves the bed dirtier than it found it is a control "
        f"that breaks the next run"
    )
