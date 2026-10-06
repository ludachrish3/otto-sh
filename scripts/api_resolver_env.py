"""The environment a resolver subprocess runs in, so it imports the tree under test.

A leaf module: it imports nothing from ``scripts``. ``scripts/api_agreement.py``
and ``scripts/check_breaking_marks.py`` both start children with it.
"""

import os
from pathlib import Path


def resolver_env(repo: Path) -> dict[str, str]:
    """Return the resolver subprocess's environment, with *repo*'s ``src/`` first.

    otto is a src-layout project installed into a virtualenv, so a bare
    interpreter imports it through the venv's ``.pth`` file -- which names ONE
    absolute source directory, not "wherever you happen to be". Running the
    child with ``cwd`` set to the tree under test therefore proves nothing:
    from a worktree it would still answer for the primary checkout. Putting
    that tree's ``src/`` on ``PYTHONPATH`` does prove it, because
    ``PYTHONPATH`` precedes site-packages on ``sys.path``. Any inherited
    ``PYTHONPATH`` is kept, after ours.
    """
    src = str(Path(repo) / "src")
    inherited = os.environ.get("PYTHONPATH", "")
    return {**os.environ, "PYTHONPATH": src + os.pathsep + inherited if inherited else src}
