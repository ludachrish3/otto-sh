"""WHAT each contract watched, DECLARED by the contract and never derived.

Spec 2026-08-22 §5 requires a ``measured-ok`` cell to name its ``observable``.
The obvious cheap implementation -- derive it from the surface id -- is worse
than leaving the field absent, and the reason is the whole of this module's
job: **a surface's observable differs by ENVIRONMENT.** §5's own worked example
is shell-history suppression, provable on bash and not provable at all on the
five BusyBox guests. A field that cannot disagree with the surface's own name
carries no information; it satisfies the schema while proving nothing, which is
this item's signature defect one level up.

So the declaration lives on the CONTRACT, beside the
``@pytest.mark.positive_control`` marker that already separates a control from
a contract, and it is read back rather than tabulated -- the same discipline
``tests/conformance/_controls.py`` records for its own marker: a hand-written
table mapping contract to observable is a second copy of a fact the tree
already holds, and *a missing entry looks like a passing cell*.

TWO LAYERS, AND BOTH ARE REQUIRED FOR A DIFFERENT REASON.

``@pytest.mark.observable("<template>")`` -- THE DECLARATION
    A single string literal, so :func:`discover_observables` can read it out
    of the SOURCE without collecting or running anything. That static
    readability is what lets ``tests/unit/test_support_matrix.py`` assert that
    every surface the matrix declares has an observable at all, and it is what
    makes "a surface with no declared observable cannot produce a
    ``measured-ok`` cell" checkable rather than merely intended.

    It is a FORMAT TEMPLATE, rendered per cell against that cell's
    :class:`~tests.conformance._vocabulary.Vocabulary`, and that is where the
    first layer of environment-dependence comes from. MEASURED, the exec
    exit-code observable renders as::

        gnu / busybox-*   ... for `(exit 42)` (must be 42) and `(exit 0)` ...
        zephyr-*          ... for `definitely_not_a_zephyr_command` (must be -8)
                              and `kernel version` (must be 0) ...

    Those are materially different claims: on a Zephyr shell the exit-code
    evidence rests on an unknown-command errno, because that userland has
    exactly ONE stable failure code (``tests/conformance/_vocabulary.py``'s
    ``sequence_failing_code``). A reader of the rendered matrix learns which
    one they got.

:func:`note_observable` -- THE REFINEMENT
    A template cannot express a branch the CELL decides at run time, and two
    contracts here take one:

    * ``test_put_lands_the_documented_mode_on_the_host`` watches the mode read
      back by ``stat -c %a`` where the backend carries a permission model, and
      watches the pre-flight REFUSAL where it does not
      (``BaseFileTransfer.supports_mode``). Those are not two spellings of one
      observable; they are different observables, and only the running test
      knows which it took.
    * ``test_exec_frames_output_without_prompt_noise`` can assert exact
      equality only where the tester chose the output; on a stock Zephyr
      builtin the text belongs to the firmware and there is nothing to compare
      against.

    So a contract may narrow its declared observable to what it actually
    watched, from inside its body. **This does not weaken the record's
    un-fabricatability**: the OUTCOME still comes from pytest's own report at
    teardown (``tests/conformance/_observation.py``), and nothing a body writes
    here can turn a failure into a pass. What the body supplies is the
    DESCRIPTION of what it looked at, which is the one thing only it knows.

    The marker's rendered template is the floor: a contract that fails before
    it reaches its note still records an observable, so a ``measured-broken``
    cell is never left without one.

Not to be confused with :mod:`otto.testing.conformance`, which asserts that
pluggable BACKEND INTERFACES conform. This tree is about HOST CONTRACTS.
"""

import ast
from dataclasses import dataclass

import pytest

from tests._fixtures.paths import PROJECT_ROOT
from tests.conformance._controls import mark_string_argument, walk_test_functions
from tests.conformance._resolved import ResolvedCell

OBSERVABLE_MARK = "observable"
"""The marker a CONTRACT carries, holding the template of what it watches.

Spelled ``@pytest.mark.observable("...")``. Registered in ``pyproject.toml``
like every other marker in this repo -- ``filterwarnings = ["error"]`` turns an
unregistered one into a collection error, so the registration is load-bearing
rather than documentation.
"""

CONFORMANCE_ROOT = PROJECT_ROOT / "tests" / "conformance"


@dataclass(frozen=True)
class Observable:
    """One contract's declaration of what it watches."""

    contract: str
    """The contract's pytest nodeid, WITHOUT its ``[cell]`` parametrization."""

    template: str
    """The declaration, as written. ``{words.<field>}`` renders per cell."""


