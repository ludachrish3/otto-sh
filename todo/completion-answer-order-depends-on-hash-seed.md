# Completion answers order flag pairs by hash seed (`-h --help` vs `--help -h`)

Found 2026-10-15 while auditing the shim-vs-Typer differential (#566): a dump
of every completion answer over the generated corpus differs between two runs
of the SAME code unless `PYTHONHASHSEED=0` is pinned. The only difference is
the order of equivalent flags, e.g. `-h --help` in one run and `--help -h` in
the other: somewhere on the answer path a `set` (or a dict built from one) is
iterated into the candidate list.

The differential itself is unaffected: it compares the shim and Typer in one
process, under one seed, so both sides see the same order. What is affected:

- any byte-level oracle of completion output (the #566 audit had to pin the
  seed; a future golden file would flake);
- the user: `otto <TAB>` can list the same flags in a different order from
  one shell session to the next. Harmless, but not deterministic.

Fix: find the set iteration on the candidate path (likely where short and
long aliases of one option are merged) and emit the aliases in declaration
order, then pin it with a test that runs the completion twice under two
different `PYTHONHASHSEED` values in subprocesses and asserts identical
output.
