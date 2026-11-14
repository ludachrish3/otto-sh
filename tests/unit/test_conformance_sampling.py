"""How the host-contract conformance suite draws its cells, and why that draw reproduces.

Not to be confused with :mod:`otto.testing.conformance`, which asserts that
pluggable *backend interfaces* conform. This covers the *host contract*
conformance suite's sampler under ``tests/conformance/``.

Everything here builds FAKE cells. :func:`~tests.conformance._sample.draw` is a
pure function of ``(space, budget, seed)``, and its tests must stay that way: a
test that resolved the real space would stand up a loopback ``sshd``, would
need the BusyBox cache, and would have stopped being a test of the sampler.
"""

import ast
import logging
import os
import random
import subprocess
import sys
from collections import Counter
from contextlib import AbstractAsyncContextManager
from pathlib import Path

import pytest

from otto.host.host import BaseHost
from tests._fixtures.paths import CHECKOUT_ROOT
from tests._fixtures.profiles import Cell
from tests.conformance._resolved import ResolvedCell
from tests.conformance._sample import cell_label, draw, log_draw, root_seed
from tests.conformance._vocabulary import POSIX


def _never_opened() -> "AbstractAsyncContextManager[BaseHost]":
    """A cell opener that fails if the sampler ever calls it.

    Choosing between cells must never STAND ONE UP: an opener that ran here
    would start an ``sshd`` per candidate rather than per drawn cell, which is
    the difference between a sampler and a full run wearing a budget.
    """
    raise AssertionError("draw() opened a cell; sampling must only CHOOSE between them")


def _fake(i: int) -> ResolvedCell:
    """A cell with a distinguishable identity and nothing behind it.

    ``remote_scratch=None`` for the same reason ``open_host`` refuses to run:
    sampling CHOOSES between cells and never stands one up, so a fabrication
    that answered a landing directory would be claiming a property nothing
    here reads. ``vocabulary`` is the one required field that cannot be
    fabricated away -- there is no "no dialect" -- so it takes the POSIX one
    and nothing here reads it either.
    """
    return ResolvedCell(
        cell=Cell(f"host{i}", "ssh", "scp"),
        kind="fake",
        open_host=_never_opened,
        remote_scratch=None,
        vocabulary=POSIX,
    )


def test_the_same_seed_draws_the_same_cells() -> None:
    """The reproduce handle is ``--randomly-seed=N`` from the pytest header.

    If the draw is not a pure function of that seed, the handle is a lie.
    """
    space = [_fake(i) for i in range(20)]
    assert draw(space, 5, seed=7) == draw(space, 5, seed=7)


def test_different_seeds_draw_different_cells() -> None:
    """Otherwise the sampler is a constant wearing a seed parameter."""
    space = [_fake(i) for i in range(20)]
    assert draw(space, 5, seed=7) != draw(space, 5, seed=8)


def test_a_budget_larger_than_the_space_draws_the_whole_space() -> None:
    space = [_fake(i) for i in range(3)]
    assert len(draw(space, 10, seed=1)) == 3


def test_a_none_budget_draws_everything() -> None:
    space = [_fake(i) for i in range(9)]
    assert len(draw(space, None, seed=1)) == 9


def test_the_draw_never_repeats_a_cell() -> None:
    """Sampling WITH replacement would report 8 cells having tested 3."""
    space = [_fake(i) for i in range(20)]
    drawn = draw(space, 8, seed=3)
    assert len(drawn) == len({d.cell for d in drawn}) == 8


def test_every_drawn_cell_came_from_the_space() -> None:
    """A sampler that invented a cell would parametrize over something unbuildable."""
    space = [_fake(i) for i in range(20)]
    assert all(cell in space for cell in draw(space, 6, seed=5))


def test_the_draw_leaves_the_space_it_was_given_alone() -> None:
    """The conftest holds ONE space and draws from it; an in-place shuffle there
    would change what the next call sees, and two calls with one seed would
    disagree."""
    space = [_fake(i) for i in range(20)]
    before = list(space)
    draw(space, 5, seed=7)
    assert space == before


