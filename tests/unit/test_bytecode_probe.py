"""Both controls for scripts/bytecode_probe.py, the instrument behind mutation sweeps.

An instrument whose output is *evidence about other code* has to be verified in
both directions before any run of it is worth reading. A positive control alone
is what a probe that always says MATCH would pass; a negative control alone is
what one that always says DIVERGED would pass. This module drives it against a
synthetic module in `tmp_path` — never a repo file — so the negative control can
poison a real `__pycache__` without touching anything under version control.

The poisoning is reproduced DETERMINISTICALLY rather than by racing the clock:
write the mutation, `os.utime` the source back to the original's mtime, import
it in a subprocess so the `.pyc` is written, then restore the bytes and re-pin
the mtime. Size and mtime then match exactly what the `.pyc` recorded, which is
the same on-disk state a same-second, length-preserving restore produces.
"""

import functools
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from tests._fixtures.paths import PROJECT_ROOT

_PROBE = PROJECT_ROOT / "scripts" / "bytecode_probe.py"

# Same length, different behaviour — the shape that defeats mtime+size
# validation. Asserted below rather than trusted, because the whole scenario is
# void if they ever drift apart.
_ORIGINAL = "VALUE = 'aaa'"
_MUTATED = "VALUE = 'bbb'"

# A comment and an assignment that swap places. Same bytes, same instructions,
# same constants — only the LINE TABLE moves. That is the one poisoning a
# digest built from `co_code` and constants alone would miss, and a stale cache
# there is not harmless: every traceback points at the wrong source line.
_COMMENT = "# a comment whose position moves"
_ASSIGN_LINE = "VALUE = 'aaa'                   "  # padded to _COMMENT's width

_SET_LITERAL = '{"integration", "embedded", "hops"}'

# How far to search for two hash seeds that disagree. Small: disagreement is
# common, so a pair turns up within the first few candidates on every version
# otto supports. The ceiling exists to fail loudly rather than spin.
_SEED_SEARCH_CEILING = 32


def _module(tmp_path: Path, header: "str | None" = None) -> Path:
    """A throwaway importable module holding a set-of-strings constant.

    The set literal is the point: it compiles to a `frozenset` constant whose
    internal order depends on per-process string hashing, which is exactly what
    made a `marshal.dumps` oracle report false DIVERGEDs. A probe that
    regressed to marshal fails the warm positive control below rather than
    passing it quietly.
    """
    source = tmp_path / "probe_subject.py"
    source.write_text(
        (header if header is not None else f"{_COMMENT}\n{_ASSIGN_LINE}\n")
        + textwrap.dedent(f"""

            def classify(name):
                return name in {_SET_LITERAL}
        """)
    )
    return source


def _child_env(seed: str) -> "dict[str, str]":
    """The bytecode-cache environment for a child, under THIS test's control.

    `PYTHONDONTWRITEBYTECODE` and `PYTHONPYCACHEPREFIX` are stripped rather than
    inherited, and that is load-bearing: every claim here is about where a
    `.pyc` gets written and read, so inheriting either one silently rewrites
    the experiment. Caught by mutation — under a mutation driver that sets both
    (the recommended way to run one), the "probe must not write" guard passed
    with the probe writing freely, and the warm-cache control degenerated into
    a second cold-cache run. Two guards, inert, green.
    """
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTHONPYCACHE")}
    env.pop("PYTHONDONTWRITEBYTECODE", None)
    return {**env, "PYTHONHASHSEED": seed}


