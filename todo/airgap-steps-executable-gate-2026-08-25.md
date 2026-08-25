# The air-gap install steps: claims are gated, the procedure is not

Asked 2026-08-25: were the air-gap download and installation steps codified as
docs tests, or run by hand once? The honest answer is *both*, split along a
line worth naming — because the half that is gated is easy to mistake for the
whole.

## What IS gated, on every docs build

`scripts/check_docs_wheel_matrix.py` runs in `docs-lint`, and
`docs: docs-lint docs-html doctest doctest-src` (`Makefile:1192`), so it fires
on every `make docs` and therefore every `make release`. It cross-checks four
claims in `docs/installation.md` against `uv.lock` and `pyproject.toml`:

1. the recipe's `for PYVER in …` list equals the minors in `[project] classifiers`
2. the "Native-extension dependencies" table lists exactly the runtime packages
   that ship binary wheels — no more, no less
3. each row's `abi3` / `per-version` label matches what the lock's wheel
   filenames actually say
4. every such package has an installable wheel for **every** supported minor on
   the documented Linux target

Claim 4 is the one that matters inside the gap: a missing wheel there does not
surface until `pip install --no-index` runs on the isolated host, long after
the media has crossed. `tests/unit/test_docs_wheel_matrix.py` (24 tests) guards
the gate itself, and injects every hostile condition into a synthetic
lock/table rather than inheriting the repo's currently-healthy state — the
right discipline, and worth preserving in anything added here.

## What is NOT gated

**Nothing executes the procedure.** No `pip download`, no
`pip install --no-index --find-links`, no subprocess in those tests at all.
`installation.md` carries 17 shell fences and zero termynal blocks. The only
markdown linter, `scripts/lint_markdown_doctests.py`, checks that `>>>`
prompts live in `{doctest}` fences — that is about Python doctests, not shell —
and Sphinx's doctest builder runs `{doctest}` blocks only.

So these can rot with a green docs gate:

- **Flag drift.** `pip download --python-version/--platform/--only-binary :all:`
  and `uv run --no-project --python "$PYVER" --with pip`. A deprecation or
  rename in pip or uv silently invalidates the recipe. Most likely failure,
  and the most damaging, given how subtle `--python-version`'s semantics
  already are.
- **The `pypiserver` path.** Outside the matrix gate entirely.
- **`pip download uv`**, for bringing uv across. uv is not a runtime
  dependency, so it sits outside the closure the gate walks.
- **The `otto_sh-${VERSION}-py3-none-any.whl` filename pattern.**
- **End to end**: that a bundle the recipe builds actually installs offline
  into a clean interpreter.

## The constraint any fix must respect

`make docs` runs offline today and rides `make release`. A true air-gap round
trip needs network for the download half. Putting that in the docs gate makes
`make docs` require network — a regression, not an improvement. So the work
splits three ways:

1. **Flag-existence check, in `docs-lint`.** Extract every `pip`/`uv`
   invocation from installation.md's fences and assert each flag still exists
   in the installed tool's `--help`. Offline, milliseconds, catches the
   likeliest rot, and fails the docs gate.
2. **Widen the static gate** to the pypiserver path, the uv bundle, and the
   wheel filename pattern, so the gate's scope matches the document's scope.
3. **A real bundle round trip**, out of band: one interpreter, `pip download`
   into a tmpdir, `pip install --no-index --find-links` into a throwaway venv
   with the network cut, assert `otto --version`. Only this proves the steps
   work. Needs network for half of it, so it belongs in a nox session run
   nightly, not in `make docs`.

**Recommendation: 1 and 2 now, 3 nightly.** Be clear about what each buys:
1 and 2 make stale *claims and flags* impossible to miss; only 3 proves the
*procedure*. Anyone who wants the guarantee stated as "the steps cannot go
stale without failing the docs gate" should know that 3 is the only layer that
delivers it, and that it costs `make docs` its offline property.

## Not to be confused with

`scripts/check_airgap.sh` is unrelated: it gates the built **web bundle**
against external CDN references (`Makefile:456-458`, `511`), so an air-gapped
install ships a frontend that renders without the internet. Same adjective,
different subject.
