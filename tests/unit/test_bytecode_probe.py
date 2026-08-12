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

# Different, FIXED hash seeds for the process that writes the .pyc and the one
# that probes it. Real runs differ by chance; pinning two seeds makes the warm
# case deterministic instead of ~5-in-6, so a regression to a non-canonical
# oracle fails every time rather than most times. The premise — that these two
# seeds really do order the set differently — is asserted below, because a
# CPython change could quietly collapse them and take the control with it.
_WRITER_SEED = "1"
_PROBER_SEED = "4"

_SET_LITERAL = '{"integration", "embedded", "hops"}'


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


def _run(tmp_path: Path, args: "list[str]", seed: str) -> "subprocess.CompletedProcess[str]":
    """Spawn a child with the bytecode-cache environment under THIS test's control.

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
    return subprocess.run(
        [sys.executable, *args],
        cwd=tmp_path,
        env={**env, "PYTHONHASHSEED": seed},
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def _run_probe(tmp_path: Path) -> "subprocess.CompletedProcess[str]":
    """Run the probe against the synthetic module, cwd'd at its directory."""
    return _run(tmp_path, [str(_PROBE), "probe_subject"], _PROBER_SEED)


def _import_in_subprocess(tmp_path: Path) -> None:
    """Import the module in a fresh interpreter, so its `.pyc` gets written."""
    result = _run(tmp_path, ["-c", "import probe_subject"], _WRITER_SEED)
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


def test_the_two_seeds_really_do_order_the_set_differently(tmp_path):
    """Guard the premise of the warm control: the seeds must actually disagree.

    If a CPython change made these two seeds produce the same frozenset order,
    the warm positive control would still pass — while no longer testing
    anything. That is a guard that cannot fail, so the disagreement is checked
    rather than assumed.
    """
    orders = [
        _run(tmp_path, ["-c", f"print(list({_SET_LITERAL}))"], seed).stdout.strip()
        for seed in (_WRITER_SEED, _PROBER_SEED)
    ]
    assert orders[0] != orders[1], (
        f"PYTHONHASHSEED {_WRITER_SEED} and {_PROBER_SEED} now order the set "
        f"identically ({orders[0]}), so the warm-cache control no longer "
        f"exercises cross-process set ordering — pick two seeds that differ"
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
        _PROBER_SEED,
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