def _run(tmp_path: Path, args: "list[str]", seed: str) -> "subprocess.CompletedProcess[str]":
    """Spawn a child in *tmp_path* with a controlled bytecode-cache environment."""
    return subprocess.run(
        [sys.executable, *args],
        cwd=tmp_path,
        env=_child_env(seed),
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def _set_order_under(seed: str) -> str:
    """The set literal's iteration order in a child running under *seed*.

    No `cwd` and no imports: this only prints, so it cannot write a `.pyc`
    anywhere and needs no `tmp_path` to be safe in.
    """
    result = subprocess.run(
        [sys.executable, "-c", f"print(list({_SET_LITERAL}))"],
        env=_child_env(seed),
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
    )
    return result.stdout.strip()


@functools.cache
def _discriminating_seeds() -> "tuple[str, str]":
    """Two `PYTHONHASHSEED` values that really do order the set differently.

    Measured on the running interpreter, never pinned. Which seeds disagree is
    a property of CPython's string hashing: 3.11 switched from siphash24 to
    siphash13, and the pair this module used to hard-code — "1" and "4" —
    disagreed on 3.10 while collapsing to one ordering on 3.11 and every
    version above it. That reddened four CI lanes and stayed green on this
    machine's 3.10, where the suite is normally run. Choosing a different fixed
    pair would only defer the same failure to the next hashing change, so the
    pair is discovered instead.

    Cached, so the warm-cache control stays deterministic within a run: both
    the writing process and the probing process get the same two seeds every
    time, rather than a fresh roll per test.
    """
    orders: "dict[str, str]" = {}
    for candidate in range(1, _SEED_SEARCH_CEILING + 1):
        seed = str(candidate)
        order = _set_order_under(seed)
        for earlier_seed, earlier_order in orders.items():
            if earlier_order != order:
                return earlier_seed, seed
        orders[seed] = order
    raise AssertionError(
        f"no two seeds in 1..{_SEED_SEARCH_CEILING} order {_SET_LITERAL} differently "
        f"under Python {sys.version.split()[0]} — every one gave "
        f"{next(iter(orders.values()), 'nothing')}. The warm-cache control needs two "
        f"processes to disagree about set order, so it cannot be made meaningful here; "
        f"widen the set literal rather than raising the ceiling"
    )


def _run_probe(tmp_path: Path) -> "subprocess.CompletedProcess[str]":
    """Run the probe against the synthetic module, cwd'd at its directory."""
    return _run(tmp_path, [str(_PROBE), "probe_subject"], _discriminating_seeds()[1])


def _import_in_subprocess(tmp_path: Path) -> None:
    """Import the module in a fresh interpreter, so its `.pyc` gets written."""
    result = _run(tmp_path, ["-c", "import probe_subject"], _discriminating_seeds()[0])
    assert result.returncode == 0, f"could not warm the cache: {result.stderr}"


def test_the_lengths_that_make_the_scenario_possible_are_still_equal():
    """Guard the premise. Unequal lengths and the negative controls prove nothing."""
    assert len(_ORIGINAL) == len(_MUTATED), (
        "the poisoning depends on a length-PRESERVING edit; with different "
        "sizes CPython invalidates the .pyc on its own and the negative "
        "control below would pass for the wrong reason"
    )
    assert len(_COMMENT) == len(_ASSIGN_LINE), (
        "the line-shift control swaps two lines and needs their combined length "
        "unchanged; pad _ASSIGN_LINE back to _COMMENT's width"
    )


def test_the_seed_search_returns_a_pair_that_really_does_disagree():
    """Guard the premise of the warm control, by checking the SEARCH's answer.

    The claim under test is `_discriminating_seeds`' output, not the abstract
    existence of disagreeing seeds — so the two orderings are re-measured here
    independently rather than taken from the search's own bookkeeping. A search
    that returned the first two candidates without comparing them, or returned
    one seed twice, passes its own internal logic and fails this.

    Two ways to red it, both worth knowing: break the comparison in the search
    and this reports identical orderings; drop `_SEED_SEARCH_CEILING` to 1 and
    the search raises before this test can assert anything, naming the
    interpreter it gave up on. The previous version of this test pinned the two
    seeds as constants and asserted they disagreed — an honest guard over a
    dishonest premise, which is exactly how it came to fail on four CI lanes at
    once while passing here.
    """
    writer_seed, prober_seed = _discriminating_seeds()

    assert writer_seed != prober_seed, (
        f"the search returned the same seed twice ({writer_seed}); the warm "
        f"control needs the writing and probing processes to differ"
    )
    orders = [_set_order_under(writer_seed), _set_order_under(prober_seed)]
    assert orders[0] != orders[1], (
        f"the search chose PYTHONHASHSEED {writer_seed} and {prober_seed}, but "
        f"they order the set identically ({orders[0]}) — so the warm-cache "
        f"control below no longer exercises cross-process set ordering"
    )


def test_the_child_environment_is_free_of_inherited_cache_settings(tmp_path, monkeypatch):
    """Guard the strip in `_run` by INJECTING the hostility, never by inheriting it.

    Two guards below were once inert — passing while testing nothing — because
    the child inherited `PYTHONDONTWRITEBYTECODE` and `PYTHONPYCACHEPREFIX`
    from a mutation driver. `_run` strips them now, and the first attempt to
    guard that strip reproduced the very defect it was sent to fix: it only
    reddened when the ambient shell already carried those variables, so
    deleting the strip stayed a no-op in a clean shell — CI's shell, and every
    developer's. A guard whose RED depends on how the runner happens to be
    configured is not a guard.

    Hence the two `setenv` lines. They put the hostile condition INSIDE the
    test, so the claim is about `_run`'s behaviour rather than about the
    environment `_run` was lucky enough to find, and removing the strip reds
    here on any machine with no special setup. Neither variable can be checked
    by asking the parent, because `os.environ` is exactly what `_run` is
    supposed to be filtering — so the child is asked what it actually got.

    The other two guards cannot cover this between them, and it is worth
    knowing why: contaminated, both go quietly GREEN. Suppressed writes make
    "the probe must not write a .pyc" trivially true, and a redirected cache
    turns the warm-cache control into a second cold-cache run. Inertness is
    invisible from inside the tests it disables.
    """
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "1")
    monkeypatch.setenv("PYTHONPYCACHEPREFIX", str(tmp_path / "elsewhere"))

    result = _run(
        tmp_path,
        ["-c", "import sys; print(sys.flags.dont_write_bytecode, sys.pycache_prefix)"],
        "1",  # any seed: this asks what the child inherited, not how it hashes
    )

    # `_run` passes check=False, so a child that died would otherwise be
    # reported below as an empty inherited-settings string — a misleading
    # message for a crash.
    assert result.returncode == 0, (
        f"the reporting child failed to run (exit {result.returncode}): {result.stderr}"
    )
    assert result.stdout.strip() == "0 None", (
        f"the child inherited bytecode-cache settings ({result.stdout.strip()!r}; "
        f"want '0 None'). Every claim in this module is about where a .pyc is "
        f"written and read, so an inherited PYTHONDONTWRITEBYTECODE or "
        f"PYTHONPYCACHEPREFIX rewrites the experiment: writes suppressed makes "
        f"'the probe must not write' trivially true, and a redirected cache "
        f"turns the warm-cache control into a second cold-cache run"
    )


