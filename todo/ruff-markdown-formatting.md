# Adopt ruff's Markdown formatting (drop the `*.md` format exclude)

**Filed:** 2026-07-25 (surfaced fixing CI on the dependabot ruff 0.15.22 -> 0.16.0 bump, PR #174)

## What happened

ruff 0.16.0 added a new capability: `ruff format` now formats Python code blocks
**inside Markdown files**, not just `.py` files. That silently widened the
formatter's file discovery from 652 files to 933 (our 653 tracked `.py` plus 280
tracked `.md`), and `nox -s lint`'s `ruff format --check .` started failing.

The split is clean:

- **All 652 Python files stay byte-identical** under 0.16.0 — zero Python churn.
- **78 of our 280 Markdown files** would be rewritten, **75 of them under
  `docs/`**.

Reformatting the docs is a documentation decision, not a version bump, so PR #174
parked it behind a formatter-only exclude in `.ruff.toml`:

```toml
[format]
exclude = ["*.md"]
```

That restores the exact 0.15.22 baseline (652 files, all formatted). It is scoped
to `[format]`, so `ruff check`'s behaviour is untouched.

## Why it wasn't just accepted

The rewrites are legitimate black-style formatting, but they override deliberate
authoring choices in prose samples — compact snippets grow noticeably taller, and
hand-aligned trailing comments lose their alignment:

```
docs/guide/coverage-embedded.md
- stamps = [struct.unpack_from("<I", blob, i + 8)[0]
-           for i in range(len(blob) - 12) if blob[i:i + 4] == ver]
+ stamps = [
+     struct.unpack_from("<I", blob, i + 8)[0]
+     for i in range(len(blob) - 12)
+     if blob[i : i + 4] == ver
+ ]

docs/architecture/lifecycle.md
- h = ctx.get_host("router1")   # 2. no ceremony — the scope
- await h.run("uptime")         #    closes it at command end
+ h = ctx.get_host("router1")  # 2. no ceremony — the scope
+ await h.run("uptime")  #    closes it at command end
```

A docs code block is read, not executed — vertical compactness and comment
alignment carry real explanatory weight there. So this wants a read-through, not
a blind `ruff format`.

## The work

1. Drop `exclude = ["*.md"]` from `[format]` in `.ruff.toml`.
2. Run `ruff format .` and review all 78 files. Where the reflow hurts a
   teaching example, reshape the *source* snippet so ruff's output is also the
   readable one (e.g. pre-break the comprehension, shorten the line so the
   trailing comment survives) rather than fighting the formatter.
3. Check whether `ruff check` also has anything to say about Markdown code
   blocks at this version — the bump only proved `ruff check .` clean *with* the
   format exclude in place, and the two settings are independent.
4. Watch the `docs` CI job: 75 of the 78 files are under `docs/`, and
   `sphinx-build -W` treats warnings as errors.

## Not urgent

Nothing is broken — the exclude reproduces pre-0.16.0 behaviour exactly, and no
Python file is affected either way. This is adopting a new capability on our own
schedule, decoupled from dependabot's cadence.
