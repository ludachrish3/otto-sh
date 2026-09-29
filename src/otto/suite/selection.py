"""Test selection: the name rule and the unknown-name message.

The pure selection logic behind ``otto test`` and :func:`otto.suite.run.run_tests`:
the one rule for whether a requested name (a test, a class, or a
``Class::test`` path) selects a test (:func:`matches_name`), and the
did-you-mean message for a name nothing matched
(:func:`unknown_names_message`). The matching itself happens inside the run's
own pytest session (:class:`otto.suite.plugin.OttoPlugin`). It is a plain
library, independent of Typer.

This module never imports ``typer`` — the library raises library exceptions
(:class:`UnknownSelectionError` for a genuinely unknown test name); the CLI
adapter in ``otto.cli.test`` owns the translation to ``typer.BadParameter``.
"""

import difflib

from ..errors import OttoError


class UnknownSelectionError(OttoError, ValueError):
    """A requested test name matched nothing despite a non-empty test universe.

    Raised by :func:`otto.suite.run.run_tests` when at least one requested
    name matched no test in any repo's whole tree while there *were*
    collected tests to search — i.e. a genuine typo, not an empty selection.
    The message carries the same did-you-mean suggestions the CLI has always
    shown; ``param_hint`` names the CLI parameter the selection came from so
    the CLI adapter can re-raise as ``typer.BadParameter`` with an identical
    rendering.

    Subclasses ``ValueError``, so callers that catch a generic ``ValueError``
    for "nothing matched" must catch this first to distinguish the typo case.
    """

    def __init__(self, message: str, *, param_hint: str = "NAMES") -> None:
        super().__init__(message)
        self.param_hint = param_hint


def base_test_name(name: str) -> str:
    """``test_param[a-b]`` → ``test_param`` (parametrization-insensitive match)."""
    return name.partition("[")[0]


def matches_name(wanted: str, classes: list[str], name: str) -> bool:
    """Whether the requested name *wanted* selects the test *name* nested in *classes*.

    *classes* is the test's enclosing classes, outermost first, and *name* its
    collected name (a parametrization suffix is ignored). *wanted* is split on
    ``::``; it matches when its parts are a contiguous run of
    ``classes + [base name]`` that either ends at the test (naming the test,
    optionally qualified by the classes right above it) or lies wholly inside
    *classes* (naming a class, whose every test it selects, at any depth).

    So for a ``test_x`` in ``TestOuter::TestInner``: ``test_x``,
    ``TestInner::test_x``, ``TestOuter::TestInner::test_x``, ``TestInner`` and
    ``TestOuter`` all match, while ``TestOuter::test_x`` does not -- it would
    name a ``test_x`` defined directly in ``TestOuter``.

    Nothing ranks one reading over the other: a name that is both a class's
    name and a test's name (possible under a custom ``python_functions``)
    selects both -- every test in the class and the test of that name.
    """
    parts = wanted.split("::")
    full = [*classes, base_test_name(name)]
    width = len(parts)
    for start in range(len(full) - width + 1):
        end = start + width
        if full[start:end] == parts and (end == len(full) or end <= len(classes)):
            return True
    return False


def unknown_names_message(
    unknown: list[str], known: "set[str]", *, broken: list[str] | None = None
) -> str:
    """Say which of *unknown* matched no collected test, each with its did-you-mean.

    *known* is every name the collected tests answer to
    (:func:`otto.config.repo.selectable_names`). *broken* says, one line per
    file, which files did not collect (a name in one of them is unknown
    until it does). One sentence for a run and for ``--list-tests`` alike.
    """
    hints = []
    for n in unknown:
        close = difflib.get_close_matches(n, sorted(known), n=3)
        hint = f" (did you mean: {', '.join(close)}?)" if close else ""
        hints.append(f"{n!r}{hint}")
    return "; ".join([f"no collected test matches: {'; '.join(hints)}", *(broken or [])])