@pytest.mark.parametrize("warm", [False, True], ids=["cold-cache", "warm-cache"])
def test_a_clean_module_reports_match(tmp_path, warm):
    """Positive control, on both cache states.

    Warm matters independently: on a cold cache `get_code` compiles in the
    probe's own process, so both sides share a hash seed and even a
    non-canonical oracle agrees. Only the warm case — a `.pyc` written by a
    DIFFERENT process — can expose the set-ordering instability, which is why
    the false positives took a round to surface.
    """
    _module(tmp_path)
    if warm:
        _import_in_subprocess(tmp_path)

    result = _run_probe(tmp_path)

    assert result.returncode == 0, f"clean module reported dirty: {result.stdout}{result.stderr}"
    assert "MATCH probe_subject" in result.stdout


def test_the_probe_does_not_write_the_cache_it_inspects(tmp_path):
    """A detector that repopulates the cache can manufacture its own quarry.

    `get_code` writes the `.pyc` on a miss, stamped with the source's current
    mtime and size — half the poisoning condition, created by the observer.

    The two assertions before the glob are what make this able to fail at all.
    "Wrote no .pyc" is satisfied by a probe that did NOTHING, so without them a
    probe that exits on its first line passes here — measured, with
    `raise SystemExit` at the top of `scripts/bytecode_probe.py`: 5 failed
    elsewhere and this test green. The other probe-driving tests all read
    stdout and so caught it; this one had nothing to catch it with.
    """
    _module(tmp_path)

    result = _run_probe(tmp_path)

    assert result.returncode == 0, f"the probe did not run clean: {result.stdout}{result.stderr}"
    assert "MATCH probe_subject" in result.stdout, (
        "the probe must have actually inspected the module before its silence "
        "about the cache means anything"
    )
    assert not list(tmp_path.glob("__pycache__/*.pyc")), (
        "the probe wrote a .pyc; it must set sys.dont_write_bytecode before "
        "its first import so inspecting a cache cannot also populate it"
    )


