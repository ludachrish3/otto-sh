"""Where a test's artifacts go: the pytest test ID, as directories (spec §5.3)."""

import dataclasses
import re
from pathlib import Path

import pytest


def sanitize_node_name(name: str) -> str:
    """Replace filesystem-unsafe characters from parametrized test names.

    ``test_foo[router-True]`` becomes ``test_foo_router-True_``.
    """
    return re.sub(r'[\[\]/<>:"|?*\\]', "_", name)


@dataclasses.dataclass(frozen=True)
class ArtifactLayout:
    """Maps a test to its artifact directories under one run (or one repo's part of it).

    ``root`` is the run's output directory, or ``<output dir>/<repo name>``
    when more than one repo takes part in the run (built once per pytest
    session in :mod:`otto.suite.run`). ``test_roots`` are that session's test
    directories (a repo's ``Repo.tests``); they disambiguate two modules that
    share a basename in different packaged test directories — see
    :meth:`module_dir`. Neither method below creates a directory — the
    ``module_dir``/``test_dir`` fixtures do that, on request, like pytest's
    own ``tmp_path``.
    """

    root: Path
    test_roots: list[Path] = dataclasses.field(default_factory=list)

    def module_dir(self, module_path: Path) -> Path:
        """``<root>/<module's path relative to its test root, suffix dropped>``.

        The module's test root is the DEEPEST directory in ``test_roots``
        that actually contains it — deepest so a root nested inside another
        (an unusual but legal ``Repo.tests`` shape) wins over its ancestor.
        A module directly in that root keeps the flat ``<root>/<stem>``
        shape unchanged; one two-or-more levels down (``tests/a/b/test_x.py``
        under root ``tests``) becomes ``<root>/a/b/test_x``, which is what
        keeps two same-named modules in different packaged test directories
        (``pa/test_same.py`` and ``pb/test_same.py``) from landing on the
        same ``module_dir``. A module under no configured test root — or
        with ``test_roots`` empty — falls back to ``<root>/<stem>``.
        """
        containing = [
            candidate
            for candidate in self.test_roots
            if candidate.is_dir() and module_path.is_relative_to(candidate)
        ]
        if not containing:
            return self.root / module_path.stem
        deepest = max(containing, key=lambda candidate: len(candidate.parts))
        return self.root / module_path.relative_to(deepest).with_suffix("")

    def test_dir(self, item: pytest.Item) -> Path:
        """``<module_dir>/<Class>/.../<sanitized test name>``, classes outermost first."""
        classes = [node.name for node in item.listchain() if isinstance(node, pytest.Class)]
        return self.module_dir(item.path).joinpath(*classes, sanitize_node_name(item.name))
