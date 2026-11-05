"""otto's user-level home root and the default xdir: the one reader of ``OTTO_HOME``.

The home is ``$OTTO_HOME`` when that is set and non-empty, else ``~/.otto``,
with ``~`` expanded either way. Every path under the home starts here: the
workspace keying below it is :mod:`otto.config.home`'s, and the default run
tree, :func:`default_xdir`, is ``<home>/otto-xdir``. The amended contract is
that everything under the home is derived state otto may delete and rebuild,
EXCEPT ``otto-xdir/``, which holds run outputs and is pruned only by
``--log-days`` retention, inside itself, and ``tls/``, the per-user location a
``[monitor]`` table's certificate settings point at, which otto never creates.

A foundation leaf on purpose. The host layer (``Host.log_dest``) needs the
default xdir and sits below the configuration layer, so the owner lives down
here rather than being derived twice. It imports only :mod:`os` and
:mod:`typing` at module level, because the completion shim calls
:func:`home_dir` on every TAB and must not pay for :mod:`pathlib`
(``.ruff.toml`` exempts this file from ``PTH``); the functions that return a
:class:`~pathlib.Path` import it when they run.
"""

import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

HOME_ENV_VAR = "OTTO_HOME"
"""The environment variable that relocates otto's home wholesale."""

XDIR_DIRNAME = "otto-xdir"
"""The directory under the home that is the default xdir (run outputs, never derived state)."""

_DEFAULT_HOME = os.path.join("~", ".otto")


def home_dir(environ: "Mapping[str, str]") -> str:
    """Return the home *environ* names: ``OTTO_HOME``, else ``~/.otto``, ``~`` expanded.

    The one derivation, over an injected environment so the completion shim
    can hand it the environment it was given. An empty ``OTTO_HOME`` counts as
    unset, as an empty ``OTTO_*`` variable does everywhere else in otto.
    """
    return os.path.expanduser(environ.get(HOME_ENV_VAR) or _DEFAULT_HOME)


def home_from_env() -> "Path | None":
    """Return ``$OTTO_HOME`` expanded, or ``None`` when it is unset or empty.

    The answer to "did ``OTTO_HOME`` decide the home", for a caller that labels
    where the home came from.
    """
    from pathlib import Path

    if not os.environ.get(HOME_ENV_VAR):
        return None
    return Path(home_dir(os.environ)).expanduser()


def otto_home() -> "Path":
    """Return otto's user-level home: ``$OTTO_HOME`` if set, else ``~/.otto``.

    PURE -- never creates the directory. Creation belongs to whoever writes
    into it, at the moment it writes, so a read-only code path (completion,
    ``--help``) can ask where the home *would* be without making one.

    Raises:
        RuntimeError: ``~`` cannot be resolved (no ``HOME`` and no passwd
            entry); :meth:`pathlib.Path.expanduser` refuses what
            :func:`os.path.expanduser` would leave unexpanded.
    """
    from pathlib import Path

    return Path(home_dir(os.environ)).expanduser()


def default_xdir() -> "Path":
    """Return the default xdir, ``<home>/otto-xdir``, for the CLI and library calls alike.

    Read per call, so a changed ``OTTO_HOME`` is a new answer. PURE: the
    directory is created by the first run that writes into it.
    """
    return otto_home() / XDIR_DIRNAME