def test_the_draw_does_not_disturb_the_module_level_random_stream() -> None:
    """``random.sample(space, budget)`` passes every assertion above and breaks
    this one.

    Module-level ``random`` is process-global. A sampler that consumed from it
    would advance the stream every other test-infra consumer shares, and --
    worse for the sampler's own contract -- would itself be steered by whoever
    seeded that stream last, so ``--randomly-seed=N`` would stop reproducing
    the draw. Compares the generator STATE rather than the next few draws:
    reading the state cannot itself advance the stream, so this observes the
    property without perturbing it. Seeding the global stream is safe HERE
    because pytest-randomly reseeds it before every test.
    """
    space = [_fake(i) for i in range(20)]
    random.seed(1234)
    before = random.getstate()

    draw(space, 5, seed=7)

    assert random.getstate() == before


def test_the_run_reports_both_the_draw_and_the_space_size(caplog) -> None:
    """``drawn=8`` alone cannot distinguish a healthy space from a collapsed one,
    which is the entire reason the spec asks for both numbers.

    No ``caplog.set_level`` on purpose: the ini pins ``log_level = INFO`` with
    ``log_cli = true``, so this asserts the line at the level a real run emits
    and displays it. Raising the level here would prove the message's text and
    nothing about whether anyone ever sees it.
    """
    space = [_fake(i) for i in range(20)]
    drawn = draw(space, 3, seed=11)

    log_draw(venue="hermetic", space=space, drawn=drawn, seed=11)

    text = caplog.text
    assert "space=20" in text, f"the space size is missing from the run's own log:\n{text}"
    assert "drawn=3" in text, f"the draw size is missing from the run's own log:\n{text}"
    assert "seed=11" in text, f"the reproduce handle is missing from the run's own log:\n{text}"
    assert "venue=hermetic" in text, f"the venue is missing from the run's own log:\n{text}"


def test_the_run_names_the_cells_it_drew_exactly_as_the_test_ids_do(caplog) -> None:
    """The log is how a failing id is traced back to a cell, so the two spellings
    must be one spelling."""
    space = [_fake(i) for i in range(20)]
    drawn = draw(space, 3, seed=11)

    log_draw(venue="hermetic", space=space, drawn=drawn, seed=11)

    for resolved in drawn:
        assert cell_label(resolved) in caplog.text
    assert cell_label(_fake(99)) not in caplog.text


def test_the_draw_is_uniform_across_seeds() -> None:
    """Min-hash sampling is only fair if the digest spreads evenly over labels.

    ``random.sample`` would give this for free; a bespoke ordering has to show
    it. A digest that clumped -- keyed on a field most labels share, say, or
    truncated to too few bits -- would still satisfy every determinism
    assertion above while quietly measuring the same two cells forever and
    reporting a healthy ``drawn=N``.

    Draws 1 of 8 across a FIXED range of 400 seeds, so this cannot flake: the
    seeds are not random, and a red here is a real change in the ordering
    function rather than an unlucky run. MEASURED at this space and range:
    every cell drawn, counts 41..63 against an expected 50, busiest cell
    1.26x expected. The bounds below are deliberately far looser than that
    (2x either side) -- a fairness smoke test, not a chi-squared test.
    """
    space = [_fake(i) for i in range(8)]
    seeds = range(400)
    expected = len(seeds) / len(space)

    picked = Counter(draw(space, 1, seed=seed)[0].cell for seed in seeds)

    assert len(picked) == len(space), (
        f"only {len(picked)} of {len(space)} cells were ever drawn across "
        f"{len(seeds)} seeds; the digest order does not reach the whole space"
    )
    assert max(picked.values()) <= 2 * expected, (
        f"one cell took {max(picked.values())} of {len(seeds)} draws against an "
        f"expected {expected:.0f}: {sorted(picked.values())}"
    )
    assert min(picked.values()) >= expected / 2, (
        f"one cell took only {min(picked.values())} of {len(seeds)} draws against "
        f"an expected {expected:.0f}: {sorted(picked.values())}"
    )


def test_a_draw_with_no_root_seed_is_the_whole_space() -> None:
    """``-p no:randomly`` unregisters ``--randomly-seed``, so there is no handle
    to reproduce a sample WITH -- and a sample nobody can reproduce is worse
    than no sample. The budget can only ever REDUCE what runs, so falling back
    to everything is the direction that cannot hide a cell.
    """
    space = [_fake(i) for i in range(20)]
    assert draw(space, 5, seed=None) == space


