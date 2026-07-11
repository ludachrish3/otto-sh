"""Suite-less test selection — resolve ``--tests`` / ``-m`` against collected tests.

This module holds the pure selection-resolution logic that ``otto test``
carried inline in ``otto.cli.test`` before it moved here (library-extraction
Phase A): matching ``--tests`` names against every repo's collected tests
(:func:`resolve_selection`) and narrowing repos by a marker expression alone
(:func:`repos_with_marker_matches`). Extracting it here lets a suite-less
selection be resolved as a plain library call, independent of Typer.

This module never imports ``typer`` — the library raises library exceptions
(:class:`UnknownSelectionError` for a genuinely unknown test name); the CLI
adapter in ``otto.cli.test`` owns the translation to ``typer.BadParameter``.
"""

import difflib
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..config.repo import CollectedTest, Repo


@dataclass(frozen=True)
class SelectionMatch:
    """One repo's resolved selection: the repo plus its matched pytest targets.

    ``targets`` are absolute nodeids (from :func:`resolve_selection`) or
    absolute test-directory paths (from the ``-m``-alone branch of
    :func:`otto.suite.run.run_selection`) — always non-empty, since callers
    only construct a :class:`SelectionMatch` for a repo that matched.
    """

    repo: "Repo"
    targets: list[str]


class UnknownSelectionError(ValueError):
    """A requested test name matched nothing despite a non-empty test universe.

    Raised by :func:`resolve_selection` when at least one requested name went
    unmatched while there *were* collected tests to search — i.e. a genuine
    typo, not an empty selection. The message carries the same did-you-mean
    suggestions the CLI has always shown; ``param_hint`` names the CLI flag
    the selection came from so the CLI adapter can re-raise as
    ``typer.BadParameter`` with an identical rendering.

    Subclasses ``ValueError``, so callers that catch a generic ``ValueError``
    for "nothing matched" must catch this first to distinguish the typo case.
    """

    def __init__(self, message: str, *, param_hint: str = "--tests") -> None:
        super().__init__(message)
        self.param_hint = param_hint


def _base_test_name(name: str) -> str:
    """``test_param[a-b]`` → ``test_param`` (parametrization-insensitive match)."""
    return name.partition("[")[0]


def _absolute_nodeid(item: "CollectedTest") -> str:
    """Rebuild a collected test's nodeid with an absolute file path.

    ``CollectedTest.nodeid`` (from pytest's own ``item.nodeid``) is relative
    to the collection rootdir chosen by :meth:`Repo.collect_tests` — not
    otto's own process cwd — so it cannot be handed to a later, independent
    ``pytest.main()`` call. ``CollectedTest.path`` is always absolute, so
    rebuild the ``path::Class::name`` (or ``path::name``) suffix from it.
    """
    suffix = item.nodeid.split("::", 1)[1] if "::" in item.nodeid else ""
    return f"{item.path}::{suffix}" if suffix else str(item.path)


def resolve_selection(
    repos: "list[Repo]", names: list[str], markers: str
) -> "list[SelectionMatch]":
    """Resolve --tests names to exact nodeids, one entry per matching repo.

    A bare name matches every collected test with that function name (all
    parametrizations); ``Class::name`` restricts to one suite. Unknown names
    raise :class:`UnknownSelectionError` with did-you-mean suggestions — never
    a silent empty run — but only when there was an actual test universe to
    search (at least one collected test across every searched repo). With no
    repos, or repos that collected nothing, there is nothing to suggest, so
    this returns an empty list instead: the generic "no tests matched the
    selection" failure (raised by callers such as
    :func:`otto.suite.run.run_selection`) is the more honest message than a
    did-you-mean hint with no hints to offer.
    """
    per_repo: list[SelectionMatch] = []
    matched: set[str] = set()
    seen_names: set[str] = set()
    for repo in repos:
        items = repo.collect_tests(markers=markers or None)
        nodeids: list[str] = []
        for item in items:
            base = _base_test_name(item.name)
            seen_names.add(base)
            if item.cls_name:
                seen_names.add(f"{item.cls_name}::{base}")
            for wanted in names:
                cls_part, _, name_part = wanted.rpartition("::")
                if base == name_part and (not cls_part or item.cls_name == cls_part):
                    nodeids.append(_absolute_nodeid(item))
                    matched.add(wanted)
                    break
        if nodeids:
            per_repo.append(SelectionMatch(repo=repo, targets=nodeids))

    unknown = [n for n in names if n not in matched]
    if unknown and seen_names:
        hints = []
        for n in unknown:
            close = difflib.get_close_matches(n, sorted(seen_names), n=3)
            hint = f" (did you mean: {', '.join(close)}?)" if close else ""
            hints.append(f"{n!r}{hint}")
        raise UnknownSelectionError(
            f"no collected test matches: {'; '.join(hints)}", param_hint="--tests"
        )
    return per_repo


def repos_with_marker_matches(repos: "list[Repo]", markers: str) -> "list[Repo]":
    """Filter to repos whose collection has >=1 item matching ``markers``.

    Used by the ``-m``-alone branch of ``run_selection`` so a repo whose
    suites don't carry the given marker never gets a pytest session of its
    own — such a session would collect nothing and exit 5
    (NO_TESTS_COLLECTED), which previously failed the whole multi-repo run
    via ``worst = max(worst, rc)`` even when every other repo matched fine.
    """
    return [repo for repo in repos if repo.collect_tests(markers=markers or None)]