def test_a_poisoned_cache_reports_diverged(tmp_path):
    """Negative control: the failure the probe exists for, staged on purpose."""
    source = _module(tmp_path)
    original = source.read_text()
    stamp = source.stat()

    source.write_text(original.replace(_ORIGINAL, _MUTATED))
    os.utime(source, (stamp.st_atime, stamp.st_mtime))
    _import_in_subprocess(tmp_path)  # caches the MUTATED bytecode
    source.write_text(original)
    os.utime(source, (stamp.st_atime, stamp.st_mtime))

    assert source.read_text() == original, "the source on disk is the original again"
    result = _run_probe(tmp_path)

    assert result.returncode != 0, (
        f"the probe missed a poisoned cache — the source reads clean while the "
        f"cached bytecode says {_MUTATED!r}: {result.stdout}{result.stderr}"
    )
    assert "DIVERGED probe_subject" in result.stdout


def test_a_cache_that_differs_only_in_line_numbers_reports_diverged(tmp_path):
    """The line-table half of the digest, which `co_code` and constants cannot see.

    Swapping a comment and an assignment leaves the instruction stream and
    every constant identical and moves only the source positions. Without the
    line table in the digest this poisoning is invisible, and the cost is not
    theoretical: the cached module raises tracebacks pointing at the wrong
    lines of a file that reads correctly.
    """
    source = _module(tmp_path, header=f"{_COMMENT}\n{_ASSIGN_LINE}\n")
    original = source.read_text()
    stamp = source.stat()
    shifted = original.replace(f"{_COMMENT}\n{_ASSIGN_LINE}\n", f"{_ASSIGN_LINE}\n{_COMMENT}\n")
    assert shifted != original, "the swap must actually change the file"
    assert len(shifted) == len(original), "and must not change its size, or mtime+size sees it"

    source.write_text(shifted)
    os.utime(source, (stamp.st_atime, stamp.st_mtime))
    _import_in_subprocess(tmp_path)
    source.write_text(original)
    os.utime(source, (stamp.st_atime, stamp.st_mtime))

    result = _run_probe(tmp_path)

    assert result.returncode != 0, (
        f"a cache compiled from a line-shifted source was accepted; the digest "
        f"must include the line table: {result.stdout}{result.stderr}"
    )
    assert "DIVERGED probe_subject" in result.stdout


def test_an_unimportable_name_is_reported_not_ignored(tmp_path):
    """A typo'd module name must not read as a clean bill of health."""
    result = _run_probe(tmp_path)  # no module written

    assert result.returncode != 0, "a missing module must not exit 0"
    assert "MISSING probe_subject" in result.stdout