def test_a_seedless_run_says_so_rather_than_leaving_it_to_be_inferred(caplog) -> None:
    """A run whose budget silently did not apply must not read like a run that
    sampled; the only other evidence is a test count nobody is comparing."""
    space = [_fake(i) for i in range(20)]

    log_draw(venue="hermetic", space=space, drawn=space, seed=None)

    assert "space=20" in caplog.text
    assert "drawn=20" in caplog.text
    assert any(r.levelno >= logging.WARNING for r in caplog.records), (
        f"a seedless run is a run with no reproduce handle; it must not pass "
        f"unremarked:\n{caplog.text}"
    )


def test_the_root_seed_is_the_one_pytest_randomly_published(pytestconfig) -> None:
    """Read off the live config, not a double: the sampler's whole contract is
    that its seed is the number in THIS run's header, and a stub would keep
    agreeing after pytest-randomly renamed the option.
    """
    seed = root_seed(pytestconfig)
    assert isinstance(seed, int), (
        "pytest-randomly published no integer seed for this run, so the "
        "conformance sampler has no root seed to derive from"
    )
    assert seed == pytestconfig.getoption("randomly_seed")


def test_a_config_without_pytest_randomly_reports_no_root_seed() -> None:
    """``-p no:randomly`` UNREGISTERS the option, and ``getoption`` raises on an
    undeclared name -- which would crash collection of a tree that merely sits
    in ``testpaths``.

    The double copies the SEAM'S CALL SHAPE, verified against
    ``_pytest/config/__init__.py``: for an undeclared option ``getoption``
    raises ``ValueError`` unless the caller passed a ``default``, and it is the
    passing of that default -- not the value ``None`` -- that decides. A double
    that simply returned ``None`` regardless would keep passing after
    ``root_seed`` dropped its ``default=`` argument, i.e. it would inherit the
    safe condition instead of injecting the hostile one. MEASURED: with a
    ``default=None``-signature double, deleting ``default=None`` from
    ``root_seed`` left this test GREEN.
    """
    notset = object()

    class _NoRandomlyConfig:
        def getoption(self, name: str, default: object = notset, skip: bool = False) -> object:
            if default is notset:
                raise ValueError(f"no option named {name!r}")
            return default

    assert root_seed(_NoRandomlyConfig()) is None


def _requests_resolved_cell(module: "Path") -> bool:
    """Whether any test in *module* actually takes the ``resolved_cell`` fixture.

    Parsed, not grepped, and the difference is not academic: the first
    version of this scanned the file TEXT for the fixture name and matched
    the bed witness, whose docstring says "Takes no ``resolved_cell``". A
    prose mention of a fixture is the opposite of a request for it, so the
    text scan classified the one module it existed to exclude as the thing it
    was excluding it from.
    """
    tree = ast.parse(module.read_text())
    return any(
        "resolved_cell" in {arg.arg for arg in (node.args.args + node.args.kwonlyargs)}
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    )


