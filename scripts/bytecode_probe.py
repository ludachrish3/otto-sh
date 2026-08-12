"""Report whether a module's LOADED bytecode still matches its source on disk.

    uv run python scripts/bytecode_probe.py pkg.mod [pkg.other ...]

Prints ``MATCH``/``DIVERGED``/``MISSING`` per module and exits non-zero on any
divergence. Run it from the repo root; it puts the cwd on ``sys.path``.

Why this exists
---------------

CPython validates a cached ``.pyc`` against the source's **mtime truncated to
whole seconds and its byte size** — not a hash. A mutation-testing harness that
writes a mutated module, lets pytest import it, then restores the original in a
``finally`` will therefore leave the MUTATED bytecode looking valid whenever the
mutation preserved byte length and the restore landed in the same clock second.
Every later run keeps executing the mutation while ``git status`` reads clean.

That is not hypothetical: it happened here, and the symptom was a gate reporting
five tests SKIPPED that passed when the module ran alone. The cure is to keep
the cache out of the loop (run the harness's pytest under
``PYTHONDONTWRITEBYTECODE=1`` and a fresh ``PYTHONPYCACHEPREFIX`` per mutation),
and then to VERIFY that with this probe rather than assume it. Run it in the
default environment — no prefix — because the question is what an ordinary
later run would load.

Two traps in writing such a probe, both of which bit the first version:

1. ``get_code`` WRITES the ``.pyc`` on a cache miss. A read-only-looking
   detector then repopulates the cache it exists to inspect, stamped with the
   source's current mtime and size — one half of the very condition it hunts.
   Hence ``sys.dont_write_bytecode`` before the first import.
2. ``marshal.dumps`` is not a usable equality oracle. It does not serialise a
   ``set``/``frozenset`` constant in a canonical order, so two code objects
   compiled from byte-identical source marshal differently whenever the module
   holds a set literal of strings (whose hashes are per-process randomised).
   That produced intermittent false DIVERGEDs, and a verification instrument
   that cries wolf is worse than none — the next real report gets waved
   through. Hence the normalised digest below.

Known limit: this compares only the plain ``__pycache__/<stem>.cpython-3XX.pyc``
that ``get_code`` reads. Pytest's assertion-rewritten
``<stem>.cpython-3XX-pytest-N.N.N.pyc`` is invisible here; that cache is handled
by redirecting reads with ``PYTHONPYCACHEPREFIX``, not by this probe.
"""

import sys

sys.dont_write_bytecode = True  # before the first import — see (1) above

import importlib.util  # noqa: E402  (must follow the line above)
from pathlib import Path  # noqa: E402


def digest(code: object) -> tuple:
    """Order-canonical structural fingerprint of a code object, recursively.

    Covers what a mutation can change and what a stale cache would preserve:
    the instruction stream, the names it touches, its signature shape, its
    constants — and the line table, so a mutation that only shifts lines is
    still visible (a stale cache there yields tracebacks pointing at the wrong
    source lines, which is its own quiet defect).

    Set constants are sorted by ``repr``; everything else is already ordered by
    compilation and so deterministic across processes.
    """
    consts = []
    for const in code.co_consts:  # type: ignore[attr-defined]
        if hasattr(const, "co_code"):
            consts.append(digest(const))
        elif isinstance(const, (frozenset, set)):
            consts.append(("set", tuple(sorted(map(repr, const)))))
        else:
            consts.append(repr(const))
    return (
        code.co_name,  # type: ignore[attr-defined]
        code.co_code,  # type: ignore[attr-defined]
        code.co_names,  # type: ignore[attr-defined]
        code.co_varnames,  # type: ignore[attr-defined]
        code.co_argcount,  # type: ignore[attr-defined]
        code.co_flags,  # type: ignore[attr-defined]
        # `co_lnotab` on <=3.9, `co_linetable` from 3.10; getattr keeps the
        # probe usable across otto's whole supported range.
        getattr(code, "co_linetable", None) or getattr(code, "co_lnotab", b""),
        tuple(consts),
    )


def verdicts(names: "list[str]") -> "list[str]":
    """One ``MATCH``/``DIVERGED``/``MISSING`` line per module, in order."""
    lines = []
    for name in names:
        spec = importlib.util.find_spec(name)
        if spec is None or spec.origin is None:
            lines.append(f"MISSING {name}")
            continue
        loaded = spec.loader.get_code(name)  # type: ignore[union-attr]
        source = Path(spec.origin).read_bytes()
        fresh = compile(source, spec.origin, "exec", dont_inherit=True)
        if digest(loaded) == digest(fresh):
            lines.append(f"MATCH {name}")
        else:
            lines.append(f"DIVERGED {name} -> {spec.origin} (loaded bytecode != compiled source)")
    return lines


def main(argv: "list[str]") -> int:
    """Print one verdict per module named in *argv*; exit non-zero on any problem."""
    sys.path.insert(0, str(Path.cwd()))
    lines = verdicts(argv)
    print("\n".join(lines))
    return 1 if any(line.startswith(("DIVERGED", "MISSING")) for line in lines) else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
