"""Which venue the host-contract conformance suite resolves cells in.

Not to be confused with ``otto.testing.conformance``, which asserts that
pluggable BACKEND INTERFACES conform. This tree is about HOST CONTRACTS.

Both knobs are declared harness opt-ins and are read through
:func:`tests._ambient_env.ambient`, per ``docs/contributing.md`` ("Adding a
harness environment knob"): undeclared, ``tests/conftest.py`` strips the
variable before any reader runs, so a straight ``os.environ`` read here
would return its own default and select the wrong venue in silence. That is
issue #192. ``ambient()`` raises on an undeclared name instead; the
declarations themselves are pinned by
``tests/unit/test_conformance_venue.py``.
"""

from tests._ambient_env import ambient

HERMETIC = "hermetic"
BED = "bed"


def current_venue() -> str:
    """``bed`` when ``OTTO_CONFORMANCE_BED`` is set truthy, else ``hermetic``.

    Reads the environment at call time rather than at import: the root
    conftest's strip runs at import of ``tests/conftest.py``, and a module-level
    constant here would capture whatever survived that, which is a different
    question from what the caller set.
    """
    raw = ambient("OTTO_CONFORMANCE_BED", "")
    return BED if raw.strip().lower() in {"1", "true", "yes"} else HERMETIC


def cell_budget() -> int | None:
    """How many cells to sample. ``None`` means every resolvable cell.

    Default 8 (spec §4). ``all`` means None. Anything else that is not a
    positive integer RAISES -- a typo'd budget must not silently become the
    default, which would make a run that sampled 8 look like a run that
    sampled 200.
    """
    raw = ambient("OTTO_CONFORMANCE_CELLS", "").strip()
    if not raw:
        return 8
    if raw.lower() == "all":
        return None
    if not raw.isdigit() or int(raw) < 1:
        raise ValueError(f"OTTO_CONFORMANCE_CELLS must be a positive integer or 'all', got {raw!r}")
    return int(raw)