def test_a_real_run_collects_the_cells_its_log_says_it_drew() -> None:
    """The wiring, end to end, in the only shape that can prove it: a subprocess.

    Three things nothing above can see, because they live between the sampler
    and pytest rather than inside either:

    - ``OTTO_CONFORMANCE_CELLS`` actually REACHES the conftest. It is an
      ``OTTO_``-prefixed variable, and ``tests/conftest.py`` strips every one
      of those that ``tests/_ambient_env.py`` does not declare -- issue #192,
      whose entire symptom was a knob that changed nothing while the run
      stayed green. Only a child process with the variable set can show it
      landing.
    - the budget narrows what is COLLECTED. A sampler whose draw never
      reached ``pytest_generate_tests`` would still log a tidy ``drawn=2``.
    - the log names the cells that actually ran. ``ResolvedCell`` is frozen
      to stop the log naming one cell while another is measured; this is the
      assertion that would notice if it did.
    """
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "tests/conformance",
            "--collect-only",
            "-q",
            "-n0",
            "--no-cov",
            "-p",
            "no:cacheprovider",
            "--randomly-seed=99",
        ],
        cwd=str(CHECKOUT_ROOT),
        # The child must be driven by this argv and this budget alone: an
        # ambient PYTEST_ADDOPTS would re-add `-n auto` (whose workers do not
        # surface the live log this parses) and an ambient
        # OTTO_CONFORMANCE_CELLS would silently retarget the assertion.
        env={**os.environ, "PYTEST_ADDOPTS": "", "OTTO_CONFORMANCE_CELLS": "2"},
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    output = result.stdout + result.stderr
    assert result.returncode == 0, f"collection failed (rc={result.returncode}):\n{output}"

    # One draw per distinct space: the session-start draw (``venue=<v> space=``)
    # and one per declared-entries tuple (``venue=<v> entries=<e> space=``),
    # each reported by a header line followed by its ``drew`` line. Each draw
    # is made and logged once (``_drawn_cells`` / ``_drawn_for`` memoise on
    # the config stash), so a header repeated is a draw computed twice.
    lines = output.splitlines()
    headers = [i for i, line in enumerate(lines) if "conformance: venue=" in line]
    draws: "dict[str, list[str]]" = {}
    for i in headers:
        header = lines[i].split("conformance: venue=", 1)[1].split(" space=", 1)[0]
        assert header not in draws, f"the draw {header!r} was reported twice:\n{output}"
        assert "conformance: drew " in lines[i + 1], f"{header!r} has no drew line:\n{output}"
        draws[header] = lines[i + 1].split("conformance: drew ", 1)[1].split(", ")
    drew = [line for line in lines if "conformance: drew " in line]
    assert len(drew) == len(draws), f"a drew line without its header:\n{output}"
    session_draws = [h for h in draws if " entries=" not in h]
    assert len(session_draws) == 1, f"the session-start draw is not reported once:\n{output}"
    logged = {label for cells in draws.values() for label in cells}

    # Only the items parametrized over CELLS, and the set is derived rather
    # than listed: `tests/conformance/` also holds the bed openers' witness,
    # which is a `conformance_bed` test of the openers themselves and takes no
    # `resolved_cell`, so it is not part of any draw. Counting its rows here
    # made this assertion report `['scp', 'sftp']` as cells the run collected
    # and the log had not named -- measured, the day that file landed. Reading
    # the fixture name out of the modules keeps the rule stated as what it
    # means (an item drawn from the space) instead of as a filename this test
    # would have to be told about again next time.
    cell_modules = sorted(
        str(module.relative_to(CHECKOUT_ROOT))
        for module in (CHECKOUT_ROOT / "tests" / "conformance").glob("test_*.py")
        if _requests_resolved_cell(module)
    )
    assert cell_modules, "no conformance module takes `resolved_cell` (guard misparse?)"
    node_ids = [
        line
        for line in output.splitlines()
        if any(line.startswith(f"{module}::") for module in cell_modules)
    ]
    assert node_ids, f"the run collected no cell-parametrized item at all:\n{output}"
    per_contract: "dict[str, set[str]]" = {}
    for line in node_ids:
        per_contract.setdefault(line[: line.index("[")], set()).add(
            line[line.index("[") + 1 : line.rindex("]")]
        )
    collected = {label for labels in per_contract.values() for label in labels}

    for header, cells in draws.items():
        assert len(cells) == 2, f"the budget of 2 did not reach the draw {header!r}:\n{output}"
    assert collected == logged, (
        f"the run collected {sorted(collected)} but its log named {sorted(logged)}"
    )
    # Every contract is parametrized over exactly one of the logged draws --
    # the session draw, or its entries' projected one -- whole: a contract
    # collected over part of a draw, or over cells from two, is a draw the log
    # does not describe.
    drawn_sets = [set(cells) for cells in draws.values()]
    uneven = {c: sorted(labels) for c, labels in per_contract.items() if labels not in drawn_sets}
    assert not uneven, f"contracts collected over no single logged draw: {uneven}"


@pytest.mark.parametrize("budget", [1, 4, 8])
def test_the_draw_is_the_budget_whenever_the_space_can_cover_it(budget: int) -> None:
    """The run's selected-test count is ``budget x contracts``; a draw that
    quietly returned fewer would under-report as a pass."""
    space = [_fake(i) for i in range(9)]
    assert len(draw(space, budget, seed=2)) == budget