def observable_template_of(node: "ast.FunctionDef | ast.AsyncFunctionDef") -> "str | None":
    """The template *node* declares, ``None`` if it declares none, ``""`` if unreadable.

    Three answers and not two, for the reason
    :func:`~tests.conformance._controls.mark_string_argument` gives: "declares
    nothing" is a contract that can never produce a ``measured-ok`` cell, and
    "declares something this cannot read" is a tree the guard must fail on
    rather than silently treat as the first.
    """
    for decorator in node.decorator_list:
        template = mark_string_argument(decorator, OBSERVABLE_MARK)
        if template is not None:
            return template
    return None


def discover_observables() -> "list[Observable]":
    """Every observable the tree's contracts declare, in file then source order.

    Read from the tree rather than tabulated. ``tests/unit/test_support_matrix.py``
    asserts this covers every surface exactly once, so a contract that loses
    its declaration fails loudly instead of quietly becoming a surface no run
    can ever mark ``measured-ok``.
    """
    return [
        Observable(contract=nodeid, template=template)
        for path in sorted(CONFORMANCE_ROOT.glob("test_*.py"))
        for nodeid, node in walk_test_functions(path)
        if (template := observable_template_of(node)) is not None
    ]


def observable_template_for(contract: str) -> str:
    """The template *contract* declares.

    RAISES on a contract with no declaration, and never answers a default. A
    default would be a ``measured-ok`` cell naming an observable nobody chose,
    which is the field being a restatement of the cell's own key -- exactly
    what §5 asks the field for instead of.
    """
    matches = [entry for entry in discover_observables() if entry.contract == contract]
    if len(matches) != 1 or not matches[0].template:
        raise KeyError(
            f"contract {contract!r} declares {len(matches)} readable observables, not 1: "
            f"{[entry.template for entry in matches]} -- no run may write a `measured-ok` "
            f"cell for it, because the cell could not say WHAT was watched"
        )
    return matches[0].template


def render_observable(template: str, resolved: ResolvedCell) -> str:
    """*template* rendered against *resolved*'s vocabulary and cell.

    The rendering namespace is deliberately tiny: ``words`` (this cell's
    :class:`~tests.conformance._vocabulary.Vocabulary`) and ``cell`` (the
    :class:`~tests._fixtures.profiles.Cell` triple). A template reaching for
    anything else is a template that could open a host from a reporting hook,
    which is not what a description of an observable is for.

    Raises rather than falling back on a partial render. A template naming a
    vocabulary field that does not exist would otherwise reach the artifact as
    a literal ``{words.typo}``, and a rendered page would publish it.
    """
    try:
        return template.format(words=resolved.vocabulary, cell=resolved.cell)
    except (AttributeError, IndexError, KeyError) as exc:
        raise ValueError(
            f"the observable template {template!r} cannot be rendered against "
            f"{resolved.cell}: {exc!r}. The namespace is `words` (this cell's "
            f"Vocabulary) and `cell` only"
        ) from exc


_NOTED = pytest.StashKey[str]()
"""Per ITEM: the observable a contract's own body narrowed itself to."""


def note_observable(request: "pytest.FixtureRequest", observable: str) -> None:
    """Declare, from inside a contract, WHICH observable this cell gave it.

    For the branch a template cannot express -- see this module's docstring.
    Called from the body, so it says what the run actually watched rather than
    what the environment was predicted to offer.

    Rejects the empty string outright: "narrowed to nothing" is not a
    narrowing, and a cell whose observable rendered empty would fail the
    schema's ``minLength`` far from the edit that caused it.
    """
    if not observable.strip():
        raise ValueError(
            "a contract may narrow its declared observable but not erase it -- "
            "an empty observable is a `measured-ok` cell that cannot say what it watched"
        )
    request.node.stash[_NOTED] = observable


def observable_of(item: "pytest.Item", resolved: ResolvedCell) -> "str | None":
    """What *item* watched on *resolved*: its own note, else its rendered template.

    ``None`` only when the item declares no observable at all, which is what
    stops the collator writing ``measured-ok`` for that surface. Answered
    rather than raised, so a tree caught mid-edit reports a test's real outcome
    instead of an INTERNALERROR from a reporting hook -- the same stance
    ``tests/conformance/_observation.py``'s ``surface_for`` takes.
    """
    noted = item.stash.get(_NOTED, None)
    if noted is not None:
        return noted
    marker = item.get_closest_marker(OBSERVABLE_MARK)
    if marker is None or len(marker.args) != 1 or not isinstance(marker.args[0], str):
        return None
    template = marker.args[0]
    return render_observable(template, resolved) if template else None
